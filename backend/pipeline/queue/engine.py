"""Unified Job Queue + resource scheduler for clone and review."""
from __future__ import annotations

import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from pipeline.core.app_log import append_log
from pipeline.core.config import DATA, PUBLIC_DATA, safe_child
from pipeline.core.jobs import Cancelled, arm_job, begin_job, check_cancel, request_cancel, set_job_context
from pipeline.core.resources import gpu_job_cap
from pipeline.gpu.manager import assign_device, diagnostics, vram_free_mb
from pipeline.queue import store
from pipeline.queue.paths import output_name, scan_videos

_engine_lock = threading.Lock()
_engine: "QueueEngine | None" = None

DISK_PAUSE_BYTES = 2 * 1024 ** 3
ERROR_TYPES = {
    "SOURCE_ERROR", "FFMPEG_ERROR", "MODEL_ERROR", "GPU_OOM", "DRIVER_ERROR",
    "TTS_ERROR", "ASR_ERROR", "VISION_ERROR", "DISK_FULL", "RENDER_ERROR",
    "CANCELLED", "UNKNOWN",
}


class ArtifactBusyError(OSError):
    """An owned artifact is still locked by a player or Explorer on Windows."""


def classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, Cancelled) or type(exc).__name__ == "Cancelled":
        return "CANCELLED"
    if "review_cloud_" in text:
        # Cloud Review has a stable, UI-safe code.  It is a model/provider
        # failure, never an unclassified queue error.
        return "MODEL_ERROR"
    if "oom" in text or "out of memory" in text:
        return "GPU_OOM"
    if "disk" in text or "no space" in text:
        return "DISK_FULL"
    if "ffmpeg" in text:
        return "FFMPEG_ERROR"
    if "whisper" in text or "asr" in text:
        return "ASR_ERROR"
    if "tts" in text or "vieneu" in text:
        return "TTS_ERROR"
    if "vision" in text or "vlm" in text:
        return "VISION_ERROR"
    if "model" in text or "ollama" in text:
        return "MODEL_ERROR"
    if "driver" in text:
        return "DRIVER_ERROR"
    if "source" in text or "not found" in text:
        return "SOURCE_ERROR"
    if "render" in text or "export" in text:
        return "RENDER_ERROR"
    return "UNKNOWN"


def _with_part_status(job: dict[str, Any], current: set[str], status: str) -> dict[str, Any]:
    parts = []
    for raw in job.get("parts") or []:
        part = dict(raw)
        if str(part.get("status") or "pending") in current:
            part["status"] = status
        parts.append(part)
    return {"parts": parts} if parts else {}


def _source_key(job: dict[str, Any]) -> str:
    source = str(job.get("source") or "").strip()
    if not source or source.startswith(("http://", "https://")):
        return source
    return str(Path(source).expanduser().resolve())


def _inside(path: Path, root: Path) -> bool:
    """Return whether a resolved path is contained by a managed root."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _unlink_file(path: Path, *, source: Path | None = None) -> int:
    """Delete one generated file, never the user's selected input file."""
    resolved = path.resolve()
    if source is not None and resolved == source.resolve():
        return 0
    if not resolved.is_file():
        return 0
    last_error: OSError | None = None
    # Windows keeps an MP4 handle open while it is previewed in Explorer,
    # Photos, or a browser <video>. Retrying avoids a false 500 during the
    # short interval in which the player releases the handle.
    for attempt in range(8):
        try:
            resolved.unlink()
            return 1
        except FileNotFoundError:
            return 0
        except PermissionError as exc:
            last_error = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32} and getattr(exc, "errno", None) not in {13, 16}:
                raise
            last_error = exc
        time.sleep(0.12 * (attempt + 1))
    raise ArtifactBusyError(
        f"Không thể xóa vì file đang được sử dụng: {resolved}. "
        "Hãy đóng video/Explorer rồi thử lại."
    ) from last_error
    return 0


def remove_job_artifacts(job: dict[str, Any]) -> int:
    """Delete artifacts owned by one queue job, never its external source.

    A queue row is not the source of truth for disk usage: Review also owns a
    cache root and an editor project directory. Only paths beneath known app
    roots are removed recursively; an output outside the app is one file.
    """
    source_text = str(job.get("source") or "").strip()
    source = None if source_text.startswith(("http://", "https://")) else Path(source_text).expanduser()
    removed = 0
    candidates = [str(job.get("output") or "")]
    candidates.extend(str(part.get("output") or "") for part in job.get("parts") or [])
    for raw in candidates:
        if raw:
            removed += _unlink_file(Path(raw), source=source)

    refs = job.get("cacheRefs") or {}
    cache_root = Path(str(refs.get("root") or "")) if refs.get("root") else None
    review_root = DATA / "review_cache"
    if (
        cache_root
        and cache_root.resolve() != review_root.resolve()
        and _inside(cache_root, review_root)
        and cache_root.is_dir()
    ):
        shutil.rmtree(cache_root)
        removed += 1

    project_id = str(job.get("projectId") or "").strip()
    if project_id:
        for base in (PUBLIC_DATA, DATA):
            project_root = safe_child(base, project_id)
            if project_root and project_root.is_dir():
                shutil.rmtree(project_root)
                removed += 1
    return removed


