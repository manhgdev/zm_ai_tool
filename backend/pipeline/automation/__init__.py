"""Multi-job end-to-end video production automation — lazy service import."""
from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["AutomationService", "service"]


def __getattr__(name: str) -> Any:
    if name in {"AutomationService", "service"}:
        mod = import_module(".service", __name__)
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
