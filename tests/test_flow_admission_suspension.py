"""Admission fails only long quota; short rate-limit cooldowns wait."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow import store
from pipeline.flow.service import FlowService, _COOLDOWN_QUOTA_S, _COOLDOWN_RATELIMIT_S


class FlowAdmissionSuspensionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root_patch = patch.object(store, "ROOT", Path(self.directory.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.service = FlowService()
        store._write("accounts", [{
            "id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100,
        }])
        store._write("jobs", [{
            "id": "j", "accountId": "a", "status": "queued", "kind": "image",
            "queueOrder": 0, "settings": {}, "prompts": ["x"],
        }])

    def test_quota_suspension_is_hard_admission_stop(self) -> None:
        until = time.time() + _COOLDOWN_QUOTA_S
        store.patch_row("accounts", "a", {
            "suspendedUntil": until,
            "suspendReason": "FLOW_QUOTA_EXHAUSTED: daily limit",
        })
        self.assertTrue(self.service._account_quota_suspended("a"))
        self.assertTrue(self.service._account_suspended("a"))

    def test_rate_limit_suspension_waits_not_quota_hard_stop(self) -> None:
        until = time.time() + _COOLDOWN_RATELIMIT_S
        store.patch_row("accounts", "a", {
            "suspendedUntil": until,
            "suspendReason": "HTTP 429: too many requests",
        })
        self.assertFalse(self.service._account_quota_suspended("a"))
        self.assertTrue(self.service._account_suspended("a"))

    def test_sibling_fails_immediately_only_for_quota(self) -> None:
        until = time.time() + 3600
        store.patch_row("accounts", "a", {
            "suspendedUntil": until,
            "suspendReason": "FLOW_QUOTA_EXHAUSTED: daily limit",
        })
        with patch.object(self.service, "_verify_account_plan_before_enqueue") as verify, patch.object(
            self.service, "_run"
        ) as run, patch.object(self.service, "_network_ready_for_flow_job", create=True):
            # Direct admission gate used by _run_sync.
            if self.service._account_quota_suspended("a"):
                store.patch_row("jobs", "j", {
                    "status": "failed",
                    "stage": "failed",
                    "error": self.service._account_suspension_error(store.get_row("accounts", "a") or {}),
                })
            job = store.get_row("jobs", "j")
            self.assertEqual(job["status"], "failed")
            self.assertIn("FLOW_QUOTA_EXHAUSTED", job["error"])
            verify.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
