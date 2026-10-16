"""Run: PYTHONPATH=backend backend/.venv/bin/python tests/test_flow_block_recovery.py"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pipeline.flow import store
from pipeline.flow.service import FlowService


BLOCK = "FLOW_AUTOMATION_BLOCKED: unusual activity"


class FlowBlockRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(store, "ROOT", Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        store._CACHE.clear()
        self.addCleanup(store._CACHE.clear)
        self.threads = patch("pipeline.flow.recovery.threading.Thread")
        self.thread = self.threads.start()
        self.addCleanup(self.threads.stop)
        network = patch("pipeline.flow.worker._network_ready_for_flow_job", return_value=True)
        network.start()
        self.addCleanup(network.stop)
        self.service = FlowService()
        self.account = {"id": "a", "status": "online", "plan": "Pro", "credits": 100,
                        "imageProjectId": "old-project"}
        store.put_row("accounts", self.account)
        self.service._verify_account_plan_before_enqueue = Mock(return_value=self.account)
        self.service._account_session_blocked = Mock(return_value=False)
        self.service._session_warmup_wait = Mock(return_value=False)
        self.service._account_suspended = Mock(return_value=False)
        self.service._close_auto_session_if_idle = Mock()
        self.service._pick_eligible_account = Mock(return_value={"id": "b", "label": "B"})
        self.service.suspend_account = Mock()

    def job(self, **changes):
        row = {"id": "job", "accountId": "a", "kind": "image", "queueOrder": 0,
               "status": "queued", "stage": "queued", "settings": {"model": "Nano Banana 2.1"},
               "randomAccount": True, "allowAccountFallback": True, "triedAccountIds": ["a"],
               "mediaIds": [], "outputs": [], "submissionStartedAt": 123.0,
               "submissionProjectId": "old-project", "lastGenerationRejectedSubmissionAt": 123.0,
               **changes}
        store.put_row("jobs", row)
        return row

    def run_blocked_worker(self):
        async def fail(job_id, **kwargs):
            store.patch_row("jobs", job_id, {"status": "failed", "stage": "failed", "error": BLOCK})
        self.service._run = fail
        self.service._run_sync("job")
        return store.get_row("jobs", "job")

    def test_first_block_resets_and_creates_a_fresh_generation(self):
        self.job()
        self.service._start_cookie_recovery = Mock()
        result = self.run_blocked_worker()
        self.assertEqual(result["accountId"], "a")
        self.assertEqual(result["stage"], "waiting_session_reset")
        self.assertEqual(result["sessionResetRetryCount"], 1)
        self.assertIsNone(result["submissionStartedAt"])
        self.service._start_cookie_recovery.assert_called_once_with("a", job_id="job", reason=BLOCK)
        self.service._pick_eligible_account.assert_not_called()

    def test_second_block_switches_account_in_worker_finally(self):
        self.job(sessionResetRetryCount=1, resumeOnly=True)
        self.service._start_cookie_recovery = Mock()
        result = self.run_blocked_worker()
        self.assertEqual(result["accountId"], "b")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["triedAccountIds"], ["a", "b"])
        self.assertEqual(result["sessionResetRetryCount"], 0)
        self.assertIsNone(result["submissionProjectId"])
        self.assertIsNone(result["lastGenerationRejectedSubmissionAt"])
        self.assertFalse(result["resumeOnly"])
        self.service._start_cookie_recovery.assert_not_called()
        self.service._pick_eligible_account.assert_called_once_with(
            kind="image", model="Nano Banana 2.1", exclude_ids=["a"])
        self.assertEqual(self.thread.call_args.kwargs["name"], "flow-job-job-fallback")
        self.thread.return_value.start.assert_called_once()

    def test_opted_in_account_requeues_second_headless_block_on_the_same_account(self):
        store.patch_row("accounts", "a", {"headlessFallbackEnabled": True})
        self.job(sessionResetRetryCount=1, effectiveHeadless=True)
        with patch("pipeline.flow.recovery._display_available", return_value=True):
            result = self.run_blocked_worker()
        self.assertEqual(result["accountId"], "a")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["headedFallbackCount"], 1)
        self.assertIsNone(result["submissionStartedAt"])
        self.assertTrue(store.get_row("accounts", "a")["headlessFallbackAt"])
        self.service._pick_eligible_account.assert_not_called()
        self.assertEqual(self.thread.call_args.kwargs["name"], "flow-job-retry-job")
        self.thread.return_value.start.assert_called_once()

    def test_headed_fallback_is_bounded_and_preserves_accepted_media(self):
        store.patch_row("accounts", "a", {"headlessFallbackEnabled": True})
        for change in ({"headedFallbackCount": 1}, {"effectiveHeadless": False},
                       {"mediaIds": ["media"]}, {"outputs": ["output.jpg"]},
                       {"status": "cancelled"}, {"sessionResetRetryCount": 0}):
            with self.subTest(change=change):
                self.job(**{"status": "failed", "sessionResetRetryCount": 1,
                            "effectiveHeadless": True, **change})
                with patch("pipeline.flow.recovery._display_available", return_value=True):
                    self.assertFalse(self.service._try_headed_fallback("job", "a", BLOCK))
        self.job(status="failed", sessionResetRetryCount=1, effectiveHeadless=True)
        with patch("pipeline.flow.recovery._display_available", return_value=False):
            self.assertFalse(self.service._try_headed_fallback("job", "a", BLOCK))
        self.assertFalse(self.service._try_headed_fallback("job", "a", "FLOW_COOKIE_EXPIRED"))
        self.service._cancelled.add("job")
        self.assertFalse(self.service._try_headed_fallback("job", "a", BLOCK))

    def test_account_edit_preserves_fallback_until_explicitly_disabled(self):
        self.service.save_account({"headlessFallbackEnabled": True}, "a")
        store.patch_row("accounts", "a", {"headlessFallbackAt": 123.0})
        row = self.service.save_account({"label": "changed"}, "a")
        self.assertTrue(row["headlessFallbackEnabled"])
        self.assertEqual(row["headlessFallbackAt"], 123.0)
        row = self.service.save_account({"headlessFallbackEnabled": False}, "a")
        self.assertFalse(row["headlessFallbackEnabled"])
        self.assertIsNone(row["headlessFallbackAt"])

    def test_only_opted_in_background_jobs_use_the_account_headed_mode(self):
        self.threads.stop()  # asyncio.to_thread needs a real executor thread.
        self.service._clone_runtime_profile = Mock(return_value=Path(self.temp.name) / "profile")
        for enabled, override, expected in ((True, None, (False, (-30000, -30000))),
                                            (False, None, (True, None)),
                                            (True, False, (False, None))):
            with self.subTest(enabled=enabled, override=override):
                store.patch_row("accounts", "a", {
                    "headlessFallbackEnabled": enabled, "headlessFallbackAt": 123.0,
                })
                self.job(settings={"headless": True}, submissionStartedAt=None)
                with patch("pipeline.flow.browser.BrowserManager", side_effect=RuntimeError("test stop before launch")) as browser, \
                     patch("pipeline.flow.worker.network_available", return_value=True):
                    asyncio.run(self.service._run_once("job", headless_override=override))
                self.assertEqual((browser.call_args.kwargs["headless"], browser.call_args.kwargs["window_position"]), expected)

    def test_block_always_creates_fresh_even_without_a_tile_marker(self):
        self.job(sessionResetRetryCount=1, lastGenerationRejectedSubmissionAt=100.0)
        result = self.run_blocked_worker()
        self.assertEqual(result["accountId"], "b")
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(result["submissionStartedAt"])
        self.assertIsNone(result["submissionProjectId"])

    def test_recovery_marks_the_whole_live_cohort_once(self):
        self.job(status="queued", sessionResetRetryCount=1)
        self.job(id="sibling", status="processing", sessionResetRetryCount=0)
        self.job(id="done", status="done", sessionResetRetryCount=0)
        self.service._reset_account_session_locked = Mock()
        self.service._start_cookie_recovery("a", job_id="job", reason=BLOCK)
        self.assertEqual(store.get_row("jobs", "sibling")["sessionResetRetryCount"], 1)
        self.assertIsNone(store.get_row("jobs", "sibling")["recoveryNotice"])
        self.assertEqual(store.get_row("jobs", "done")["sessionResetRetryCount"], 0)
        self.service._start_cookie_recovery("a", job_id="sibling", reason=BLOCK)
        self.thread.assert_called_once()
        self.thread.call_args.kwargs["target"]()
        self.service._reset_account_session_locked.assert_called_once_with(
            "a", open_ui_on_failure=False, keep_running_jobs=True)

    def test_fallback_preserves_media_and_account_selection(self):
        for change in ({"mediaIds": ["media"]}, {"outputs": ["output.jpg"]},
                       {"allowAccountFallback": False}, {"sessionResetRetryCount": 0}):
            with self.subTest(change=change):
                self.job(**{"sessionResetRetryCount": 1, "submissionStartedAt": None, **change})
                self.assertFalse(self.service._try_fallback_account("job", "a", BLOCK))
        self.service._pick_eligible_account.assert_not_called()

    def test_account_fallback_clears_old_submission(self):
        self.job(status="failed", error="FLOW_GENERATION_TIMEOUT")
        self.assertTrue(self.service._try_fallback_account("job", "a", "FLOW_GENERATION_TIMEOUT"))
        result = store.get_row("jobs", "job")
        self.assertEqual(result["accountId"], "b")
        self.assertIsNone(result["submissionStartedAt"])
        self.assertIsNone(result["submissionProjectId"])
        self.assertFalse(result["resumeOnly"])

    def test_unresolved_accepted_image_stops_without_repeating_recovery_or_switching_account(self):
        self.job()
        async def timeout(job_id, **kwargs):
            store.patch_row("jobs", job_id, {
                "status": "failed", "stage": "failed", "resumeOnly": True,
                "error": "FLOW_GENERATION_TIMEOUT: original result could not be collected",
            })
        self.service._run = AsyncMock(side_effect=timeout)
        self.service._run_sync("job")
        result = store.get_row("jobs", "job")
        self.service._run.assert_awaited_once()
        self.service._pick_eligible_account.assert_not_called()
        self.assertEqual(result["accountId"], "a")
        self.assertEqual(result["submissionStartedAt"], 123.0)
        self.assertTrue(result["resumeOnly"])

    def test_single_and_batch_retry_recover_only_on_the_same_account(self):
        store.put_row("accounts", {**self.account, "id": "b", "imageProjectId": "new-project"})
        self.service._outputs_exist = Mock(return_value=False)
        for batch in (False, True):
            for account_id, error, expected_recovery in (
                ("a", "FLOW_GENERATION_TIMEOUT", True),
                ("b", "FLOW_GENERATION_TIMEOUT", False),
                ("a", BLOCK, False),
                ("b", "FLOW_DOWNLOAD_FAILED", False),
            ):
                with self.subTest(batch=batch, account=account_id, error=error):
                    self.job(status="failed", error=error, resumeOnly=True)
                    payload = {"accountId": account_id, "manual": True}
                    if batch:
                        result = self.service.retry_many([{"jobId": "job", **payload}])
                        self.assertEqual(result["rejectedCount"], 0)
                    else:
                        self.service.retry("job", payload)
                    row = store.get_row("jobs", "job")
                    self.assertEqual(row["accountId"], account_id)
                    self.assertEqual(row["resumeOnly"], expected_recovery)
                    if expected_recovery:
                        self.assertEqual(row["submissionProjectId"], "old-project")
                        self.assertEqual(row["submissionStartedAt"], 123.0)
                    else:
                        self.assertIsNone(row["submissionProjectId"])
                        self.assertIsNone(row["submissionStartedAt"])
                        self.assertEqual(row["mediaIds"], [])
                        self.assertIsNone(row["lastGenerationRejectedSubmissionAt"])

    def test_video_download_keeps_ids_only_when_account_is_unchanged(self):
        store.put_row("accounts", {**self.account, "id": "b", "projectId": "new-project"})
        store.patch_row("accounts", "a", {"projectId": "old-project"})
        self.service._outputs_exist = Mock(return_value=False)
        for batch in (False, True):
            for account_id in ("a", "b"):
                with self.subTest(batch=batch, account=account_id):
                    self.job(kind="video", status="failed", error="FLOW_DOWNLOAD_FAILED", mediaIds=["media"])
                    payload = {"accountId": account_id, "manual": True}
                    if batch:
                        result = self.service.retry_many([{"jobId": "job", **payload}])
                        self.assertEqual(result["rejectedCount"], 0)
                    else:
                        self.service.retry("job", payload)
                    row = store.get_row("jobs", "job")
                    self.assertEqual(row["resumeOnly"], account_id == "a")
                    self.assertEqual(row["mediaIds"], ["media"] if account_id == "a" else [])


class FlowImageCollectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = patch.object(store, "ROOT", Path(self.temp.name))
        root.start()
        self.addCleanup(root.stop)
        store._CACHE.clear()
        self.addCleanup(store._CACHE.clear)
        self.service = FlowService()
        self.job = {"id": "job", "accountId": "a", "kind": "image", "prompt": "con gà",
                    "status": "processing", "stage": "recovering", "progress": 15,
                    "settings": {"count": 1}, "submissionStartedAt": 123.0,
                    "submissionProjectId": "project", "mediaIds": [], "outputs": []}
        store.put_row("jobs", self.job)
        self.item = {"id": "chicken", "tag": "img", "src": "https://example.test/chicken.jpg", "width": 512}
        self.service._claim_visible_flow_error = AsyncMock(return_value="")
        self.service._find_existing_project_media = AsyncMock(return_value=[])
        self.service._flow_tile_percent = AsyncMock(return_value=-1)
        self.service._flow_pending_generation = AsyncMock(return_value=False)
        self.now = 0.0
        self.stages = []
        async def tick(seconds):
            self.now += seconds
            self.stages.append((store.get_row("jobs", "job") or {}).get("stage"))
            if self.now > 500:
                self.fail("Flow collection did not stop at its deadline")
        clock = patch("pipeline.flow.media.time", SimpleNamespace(monotonic=lambda: self.now, time=lambda: 1000 + self.now))
        clock.start()
        self.addCleanup(clock.stop)
        sleep = patch("pipeline.flow.media.asyncio.sleep", tick)
        sleep.start()
        self.addCleanup(sleep.stop)

    async def test_completed_known_id_skips_slow_project_api_and_unrelated_error(self):
        for field in ("mediaIds", "rpcMediaIds"):
            with self.subTest(identity=field):
                job = {**self.job, field: ["chicken"]}
                store.put_row("jobs", job)
                self.service._claimed_media_ids.clear()
                self.service._find_existing_project_media = FlowService._find_existing_project_media.__get__(self.service)
                self.service._project_media_elements = AsyncMock(return_value=[self.item])
                api = SimpleNamespace(get_project_data=AsyncMock(side_effect=AssertionError("unneeded API probe")))
                result = await self.service._recover_submitted_media(api, object(), job)
                self.assertEqual(result, ["chicken"])
                api.get_project_data.assert_not_awaited()
                self.service._claim_visible_flow_error.assert_not_awaited()

    async def test_missing_tile_stops_after_short_lookup_and_keeps_submission(self):
        with self.assertRaisesRegex(RuntimeError, "FLOW_GENERATION_TIMEOUT"):
            await self.service._recover_submitted_media(None, object(), self.job)
        self.assertLessEqual(self.now, 8)
        row = store.get_row("jobs", "job")
        self.assertEqual(row["submissionStartedAt"], 123.0)
        self.assertEqual(row["submissionProjectId"], "project")
        self.assertTrue(row["resumeOnly"])

    async def test_active_tile_keeps_generating_until_its_completed_image_is_collected(self):
        self.service._find_existing_project_media.side_effect = [[], [], [self.item]]
        self.service._flow_tile_percent.side_effect = [7, 48]
        result = await self.service._recover_submitted_media(None, object(), self.job)
        self.assertEqual(result, ["chicken"])
        self.assertEqual(self.stages, ["generating", "generating"])
        self.assertEqual(store.get_row("jobs", "job")["stage"], "downloading")

    async def test_recovery_does_not_extend_a_stuck_99_percent_tile_forever(self):
        self.service._flow_tile_percent.return_value = 99
        with self.assertRaisesRegex(RuntimeError, "FLOW_GENERATION_TIMEOUT"):
            await self.service._recover_submitted_media(None, object(), self.job, timeout_s=3)
        self.assertLessEqual(self.now, 3)
        self.assertTrue(all(stage == "generating" for stage in self.stages))

    async def test_healthy_pending_prompt_does_not_claim_a_sibling_failure(self):
        self.service._claim_visible_flow_error = FlowService._claim_visible_flow_error.__get__(self.service)
        self.service._flow_pending_generation.return_value = True
        self.service._scan_visible_errors = AsyncMock(return_value=[{"text": BLOCK, "tile": True, "prompt": "con vịt"}])
        self.assertEqual(await self.service._claim_visible_flow_error(object(), self.job, 1), "")
        self.service._scan_visible_errors.return_value = [{"text": BLOCK, "tile": True, "prompt": "con gà"}]
        self.assertEqual(await self.service._claim_visible_flow_error(object(), self.job, 1), BLOCK)

    async def test_initial_wait_respects_hard_cap_at_99_percent(self):
        self.service._project_media_elements = AsyncMock(return_value=[])
        self.service._flow_tile_percent.return_value = 99
        self.service._flow_pending_generation.return_value = True
        with self.assertRaisesRegex(RuntimeError, "FLOW_GENERATION_TIMEOUT"):
            await self.service._wait_for_project_media(object(), set(), "image", 1, "job", timeout_s=2, job=self.job)
        self.assertLessEqual(self.now, 10)


if __name__ == "__main__":
    unittest.main()
