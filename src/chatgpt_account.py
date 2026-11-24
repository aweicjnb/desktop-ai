"""ChatGPT plan authorization for the Desktop AI open-source local app.

Uses the documented public-client OAuth flow, not browser cookie extraction.
Credentials stay in Windows DPAPI storage, outside the source tree.
"""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import msvcrt
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from diagnostics import record
from web_account import protect


ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
PROCESS_LOCK = threading.RLock()


class ChatGPTError(Exception):
    pass


class ChatGPTLoginRequired(ChatGPTError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def data_directory():
    return Path(os.environ["LOCALAPPDATA"]) / "DesktopAI"


def read_protected(path):
    return json.loads(protect(path.read_bytes(), decrypt=True))


def write_protected(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(protect(json.dumps(value, ensure_ascii=False).encode("utf-8")))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def account_lock():
    # Refresh tokens rotate. Serialize reads/refreshes/writes across processes.
    with PROCESS_LOCK:
        directory = data_directory()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "chatgpt-account.lock").open("a+b") as lock:
            if lock.seek(0, 2) == 0:
                lock.write(b"\0")
                lock.flush()
            end = time.monotonic() + 65
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= end:
                        raise ChatGPTError("另一个 ChatGPT 登录操作尚未结束，请稍后重试。") from None
                    time.sleep(.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def load_store():
    try:
        value = read_protected(data_directory() / "chatgpt-accounts.bin")
    except FileNotFoundError:
        return {"version": 1, "registrations": {}}
    if not isinstance(value, dict) or not isinstance(value.get("registrations"), dict):
        raise ValueError("ChatGPT 本地登录数据无法读取，请重新连接。")
    return value


def save_registration(account, activate=True):
    with account_lock():
        store = load_store()
        store["registrations"][account["client_id"]] = account
        if activate:
            store["active"] = account["client_id"]
        write_protected(data_directory() / "chatgpt-accounts.bin", store)


def saved_accounts():
    return list(load_store()["registrations"].values())


def update_registration(client_id, fields=None, activate=False):
    with account_lock():
        store = load_store()
        account = store["registrations"][client_id]
        account.update(fields or {})
        if activate:
            store["active"] = client_id
        write_protected(data_directory() / "chatgpt-accounts.bin", store)


def load_account(client_id=None):
    store = load_store()
    account = store["registrations"].get(client_id or store.get("active"))
    if not account or not account.get("access_token"):
        raise FileNotFoundError("请先连接 ChatGPT 账号。")
    return dict(account)


def host_id():
    with account_lock():
        path = data_directory() / "chatgpt-host.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["id"]
            uuid.UUID(value.removeprefix("urn:uuid:"))
            return value
        except FileNotFoundError:
            value = "urn:uuid:" + str(uuid.uuid4())
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"id": value}), encoding="utf-8")
            temporary.replace(path)
            return value


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) else "unknown"


def http_error(error, stage="request"):
    try:
        body = json.loads(error.read(65536))
    except (ValueError, OSError):
        body = {}
    detail = body.get("error", {}) if isinstance(body, dict) else {}
    code = safe_code(detail.get("code") or detail.get("type")) if isinstance(detail, dict) else safe_code(detail)
    request_id = error.headers.get("x-request-id", "")
    fields = {"http_status": error.code, "code": code}
    if re.fullmatch(r"[\w-]{1,150}", request_id):
        fields["server_request"] = request_id
    record("chatgpt." + stage + "_failed", **fields)
    if code in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                "refresh_token_invalidated", "refresh_token_reused", "subscription_sharing_invalid_user"}:
        return ChatGPTLoginRequired("ChatGPT 登录已失效，请在设置中重新授权。", code=code)
    if code == "subscription_sharing_user_not_eligible":
        return ChatGPTError("当前 ChatGPT 账号或工作区暂不支持授权其他应用使用套餐。")
    if error.code == 429:
        return ChatGPTError("ChatGPT 使用额度或请求频率已达限制，请稍后重试，可在 ChatGPT 的用量设置中查看。")
    if error.code == 401:
        return ChatGPTLoginRequired("ChatGPT 未接受当前登录授权，请重新连接账号。")
    if error.code == 403:
        return ChatGPTError("ChatGPT 拒绝了本次访问，请检查账号权限和服务地区。" + f"（{code}）")
    if error.code >= 500:
        return ChatGPTError("ChatGPT 服务暂时不可用，请稍后重试。" + f"（HTTP {error.code}）")
    return ChatGPTError(f"ChatGPT 请求失败（HTTP {error.code}，{code}）。")


