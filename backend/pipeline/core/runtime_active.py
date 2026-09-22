"""Resolve the immutable desktop AI runtime selected by ``current.json``."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def runtime_home() -> Path:
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
        if relative and root in candidate.parents and candidate.is_dir():
            return candidate
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return home / ".venv-runtime"


def runtime_python() -> Path:
    root = active_runtime_dir()
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def runtime_site() -> Path:
    root = active_runtime_dir()
    if sys.platform == "win32":
        return root / "Lib/site-packages"
    return root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
