from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline import srt_image


class SrtImageSourceAudioTests(unittest.TestCase):
    def test_video_segments_keep_source_audio_and_stills_get_silence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "clip.mp4"
            still = root / "still.png"
            video.write_bytes(b"video")
            still.write_bytes(b"image")
            commands: list[list[str]] = []

            def capture(_job_id: str, command: list[str]) -> None:
                commands.append(command)

            with patch.object(srt_image, "_run_stage", side_effect=capture), \
                    patch.object(srt_image, "_has_audio_stream", side_effect=lambda path: path.suffix == ".mp4"), \
                    patch.object(srt_image, "_encoder_args", return_value=[]), \
                    patch.object(srt_image.os, "cpu_count", return_value=1):
                srt_image._prepare_video_segments(
                    "job", [video, still], [1.0, 1.0], root / "work",
                    320, 180, 24, 20, False, preserve_audio=True,
                )

        video_command = next(command for command in commands if any("clip.mp4" in arg for arg in command))
        still_command = next(command for command in commands if any("still.png" in arg for arg in command))
        self.assertIn("0:a:0", video_command)
        self.assertNotIn("-an", video_command)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", still_command)
        self.assertIn("1:a:0", still_command)


if __name__ == "__main__":
    unittest.main()
