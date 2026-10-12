import asyncio
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from pipeline.flow.service import FlowService


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
        self.context = Mock(pages=[self.page_instance])
        self.stopped = False

    async def start(self):
        return self

    async def page(self):
        return self.page_instance

    async def stop(self):
        self.stopped = True


class FlowManualSessionTests(unittest.TestCase):
    def test_video_plan_verification_opens_saved_session(self):
        service = FlowService()
        account = {
            "id": "a", "status": "online", "projectId": "p", "plan": "Ultra",
            "planStatus": "verified", "credits": 100,
            "creditsSyncedAt": time.time(),
        }

        def open_video_session(account_id, **_kwargs):
            service._manual_sessions[f"{account_id}:video"] = {"cdp_url": "http://127.0.0.1:1"}
            return {"opened": True}

        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch.object(service, "open_account_session", side_effect=open_video_session) as open_session:
            result = service._verify_account_plan_before_enqueue(
                "a", "video", {"headless": False},
            )
        self.assertEqual(result, account)
        open_session.assert_called_once_with(
            "a", kind="video", fullscreen=False, page_zoom=1.0, auto_close=True,
        )

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
             patch("pipeline.flow.service.threading.Thread") as thread:
            self.assertEqual(service.open_account_session("a"), {"opened": True})
            self.assertIn("a:image", service._manual_sessions)
            target = thread.call_args.kwargs["target"]
            target()
            browser = browsers[0]
            self.assertNotIn("a", service._manual_sessions)
            self.assertEqual(browser.kwargs["headless"], False)
            self.assertEqual(browser.kwargs["google_login_compat"], True)
            self.assertEqual(browser.kwargs["fullscreen"], True)
            self.assertGreater(browser.kwargs["remote_debugging_port"], 0)
            query = parse_qs(urlsplit(browser.page_instance.url).query)
            self.assertEqual(query["login_hint"], [account["email"]])
            self.assertEqual(query["continue"], ["https://flow.google.com/project/project-id"])
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
             patch("pipeline.flow.service.threading.Thread") as thread:
            self.assertEqual(service.open_account_session("a"), {"opened": True})
            thread.call_args.kwargs["target"]()

        self.assertEqual(len(browsers), 1)
        self.assertEqual(browsers[0].page_instance.url, "https://flow.google.com/project/project-id")
        self.assertNotIn("accounts.google.com", browsers[0].page_instance.url)

    def test_open_session_blocks_connect_sync_and_delete_until_closed(self):
        account = {"id": "a", "email": "example@gmail.com", "status": "online", "projectId": "project-id"}
        service = FlowService()
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            service.open_account_session("a")
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                service.connect("a")
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                asyncio.run(service.sync_credits_for_account("a"))
            with self.assertRaisesRegex(RuntimeError, "FLOW_PROFILE_BUSY"):
                service.delete_account("a")
            self.assertIsNotNone(thread.call_args)

    def test_open_session_reuses_existing_and_missing_account_is_rejected(self):
        account = {"id": "a", "status": "online", "projectId": "project-id"}
        service = FlowService()
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.profile_dir", return_value="/tmp/flow-profile-a"), \
             patch("pipeline.flow.service.threading.Thread") as thread:
            service._manual_sessions["a:image"] = object()
            self.assertEqual(service.open_account_session("a"), {"opened": True})
            thread.assert_not_called()
        with patch("pipeline.flow.store.get_row", return_value=None):
            with self.assertRaises(KeyError):
                FlowService().open_account_session("missing")


if __name__ == "__main__":
    unittest.main()
