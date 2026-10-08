"""System RAM headroom and idle release for heavyweight local models."""
from __future__ import annotations

import contextlib
import functools
import gc
import math
import os
import sys
import threading
import time
import logging
from dataclasses import dataclass
from typing import Callable, Iterator


logger = logging.getLogger(__name__)

try:
    import resource
except ImportError:  # pragma: no cover - Windows uses psutil below.
    resource = None  # type: ignore[assignment]


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def max_rss_mb() -> float:
    """Legacy status field; RSS is not used as an admission limit anymore."""
    return 0.0


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


def _system_memory_mb() -> tuple[float, float]:
    """Return (total, available) RAM; keep Linux usable before psutil install."""
    try:
        import psutil

        memory = psutil.virtual_memory()
        return memory.total / (1024.0 * 1024.0), memory.available / (1024.0 * 1024.0)
    except ImportError:
        if sys.platform.startswith("linux"):
            values: dict[str, float] = {}
            with open("/proc/meminfo", encoding="ascii") as stream:
                for line in stream:
                    key, separator, raw = line.partition(":")
                    if separator and raw.strip().endswith(" kB"):
                        values[key] = float(raw.strip()[:-3]) / 1024.0
            total = values.get("MemTotal", 0.0)
            available = values.get("MemAvailable", values.get("MemFree", 0.0))
            if total > 0 and available >= 0:
                return total, available
        raise RuntimeError(
            "Không đo được RAM hệ thống; cài psutil / Unable to measure system RAM; install psutil"
        )


@dataclass
class _Entry:
    release: Callable[[], None]
    last_used: float
    active: int = 0


_lock = threading.RLock()
_entries: dict[str, _Entry] = {}
_active_names: dict[str, int] = {}
_monitor_started = False
_last_memory_state: dict[str, float | str] = {}


def _pressure_level(available_ratio: float) -> str:
    if available_ratio < 0.05:
        return "critical"
    if available_ratio < 0.10:
        return "high"
    if available_ratio < 0.15:
        return "elevated"
    return "normal"


def memory_pressure() -> dict[str, float | str]:
    """Return current host RAM pressure without blocking a job admission."""
    global _last_memory_state
    try:
        total_mb, available_mb = _system_memory_mb()
    except Exception as exc:
        logger.debug("Unable to sample system RAM: %s", exc, exc_info=True)
        return {"totalMb": 0.0, "availableMb": 0.0, "availableRatio": 1.0, "level": "unknown"}
    ratio = max(0.0, min(1.0, available_mb / max(1.0, total_mb)))
    state: dict[str, float | str] = {
        "totalMb": round(total_mb, 1),
        "availableMb": round(available_mb, 1),
        "availableRatio": ratio,
        "level": _pressure_level(ratio),
    }
    with _lock:
        _last_memory_state = state
    return state


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
    """Apply soft RAM pressure controls before loading another model.

    This intentionally never rejects a job for low available RAM.  It releases
    idle models and records a warning so callers can continue while adaptive
    worker pools reduce concurrency. Reclaimable filesystem cache counts as
    available RAM; peak process RSS is not a pressure signal.
    """
    reserve = max(0.0, reserve_mb)
    state = memory_pressure()
    if state["level"] == "unknown":
        return
    total_mb = float(state["totalMb"])
    available_mb = float(state["availableMb"])
    ratio = float(state["availableRatio"])
    # Start reclaiming before the critical 5% boundary. Force release is only
    # used under high pressure; activity() keeps in-flight models pinned.
    if ratio < 0.15 or available_mb < total_mb * 0.05 + reserve:
        released = _release_idle(force=ratio < 0.10 or available_mb < total_mb * 0.05 + reserve)
        refreshed = memory_pressure()
        logger.warning(
            "System RAM pressure=%s; available=%s/%s MB (%.1f%%), released_idle_models=%s, "
            "requested_reserve=%s MB; continuing with adaptive throttling",
            refreshed["level"], refreshed["availableMb"], refreshed["totalMb"],
            float(refreshed["availableRatio"]) * 100.0, released, reserve,
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


def status() -> dict[str, float | int | str]:
    with _lock:
        loaded = len(_entries)
    state = memory_pressure()
    return {
        "rssMb": round(_rss_mb(), 1),
        "maxRssMb": max_rss_mb(),
        "loadedModels": loaded,
        "availableMemoryMb": float(state["availableMb"]),
        "availableMemoryRatio": float(state["availableRatio"]),
        "memoryPressure": str(state["level"]),
    }
