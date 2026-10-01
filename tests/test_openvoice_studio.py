"""Studio jobs use the direct VieNeu/cloud routes and omit OpenVoice metadata."""
import json
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pipeline.tts import studio


def _write_audio(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x10\x01" * 24000)


def test_text_job_uses_direct_tts_and_no_openvoice_metadata():
    with tempfile.TemporaryDirectory(prefix="tts-studio-test-") as temp:
        root = Path(temp)
        calls = []

        def synth(text, voice, output, target, match, **options):
            calls.append((text, voice, target, match))
            _write_audio(output)
            if options.get("on_progress"):
                options["on_progress"](1)
            return 1.0

        with patch.object(studio, "TTS_OUTPUT", root), patch.object(studio, "TTS_TEMP", root), \
            patch.object(studio, "ensure_vieneu_dirs"), patch.object(studio, "list_voices", return_value=[]), \
            patch.object(studio, "reference_cache_token", return_value="reference"), \
            patch.object(studio, "tts_segment", side_effect=synth):
            result = studio.synth_text_job(text="Xin chào", voice="vn:clone:test", lang="vi")
            assert calls[0][0] == "Xin chào"
            assert "cloneRoute" not in result["meta"]
            metadata = json.loads((root / result["id"] / "meta.json").read_text())
            assert "cloneRoute" not in metadata
