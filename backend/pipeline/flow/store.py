"""Small atomic JSON store dedicated to Flow accounts and generation jobs."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pipeline.core.config import DATA

ROOT = DATA / "flow"
_LOCK = threading.RLock()


def root() -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


def profile_dir(account_id: str) -> Path:
    path = root() / "profiles" / account_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(name: str) -> list[dict[str, Any]]:
    path = root() / f"{name}.json"
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write(name: str, rows: list[dict[str, Any]]) -> None:
    path = root() / f"{name}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def list_rows(name: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(row) for row in _read(name)]


def get_row(name: str, row_id: str) -> dict[str, Any] | None:
    return next((row for row in list_rows(name) if row.get("id") == row_id), None)


def put_row(name: str, row: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        rows = _read(name)
        index = next((i for i, item in enumerate(rows) if item.get("id") == row.get("id")), -1)
        if index < 0:
            rows.append(dict(row))
        else:
            rows[index] = dict(row)
        _write(name, rows)
        result = dict(row)
    _publish_row(name, "created" if index < 0 else "updated", result)
    return result


def put_rows(name: str, new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch upsert: one read + one write regardless of N. O(N) vs O(N²) for N put_row calls."""
    if not new_rows:
        return []
    with _LOCK:
        rows = _read(name)
        idx = {str(row.get("id")): i for i, row in enumerate(rows) if row.get("id")}
        for row in new_rows:
            rid = str(row.get("id") or "")
            if rid and rid in idx:
                rows[idx[rid]] = dict(row)
            else:
                rows.append(dict(row))
        _write(name, rows)
    result = [dict(r) for r in new_rows]
    for row in result:
        _publish_row(name, "updated", row)
    return result


def patch_row(name: str, row_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    with _LOCK:
        rows = _read(name)
        for row in rows:
            if row.get("id") == row_id:
                row.update(patch)
                _write(name, rows)
                result = dict(row)
                break
    if result is not None:
        _publish_row(name, "updated", result)
    return result


def delete_row(name: str, row_id: str) -> bool:
    with _LOCK:
        rows = _read(name)
        kept = [row for row in rows if row.get("id") != row_id]
        if len(kept) == len(rows):
            return False
        _write(name, kept)
    _publish(name, "deleted", row_id, None)
    return True


def cancel_active_jobs(ids: set[str], updated_at: float) -> int:
    changed_rows: list[dict[str, Any]] = []
    with _LOCK:
        rows = _read('jobs')
        changed = 0
        for row in rows:
            if row.get('id') in ids and row.get('status') not in {'done', 'failed', 'cancelled', 'action_required'}:
                row.update(status='cancelled', stage='cancelled', progress=0, updatedAt=updated_at)
                changed += 1
                changed_rows.append(dict(row))
        if changed:
            _write('jobs', rows)
    for row in changed_rows:
        _publish_row("jobs", "updated", row)
    return changed


def delete_rows(name: str, ids: set[str]) -> list[dict[str, Any]]:
    with _LOCK:
        rows = _read(name)
        removed = [row for row in rows if row.get('id') in ids]
        if removed:
            _write(name, [row for row in rows if row.get('id') not in ids])
    for row in removed:
        _publish_row(name, "deleted", row)
    return removed


def _publish(topic_name: str, action: str, row_id: str, row: dict[str, Any] | None) -> None:
    try:
        from pipeline.core.realtime import realtime
        topic = "series" if topic_name == "series" else "flow"
        if topic_name == "accounts":
            kind = "flow.account"
        elif topic_name == "logs":
            kind = "flow.log"
        elif topic_name == "series":
            kind = "series"
        else:
            kind = "flow.job"
        realtime.publish(topic, f"{kind}.{action}", str(row_id), row)
    except Exception:
        # Realtime delivery is best-effort; persistence must never fail because
        # the browser stream is unavailable.
        return


def _publish_row(topic_name: str, action: str, row: dict[str, Any]) -> None:
    _publish(topic_name, action, str(row.get("id") or ""), row)
