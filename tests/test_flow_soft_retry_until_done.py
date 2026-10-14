"""Soft failures retry a finite budget; hard stops never soft-loop."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow import store
from pipeline.flow.service import FlowService, _JOB_AUTO_RETRY_MAX, _soft_retry_backoff_s, _SOFT_RETRY_BACKOFF_CAP_S


class FlowSoftRetryBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root_patch = patch.object(store, "ROOT", Path(self.directory.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.service = FlowService()
        self.account = {
            "id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100,
            "imageProjectId": "p",
        }
        store._write("accounts", [self.account])
        store._write("jobs", [{
            "id": "j", "accountId": "a", "status": "queued", "kind": "image",
            "queueOrder": 0, "settings": {}, "prompts": ["hello"],
        }])

    def test_soft_timeout_retries_within_budget_then_succeeds(self) -> None:
        attempts = {"n": 0}

        async def flaky_run(job_id, **kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                store.patch_row("jobs", job_id, {
                    "status": "failed", "stage": "failed",
                    "error": "FLOW_GENERATION_TIMEOUT: no completed image media appeared after submit",
                    "mediaIds": [], "outputs": [],
                })
                return
            store.patch_row("jobs", job_id, {
                "status": "done", "stage": "done", "progress": 100,
                "error": None, "outputs": ["/tmp/ok.png"], "mediaIds": ["m1"],
            })

        with patch.object(self.service, "_verify_account_plan_before_enqueue", return_value=self.account), \
             patch.object(self.service, "_run", side_effect=flaky_run), \
             patch("pipeline.flow.service.time.sleep"), \
             patch("pipeline.flow.service._network_ready_for_flow_job", return_value=True):
            self.service._run_sync("j")
        job = store.get_row("jobs", "j")
        self.assertEqual(job["status"], "done")
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(int(job.get("autoRetryCount") or 0), 2)

    def test_soft_timeout_stops_after_finite_budget(self) -> None:
        async def always_timeout(job_id, **kwargs):
            store.patch_row("jobs", job_id, {
                "status": "failed", "stage": "failed",
                "error": "FLOW_GENERATION_TIMEOUT: no completed image media appeared after submit",
                "mediaIds": [], "outputs": [],
            })

        with patch.object(self.service, "_verify_account_plan_before_enqueue", return_value=self.account), \
             patch.object(self.service, "_run", side_effect=always_timeout) as run, \
             patch("pipeline.flow.service.time.sleep") as sleep, \
             patch("pipeline.flow.service._network_ready_for_flow_job", return_value=True):
            self.service._run_sync("j")
        # 1 initial + _JOB_AUTO_RETRY_MAX soft retries
        self.assertEqual(run.call_count, 1 + _JOB_AUTO_RETRY_MAX)
        self.assertEqual(sleep.call_count, _JOB_AUTO_RETRY_MAX)
        job = store.get_row("jobs", "j")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(int(job.get("autoRetryCount") or 0), _JOB_AUTO_RETRY_MAX)

    def test_credits_hard_stop_does_not_soft_retry(self) -> None:
        async def credits_fail(job_id, **kwargs):
            store.patch_row("jobs", job_id, {
                "status": "failed", "stage": "failed",
                "error": "FLOW_CREDITS_EMPTY: Out of Flow credits",
                "mediaIds": [], "outputs": [],
            })

        with patch.object(self.service, "_verify_account_plan_before_enqueue", return_value=self.account), \
             patch.object(self.service, "_run", side_effect=credits_fail) as run, \
             patch("pipeline.flow.service.time.sleep") as sleep, \
             patch("pipeline.flow.service._network_ready_for_flow_job", return_value=True):
            self.service._run_sync("j")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(store.get_row("jobs", "j")["status"], "failed")

    def test_soft_retry_backoff_caps(self) -> None:
        self.assertEqual(_soft_retry_backoff_s(1), 3.0)
        self.assertEqual(_soft_retry_backoff_s(2), 6.0)
        self.assertLessEqual(_soft_retry_backoff_s(99), _SOFT_RETRY_BACKOFF_CAP_S)


if __name__ == "__main__":
    unittest.main()
