"""One unused request check, refreshed in memory every eight minutes."""
from dataclasses import dataclass
import math
import threading
import time
import uuid

from chatgpt_account import ChatGPTError, ChatGPTLoginRequired
from chatgpt_client import Cancelled
from diagnostics import record
from http_transport import abort_io


REFRESH_SECONDS = 8 * 60


def positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass(repr=False)
class PreparedCheck:
    headers: dict
    identity: bytes
    monotonic: float
    wall: float
    expires_at: float | None = None
    expires_after: float | None = None

    def remaining(self, monotonic, wall, fallback):
        lifetime = max(0, self.expires_after-10) if self.expires_after is not None else fallback
        remaining = min(lifetime-(monotonic-self.monotonic), lifetime-(wall-self.wall))
        if self.expires_at is not None:
            remaining = min(remaining, self.expires_at-wall-10)
        return remaining

    def refresh_in(self, monotonic, wall, interval):
        due = interval-max(monotonic-self.monotonic, wall-self.wall)
        if self.expires_after is not None or self.expires_at is not None:
            due = min(due, self.remaining(monotonic, wall, interval)-15)
        return due


class PreparedCheckCache:
    def __init__(self, generate, identity, interval=REFRESH_SECONDS,
                 monotonic=time.monotonic, wall=time.time):
        self.generate, self.identity, self.interval = generate, identity, interval
        self.monotonic, self.wall = monotonic, wall
        self.condition = threading.Condition()
        self.thread = self.job = self.prepared = None
        self.enabled = self.closed = self.busy = self.wanted = False
        self.serial = self.failure_serial = self.epoch = 0
        self.last_error = None
        self.failures = 0
        self.retry_at = 0

    def usable(self):
        item = self.prepared
        return bool(item and item.identity == self.identity()
                    and item.remaining(self.monotonic(), self.wall(), self.interval) > 0)

    def start(self):
        with self.condition:
            if self.closed:
                return
            if not self.enabled:
                self.enabled = self.wanted = True
                self.thread = threading.Thread(target=self.run, name='chatgpt-check-cache', daemon=True)
                self.thread.start()
        self.refill(urgent=True)

    def refill(self, urgent=False):
        with self.condition:
            if not self.enabled or self.closed:
                return
            if not self.usable() or self.prepared.refresh_in(self.monotonic(), self.wall(), self.interval) <= 0:
                self.wanted = True
                if urgent:
                    self.retry_at = 0
            self.condition.notify_all()

    def take(self, job, force_new=False):
        started = self.monotonic()
        with self.condition:
            if force_new:
                self.epoch += 1
                self.prepared = None
            requested = None
            while True:
                if self.closed or job['cancel'].is_set():
                    raise Cancelled()
                if job.get('_abort_io'):
                    raise ChatGPTError('连接准备已中断，请重试。')
                if self.usable():
                    item, self.prepared = self.prepared, None
                    record('chatgpt.web_check_cache_taken', job['id'],
                           age_ms=round((self.monotonic()-item.monotonic)*1000),
                           waited_ms=round((self.monotonic()-started)*1000))
                    # Refill after the service accepts this message, keeping
                    # preparation traffic out of the send's critical path.
                    return dict(item.headers)
                self.prepared = None
                if requested is None:
                    requested = self.serial if self.busy and not force_new else self.serial+1
                    self.wanted = True
                    self.retry_at = 0
                    self.condition.notify_all()
                if self.failure_serial >= requested and self.last_error is not None:
                    raise self.last_error
                self.condition.wait(.1)

    def run(self):
        while True:
            with self.condition:
                while True:
                    if self.closed:
                        return
                    if self.prepared and not self.usable():
                        self.prepared = None
                        self.wanted = True
                    due = self.prepared.refresh_in(self.monotonic(), self.wall(), self.interval) if self.prepared else None
                    if due is not None and due <= 0:
                        self.wanted = True
                    if self.wanted and self.monotonic() >= self.retry_at:
                        self.wanted = False
                        self.busy = True
                        self.serial += 1
                        serial, epoch = self.serial, self.epoch
                        job = self.job = {'id': uuid.uuid4().hex, 'cancel': threading.Event(), '_background_check': True}
                        break
                    waits = [30]
                    if due is not None and due > 0:
                        waits.append(due)
                    if self.wanted and self.retry_at > self.monotonic():
                        waits.append(self.retry_at-self.monotonic())
                    self.condition.wait(max(.01, min(waits)))
            record('chatgpt.web_check_prefetch_started', job['id'])
            error = item = None
            try:
                item = self.generate(job)
                if not isinstance(item, PreparedCheck):
                    raise ChatGPTError('ChatGPT 连接准备未完成，请重试。')
                if item.remaining(self.monotonic(), self.wall(), self.interval) <= 0:
                    raise ChatGPTError('ChatGPT 返回的连接校验已过期，请重试。')
            except Exception as failure:
                error = failure if isinstance(failure, ChatGPTError) else ChatGPTError('ChatGPT 连接准备失败，请重试。')
                if not job['cancel'].is_set():
                    record('chatgpt.web_check_prefetch_failed', job['id'], error=type(failure).__name__)
            with self.condition:
                self.busy = False
                self.job = None
                if self.closed:
                    self.condition.notify_all()
                    return
                if epoch != self.epoch or (item is not None and item.identity != self.identity()):
                    self.wanted = True
                    self.retry_at = 0
                    self.last_error = None
                elif error:
                    self.last_error, self.failure_serial = error, serial
                    self.failures += 1
                    delay = min(60, 5 * 2**min(4, self.failures-1))
                    self.retry_at = float('inf') if isinstance(error, ChatGPTLoginRequired) else self.monotonic()+delay
                    self.wanted = True
                else:
                    self.prepared = item
                    self.last_error = None
                    self.failures = 0
                    self.retry_at = 0
                    self.wanted = False
                    record('chatgpt.web_check_prefetch_ready', job['id'], refresh_after_s=self.interval)
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.prepared = None
            self.last_error = None
            job = self.job
            self.condition.notify_all()
        if job:
            job['cancel'].set()
            abort_io(job)
            process = job.get('process')
            if process and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
