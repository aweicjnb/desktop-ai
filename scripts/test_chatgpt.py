"""Account-free Windows regression checks: python scripts/test_chatgpt.py."""
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import gc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import chatgpt_account as account
import chatgpt_client as client


def fixture_account(client_id="test-registration", expired=False):
    return {"client_id": client_id, "subject": "test-subject", "issuer": account.ISSUER,
            "email": "test@example.invalid", "access_token": "test-access-token",
            "refresh_token": "test-refresh-token", "id_token": "test-id-token",
            "ext_agent_host_id": "urn:uuid:5c67d940-134d-48e2-86db-c8e5d7b43f4",
            "expires_at": time.time() + (-1 if expired else 3600), "scopes": account.SCOPES.split(),
            "model": "test-model", "models": [{"slug": "test-model", "display_name": "Test model",
                                               "visibility": "list", "supported_reasoning_levels": ["none", "high"]}]}


def job(**fields):
    return {"id": "a" * 32, "day": "2026-11-24", "prompt": "hello", "model": "test-model",
            "search": False, "thinking": False, "cancel": threading.Event(), **fields}


def completed(text="hello"):
    return {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "id": "test-message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": text, "annotations": []}]}]}}


class FakeResponse(io.BytesIO):
    def __init__(self, items):
        super().__init__(b"".join(("data: " + json.dumps(item, ensure_ascii=False) + "\n\n").encode("utf-8") for item in items))
        self.headers = {"Content-Type": "text/event-stream"}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="desktop-ai-test-")
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.directory.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()


