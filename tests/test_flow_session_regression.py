"""Selected account and bounded session recovery regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pipeline.flow import store
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

    def test_unusual_activity_retries_only_that_job_in_fresh_chrome(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            account = {"id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100}
            store._write("accounts", [account])
            store._write("jobs", [
                {"id": "j", "accountId": "a", "status": "queued", "kind": "image", "queueOrder": 0, "settings": {}},
                {"id": "sibling", "accountId": "a", "status": "processing", "kind": "video", "queueOrder": 1, "settings": {}},
            ])

            attempts = []

            async def run(job_id, **kwargs):
                attempts.append(job_id)
                if len(attempts) == 1:
                    store.patch_row("jobs", job_id, {
                        "status": "failed", "stage": "failed", "submissionStartedAt": 1.0,
                        "error": "FLOW_AUTOMATION_BLOCKED: We noticed some unusual activity",
                    })
                else:
                    store.patch_row("jobs", job_id, {"status": "done", "stage": "done"})

            with patch.object(service, "_verify_account_plan_before_enqueue", return_value=account), \
                 patch.object(service, "_run", side_effect=run) as runner, \
                 patch("pipeline.flow.service.time.sleep"):
                service._run_sync("j")
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(store.get_row("jobs", "j")["status"], "done")
            self.assertEqual(store.get_row("jobs", "sibling")["status"], "processing")
            refreshed = store.get_row("accounts", "a")
            self.assertEqual(refreshed["status"], "online")
            self.assertFalse(refreshed.get("suspendedUntil"))
            self.assertFalse(service._account_session_blocked("a"))

    async def test_rising_flow_progress_extends_generation_timeout(self):
        service = FlowService()
        clock = iter(range(0, 10_000, 1))
        result = {"id": "new", "tag": "img", "src": "https://example.test/image.png"}
        # Base timeout is 3s; Flow keeps advancing, so the result at tick ~8 is still collected.
        media = [[]] * 6 + [[result]]
        with patch("pipeline.flow.service.time.monotonic", side_effect=lambda: next(clock)), \
             patch.object(service, "_project_media_elements", AsyncMock(side_effect=media)), \
             patch.object(service, "_claim_visible_flow_error", AsyncMock(return_value="")), \
             patch.object(service, "_flow_tile_percent", AsyncMock(side_effect=[10, 20, 30, 40, 50, 60])), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j", "progress": 0}), \
             patch("pipeline.flow.service.store.patch_row"), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            self.assertEqual(await service._wait_for_project_media(page=None, baseline_ids=set(), kind="image",
                                                                   expected_count=1, job_id="j", timeout_s=3), [result])

    async def test_rpc_owned_media_is_claimed_when_flow_rewrites_the_prompt(self):
        service = FlowService()
        response = MagicMock(ok=True)
        response.json = AsyncMock(return_value={"media": [{
            "name": "m1", "image": {"generatedImage": {"fifeUrl": "https://flow.test/m1.jpg"}},
        }]})
        items, names = await service._rpc_media([response])
        self.assertEqual(items, [{"id": "m1", "src": "https://flow.test/m1.jpg"}])
        api = MagicMock()
        api.get_project_data = AsyncMock(return_value={"projectContents": {"media": [{
            "name": "m1", "image": {"generatedImage": {"fifeUrl": "https://flow.test/m1.jpg"}},
            "mediaMetadata": {"requestData": {"promptInputs": [{"textInput": "Morning mist over lake"}]}},
        }]}})
        job = {"id": "j", "prompt": "Buổi sáng yên bình bên hồ", "submissionStartedAt": 1.0, "rpcMediaIds": names}
        with patch.object(service, "jobs", return_value=[]):
            found = await service._find_existing_project_media(api, None, job, "image", 1)
        self.assertEqual([item["id"] for item in found], ["m1"])

    async def test_batchexecute_unusual_activity_fails_fast_as_automation_blocked(self):
        response = MagicMock(ok=True, status=200)
        response.text = AsyncMock(return_value=')]}\'\n[["wrb.fr","YhhmEf",null,null,null,[7,null,'
                                  '[["type.googleapis.com/google.rpc.ErrorInfo",["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]]]]]]')
        button = MagicMock(hover=AsyncMock(), click=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "FLOW_AUTOMATION_BLOCKED"):
            await FlowService()._click_flow_submit_and_confirm(
                MagicMock(), button, 0, "", set(), [object()], [response],
            )

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
