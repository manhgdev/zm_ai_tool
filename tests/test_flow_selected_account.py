import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from pipeline.flow.service import FlowService


class TestFlowSelectedAccount(unittest.TestCase):
    def test_disconnected_accounts_are_rejected_for_create_retry_and_random(self):
        for status, project in [("reconnect", "project"), ("connecting", "project"), ("online", "")]:
            with self.subTest(status=status, project=project):
                account = {"id": "a", "label": "A", "status": status, "projectId": project, "plan": "Pro", "credits": 100}
                job = {"id": "j", "accountId": "a", "kind": "image", "settings": {}}
                def get_row(table, row_id):
                    return job if table == "jobs" else account
                with patch("pipeline.flow.store.get_row", side_effect=get_row), patch(
                    "pipeline.flow.store.list_rows", side_effect=lambda table: [account] if table == "accounts" else []
                ), patch("pipeline.flow.store.put_rows") as put, patch("threading.Thread.start") as start:
                    service = FlowService()
                    with self.assertRaisesRegex(ValueError, "FLOW_LOGIN_REQUIRED"):
                        service.enqueue({"prompts": ["scene"], "kind": "image", "accountId": "a"})
                    with self.assertRaisesRegex(ValueError, "FLOW_LOGIN_REQUIRED"):
                        service.retry("j")
                    self.assertIsNone(service._pick_eligible_account())
                    with self.assertRaisesRegex(ValueError, "FLOW_NO_ONLINE_ACCOUNTS"):
                        service.retry("j", {"accountId": "random"})
                    put.assert_not_called()
                    start.assert_not_called()

    def test_random_retry_does_not_bypass_eligibility_when_picker_is_empty(self):
        job = {"id": "j", "accountId": "a", "kind": "image", "settings": {}}
        with patch("pipeline.flow.store.get_row", return_value=job), patch.object(
            FlowService, "_pick_eligible_account", return_value=None
        ), patch("pipeline.flow.store.list_rows", return_value=[{"id": "a", "status": "online", "credits": 0}]), patch(
            "threading.Thread.start"
        ) as start:
            with self.assertRaisesRegex(ValueError, "FLOW_NO_ONLINE_ACCOUNTS"):
                FlowService().retry("j", {"accountId": "random"})
            start.assert_not_called()

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
