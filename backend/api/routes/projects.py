"""Domain API routes."""
from __future__ import annotations

import json
import math
import re
import shutil
import threading
import uuid
import mimetypes
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.deps import (
    AppConfigIn,
    CloneRenameIn,
    CompoundClipIn,
    ExportPayload,
    PreviewTtsIn,
    RebakeSpeedIn,
    RetranslateIn,
    SEG_PRESERVE,
    SegmentIn,
    Settings,
    StudioSynthIn,
    TextOverlayIn,
    VoiceBulkMoveIn,
    VoicePatchIn,
    require_meta,
    validate_overlay,
    validate_segment_editor_fields,
)
from api.job_spawn import spawn
from api.video_serve import serve_video_file
from pipeline.core.project import apply_meta_patch, resolve_project_video
from pipeline.core.config import DATA
from pipeline.core.media import ffprobe_duration
from pipeline.core.project import (
    ensure_layout,
    find_project_by_fp,
    load_meta,
    save_meta,
    set_status,
    video_fingerprint,
)
from pipeline.core.jobs import Cancelled, arm_job, clear_job
from pipeline.core.media import meta_baked_speed, meta_has_user_bake
from pipeline.subtitles import subtitle_segments

router = APIRouter()


def _subtitle_file(project_id: str, name: str) -> Path:
    safe = Path(name or "").name
    if not safe or Path(safe).suffix.lower() != ".srt":
        raise HTTPException(422, "Chỉ hỗ trợ file phụ đề .srt")
    return ensure_layout(project_id) / "subtitles" / safe


_MEDIA_ASSET_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".srt", ".cube",
}


def _media_asset_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".cube": return "lut"
    if ext == ".srt": return "srt"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}: return "image"
    if ext in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}: return "audio"
    return "video"


@router.get("/api/projects/{project_id}/assets")
def api_project_assets(project_id: str):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    return {"items": meta.get("mediaAssets") or []}


@router.get("/api/projects/{project_id}/media-timeline")
def api_media_timeline(project_id: str):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    return {"items": meta.get("mediaTimeline") or []}


@router.put("/api/projects/{project_id}/media-timeline")
def api_replace_media_timeline(project_id: str, body: list[dict[str, Any]] = Body(...)):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    assets = {str(item.get("id")) for item in meta.get("mediaAssets") or [] if isinstance(item, dict)}
    cleaned = []
    for clip in body:
        if not isinstance(clip, dict) or str(clip.get("assetId") or "") not in assets:
            raise HTTPException(422, "Timeline chứa asset không tồn tại")
        start, end = float(clip.get("start") or 0), float(clip.get("end") or 0)
        if start < 0 or end <= start: raise HTTPException(422, "Khoảng clip không hợp lệ")
        cleaned.append({**clip, "start": start, "end": end})
    meta["mediaTimeline"] = cleaned
    save_meta(project_id, meta)
    return {"items": cleaned}


@router.post("/api/projects/{project_id}/assets")
async def api_upload_project_asset(project_id: str, file: UploadFile = File(...)):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if not name or ext not in _MEDIA_ASSET_EXTS:
        raise HTTPException(422, "Chỉ hỗ trợ video, audio, ảnh, SRT hoặc LUT .cube")
    asset_id = uuid.uuid4().hex[:12]
    folder = ensure_layout(project_id) / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{asset_id}{ext}"
    target = folder / stored
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    kind = _media_asset_kind(name)
    item: dict[str, Any] = {"id": asset_id, "name": name, "kind": kind, "file": stored, "mime": file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"}
    if kind in {"video", "audio"}:
        item["duration"] = max(0.0, float(ffprobe_duration(target) or 0))
    if kind == "srt":
        try: item["cueCount"] = len(subtitle_segments(target))
        except OSError: item["cueCount"] = 0
    assets = [x for x in meta.get("mediaAssets") or [] if isinstance(x, dict)]
    assets.append(item)
    meta["mediaAssets"] = assets
    save_meta(project_id, meta)
    return {"item": item}


@router.get("/api/projects/{project_id}/assets/{asset_id}/file")
def api_project_asset_file(project_id: str, asset_id: str):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    item = next((x for x in meta.get("mediaAssets") or [] if x.get("id") == asset_id), None)
    path = ensure_layout(project_id) / "assets" / str((item or {}).get("file") or "")
    if not item or not path.is_file(): raise HTTPException(404)
    return FileResponse(path, media_type=item.get("mime"), filename=item.get("name"))


@router.get("/api/projects/{project_id}/assets/{asset_id}/thumbnail")
async def api_project_asset_thumbnail(project_id: str, asset_id: str):
    import anyio
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    item = next((x for x in meta.get("mediaAssets") or [] if x.get("id") == asset_id), None)
    src = ensure_layout(project_id) / "assets" / str((item or {}).get("file") or "")
    if not item or not src.is_file(): raise HTTPException(404)
    if item.get("kind") == "image":
        return FileResponse(src, media_type=item.get("mime"))
    if item.get("kind") != "video": raise HTTPException(404)
    thumb = ensure_layout(project_id) / "assets" / f"{asset_id}.thumb.jpg"
    if not thumb.is_file() or thumb.stat().st_mtime < src.stat().st_mtime:
        temp = thumb.with_suffix(".tmp.jpg")
        def _make_thumb() -> None:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", "0.5", "-i", str(src), "-frames:v", "1",
                 "-vf", "scale=320:-2", str(temp)],
                check=True, timeout=45,
            )
            temp.replace(thumb)
        try:
            await anyio.to_thread.run_sync(_make_thumb)
        except (OSError, subprocess.SubprocessError):
            temp.unlink(missing_ok=True)
            raise HTTPException(422, "Không tạo được thumbnail video") from None
    return FileResponse(thumb, media_type="image/jpeg")



