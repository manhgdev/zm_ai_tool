"""Resolve the immutable desktop AI runtime selected by ``current.json``."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from functools import lru_cache


def runtime_home() -> Path:
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        install = Path(sys.executable).absolute().parent
        if (install / '.zmaio-installed').is_file():
            # Match the launcher even if an old HOME survives an update or
            # was supplied by a parent process. Never install back onto C.
            return install / 'user-data'
    raw = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parents[2]


def active_runtime_dir() -> Path:
    """Return the activated runtime, falling back to the legacy venv."""
    home = runtime_home()
    pointer = home / "runtime" / "current.json"
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        relative = str(payload.get("path") or "").strip()
        candidate = (pointer.parent / relative).resolve()
        root = pointer.parent.resolve()
        legacy = (home / '.venv-runtime').resolve()
        if relative and (root in candidate.parents or candidate == legacy) and candidate.is_dir():
            return candidate
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return home / ".venv-runtime"


@lru_cache(maxsize=1)
def _linux_cpu_only() -> bool:
    """Cache physical inventory per worker, not user mode or GPU readiness."""
    from .media import detect_device

    device = detect_device()
    # An NVIDIA adapter with a broken driver is still a GPU, not CPU-only.
    return (device.get('gpuKind') not in ('nvidia', 'apple')
            and device.get('accel', 'cpu') == 'cpu')


def cpu_mode_enabled() -> bool:
    """Shared CPU-only state: saved choice on any OS, or a CPU-only Linux host."""
    try:
        data = json.loads((runtime_home() / 'runtime/current.json').read_text(encoding='utf-8'))
        if data.get('computeMode') == 'cpu':
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return sys.platform.startswith('linux') and _linux_cpu_only()


def clear_compute_caches() -> None:
    _linux_cpu_only.cache_clear()
    from .accel import _cache, _lock
    with _lock:
        _cache.clear()
    for module_name, names in {
        'pipeline.ocr.extract_parts.runtime': ('_rapidocr_gpu_kwargs',),
        'pipeline.core.system_check.checks': ('_invalidate_checks_cache',),
    }.items():
        module = sys.modules.get(module_name)
        if module:
            for name in names:
                fn = getattr(module, name, None)
                if fn:
                    getattr(fn, 'cache_clear', fn)()


def runtime_python() -> Path:
    root = active_runtime_dir()
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def runtime_site() -> Path:
    root = active_runtime_dir()
    if sys.platform == "win32":
        return root / "Lib/site-packages"
    return root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
