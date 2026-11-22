import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import traceback


logger = logging.getLogger("desktop_ai")
logger.propagate = False
request_pattern = re.compile(r"[0-9a-f]{32}")

EDGE_STAGES = frozenset({
    "job_received", "page_ready", "page_login", "page_unavailable", "hook_ready",
    "composer_accepted", "composer_rejected", "send_failed", "already_busy", "tab_closed",
    "no_stream", "stream_login", "stream_no_reply", "stream_interrupted", "reply_timeout",
    "session_save_failed", "relay_retry", "relay_failed",
})


def start_logging():
    directory = Path(os.environ["LOCALAPPDATA"]) / "DesktopAI"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / "diagnostics.log", maxBytes=512 * 1024,
                                      backupCount=2, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def valid_request_id(value):
    return isinstance(value, str) and request_pattern.fullmatch(value) is not None


def record(stage, request_id=None, **fields):
    parts = [stage]
    if valid_request_id(request_id):
        parts.append(f"request={request_id}")
    parts.extend(f"{key}={value}" for key, value in fields.items())
    logger.info(" ".join(parts))


def record_edge(event):
    stage, request_id = event.get("stage"), event.get("id")
    if not isinstance(stage, str) or stage not in EDGE_STAGES or not valid_request_id(request_id):
        return False
    status = event.get("http_status")
    fields = {"http_status": status} if type(status) is int and (status == 0 or 100 <= status <= 599) else {}
    record("edge." + stage, request_id, **fields)
    return True


def record_callback_error(error_type, stack, request_id=None):
    frames = traceback.extract_tb(stack)
    location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
    record("desktop.callback_error", request_id, error=error_type.__name__, location=location)
