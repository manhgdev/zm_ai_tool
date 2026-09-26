"""Regression: Veo Lite can finish without a nested video object yet."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import _video_fife_url, _video_media_ready


class FlowVideoReadyTest(unittest.TestCase):
    def test_successful_status_without_video_key_is_ready(self) -> None:
        media = {
            "name": "abc",
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"},
            },
        }
        self.assertTrue(_video_media_ready(media))
        self.assertEqual(_video_fife_url(media), "")

    def test_fife_url_makes_ready_even_if_status_pending(self) -> None:
        media = {
            "name": "abc",
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"},
            },
            "video": {"generatedVideo": {"fifeUrl": "https://lh3.googleusercontent.com/x.mp4"}},
        }
        self.assertTrue(_video_media_ready(media))
        self.assertEqual(_video_fife_url(media), "https://lh3.googleusercontent.com/x.mp4")

    def test_failed_status_not_ready(self) -> None:
        media = {
            "name": "abc",
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_FAILED"},
            },
            "video": {},
        }
        self.assertFalse(_video_media_ready(media))


if __name__ == "__main__":
    unittest.main()
