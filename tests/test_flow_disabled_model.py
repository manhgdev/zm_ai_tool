import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import FlowService, _normalize_catalog_settings, _normalize_video_model


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
             patch("pipeline.flow.service.store.put_row", side_effect=lambda _table, row: saved.append(row)), \
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


if __name__ == "__main__":
    unittest.main()
