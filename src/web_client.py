from base64 import b64decode, b64encode
from datetime import date
import http.client
import json
from pathlib import Path
from queue import Queue
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).with_name("_vendor")))
import wasmtime

from diagnostics import record
from web_account import load_account, save_account


HOST = "chat.deepseek.com"
COMPLETION = "/api/v0/chat/completion"
UPLOAD = "/api/v0/file/upload_file"
WASM = Path(__file__).resolve().parents[1] / "assets" / "deepseek-pow.wasm"


class WebError(Exception):
    pass


class LoginRequired(WebError):
    pass


class Cancelled(Exception):
    pass


class Stream:
    def __init__(self):
        self.state = {}
        self.path = ""
        self.operation = "SET"
        self.parent = None
        self.finished = False
        self.error = None

    def patch(self, patch, prefix="", cursor=None):
        if not isinstance(patch, dict):
            return
        cursor = cursor if cursor is not None else [self.path, self.operation]
        cursor[0] = patch.get("p", cursor[0])
        cursor[1] = patch.get("o", cursor[1])
        path, operation = cursor
        parts = [part for part in (prefix + path).split("/") if part]
        if operation == "BATCH" and isinstance(patch.get("v"), list):
            child_cursor = ["", "SET"]
            for child in patch["v"]:
                self.patch(child, "/".join(parts) + "/" if parts else "", child_cursor)
        elif not parts:
            if operation == "SET" and isinstance(patch.get("v"), dict):
                self.state = patch["v"]
        else:
            node = self.state
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
            key = int(parts[-1]) if isinstance(node, list) else parts[-1]
            if operation == "SET":
                node[key] = patch.get("v")
            elif operation == "APPEND":
                node[key] += patch["v"]
        if prefix == "" and cursor is not None:
            self.path, self.operation = path, operation

    def event(self, name, data):
        if name in ("finish", "close") or data == "[DONE]":
            self.finished = True
            return
        value = json.loads(data)
        if name == "ready":
            self.parent = value.get("response_message_id")
        elif name in ("hint", "toast", "error") and value.get("type") == "error":
            self.error = value.get("content", "DeepSeek 网页接口返回错误。")
        elif name in ("", "delta", "message") and "v" in value:
            self.patch(value)

    def content(self):
        fragments = self.state.get("response", self.state).get("fragments", [])
        answer = "\n".join(item.get("content", "") for item in fragments
                           if item.get("type") in ("RESPONSE", "TEMPLATE_RESPONSE"))
        thought = "\n".join(item.get("content", "") for item in fragments if item.get("type") == "THINK")
        searches = [item for item in fragments if item.get("type") in ("SEARCH", "TOOL_SEARCH")]
        search = "searching" if any(item.get("status") == "WIP" for item in searches) and not answer else "done" if searches else ""
        return {"answer": answer[:120000], "thought": thought[:120000], "search": search}