@router.delete("/api/projects/{project_id}/assets/{asset_id}")
def api_delete_project_asset(project_id: str, asset_id: str):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    assets = [x for x in meta.get("mediaAssets") or [] if isinstance(x, dict)]
    item = next((x for x in assets if x.get("id") == asset_id), None)
    if not item: raise HTTPException(404)
    # Timeline references are intentionally protected before destructive removal.
    if any(str(c.get("assetId")) == asset_id for c in meta.get("mediaTimeline") or [] if isinstance(c, dict)):
        raise HTTPException(409, "Asset đang được dùng trên timeline")
    (ensure_layout(project_id) / "assets" / str(item.get("file") or "")).unlink(missing_ok=True)
    meta["mediaAssets"] = [x for x in assets if x.get("id") != asset_id]
    save_meta(project_id, meta)
    return {"ok": True}


@router.post("/api/projects/{project_id}/assets/{asset_id}/apply-srt")
def api_apply_project_asset_srt(project_id: str, asset_id: str):
    meta = load_meta(project_id)
    if not meta: raise HTTPException(404)
    item = next((x for x in meta.get("mediaAssets") or [] if x.get("id") == asset_id and x.get("kind") == "srt"), None)
    path = ensure_layout(project_id) / "assets" / str((item or {}).get("file") or "")
    if not item or not path.is_file(): raise HTTPException(404)
    segments = subtitle_segments(path)
    if not segments: raise HTTPException(422, "File SRT không có cue hợp lệ")
    settings = dict(meta.get("settings") or {})
    settings.update({"engine": "subtitle", "subtitleSource": str(item.get("name") or ""), "matchDuration": "none"})
    meta["segments"], meta["settings"] = segments, settings
    save_meta(project_id, meta)
    return {"segments": segments, "settings": settings}


@router.get("/api/projects/{project_id}/subtitles")
def api_subtitles(project_id: str):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    folder = ensure_layout(project_id) / "subtitles"
    saved = {str(x.get("name")): x for x in meta.get("subtitleSources") or [] if isinstance(x, dict)}
    items = [
        {
            "name": p.name,
            "label": str(saved.get(p.name, {}).get("label") or f"File đã nhập · {p.name}"),
        }
        for p in sorted(folder.glob("*.srt"))
    ]
    return {"items": items, "active": str((meta.get("settings") or {}).get("subtitleSource") or "")}


@router.post("/api/projects/{project_id}/subtitles")
async def api_upload_subtitle(project_id: str, file: UploadFile = File(...)):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    target = _subtitle_file(project_id, file.filename or "")
    target.parent.mkdir(exist_ok=True)
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        cues = subtitle_segments(target)
    except OSError as e:
        target.unlink(missing_ok=True)
        raise HTTPException(422, f"Không đọc được file phụ đề: {e}") from e
    if not cues:
        target.unlink(missing_ok=True)
        raise HTTPException(422, "File SRT không có cue hợp lệ")
    sources = [x for x in meta.get("subtitleSources") or [] if x.get("name") != target.name]
    sources.append({"name": target.name, "label": f"File đã nhập · {target.name}", "origin": "manual"})
    meta["subtitleSources"] = sources
    save_meta(project_id, meta)
    return {"name": target.name, "items": sources}