class QueueEngine:
    def __init__(self) -> None:
        self._pause_all = False
        self._active: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="unified-queue")
        store.mark_interrupted()
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        jobs = store.load_all()
        return {
            "jobs": jobs,
            "pauseAll": self._pause_all,
            "active": list(self._active),
            "diagnostics": diagnostics(),
        }

    def enqueue_many(
        self,
        *,
        job_type: str,
        sources: list[str],
        settings: dict[str, Any],
        recursive: bool = True,
        start_now: bool = True,
    ) -> list[dict[str, Any]]:
        files = scan_videos(sources, recursive=recursive)
        created: list[dict[str, Any]] = []
        now = time.time()
        dest_dir = str(settings.get("outputDir") or "")
        template = str(settings.get("naming") or "{name}_{type}")
        for src in files:
            job = {
                "id": uuid.uuid4().hex[:12],
                "type": job_type,
                "mode": "batch",
                "source": src,
                "settings_snapshot": dict(settings),
                # Batch selection creates an editable job first.  It only
                # consumes CPU/GPU after the user explicitly starts the batch.
                "status": "queued" if start_now else "paused",
                "stage": "queued" if start_now else "ready",
                "progress": 0.0,
                "checkpoints": [],
                "cacheRefs": {},
                "output": "",
                "outputName": output_name(Path(src), template, {**settings, "type": job_type}),
                "outputDir": dest_dir,
                "error": None,
                "errorType": None,
                "log": [],
                "projectId": None,
                "createdAt": now,
                "updatedAt": now,
            }
            store.insert(job)
            created.append(job)
        if start_now:
            self.kick()
        return created

    def enqueue_project_clone(self, *, project_id: str, source: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Put an existing editor project on the same durable queue as Batch.

        Interactive Clone used to spawn a private thread, so it was invisible
        in the Batch page and could compete with queued work.  This row keeps
        the project identity while using the unified scheduler/cancellation.
        """
        for existing in store.load_all():
            if existing.get("mode") == "project" and existing.get("projectId") == project_id and existing.get("status") in {"queued", "running"}:
                store.mutate(existing["id"], {"settings_snapshot": dict(settings), "source": source})
                return store.get(existing["id"]) or existing
        now = time.time()
        job = {
            "id": uuid.uuid4().hex[:12], "type": "clone", "mode": "project",
            "source": source, "projectId": project_id,
            "settings_snapshot": dict(settings), "status": "queued", "stage": "queued", "progress": 0.0,
            "checkpoints": [], "cacheRefs": {"projectId": project_id}, "output": "",
            "outputName": output_name(Path(source), "{name}_clone", settings),
            "outputDir": "", "error": None, "errorType": None, "log": [],
            "createdAt": now, "updatedAt": now,
        }
        store.insert(job)
        self.kick()
        return job

    def action(self, job_id: str, op: str) -> dict[str, Any]:
        job = store.get(job_id)
        if not job and op not in {"pause_all", "resume_all", "retry_failed", "clear_completed", "clear_logs"}:
            raise KeyError(job_id)
        if op == "pause":
            patch = {"status": "paused", "error": None, "errorType": None}
            patch.update(_with_part_status(job or {}, {"running"}, "paused"))
            store.mutate(job_id, patch, log="Đã dừng")
            request_cancel(job_id)
        elif op == "resume":
            arm_job(job_id)
            patch = {"status": "queued", "error": None, "errorType": None}
            patch.update(_with_part_status(job or {}, {"paused", "interrupted", "running"}, "pending"))
            store.mutate(job_id, patch, log="Tiếp tục")
            self.kick()
        elif op == "cancel":
            # Stop first: a status update alone must never leave FFmpeg, OCR,
            # TTS or a model worker consuming CPU/GPU while the UI says Cancelled.
            request_cancel(job_id)
            patch = {"status": "cancelled", "errorType": "CANCELLED"}
            patch.update(_with_part_status(job or {}, {"pending", "running", "paused", "interrupted"}, "cancelled"))
            store.mutate(job_id, patch, log="Đã gửi lệnh huỷ tới worker")
        elif op == "retry":
            arm_job(job_id)
            patch = {"status": "queued", "error": None, "errorType": None, "progress": 0}
            patch.update(_with_part_status(job or {}, {"cancelled", "failed", "paused", "interrupted"}, "pending"))
            store.mutate(job_id, patch, log="Thử lại")
            self.kick()
        elif op == "remove":
            request_cancel(job_id)
            with self._guard:
                active = job_id in self._active and self._active[job_id].is_alive()
            if active:
                # FFmpeg/model workers may still be writing: clean up in the
                # worker's finally block once cancellation reaches a checkpoint.
                store.mutate(job_id, {"status": "cancelled", "deleteWhenFinished": True}, log="Đang hủy và dọn file dự án")
            else:
                removed = remove_job_artifacts(job or {})
                append_log(f"[queue:{job_id}] removed {removed} owned artifact(s)")
                store.replace_all([j for j in store.load_all() if j.get("id") != job_id])
        elif op == "pause_all":
            self._pause_all = True
            for item in store.load_all():
                if item.get("status") in {"running", "queued"}:
                    patch = {"status": "paused", "error": None, "errorType": None}
                    patch.update(_with_part_status(item, {"running"}, "paused"))
                    store.mutate(item["id"], patch, log="Đã dừng")
                    request_cancel(str(item["id"]))
        elif op == "resume_all":
            self._pause_all = False
            for item in store.load_all():
                if item.get("status") in {"paused", "interrupted"}:
                    arm_job(str(item["id"]))
                    patch = {"status": "queued", "error": None, "errorType": None}
                    patch.update(_with_part_status(item, {"paused", "interrupted", "running"}, "pending"))
                    store.mutate(item["id"], patch, log="Tiếp tục")
            self.kick()
        elif op == "retry_failed":
            for item in store.load_all():
                if item.get("status") in {"failed", "interrupted"}:
                    arm_job(str(item["id"]))
                    store.mutate(item["id"], {"status": "queued", "error": None, "errorType": None})
            self.kick()
        elif op == "clear_completed":
            completed = [item for item in store.load_all() if item.get("status") == "done"]
            removed = sum(remove_job_artifacts(item) for item in completed)
            append_log(f"[queue] cleared {len(completed)} completed job(s), {removed} owned artifact(s)")
            store.replace_all([item for item in store.load_all() if item.get("status") != "done"])
        elif op == "clear_logs":
            rows = store.load_all()
            for item in rows:
                item["log"] = []
            store.replace_all(rows)
        return self.snapshot()

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=1.5)
            self._wake.clear()
            try:
                self._schedule()
            except Exception as exc:
                append_log(f"[queue] schedule error: {exc}")

    def _disk_ok(self, path: str) -> bool:
        try:
            target = Path(path) if path else Path.home()
            if not target.exists():
                target = target.parent if target.parent.exists() else Path.home()
            return shutil.disk_usage(str(target)).free >= DISK_PAUSE_BYTES
        except OSError:
            return True

    def _capacity(self) -> int:
        cap = gpu_job_cap(per_job_mb=1800, reserve_mb=800, hard_max=4)
        free = vram_free_mb(0)
        if free is not None and free < 1500:
            cap = min(cap, 1)
        return max(1, cap)

    def _schedule(self) -> None:
        from pipeline.core.license import can_start_job
        if not can_start_job(activation_required=False):
            for job in store.load_all():
                if job.get('status') == 'queued':
                    store.mutate(job['id'], {'status': 'paused', 'error': 'ACCESS_REQUIRED', 'errorType': 'ACCESS_REQUIRED'})
            return
        if self._pause_all:
            return
        with self._guard:
            live = {jid: th for jid, th in self._active.items() if th.is_alive()}
            self._active = live
            running = len(live)
        jobs = store.load_all()
        # Interrupted jobs wait for an explicit Resume (do not auto-queue —
        # that blocked settings edits and hid the continue button).
        cap = self._capacity()
        live_sources = {
            _source_key(item)
            for item in jobs
            if str(item.get("id")) in live and _source_key(item)
        }
        for job in jobs:
            if running >= cap:
                return
            if job.get("status") not in {"queued"}:
                continue
            dest = str(job.get("outputDir") or "")
            if not self._disk_ok(dest):
                store.mutate(job["id"], {
                    "status": "paused",
                    "error": "DISK_FULL: dung lượng đĩa thấp, tạm dừng job mới",
                    "errorType": "DISK_FULL",
                })
                continue
            jid = str(job["id"])
            if jid in live:
                continue
            source_key = _source_key(job)
            if source_key and source_key in live_sources:
                continue
            th = threading.Thread(target=self._run_job, args=(jid,), daemon=True, name=f"q-{jid}")
            with self._guard:
                self._active[jid] = th
            th.start()
            running += 1
            if source_key:
                live_sources.add(source_key)

    def _run_job(self, job_id: str) -> None:
        from pipeline.core.license import can_start_job, run_admitted
        if not can_start_job(activation_required=False):
            store.mutate(job_id, {"status": "paused", "error": "ACCESS_REQUIRED", "errorType": "ACCESS_REQUIRED"})
            with self._guard:
                self._active.pop(job_id, None)
            return
        return run_admitted(self._run_admitted_job, job_id)

    def _run_admitted_job(self, job_id: str) -> None:
        begin_job(job_id)
        set_job_context(job_id)
        job = store.get(job_id) or {}
        store.mutate(job_id, {"status": "running", "stage": "start", "progress": 0.01, "error": None},
                     log=f"Bắt đầu job {job.get('type') or '?'} · {Path(str(job.get('source') or '')).name or job.get('source')}")
        job = store.get(job_id) or job
        try:
            check_cancel(job_id)
            device = assign_device("clone_ai" if job.get("type") == "clone" else "vision")
            store.mutate(job_id, {"device": device, "stage": "assigned"}, log=f"GPU/device: {device}")
            if job.get("type") == "review":
                from pipeline.review.run import run_review_job
                result = run_review_job(job)
            elif job.get("mode") == "project":
                from pipeline.clone_run.headless import run_existing_project_clone_job
                result = run_existing_project_clone_job(job)
            else:
                from pipeline.clone_run.headless import run_clone_job
                result = run_clone_job(job)
            store.mutate(job_id, {
                "status": "done",
                "stage": "done",
                "progress": 1.0,
                "output": result.get("output") or "",
                "projectId": result.get("projectId"),
                "cacheRefs": result.get("cacheRefs") or {},
            }, log=f"Xong · {result.get('output') or 'no output'}")
        except Cancelled:
            if (store.get(job_id) or {}).get("status") in {"paused", "queued"}:
                store.mutate(job_id, {"error": None, "errorType": None}, log="Đã dừng tại checkpoint")
            else:
                store.mutate(job_id, {"status": "cancelled", "errorType": "CANCELLED"}, log="Đã huỷ")
        except Exception as exc:
            kind = classify_error(exc)
            append_log(f"[queue:{job_id}] {kind}: {exc}\n{traceback.format_exc()[-1500:]}")
            if kind == "GPU_OOM":
                store.mutate(job_id, {"status": "queued", "error": str(exc)[:800], "errorType": kind, "oomRetry": True},
                             log=f"LỖI GPU_OOM — xếp lại hàng đợi: {exc}")
                time.sleep(2)
                self.kick()
                return
            store.mutate(job_id, {
                "status": "failed",
                "error": str(exc)[:1200],
                "errorType": kind,
            }, log=f"LỖI {kind}: {exc}\n{traceback.format_exc()[-2500:]}")
        finally:
            set_job_context(None)
            with self._guard:
                self._active.pop(job_id, None)
            completed = store.get(job_id)
            if completed and completed.get("deleteWhenFinished"):
                try:
                    removed = remove_job_artifacts(completed)
                    append_log(f"[queue:{job_id}] removed {removed} owned artifact(s) after cancellation")
                    store.replace_all([row for row in store.load_all() if row.get("id") != job_id])
                except OSError as exc:
                    store.mutate(job_id, {"deleteWhenFinished": False}, log=f"Không thể dọn file: {exc}")
            self.kick()


def get_engine() -> QueueEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = QueueEngine()
        return _engine


def enqueue(
    job_type: str,
    sources: list[str],
    settings: dict[str, Any],
    recursive: bool = True,
    start_now: bool = True,
) -> list[dict[str, Any]]:
    return get_engine().enqueue_many(
        job_type=job_type,
        sources=sources,
        settings=settings,
        recursive=recursive,
        start_now=start_now,
    )


def enqueue_project_clone(project_id: str, source: str, settings: dict[str, Any]) -> dict[str, Any]:
    return get_engine().enqueue_project_clone(project_id=project_id, source=source, settings=settings)


def list_jobs() -> dict[str, Any]:
    return get_engine().snapshot()


def job_action(job_id: str, op: str) -> dict[str, Any]:
    return get_engine().action(job_id, op)
