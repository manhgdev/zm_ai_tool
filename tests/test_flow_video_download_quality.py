"""Download quality mapping for Flow video menu (720p / 1080p)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import _video_download_quality


class FlowVideoDownloadQualityTest(unittest.TestCase):
    def test_defaults_to_720p(self) -> None:
        self.assertEqual(_video_download_quality({}), "720p")
        self.assertEqual(_video_download_quality({"quality": "Standard"}), "720p")

    def test_maps_high_to_1080p(self) -> None:
        self.assertEqual(_video_download_quality({"quality": "High"}), "1080p")
        self.assertEqual(_video_download_quality({"quality": "1080p"}), "1080p")
        self.assertEqual(_video_download_quality({"quality": "1080p Upscaled"}), "1080p")


if __name__ == "__main__":
    unittest.main()
