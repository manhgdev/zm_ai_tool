import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from pipeline.flow.service import FlowService


class TestFlowSelectedAccount(unittest.TestCase):
    def test_selected_quota_account_is_rejected_before_enqueue(self):
        import time
        service = FlowService()
        account = {
            "id": "acc_selected",
            "label": "Selected",
            "plan": "Pro",
            "status": "online",
            "projectId": "project",
            "credits": 10,
            "suspendedUntil": time.time() + 7200,
            "suspendReason": "FLOW_QUOTA_EXHAUSTED: Bạn đã đạt đến hạn mức sử dụng",
        }
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.list_rows", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                service.enqueue({
                    "prompts": ["scene"],
                    "kind": "video",
                    "accountId": "acc_selected",
                    "settings": {"model": "Veo 3.1 - Fast"},
                })
        self.assertIn("FLOW_QUOTA_EXHAUSTED", str(ctx.exception))

    def test_concrete_account_disables_fallback_by_default(self):
        service = FlowService()
        account = {
            "id": "acc_selected",
            "label": "Selected",
            "plan": "Pro",
            "status": "online",
            "projectId": "project",
            "credits": 10,
        }
        with patch("pipeline.flow.store.get_row", return_value=account), \
             patch("pipeline.flow.store.list_rows", return_value=[]), \
             patch("pipeline.flow.store.put_rows"), \
             patch("threading.Thread.start"):
            jobs = service.enqueue({
                "prompts": ["scene"],
                "kind": "video",
                "accountId": "acc_selected",
                "settings": {"model": "Veo 3.1 - Fast"},
            })

        self.assertEqual(jobs[0]["accountId"], "acc_selected")
        self.assertFalse(jobs[0]["randomAccount"])
        self.assertFalse(jobs[0]["allowAccountFallback"])

    def test_selected_account_failure_does_not_switch_without_opt_in(self):
        service = FlowService()
        job = {
            "id": "job_selected",
            "status": "failed",
            "kind": "video",
            "accountId": "acc_selected",
            "randomAccount": False,
            "settings": {"model": "Veo 3"},
        }
        with patch("pipeline.flow.store.get_row", return_value=job), \
             patch("pipeline.flow.store.patch_row") as patch_row, \
             patch.object(service, "suspend_account"):
            switched = service._try_fallback_account("job_selected", "acc_selected", "quota")

        self.assertFalse(switched)
        self.assertFalse(any(call.args[0] == "jobs" for call in patch_row.call_args_list))


if __name__ == "__main__":
    unittest.main()
