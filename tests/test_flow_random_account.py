import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from unittest.mock import patch
from pipeline.flow.service import FlowService, _COOLDOWN_BOT_S, _COOLDOWN_QUOTA_S


class TestFlowRandomAccountAndFallback(unittest.TestCase):
    def setUp(self):
        self.service = FlowService()
        self.mock_accounts = [
            {
                "id": "acc_free",
                "label": "free_user",
                "plan": "Free",
                "status": "online",
                "projectId": "proj_free",
                "credits": 100,
            },
            {
                "id": "acc_pro",
                "label": "pro_user",
                "plan": "Pro",
                "status": "online",
                "projectId": "proj_pro",
                "credits": 200,
            },
            {
                "id": "acc_ultra",
                "label": "ultra_user",
                "plan": "Ultra",
                "status": "online",
                "projectId": "proj_ultra",
                "credits": 500,
            },
            {
                "id": "acc_zero_credit",
                "label": "zero_user",
                "plan": "Pro",
                "status": "online",
                "projectId": "proj_zero",
                "credits": 0,
            },
            {
                "id": "acc_offline",
                "label": "offline_user",
                "plan": "Ultra",
                "status": "offline",
                "projectId": "proj_off",
                "credits": 300,
            },
        ]

    def test_pick_eligible_account_filters_free_for_banana_pro(self):
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts):
            for _ in range(20):
                picked = self.service._pick_eligible_account(kind="image", model="Nano Banana Pro")
                self.assertIsNotNone(picked)
                self.assertIn(picked["id"], {"acc_pro", "acc_ultra"})
                self.assertNotEqual(picked["plan"], "Free")

    def test_pick_eligible_account_allows_free_for_other_models(self):
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts):
            picked_ids = set()
            for _ in range(50):
                picked = self.service._pick_eligible_account(kind="image", model="Nano Banana 2")
                self.assertIsNotNone(picked)
                picked_ids.add(picked["id"])
            self.assertIn("acc_free", picked_ids)

    def test_pick_eligible_account_respects_exclude_ids(self):
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts):
            picked = self.service._pick_eligible_account(
                kind="image",
                model="Nano Banana Pro",
                exclude_ids=["acc_pro"],
            )
            self.assertIsNotNone(picked)
            self.assertEqual(picked["id"], "acc_ultra")

            # Exclude all eligible Pro/Ultra
            picked_none = self.service._pick_eligible_account(
                kind="image",
                model="Nano Banana Pro",
                exclude_ids=["acc_pro", "acc_ultra"],
            )
            self.assertIsNone(picked_none)

    def test_enqueue_random_account_selection(self):
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts), \
             patch("pipeline.flow.store.get_row") as mock_get_row, \
             patch("pipeline.flow.store.put_rows") as mock_put_rows, \
             patch("threading.Thread.start"):
            def fake_get_row(table, row_id):
                return next((a for a in self.mock_accounts if a["id"] == row_id), None)
            mock_get_row.side_effect = fake_get_row

            jobs = self.service.enqueue({
                "prompts": ["a beautiful cat in a garden"],
                "kind": "image",
                "accountId": "random",
                "settings": {"model": "Nano Banana Pro"},
            })
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertIn(job["accountId"], {"acc_pro", "acc_ultra"})
            self.assertTrue(job["allowAccountFallback"])
            self.assertTrue(job["randomAccount"])
            self.assertEqual(job["triedAccountIds"], [job["accountId"]])

    def test_enqueue_free_account_with_banana_pro_auto_fallbacks_to_pro(self):
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts), \
             patch("pipeline.flow.store.get_row") as mock_get_row, \
             patch("pipeline.flow.store.put_rows"), \
             patch("threading.Thread.start"):
            def fake_get_row(table, row_id):
                return next((a for a in self.mock_accounts if a["id"] == row_id), None)
            mock_get_row.side_effect = fake_get_row

            jobs = self.service.enqueue({
                "prompts": ["a majestic mountain"],
                "kind": "image",
                "accountId": "acc_free",
                "settings": {"model": "Nano Banana Pro"},
            })
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            # Should have fallen back to acc_pro or acc_ultra instead of staying acc_free or downscaling
            self.assertIn(job["accountId"], {"acc_pro", "acc_ultra"})
            self.assertEqual(job["settings"]["model"], "Nano Banana Pro")

    def test_enqueue_free_account_with_banana_pro_errors_if_no_pro_account(self):
        only_free = [self.mock_accounts[0]]
        with patch("pipeline.flow.store.list_rows", return_value=only_free), \
             patch("pipeline.flow.store.get_row", return_value=only_free[0]):
            with self.assertRaises(ValueError) as ctx:
                self.service.enqueue({
                    "prompts": ["a majestic mountain"],
                    "kind": "image",
                    "accountId": "acc_free",
                    "settings": {"model": "Nano Banana Pro"},
                })
            self.assertIn("FLOW_PLAN_INSUFFICIENT", str(ctx.exception))


    def test_try_fallback_account_switches_to_next_eligible(self):
        fake_job = {
            "id": "job_123",
            "status": "failed",
            "kind": "image",
            "accountId": "acc_pro",
            "settings": {"model": "Nano Banana Pro"},
            "triedAccountIds": ["acc_pro"],
            "allowAccountFallback": True,
        }
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts), \
             patch("pipeline.flow.store.get_row", return_value=fake_job), \
             patch("pipeline.flow.store.patch_row") as mock_patch_row, \
             patch("threading.Thread.start"):
            switched = self.service._try_fallback_account("job_123", "acc_pro", "Quota exceeded")
            self.assertTrue(switched)
            self.assertEqual(mock_patch_row.call_count, 2)
            account_patch = next(c[0] for c in mock_patch_row.call_args_list if c[0][0] == "accounts")
            self.assertEqual(account_patch[1], "acc_pro")
            self.assertIn("suspendedUntil", account_patch[2])

            job_patch = next(c[0] for c in mock_patch_row.call_args_list if c[0][0] == "jobs")
            self.assertEqual(job_patch[1], "job_123")
            patched_data = job_patch[2]
            # Since acc_pro was tried and Banana Pro requires non-free, next eligible MUST be acc_ultra
            self.assertEqual(patched_data["accountId"], "acc_ultra")
            self.assertEqual(patched_data["status"], "queued")
            self.assertIn("acc_ultra", patched_data["triedAccountIds"])
            self.assertIn("acc_pro", patched_data["triedAccountIds"])

    def test_enqueue_multiple_prompts_distributes_evenly_across_accounts(self):
        prompts = [f"Prompt {i}" for i in range(6)]
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts), \
             patch("pipeline.flow.store.get_row") as mock_get_row, \
             patch("pipeline.flow.store.put_rows"), \
             patch("threading.Thread.start"):
            def fake_get_row(table, row_id):
                return next((a for a in self.mock_accounts if a["id"] == row_id), None)
            mock_get_row.side_effect = fake_get_row

            jobs = self.service.enqueue({
                "prompts": prompts,
                "kind": "image",
                "accountId": "random",
                "settings": {"model": "Nano Banana Pro"},
            })
            self.assertEqual(len(jobs), 6)
            account_counts = {}
            for j in jobs:
                acc = j["accountId"]
                account_counts[acc] = account_counts.get(acc, 0) + 1
            # Với Banana Pro, chỉ có acc_pro và acc_ultra hợp lệ. 6 jobs phải chia đều đúng 3 - 3!
            self.assertEqual(account_counts.get("acc_pro"), 3)
            self.assertEqual(account_counts.get("acc_ultra"), 3)

    def test_pick_eligible_account_skips_suspended_account(self):
        import time
        accounts_with_suspension = [
            dict(self.mock_accounts[1], suspendedUntil=time.time() + 7200, suspendReason="Quota reached"), # acc_pro suspended
            self.mock_accounts[2], # acc_ultra online
        ]
        with patch("pipeline.flow.store.list_rows", return_value=accounts_with_suspension):
            for _ in range(10):
                picked = self.service._pick_eligible_account(kind="image", model="Nano Banana Pro")
                self.assertIsNotNone(picked)
                self.assertEqual(picked["id"], "acc_ultra")

    def test_quota_suspension_is_detected_for_queued_jobs(self):
        import time
        account = dict(self.mock_accounts[1], suspendedUntil=time.time() + 7200,
                       suspendReason="FLOW_QUOTA_EXHAUSTED: Bạn đã đạt đến hạn mức sử dụng")
        with patch("pipeline.flow.store.get_row", return_value=account):
            self.assertTrue(self.service._account_quota_suspended("acc_pro"))

    def test_pick_eligible_account_reincludes_after_suspension_expires(self):
        import time
        accounts_expired_suspension = [
            dict(self.mock_accounts[1], suspendedUntil=time.time() - 100, suspendReason="Old issue"), # expired
            self.mock_accounts[2],
        ]
        with patch("pipeline.flow.store.list_rows", return_value=accounts_expired_suspension):
            picked_ids = set()
            for _ in range(20):
                picked = self.service._pick_eligible_account(kind="image", model="Nano Banana Pro")
                self.assertIsNotNone(picked)
                picked_ids.add(picked["id"])
            self.assertIn("acc_pro", picked_ids)
            self.assertIn("acc_ultra", picked_ids)

    def test_suspend_account_severe_duration(self):
        import time
        acc = dict(self.mock_accounts[1])
        with patch("pipeline.flow.store.get_row", return_value=acc), \
             patch("pipeline.flow.store.patch_row") as mock_patch:
            now = time.time()
            self.service.suspend_account("acc_pro", "FLOW_AUTOMATION_BLOCKED: abnormal activity")
            mock_patch.assert_called_once()
            patched = mock_patch.call_args[0][2]
            self.assertAlmostEqual(patched["suspendedUntil"], now + _COOLDOWN_BOT_S, delta=5)
            self.assertIn("AUTOMATION_BLOCKED", patched["suspendReason"])

    def test_suspend_account_standard_duration(self):
        import time
        acc = dict(self.mock_accounts[1])
        with patch("pipeline.flow.store.get_row", return_value=acc), \
             patch("pipeline.flow.store.patch_row") as mock_patch:
            now = time.time()
            self.service.suspend_account("acc_pro", "FLOW_QUOTA_EXHAUSTED: daily limit reached")
            mock_patch.assert_called_once()
            patched = mock_patch.call_args[0][2]
            self.assertAlmostEqual(patched["suspendedUntil"], now + _COOLDOWN_QUOTA_S, delta=5)
            self.assertIn("QUOTA_EXHAUSTED", patched["suspendReason"])

    def test_suspend_account_raw_usage_limit(self):
        import time
        acc = dict(self.mock_accounts[1])
        with patch("pipeline.flow.store.get_row", return_value=acc), \
             patch("pipeline.flow.store.patch_row") as mock_patch:
            now = time.time()
            self.service.suspend_account("acc_pro", "FLOW: bạn đã đạt đến hạn mức sử dụng")
            mock_patch.assert_called_once()
            self.assertAlmostEqual(
                mock_patch.call_args[0][2]["suspendedUntil"],
                now + _COOLDOWN_QUOTA_S, delta=5,
            )

    def test_clear_account_suspension(self):
        acc = dict(self.mock_accounts[1], suspendedUntil=9999999999, suspendReason="Some error")
        with patch("pipeline.flow.store.get_row", return_value=acc), \
             patch("pipeline.flow.store.patch_row") as mock_patch:
            self.service.clear_account_suspension("acc_pro")
            mock_patch.assert_called_once()
            patched = mock_patch.call_args[0][2]
            self.assertIsNone(patched["suspendedUntil"])
            self.assertIsNone(patched["suspendReason"])

    def test_try_fallback_account_suspends_failed_account(self):
        fake_job = {
            "id": "job_123",
            "status": "failed",
            "kind": "image",
            "accountId": "acc_pro",
            "settings": {"model": "Nano Banana Pro"},
            "triedAccountIds": ["acc_pro"],
            "allowAccountFallback": True,
        }
        with patch("pipeline.flow.store.list_rows", return_value=self.mock_accounts), \
             patch("pipeline.flow.store.get_row", return_value=fake_job), \
             patch("pipeline.flow.store.patch_row") as mock_patch_row, \
             patch.object(self.service, "suspend_account") as mock_suspend, \
             patch("threading.Thread.start"):
            switched = self.service._try_fallback_account("job_123", "acc_pro", "FLOW_AUTOMATION_BLOCKED: captcha")
            self.assertTrue(switched)
            mock_suspend.assert_called_once_with("acc_pro", "FLOW_AUTOMATION_BLOCKED: captcha")


if __name__ == "__main__":
    unittest.main()
