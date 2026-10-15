"""Google Flow automation integration — keep package import free of service.py."""
from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["service"]


def __getattr__(name: str) -> Any:
    if name == "service":
        mod = import_module(".service", __name__)
        value = getattr(mod, "service")
        globals()["service"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
