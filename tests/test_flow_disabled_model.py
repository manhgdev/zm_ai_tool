import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import FlowService, _normalize_catalog_settings, _normalize_video_model, _video_ui_resolution


class DisabledFlowModelTests(unittest.TestCase):
    def test_disabled_model_migrates_to_fast(self):
        self.assertEqual(
            _normalize_video_model("Veo 3.1 - Lite [Lower Priority]"),
            "Veo 3.1 - Fast",
        )

    def test_enqueue_never_persists_disabled_model(self):
        service = FlowService()
        saved = []

        def get_row(table, row_id):
            return {"id": row_id, "plan": "Pro"} if table == "accounts" else None

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[]), \
             patch("pipeline.flow.service.store.put_rows", side_effect=lambda _table, rows: saved.extend(rows)), \
             patch.object(FlowService, "_output_folder", return_value=Path("/tmp/flow-test")), \
             patch.object(FlowService, "_display_output_folder", return_value=Path("/tmp/flow-test")), \
             patch("pipeline.flow.service.threading.Thread.start"):
            jobs = service.enqueue({
                "prompts": ["test"],
                "accountId": "account",
                "kind": "video",
                "settings": {"model": "Veo 3.1 - Lite [Lower Priority]"},
            })

        self.assertEqual(jobs[0]["settings"]["model"], "Veo 3.1 - Fast")
        self.assertEqual(saved[0]["settings"]["model"], "Veo 3.1 - Fast")

    def test_verified_catalog_normalizes_dynamic_resolution(self):
        account = {
            "capabilityStatus": "verified",
            "capabilityCatalog": {
                "video": {
                    "defaultModel": "Omni 1.1 Flash",
                    "models": [{
                        "name": "Omni 1.1 Flash",
                        "ratios": ["16:9", "9:16"],
                        "durations": ["4", "6", "8", "10"],
                        "resolutions": ["360p", "720p"],
                        "defaultRatio": "16:9",
                        "defaultDuration": "8",
                        "defaultResolution": "720p",
                    }],
                },
            },
        }
        settings, changed = _normalize_catalog_settings(account, "video", {
            "model": "Omni 1.1 Flash",
            "ratio": "4:3",
            "duration": "12",
            "resolution": "4K",
        })
        self.assertTrue(changed)
        self.assertEqual(settings["ratio"], "16:9")
        self.assertEqual(settings["duration"], "8")
        self.assertEqual(settings["resolution"], "720p")

    def test_video_strips_image_plan_resolution_without_catalog(self):
        self.assertEqual(_video_ui_resolution("1K"), "")
        self.assertEqual(_video_ui_resolution("720p"), "720p")
        settings, changed = _normalize_catalog_settings({}, "video", {
            "model": "Veo 3.1 - Lite",
            "resolution": "1K",
        })
        self.assertTrue(changed)
        self.assertEqual(settings["resolution"], "")

    def test_retry_falls_back_when_account_id_is_stale(self):
        service = FlowService()
        job = {
            "id": "retry-job",
            "kind": "video",
            "accountId": "account",
            "settings": {"model": "Veo 3.1 - Lite", "resolution": "1K"},
            "status": "failed",
        }
        accounts = [
            {"id": "acc-live", "label": "live", "status": "online", "isDefault": True},
        ]
        patched = {}

        def get_row(table, row_id):
            if table == "jobs" and row_id == "retry-job":
                return dict(job)
            if table == "accounts":
                return next((row for row in accounts if row["id"] == row_id), None)
            return None

        def patch_row(_table, row_id, patch):
            patched.update(patch)
            return {**job, **patch, "id": row_id}

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", side_effect=lambda table: accounts if table == "accounts" else [job]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch("pipeline.flow.service.threading.Thread.start"):
            result = service.retry("retry-job", {"accountId": "account"})

        self.assertEqual(result["accountId"], "acc-live")
        self.assertEqual(patched["accountId"], "acc-live")
        self.assertEqual(patched["settings"]["resolution"], "")

    def test_video_strips_image_tier_scraped_into_catalog(self):
        account = {
            "capabilityStatus": "verified",
            "capabilityCatalog": {
                "video": {
                    "defaultModel": "Veo 3.1 - Lite",
                    "models": [{
                        "name": "Veo 3.1 - Lite",
                        "ratios": ["16:9"],
                        "durations": ["8"],
                        "resolutions": ["1k", "2k"],
                        "defaultRatio": "16:9",
                        "defaultDuration": "8",
                        "defaultResolution": "1k",
                    }],
                },
            },
        }
        settings, changed = _normalize_catalog_settings(account, "video", {
            "model": "Veo 3.1 - Lite",
            "ratio": "16:9",
            "duration": "8",
            "resolution": "1K",
        })
        self.assertTrue(changed)
        self.assertEqual(settings["resolution"], "")


if __name__ == "__main__":
    unittest.main()
