"""Whisper-only site-packages must not mark OCR/VieNeu as missing on fast checks."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from pipeline.core.system_check.probe import _runtime_modules_fs_missing, _venv_site_packages


def _dist(site: Path, name: str) -> None:
    (site / f"{name}-1.0.dist-info").mkdir(parents=True)


def test_whisper_only_runtime_reports_only_other_modules(tmp_path: Path) -> None:
    root = tmp_path / "env-whisper"
    py = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    site = _venv_site_packages(root)
    site.mkdir(parents=True)
    for dist in ("faster-whisper", "av", "torch", "torchaudio", "soundfile", "cffi"):
        _dist(site, dist)

    with (
        patch("pipeline.core.runtime_active.active_runtime_dir", return_value=root),
        patch("pipeline.core.runtime_active.runtime_python", return_value=py),
    ):
        missing = _runtime_modules_fs_missing()

    assert "faster_whisper" not in missing
    assert "torch" not in missing
    assert "rapidocr_onnxruntime" in missing
    assert "vieneu" in missing
    assert "sherpa_onnx" in missing


def test_empty_runtime_reports_all_modules(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    with (
        patch("pipeline.core.runtime_active.active_runtime_dir", return_value=root),
        patch("pipeline.core.runtime_active.runtime_python", return_value=root / "missing"),
    ):
        missing = _runtime_modules_fs_missing()
    assert "faster_whisper" in missing
    assert len(missing) >= 8


def test_frozen_fast_checks_mark_whisper_ready_when_only_whisper_installed(tmp_path: Path) -> None:
    from pipeline.core.system_check import checks

    root = tmp_path / "env-whisper"
    py = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    site = _venv_site_packages(root)
    site.mkdir(parents=True)
    for dist in ("faster-whisper", "av", "torch", "torchaudio", "soundfile", "cffi"):
        _dist(site, dist)

    device = {"gpuKind": "none", "accel": "cpu", "install": {}, "osLabel": "Windows", "arch": "x64"}
    with (
        patch.object(sys, "frozen", True, create=True),
        patch("pipeline.core.config.DATA", tmp_path),
        patch("pipeline.core.media.detect_device", return_value=device),
        patch("pipeline.core.media._ff_bin", return_value=str(tmp_path / "missing.exe")),
        patch.object(checks, "_ollama_executable", return_value=None),
        patch.object(checks, "_node_executable", return_value=None),
        patch.object(checks, "_demucs_venv_fast", return_value=(False, "missing")),
        patch.object(checks, "_which", return_value=None),
        patch("pipeline.core.runtime_active.active_runtime_dir", return_value=root),
        patch("pipeline.core.runtime_active.runtime_python", return_value=py),
        patch("httpx.get", side_effect=OSError("offline")),
    ):
        result = checks._system_checks_uncached(fast=True)

    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["ai_runtime"]["ok"] is True
    assert by_id["ai_runtime_ocr"]["ok"] is False
    assert by_id["ai_runtime_vieneu"]["ok"] is False