@router.post("/api/projects/{project_id}/subtitles/{name}/apply")
def api_apply_subtitle(project_id: str, name: str):
    """Apply a selected SRT immediately, so the editor never shows the old file's cues."""
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    source = _subtitle_file(project_id, name)
    if not source.is_file():
        raise HTTPException(404, "Không tìm thấy file phụ đề")
    segments = subtitle_segments(source)
    if not segments:
        raise HTTPException(422, "File SRT không có cue hợp lệ")
    settings = dict(meta.get("settings") or {})
    settings.update({"engine": "subtitle", "subtitleSource": source.name, "matchDuration": "none"})
    meta["segments"] = segments
    meta["settings"] = settings
    meta["cache"] = {}
    meta["translationCaches"] = {}
    set_status(project_id, step="asr", progress=100, message=f"Đã nạp {len(segments)} câu từ phụ đề SRT", running=False, error=None)
    save_meta(project_id, meta)
    return {"segments": segments, "settings": settings}

# Aliases matching original routes_all names
_spawn = spawn
_serve_video_file = serve_video_file
_validate_overlay = validate_overlay
_validate_segment_editor_fields = validate_segment_editor_fields
_SEG_PRESERVE = SEG_PRESERVE


@router.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "Thiếu file")
    ext = Path(file.filename).suffix or ".mp4"
    tmp = DATA / f"_upload_{uuid.uuid4().hex}{ext}"
    DATA.mkdir(exist_ok=True)
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    fp = video_fingerprint(tmp)
    existing = find_project_by_fp(fp)
    if existing:
        tmp.unlink(missing_ok=True)
        meta = load_meta(existing)
        ensure_layout(existing)
        set_status(
            existing,
            step=meta.get("status", {}).get("step") or "video",
            progress=100,
            message="Video đã có sẵn (cache)",
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
        }

    project_id = uuid.uuid4().hex[:12]
    root = ensure_layout(project_id)
    dest = root / f"source{ext}"
    tmp.replace(dest)
    duration = ffprobe_duration(dest)
    init_settings = Settings().model_dump()
    from pipeline.core.media import ensure_project_initial_playback_rate

    init_meta: dict = {
        "videoPath": str(dest),
        "duration": duration,
        "sourceFp": fp,
        "segments": [],
        "cache": {},
        "settings": init_settings,
        "status": {
            "step": "video",
            "progress": 100,
            "message": "Video sẵn sàng",
            "running": False,
        },
    }
    ensure_project_initial_playback_rate(init_meta, init_settings)
    save_meta(project_id, init_meta)
    return {
        "projectId": project_id,
        "videoUrl": f"/api/projects/{project_id}/video",
        "duration": duration,
        "cached": False,
        "segments": [],
        "settings": init_settings,
    }


@router.get("/api/projects/{project_id}/video")
@router.get("/api/projects/{project_id}/video/{_rev}")
def api_video(project_id: str, request: Request, _rev: str | None = None):
    # {_rev} = cache-bust từ frontend (…/video/1) — bỏ qua
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    return _serve_video_file(resolve_project_video(meta, project_id), request)


def ensure_project_thumbnail(project_id: str, meta: dict[str, Any]) -> Path:
    """Create one cached still frame for the current project video."""
    source = resolve_project_video(meta, project_id)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    thumbnail = ensure_layout(project_id) / "cache" / "input_thumbnail.jpg"
    if thumbnail.is_file() and thumbnail.stat().st_mtime >= source.stat().st_mtime:
        return thumbnail
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    temp = thumbnail.with_name(f"{thumbnail.stem}.{uuid.uuid4().hex}.jpg")
    try:
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "0.5", "-i", str(source), "-frames:v", "1",
            "-vf", "scale=640:-2", "-q:v", "3", str(temp),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except subprocess.SubprocessError:
            # Very short clips may not have a frame at 0.5s; use the first frame.
            temp.unlink(missing_ok=True)
            command[command.index("0.5")] = "0"
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        temp.replace(thumbnail)
        return thumbnail
    finally:
        temp.unlink(missing_ok=True)


