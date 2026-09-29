"""Ghép ảnh/video theo timeline prompt và SRT."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from pipeline.srt_image import (
    ROOT, cancel, create_job, get_job, list_jobs, output_path,
    parse_timing_cues_detailed, parse_timing_times, pause, start,
)
from pipeline.core.output_paths import downloads_folder

router = APIRouter()
MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
}
TIMELINE_SUFFIXES = {".txt", ".srt", ".vtt", ".ass", ".ssa", ".csv", ".tsv", ".json", ".lrc"}


def _natural_name(path: Path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def _resolve_output_target(raw: str) -> Path | None:
    """Anchor WEB-relative names under the shared SRT image/video output root."""
    value = raw.strip()
    if not value:
        return None
    selected = Path(value).expanduser()
    target = selected if selected.is_absolute() else downloads_folder("media-compose") / selected
    target = target.resolve()
    if target.suffix.lower() != ".mp4":
        raise HTTPException(400, "File xuất phải có đuôi .mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _media_size(folder: str) -> dict | None:
    """Trả về {w, h} của file media đầu tiên trong folder."""
    p = Path(folder)
    if not p.is_dir():
        return None
    files = sorted(
        (f for f in p.iterdir() if f.suffix.lower() in MEDIA_SUFFIXES),
        key=_natural_name,
    )
    if not files:
        return None
    first = files[0]
    if first.suffix.lower() in {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp"}:
        try:
            from PIL import Image
            with Image.open(first) as im:
                return {"w": im.width, "h": im.height}
        except Exception:
            pass
    # Video: dùng ffprobe
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(first)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(r.stdout)
        s = data["streams"][0]
        return {"w": s["width"], "h": s["height"]}
    except Exception:
        return None


@router.get("/api/media-compose/media-size")
def media_size(folder: str = ""):
    """Kích thước file đầu tiên trong media folder — để preview đúng tỉ lệ."""
    result = _media_size(folder)
    if result is None:
        raise HTTPException(404, "Không tìm thấy file media")
    return result


@router.get("/api/media-compose/media-thumb")
def media_thumb(folder: str = "", index: int = 0):
    """Trả ảnh thứ index trong folder — dùng cho preview delogo."""
    p = Path(folder)
    if not p.is_dir():
        raise HTTPException(404, "Folder không tồn tại")
    files = sorted(
        (f for f in p.iterdir() if f.suffix.lower() in MEDIA_SUFFIXES),
        key=_natural_name,
    )
    if not files:
        raise HTTPException(404, "Không có media")
    idx = max(0, min(index, len(files) - 1))
    target = files[idx]
    img_exts = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp"}
    headers = {"X-Total": str(len(files)), "X-Index": str(idx), "X-Name": target.name}
    if target.suffix.lower() in img_exts:
        mt = "image/jpeg" if target.suffix.lower() in {".jpg", ".jpeg", ".jfif"} else f"image/{target.suffix.lower().strip('.')}"
        return FileResponse(target, media_type=mt, headers=headers)
    # Video: trích frame đầu
    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(target), "-frames:v", "1", "-q:v", "3", str(tmp)],
        capture_output=True, timeout=15,
    )
    if tmp.exists():
        return FileResponse(tmp, media_type="image/jpeg", headers=headers)
    raise HTTPException(500, "Không trích được frame")


@router.get("/api/media-compose/logo-preview")
def logo_preview(path: str = ""):
    target = Path(path).expanduser().resolve()
    if not target.is_file() or target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp"}:
        raise HTTPException(404, "Ảnh logo không tồn tại")
    return FileResponse(target)


@router.get("/api/media-compose/jobs")
def jobs():
    return list_jobs()


@router.post("/api/media-compose/jobs")
async def create(
    images: list[UploadFile] | None = File(None),
    media_folder: str = Form(""),
    timeline: UploadFile | None = File(None),
    srt: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
    watermark: UploadFile | None = File(None),
    timeline_path: str = Form(""),
    srt_path: str = Form(""),
    audio_path: str = Form(""),
    watermark_path: str = Form(""),
    options: str = Form("{}"),
    output_name: str = Form("ghep-anh-srt.mp4"),
    output_path: str = Form(""),
):
    work = ROOT / f"upload_{uuid.uuid4().hex[:10]}"
    work.mkdir(parents=True)
    image_paths: list[Path] = []
    if media_folder.strip():
        folder = Path(media_folder.strip()).expanduser().resolve()
        if not folder.is_dir():
            raise HTTPException(400, "Thư mục ảnh/video không tồn tại")
        image_paths = sorted(
            (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES),
            key=_natural_name,
        )
    else:
        for index, upload in enumerate(images or []):
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in MEDIA_SUFFIXES:
                continue
            target = work / f"{index:05d}{suffix}"
            with target.open("wb") as stream:
                shutil.copyfileobj(upload.file, stream)
            image_paths.append(target)
    if not image_paths:
        raise HTTPException(400, "Chưa chọn ảnh hoặc video hợp lệ")
    def copy_input(raw_path: str, upload: UploadFile | None, target_name: str,
                   suffixes: set[str], required: bool = False) -> Path | None:
        if raw_path.strip():
            source = Path(raw_path.strip()).expanduser().resolve()
            if not source.is_file() or source.suffix.lower() not in suffixes:
                raise HTTPException(400, f"File {target_name} không hợp lệ hoặc không còn tồn tại")
            target = work / f"{target_name}{source.suffix.lower()}"
            shutil.copy2(source, target)
            return target
        if upload and upload.filename:
            suffix = Path(upload.filename).suffix.lower()
            if suffix not in suffixes:
                raise HTTPException(400, f"File {target_name} không đúng định dạng")
            target = work / f"{target_name}{suffix}"
            with target.open("wb") as stream:
                shutil.copyfileobj(upload.file, stream)
            return target
        if required:
            raise HTTPException(400, f"Chưa chọn file {target_name}")
        return None

    timeline_file = copy_input(timeline_path, timeline, "timeline", TIMELINE_SUFFIXES)
    srt_file = copy_input(srt_path, srt, "subtitles", {".srt"})
    audio_file = copy_input(
        audio_path, audio, "audio", {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    )
    watermark_file = copy_input(
        watermark_path, watermark, "watermark", {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp"},
    )
    try:
        opts = json.loads(options)
    except json.JSONDecodeError:
        opts = {}
    if timeline_file and not opts.get("allowMissingMedia"):
        try:
            cues_detailed = parse_timing_cues_detailed(timeline_file)
            required = len(cues_detailed)
        except ValueError:
            cues_detailed = []
            required = 0
        if required > len(image_paths):
            shutil.rmtree(work, ignore_errors=True)
            avail_count = len(image_paths)
            missing_cues = cues_detailed[avail_count:]
            raise HTTPException(409, detail={
                "code": "missing_media",
                "required": required,
                "available": avail_count,
                "missing_count": required - avail_count,
                "available_files": [p.name for p in image_paths],
                "missing_cues": missing_cues,
            })
    output_target = _resolve_output_target(output_path)
    job = create_job(
        output_name, work, image_paths, audio_file, timeline_file, srt_file,
        opts, watermark_file, output_target,
    )
    start(job["id"])
    return job


@router.get("/api/media-compose/jobs/{job_id}")
def status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job không tồn tại")
    return job


@router.post("/api/media-compose/jobs/{job_id}/cancel")
def stop(job_id: str):
    if not cancel(job_id):
        raise HTTPException(404, "Job không tồn tại")
    return {"ok": True}


@router.post("/api/media-compose/jobs/{job_id}/pause")
def set_paused(job_id: str, paused: bool = True):
    if not pause(job_id, paused):
        raise HTTPException(409, "Không thể đổi trạng thái tiến trình")
    return {"ok": True, "paused": paused}


@router.post("/api/media-compose/open-folder")
def open_folder(job_id: str = "", selected_output: str = ""):
    job = get_job(job_id) if job_id else None
    output = (
        Path(selected_output).expanduser().resolve()
        if selected_output.strip()
        else Path(job["output"]) if job else None
    )
    folder = output.parent if output else ROOT
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Mở đúng thư mục xuất; không dùng /select vì Windows có thể giữ Explorer ẩn phía sau app.
        subprocess.Popen(["explorer.exe", str(folder)])
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
    return {"ok": True}


@router.post("/api/media-compose/jobs/{job_id}/open")
def open_video(job_id: str):
    path = output_path(job_id)
    if not path:
        raise HTTPException(404, "Video chưa hoàn thành")
    try:
        if os.name == "nt":
            # ponytail: 'start' đưa cửa sổ player lên foreground, os.startfile() thì không
            subprocess.Popen(["cmd", "/c", "start", "", str(path)], creationflags=0x00000008)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    except OSError as exc:
        raise HTTPException(500, f"Không mở được video: {exc}") from exc
    return {"ok": True, "path": str(path)}


@router.get("/api/media-compose/jobs/{job_id}/file")
def file(job_id: str):
    path = output_path(job_id)
    if not path:
        raise HTTPException(404, "Video chưa hoàn thành")
    return FileResponse(path, media_type="video/mp4", filename=path.name)
