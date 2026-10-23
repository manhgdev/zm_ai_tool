"""Unified job queue API."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
import subprocess
import sys
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline.core.config import DATA, safe_child
from pipeline.core.output_paths import selected_or_default
from pipeline.queue.engine import ArtifactBusyError, enqueue, get_engine, job_action, list_jobs
from pipeline.queue.store import get as get_job, mutate

router = APIRouter()


class EnqueueIn(BaseModel):
    type: str = "clone"
    sources: list[str] = []
    settings: dict[str, Any] = {}
    recursive: bool = True
    start_now: bool = True


class QueueActionIn(BaseModel):
    op: str


class StartQueueJobsIn(BaseModel):
    ids: list[str]


class JobSettingsIn(BaseModel):
    settings: dict[str, Any]


def resolve_job_file(job: dict[str, Any], part: int | None = None) -> Path:
    if part is not None:
        for item in job.get("parts") or []:
            if int(item.get("index") or 0) == int(part):
                path = Path(str(item.get("output") or ""))
                if path.is_file():
                    return path
                raise HTTPException(404, "Phần này chưa có file")
        raise HTTPException(404, "Không thấy phần")
    out = Path(str(job.get("output") or ""))
    if out.is_file():
        return out
    for item in reversed(list(job.get("parts") or [])):
        path = Path(str(item.get("output") or ""))
        if path.is_file():
            return path
    raise HTTPException(404, "Chưa có file video")


def resolve_job_thumbnail_source(job: dict[str, Any]) -> Path:
    """Use the render when present, otherwise a completed Clone project's source."""
    try:
        return resolve_job_file(job)
    except HTTPException:
        source = Path(str(job.get("source") or "")).expanduser()
        if source.is_file():
            return source
    raise FileNotFoundError(str(job.get("id") or "queue job"))


def _existing_thumbnail(video: Path) -> Path | None:
    """Prefer a thumbnail already emitted next to an export before decoding it again."""
    candidates = (
        video.with_suffix(".jpg"),
        video.with_suffix(".jpeg"),
        video.with_name(f"{video.stem}_thumbnail.jpg"),
        video.with_name(f"{video.stem}.thumb.jpg"),
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def ensure_job_thumbnail(job_id: str, job: dict[str, Any]) -> Path:
    """Return a cached still frame for a completed Clone or Review queue job."""
    output = resolve_job_thumbnail_source(job)
    existing = _existing_thumbnail(output)
    if existing:
        return existing
    cache_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    thumbnail = DATA / "queue_thumbnails" / f"{cache_key}.jpg"
    if thumbnail.is_file() and thumbnail.stat().st_mtime >= output.stat().st_mtime:
        return thumbnail
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    temp = thumbnail.with_name(f"{cache_key}.{uuid.uuid4().hex}.jpg")
    try:
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1",
                   "-i", str(output), "-frames:v", "1", "-vf", "scale=320:-2",
                   "-q:v", "3", str(temp)]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except subprocess.SubprocessError:
            temp.unlink(missing_ok=True)
            command[command.index("1")] = "0"
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        temp.replace(thumbnail)
        return thumbnail
    finally:
        temp.unlink(missing_ok=True)


def _delete_review_part_cache(job: dict[str, Any], index: int) -> None:
    """Remove large intermediate files for one deleted Review part."""
    refs = job.get("cacheRefs") or {}
    root_raw = str(refs.get("root") or "")
    run_id = str(refs.get("run") or job.get("runId") or "")
    if not root_raw or not run_id:
        return
    try:
        root = Path(root_raw).resolve()
        review_cache = (DATA / "review_cache").resolve()
        root.relative_to(review_cache)
    except (OSError, ValueError):
        return
    if root == review_cache:
        return
    runs = root / "runs"
    run = safe_child(runs, run_id)
    if not run or not run.is_dir():
        return
    for stem, suffix in (
        ("raw_part", ".mp4"),
        ("burned_part", ".mp4"),
        ("part", ".mp4"),
        ("script", ".json"),
        ("plan", ".json"),
        ("voice", ".json"),
        ("final", ".json"),
    ):
        candidate = run / f"{stem}_{index:02d}{suffix}"
        if candidate.is_file():
            candidate.unlink()