@router.get("/api/projects/{project_id}/thumbnail")
async def api_project_thumbnail(project_id: str):
    import anyio

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    try:
        thumbnail = await anyio.to_thread.run_sync(lambda: ensure_project_thumbnail(project_id, meta))
    except (OSError, subprocess.SubprocessError, KeyError):
        raise HTTPException(422, "Không tạo được thumbnail video") from None
    return FileResponse(thumbnail, media_type="image/jpeg")


@router.post("/api/projects/{project_id}/rebake-speed")
def api_rebake_speed(project_id: str, body: RebakeSpeedIn):
    """Bake tốc độ preview từ file 1× + remap timeline theo tốc độ mới."""
    from pipeline.core.media import (
        clamp_playback_speed,
        ensure_playback_speed,
        meta_baked_speed,
        preview_1x_path,
        remap_timeline_for_speed_change,
        speed_cache_tag,
    )

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)

    # Chống request cũ ghi đè: chỉ revision >= speedRevision đã lưu mới chạy
    req_rev = body.speedRevision
    if req_rev is not None:
        saved_rev = int(meta.get("speedRevision") or 0)
        if int(req_rev) < saved_rev:
            return {
                "ok": True,
                "ignored": True,
                "reason": "STALE_SPEED_REVISION",
                "speedRevision": saved_rev,
                "bakedSpeed": meta_baked_speed(meta),
                "bakedPreferVideo": bool(meta.get("bakedPreferVideo")),
                "hasBakedSpeed": bool(meta.get("bakedSpeed") is not None),
                "workClipSec": float(meta.get("workDuration") or meta.get("previewSec") or 0),
                "duration": float(meta.get("workDuration") or meta.get("duration") or 0),
                "segments": meta.get("segments") or [],
                "overlays": meta.get("overlays") or [],
                "videoUrl": f"/api/projects/{project_id}/video",
                "timeScale": 1.0,
                "prevBakedSpeed": meta_baked_speed(meta),
            }
        meta["speedRevision"] = int(req_rev)
        save_meta(project_id, meta)

    # Job khác (export/dịch) — không chặn nếu chính bake tốc độ đang chạy sẽ clear
    st_run = (meta.get("status") or {}).get("running")
    st_step = str((meta.get("status") or {}).get("step") or "")
    if st_run and st_step not in ("", "video", "idle"):
        raise HTTPException(409, "Đang có job chạy — đợi xong rồi áp dụng tốc độ")

    speed = clamp_playback_speed(body.speed)
    old = meta_baked_speed(meta)
    base = preview_1x_path(project_id, meta)
    if not base.is_file():
        raise HTTPException(404, "Chưa có preview 1× — chạy Dịch trước")

    cache = ensure_layout(project_id) / "cache"
    preview_sec = max(0, int(meta.get("previewSec") or 0))
    arm_job(project_id)
    try:
        set_status(
            project_id,
            step="video",
            progress=8,
            message=f"Chuẩn bị tốc độ {speed:.2f}× (từ {old:.2f}×)…",
            running=True,
            error=None,
        )
        # Remap timeline TRƯỚC bake file — scale từ baseline 1×, không nhân chồng
        if not body.skipRemap:
            set_status(
                project_id,
                step="video",
                progress=18,
                message="Đang scale timeline / caption / TTS…",
                running=True,
                error=None,
            )
            remap_timeline_for_speed_change(meta, old, speed)

        if abs(speed - 1.0) < 0.001:
            set_status(
                project_id,
                step="video",
                progress=70,
                message="Khóa file 1.00×…",
                running=True,
                error=None,
            )
            work = base
            meta.pop("bakedPreferVideo", None)
            meta["bakedSpeed"] = 1.0
            meta.pop("workDuration", None)
        else:
            set_status(
                project_id,
                step="video",
                progress=35,
                message=f"Đang bake video {speed:.2f}× (ffmpeg)…",
                running=True,
                error=None,
            )
            tag = speed_cache_tag(speed)
            dest = (
                cache / f"preview_{preview_sec}_{tag}.mp4"
                if preview_sec > 0
                else cache / f"source_{tag}.mp4"
            )
            work = ensure_playback_speed(base, dest, speed, project_id=project_id)
            set_status(
                project_id,
                step="video",
                progress=85,
                message="Đang đo độ dài file bake…",
                running=True,
                error=None,
            )
            meta["bakedPreferVideo"] = True
            meta["bakedSpeed"] = speed
            meta["workDuration"] = float(ffprobe_duration(work) or 0)
        meta["workVideo"] = str(work.resolve())
        # FE timeline sau Áp dụng = đồng hồ display — BE export tin mốc start/end
        meta["timelineClock"] = "display"
        # Trước khi ghi: request mới hơn đã claim revision → bỏ kết quả cũ
        if req_rev is not None:
            latest = load_meta(project_id) or {}
            if int(req_rev) < int(latest.get("speedRevision") or 0):
                set_status(
                    project_id,
                    step="video",
                    progress=100,
                    message="Bỏ qua bake cũ (đã có tốc độ mới hơn)",
                    running=False,
                    error=None,
                )
                return {
                    "ok": True,
                    "ignored": True,
                    "reason": "STALE_SPEED_REVISION",
                    "speedRevision": int(latest.get("speedRevision") or 0),
                    "bakedSpeed": meta_baked_speed(latest),
                    "bakedPreferVideo": bool(latest.get("bakedPreferVideo")),
                    "hasBakedSpeed": True,
                    "workClipSec": float(latest.get("workDuration") or latest.get("duration") or 0),
                    "duration": float(latest.get("workDuration") or latest.get("duration") or 0),
                    "segments": latest.get("segments") or [],
                    "overlays": latest.get("overlays") or [],
                    "videoUrl": f"/api/projects/{project_id}/video",
                    "timeScale": 1.0,
                    "prevBakedSpeed": old,
                }
            meta["speedRevision"] = int(req_rev)
        # Patch theo key (không ghi đè cả snapshot): bake chạy vài phút, PUT
        # segments/overlays hoặc set_status trong lúc đó không được bị nuốt.
        _rebake_keys = (
            "segments", "overlays", "timelineBaseline", "bakedSpeed",
            "workVideo", "timelineClock", "speedRevision", "duration",
            "workDuration", "bakedPreferVideo",
        )
        apply_meta_patch(
            project_id,
            {k: meta[k] for k in _rebake_keys if k in meta},
            remove=tuple(k for k in ("bakedPreferVideo", "workDuration") if k not in meta),
        )
    except Cancelled as e:
        set_status(
            project_id,
            step="video",
            progress=0,
            message="Đã huỷ áp dụng tốc độ",
            running=False,
            error="cancelled",
        )
        raise HTTPException(409, "cancelled") from e
    except Exception as e:
        set_status(
            project_id,
            step="video",
            progress=0,
            message="Bake tốc độ thất bại",
            running=False,
            error=str(e)[:500],
        )
        raise HTTPException(500, f"Bake tốc độ thất bại: {e}") from e
    finally:
        clear_job(project_id)

    speed_baked = abs(speed - 1.0) > 0.001
    # workClipSec / duration = đúng cửa sổ display (khớp thước sau bake)
    work_clip = float(meta.get("workDuration") or 0)
    if work_clip <= 0.2 and work.is_file():
        try:
            work_clip = float(ffprobe_duration(work) or 0)
        except Exception:
            work_clip = 0.0
    if work_clip <= 0.2 and preview_sec > 0:
        work_clip = float(preview_sec) / speed if speed_baked and speed > 0.2 else float(preview_sec)
    duration = work_clip if work_clip > 0.2 else float(meta.get("duration") or 0)
    set_status(
        project_id,
        step="video",
        progress=100,
        message=f"Đã áp dụng {speed:.2f}× — thước {duration:.1f}s",
        running=False,
        error=None,
    )
    return {
        "ok": True,
        "bakedSpeed": speed,
        "bakedPreferVideo": speed_baked,
        "hasBakedSpeed": True,
        "workClipSec": duration,
        "duration": duration,
        "segments": meta.get("segments") or [],
        "overlays": meta.get("overlays") or [],
        "videoUrl": f"/api/projects/{project_id}/video",
        "timeScale": (old / speed) if speed > 0.2 and old > 0.2 else 1.0,
        "prevBakedSpeed": old,
    }


