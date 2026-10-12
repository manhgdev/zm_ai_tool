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
             patch("pipeline.flow.service.store.list_rows", return_value=[job]), \
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

    def test_automation_block_defers_reset_until_account_is_idle(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "automationReconnectCount": 0}
        failed = {"id": "failed", "accountId": "a", "status": "failed"}
        active = {"id": "active", "accountId": "a", "status": "processing"}
        queued = {"id": "queued", "accountId": "a", "status": "queued"}
        jobs = [failed, active, queued]

        def get_row(table, row_id):
            if table == "accounts":
                return account
            return next((row for row in jobs if row["id"] == row_id), None)

        def patch_row(table, row_id, data):
            target = account if table == "accounts" else next(row for row in jobs if row["id"] == row_id)
            target.update(data)
            return target

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", side_effect=lambda table: jobs), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "reset_account_session") as reset_session, \
             patch.object(service, "_log"):
            self.assertTrue(service._schedule_automation_reconnect(
                "a", "failed", "FLOW_AUTOMATION_BLOCKED: unusual activity",
            ))
            reset_session.assert_not_called()
            self.assertTrue(account["automationRecoveryPending"])
            self.assertTrue(service._account_session_blocked("a"))
            active["status"] = "done"
            self.assertTrue(service._maybe_start_automation_recovery("a"))
            reset_session.assert_called_once_with("a", auto_recovery=True)

    def test_unrelated_success_does_not_clear_recovery_budget(self):
        service = FlowService()
        account = {
            "id": "a", "automationReconnectCount": 1,
            "automationRecoveryJobId": "recovery",
        }
        unrelated = {"id": "other", "accountId": "a", "status": "done"}
        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch("pipeline.flow.service.store.patch_row") as patch_row:
            service._finish_automation_recovery_if_owned("a", unrelated)
        patch_row.assert_not_called()

    def test_recovery_success_clears_only_its_owned_budget(self):
        service = FlowService()
        account = {
            "id": "a", "automationReconnectCount": 1,
            "automationRecoveryJobId": "recovery",
        }
        recovery = {
            "id": "recovery", "accountId": "a", "status": "done",
            "automationRecoveryJobId": "recovery",
        }

        def patch_row(table, row_id, data):
            account.update(data)
            return account

        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row) as patch_store:
            service._finish_automation_recovery_if_owned("a", recovery)
        self.assertGreaterEqual(patch_store.call_count, 2)
        self.assertEqual(patch_store.call_args_list[-1].args[2]["automationReconnectCount"], 0)

    def test_parallel_cohort_keeps_budget_until_all_workers_finish(self):
        service = FlowService()
        account = {
            "id": "a", "automationReconnectCount": 1,
            "automationRecoveryJobId": "recovery",
            "automationRecoveryActiveJobIds": ["old"],
        }
        recovery = {
            "id": "recovery", "accountId": "a", "status": "done",
            "automationRecoveryJobId": "recovery",
        }

        def patch_row(table, row_id, data):
            account.update(data)
            return account

        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row):
            service._finish_automation_recovery_if_owned("a", recovery)
            self.assertEqual(account["automationReconnectCount"], 1)
            self.assertTrue(account["automationRecoveryOwnerDone"])
            service._mark_automation_recovery_job_finished("a", "old")
        self.assertEqual(account["automationReconnectCount"], 0)
        self.assertIsNone(account["automationRecoveryJobId"])

    def test_parallel_old_session_block_joins_existing_recovery_cycle(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "automationReconnectCount": 0}
        first = {"id": "first", "accountId": "a", "status": "failed"}
        old = {"id": "old", "accountId": "a", "status": "processing"}
        second = {"id": "second", "accountId": "a", "status": "processing"}
        jobs = [first, old, second]

        def get_row(table, row_id):
            if table == "accounts":
                return account
            return next((row for row in jobs if row["id"] == row_id), None)

        def patch_row(table, row_id, data):
            target = account if table == "accounts" else next(row for row in jobs if row["id"] == row_id)
            target.update(data)
            return target

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=jobs), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "reset_account_session") as reset_session, \
             patch.object(service, "_log"):
            self.assertTrue(service._schedule_automation_reconnect(
                "a", "first", "FLOW_AUTOMATION_BLOCKED: first block",
            ))
            self.assertTrue(service._schedule_automation_reconnect(
                "a", "second", "FLOW_AUTOMATION_BLOCKED: second block",
            ))
            self.assertTrue(service._account_session_blocked("a"))
        reset_session.assert_not_called()
        self.assertTrue(account["automationRecoveryPending"])
        self.assertEqual(second["automationRecoveryJobId"], "first")

    def test_second_block_from_post_reset_worker_holds_queue_without_reset(self):
        service = FlowService()
        account = {
            "id": "a", "status": "online", "automationReconnectCount": 1,
            "automationRecoveryJobId": None,
            "automationRecoveryPending": False,
        }
        queued = {"id": "queued", "accountId": "a", "status": "queued"}
        second = {"id": "second", "accountId": "a", "status": "failed"}
        jobs = [queued, second]

        def get_row(table, row_id):
            if table == "accounts":
                return account
            return next((row for row in jobs if row["id"] == row_id), None)

        def patch_row(table, row_id, data):
            target = account if table == "accounts" else next(row for row in jobs if row["id"] == row_id)
            target.update(data)
            return target

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=jobs), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "reset_account_session") as reset_session, \
             patch.object(service, "_log"):
            self.assertFalse(service._schedule_automation_reconnect(
                "a", "second", "FLOW_AUTOMATION_BLOCKED: second block",
            ))
            service._mark_automation_recovery_blocked("a", "FLOW_AUTOMATION_BLOCKED: second block")
        reset_session.assert_not_called()
        self.assertTrue(account["automationRecoveryBlocked"])
        self.assertEqual(queued["status"], "queued")

    def test_automation_recovery_requeues_only_marked_job(self):
        service = FlowService()
        account = {
            "id": "a", "status": "online", "projectId": "p",
            "automationRecoveryJobId": "recovery",
        }
        jobs = [
            {
                "id": "recovery", "accountId": "a", "status": "action_required",
                "error": "FLOW_LOGIN_REQUIRED: redirected to login",
                "automationRecoveryJobId": "recovery",
            },
            {
                "id": "other", "accountId": "a", "status": "action_required",
                "error": "FLOW_LOGIN_REQUIRED: redirected to login",
            },
            {
                "id": "sibling", "accountId": "a", "status": "action_required",
                "error": "FLOW_LOGIN_REQUIRED: joining the shared Flow recovery cycle",
                "automationRecoveryJobId": "recovery",
            },
        ]

        def patch_row(table, row_id, data):
            row = next(job for job in jobs if job["id"] == row_id)
            row.update(data)
            return row

        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch("pipeline.flow.service.store.list_rows", return_value=jobs), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch("pipeline.flow.service.threading.Thread") as thread, \
             patch.object(service, "_log"):
            self.assertEqual(service._requeue_auth_failed_jobs("a"), 2)
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertEqual(jobs[1]["status"], "action_required")
        self.assertEqual(jobs[2]["status"], "queued")
        self.assertEqual(thread.call_count, 2)
        self.assertTrue(all(
            call.kwargs["name"] == "flow-reauth-" + call.kwargs["args"][0]
            for call in thread.call_args_list
        ))

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