@router.get("/api/queue")
def api_queue():
    get_engine()
    return list_jobs()


@router.post("/api/queue")
def api_queue_enqueue(body: EnqueueIn):
    if body.type not in {"clone", "review"}:
        raise HTTPException(422, "type phải là clone hoặc review")
    if not body.sources:
        raise HTTPException(422, "Thiếu nguồn video")
    jobs = enqueue(body.type, body.sources, body.settings, recursive=body.recursive, start_now=body.start_now)
    return {"ok": True, "jobs": jobs}


@router.post("/api/queue/start")
def api_queue_start(body: StartQueueJobsIn):
    ids = [uid for uid in dict.fromkeys(body.ids) if (get_job(uid) or {}).get("status") in {"paused", "interrupted"}]
    for uid in ids:
        job_action(uid, "resume")
    return {"ok": True, "ids": ids}


@router.post("/api/queue/action")
def api_queue_global(body: QueueActionIn):
    if body.op not in {"pause_all", "resume_all", "retry_failed", "clear_completed", "clear_logs"}:
        raise HTTPException(422, "Lệnh không hợp lệ")
    return job_action("*", body.op)


@router.get("/api/queue/{job_id}/file")
def api_queue_file(job_id: str, part: int | None = None, download: int = 0):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Không thấy job")
    path = resolve_job_file(job, part)
    name = path.name if part is None else f"part_{part:02d}.mp4"
    if download:
        return FileResponse(path, filename=name, media_type="video/mp4")
    return FileResponse(path, media_type="video/mp4", content_disposition_type="inline")


@router.get("/api/queue/{job_id}/thumbnail")
def api_queue_thumbnail(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Không thấy job")
    try:
        thumbnail = ensure_job_thumbnail(job_id, job)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        raise HTTPException(404, "Không tạo được ảnh xem trước") from None
    return FileResponse(thumbnail, media_type="image/jpeg")


@router.post("/api/queue/{job_id}/reveal")
def api_queue_reveal(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Không thấy job")
    try:
        path = resolve_job_file(job)
        folder = path.parent
    except HTTPException:
        tab = "film" if str(job.get("type") or "") == "review" else "video-clone"
        folder = selected_or_default(tab, str(job.get("outputDir") or ""))
        path = None
    try:
        if sys.platform == "win32" and path:
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(folder)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if path else ["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        raise HTTPException(500, f"Không mở được thư mục: {exc}") from exc
    return {"ok": True, "path": str(path or folder)}


@router.delete("/api/queue/{job_id}/parts/{index}")
def api_queue_delete_part(job_id: str, index: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Không thấy job")
    parts = list(job.get("parts") or [])
    found = False
    for item in parts:
        if int(item.get("index") or 0) != int(index):
            continue
        found = True
        path = Path(str(item.get("output") or ""))
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                raise HTTPException(500, str(exc)) from exc
        try:
            _delete_review_part_cache(job, int(index))
        except OSError as exc:
            raise HTTPException(500, str(exc)) from exc
        item["status"] = "cancelled"
        item["output"] = ""
    if not found:
        raise HTTPException(404, "Không thấy phần")
    mutate(job_id, {"parts": parts})
    return list_jobs()


@router.post("/api/queue/{job_id}/action")
def api_queue_job(job_id: str, body: QueueActionIn):
    try:
        return job_action(job_id, body.op)
    except ArtifactBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError:
        raise HTTPException(404, "Không thấy job") from None


@router.patch("/api/queue/{job_id}/settings")
def api_queue_settings(job_id: str, body: JobSettingsIn):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Không thấy job")
    if job.get("status") == "running":
        raise HTTPException(422, "Không thể đổi cài đặt khi job đang chạy")
    settings = dict(body.settings)
    source = str(settings.get("source") or job.get("source") or "").strip()
    if not source:
        raise HTTPException(422, "Thiếu nguồn video")
    mutate(job_id, {
        "source": source,
        "settings_snapshot": settings,
        "outputDir": str(settings.get("outputDir") or job.get("outputDir") or ""),
    })
    return list_jobs()
