"""Series continuity: end-frame extract and same-episode previous scene."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.series import (
    _previous_scene,
    extract_video_end_frame,
    generation_context,
)


class SeriesContinuityTests(unittest.TestCase):
    def test_extract_keeps_landscape_aspect(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        video = tmp / "clip.mp4"
        dest = tmp / "end.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=1280x720:d=0.25",
                "-pix_fmt", "yuv420p", str(video),
            ],
            check=True,
        )
        self.assertTrue(extract_video_end_frame(video, dest))
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(dest),
        ]))
        width = int(probe["streams"][0]["width"])
        height = int(probe["streams"][0]["height"])
        self.assertGreaterEqual(width, height)
        self.assertNotEqual((width, height), (768, 1376))

    def test_previous_scene_stays_inside_episode(self) -> None:
        series = {
            "episodes": [
                {
                    "id": "ep1",
                    "scenes": [{"id": "s1"}, {"id": "s2"}],
                },
                {
                    "id": "ep2",
                    "scenes": [{"id": "s3"}],
                },
            ]
        }
        self.assertIsNone(_previous_scene(series, "ep2", "s3"))
        prev = _previous_scene(series, "ep1", "s2")
        self.assertEqual(prev["id"], "s1")

    def test_continuation_uses_previous_end_frame_as_start(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        end = tmp / "prev_end.png"
        end.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 80)
        series = {
            "id": "series_x",
            "title": "Tom Jerry",
            "slug": "tom-jerry",
            "bible": "Tom the cat and Jerry the mouse, classic cartoon.",
            "anchorAssets": [],
            "assets": [],
            "episodes": [{
                "id": "ep1",
                "index": 1,
                "state": "",
                "scenes": [
                    {
                        "id": "s1", "index": 1, "prompt": "chase starts",
                        "continuityEnabled": True, "endFrame": str(end),
                        "videoOutput": "", "approvedKeyframe": "",
                        "referenceAssetIds": [],
                    },
                    {
                        "id": "s2", "index": 2, "prompt": "Jerry slips under the table",
                        "continuityEnabled": True, "endFrame": "",
                        "videoOutput": "", "approvedKeyframe": "",
                        "referenceAssetIds": [], "promptOverride": "",
                    },
                ],
            }],
        }
        with patch("pipeline.flow.series.get_series", return_value=series):
            ctx = generation_context("series_x", "ep1", "s2", "video")
        self.assertEqual(ctx["sourceFiles"], [str(end)])
        self.assertTrue(ctx.get("continuity"))
        self.assertIn("Continue the exact preceding video", ctx["prompt"])
        self.assertNotIn("Tom the cat and Jerry the mouse, classic cartoon.", ctx["prompt"])


if __name__ == "__main__":
    unittest.main()
