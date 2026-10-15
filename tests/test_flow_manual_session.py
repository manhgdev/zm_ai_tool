import asyncio
import time
import unittest
from unittest.mock import Mock, patch

from pipeline.flow.browser import FLOW_BASE_URL
from pipeline.flow.service import FlowService


async def _done(*_args, **_kwargs):
    return None


class _ManualPage:
    def __init__(self):
        self.url = ""

    async def goto(self, url, **_kwargs):
        self.url = url

    def is_closed(self):
        return True


class _ManualBrowser:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cdp_url = f"http://127.0.0.1:{kwargs.get('remote_debugging_port', 0)}"
        self.page_instance = _ManualPage()
        self.context = Mock(pages=[])
        self.stopped = False

    async def start(self):
        return self

    async def page(self):
        return self.page_instance

    async def stop(self):
        self.stopped = True


class FlowManualSessionTests(unittest.TestCase):
    def test_video_plan_verification_reuses_verified_snapshot_without_open_session(self):
        """Admission uses credits/catalog TTL; jobs clone runtime profiles by kind."""
        service = FlowService()
        account = {
            "id": "a", "status": "online", "projectId": "p", "plan": "Ultra",
            "planStatus": "verified", "credits": 100,
            "creditsSyncedAt": time.time(),
            "capabilityStatus": "verified",
            "capabilityCatalog": {"version": 1},
        }
        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch.object(service, "open_account_session") as open_session:
            result = service._verify_account_plan_before_enqueue(
                "a", "video", {"headless": False},
            )
        self.assertEqual(result, account)
        open_session.assert_not_called()

    def test_auto_session_does_not_bypass_automation_cooldown(self):
        service = FlowService()
        service._manual_sessions["a:video"] = {"cdp_url": "http://127.0.0.1:1"}
        service._auto_close_sessions.add("a:video")
        account = {
            "id": "a",
            "suspendedUntil": 9999999999,
            "suspendReason": "FLOW_AUTOMATION_BLOCKED: abnormal activity",
        }
        with patch("pipeline.flow.store.get_row", return_value=account):
            self.assertTrue(service._account_suspended("a"))

    def test_prompt_owned_session_is_closed_when_account_queue_is_idle(self):
        service = FlowService()
        browser = Mock()
        service._manual_sessions["a:image"] = {"browser": browser, "cdp_url": "http://127.0.0.1:1"}
        service._auto_close_sessions.add("a:image")
        with patch("pipeline.flow.store.list_rows", return_value=[
            {"id": "job-1", "accountId": "a", "status": "failed"},
        ]), patch.object(service, "_log"):
            service._close_auto_session_if_idle("a")
        browser.kill_now.assert_called_once()
        self.assertNotIn("a:image", service._auto_close_sessions)

    def test_prompt_owned_session_stays_until_last_job_finishes(self):
        service = FlowService()
        browser = Mock()
        service._manual_sessions["a:image"] = {"browser": browser, "cdp_url": "http://127.0.0.1:1"}
        service._auto_close_sessions.add("a:image")
        with patch("pipeline.flow.store.list_rows", return_value=[
            {"id": "job-1", "accountId": "a", "status": "processing"},
        ]):
            service._close_auto_session_if_idle("a")
        browser.kill_now.assert_not_called()
        self.assertIn("a:image", service._auto_close_sessions)

    def test_video_session_closes_when_only_image_jobs_are_queued(self):
        service = FlowService()
        browser = Mock()
        service._manual_sessions["a:video"] = {"browser": browser, "cdp_url": "http://127.0.0.1:1"}
        service._auto_close_sessions.add("a:video")
        with patch("pipeline.flow.store.list_rows", return_value=[
            {"id": "job-1", "accountId": "a", "kind": "image", "status": "queued"},
        ]), patch.object(service, "_log"):
            service._close_auto_session_if_idle("a", "video")
        browser.kill_now.assert_called_once()
        self.assertNotIn("a", service._auto_close_sessions)

    def test_headless_jobs_clone_profile_even_when_manual_session_is_open(self):
        service = FlowService()
        service._manual_sessions["a"] = {"cdp_url": "http://127.0.0.1:4567"}
        with patch.object(service, "_clone_closed_profile") as clone:
            service._clone_runtime_profile("a", "job", "image")
            service._clone_runtime_profile("a", "job", "video")
        self.assertEqual(clone.call_count, 2)

    def test_saved_playwright_profile_opens_full_login_and_stays_manual(self):
        account = {
            "id": "a", "email": "example@gmail.com", "status": "online",
            "projectId": "project-id",
        }
        service = FlowService()
        browsers = []
        def make_browser(**kwargs):
            browser = _ManualBrowser(**kwargs)
            browsers.append(browser)
            return browser
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.browser.BrowserManager", side_effect=make_browser), \
             patch("pipeline.flow.service.asyncio.to_thread", new=_done), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            self.assertEqual(service.open_account_session("a"), {"opened": True})
            self.assertIn("a:account", service._manual_sessions)
            target = thread.call_args.kwargs["target"]
            target()
            browser = browsers[0]
            self.assertNotIn("a:account", service._manual_sessions)
            self.assertEqual(str(browser.kwargs["profile_dir"]), "/tmp/flow-profile-a")
            self.assertEqual(browser.kwargs["headless"], False)
            self.assertEqual(browser.kwargs["google_login_compat"], True)
            self.assertEqual(browser.kwargs["fullscreen"], True)
            self.assertGreater(browser.kwargs["remote_debugging_port"], 0)
            self.assertEqual(browser.page_instance.url, FLOW_BASE_URL)
            self.assertNotIn("accounts.google.com", browser.page_instance.url)
            self.assertTrue(browser.stopped)

    def test_cookie_profile_opens_flow_without_password_login(self):
        account = {
            "id": "a", "email": "example@gmail.com", "authMethod": "cookie",
            "status": "online", "projectId": "project-id",
        }
        service = FlowService()
        browsers = []

        def make_browser(**kwargs):
            browser = _ManualBrowser(**kwargs)
            browsers.append(browser)
            return browser

        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.browser.BrowserManager", side_effect=make_browser), \
             patch("pipeline.flow.service.asyncio.to_thread", new=_done), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            self.assertEqual(service.open_account_session("a"), {"opened": True})
            thread.call_args.kwargs["target"]()

        self.assertEqual(len(browsers), 1)
        self.assertEqual(browsers[0].page_instance.url, FLOW_BASE_URL)
        self.assertNotIn("accounts.google.com", browsers[0].page_instance.url)

    def test_open_session_blocks_connect_and_delete_until_closed(self):
        account = {"id": "a", "email": "example@gmail.com", "status": "online", "projectId": "project-id"}
        service = FlowService()
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            service.open_account_session("a")
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                service.connect("a")
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                service.delete_account("a")
            self.assertIsNotNone(thread.call_args)

    def test_closed_chrome_is_not_profile_busy(self):
        """After Chrome exits, Reset/Connect must not stay stuck on FLOW_PROFILE_BUSY."""
        import tempfile
        from pathlib import Path
        from pipeline.flow import store

        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "online", "projectId": "p", "cookie": [{"name": "SID"}]}])
            service = FlowService()
            browser = Mock(profile_dir=str(store.profile_dir("a")))
            service._manual_sessions["a:account"] = {
                "browser": browser, "cdp_url": "http://127.0.0.1:1", "startedAt": time.monotonic(),
            }
            with patch("pipeline.flow.browser.chrome_profile_running", return_value=False), \
                 patch.object(service, "connect") as connect, \
                 patch.object(service, "_sync_runtime_cookies"):
                service.reset_account_session("a")
            self.assertNotIn("a:account", service._manual_sessions)
            connect.assert_called_once()

    def test_stuck_connecting_without_chrome_is_not_profile_busy(self):
        """Closed Connect Chrome must free Reset; stale opening must not fake Mở phiên."""
        import tempfile
        from pathlib import Path
        from pipeline.flow import store

        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "connecting", "projectId": "p"}])
            service = FlowService()
            service._connecting_accounts.add("a")
            service._connecting_started_at["a"] = time.monotonic() - 30
            service._manual_sessions["a:account"] = {"opening": True, "startedAt": time.monotonic() - 30}
            with patch("pipeline.flow.browser.chrome_profile_running", return_value=False), \
                 patch.object(service, "connect") as connect, \
                 patch.object(service, "_sync_runtime_cookies"):
                service.reset_account_session("a")
            self.assertNotIn("a", service._connecting_accounts)
            self.assertNotIn("a:account", service._manual_sessions)
            connect.assert_called_once()

            service._manual_sessions["a:account"] = {"opening": True, "startedAt": time.monotonic() - 30}
            with patch("pipeline.flow.browser.chrome_profile_running", return_value=False), \
                 patch("pipeline.flow.service.threading.Thread") as thread:
                self.assertEqual(service.open_account_session("a"), {"opened": True})
            self.assertTrue(thread.called)
            self.assertIn("a:account", service._manual_sessions)
            self.assertTrue(service._manual_sessions["a:account"].get("opening"))

    def test_reset_reaps_orphan_chrome_after_window_close(self):
        """Closed UI can leave Chromium alive; Reset must kill it before busy check."""
        import tempfile
        from pathlib import Path
        from pipeline.flow import store

        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "online", "projectId": "p"}])
            service = FlowService()
            killer = Mock()
            with patch("pipeline.flow.browser.chrome_profile_running", side_effect=[True, False, False, False]), \
                 patch("pipeline.flow.browser.BrowserManager", return_value=Mock(kill_now=killer)) as manager, \
                 patch.object(service, "connect") as connect, \
                 patch.object(service, "_sync_runtime_cookies"):
                service.reset_account_session("a")
            manager.assert_called()
            killer.assert_called()
            connect.assert_called_once()

    def test_dead_open_session_redirects_to_google_login(self):
        account = {"id": "a", "email": "example@gmail.com", "status": "online"}
        service = FlowService()
        browsers = []

        def make_browser(**kwargs):
            browser = _ManualBrowser(**kwargs)
            goto = browser.page_instance.goto

            async def redirect_home(url, **options):
                await goto(f"{FLOW_BASE_URL}/about" if url == FLOW_BASE_URL else url, **options)

            browser.page_instance.goto = redirect_home
            browsers.append(browser)
            return browser

        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.browser.BrowserManager", side_effect=make_browser), \
             patch("pipeline.flow.service.asyncio.to_thread", new=_done), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            service.open_account_session("a")
            thread.call_args.kwargs["target"]()
        url = browsers[0].page_instance.url
        self.assertTrue(url.startswith("https://accounts.google.com/ServiceLogin"))
        self.assertIn("login_hint=example%40gmail.com", url)

    def test_open_session_login_wall_saves_cookies_once_signed_in(self):
        service = FlowService()
        login = Mock(url="https://accounts.google.com/ServiceLogin")
        flow = Mock(url=FLOW_BASE_URL)
        cookie = {"name": "SID", "value": "new", "domain": ".google.com", "path": "/"}

        async def cookies():
            return [cookie]

        browser = Mock()
        browser.context = Mock(pages=[login], cookies=cookies)
        patches = []
        with patch("pipeline.flow.store.get_row", return_value={"id": "a", "status": "reconnect"}), \
             patch("pipeline.flow.store.patch_row", side_effect=lambda *args: patches.append(args)):
            self.assertFalse(asyncio.run(service._confirm_open_session(browser, "a", login)))
            self.assertEqual(patches, [])
            browser.context.pages = [login, flow]
            self.assertTrue(asyncio.run(service._confirm_open_session(browser, "a", login)))
        cookie_patch = next(args[2] for args in patches if "cookie" in args[2])
        status_patch = next(args[2] for args in patches if args[2].get("status") == "online")
        self.assertEqual(cookie_patch["cookie"][0]["value"], "new")
        self.assertEqual(status_patch["status"], "online")

    def test_open_session_reuses_existing_and_missing_account_is_rejected(self):
        account = {"id": "a", "status": "online", "projectId": "project-id"}
        service = FlowService()
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            service._manual_sessions["a:account"] = {
                "browser": Mock(profile_dir="/tmp/flow-profile-a"),
                "cdp_url": "http://127.0.0.1:1",
                "startedAt": time.monotonic(),
            }
            with patch("pipeline.flow.browser.chrome_profile_running", return_value=True):
                self.assertEqual(service.open_account_session("a"), {"opened": True})
            thread.assert_not_called()
            # Kind-specific auto slots still open separately from manual account.
            self.assertEqual(service.open_account_session("a", kind="video", auto_close=True), {"opened": True})
            thread.assert_called_once()
        with patch("pipeline.flow.store.get_row", return_value=None):
            with self.assertRaises(KeyError):
                FlowService().open_account_session("missing")


if __name__ == "__main__":
    unittest.main()
