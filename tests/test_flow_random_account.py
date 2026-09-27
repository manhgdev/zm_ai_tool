import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from unittest.mock import patch
from pipeline.flow.service import FlowService


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
            mock_patch_row.assert_called_once()
            args = mock_patch_row.call_args[0]
            self.assertEqual(args[0], "jobs")
            self.assertEqual(args[1], "job_123")
            patched_data = args[2]
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


if __name__ == "__main__":
    unittest.main()
