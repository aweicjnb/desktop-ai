import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import threading
import time

from diagnostics import record, record_edge


PORT = 41692
ORIGIN = "chrome-extension://phkjcieoiikackdmcbkfbfmmjnookdoo"
MAX_BODY = 1024 * 1024
REPLY_EVENTS = ("started", "complete", "stopped", "login", "error")


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
            bridge.last_seen = time.monotonic()
            try:
                job = bridge.jobs.get(timeout=11)
            except Empty:
                job = None
            if job and job.get("type") == "ask" and job.get("id") != bridge.active_request:
                record("bridge.skipped_cancelled_ask", job.get("id"))
                job = None
            if job and job.get("type") in ("ask", "stop"):
                record("bridge.polled_" + job["type"], job.get("id"))
            self.respond(200, job)
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
        self.jobs = Queue()
        self.events = Queue()
        self.server = ThreadingHTTPServer(("127.0.0.1", PORT), BridgeHandler)
        self.server.bridge = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        record("bridge.listening", port=PORT)

    @property
    def connected(self):
        return time.monotonic() - self.last_seen < 22

    def close(self):
        self.server.server_close()
