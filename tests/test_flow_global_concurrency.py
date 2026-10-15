"""Luồng chạy is a process-wide cap, not per-account lanes."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow import store
from pipeline.flow.service import FlowService


class FlowGlobalConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root_patch = patch.object(store, "ROOT", Path(self.directory.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.service = FlowService()
        store._write("accounts", [
            {"id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100, "imageProjectId": "p"},
            {"id": "b", "status": "online", "projectId": "p2", "plan": "Pro", "credits": 100, "imageProjectId": "p2"},
        ])

    def test_admission_waits_when_global_cap_full_across_accounts(self) -> None:
        """concurrency=2 with one active on a and one on b blocks a third job."""
        store._write("jobs", [
            {"id": "j1", "accountId": "a", "status": "processing", "kind": "image",
             "queueOrder": 0, "settings": {"concurrency": 2}},
            {"id": "j2", "accountId": "b", "status": "processing", "kind": "image",
             "queueOrder": 0, "settings": {"concurrency": 2}},
            {"id": "j3", "accountId": "a", "status": "queued", "kind": "image",
             "queueOrder": 1, "settings": {"concurrency": 2}, "generationToken": "t3"},
        ])
        self.service._account_active = {"a": 1, "b": 1}
        self.service._account_next_start["a"] = 1
        self.service._mark_session_ready("a")
        self.service._mark_session_ready("b")
        waits = {"n": 0}

        def wait(timeout=None):
            waits["n"] += 1
            if waits["n"] == 1:
                # Free one global slot so admission can proceed.
                self.service._account_active["b"] = 0
                self.service._account_condition.notify_all()
            elif waits["n"] > 5:
                raise RuntimeError("admission stuck under global concurrency")

        account = store.get_row("accounts", "a")

        async def done_run(job_id, **kwargs):
            store.patch_row("jobs", job_id, {
                "status": "done", "stage": "done", "progress": 100,
                "error": None, "outputs": ["/tmp/ok.png"], "mediaIds": ["m1"],
            })

        with patch.object(self.service._account_condition, "wait", side_effect=wait), \
             patch.object(self.service, "_verify_account_plan_before_enqueue", return_value=account), \
             patch.object(self.service, "_run", side_effect=done_run), \
             patch("pipeline.flow.service._network_ready_for_flow_job", return_value=True):
            self.service._run_sync("j3")

        self.assertGreaterEqual(waits["n"], 1)
        self.assertEqual(store.get_row("jobs", "j3")["status"], "done")
        # After finish, active for a was incremented then decremented.
        self.assertEqual(self.service._account_active.get("a", 0), 1)

    def test_enqueue_random_job_count_matches_prompts_not_accounts(self) -> None:
        prompts = [f"p{i}" for i in range(8)]
        with patch("pipeline.flow.service.threading.Thread"):
            jobs = self.service.enqueue({
                "prompts": prompts,
                "kind": "image",
                "accountId": "random",
                "settings": {"model": "Nano Banana 2", "concurrency": 8},
            })
        self.assertEqual(len(jobs), 8)
        self.assertTrue(all(job.get("randomAccount") for job in jobs))
        self.assertEqual({job["settings"]["concurrency"] for job in jobs}, {8})


if __name__ == "__main__":
    unittest.main()
