from __future__ import annotations

import json
import sys
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pipeline.tts import studio


def _empty_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))


def _valid_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x10\x01" * 24000)


def test_empty_cache_is_ignored(tmp_path: Path) -> None:
    job = tmp_path / "empty"
    job.mkdir()
    _empty_wav(job / "audio.wav")
    (job / "meta.json").write_text(json.dumps({"fingerprint": "same", "status": "done"}))
    with patch.object(studio, "TTS_OUTPUT", tmp_path), patch.object(studio, "ensure_vieneu_dirs"):
        assert studio._find_cached_job("same") is None


def test_history_hides_empty_done_jobs_without_deleting_them(tmp_path: Path) -> None:
    job = tmp_path / "empty"
    job.mkdir()
    _empty_wav(job / "audio.wav")
    meta = job / "meta.json"
    meta.write_text(json.dumps({"status": "done", "title": "empty"}))
    with patch.object(studio, "TTS_OUTPUT", tmp_path), patch.object(studio, "ensure_vieneu_dirs"):
        assert studio.list_history() == []
    assert meta.is_file() and (job / "audio.wav").is_file()


def test_ensure_wav_rejects_empty_audio(tmp_path: Path) -> None:
    job = tmp_path / "empty"
    job.mkdir()
    _empty_wav(job / "audio.wav")
    with patch.object(studio, "TTS_OUTPUT", tmp_path):
        with pytest.raises(FileNotFoundError):
            studio.ensure_wav("empty")


def test_publish_rejects_empty_audio_before_creating_output(tmp_path: Path) -> None:
    job = tmp_path / "empty"
    job.mkdir()
    _empty_wav(job / "audio.wav")
    with patch.object(studio, "TTS_OUTPUT", tmp_path):
        with pytest.raises(FileNotFoundError):
            studio.publish_job_outputs("empty", str(tmp_path / "published"))
    assert not (tmp_path / "published").exists()


def test_publish_error_marks_existing_job_as_error(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    _valid_wav(job / "audio.wav")
    meta = job / "meta.json"
    meta.write_text(json.dumps({"status": "done", "title": "job"}))
    with patch.object(studio, "TTS_OUTPUT", tmp_path):
        studio.mark_job_meta_error("job", "publish failed")
        assert json.loads(meta.read_text())["status"] == "error"
        assert studio.list_history() == []