class WebClient:
    def __init__(self, account=None):
        self.account = dict(account) if account is not None else load_account()
        if account is None and not self.account.get("session_id"):
            raise ValueError("本机保存的会话信息不完整，请重新导入登录。")
        if account is None and self.account.get("session_id") and not self.account.get("session_date"):
            self.account["session_date"] = date.today().isoformat()
            save_account(self.account)
        self.events = Queue()
        self.active_request = None
        self.active = None
        self.lock = threading.Lock()
        self.engine = wasmtime.Engine()
        self.module = wasmtime.Module.from_file(self.engine, str(WASM))
        self.connected = True

    def connect_account(self):
        day = date.today().isoformat()
        job = {"id": uuid.uuid4().hex, "day": day, "cancel": threading.Event()}
        if self.account.get("session_id") and self.account.get("session_date") == day:
            query = urlencode({"chat_session_id": self.account["session_id"], "cache_version": 0})
            self.json_request("/api/v0/chat/history_messages?" + query, job=job)
            save_account(self.account)
        else:
            self.daily_session(job)
        record("web.account_connected")

    def emit(self, job, kind, **fields):
        self.events.put({"id": job["id"], "type": kind, **fields})

    def enqueue(self, message):
        if message["type"] == "stop":
            self.stop(message["id"])
            return
        with self.lock:
            if self.active is not None:
                self.events.put({"type": "error", "id": message["id"], "text": "上一条请求正在结束，请稍后重试。"})
                return
            job = {**message, "day": date.today().isoformat(), "cancel": threading.Event(),
                   "connection": None, "submitted": False, "parent": None}
            self.active = job
        threading.Thread(target=self.run, args=(job,), daemon=True).start()

    def open_request(self, path, body=None, headers=None, job=None):
        if job and job["cancel"].is_set():
            raise Cancelled()
        options = {
            "Authorization": "Bearer " + self.account["token"],
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://" + HOST + "/",
            "x-client-platform": "web",
            "Accept": "*/*",
        }
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            options["Content-Type"] = "application/json"
        options.update(headers or {})
        connection = http.client.HTTPSConnection(HOST, timeout=35, context=ssl.create_default_context())
        if job is not None:
            job["connection"] = connection
        try:
            connection.request("POST" if body is not None else "GET", path, body=body, headers=options)
            response = connection.getresponse()
            if response.status in (401, 403):
                if job:
                    record("web.http_error", job["id"], status=response.status, path=path.split("?")[0])
                raise LoginRequired("网页登录状态已失效或需要验证，请在 Edge 登录后重新导入登录状态。")
            if not 200 <= response.status < 300:
                if job:
                    record("web.http_error", job["id"], status=response.status, path=path.split("?")[0])
                raise WebError(f"DeepSeek 网页接口返回 HTTP {response.status}，请稍后重试。")
            return connection, response
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            if job:
                record("web.network_error", job["id"], error=type(error).__name__)
            raise WebError("连接 DeepSeek 失败，请检查网络后重试。") from None
        except BaseException:
            connection.close()
            raise

    def json_request(self, path, body=None, headers=None, job=None):
        connection, response = self.open_request(path, body, headers, job)
        try:
            value = json.load(response)
        finally:
            connection.close()
        if value.get("code") != 0 or value.get("data", {}).get("biz_code") != 0:
            if job:
                record("web.api_error", job["id"], path=path.split("?")[0], code=value.get("code"),
                       business_code=value.get("data", {}).get("biz_code"))
            raise WebError(str(value.get("data", {}).get("biz_msg") or value.get("msg") or "DeepSeek 请求未成功。")[:300])
        return value["data"]["biz_data"]

    def proof(self, target, job):
        challenge = self.json_request("/api/v0/chat/create_pow_challenge", {"target_path": target}, job=job)["challenge"]
        if challenge.get("algorithm") != "DeepSeekHashV1":
            raise WebError("DeepSeek 网页的计算校验已变化，需要更新程序。")
        store = wasmtime.Store(self.engine)
        exports = wasmtime.Instance(store, self.module, []).exports(store)
        def put(value):
            raw = value.encode("utf-8")
            pointer = exports["__wbindgen_export_0"](store, len(raw), 1)
            exports["memory"].write(store, raw, pointer)
            return pointer, len(raw)
        output = exports["__wbindgen_add_to_stack_pointer"](store, -16)
        a, b = put(challenge["challenge"])
        c, d = put(f'{challenge["salt"]}_{challenge["expire_at"]}_')
        exports["wasm_solve"](store, output, a, b, c, d, float(challenge["difficulty"]))
        result = exports["memory"].read(store, output, output + 16)
        if not struct.unpack_from("<i", result)[0]:
            raise WebError("DeepSeek 计算校验未完成，请重试。")
        answer = int(struct.unpack_from("<d", result, 8)[0])
        proof = {name: challenge[name] for name in ("algorithm", "challenge", "salt", "signature")}
        proof.update(answer=answer, target_path=target)
        return {"X-DS-PoW-Response": b64encode(json.dumps(proof, separators=(",", ":")).encode()).decode("ascii")}

    def upload(self, job, model):
        self.emit(job, "progress", text="正在上传图片")
        data = b64decode(job["image"], validate=True)
        boundary = "DesktopAI" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="image.png"\r\n'
                'Content-Type: image/png\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = self.proof(UPLOAD, job)
        headers.update({"Content-Type": "multipart/form-data; boundary=" + boundary,
                        "x-thinking-enabled": "1" if job["thinking"] else "0",
                        "x-model-type": model, "x-file-size": str(len(data))})
        value = self.json_request(UPLOAD, body, headers, job)
        identifier = value.get("id")
        if not identifier:
            raise WebError("图片上传后未返回附件编号，请重试。")
        deadline = time.monotonic() + 45
        while value.get("status") in ("PENDING", "PARSING") and time.monotonic() < deadline:
            if job["cancel"].wait(.5):
                raise Cancelled()
            result = self.json_request("/api/v0/file/fetch_files?" + urlencode({"file_ids": identifier}), job=job)
            value = next((item for item in result.get("files", []) if item["id"] == identifier), value)
        if value.get("status") not in ("SUCCESS", "CONTENT_EMPTY") or value.get("audit_result") == "reject":
            raise WebError("DeepSeek 未完成图片处理，请稍后重试。")
        return identifier

    def daily_session(self, job):
        if not self.account.get("session_id") or self.account.get("session_date") != job["day"]:
            self.emit(job, "progress", text="正在开启今日会话")
            result = self.json_request("/api/v0/chat_session/create", {}, job=job)
            session_id = result.get("chat_session", {}).get("id")
            try:
                uuid.UUID(session_id)
            except (ValueError, TypeError, AttributeError):
                raise WebError("DeepSeek 未返回有效的新会话，请稍后重试。") from None
            account = {**self.account, "session_id": session_id, "session_date": job["day"]}
            try:
                save_account(account)
            except OSError:
                raise WebError("新会话信息未能保存，请检查本机存储后重试。") from None
            self.account = account
            record("web.session_created", job["id"], day=job["day"])
        job["session_id"] = self.account["session_id"]
        return job["session_id"]

    def run(self, job):
        connection = None
        try:
            record("web.request_started", job["id"], image=bool(job.get("image")))
            self.emit(job, "progress", text="正在连接 DeepSeek")
            session_id = self.daily_session(job)
            query = urlencode({"chat_session_id": session_id, "cache_version": 0})
            history = self.json_request("/api/v0/chat/history_messages?" + query, job=job)
            session = history["chat_session"]
            model = session.get("model_type") or "default"
            files = [self.upload(job, model)] if job.get("image") else []
            if files and not files[0]:
                raise WebError("图片上传后未返回附件编号，请重试。")
            self.emit(job, "progress", text="正在发送")
            headers = self.proof(COMPLETION, job)
            payload = {
                "chat_session_id": session_id,
                "parent_message_id": session.get("current_message_id"),
                "model_type": model, "prompt": job["prompt"], "ref_file_ids": files,
                "thinking_enabled": job["thinking"], "search_enabled": job["search"], "action": None,
            }
            if job["cancel"].is_set():
                raise Cancelled()
            job["submitted"] = True
            connection, response = self.open_request(COMPLETION, payload, headers, job)
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                value = json.load(response)
                record("web.api_error", job["id"], path=COMPLETION, code=value.get("code"),
                       business_code=value.get("data", {}).get("biz_code"))
                description = str(value.get("data", {}).get("biz_msg") or value.get("msg") or "DeepSeek 未开始回复。")[:300]
                if "still wip" in description.lower():
                    description = "这个会话里还有回复正在生成，请等结束后再发送。"
                raise WebError(description)
            record("web.stream_started", job["id"])
            self.emit(job, "started")
            stream = Stream()
            event, data = "", []
            previous = None
            for raw in response:
                if job["cancel"].is_set():
                    raise Cancelled()
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    stream.event(event, "\n".join(data))
                    job["parent"] = stream.parent
                    content = stream.content()
                    if content != previous and any(content.values()):
                        self.emit(job, "delta", **content)
                        previous = content
                    event, data = "", []
            if data:
                stream.event(event, "\n".join(data))
            if job["cancel"].is_set():
                raise Cancelled()
            if stream.error:
                raise WebError(stream.error)
            content = stream.content()
            if not stream.finished or not content["answer"].strip():
                raise WebError("DeepSeek 回复中断，已收到的内容保留在弹窗中。")
            self.emit(job, "complete", **content)
            record("web.complete", job["id"])
        except Cancelled:
            self.finish_stop(job)
        except Exception as error:
            if job["cancel"].is_set():
                self.finish_stop(job)
            else:
                record("web.error", job["id"], error=type(error).__name__)
                description = str(error) if isinstance(error, WebError) else "连接 DeepSeek 超时或中断，请重试。"
                self.emit(job, "login" if isinstance(error, LoginRequired) else "error", text=description)
        finally:
            if connection:
                connection.close()
            with self.lock:
                if self.active is job:
                    self.active = None

    def stop(self, request_id):
        with self.lock:
            job = self.active
        if job is None or job["id"] != request_id:
            self.events.put({"type": "stopped", "id": request_id, "confirmed": True})
            return
        job["cancel"].set()
        connection = job.get("connection")
        if connection and connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if connection:
            connection.close()

    def finish_stop(self, job):
        confirmed = not job["submitted"]
        if job["parent"] is not None:
            try:
                self.json_request("/api/v0/chat/stop_stream",
                                  {"chat_session_id": job["session_id"], "message_id": job["parent"]})
                confirmed = True
            except Exception:
                pass
        self.emit(job, "stopped", confirmed=confirmed)

    def close(self):
        with self.lock:
            job = self.active
        if job:
            self.stop(job["id"])