def open_request(url, body=None, headers=None, timeout=30, stage="request"):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"auth.openai.com", "api.openai.com"}:
        raise ValueError("Unexpected ChatGPT endpoint")
    request = Request(url, data=body, headers={"User-Agent": "DesktopAI/0.3.0", **(headers or {})})
    try:
        return build_opener(NoRedirect()).open(request, timeout=timeout)
    except HTTPError as error:
        raise http_error(error, stage) from None
    except (URLError, TimeoutError, OSError):
        raise ChatGPTError("连接 ChatGPT 超时或中断，请检查网络后重试。") from None


def json_request(url, body=None, headers=None, form=False, stage="request"):
    if body is not None:
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(headers or {})}
        body = (urlencode(body) if form else json.dumps(body)).encode("utf-8")
    with open_request(url, body, headers, stage=stage) as response:
        return json.loads(response.read(4 * 1024 * 1024))


def decode64(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_id_token(token, client_id, nonce=None):
    try:
        if not isinstance(token, str) or len(token) > 32768:
            raise ValueError()
        header_text, payload_text, signature_text = token.split(".")
        header, payload = json.loads(decode64(header_text)), json.loads(decode64(payload_text))
        if header.get("alg") != "RS256" or header.get("crit"):
            raise ValueError()
        keys = json_request(ISSUER + "/.well-known/jwks.json")["keys"]
        keys = [key for key in keys if key.get("kid") == header.get("kid") and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig" and key.get("alg", "RS256") == "RS256"]
        if len(keys) != 1:
            raise ValueError()
        key = keys[0]
        modulus, exponent = int.from_bytes(decode64(key["n"])), int.from_bytes(decode64(key["e"]))
        signature = decode64(signature_text)
        width = (modulus.bit_length() + 7) // 8
        if width < 256 or len(signature) != width or not 3 <= exponent <= 2**32 or exponent % 2 == 0:
            raise ValueError()
        encoded = int.from_bytes(signature)
        if encoded >= modulus:
            raise ValueError()
        digest = hashlib.sha256((header_text + "." + payload_text).encode("ascii")).digest()
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
        expected = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
        if not hmac.compare_digest(pow(encoded, exponent, modulus).to_bytes(width), expected):
            raise ValueError()
        audience = payload.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or client_id not in audiences:
            raise ValueError()
        if (len(audiences) > 1 or "azp" in payload) and payload.get("azp") != client_id:
            raise ValueError()
        now = time.time()
        if payload.get("iss") != ISSUER or not isinstance(payload.get("exp"), (int, float)) or payload["exp"] <= now:
            raise ValueError()
        if payload.get("nbf", 0) > now + 60 or payload.get("iat", 0) > now + 60:
            raise ValueError()
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise ValueError()
        if nonce is not None and not hmac.compare_digest(str(payload.get("nonce", "")), nonce):
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ChatGPTError("ChatGPT 登录身份验证失败，未替换原有账号，请重新连接。") from None


def token_record(tokens, client_id, identity, host, previous=None):
    scopes = tokens.get("scope", "").split()
    if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"] or tokens.get("token_type", "").lower() != "bearer":
        raise ChatGPTError("ChatGPT 返回的登录数据不完整，请重新连接。")
    if previous and (identity["sub"] != previous["subject"] or identity["iss"] != previous["issuer"]):
        raise ChatGPTError("本次登录与所选 ChatGPT 账号不一致，原有账号已保留。")
    return {**(previous or {}), "client_id": client_id, "ext_agent_host_id": host,
            "issuer": identity["iss"], "subject": identity["sub"], "email": identity.get("email", "ChatGPT 账号"),
            "id_token": tokens["id_token"], "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""), "scopes": scopes,
            "expires_at": time.time() + float(tokens.get("expires_in", 3600))}


class Authorization:
    def __init__(self, previous=None, timeout=360):
        self.previous = previous
        self.timeout = timeout
        self.cancelled = threading.Event()
        self.state, self.nonce, self.verifier = [secrets.token_urlsafe(32) for _ in range(3)]
        self.host = previous["ext_agent_host_id"] if previous else host_id()
        self.result = None
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # OAuth codes and URLs must never enter logs.

            def do_GET(self):
                parsed = urlsplit(self.path)
                status, message = 400, "This authorization callback is invalid. Return to Desktop AI."
                if parsed.path == "/auth/callback" and len(self.path) < 20000:
                    values = parse_qs(parsed.query, keep_blank_values=True)
                    if all(len(value) == 1 for value in values.values()) and hmac.compare_digest(values.get("state", [""])[0], owner.state):
                        if "error" in values or values.get("code", [""])[0]:
                            owner.result = {key: value[0] for key, value in values.items()}
                            status, message = 200, "Authorization received. You can close this tab and return to Desktop AI."
                body = ("<!doctype html><meta charset=utf-8><meta name=referrer content=no-referrer>"
                        "<title>Desktop AI</title><p>" + message + "</p>").encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Callback)
        self.server.timeout = .5
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
        params = {"client_id": previous["client_id"] if previous else "dynamic_agent_client",
                  "ext_agent_host_id": self.host, "response_type": "code", "redirect_uri": self.redirect_uri,
                  "scope": SCOPES, "resource": RESOURCE, "state": self.state, "nonce": self.nonce,
                  "code_challenge_method": "S256",
                  "code_challenge": urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")}
        if previous:
            if previous.get("id_token"):
                params["id_token_hint"] = previous["id_token"]
            if previous.get("email"):
                params["login_hint"] = previous["email"]
            if PLAN_SCOPE not in previous.get("scopes", []):
                params["prompt"] = "consent"
        else:
            params["agent_name_hint"] = "Desktop AI"
        self.url = AUTHORIZE + "?" + urlencode(params)

    def cancel(self):
        self.cancelled.set()

    def wait(self):
        deadline = time.monotonic() + self.timeout
        try:
            while self.result is None:
                if self.cancelled.is_set():
                    raise ChatGPTError("已取消 ChatGPT 登录。")
                if time.monotonic() >= deadline:
                    raise ChatGPTError("ChatGPT 授权等待超时，请重新点击登录。")
                self.server.handle_request()
        finally:
            self.server.server_close()
        if self.cancelled.is_set():
            raise ChatGPTError("已取消 ChatGPT 登录。")
        result = self.result
        if "error" in result:
            raise ChatGPTError("未完成 ChatGPT 授权，请重新登录并允许 Desktop AI 使用套餐。" + f"（{safe_code(result['error'])}）")
        client_id = result.get("client_id") or (self.previous or {}).get("client_id")
        if not client_id or client_id == "dynamic_agent_client" or len(client_id) > 256:
            raise ChatGPTError("ChatGPT 注册未完成，未收到应用标识，请重新连接。")
        if self.previous and client_id != self.previous["client_id"]:
            raise ChatGPTError("ChatGPT 返回了不同的应用标识，原有登录已保留。")
        tokens = json_request(TOKEN, {"grant_type": "authorization_code", "client_id": client_id,
                                     "code": result["code"], "code_verifier": self.verifier,
                                     "redirect_uri": self.redirect_uri, "resource": RESOURCE}, form=True, stage="authorize")
        identity = verify_id_token(tokens.get("id_token"), client_id, self.nonce)
        account = token_record(tokens, client_id, identity, self.host, self.previous)
        if self.cancelled.is_set():
            raise ChatGPTError("已取消 ChatGPT 登录。")
        save_registration(account, activate=False)
        if PLAN_SCOPE not in account["scopes"]:
            raise ChatGPTError("已登录 ChatGPT，但尚未允许 Desktop AI 使用套餐，请重新授权。")
        record("chatgpt.account_connected")
        return account


def fresh_account(client_id=None, force=False):
    with account_lock():
        account = load_account(client_id)
        if PLAN_SCOPE not in account.get("scopes", []):
            raise ChatGPTLoginRequired("请重新连接 ChatGPT 并允许 Desktop AI 使用套餐。")
        if not force and account.get("expires_at", 0) > time.time() + 90:
            return account
        if not account.get("refresh_token"):
            raise ChatGPTLoginRequired("ChatGPT 登录需要重新授权，请在设置中连接。")
        try:
            tokens = json_request(TOKEN, {"grant_type": "refresh_token", "client_id": account["client_id"],
                                         "refresh_token": account["refresh_token"], "resource": RESOURCE}, form=True, stage="refresh")
        except ChatGPTLoginRequired as error:
            if error.code in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                              "refresh_token_invalidated", "refresh_token_reused"}:
                account.update(access_token="", refresh_token="", expires_at=0)
                store = load_store()
                store["registrations"][account["client_id"]] = account
                write_protected(data_directory() / "chatgpt-accounts.bin", store)
            raise
        identity = {"iss": account["issuer"], "sub": account["subject"], "email": account.get("email")}
        if tokens.get("id_token"):
            identity = verify_id_token(tokens["id_token"], account["client_id"])
        else:
            tokens["id_token"] = account["id_token"]
        tokens.setdefault("scope", " ".join(account["scopes"]))
        refreshed = token_record(tokens, account["client_id"], identity, account["ext_agent_host_id"], account)
        if not refreshed["refresh_token"]:
            raise ChatGPTError("ChatGPT 未返回续期凭据，已保留原有登录，请稍后重试。")
        # Already holding the interprocess lock: do not acquire it a second time.
        store = load_store()
        store["registrations"][refreshed["client_id"]] = refreshed
        write_protected(data_directory() / "chatgpt-accounts.bin", store)
        record("chatgpt.account_refreshed")
        return refreshed
