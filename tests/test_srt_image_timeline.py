"""Timeline tokens for SRT image/video merge."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline.srt_image import _timeline_seconds, parse_timing_cues_detailed


class TimelineSecondsTest(unittest.TestCase):
    def test_mm_ss_colon_and_dot(self):
        self.assertEqual(_timeline_seconds("00:00"), 0.0)
        self.assertEqual(_timeline_seconds("00:05"), 5.0)
        self.assertEqual(_timeline_seconds("00.00"), 0.0)
        self.assertEqual(_timeline_seconds("00.05"), 5.0)
        self.assertEqual(_timeline_seconds("01.30"), 90.0)

    def test_plain_seconds_still_work(self):
        self.assertEqual(_timeline_seconds("5"), 5.0)
        self.assertEqual(_timeline_seconds("1.5"), 1.5)

    def test_prompt_lines_mm_ss_formats(self):
        text = "001_[00.00-00.05] Scene A\n002_[00:00-00:05] Scene B\n"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "timeline.txt"
            path.write_text(text, encoding="utf-8")
            cues = parse_timing_cues_detailed(path)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["start"], 0.0)
        self.assertEqual(cues[0]["end"], 5.0)
        self.assertEqual(cues[1]["start"], 0.0)
        self.assertEqual(cues[1]["end"], 5.0)


if __name__ == "__main__":
    unittest.main()
