"""Streaming Responses API transport with a local, daily ChatGPT conversation."""
from datetime import date
import hashlib
import json
from queue import Queue
import socket
import threading
import time

from chatgpt_account import (
    ChatGPTError, ChatGPTLoginRequired, RESOURCE, data_directory, fresh_account,
    json_request, load_account, open_request, read_protected, safe_code, write_protected,
)
from diagnostics import record


MAX_HISTORY_BYTES = 48 * 1024 * 1024


class Cancelled(Exception):
    pass


def events(response):
    data, size = [], 0
    for raw in response:
        if len(raw) > 4 * 1024 * 1024:
            raise ChatGPTError("ChatGPT 返回的数据过大，已保留收到的回答。")
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            text = line[5:].lstrip()
            size += len(text)
            if size > 4 * 1024 * 1024:
                raise ChatGPTError("ChatGPT 返回的数据过大，已保留收到的回答。")
            data.append(text)
        elif not line and data:
            joined = "\n".join(data)
            if joined != "[DONE]":
                yield json.loads(joined)
            data, size = [], 0
    if data:
        joined = "\n".join(data)
        if joined != "[DONE]":
            yield json.loads(joined)


class ResponseStream:
    def __init__(self):
        self.parts = {}
        self.completed = False
        self.response = None
        self.search = ""
        self.error = None

    def event(self, event):
        kind = event.get("type", "")
        key = (event.get("output_index", 0), event.get("content_index", 0))
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            self.parts[key] = self.parts.get(key, "") + event.get("delta", "")
        elif kind in {"response.output_text.done", "response.refusal.done"}:
            self.parts[key] = event.get("text", event.get("refusal", self.parts.get(key, "")))
        elif kind.startswith("response.web_search_call."):
            self.search = "done" if kind.endswith("completed") else "searching"
        elif kind == "response.completed":
            self.response = event.get("response", {})
            if self.response.get("status") != "completed":
                self.error = "ChatGPT 回复未完成，已保留收到的内容。"
                return
            self.completed = True
            self.search = "done" if self.search else ""
            # Terminal content is authoritative and may carry multiple messages.
            messages = []
            citations = []
            for item in self.response.get("output", []):
                if item.get("type") != "message":
                    continue
                message = []
                for part in item.get("content", []):
                    message.append(part.get("text", part.get("refusal", "")))
                    for citation in part.get("annotations", []):
                        if citation.get("type") == "url_citation" and citation.get("url", "").startswith(("https://", "http://")):
                            pair = (str(citation.get("title") or citation["url"]), citation["url"])
                            if pair not in citations:
                                citations.append(pair)
                messages.append("".join(message))
            if messages:
                answer = "\n\n".join(messages)
                if citations:
                    # Reuse the existing safe Markdown link renderer.
                    answer += "\n\n来源\n" + "\n".join(
                        f"- [{title.replace('[', '').replace(']', '')}]({url.replace(')', '%29').replace(' ', '%20')})"
                        for title, url in citations)
                self.parts = {(0, 0): answer}
        elif kind in {"response.failed", "response.incomplete", "error"}:
            response = event.get("response", {})
            error = response.get("error") or event.get("error") or event
            code = safe_code(error.get("code")) if isinstance(error, dict) else "unknown"
            record("chatgpt.stream_failed", code=code)
            if code == "subscription_sharing_usage_limit_exceeded":
                self.error = "ChatGPT 使用额度已达限制，请在 ChatGPT 的用量设置中查看。"
            elif code == "subscription_sharing_user_not_eligible":
                self.error = "当前 ChatGPT 账号或工作区暂不支持授权其他应用使用套餐。"
            else:
                self.error = f"ChatGPT 回复中断（{code}），已保留收到的内容。"

    def content(self):
        return {"answer": "\n".join(value for _, value in sorted(self.parts.items())),
                "thought": "", "search": self.search}


