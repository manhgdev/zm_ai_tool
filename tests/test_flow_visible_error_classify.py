"""Classify Flow UI tile errors into credits vs quota codes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import _classify_visible_flow_error


class FlowVisibleErrorClassifyTest(unittest.TestCase):
    def test_credits(self) -> None:
        msg = _classify_visible_flow_error("Not enough credits to generate")
        self.assertTrue(msg.startswith("FLOW_CREDITS_EMPTY:"))
        self.assertIn("Not enough credits", msg)

    def test_quota(self) -> None:
        msg = _classify_visible_flow_error("Bạn đã hết lượt tạo hôm nay")
        self.assertTrue(msg.startswith("FLOW_QUOTA_EXHAUSTED:"))

    def test_generic(self) -> None:
        msg = _classify_visible_flow_error("Something went wrong")
        self.assertTrue(msg.startswith("FLOW_GENERATION_REJECTED:"))


if __name__ == "__main__":
    unittest.main()
