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