class ChatGPTClient:
    provider = "chatgpt"

    def __init__(self, account=None, model=None):
        self.account = dict(account) if account is not None else load_account()
        self.client_id = self.account["client_id"]
        self.models = self.account.get("models", [])
        self.model = model or self.account.get("model") or (self.models[0]["slug"] if self.models else None)
        self.events = Queue()
        self.lock = threading.Lock()
        self.active_request = self.active = None
        self.connected = True
        registration = hashlib.sha256(self.client_id.encode()).hexdigest()[:24]
        self.history_path = data_directory() / ("chatgpt-history-" + registration + ".bin")

    def authorized_request(self, path, body=None, stream=False, job=None):
        self.account = fresh_account(self.client_id)
        for attempt in range(2):
            if job and job["cancel"].is_set():
                raise Cancelled()
            headers = {"Authorization": "Bearer " + self.account["access_token"]}
            try:
                if stream:
                    headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
                    return open_request(RESOURCE + path, json.dumps(body).encode("utf-8"), headers, timeout=40)
                return json_request(RESOURCE + path, body, headers)
            except ChatGPTLoginRequired:
                if attempt:
                    raise
                # Only a rejected authentication is retried, never an ambiguous send.
                self.account = fresh_account(self.client_id, force=True)

    def connect_account(self):
        catalog = self.authorized_request("/models")
        models = [model for model in catalog.get("models", [])
                  if model.get("visibility") == "list" and isinstance(model.get("slug"), str)]
        if not models:
            raise ChatGPTError("该 ChatGPT 账号暂未返回可用模型，请检查套餐授权。")
        self.models = models
        if self.model not in {model["slug"] for model in models}:
            self.model = models[0]["slug"]
        self.account.update(models=self.models, model=self.model)
        from chatgpt_account import update_registration
        update_registration(self.client_id, {"models": self.models, "model": self.model})
        record("chatgpt.models_loaded", count=len(models))

    def set_model(self, slug):
        if slug not in {model["slug"] for model in self.models}:
            raise ChatGPTError("请选择该账号可用的 ChatGPT 模型。")
        self.model = slug

    def daily_history(self, day):
        try:
            value = read_protected(self.history_path)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise ChatGPTError("ChatGPT 本地会话无法读取，请在设置中开始新会话。") from None
        if not isinstance(value, dict) or value.get("client_id") != self.client_id or not isinstance(value.get("input"), list):
            raise ChatGPTError("ChatGPT 本地会话无法读取，请在设置中开始新会话。")
        return value["input"] if value.get("day") == day else []

    def save_history(self, day, items):
        value = {"day": day, "client_id": self.client_id, "input": items}
        if len(json.dumps(value).encode("utf-8")) > MAX_HISTORY_BYTES:
            raise ChatGPTError("本次回答已完成，但当天会话过大，未能保存上下文。请在设置中开始新会话。")
        write_protected(self.history_path, value)

    def reset_session(self):
        if self.active:
            raise ChatGPTError("请先结束当前请求。")
        self.save_history(date.today().isoformat(), [])

    def enqueue(self, message):
        if message["type"] == "stop":
            self.stop(message["id"])
            return
        with self.lock:
            if self.active is not None:
                self.events.put({"type": "error", "id": message["id"], "text": "上一条请求正在结束，请稍后重试。"})
                return
            job = {**message, "day": date.today().isoformat(), "model": self.model,
                   "cancel": threading.Event(), "response": None}
            self.active = job
        threading.Thread(target=self.run, args=(job,), daemon=True).start()

    def emit(self, job, kind, **fields):
        self.events.put({"id": job["id"], "type": kind, **fields})

    def build_payload(self, job, history):
        if not job["model"]:
            raise ChatGPTError("尚未取得 ChatGPT 模型列表，请重新连接账号。")
        content = [{"type": "input_text", "text": job["prompt"]}]
        if job.get("image"):
            content.append({"type": "input_image", "image_url": "data:image/png;base64," + job["image"]})
        user = {"role": "user", "content": content}
        payload = {"model": job["model"], "input": [*history, user], "stream": True, "store": False}
        if len(json.dumps(payload).encode("utf-8")) > MAX_HISTORY_BYTES:
            raise ChatGPTError("当天会话内容过大，请在设置中开始新会话后再发送。")
        if job.get("search"):
            payload["tools"] = [{"type": "web_search"}]
        selected = next((item for item in self.models if item["slug"] == job["model"]), {})
        raw_levels = selected.get("supported_reasoning_levels", [])
        levels = [item.get("effort") if isinstance(item, dict) else item for item in raw_levels]
        if levels:
            preferences = ["high", "medium", "low"] if job.get("thinking") else ["none", "minimal", "low"]
            effort = next((value for value in preferences if value in levels), levels[0])
            payload["reasoning"] = {"effort": effort}
        elif job.get("thinking"):
            raise ChatGPTError("该模型未声明可调思考强度，请关闭深度思考或切换模型后重试。")
        return payload, user

    def run(self, job):
        response = None
        try:
            record("chatgpt.request_started", job["id"], image=bool(job.get("image")))
            self.emit(job, "progress", text="正在连接 ChatGPT")
            if not self.models:
                self.connect_account()
                job["model"] = self.model
            history = self.daily_history(job["day"])
            payload, user = self.build_payload(job, history)
            response = self.authorized_request("/responses", payload, stream=True, job=job)
            job["response"] = response
            if job["cancel"].is_set():
                raise Cancelled()
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise ChatGPTError("ChatGPT 未返回流式回答，请稍后重试。")
            fields = {}
            server_request = response.headers.get("x-request-id", "")
            if safe_code(server_request) != "unknown":
                fields["server_request"] = server_request
            record("chatgpt.stream_started", job["id"], **fields)
            self.emit(job, "started")
            stream = ResponseStream()
            previous = None
            last_emit = 0
            for event in events(response):
                if job["cancel"].is_set():
                    raise Cancelled()
                stream.event(event)
                if stream.error:
                    raise ChatGPTError(stream.error)
                content = stream.content()
                now = time.monotonic()
                if content != previous and any(content.values()) and (now - last_emit >= .035 or stream.completed):
                    self.emit(job, "delta", **content)
                    previous, last_emit = content, now
                if stream.completed:
                    break
            if job["cancel"].is_set():
                raise Cancelled()
            if not stream.completed or not stream.content()["answer"].strip():
                raise ChatGPTError("ChatGPT 回复中断，已收到的内容保留在弹窗中。")
            # Persist only a confirmed, completed turn. Failed or cancelled sends
            # never poison the next turn's context. HTTP cannot use response IDs.
            output = [item for item in stream.response.get("output", []) if item.get("type") == "message"]
            self.save_history(job["day"], [*history, user, *output])
            self.emit(job, "complete", **stream.content())
            record("chatgpt.complete", job["id"])
        except Exception as error:
            if job["cancel"].is_set() or isinstance(error, Cancelled):
                self.emit(job, "stopped", confirmed=True)
                record("chatgpt.stopped", job["id"])
            else:
                record("chatgpt.error", job["id"], error=type(error).__name__)
                description = str(error) if isinstance(error, ChatGPTError) else "连接 ChatGPT 超时或中断，请重试。"
                self.emit(job, "login" if isinstance(error, ChatGPTLoginRequired) else "error", text=description)
        finally:
            if response:
                response.close()
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
        response = job.get("response")
        if response:
            # Shutdown the socket before closing the buffered reader, which may
            # otherwise wait for a read in the worker thread to finish.
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            threading.Thread(target=response.close, daemon=True).start()

    def close(self):
        with self.lock:
            job = self.active
        if job:
            self.stop(job["id"])
