"""Small atomic JSON store dedicated to Flow accounts and generation jobs."""
from __future__ import annotations

import json
import threading
import time
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


# ponytail: this process is the only writer, so parsed tables are cached in
# memory (invalidated by mtime). Ceiling: external edits are seen only after
# the file's mtime changes; upgrade path is sqlite.
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _read(name: str) -> list[dict[str, Any]]:
    path = root() / f"{name}.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return []
    cached = _CACHE.get(name)
    if cached and cached[0] == mtime:
        return list(cached[1])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        rows = value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []
    _CACHE[name] = (mtime, rows)
    return list(rows)


def _write(name: str, rows: list[dict[str, Any]]) -> None:
    path = root() / f"{name}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    _CACHE[name] = (path.stat().st_mtime_ns, rows)


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
                if name == "jobs" and row.get("status") == "cancelled" and patch.get("status") == "processing":
                    # ponytail: late worker write after cancel; a retry re-queues via status=queued first.
                    patch = {k: v for k, v in patch.items() if k not in {"status", "stage", "progress"}}
                if name == "jobs":
                    # Keep lifecycle timestamps at the persistence boundary so
                    # every worker path reports the same history data.
                    now = time.time()
                    effective_status = patch.get("status", row.get("status"))
                    lifecycle_patch = dict(patch)
                    lifecycle_patch.setdefault("updatedAt", now)
                    if effective_status == "processing" and not row.get("startedAt"):
                        lifecycle_patch["startedAt"] = now
                    if effective_status in {"queued", "processing"}:
                        lifecycle_patch["finishedAt"] = None
                    if effective_status in {"done", "failed", "cancelled", "action_required"}:
                        finished_at = row.get("finishedAt") or now
                        lifecycle_patch.setdefault("finishedAt", finished_at)
                        started = row.get("startedAt") or row.get("createdAt") or finished_at
                        try:
                            lifecycle_patch.setdefault("elapsedSeconds", max(0.0, float(finished_at) - float(started)))
                        except (TypeError, ValueError):
                            lifecycle_patch.setdefault("elapsedSeconds", 0.0)
                    elif row.get("startedAt") or lifecycle_patch.get("startedAt"):
                        try:
                            lifecycle_patch["elapsedSeconds"] = max(0.0, now - float(lifecycle_patch.get("startedAt") or row["startedAt"]))
                        except (TypeError, ValueError):
                            pass
                    if (
                        effective_status in {"done", "failed", "cancelled"}
                        and row.get("fallbackNotice")
                        and not patch.get("fallbackNotice")
                    ):
                        lifecycle_patch["fallbackNotice"] = None
                    if (
                        effective_status in {"done", "failed", "cancelled"}
                        and row.get("recoveryNotice")
                        and not patch.get("recoveryNotice")
                    ):
                        lifecycle_patch["recoveryNotice"] = None
                    row.update(lifecycle_patch)
                else:
                    row.update(patch)
                _write(name, rows)
                result = dict(row)
                break
    if result is not None:
        _publish_row(name, "updated", result)
    return result


def patch_rows(name: str, patches: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Patch many rows with one read/write transaction.

    Job lifecycle timestamps mirror ``patch_row`` so a bulk retry has the same
    history semantics while avoiding one JSON rewrite per job.
    """
    if not patches:
        return []
    changed: list[dict[str, Any]] = []
    with _LOCK:
        rows = _read(name)
        for row in rows:
            row_id = str(row.get("id") or "")
            patch = patches.get(row_id)
            if patch is None:
                continue
            if name == "jobs" and row.get("status") == "cancelled" and patch.get("status") == "processing":
                patch = {k: v for k, v in patch.items() if k not in {"status", "stage", "progress"}}
            if name == "jobs":
                now = time.time()
                effective_status = patch.get("status", row.get("status"))
                lifecycle_patch = dict(patch)
                lifecycle_patch.setdefault("updatedAt", now)
                if effective_status == "processing" and not row.get("startedAt"):
                    lifecycle_patch["startedAt"] = now
                if effective_status in {"queued", "processing"}:
                    lifecycle_patch["finishedAt"] = None
                if effective_status in {"done", "failed", "cancelled", "action_required"}:
                    finished_at = row.get("finishedAt") or now
                    lifecycle_patch.setdefault("finishedAt", finished_at)
                    started = row.get("startedAt") or row.get("createdAt") or finished_at
                    try:
                        lifecycle_patch.setdefault("elapsedSeconds", max(0.0, float(finished_at) - float(started)))
                    except (TypeError, ValueError):
                        lifecycle_patch.setdefault("elapsedSeconds", 0.0)
                elif row.get("startedAt") or lifecycle_patch.get("startedAt"):
                    try:
                        lifecycle_patch["elapsedSeconds"] = max(0.0, now - float(lifecycle_patch.get("startedAt") or row["startedAt"]))
                    except (TypeError, ValueError):
                        pass
                if (
                    effective_status in {"done", "failed", "cancelled"}
                    and row.get("fallbackNotice")
                    and not patch.get("fallbackNotice")
                ):
                    lifecycle_patch["fallbackNotice"] = None
                if (
                    effective_status in {"done", "failed", "cancelled"}
                    and row.get("recoveryNotice")
                    and not patch.get("recoveryNotice")
                ):
                    lifecycle_patch["recoveryNotice"] = None
                row.update(lifecycle_patch)
            else:
                row.update(patch)
            changed.append(dict(row))
        if changed:
            _write(name, rows)
    for row in changed:
        _publish_row(name, "updated", row)
    return changed


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
                started = row.get('startedAt') or row.get('createdAt') or updated_at
                try:
                    elapsed = max(0.0, float(updated_at) - float(started))
                except (TypeError, ValueError):
                    elapsed = 0.0
                row.update(status='cancelled', stage='cancelled', progress=0,
                           finishedAt=updated_at, elapsedSeconds=elapsed, updatedAt=updated_at)
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
        if topic_name == "accounts" and row is not None:
            # Account passwords are intentionally persisted for reconnect, but
            # realtime events are still public to the browser session.
            row = dict(row)
            row.pop("password", None)
        if topic_name == "accounts":
            kind = "flow.account"
        elif topic_name == "logs":
            kind = "flow.log.appended" if action in {"created", "updated"} else "flow.log.deleted"
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