@router.get("/api/projects/{project_id}/status")
def api_status(project_id: str):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    st = dict(meta.get("status") or {"step": "video", "progress": 0, "message": "", "running": False})
    # UI restore: duration = cửa sổ làm việc (preview/bake), không phải full source
    from pipeline.core.media import meta_baked_speed, meta_has_user_bake

    src_dur = float(meta.get("duration") or 0)
    preview_1x = max(0, int(meta.get("previewSec") or 0))
    baked_speed = meta_baked_speed(meta)
    user_bake = meta_has_user_bake(meta)
    speed_baked = abs(baked_speed - 1.0) > 0.001
    work_dur = float(meta.get("workDuration") or 0)
    work_clip = 0.0
    display_dur = src_dur
    if speed_baked and work_dur > 0:
        work_clip = work_dur
        display_dur = work_dur
    elif preview_1x > 0:
        work_clip = float(preview_1x)
        if speed_baked and baked_speed > 0.2:
            work_clip = float(preview_1x) / baked_speed
        display_dur = work_clip
        work_path = Path(str(meta.get("workVideo") or ""))
        if work_path.is_file():
            try:
                wd = float(ffprobe_duration(work_path) or 0)
                if wd > 0.2:
                    work_clip = wd
                    display_dur = wd
            except Exception:
                pass
    elif work_dur > 0:
        display_dur = work_dur
    st["sourceDuration"] = src_dur
    st["workClipSec"] = work_clip
    st["duration"] = display_dur if display_dur > 0 else src_dur
    # bakedPreferVideo = file chậm ≠1; hasBakedSpeed = user đã Áp dụng (cả 1×)
    st["bakedPreferVideo"] = bool(speed_baked)
    st["bakedSpeed"] = baked_speed if user_bake else 1.0
    st["hasBakedSpeed"] = bool(user_bake)
    # Tốc độ khởi tạo luôn 1× — không bake file.
    from pipeline.core.media import ensure_project_initial_playback_rate

    had_init = meta.get("projectInitialPlaybackRate") is not None
    init_rate = ensure_project_initial_playback_rate(meta, meta.get("settings") or {})
    if not had_init:
        save_meta(project_id, meta)
    st["projectInitialPlaybackRate"] = float(init_rate)
    if meta.get("settings"):
        st["settings"] = meta["settings"]
    # Watermark discovery is separate from caption segments so the UI can show
    # what will be covered without putting handles into the dubbing script.
    if isinstance(meta.get("logoDetection"), dict):
        st["logoDetection"] = meta["logoDetection"]
    if meta.get("outputRel"):
        st["outputRel"] = st.get("outputRel") or meta.get("outputRel")
    return st


