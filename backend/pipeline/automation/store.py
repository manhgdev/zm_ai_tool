from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.core.config import DATA


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutomationStore:
    """Small SQLite store for durable multi-job automation state."""

    def __init__(self, db_path: Path | None = None, jobs_root: Path | None = None):
        root = DATA / "automation"
        self.db_path = Path(db_path or root / "automation.sqlite3")
        self.jobs_root = Path(jobs_root or root / "jobs")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS automation_jobs (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              input_mode TEXT NOT NULL,
              status TEXT NOT NULL,
              stage TEXT NOT NULL,
              progress REAL NOT NULL DEFAULT 0,
              input_json TEXT NOT NULL DEFAULT '{}',
              settings_json TEXT NOT NULL DEFAULT '{}',
              artifacts_json TEXT NOT NULL DEFAULT '{}',
              child_job_ids_json TEXT NOT NULL DEFAULT '[]',
              error_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automation_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              level TEXT NOT NULL,
              stage TEXT NOT NULL,
              message TEXT NOT NULL,
              details_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES automation_jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS automation_logs_job_idx
              ON automation_logs(job_id, id);
            """
        )
        # The pipeline writes its final durable checkpoint (done/100) just
        # before the worker returns.  A restart in that tiny window must not
        # turn a finished video into a resumable interruption.
        self._db.execute(
            "UPDATE automation_jobs SET status='completed', error_json=NULL "
            "WHERE status = 'running' AND stage = 'done' AND progress >= 100"
        )
        self._db.execute(
            "UPDATE automation_jobs SET error_json=NULL WHERE status = 'completed'"
        )
        self._db.execute(
            "UPDATE automation_jobs SET status='interrupted', error_json=? "
            "WHERE status = 'running'",
            (json.dumps({"code": "AUTOMATION_INTERRUPTED", "message": "Job interrupted by app restart"}),),
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for column, fallback in (
            ("input_json", {}),
            ("settings_json", {}),
            ("artifacts_json", {}),
            ("child_job_ids_json", []),
            ("error_json", None),
        ):
            raw = item.pop(column)
            if raw is None:
                item[column.removesuffix("_json")] = fallback
                continue
            try:
                item[column.removesuffix("_json")] = json.loads(raw)
            except (TypeError, ValueError):
                item[column.removesuffix("_json")] = fallback
        item["progress"] = float(item.get("progress") or 0)
        return item

    def workspace(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
            raise ValueError("Invalid automation job id")
        target = self.jobs_root / job_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def create_job(self, input_mode: str, title: str, settings: dict[str, Any], input_data: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = f"auto_{uuid.uuid4().hex[:16]}"
        now = _now()
        item = {
            "id": job_id,
            "title": str(title or "Automation job")[:160],
            "input_mode": str(input_mode),
            "status": "queued",
            "stage": "input",
            "progress": 0,
            "input": dict(input_data or {}),
            "settings": dict(settings or {}),
            "artifacts": {},
            "child_job_ids": [],
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        self.workspace(job_id)
        with self._lock:
            self._db.execute(
                "INSERT INTO automation_jobs "
                "(id,title,input_mode,status,stage,progress,input_json,settings_json,artifacts_json,child_job_ids_json,error_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, item["title"], item["input_mode"], item["status"], item["stage"], item["progress"],
                    json.dumps(item["input"], ensure_ascii=False), json.dumps(item["settings"], ensure_ascii=False),
                    "{}", "[]", None, now, now,
                ),
            )
            self._db.commit()
        _publish("automation.job.created", job_id, item)
        return item

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode(row)

    def delete_job(self, job_id: str) -> bool:
        """Delete one job and its log history atomically."""
        if not job_id or "/" in job_id or "\\" in job_id:
            raise ValueError("Invalid automation job id")
        with self._lock:
            cursor = self._db.execute("DELETE FROM automation_jobs WHERE id=?", (job_id,))
            self._db.execute("DELETE FROM automation_logs WHERE job_id=?", (job_id,))
            self._db.commit()
        removed = cursor.rowcount > 0
        if removed:
            _publish("automation.job.deleted", job_id, None)
        return removed

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM automation_jobs ORDER BY updated_at DESC").fetchall()
        return [self._decode(row) for row in rows if row is not None]

    def update_job(self, job_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {
            "title", "input_mode", "status", "stage", "progress", "input", "settings",
            "artifacts", "child_job_ids", "error",
        }
        patch = {key: value for key, value in values.items() if key in allowed}
        if not patch:
            return self.get_job(job_id)
        encoded: dict[str, Any] = {}
        for key, value in patch.items():
            target = f"{key}_json" if key in {"input", "settings", "artifacts", "child_job_ids", "error"} else key
            encoded[target] = json.dumps(value, ensure_ascii=False) if target.endswith("_json") and value is not None else value
        encoded["updated_at"] = _now()
        with self._lock:
            columns = ",".join(f"{key}=?" for key in encoded)
            self._db.execute(f"UPDATE automation_jobs SET {columns} WHERE id=?", (*encoded.values(), job_id))
            self._db.commit()
        result = self.get_job(job_id)
        if result:
            _publish("automation.job.updated", job_id, result)
        return result

    def append_log(self, job_id: str, level: str, message: str, *, stage: str = "", details: dict[str, Any] | None = None) -> dict[str, Any]:
        item = {
            "jobId": job_id,
            "level": str(level or "info"),
            "stage": str(stage or ""),
            "message": str(message or "")[:4000],
            "details": dict(details or {}),
            "createdAt": _now(),
        }
        with self._lock:
            self._db.execute(
                "INSERT INTO automation_logs(job_id,level,stage,message,details_json,created_at) VALUES (?,?,?,?,?,?)",
                (job_id, item["level"], item["stage"], item["message"], json.dumps(item["details"], ensure_ascii=False), item["createdAt"]),
            )
            self._db.commit()
        return item

    def list_logs(self, job_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT job_id,level,stage,message,details_json,created_at FROM automation_logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError):
                item["details"] = {}
            item["jobId"] = item.pop("job_id")
            item["createdAt"] = item.pop("created_at")
            result.append(item)
        return result


def _publish(event_type: str, job_id: str, job: dict[str, Any] | None) -> None:
    try:
        from pipeline.core.realtime import realtime
        payload = None
        if job:
            payload = {
                key: job.get(key) for key in (
                    "id", "title", "input_mode", "status", "stage", "progress",
                    "artifacts", "child_job_ids", "error", "created_at", "updated_at",
                )
            }
        realtime.publish("automation", event_type, job_id, payload)
    except Exception:
        return
