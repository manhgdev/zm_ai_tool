"""Run: PYTHONPATH=backend backend/.venv/bin/python tests/test_flow_cookie_replacement.py"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from api.routes import flow
from pipeline.flow import store
from pipeline.flow.browser import profile_lock
from pipeline.flow.errors import _FlowSessionGenerationChanged
from pipeline.flow.profiles import _seed_stored_cookies
from pipeline.flow.service import FlowService


class FlowCookieReplacementTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = patch.object(store, "ROOT", Path(temp.name))
        root.start()
        self.addCleanup(root.stop)
        store._CACHE.clear()
        self.addCleanup(store._CACHE.clear)
        threads = patch("pipeline.flow.sessions.threading.Thread")
        self.thread = threads.start()
        self.addCleanup(threads.stop)
        running = patch("pipeline.flow.browser.chrome_profile_running", return_value=False)
        self.running = running.start()
        self.addCleanup(running.stop)
        self.service = FlowService()
        self.service._log = Mock()
        self.service._login = AsyncMock()
        self.old_jar = [{"name": "OLD_ONLY", "value": "old-test-value", "domain": ".google.com", "path": "/"}]
        store.put_row("accounts", {
            "id": "a", "email": "owner@gmail.com", "authMethod": "cookie", "plan": "Pro",
            "status": "online", "cookie": self.old_jar, "sessionGeneration": 4,
            "projectId": "project", "imageProjectId": "image-project", "videoProjectId": "video-project",
            "createdAt": 123, "isDefault": True, "credits": 100,
            "accessToken": "old-test-token", "refreshToken": "old-test-refresh",
        })
        self.profile = store.profile_dir("a")
        for relative in ("Local State", "Default/Network/Cookies", "Default/Preferences", "Default/Local Storage/state", "SingletonLock"):
            path = self.profile / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old profile")
        self.disposable = [self.service._session_profile_dir("a", kind) for kind in ("image", "video")]
        self.disposable.append(store.root() / "runtime-profiles" / "a" / "done-job")
        for target in self.disposable:
            target.mkdir(parents=True)
            (target / "old-state").touch()
        self.sibling = store.profile_dir("other") / "keep"
        self.sibling.touch()

    def import_cookies(self):
        return self.service.connect("a", cookies=flow._labs_cookies("SID=new-test-value; NEW_ONLY=test"))

    def test_edit_replaces_all_browser_state_and_bootstraps_only_new_cookies(self):
        for status in ("queued", "done"):
            store.put_row("jobs", {"id": status, "status": status, "accountId": "a", "outputs": ["keep.mp4"]})
        jobs = store.list_rows("jobs")
        with patch.object(flow, "service", self.service):
            public = flow.accounts_update("a", flow.AccountUpdateIn(
                email="owner", plan="Pro", authMethod="cookie", isDefault=True,
                cookie="SID=new-test-value; NEW_ONLY=test",
            ))
        saved = store.get_row("accounts", "a")
        self.assertEqual({cookie["name"] for cookie in saved["cookie"]}, {"SID", "NEW_ONLY"})
        self.assertEqual(saved["sessionGeneration"], 5)
        for key, value in {"projectId": "project", "imageProjectId": "image-project", "videoProjectId": "video-project",
                           "createdAt": 123, "credits": 100, "isDefault": True}.items():
            self.assertEqual(saved[key], value)
        self.assertIsNone(saved["accessToken"])
        self.assertIsNone(saved["refreshToken"])
        self.assertNotIn("cookie", public)
        self.assertEqual(store.list_rows("jobs"), jobs)
        self.assertTrue(self.sibling.exists())
        self.assertTrue(all(not path.exists() for path in self.disposable))
        self.assertTrue(_seed_stored_cookies(saved))
        self.assertEqual(list(self.profile.iterdir()), [])

        page = SimpleNamespace(url="about:blank", goto=AsyncMock(side_effect=RuntimeError("test stop after cookie import")))
        context = SimpleNamespace(add_cookies=AsyncMock())
        browser = SimpleNamespace(headless=True, context=context, start=AsyncMock(), stop=AsyncMock(),
                                  page=AsyncMock(return_value=page), _cdp_browser=Mock())
        with patch("pipeline.flow.browser.BrowserManager", return_value=browser):
            asyncio.run(FlowService._login(self.service, "a", headless=True))
        imported = context.add_cookies.call_args.args[0]
        self.assertEqual({cookie["name"] for cookie in imported}, {"SID", "NEW_ONLY"})
        self.assertTrue(all(cookie["sameSite"] == "None" for cookie in imported))

    def test_new_cookie_account_uses_the_same_clean_bootstrap(self):
        with patch.object(flow, "service", self.service):
            public = flow.accounts_create(flow.AccountCreateIn(authMethod="cookie", cookie="SID=new-test-value"))
        saved = store.get_row("accounts", public["id"])
        self.assertEqual([cookie["name"] for cookie in saved["cookie"]], ["SID"])
        self.assertTrue(_seed_stored_cookies(saved))
        self.assertEqual(public["status"], "connecting")

    def test_invalid_cookie_input_leaves_account_and_profile_untouched(self):
        original = store.get_row("accounts", "a")
        with patch.object(flow, "service", self.service):
            with self.assertRaises(HTTPException) as raised:
                flow.accounts_update("a", flow.AccountUpdateIn(email="changed", cookie="not-a-cookie"))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(store.get_row("accounts", "a"), original)
        self.assertTrue((self.profile / "Default/Network/Cookies").exists())
        self.thread.assert_not_called()

    def test_busy_session_does_not_silently_drop_replacement_cookies(self):
        self.service._reap_closed_account_sessions = Mock()
        for owner in ("manual", "connecting", "syncing", "reset"):
            with self.subTest(owner=owner):
                self.service._manual_sessions.clear()
                self.service._connecting_accounts.clear()
                self.service._syncing_accounts.clear()
                lock = self.service._account_reset_locks.setdefault("a", threading.Lock())
                if owner == "manual":
                    self.service._manual_sessions["a:account"] = {"browser": Mock()}
                elif owner == "connecting":
                    self.service._connecting_accounts.add("a")
                elif owner == "syncing":
                    self.service._syncing_accounts.add("a")
                else:
                    lock.acquire()
                try:
                    with patch.object(flow, "service", self.service):
                        with self.assertRaises(HTTPException) as raised:
                            flow.accounts_update("a", flow.AccountUpdateIn(authMethod="cookie", cookie="SID=new-test-value"))
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(raised.exception.detail, "FLOW_PROFILE_BUSY")
                finally:
                    if owner == "reset":
                        lock.release()
                self.assertEqual(store.get_row("accounts", "a")["cookie"], self.old_jar)
                self.assertTrue((self.profile / "Default/Network/Cookies").exists())
        self.thread.assert_not_called()

    def test_locked_or_still_running_profile_prevents_any_deletion(self):
        for target in [self.profile, *self.disposable]:
            with self.subTest(profile=target.name), profile_lock(target):
                with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                    self.import_cookies()
                self.assertTrue((self.profile / "Local State").exists())
                self.assertTrue(all(path.exists() for path in self.disposable))
                self.assertEqual(store.get_row("accounts", "a")["cookie"], self.old_jar)
                self.assertEqual(store.get_row("accounts", "a")["resetState"], "action_required")
        self.running.side_effect = lambda path: path == self.disposable[0]
        with patch("pipeline.flow.browser.BrowserManager") as manager:
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                self.import_cookies()
        manager.return_value.kill_now.assert_called_once()
        self.assertTrue((self.profile / "Local State").exists())
        self.thread.assert_not_called()

    def test_cleanup_error_does_not_publish_new_cookies_or_start_chrome(self):
        with patch("pipeline.flow.sessions.shutil.rmtree", side_effect=PermissionError("test locked file")):
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                self.import_cookies()
        saved = store.get_row("accounts", "a")
        self.assertEqual(saved["cookie"], self.old_jar)
        self.assertEqual(saved["resetState"], "action_required")
        self.thread.assert_not_called()

    def test_old_browser_cannot_restore_old_cookies_after_replacement(self):
        self.import_cookies()
        saved = store.get_row("accounts", "a")
        stale = SimpleNamespace(_flow_session_generation=4, context=Mock())
        self.assertFalse(asyncio.run(self.service._persist_browser_cookies("a", stale)))
        self.assertEqual(store.get_row("accounts", "a")["cookie"], saved["cookie"])
        stale.context.cookies.assert_not_called()
        with self.assertRaises(_FlowSessionGenerationChanged):
            asyncio.run(self.service._seed_browser_cookies({"id": "a", "cookie": self.old_jar}, stale))

        current = SimpleNamespace(_flow_session_generation=5, context=SimpleNamespace(
            cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock(),
        ))
        self.assertTrue(asyncio.run(self.service._seed_browser_cookies({"id": "a", "cookie": self.old_jar}, current)))
        imported = current.context.add_cookies.call_args.args[0]
        self.assertEqual({cookie["name"] for cookie in imported}, {"SID", "NEW_ONLY"})

    def test_reconnect_and_regular_reset_keep_existing_google_identity(self):
        self.service.connect("a")
        self.assertTrue((self.profile / "Default/Network/Cookies").exists())
        self.assertEqual(store.get_row("accounts", "a")["sessionGeneration"], 4)
        self.service._connecting_accounts.clear()
        self.service._connecting_started_at.clear()
        self.service.reset_account_session("a")
        self.assertTrue((self.profile / "Local State").exists())
        self.assertTrue((self.profile / "Default/Network/Cookies").exists())
        self.assertFalse((self.profile / "Default/Local Storage/state").exists())
        self.assertEqual(store.get_row("accounts", "a")["cookie"], self.old_jar)


if __name__ == "__main__":
    unittest.main()
