"""Two cancellable image workers; the Tk owner polls the result queue."""
from queue import Queue
import threading
import uuid

from diagnostics import record
from http_transport import abort_io
from image_codec import ImageLoadError, decode_image


class ReplyImages:
    def __init__(self):
        self.tasks, self.results = Queue(), Queue()
        self.lock = threading.Lock()
        self.jobs = {}
        self.threads = []
        self.closed = False
        self.generation = 0

    def submit(self, key, revision, descriptor, fetch):
        with self.lock:
            if self.closed:
                return
            previous = self.jobs.get(key)
            if previous:
                previous['cancel'].set()
                abort_io(previous)
            job = {'id':uuid.uuid4().hex,'cancel':threading.Event()}
            self.jobs[key] = job
            if not self.threads:
                self.threads = [threading.Thread(target=self.run, name='desktop-ai-image', daemon=True) for _ in range(2)]
                for thread in self.threads:
                    thread.start()
            self.tasks.put((self.generation, key, revision, descriptor, fetch, job))

    def run(self):
        while True:
            task = self.tasks.get()
            if task is None:
                return
            generation, key, revision, descriptor, fetch, job = task
            if job['cancel'].is_set():
                task = descriptor = fetch = job = None
                continue
            loaded, error = None, None
            try:
                loaded = decode_image(fetch(descriptor, job))
                record('desktop.image_loaded', job['id'], bytes=len(loaded.raw), width=loaded.width, height=loaded.height)
            except Exception as failure:
                if not job['cancel'].is_set():
                    error = str(failure) if isinstance(failure, ImageLoadError) else '图片暂未加载，请重试或在网页查看。'
                    record('desktop.image_failed', job['id'], error=type(failure).__name__)
            with self.lock:
                if self.jobs.get(key) is job:
                    self.jobs.pop(key, None)
                if not self.closed and not job['cancel'].is_set() and generation == self.generation:
                    self.results.put((generation,key,revision,loaded,error))
            task = loaded = error = descriptor = fetch = job = None

    def clear(self):
        with self.lock:
            self.generation += 1
            jobs, self.jobs = list(self.jobs.values()), {}
        for job in jobs:
            job['cancel'].set()
            abort_io(job)
        while not self.results.empty():
            self.results.get_nowait()

    def pending(self):
        with self.lock:
            return bool(self.jobs) or not self.results.empty()

    def close(self):
        self.clear()
        with self.lock:
            self.closed = True
            for _ in self.threads:
                self.tasks.put(None)
