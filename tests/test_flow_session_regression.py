"""Selected account and bounded session recovery regressions."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipeline.flow.service import FlowService


class FlowSessionRegressionTest(unittest.IsolatedAsyncioTestCase):
    def test_reconnected_job_is_replayed_once(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "projectId": "p"}
        job = {"id": "j", "accountId": "a", "status": "action_required",
               "error": "FLOW_LOGIN_REQUIRED: redirected to /about"}

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            (account if table == "accounts" else job).update(data)
            return job

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[job]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch("pipeline.flow.service.threading.Thread") as thread, \
             patch.object(service, "_log"), patch.object(service, "connect") as connect:
            self.assertEqual(service._requeue_auth_failed_jobs("a"), 1)
            job.update(status="action_required", error="FLOW_LOGIN_REQUIRED: redirected to /about")
            service._ensure_shared_reconnect("a", job_id="j", reason=job["error"])
            self.assertEqual(service._requeue_auth_failed_jobs("a"), 0)
            connect.assert_not_called()
            thread.assert_called_once()

    def test_explicit_retry_resets_auth_replay_budget(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "projectId": "p", "plan": "Ultra", "credits": 100}
        job = {"id": "j", "accountId": "a", "kind": "image", "mode": "text",
               "status": "action_required", "authRetryCount": 1, "settings": {}}

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            job.update(data)
            return job

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch("pipeline.flow.service.threading.Thread"), patch.object(service, "_log"):
            retried = service.retry("j")
        self.assertEqual(retried["authRetryCount"], 0)
        self.assertEqual(retried["accountId"], "a")
        self.assertEqual(retried["status"], "queued")

    def test_manual_retry_clears_selected_account_suspension(self):
        service = FlowService()
        account = {
            "id": "a", "status": "online", "projectId": "p", "plan": "Ultra",
            "credits": 100, "suspendedUntil": 9999999999,
            "suspendReason": "FLOW_AUTOMATION_BLOCKED: abnormal activity",
        }
        job = {
            "id": "j", "accountId": "a", "kind": "image", "mode": "text",
            "status": "failed", "settings": {},
        }

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            job.update(data)
            return job

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "clear_account_suspension") as clear, \
             patch("pipeline.flow.service.threading.Thread"), patch.object(service, "_log"):
            service.retry("j", {"manual": True})
            clear.assert_called_once_with("a")
            clear.reset_mock()
            service.retry("j")
            clear.assert_not_called()

    def test_blocked_selected_account_never_falls_back(self):
        for reason in ("FLOW_AUTOMATION_BLOCKED: warning", "FLOW_OUTPUT_UNAVAILABLE: unavailable",
                       "FLOW_GENERATION_TIMEOUT: timeout"):
            for explicit in (False, None):
                with self.subTest(reason=reason, explicit=explicit):
                    service = FlowService()
                    job = {"id": "j", "status": "failed", "accountId": "selected", "randomAccount": False}
                    if explicit is not None:
                        job["allowAccountFallback"] = explicit
                    with patch("pipeline.flow.service.store.get_row", return_value=job), \
                         patch("pipeline.flow.service.store.patch_row") as update, \
                         patch.object(service, "suspend_account"), \
                         patch.object(service, "_pick_eligible_account") as picker:
                        self.assertFalse(service._try_fallback_account("j", "selected", reason))
                        picker.assert_not_called()
                        update.assert_not_called()

    def test_generation_timeout_does_not_suspend_account(self):
        service = FlowService()
        account = {"id": "a", "label": "account", "status": "online"}
        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch("pipeline.flow.service.store.patch_row") as patch_row:
            result = service.suspend_account("a", "FLOW_GENERATION_TIMEOUT: no completed image media appeared after submit")
        self.assertEqual(result["id"], "a")
        patch_row.assert_not_called()

    def test_automation_block_reconnects_once_then_stops(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "automationReconnectCount": 0}
        job = {"id": "j", "accountId": "a", "status": "failed"}

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            target = account if table == "accounts" else job
            target.update(data)
            return target

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "reset_account_session") as reset_session, \
             patch.object(service, "_log"):
            self.assertTrue(service._schedule_automation_reconnect("a", "j", "FLOW_AUTOMATION_BLOCKED: unusual activity"))
            reset_session.assert_called_once_with("a", auto_recovery=True)
            reset_session.reset_mock()
            self.assertFalse(service._schedule_automation_reconnect("a", "j", "FLOW_AUTOMATION_BLOCKED: unusual activity"))
            reset_session.assert_not_called()
        self.assertEqual(account["automationReconnectCount"], 1)
        self.assertEqual(job["status"], "action_required")
        self.assertTrue(job["error"].startswith("FLOW_LOGIN_REQUIRED:"))

    def test_image_session_closes_when_only_video_jobs_are_queued(self):
        service = FlowService()
        browser = MagicMock()
        service._manual_sessions["a:image"] = {"browser": browser, "cdp_url": "http://127.0.0.1:1"}
        service._auto_close_sessions.add("a:image")
        with patch("pipeline.flow.service.store.list_rows", return_value=[
            {"id": "video", "accountId": "a", "kind": "video", "status": "queued"},
        ]), patch.object(service, "_log"):
            service._close_auto_session_if_idle("a", "image")
        browser.kill_now.assert_called_once()
        self.assertNotIn("a", service._auto_close_sessions)

    def test_stale_reconnect_status_does_not_hold_queue_forever(self):
        service = FlowService()
        with patch("pipeline.flow.service.store.get_row", return_value={
            "id": "a", "status": "reconnect", "error": "login failed",
        }):
            self.assertFalse(service._account_session_blocked("a"))

    async def test_page_shell_warning_does_not_fail_pending_generation(self):
        service = FlowService()
        page = MagicMock()
        def evaluate(script, *args):
            if "document.body" in script:
                return "Skip to main content home Older generation: suspicious activity"
            return []
        page.evaluate = AsyncMock(side_effect=evaluate)
        result = {"id": "new", "tag": "img", "src": "https://example.test/image.png"}
        with patch.object(service, "_project_media_elements", AsyncMock(side_effect=[[], [result]])), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j"}), \
             patch("pipeline.flow.service.store.patch_row"), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            self.assertEqual(await service._wait_for_project_media(page, set(), "image", 1, "j"), [result])

    async def test_real_visible_tile_block_is_reported(self):
        service = FlowService()
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=[{"index": 0, "text": "Chúng tôi nhận thấy có hoạt động bất thường"}])
        with patch.object(service, "_project_media_elements", AsyncMock(return_value=[])), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j"}), \
             patch("pipeline.flow.service.store.patch_row"):
            with self.assertRaisesRegex(RuntimeError, "FLOW_AUTOMATION_BLOCKED"):
                await service._wait_for_project_media(page, set(), "image", 1, "j")


if __name__ == "__main__":
    unittest.main()
