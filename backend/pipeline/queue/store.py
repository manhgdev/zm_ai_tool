"""JSON-backed unified job queue. Survives restart."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from pipeline.core.config import DATA

_lock = threading.RLock()
_PATH = DATA / "queue" / "jobs.json"

TERMINAL = {"done", "failed", "cancelled"}
LOG_CAP = 500


def _path() -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    folder = DATA / "queue"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "jobs.json"


def load_all() -> list[dict[str, Any]]:
    path = _path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    jobs = raw if isinstance(raw, list) else raw.get("jobs") or []
    return [j for j in jobs if isinstance(j, dict)]


def save_all(jobs: list[dict[str, Any]]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def mutate(job_id: str, patch: dict[str, Any], *, log: str | None = None) -> dict[str, Any] | None:
    with _lock:
        jobs = load_all()
        found = None
        for job in jobs:
            if job.get("id") == job_id:
                job.update(patch)
                if log:
                    rows = list(job.get("log") or [])
                    stamp = time.strftime("%H:%M:%S")
                    for raw in str(log).splitlines():
                        line = raw.rstrip()
                        if line:
                            rows.append(f"[{stamp}] {line[:500]}")
                    job["log"] = rows[-LOG_CAP:]
                job["updatedAt"] = time.time()
                found = job
                break
        if found is None:
            return None
        save_all(jobs)
        return dict(found)


def insert(job: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        jobs = load_all()
        jobs.append(job)
        save_all(jobs)
        return dict(job)


def replace_all(jobs: list[dict[str, Any]]) -> None:
    with _lock:
        save_all(jobs)


def get(job_id: str) -> dict[str, Any] | None:
    with _lock:
        for job in load_all():
            if job.get("id") == job_id:
                return dict(job)
    return None


def mark_interrupted() -> None:
    """RUNNING/QUEUED jobs left from a previous session wait for an explicit Resume (never auto-run)."""
    with _lock:
        jobs = load_all()
        changed = False
        for job in jobs:
            if job.get("status") in {"running", "queued"}:
                job["status"] = "interrupted"
                job["updatedAt"] = time.time()
                changed = True
        if changed:
            save_all(jobs)
