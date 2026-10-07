"""Process memory budget and idle release for heavyweight local models."""
from __future__ import annotations

import contextlib
import functools
import gc
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

try:
    import resource
except ImportError:  # pragma: no cover - Windows uses psutil below.
    resource = None  # type: ignore[assignment]


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def max_rss_mb() -> float:
    """Maximum RSS for this backend; 0 disables the budget."""
    return _float_env("ZM_AI_TOOL_MAX_RSS_MB", 3072.0)


def idle_seconds() -> float:
    return _float_env("ZM_AI_TOOL_MODEL_IDLE_SECONDS", 10.0)


def _rss_mb() -> float:
    try:
        import psutil

        process = psutil.Process()
        value = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                value += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return value / (1024.0 * 1024.0)
    except Exception:
        # resource is a peak value, but remains a useful fallback when psutil
        # is not installed.  It is only used for the pre-load safety gate.
        if resource is None:
            return 0.0
        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value / (1024.0 * 1024.0) if sys.platform == "darwin" else value / 1024.0


@dataclass
class _Entry:
    release: Callable[[], None]
    last_used: float
    active: int = 0


_lock = threading.RLock()
_entries: dict[str, _Entry] = {}
_active_names: dict[str, int] = {}
_monitor_started = False


def register(name: str, release: Callable[[], None]) -> None:
    with _lock:
        _entries[name] = _Entry(
            release=release,
            last_used=time.monotonic(),
            active=_active_names.get(name, 0),
        )


def unregister(name: str) -> None:
    with _lock:
        _entries.pop(name, None)


def touch(name: str) -> None:
    with _lock:
        entry = _entries.get(name)
        if entry:
            entry.last_used = time.monotonic()


@contextlib.contextmanager
def activity(name: str) -> Iterator[None]:
    with _lock:
        _active_names[name] = _active_names.get(name, 0) + 1
        entry = _entries.get(name)
        if entry:
            entry.active += 1
            entry.last_used = time.monotonic()
    try:
        yield
    finally:
        with _lock:
            count = max(0, _active_names.get(name, 1) - 1)
            if count:
                _active_names[name] = count
            else:
                _active_names.pop(name, None)
            entry = _entries.get(name)
            if entry:
                entry.active = max(0, entry.active - 1)
                entry.last_used = time.monotonic()


def guarded(name: str) -> Callable:
    """Decorator keeping a model pinned for the duration of one inference."""
    def _decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            with activity(name):
                return fn(*args, **kwargs)

        return _wrapped

    return _decorate


def _release_idle(force: bool = False) -> int:
    now = time.monotonic()
    with _lock:
        candidates = sorted(
            ((name, entry) for name, entry in _entries.items()
             if entry.active == 0 and (force or now - entry.last_used >= idle_seconds())),
            key=lambda item: item[1].last_used,
        )
    released = 0
    for name, entry in candidates:
        try:
            entry.release()
        finally:
            unregister(name)
            released += 1
    if released:
        gc.collect()
    return released


def ensure_budget(reserve_mb: float = 768.0) -> None:
    """Release idle models before loading another model, or fail clearly."""
    limit = max_rss_mb()
    if not limit:
        return
    if _rss_mb() + max(0.0, reserve_mb) <= limit:
        return
    _release_idle(force=True)
    if _rss_mb() + max(0.0, reserve_mb) > limit:
        raise MemoryError(
            f"Backend RSS vượt ngân sách {limit:.0f} MB; "
            "hãy tăng ZM_AI_TOOL_MAX_RSS_MB hoặc đóng bớt tác vụ."
        )


def start_monitor() -> None:
    """Start one daemon that unloads models after the configured idle period."""
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True

    def _run() -> None:
        while True:
            time.sleep(min(10.0, max(2.0, idle_seconds() / 3.0)))
            try:
                _release_idle()
            except Exception:
                pass

    threading.Thread(target=_run, name="model-memory-guard", daemon=True).start()


def status() -> dict[str, float | int]:
    with _lock:
        loaded = len(_entries)
    return {"rssMb": round(_rss_mb(), 1), "maxRssMb": max_rss_mb(), "loadedModels": loaded}
