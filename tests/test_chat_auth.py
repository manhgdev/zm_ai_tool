"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_chat_auth.py"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import chat
from pipeline.chat.auth import ChatGPTAuth, FileTokenStore, ISSUER, REDIRECT_URI
from pipeline.chat.service import ChatService
from pipeline.chat.store import ChatStore


class ChatAuthTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = self.enterContext(patch("pipeline.chat.auth.time.time", return_value=1000))

    def auth(self, handler, profile="a"):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return ChatGPTAuth(token_store=FileTokenStore(self.root / profile), client=client, browser_opener=Mock())

    def test_device_link_polls_without_localhost_and_saves_only_its_own_tokens(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/usercode"):
                return httpx.Response(200, json={"device_auth_id": "private-device-id", "user_code": "ABCD-1234", "interval": "5"})
            if request.url.path.endswith("/deviceauth/token"):
                self.assertEqual(json.loads(request.content), {"device_auth_id": "private-device-id", "user_code": "ABCD-1234"})
                count = calls.count(request.url.path)
                return httpx.Response(403 if count == 1 else 404 if count == 2 else 200,
                                      json={"authorization_code": "code", "code_verifier": "verifier"})
            fields = parse_qs(request.content.decode())
            self.assertEqual(fields["redirect_uri"], [f"{ISSUER}/deviceauth/callback"])
            self.assertEqual(fields["code_verifier"], ["verifier"])
            return httpx.Response(200, json={"access_token": "test-token", "refresh_token": "test-refresh", "expires_in": 3600})

        auth = self.auth(handler)
        with patch.object(auth, "_callback_server", side_effect=AssertionError("Web must not listen on localhost")):
            link = auth.start_device_login()
        self.assertEqual(link["authorizationUrl"], f"{ISSUER}/codex/device")
        self.assertEqual(link["userCode"], "ABCD-1234")
        self.assertNotIn("device_auth_id", link)
        self.assertEqual(auth.start_device_login(), link, "Reloading Chat must resume the same confirmation code")
        with auth._pending[link["loginId"]].poll_lock:
            self.assertEqual(auth.poll(link["loginId"]), {"status": "pending"})
        self.assertEqual(len(calls), 1, "Concurrent tabs must not exchange a code twice")
        self.assertEqual(auth.poll(link["loginId"]), {"status": "pending"})
        self.assertEqual(auth.poll(link["loginId"]), {"status": "pending"})
        self.assertEqual(len(calls), 2, "Repeated browser polls must respect OpenAI's interval")
        self.now.return_value = 1005
        self.assertEqual(auth.poll(link["loginId"]), {"status": "pending"})
        self.now.return_value = 1010
        self.assertEqual(auth.poll(link["loginId"])["status"], "connected")
        self.assertEqual(auth.poll(link["loginId"])["status"], "connected", "Other tabs must see the completed sign-in")
        self.assertEqual(auth.store.load()["access_token"], "test-token")
        self.assertIsNone(FileTokenStore(self.root / "b").load())
        self.assertFalse(auth.login_pending)
        self.assertTrue(auth.login_complete.is_set())
        auth.browser_opener.assert_not_called()

    def test_cancel_expiry_and_failure_discard_pending_device_sessions(self):
        for outcome in ("cancel", "expired", "http-error", "incomplete"):
            with self.subTest(outcome=outcome):
                def handler(request):
                    if request.url.path.endswith("/usercode"):
                        return httpx.Response(200, json={"device_auth_id": "device", "user_code": "ABCD-1234"})
                    self.assertIn(outcome, {"http-error", "incomplete"})
                    return httpx.Response(500 if outcome == "http-error" else 200, json={})

                auth = self.auth(handler, profile=outcome)
                link = auth.start_device_login()
                if outcome == "cancel":
                    auth.cancel_login(link["loginId"])
                    auth.cancel_login(link["loginId"])
                elif outcome == "expired":
                    auth._pending[link["loginId"]].expires_at = 0
                    self.assertEqual(auth.poll(link["loginId"])["status"], "expired")
                else:
                    with self.assertRaises((httpx.HTTPStatusError, RuntimeError)):
                        auth.poll(link["loginId"])
                self.assertFalse(auth.login_pending)
                self.assertIsNone(auth.store.load())

    def test_cancel_during_token_exchange_cannot_save_tokens(self):
        auth = None
        link = None

        def handler(request):
            if request.url.path.endswith("/usercode"):
                return httpx.Response(200, json={"device_auth_id": "device", "user_code": "ABCD-1234"})
            if request.url.path.endswith("/deviceauth/token"):
                return httpx.Response(200, json={"authorization_code": "code", "code_verifier": "verifier"})
            auth.cancel_login(link["loginId"])
            return httpx.Response(200, json={"access_token": "must-not-be-saved"})

        auth = self.auth(handler)
        link = auth.start_device_login()
        self.assertEqual(auth.poll(link["loginId"])["status"], "failed")
        self.assertFalse(auth.login_pending)
        self.assertIsNone(auth.store.load())

    def test_device_auth_unavailable_or_malformed_start_keeps_saved_tokens(self):
        for status, data in ((404, {}), (200, {"user_code": "ABCD-1234"}), (200, {"device_auth_id": "device", "user_code": 1234})):
            with self.subTest(status=status, data=data):
                auth = self.auth(lambda _: httpx.Response(status, json=data))
                auth.store.save({"access_token": "existing-token"})
                with self.assertRaises(RuntimeError):
                    auth.start_device_login()
                self.assertFalse(auth.login_pending)
                self.assertEqual(auth.store.load()["access_token"], "existing-token")

    def test_domain_routes_use_device_links_and_desktop_keeps_loopback(self):
        for web in (True, False):
            for endpoint in ("login?open_browser=true", "login-link"):
                with self.subTest(web=web, endpoint=endpoint), patch.dict(os.environ, {"ZM_AI_TOOL_WEB_WORKER": "1" if web else "0"}):
                    store = ChatStore(self.root / f"{web}-{endpoint[:5]}.sqlite3", self.root / "attachments")
                    self.addCleanup(store.close)
                    account = store.create_account("Codex", self.root / "profile")
                    service = ChatService(store=store)
                    auth = self.auth(lambda _: httpx.Response(200, json={"device_auth_id": "device", "user_code": "ABCD-1234"}))
                    service._auth[account["id"]] = auth
                    app = FastAPI()
                    app.include_router(chat.router)
                    with patch.object(chat, "service", service), patch.object(auth, "_callback_server") as callback, patch("pipeline.chat.service.chrome_executable") as chrome:
                        with TestClient(app, base_url="https://zmaitool.com") as client:
                            response = client.post(f"/api/chat/accounts/{account['id']}/{endpoint}")
                            self.assertEqual(response.status_code, 200)
                            data = response.json()
                            if web:
                                self.assertEqual(data["authorizationUrl"], f"{ISSUER}/codex/device")
                                self.assertEqual(data["userCode"], "ABCD-1234")
                                resumed = client.post(f"/api/chat/accounts/{account['id']}/{endpoint}")
                                self.assertEqual(resumed.json(), data)
                                chrome.assert_not_called()
                                callback.assert_not_called()
                                auth.browser_opener.assert_not_called()
                            else:
                                self.assertIn(REDIRECT_URI, parse_qs(httpx.URL(data["authorizationUrl"]).query.decode())["redirect_uri"])
                                self.assertNotIn("userCode", data)
                                callback.assert_called_once()
                                self.assertEqual(auth.browser_opener.call_count, int(endpoint.startswith("login?")))
                            self.assertEqual(client.post(f"/api/chat/accounts/{account['id']}/login/{data['loginId']}/cancel").status_code, 200)
                            self.assertFalse(auth.login_pending)

    def test_desktop_exchange_keeps_its_original_redirect(self):
        def handler(request):
            self.assertEqual(parse_qs(request.content.decode())["redirect_uri"], [REDIRECT_URI])
            return httpx.Response(200, json={"access_token": "desktop-token"})

        auth = self.auth(handler)
        with patch.object(auth, "_callback_server"):
            link = auth.start_login(open_browser=False)
        auth._pending[link["loginId"]].code = "desktop-code"
        self.assertEqual(auth.poll(link["loginId"])["status"], "connected")
        self.assertEqual(auth.store.load()["access_token"], "desktop-token")


if __name__ == "__main__":
    unittest.main()
