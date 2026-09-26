"""Smoke checks for Flow prompt composer helpers."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


class FlowPromptFillSourceTests(unittest.TestCase):
    def test_fill_prompt_no_longer_requires_y_gt_100(self) -> None:
        path = Path(__file__).resolve().parents[1] / "backend/pipeline/flow/_flow/_flow_ui.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("getBoundingClientRect().y > 100", source)
        self.assertIn("press_sequentially", source)
        self.assertIn("role=\"textbox\"", source.replace("'", '"') if False else source)

    def test_service_skips_frames_for_omni(self) -> None:
        path = Path(__file__).resolve().parents[1] / "backend/pipeline/flow/service.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("Omni Flash ignores start image", source)
        self.assertIn("Do not Escape here", source)


if __name__ == "__main__":
    unittest.main()
