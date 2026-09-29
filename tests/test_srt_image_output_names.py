"""SRT image/video exports avoid overwriting existing output files."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline import srt_image


class SrtImageOutputNameTests(unittest.TestCase):
    def test_keeps_free_name_and_adds_sequential_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "output.mp4"
            with patch.dict(srt_image._JOBS, {}, clear=True):
                first = srt_image.create_job("output.mp4", Path(raw), [], None, None, None, {}, output_target=output)
                output.touch()
                second = srt_image.create_job("output.mp4", Path(raw), [], None, None, None, {}, output_target=output)
                Path(second["output"]).touch()
                third = srt_image.create_job("output.mp4", Path(raw), [], None, None, None, {}, output_target=output)

        self.assertEqual(Path(first["output"]), output)
        self.assertEqual(Path(second["output"]), Path(raw) / "output1.mp4")
        self.assertEqual(Path(third["output"]), Path(raw) / "output2.mp4")


if __name__ == "__main__":
    unittest.main()
