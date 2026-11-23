from collections import deque
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from queue import Queue
import re
import secrets
import threading
import time

from diagnostics import record, record_edge


PORT = 41692
ORIGIN = "chrome-extension://phkjcieoiikackdmcbkfbfmmjnookdoo"
MAX_BODY = 1024 * 1024
REPLY_EVENTS = ("started", "complete", "stopped", "login", "error")
ASK_JOBS = ("ask", "ask_image")


def connection_code():
    folder = Path(os.environ["LOCALAPPDATA"]) / "DesktopAI"
    folder.mkdir(exist_ok=True)
    path = folder / "edge-connection"
    if path.exists():
        old = path.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[a-zA-Z0-9_-]{43}", old):
            return old
    token = secrets.token_urlsafe(32)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(token, encoding="ascii")
    temporary.replace(path)
    return token


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if self.headers.get("Origin") == ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def allowed_origin(self):
        return (self.headers.get("Host") == f"127.0.0.1:{PORT}"
                and self.headers.get("Origin", ORIGIN) == ORIGIN)

    def do_OPTIONS(self):
        if not self.allowed_origin() or self.headers.get("Origin") != ORIGIN:
            self.respond(403, None)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_POST(self):
        if not self.allowed_origin():
            self.respond(403, None)
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= MAX_BODY or self.headers.get_content_type() != "application/json":
                self.respond(400, None)
                return
            request = json.loads(self.rfile.read(size))
        except (ValueError, OSError):
            self.respond(400, None)
            return

        bridge = self.server.bridge
        token = request.get("token") if isinstance(request, dict) else None
        if not isinstance(token, str) or not hmac.compare_digest(token.encode("utf-8"), bridge.token.encode("ascii")):
            self.respond(403, None)
            return
        if self.path == "/ping":
            self.respond(200, True)
        elif self.path == "/poll":
            event = request.get("event")
            can_claim = isinstance(event, dict) and event.get("claim") is True
            self.respond(200, bridge.poll_job(can_claim))
        elif self.path == "/claim":
            event = request.get("event")
            claimed = isinstance(event, dict) and bridge.claim_job(event.get("id"))
            self.respond(200, claimed)
        elif self.path == "/event":
            event = request.get("event")
            if not isinstance(event, dict):
                self.respond(400, None)
                return
            kind = event.get("type")
            if kind == "diagnostic":
                accepted = record_edge(event)
                self.respond(200 if accepted else 400, True if accepted else None)
                return
            if kind in REPLY_EVENTS:
                record("bridge.received_" + kind, event.get("id"))
            bridge.events.put(event)
            self.respond(200, True)
        else:
            self.respond(404, None)


class Bridge:
    def __init__(self):
        self.token = connection_code()
        self.last_seen = float("-inf")
        self.active_request = None
        self.jobs = deque()
        self.job_ready = threading.Condition()
        self.events = Queue()
        self.server = ThreadingHTTPServer(("127.0.0.1", PORT), BridgeHandler)
        self.server.bridge = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        record("bridge.listening", port=PORT)

    def enqueue(self, job):
        with self.job_ready:
            self.jobs.append(job)
            self.job_ready.notify_all()

    def poll_job(self, can_claim):
        self.last_seen = time.monotonic()
        with self.job_ready:
            if not self.job_ready.wait_for(lambda: self.jobs, timeout=11):
                return None
            job = self.jobs[0]
            kind, request_id = job["type"], job.get("id")
            if kind in ASK_JOBS:
                if request_id != self.active_request:
                    self.jobs.popleft()
                    record("bridge.skipped_cancelled_ask", request_id)
                    return None
                if can_claim:
                    record("bridge.offered_" + kind, request_id)
                    return {**job, "claimRequired": True}
            self.jobs.popleft()
            if kind in (*ASK_JOBS, "stop"):
                record("bridge.polled_" + kind, request_id)
            return job

    def claim_job(self, request_id):
        with self.job_ready:
            if not isinstance(request_id, str) or request_id != self.active_request or not self.jobs:
                return False
            job = self.jobs[0]
            if job["type"] not in ASK_JOBS or job["id"] != request_id:
                return False
            # Only the extension that obtains this claim may send the question.
            self.jobs.popleft()
            record("bridge.claimed_" + job["type"], request_id)
            self.events.put({"type": "received", "id": request_id})
            return True

    @property
    def connected(self):
        return time.monotonic() - self.last_seen < 22

    def close(self):
        self.server.server_close()
