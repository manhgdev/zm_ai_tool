from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build_app"))

from siv_drop import resolve_siv_drop_path, siv_drop_kind, take_dnd_path


class SivDropPathTests(unittest.TestCase):
    def test_kind_walks_parent_chain(self):
        nested = {
            "attributes": {},
            "parentNode": {
                "attributes": {"data-siv-drop": "audio"},
            },
        }
        self.assertEqual(siv_drop_kind(nested), "audio")
        self.assertEqual(siv_drop_kind({"attributes": {"data-siv-drop": "media"}}), "media")
        self.assertEqual(siv_drop_kind({"attributes": {"id": "x"}}), "")

    def test_media_dir_and_file_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "test12"
            media.mkdir()
            clip = media / "frame.png"
            clip.write_bytes(b"x")
            self.assertEqual(resolve_siv_drop_path("media", str(media)), str(media))
            self.assertEqual(resolve_siv_drop_path("media", str(clip)), str(media))

    def test_audio_and_srt_keep_file_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wav = root / "voice.wav"
            srt = root / "subs.srt"
            wav.write_bytes(b"x")
            srt.write_text("1\n", encoding="utf-8")
            self.assertEqual(resolve_siv_drop_path("audio", str(wav)), str(wav))
            self.assertEqual(resolve_siv_drop_path("srt", str(srt)), str(srt))
            self.assertEqual(resolve_siv_drop_path("audio", str(srt)), "")

    def test_take_dnd_path_matches_folder_leaf_when_cocoa_stores_parent_name(self):
        # Cocoa: (basename(dirname(dir)), full_dir_path) — name is parent folder.
        folder = "/Users/manhg/Downloads/ZM_AI_TOOL/flow/image/test12"
        dnd = [("image", folder)]
        files = [{"name": "test12"}]
        self.assertEqual(take_dnd_path(files, dnd), folder)
        self.assertEqual(dnd, [])

    def test_take_dnd_path_prefers_pywebview_full_path(self):
        dnd = [("x", "/tmp/other")]
        files = [{"name": "voice.wav", "pywebviewFullPath": "/tmp/voice.wav"}]
        self.assertEqual(take_dnd_path(files, dnd), "/tmp/voice.wav")
        self.assertEqual(dnd, [("x", "/tmp/other")])


if __name__ == "__main__":
    unittest.main()
