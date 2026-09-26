"""AI Series draft: the BIBLE block must be split off before TXT import."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.series import import_script, scene_seconds
from pipeline.flow.series_ai import _clean_text, split_bible


class SeriesAiDraftTest(unittest.TestCase):
    def test_bible_split_leaves_importable_script(self):
        raw = """```txt
# SERIES: Tom đi cắm trại
# BIBLE
Character: Tom — mèo xám, khăn đỏ
Style: hoạt hình 2D
# TẬP 01 — Lên đường
001_[00.00_00.00-00.00_08.00] Tom xếp balo
002_[00.00_00.08-00.00_16.00] Tom ra cửa
```"""
        text, bible = split_bible(_clean_text(raw))
        self.assertEqual(bible, "Character: Tom — mèo xám, khăn đỏ\nStyle: hoạt hình 2D")
        parsed = import_script(text)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["title"], "Tom đi cắm trại")
        self.assertEqual(len(parsed["episodes"][0]["scenes"]), 2)

    def test_scene_seconds_snaps_timecode_to_flow_duration(self):
        self.assertEqual(scene_seconds("00.00_08.00-00.00_14.00"), 6)
        self.assertEqual(scene_seconds("00.00_50.00-00.01_00.00"), 10)
        self.assertEqual(scene_seconds("00.00_00.00-00.00_05.00"), 4)
        self.assertIsNone(scene_seconds(""))


if __name__ == "__main__":
    unittest.main()