@router.post("/api/projects/{project_id}/status/dismiss")
def api_dismiss_status(project_id: str):
    """User đóng popup lỗi — xóa error khỏi meta để F5 không hiện lại."""
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    st = dict(meta.get("status") or {})
    if st.get("running"):
        # Job còn chạy: chỉ «Chạy nền», không xóa trạng thái
        return {"ok": True, "ignored": True}
    set_status(
        project_id,
        step=str(st.get("step") or "video"),
        progress=int(st.get("progress") or 0),
        message="",
        running=False,
        error=None,
    )
    return {"ok": True}


@router.post("/api/projects/{project_id}/settings")
def api_save_settings(project_id: str, settings: Settings):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    old_default = (meta.get("settings") or {}).get("defaultVoice") or ""
    meta["settings"] = settings.model_dump()
    new_default = settings.defaultVoice or ""
    # đổi giọng mặc định → CHỈ đồng bộ đoạn đang kế thừa (rỗng / system /
    # default cũ). Giọng user gán riêng từng câu phải giữ nguyên — khớp
    # api_dub (jobs.py) vốn đã tôn trọng lựa chọn riêng.
    if new_default and new_default != old_default:
        for seg in meta.get("segments") or []:
            current = (seg.get("voice") or "").strip()
            if not current or current == "system" or current == old_default:
                seg["voice"] = new_default
    save_meta(project_id, meta)
    return {"ok": True, "settings": meta["settings"]}


class ClearCacheBody(BaseModel):
    """parts: danh sách mục checkbox; rỗng/null = tất cả."""

    parts: list[str] | None = None


@router.delete("/api/cache/project/{project_id}")
@router.delete("/api/projects/{project_id}/cache")
@router.post("/api/cache/project/{project_id}")
@router.post("/api/projects/{project_id}/cache/clear")
def api_clear_project_cache(project_id: str, body: ClearCacheBody | None = None):
    """Xóa cache project — chỉ khi user chủ động bấm «Xóa cache» (+ chọn mục)."""
    from pipeline.core.project import clear_project_cache

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404, "Project không tồn tại")
    parts = body.parts if body else None
    result = clear_project_cache(project_id, parts=parts)
    return result
