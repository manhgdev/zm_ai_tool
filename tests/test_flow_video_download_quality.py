"""Download quality mapping for Flow video menu by account plan."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import (
    _video_download_menu_labels,
    _video_download_qualities_for_plan,
    _video_download_quality,
)


class FlowVideoDownloadQualityTest(unittest.TestCase):
    def test_plan_options(self) -> None:
        self.assertEqual(_video_download_qualities_for_plan("Free"), ["360p", "720p"])
        self.assertEqual(_video_download_qualities_for_plan("Pro"), ["360p", "720p", "1080p"])
        self.assertEqual(_video_download_qualities_for_plan("Plus"), ["360p", "720p", "1080p"])
        self.assertEqual(_video_download_qualities_for_plan("Ultra"), ["360p", "720p", "1080p", "4K"])

    def test_defaults_to_720p(self) -> None:
        self.assertEqual(_video_download_quality({}), "720p")
        self.assertEqual(_video_download_quality({"quality": "Standard"}), "720p")

    def test_maps_360_and_high(self) -> None:
        self.assertEqual(_video_download_quality({"quality": "360p"}), "360p")
        self.assertEqual(_video_download_quality({"quality": "low"}), "360p")
        self.assertEqual(_video_download_quality({"quality": "High"}), "1080p")
        self.assertEqual(_video_download_quality({"quality": "1080p"}), "1080p")
        self.assertEqual(_video_download_quality({"quality": "4K"}, "Ultra"), "4K")

    def test_clamps_4k_for_pro(self) -> None:
        self.assertEqual(_video_download_quality({"quality": "4K"}, "Pro"), "1080p")
        self.assertEqual(_video_download_quality({"quality": "1080p"}, "Free"), "720p")

    def test_menu_labels_never_downgrade(self) -> None:
        self.assertEqual(_video_download_menu_labels("360p"), ["360p"])
        self.assertEqual(_video_download_menu_labels("720p"), ["720p"])
        self.assertEqual(_video_download_menu_labels("1080p"), ["1080p Upscaled", "1080p"])
        self.assertEqual(_video_download_menu_labels("4K"), ["4K Upscaled", "4K"])
        for labels in (
            _video_download_menu_labels("1080p"),
            _video_download_menu_labels("4K"),
        ):
            self.assertNotIn("720p", labels)
            self.assertNotIn("360p", labels)


if __name__ == "__main__":
    unittest.main()
