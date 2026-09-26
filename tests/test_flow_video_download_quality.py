"""Download quality mapping for Flow video menu by account plan."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import (
    _pick_video_menu_item,
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

    def test_menu_pick_never_downgrades(self) -> None:
        vi = ["270p Ảnh GIF động", "720p Kích thước gốc", "1080p Đã tăng độ phân giải", "4K Đã tăng độ phân giải"]
        en = ["270p Animated GIF", "720p Original size", "1080p Upscaled"]
        self.assertEqual(_pick_video_menu_item(vi, "720p"), 1)
        self.assertEqual(_pick_video_menu_item(vi, "1080p"), 2)
        self.assertEqual(_pick_video_menu_item(vi, "4K"), 3)
        self.assertEqual(_pick_video_menu_item(vi, "360p"), 1)
        self.assertEqual(_pick_video_menu_item(en, "360p"), 1)
        self.assertIsNone(_pick_video_menu_item(en, "4K"))
        self.assertIsNone(_pick_video_menu_item(["720p Original size"], "1080p"))


if __name__ == "__main__":
    unittest.main()
