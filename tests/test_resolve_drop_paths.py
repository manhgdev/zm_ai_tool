from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from api.routes.system import match_drop_paths  # noqa: E402


class MatchDropPathsTests(unittest.TestCase):
    def test_matches_file_by_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio = root / "audio.mp3"
            other = root / "other.wav"
            audio.write_bytes(b"x")
            other.write_bytes(b"x")
            path = match_drop_paths([str(other), str(audio)], ["audio.mp3"], prefer_dir=False)
            self.assertEqual(path, str(audio))

    def test_prefer_dir_uses_folder_or_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "test12"
            media.mkdir()
            clip = media / "frame.png"
            clip.write_bytes(b"x")
            self.assertEqual(
                match_drop_paths([str(media)], ["test12"], prefer_dir=True),
                str(media),
            )
            self.assertEqual(
                match_drop_paths([str(clip)], ["frame.png"], prefer_dir=True),
                str(media),
            )


class ResolveDropApiTests(unittest.TestCase):
    def test_api_uses_finder_selection(self):
        from api.routes.system import ResolveDropPathsIn, api_resolve_drop_paths

        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "audio.mp3"
            audio.write_bytes(b"x")
            with patch("api.routes.system._macos_active_drop_paths", return_value=[str(audio)]), patch(
                "api.routes.system.sys.platform", "darwin"
            ):
                result = api_resolve_drop_paths(ResolveDropPathsIn(names=["audio.mp3"], preferDir=False))
            self.assertTrue(result["ok"])
            self.assertEqual(result["path"], str(audio))

    def test_api_falls_back_to_dragover_hints(self):
        from api.routes.system import ResolveDropPathsIn, api_resolve_drop_paths

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "test12"
            folder.mkdir()
            with patch("api.routes.system._macos_active_drop_paths", return_value=[]), patch(
                "api.routes.system.sys.platform", "darwin"
            ):
                result = api_resolve_drop_paths(
                    ResolveDropPathsIn(names=[], preferDir=True, hints=[str(folder)])
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["path"], str(folder))


if __name__ == "__main__":
    unittest.main()
