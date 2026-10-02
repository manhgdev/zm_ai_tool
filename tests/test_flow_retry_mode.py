"""Flow retry preserves the generation mode recorded by the source job."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import FlowService


class FlowRetryModeTests(unittest.TestCase):
    def test_series_frame_mode_is_preserved_and_can_be_sent_explicitly(self) -> None:
        existing = {
            "id": "job1",
            "kind": "video",
            "mode": "frame",
            "accountId": "account1",
            "queueOrder": 0,
            "settings": {"model": "Omni 1.1 Flash", "ratio": "16:9", "resolution": "360p"},
            "sourceFiles": ["frame.png"],
            "seriesContext": {"seriesId": "series1", "artifact": "video"},
        }
        account = {"id": "account1", "label": "Flow", "status": "online", "plan": "Pro", "projectId": "project1"}
        patched: dict = {}

        def get_row(table: str, row_id: str):
            if table == "jobs" and row_id == "job1":
                return dict(existing)
            if table == "accounts" and row_id == "account1":
                return dict(account)
            return None

        def patch_row(_table: str, _row_id: str, values: dict):
            patched.update(values)
            return {**existing, **values}

        service = FlowService()
        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), patch(
            "pipeline.flow.service.store.list_rows", return_value=[existing]
        ), patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), patch(
            "pipeline.flow.service.threading.Thread.start"
        ):
            result = service.retry("job1", {"accountId": "account1", "mode": "frame"})

        self.assertEqual(patched["mode"], "frame")
        self.assertEqual(result["mode"], "frame")


if __name__ == "__main__":
    unittest.main()
