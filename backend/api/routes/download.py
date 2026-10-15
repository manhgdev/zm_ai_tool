"""Download Video API — yt-dlp jobs."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.deps import Settings
from pipeline.core.media import ffprobe_duration
from pipeline.core.project import (
    ensure_layout,
    find_project_by_fp,
    load_meta,
    save_meta,
    set_status,
    video_fingerprint,
)
from pipeline.download.ytdlp_jobs import (
    cancel_job,
    clear_job_logs,
    clear_done_jobs,
    delete_job,
    download_root_info,
    ensure_download_dirs,
    get_job,
    get_job_file,
    list_jobs,
    reset_download_root,
    reveal_download_root,
    reveal_job_file,
    set_download_root,
    start_jobs,
)
from pipeline.subtitles import subtitle_segments

router = APIRouter()


class DownloadStartIn(BaseModel):
    url: str | None = None
    urls: list[str] | None = None
    quality: str = "1080"
    format: str = "mp4"
    writeSubs: bool = False
    writeInfoJson: bool = False
    writeThumbnail: bool = False
    mergeAv: bool = True
    preferFreeFormats: bool = False
    folderBySource: bool = False


class DownloadRootIn(BaseModel):
    path: str = Field(..., min_length=1)


def _copy_job_subtitles(root: Path, job_dir: Path) -> list[dict[str, str]]:
    """Copy SRT sidecars from a completed download into a Clone Video project."""
    if not job_dir.is_dir():
        return []
    subtitle_dir = root / "subtitles"
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, str]] = []
    for source in sorted(job_dir.glob("*.srt")):
        target = subtitle_dir / source.name
        shutil.copy2(source, target)
        copied.append({"name": source.name, "label": f"Từ Download · {source.name}", "origin": "download"})
    return copied


@router.get("/api/download/root")
def api_download_root():
    """Đường dẫn thư mục lưu — tạo nếu chưa có."""
    return download_root_info()


@router.post("/api/download/root")
def api_download_root_set(body: DownloadRootIn):
    try:
        return set_download_root(body.path)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/download/root/reset")
def api_download_root_reset():
    return reset_download_root()


@router.post("/api/download/root/reveal")
def api_download_root_reveal():
    try:
        return reveal_download_root()
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/download/jobs")
def api_download_list():
    ensure_download_dirs()
    return list_jobs()


@router.post("/api/download/jobs")
def api_download_start(body: DownloadStartIn):
    ensure_download_dirs()
    try:
        jobs = start_jobs(
            url=body.url,
            quality=body.quality,
            urls=body.urls,
            format=body.format,
            writeSubs=body.writeSubs,
            writeInfoJson=body.writeInfoJson,
            writeThumbnail=body.writeThumbnail,
            mergeAv=body.mergeAv,
            preferFreeFormats=body.preferFreeFormats,
            folderBySource=body.folderBySource,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if len(jobs) == 1:
        return jobs[0]
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/api/download/jobs/clear-done")
def api_download_clear_done():
    n = clear_done_jobs()
    return {"ok": True, "removed": n}


@router.delete("/api/download/logs")
def api_download_clear_logs():
    """Clear diagnostics without removing queued jobs or downloaded files."""
    return {"ok": True, "cleared": clear_job_logs()}


@router.get("/api/download/jobs/{job_id}")
def api_download_get(job_id: str):
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "Job không tồn tại")
    return j


@router.post("/api/download/jobs/{job_id}/cancel")
def api_download_cancel(job_id: str):
    if not cancel_job(job_id):
        raise HTTPException(404, "Job không tồn tại")
    return {"ok": True}


@router.delete("/api/download/jobs/{job_id}")
def api_download_delete(job_id: str):
    """Xóa job khỏi list + thư mục file trên disk."""
    if not delete_job(job_id, delete_files=True):
        raise HTTPException(404, "Job không tồn tại")
    return {"ok": True}


@router.get("/api/download/jobs/{job_id}/file")
def api_download_file(job_id: str):
    path = get_job_file(job_id)
    if not path:
        raise HTTPException(404, "Chưa có file")
    media = "audio/mpeg" if path.suffix.lower() == ".mp3" else "application/octet-stream"
    if path.suffix.lower() in {".mp4", ".webm", ".mkv"}:
        media = (
            f"video/{path.suffix.lower().lstrip('.')}"
            if path.suffix.lower() != ".mkv"
            else "video/x-matroska"
        )
    return FileResponse(path, filename=path.name, media_type=media)


@router.post("/api/download/jobs/{job_id}/open")
def api_download_open(job_id: str):
    """Mở / hiện file trong Explorer (local backend)."""
    try:
        return reveal_job_file(job_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/download/jobs/{job_id}/to-project")
def api_download_to_project(job_id: str):
    """Copy file tải xong → project Clone Video (giống /api/upload)."""
    path = get_job_file(job_id)
    if not path:
        raise HTTPException(404, "Chưa có file — job chưa xong")
    ext = path.suffix.lower() or ".mp4"
    if ext in {".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac"}:
        raise HTTPException(400, "File audio — không dùng được cho Clone Video")
    if ext not in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts"}:
        # vẫn thử nếu có video container lạ
        pass

    try:
        fp = video_fingerprint(path)
    except Exception as e:
        raise HTTPException(400, f"Không đọc được video: {e}") from e

    existing = find_project_by_fp(fp)
    if existing:
        meta = load_meta(existing)
        root = ensure_layout(existing)
        job = get_job(job_id) or {}
        downloaded_sources = _copy_job_subtitles(root, path.parent)
        if downloaded_sources:
            old_sources = [
                source for source in meta.get("subtitleSources") or []
                if isinstance(source, dict) and source.get("origin") != "download"
            ]
            selected = downloaded_sources[0]["name"]
            settings = dict(meta.get("settings") or {})
            settings.update({"engine": "subtitle", "subtitleSource": selected, "matchDuration": "none"})
            meta.update({
                "subtitleSources": downloaded_sources + old_sources,
                "segments": subtitle_segments(root / "subtitles" / selected),
                "settings": settings,
            })
            save_meta(existing, meta)
        set_status(
            existing,
            step=meta.get("status", {}).get("step") or "video",
            progress=100,
            message="Video + phụ đề đã sẵn sàng (cache)" if downloaded_sources else "Video đã có sẵn (cache)",
            running=False,
            error=None,
        )
        return {
            "projectId": existing,
            "videoUrl": f"/api/projects/{existing}/video",
            "duration": meta.get("duration") or ffprobe_duration(Path(meta["videoPath"])),
            "cached": True,
            "segments": meta.get("segments") or [],
            "settings": meta.get("settings") or {},
            "fromDownload": job_id,
            "subtitleSources": meta.get("subtitleSources") or [],
        }

    project_id = uuid.uuid4().hex[:12]
    root = ensure_layout(project_id)
    dest = root / f"source{ext if ext else '.mp4'}"
    shutil.copy2(path, dest)
    duration = ffprobe_duration(dest)
    job = get_job(job_id) or {}
    subtitle_dir = root / "subtitles"
    subtitle_sources = _copy_job_subtitles(root, path.parent)
    subtitle_source = subtitle_sources[0]["name"] if subtitle_sources else ""
    init_settings = Settings().model_dump()
    if subtitle_source:
        init_settings.update({"engine": "subtitle", "subtitleSource": subtitle_source, "matchDuration": "none"})
        initial_segments = subtitle_segments(subtitle_dir / subtitle_source)
    else:
        initial_segments = []
    save_meta(
        project_id,
        {
            "videoPath": str(dest),
            "duration": duration,
            "sourceFp": fp,
            "segments": initial_segments,
            "cache": {},
            "settings": init_settings,
            "subtitleSources": subtitle_sources,
            "sourceDownloadJob": job_id,
            "sourceUrl": job.get("url"),
            "status": {
                "step": "video",
                "progress": 100,
                "message": "Video + phụ đề sẵn sàng (từ Download)" if subtitle_source else "Video sẵn sàng (từ Download)",
                "running": False,
            },
        },
    )
    return {
        "projectId": project_id,
        "videoUrl": f"/api/projects/{project_id}/video",
        "duration": duration,
        "cached": False,
        "segments": initial_segments,
        "settings": init_settings,
        "subtitleSources": subtitle_sources,
        "fromDownload": job_id,
    }
