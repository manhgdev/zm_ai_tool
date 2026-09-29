"""Classify Flow UI tile errors into credits vs quota codes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import (
    _classify_visible_flow_error,
    _FLOW_SUBMIT_DELAY_MAX_S,
    _FLOW_SUBMIT_DELAY_MIN_S,
)


class FlowVisibleErrorClassifyTest(unittest.TestCase):
    def test_credits(self) -> None:
        msg = _classify_visible_flow_error("Not enough credits to generate")
        self.assertTrue(msg.startswith("FLOW_CREDITS_EMPTY:"))
        self.assertIn("Not enough credits", msg)

    def test_quota(self) -> None:
        msg = _classify_visible_flow_error("Bạn đã hết lượt tạo hôm nay")
        self.assertTrue(msg.startswith("FLOW_QUOTA_EXHAUSTED:"))

    def test_usage_limit_quota_copy(self) -> None:
        msg = _classify_visible_flow_error("Bạn đã đạt đến hạn mức sử dụng")
        self.assertTrue(msg.startswith("FLOW_QUOTA_EXHAUSTED:"))

    def test_generic(self) -> None:
        msg = _classify_visible_flow_error("Something went wrong")
        self.assertTrue(msg.startswith("FLOW_GENERATION_REJECTED:"))

    def test_abnormal_activity_stops_auto_retry(self) -> None:
        msg = _classify_visible_flow_error(
            "Chúng tôi nhận thấy có hoạt động bất thường nào đó. Vui lòng chờ vài giây rồi thử lại."
        )
        self.assertTrue(msg.startswith("FLOW_AUTOMATION_BLOCKED:"))

    def test_submit_delay_bounds(self) -> None:
        self.assertEqual(_FLOW_SUBMIT_DELAY_MIN_S, 0.1)
        self.assertEqual(_FLOW_SUBMIT_DELAY_MAX_S, 1.0)


if __name__ == "__main__":
    unittest.main()