class AccountTests(Isolated):
    def test_dpapi_and_separate_registrations(self):
        first, second = fixture_account(), fixture_account("second-registration")
        account.save_registration(first)
        account.save_registration(second)
        self.assertEqual(account.load_account()["client_id"], second["client_id"])
        self.assertEqual(account.load_account(first["client_id"])["access_token"], first["access_token"])
        data = (account.data_directory() / "chatgpt-accounts.bin").read_bytes()
        self.assertNotIn(first["access_token"].encode(), data)
        self.assertNotIn(first["email"].encode(), data)
        self.assertEqual(account.host_id(), account.host_id())

    def test_serialized_refresh_rotates_once(self):
        original = fixture_account(expired=True)
        account.save_registration(original)
        def refresh(url, body, **kwargs):
            self.assertEqual(body["client_id"], original["client_id"])
            self.assertEqual(body["refresh_token"], original["refresh_token"])
            time.sleep(.05)
            return {"access_token": "test-new-access", "refresh_token": "test-new-refresh",
                    "expires_in": 3600, "token_type": "Bearer"}
        with patch.object(account, "json_request", side_effect=refresh) as request:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: account.fresh_account(), range(2)))
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(item["refresh_token"] == "test-new-refresh" for item in results))
        self.assertEqual(account.load_account()["models"], original["models"])

    def test_temporary_refresh_failure_preserves_credentials(self):
        original = fixture_account(expired=True)
        account.save_registration(original)
        with patch.object(account, "json_request", side_effect=account.ChatGPTError("offline")):
            with self.assertRaises(account.ChatGPTError):
                account.fresh_account()
        self.assertEqual(account.load_account(), original)

    def test_metadata_update_keeps_latest_tokens(self):
        original = fixture_account()
        account.save_registration(original)
        original["refresh_token"] = "newer-rotated-token"
        account.save_registration(original)
        account.update_registration(original["client_id"], {"model": "other-model"})
        self.assertEqual(account.load_account()["refresh_token"], "newer-rotated-token")

    def test_revoked_refresh_clears_tokens_but_keeps_registration(self):
        original = fixture_account(expired=True)
        account.save_registration(original)
        revoked = account.ChatGPTLoginRequired("expired", code="invalid_grant")
        with patch.object(account, "json_request", side_effect=revoked):
            with self.assertRaises(account.ChatGPTLoginRequired):
                account.fresh_account()
        saved = account.saved_accounts()[0]
        self.assertEqual(saved["client_id"], original["client_id"])
        self.assertEqual(saved["access_token"], "")
        self.assertEqual(saved["refresh_token"], "")

    def test_unvalidated_connection_does_not_replace_active_account(self):
        first, pending = fixture_account(), fixture_account("pending-registration")
        account.save_registration(first)
        account.save_registration(pending, activate=False)
        self.assertEqual(account.load_account()["client_id"], first["client_id"])

    def test_oauth_state_pkce_and_callback_exchange(self):
        auth = account.Authorization(timeout=3)
        params = parse_qs(urlsplit(auth.url).query)
        expected = urlsafe_b64encode(hashlib.sha256(auth.verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(params["code_challenge"], [expected])
        self.assertEqual(params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(params["agent_name_hint"], ["Desktop AI"])
        self.assertTrue(auth.redirect_uri.startswith("http://127.0.0.1:"))
        tokens = {"access_token": "test-access", "refresh_token": "test-refresh", "id_token": "test-id",
                  "token_type": "Bearer", "expires_in": 3600, "scope": account.SCOPES}
        identity = {"sub": "test-user", "iss": account.ISSUER, "email": "test@example.invalid"}
        def exchange(url, body, **kwargs):
            self.assertEqual(url, account.TOKEN)
            self.assertEqual(body["client_id"], "issued-test-client")
            self.assertEqual(body["code_verifier"], auth.verifier)
            self.assertEqual(body["redirect_uri"], auth.redirect_uri)
            self.assertEqual(body["code"], "test-code")
            return tokens
        with patch.object(account, "json_request", side_effect=exchange), patch.object(account, "verify_id_token", return_value=identity):
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(auth.wait)
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(auth.redirect_uri + "?" + urlencode({"state": "wrong-state", "code": "attack"}), timeout=2)
                self.assertEqual(invalid.exception.code, 400)
                self.assertIsNone(auth.result)
                values = {"state": auth.state, "code": "test-code", "client_id": "issued-test-client"}
                with urlopen(auth.redirect_uri + "?" + urlencode(values), timeout=2) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(result.result(timeout=2)["client_id"], "issued-test-client")

    def test_cancel_authorization_closes_listener(self):
        auth = account.Authorization(timeout=3)
        auth.cancel()
        with self.assertRaisesRegex(account.ChatGPTError, "已取消"):
            auth.wait()
        self.assertEqual(auth.server.fileno(), -1)

    def test_returning_login_reuses_client_and_host(self):
        previous = fixture_account()
        auth = account.Authorization(previous)
        params = parse_qs(urlsplit(auth.url).query)
        self.assertEqual(params["client_id"], [previous["client_id"]])
        self.assertEqual(params["ext_agent_host_id"], [previous["ext_agent_host_id"]])
        self.assertNotIn("agent_name_hint", params)
        auth.server.server_close()


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Independent platform RSA signing, with an ephemeral test-only key.
        script = r'''
        $rsa = [System.Security.Cryptography.RSA]::Create(2048)
        function Enc([byte[]]$value) { [Convert]::ToBase64String($value).TrimEnd('=').Replace('+','-').Replace('/','_') }
        $header = Enc ([Text.Encoding]::UTF8.GetBytes('{"alg":"RS256","kid":"test-key"}'))
        $payload = Enc ([Text.Encoding]::UTF8.GetBytes($env:DESKTOP_AI_TEST_CLAIMS))
        $signed = $header + '.' + $payload
        $signature = $rsa.SignData([Text.Encoding]::ASCII.GetBytes($signed), [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
        $key = $rsa.ExportParameters($false)
        @{ token=($signed + '.' + (Enc $signature)); jwk=@{ kty='RSA'; kid='test-key'; n=(Enc $key.Modulus); e=(Enc $key.Exponent) } } | ConvertTo-Json -Compress
        $rsa.Dispose()
        '''
        env = {**os.environ, "DESKTOP_AI_TEST_CLAIMS": json.dumps({"sub": "test-user", "iss": account.ISSUER,
               "aud": "test-client", "nonce": "test-nonce", "exp": time.time() + 600})}
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                env=env, check=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        fixture = json.loads(result.stdout)
        cls.token, cls.key = fixture["token"], fixture["jwk"]

    def test_valid_platform_signature(self):
        with patch.object(account, "json_request", return_value={"keys": [self.key]}):
            value = account.verify_id_token(self.token, "test-client", "test-nonce")
        self.assertEqual(value["sub"], "test-user")

    def test_reject_invalid_signature_audience_nonce_and_expiry(self):
        with patch.object(account, "json_request", return_value={"keys": [self.key]}):
            for audience, nonce in [("wrong-client", "test-nonce"), ("test-client", "wrong-nonce")]:
                with self.assertRaises(account.ChatGPTError):
                    account.verify_id_token(self.token, audience, nonce)
            head, payload, signature = self.token.split(".")
            forged = head + "." + payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
            with self.assertRaises(account.ChatGPTError):
                account.verify_id_token(forged, "test-client", "test-nonce")
            with patch.object(account.time, "time", return_value=time.time() + 3600):
                with self.assertRaises(account.ChatGPTError):
                    account.verify_id_token(self.token, "test-client", "test-nonce")


class ClientTests(Isolated):
    def make_client(self):
        original = fixture_account()
        account.save_registration(original)
        return client.ChatGPTClient(original)

    def test_streaming_completion_and_daily_context(self):
        transport = self.make_client()
        received = []
        def send(path, payload, **kwargs):
            received.append(payload)
            return FakeResponse([{"type": "response.output_text.delta", "delta": "你"},
                                 {"type": "response.output_text.delta", "delta": "好"}, completed("你好")])
        with patch.object(transport, "authorized_request", side_effect=send):
            transport.run(job())
            transport.run(job(id="b" * 32, prompt="上一条说了什么"))
        self.assertEqual(len(received[0]["input"]), 1)
        self.assertEqual(len(received[1]["input"]), 3)
        self.assertEqual(transport.daily_history("2026-11-25"), [])
        self.assertEqual(len(transport.daily_history("2026-11-24")), 4)
        self.assertFalse(received[0]["store"])
        self.assertTrue(received[0]["stream"])
        events = list(transport.events.queue)
        self.assertTrue(any(item["type"] == "delta" and item["answer"] == "你" for item in events))
        self.assertEqual(events[-1]["type"], "complete")

    def test_failed_and_truncated_streams_keep_previous_history(self):
        transport = self.make_client()
        previous = [{"role": "user", "content": "previous"}]
        transport.save_history("2026-11-24", previous)
        for ending in [[], [{"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}]]:
            with patch.object(transport, "authorized_request", return_value=FakeResponse([
                    {"type": "response.output_text.delta", "delta": "partial"}, *ending])):
                transport.run(job())
            self.assertEqual(transport.daily_history("2026-11-24"), previous)
            self.assertEqual(list(transport.events.queue)[-1]["type"], "error")

    def test_cancel_does_not_commit_context(self):
        transport = self.make_client()
        current = job()
        current["cancel"].set()
        with patch.object(transport, "authorized_request", return_value=FakeResponse([completed()])):
            transport.run(current)
        self.assertEqual(transport.daily_history(current["day"]), [])
        self.assertEqual(list(transport.events.queue)[-1]["type"], "stopped")

    def test_image_search_reasoning_payload(self):
        transport = self.make_client()
        payload, user = transport.build_payload(job(image="dGVzdA==", search=True, thinking=True), [])
        self.assertEqual(user["content"][1]["image_url"], "data:image/png;base64,dGVzdA==")
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertNotIn("previous_response_id", payload)
        self.assertNotIn("conversation", payload)

    def test_model_catalog_comes_from_selected_account(self):
        transport = self.make_client()
        catalog = {"models": [{"slug": "hidden", "visibility": "hide"},
                               {"slug": "visible", "visibility": "list", "display_name": "Available"}]}
        with patch.object(transport, "authorized_request", return_value=catalog):
            transport.connect_account()
        self.assertEqual(transport.model, "visible")
        with self.assertRaises(account.ChatGPTError):
            transport.set_model("hidden")

    def test_explicit_reset_and_search_citations(self):
        transport = self.make_client()
        transport.save_history(date.today().isoformat(), [{"role": "user", "content": "old"}])
        transport.reset_session()
        self.assertEqual(transport.daily_history(date.today().isoformat()), [])
        ending = completed("fact")
        ending["response"]["output"][0]["content"][0]["annotations"] = [
            {"type": "url_citation", "title": "Source", "url": "https://example.invalid/source"}]
        stream = client.ResponseStream()
        stream.event(ending)
        self.assertIn("[Source](https://example.invalid/source)", stream.content()["answer"])
        self.assertEqual(stream.content()["thought"], "")


class PopupTests(Isolated):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        from popup import Popup
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = Popup(self.root, demo=True, smoke=True)
        self.root.attributes("-topmost", False)
        self.root.geometry("510x384+15000+15000")
        self.root.deiconify()
        self.root.update()

    def tearDown(self):
        self.popup.exit()
        self.popup = self.root = None
        # Dispose old Tcl variables on the Tk thread, before another test's
        # worker can trigger cyclic collection of a destroyed interpreter.
        gc.collect()
        super().tearDown()

    def test_provider_picker_layout_at_both_scales(self):
        from test_chatgpt_web import fixture
        import chatgpt_web_account as web_account
        web_account.save_account(fixture(), activate=True)
        view = self.popup.setup_panel
        for scale in (1, 2):
            self.popup.set_scale(scale)
            self.root.geometry(f"{round(510*scale)}x{round(384*scale)}+15000+15000")
            self.popup.show_setup()
            view.source.set("ChatGPT")
            view.source_changed()
            self.root.update()
            self.assertEqual(view.model(), "test-model")
            self.assertEqual(view.login.cget("text"), "读取网页会话")
            self.assertTrue(view.model_row.winfo_manager())
            self.assertLessEqual(view.actions.winfo_y() + view.actions.winfo_height(), view.winfo_height())
            self.assertLessEqual(view.back.winfo_x() + view.back.winfo_width(), view.actions.winfo_width())
            self.assertGreaterEqual(view.connect.winfo_width(), self.popup.dp(60))
            view.source.set("DeepSeek")
            view.source_changed()
            self.root.update()
            self.assertEqual(view.login.cget("text"), "登录 DeepSeek")
            self.assertFalse(view.model_row.winfo_manager())
            self.popup.close_setup()

    def test_switch_send_complete_and_cancelled_setup(self):
        from test_chatgpt_web import fixture, Response, web_events
        import chatgpt_web_account as web_account
        from chatgpt_web_client import ChatGPTWebClient
        original = fixture()
        web_account.save_account(original, activate=True)
        transport = ChatGPTWebClient(original)
        app = self.popup
        app.setup_task = "selected-task"
        app.setup_results.put(("selected-task", transport, ""))
        app.tick()
        self.root.update()
        self.assertEqual(app.brand.cget("text"), "ChatGPT")
        self.assertEqual(app.provider, "chatgpt")
        app.demo = False
        app.replace_input("hello")
        with patch.object(transport, 'integrity', return_value={}), patch.object(
                transport, 'request', return_value=Response(web_events(), stream=True)):
            app.submit()
            self.assertEqual(app.read_input(), "")
            end = time.monotonic() + 3
            while app.active and time.monotonic() < end:
                self.root.update()
                time.sleep(.01)
        self.assertIsNone(app.active)
        self.assertEqual(app.answer, "answer")
        self.assertIn("已完成", app.status.cget("text"))
        stale = ChatGPTWebClient(original)
        app.setup_results.put(("cancelled-task", stale, ""))
        app.tick()
        self.assertIs(app.client, transport)

    def test_first_launch_without_chatgpt_credentials(self):
        from popup import Popup
        import tkinter as tk
        self.popup.exit()
        self.popup = self.root = None
        gc.collect()
        path = account.data_directory() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"provider": "chatgpt"}), encoding="utf-8")
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = Popup(self.root, demo=False, smoke=True)
        self.root.update_idletasks()
        self.assertFalse(self.popup.ready)
        self.assertIsNone(self.popup.client)
        self.assertEqual(self.popup.setup_panel.provider(), "chatgpt")
        self.assertTrue(self.popup.setup_panel.winfo_manager())


if __name__ == "__main__":
    unittest.main(verbosity=2)
