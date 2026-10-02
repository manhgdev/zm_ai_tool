import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.flow import store
from pipeline.flow.service import FlowService, _normalize_catalog_settings


class QuotaModelRetryTest(unittest.TestCase):
    def test_retry_and_worker_keep_fallback_with_stale_catalog(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            account = {
                "id": "a", "status": "online", "projectId": "p", "plan": "Pro",
                "preferredImageModel": "Nano Banana 2", "capabilityStatus": "verified",
                "capabilityCatalog": {"image": {
                    "defaultModel": "Nano Banana Pro",
                    "models": [{"name": "Nano Banana Pro"}],
                }},
            }
            store._write("accounts", [account])
            store._write("jobs", [{
                "id": "j", "accountId": "a", "kind": "image", "status": "processing",
                "stage": "model_fallback", "queueOrder": 0,
                "settings": {"model": "Nano Banana 2"},
                "submissionStartedAt": 10, "mediaIds": ["old"],
            }])
            service = FlowService()
            with patch("threading.Thread.start"):
                job = service.retry("j")
            self.assertEqual(job["settings"]["model"], "Nano Banana 2")
            self.assertIsNone(job["submissionStartedAt"])
            self.assertEqual(job["mediaIds"], [])
            worker_settings, _ = _normalize_catalog_settings(account, "image", job["settings"])
            self.assertEqual(worker_settings["model"], "Nano Banana 2")
            queued_settings, changed = _normalize_catalog_settings(account, "image", {"model": "Nano Banana Pro"})
            self.assertEqual(queued_settings["model"], "Nano Banana Pro")
            self.assertTrue(changed)

    def test_worker_releases_slot_before_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            account = {"id": "a", "status": "online", "projectId": "p", "plan": "Pro"}
            store._write("accounts", [account])
            store._write("jobs", [{
                "id": "j", "accountId": "a", "kind": "image", "status": "queued",
                "queueOrder": 0, "settings": {"model": "Nano Banana Pro"},
            }])
            service = FlowService()

            async def failed_run(job_id, **kwargs):
                store.patch_row("jobs", job_id, {"stage": "model_fallback", "status": "processing"})

            def retry(job_id, overrides):
                self.assertNotIn(job_id, service._running_jobs)
                self.assertEqual(service._account_active["a"], 0)

            with patch.object(service, "_verify_account_plan_before_enqueue", return_value=account), \
                 patch.object(service, "_clone_runtime_profile", return_value=None), \
                 patch.object(service, "_run", side_effect=failed_run), \
                 patch.object(service, "retry", side_effect=retry) as retried:
                service._run_sync("j")
            retried.assert_called_once_with("j", {"accountId": "a"})

    def test_other_models_unchanged(self):
        account = {"preferredImageModel": "Nano Banana 2", "plan": "Pro"}
        for kind, model in [("video", "Veo 3.1 - Fast"), ("image", "Nano Banana 2 Lite")]:
            settings, _ = _normalize_catalog_settings(account, kind, {"model": model})
            self.assertEqual(settings["model"], model)

    def test_random_fallback_only_changes_same_account_waiting_images(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a"}, {"id": "b"}])
            rows = []
            for identifier, account, status, kind in [
                ("failed", "a", "processing", "image"),
                ("waiting", "a", "queued", "image"),
                ("running", "a", "processing", "image"),
                ("other", "b", "queued", "image"),
                ("video", "a", "queued", "video"),
            ]:
                rows.append({"id": identifier, "accountId": account, "status": status,
                             "kind": kind, "randomAccount": True, "settings": {"model": "Nano Banana Pro"}})
            store._write("jobs", rows)
            service = FlowService()
            service._apply_image_quota_fallback("failed", "FLOW_QUOTA_EXHAUSTED: bạn đã đạt đến hạn mức sử dụng")
            for identifier in ("failed", "waiting"):
                row = store.get_row("jobs", identifier)
                self.assertEqual(row["settings"]["model"], "Nano Banana 2")
                self.assertEqual(row["accountId"], "a")
            for identifier in ("running", "other", "video"):
                self.assertEqual(store.get_row("jobs", identifier)["settings"]["model"], "Nano Banana Pro")
            event = store.get_row("jobs", "failed")["modelFallback"]
            self.assertEqual(event, store.get_row("accounts", "a")["lastModelFallback"])
            self.assertTrue(event["randomAccount"])
