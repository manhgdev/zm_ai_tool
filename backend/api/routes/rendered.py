"""Completed video exports and their cached thumbnails."""
from __future__ import annotations

import os
import re
import json
import hashlib
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pipeline.core.config import PUBLIC_DATA
from pipeline.core.media import ffprobe_duration, video_size
from pipeline.core.output_paths import downloads_folder

router = APIRouter()
_RENDER_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class RenderRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _export_media_paths() -> list[Path]:
    """All known completed Clone, Review and standalone-tool media."""
    paths: list[Path] = []
    
    SUPPORTED_EXTS = {
        ".mp4", ".mov", ".webm", ".mkv",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp",
        ".mp3", ".wav", ".m4a", ".aac", ".ogg",
        ".srt", ".vtt", ".txt"
    }

    def _glob_media(folder: Path):
        if not folder.is_dir(): return
        for p in folder.rglob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                paths.append(p)

    flat = PUBLIC_DATA / "exports"
    if flat.is_dir():
        for p in flat.iterdir():
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                paths.append(p)

    if PUBLIC_DATA.is_dir():
        for project_dir in PUBLIC_DATA.iterdir():
            if not project_dir.is_dir() or project_dir.name == "exports":
                continue
            sub = project_dir / "exports"
            if sub.is_dir():
                for p in sub.iterdir():
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                        paths.append(p)
                        
    for folder in (
        downloads_folder("video-clone"),
        downloads_folder("film"),
        downloads_folder("flow"),
        downloads_folder("download-video"),
        downloads_folder("tts"),
        downloads_folder("subtitle-export"),
        downloads_folder("subtitle-image"),
        downloads_folder("drawing"),
        downloads_folder("cleaner"),
        downloads_folder("batch"),
    ):
        _glob_media(folder)

    seen: set[str] = set()
    uniq: list[Path] = []
    for candidate in paths:
        if not candidate.is_file():
            continue
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(candidate)
    uniq.sort(key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True)
    return uniq


def _render_id(output: Path, paths: list[Path]) -> str:
    """Keep legacy IDs when unique; hash path for unsafe/duplicate filenames."""
    stem = output.stem
    duplicates = sum(path.stem == stem for path in paths) > 1
    if _RENDER_ID.fullmatch(stem) and not duplicates:
        return stem
    digest = hashlib.sha256(str(output.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"media-{digest}"


def _render_path(render_id: str) -> Path | None:
    if not _RENDER_ID.fullmatch(render_id):
        return None
    paths = _export_media_paths()
    # 1. Exact render_id match
    for path in paths:
        if _render_id(path, paths) == render_id:
            return path
    # 2. Hash fallback (media-{hash16})
    if render_id.startswith("media-"):
        target_hash = render_id[6:]
        for path in paths:
            if hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16] == target_hash:
                return path
    # 3. Stem fallback
    for path in paths:
        if path.stem == render_id:
            return path
    return None


def _project_id(output: Path, saved: dict[str, Any]) -> str:
    explicit = str(saved.get("projectId") or "").strip()
    if explicit:
        return explicit
    if output.parent == PUBLIC_DATA / "exports":
        return output.stem.split("-", 1)[0]
    try:
        relative = output.resolve().relative_to(PUBLIC_DATA.resolve())
    except ValueError:
        return ""
    if len(relative.parts) >= 3 and relative.parts[1] == "exports":
        return relative.parts[0]
    return ""


from concurrent.futures import ThreadPoolExecutor

_PROBE_CACHE: dict[str, tuple[float, int, int, int, float]] = {}



def _get_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".mp4", ".mov", ".webm", ".mkv"}: return "video"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}: return "image"
    if ext in {".mp3", ".wav", ".m4a", ".aac", ".ogg"}: return "audio"
    if ext in {".srt", ".vtt", ".txt"}: return "srt"
    return "other"

