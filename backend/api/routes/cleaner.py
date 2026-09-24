"""Video cleaner API routes."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from pipeline.cleaner.cleaner_jobs import (
    CLEANER_TEMP_DIR,
    cancel_job,
    clear_job_logs,
    create_job,
    delete_job,
    get_job,
    get_job_output_path,
    list_jobs,
    reveal_job_file,
)
from pipeline.cleaner.cleaner_ffmpeg import start_cleaner_job

router = APIRouter()
SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
}

@router.get("/api/cleaner/jobs")
def api_cleaner_list():
    return list_jobs()

@router.post("/api/cleaner/jobs")
async def api_cleaner_start(
    files: list[UploadFile] = File(...),
    method: str = Form(...),
    options: str = Form(...),
    output_dir: str = Form(""),
):
    if method not in {"metadata", "reencode", "optimize", "logo"}:
        raise HTTPException(400, "Phương pháp làm sạch không hợp lệ")
    try:
        opts_dict = json.loads(options)
    except Exception:
        opts_dict = {}

    created_jobs = []
    
    for upload in files:
        if not upload.filename:
            continue
            
        ext = Path(upload.filename).suffix or ".mp4"
        if ext.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(400, f"Định dạng media không được hỗ trợ: {ext}")
        temp_path = CLEANER_TEMP_DIR / f"temp_{uuid.uuid4().hex[:8]}{ext}"
        
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)
            
        job = create_job(
            filename=upload.filename,
            method=method,
            options=opts_dict,
            input_path=str(temp_path),
            output_dir=output_dir,
        )
        created_jobs.append(job)
        start_cleaner_job(job["id"])
        
    return created_jobs


@router.delete("/api/cleaner/logs")
def api_cleaner_clear_logs():
    """Clear visible diagnostics without removing queued jobs or media files."""
    return {"ok": True, "cleared": clear_job_logs()}

@router.get("/api/cleaner/jobs/{job_id}")
def api_cleaner_get(job_id: str):
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "Job không tồn tại")
    return j

@router.post("/api/cleaner/jobs/{job_id}/cancel")
def api_cleaner_cancel(job_id: str):
    if not cancel_job(job_id):
        raise HTTPException(404, "Job không tồn tại")
    return {"ok": True}

@router.delete("/api/cleaner/jobs/{job_id}")
def api_cleaner_delete(job_id: str):
    if not delete_job(job_id):
        raise HTTPException(404, "Job không tồn tại")
    return {"ok": True}

@router.post("/api/cleaner/jobs/{job_id}/reveal")
def api_cleaner_reveal(job_id: str):
    if not reveal_job_file(job_id):
        raise HTTPException(404, "Không tìm thấy file hoặc chưa hoàn thành")
    return {"ok": True}

@router.get("/api/cleaner/jobs/{job_id}/file")
def api_cleaner_file(job_id: str, download: int = 0):
    p = get_job_output_path(job_id)
    if not p:
        raise HTTPException(404, "Chưa có file")
    
    media = "video/mp4"
    if p.suffix.lower() == ".webm":
        media = "video/webm"
    elif p.suffix.lower() == ".mkv":
        media = "video/x-matroska"
    elif p.suffix.lower() in {".png"}:
        media = "image/png"
    elif p.suffix.lower() in {".jpg", ".jpeg"}:
        media = "image/jpeg"
    elif p.suffix.lower() == ".webp":
        media = "image/webp"
    elif p.suffix.lower() in {".bmp"}:
        media = "image/bmp"
        
    return FileResponse(
        p,
        media_type=media,
        filename=p.name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )
