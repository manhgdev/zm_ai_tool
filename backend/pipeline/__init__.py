"""Local video clone pipeline package.

Keep import side effects minimal so desktop/API boot does not pull ASR/TTS/orchestrate.
Heavy symbols stay available via lazy ``__getattr__`` for callers that still use
``from pipeline import name``.
"""
from __future__ import annotations

from typing import Any

from .core.config import DATA, PUBLIC_DATA

__all__ = [
    "DATA",
    "PUBLIC_DATA",
    "ensure_layout",
    "ffprobe_duration",
    "find_project_by_fp",
    "hardware",
    "list_voices",
    "load_meta",
    "mutate_meta",
    "out_final",
    "project_dir",
    "request_cancel",
    "run_dub",
    "run_export",
    "run_pipeline",
    "save_meta",
    "set_status",
    "tts_cache_key",
    "tts_segment",
    "video_fingerprint",
]

_LAZY: dict[str, tuple[str, str]] = {
    "ensure_layout": (".core.project", "ensure_layout"),
    "find_project_by_fp": (".core.project", "find_project_by_fp"),
    "load_meta": (".core.project", "load_meta"),
    "mutate_meta": (".core.project", "mutate_meta"),
    "out_final": (".core.project", "out_final"),
    "project_dir": (".core.project", "project_dir"),
    "save_meta": (".core.project", "save_meta"),
    "set_status": (".core.project", "set_status"),
    "video_fingerprint": (".core.project", "video_fingerprint"),
    "request_cancel": (".core.jobs", "request_cancel"),
    "ffprobe_duration": (".core.media", "ffprobe_duration"),
    "hardware": (".core.media", "hardware"),
    "run_dub": (".run", "run_dub"),
    "run_export": (".run", "run_export"),
    "run_pipeline": (".run", "run_pipeline"),
    "list_voices": (".tts.manager", "list_voices"),
    "tts_cache_key": (".tts.manager", "tts_cache_key"),
    "tts_segment": (".tts.manager", "tts_segment"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), attr)
    globals()[name] = value
    return value