def _probe_media_info(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
        cached = _PROBE_CACHE.get(str(path))
        if cached and cached[0] == mtime and cached[1] == size:
            w, h, dur = cached[2], cached[3], cached[4]
        else:
            w, h, dur = 0, 0, 0.0
            m_type = _get_media_type(path)
            if m_type == "video":
                w, h = video_size(path)
                dur = ffprobe_duration(path)
            elif m_type == "image":
                try:
                    w, h = video_size(path)
                except Exception:
                    pass
            elif m_type == "audio":
                dur = ffprobe_duration(path)
            _PROBE_CACHE[str(path)] = (mtime, size, w, h, dur)

        return {
            "path": path,
            "width": w,
            "height": h,
            "duration": dur,
            "sizeBytes": size,
            "mtime": mtime,
        }
    except Exception:
        return None
        return {
            "path": path,
            "width": w,
            "height": h,
            "duration": dur,
            "sizeBytes": size,
            "mtime": mtime,
        }
    except Exception:
        return None


def list_rendered_videos() -> list[dict[str, Any]]:
    paths = _export_media_paths()
    if not paths:
        return []
    # ponytail: giới hạn 120 video mới nhất để phản hồi siêu tốc dưới 0.1s
    paths = paths[:120]
    archived_projects = {path.stem.split("-", 1)[0] for path in paths if "-" in path.stem}

    with ThreadPoolExecutor(max_workers=16) as pool:
        probed_results = list(pool.map(_probe_media_info, paths))

    items: list[dict[str, Any]] = []
    for info in probed_results:
        if not info:
            continue
        output: Path = info["path"]
        m_type = _get_media_type(output)

        render_id = _render_id(output, paths)
        try:
            sidecar = output.with_suffix(".json")
            saved = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
            project_id = _project_id(output, saved)
            if output.stem == project_id and project_id in archived_projects:
                continue
            name = str(saved.get("name") or "").strip() or output.stem
        except Exception:
            name = output.stem
            project_id = ""

        items.append({
            "renderId": render_id,
            "projectId": project_id,
            "type": m_type,

            "canEdit": bool(project_id),
            "name": name,
            "createdAt": datetime.fromtimestamp(info["mtime"], timezone.utc).isoformat(),
            "sizeBytes": info["sizeBytes"],
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "outputFolder": str(output.parent),
            "videoUrl": f"/api/renders/{render_id}/video",
            "downloadUrl": f"/api/renders/{render_id}/video?download=1",
            "thumbnailUrl": f"/api/renders/{render_id}/thumbnail",
        })
    return sorted(items, key=lambda item: item["createdAt"], reverse=True)


def ensure_thumbnail(render_id: str) -> Path:
    output = _render_path(render_id)
    if output is None:
        raise FileNotFoundError(render_id)
    
    m_type = _get_media_type(output)
    if m_type == "image":
        return output
    if m_type in {"audio", "srt", "other"}:
        raise FileNotFoundError("No thumbnail for this media type")


    thumbnail = PUBLIC_DATA / "exports" / "thumbnails" / f"{render_id}.jpg"
    if thumbnail.is_file() and thumbnail.stat().st_mtime >= output.stat().st_mtime:
        return thumbnail
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    temp = thumbnail.with_name(f"{render_id}.{uuid.uuid4().hex}.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1",
             "-i", str(output), "-frames:v", "1", "-vf", "scale=640:-2",
             "-q:v", "3", str(temp)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        temp.replace(thumbnail)
        return thumbnail
    finally:
        temp.unlink(missing_ok=True)


@router.get("/api/renders")
def api_renders():
    desktop = (os.environ.get("ZM_AI_TOOL_DESKTOP")) == "1" or bool(getattr(sys, "frozen", False))
    return {"items": list_rendered_videos(), "canReveal": desktop}


@router.put("/api/renders/{render_id}")
def api_rename_render(render_id: str, body: RenderRenameIn):
    path = _render_path(render_id)
    if path is None:
        raise HTTPException(404, "Khong tim thay file render")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Ten render khong duoc de trong")
    sidecar = path.with_suffix(".json")
    # Giữ projectId thật từ sidecar (tên render tự đặt không chứa project id)
    project_id = render_id.split("-", 1)[0]
    try:
        if sidecar.is_file():
            project_id = str(json.loads(sidecar.read_text(encoding="utf-8")).get("projectId") or project_id)
    except (OSError, ValueError):
        pass
    sidecar.write_text(
        json.dumps({"name": name, "projectId": project_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"renderId": render_id, "name": name}


class DeleteRendersIn(BaseModel):
    renderIds: list[str] = Field(default_factory=list)
    all: bool = False
    mediaType: str = "all"


def _delete_single_render_path(path: Path, render_id: str) -> bool:
    try:
        exports = PUBLIC_DATA / "exports"
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
        (exports / "thumbnails" / f"{render_id}.jpg").unlink(missing_ok=True)
        (exports / "thumbnails" / f"{path.stem}.jpg").unlink(missing_ok=True)
        # Bản "dễ tìm" <project>.mp4 chỉ bị ẩn khi còn bản lưu trữ <project>-*.mp4;
        # xóa bản lưu trữ cuối cùng thì dọn luôn để video không hiện lại trong tab.
        project_id = render_id.split("-", 1)[0]
        if "-" in render_id and not any(exports.glob(f"{project_id}-*.mp4")):
            (exports / f"{project_id}.mp4").unlink(missing_ok=True)
            (exports / f"{project_id}.json").unlink(missing_ok=True)
            (exports / "thumbnails" / f"{project_id}.jpg").unlink(missing_ok=True)
        return True
    except Exception:
        return False


@router.post("/api/renders/delete-batch")
def api_delete_renders_batch(body: DeleteRendersIn):
    paths = _export_media_paths()
    deleted_ids: list[str] = []

    if body.all:
        target_paths = paths
        if body.mediaType and body.mediaType != "all":
            target_paths = [p for p in paths if _get_media_type(p) == body.mediaType]
        for path in target_paths:
            rid = _render_id(path, paths)
            if _delete_single_render_path(path, rid):
                deleted_ids.append(rid)
        return {"ok": True, "deletedCount": len(deleted_ids)}

    for rid in body.renderIds:
        path = _render_path(rid)
        if path and _delete_single_render_path(path, rid):
            deleted_ids.append(rid)

    return {"ok": True, "deletedCount": len(deleted_ids), "deletedIds": deleted_ids}


@router.delete("/api/renders/{render_id}")
def api_delete_render(render_id: str):
    path = _render_path(render_id)
    if path is None:
        raise HTTPException(404, "Khong tim thay file render")
    _delete_single_render_path(path, render_id)
    return {"ok": True}


@router.get("/api/renders/{render_id}/video")
def api_render_video(render_id: str, download: bool = False):
    path = _render_path(render_id)
    if path is None:
        raise HTTPException(404)
    if download:
        return FileResponse(path, filename=path.name)
    return FileResponse(path, content_disposition_type="inline")


@router.get("/api/renders/{render_id}/thumbnail")
async def api_render_thumbnail(render_id: str):
    import anyio
    try:
        path = await anyio.to_thread.run_sync(lambda: ensure_thumbnail(render_id))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        raise HTTPException(404, "Khong tao duoc thumbnail") from None
    return FileResponse(path, media_type="image/jpeg")



@router.post("/api/renders/{render_id}/reveal")
def api_reveal_render(render_id: str):
    import platform

    path = _render_path(render_id)
    if path is None:
        raise HTTPException(404, "Khong tim thay file render")
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif platform.system() == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "path": str(path.resolve())}
