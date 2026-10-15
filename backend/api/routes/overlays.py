"""Domain API routes."""
from __future__ import annotations

import json
import math
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
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
from pipeline.core.project import apply_meta_patch
from pipeline.core.project import ensure_layout, load_meta, mutate_meta
from pipeline.core.media import video_size

router = APIRouter()

# Aliases matching original routes_all names
_spawn = spawn
_serve_video_file = serve_video_file
_validate_overlay = validate_overlay
_validate_segment_editor_fields = validate_segment_editor_fields
_SEG_PRESERVE = SEG_PRESERVE


@router.post("/api/projects/{project_id}/logo-asset")
async def api_upload_logo_asset(project_id: str, file: UploadFile = File(...)):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    if file.content_type not in {"image/png", "image/webp", "image/jpeg"}:
        raise HTTPException(415, "Logo phải là PNG, WebP hoặc JPG")
    data = await file.read(10 * 1024 * 1024 + 1)
    if not data or len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Logo tối đa 10 MB")
    from io import BytesIO
    from PIL import Image
    try:
        image = Image.open(BytesIO(data))
        image.verify()
        width, height = image.size
    except Exception as exc:
        raise HTTPException(422, "File ảnh logo không hợp lệ") from exc
    if width < 2 or height < 2 or width > 8192 or height > 8192:
        raise HTTPException(422, "Kích thước logo không hợp lệ")
    ext = {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg"}[file.content_type]
    assets = ensure_layout(project_id) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    name = f"logo-{uuid.uuid4().hex}{ext}"
    (assets / name).write_bytes(data)
    return {"url": f"/data/{project_id}/assets/{name}", "width": width, "height": height}


def _validate_overlay(body: TextOverlayIn, meta: dict) -> None:
    width, height = video_size(Path(meta["videoPath"]))
    values = (body.start, body.end, body.x, body.y, body.w, body.h)
    if not all(math.isfinite(value) for value in values) or body.end <= body.start:
        raise HTTPException(422, "Thời gian text không hợp lệ")
    if body.x < 0 or body.y < 0 or body.w <= 0 or body.h <= 0 or body.x + body.w > width or body.y + body.h > height:
        raise HTTPException(422, "Text nằm ngoài khung video")
    min_font_size = 6 if body.kind == "logo" else 12
    if body.kind != "effect" and not min_font_size <= body.fontSize <= 240:
        raise HTTPException(422, f"Cỡ chữ phải nằm trong khoảng {min_font_size}–240")


def _validate_logo(body: TextOverlayIn) -> None:
    if body.kind != "logo":
        return
    if body.logoSource not in {"text", "image", "icon"} or body.motion not in {"fixed", "random"}:
        raise HTTPException(422, "Cấu hình logo không hợp lệ")
    if body.logoSource != "text" and not str(body.assetUrl or "").startswith("/data/"):
        raise HTTPException(422, "Logo ảnh chưa được tải lên")
    if body.positionKeyframes and len(body.positionKeyframes) > 2000:
        raise HTTPException(422, "Logo có quá nhiều vị trí")


@router.get("/api/projects/{project_id}/overlays")
def api_overlays(project_id: str):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    return meta.get("overlays") or []


@router.post("/api/projects/{project_id}/overlays")
def api_create_overlay(project_id: str, body: TextOverlayIn):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    _validate_overlay(body, meta)
    _validate_logo(body)
    overlays = meta.get("overlays") or []
    if body.kind == "logo" and any(item.get("kind") == "logo" for item in overlays):
        raise HTTPException(409, "Project đã có logo")
    if any(item.get("id") == body.id for item in overlays):
        raise HTTPException(409, "Text overlay đã tồn tại")
    item = body.model_dump()
    overlays.append(item)
    meta["overlays"] = overlays
    # Đổi cấu trúc overlay → baseline 1× cũ hết hợp lệ (Áp dụng tốc độ sau đó
    # sẽ revert overlay về trạng thái baseline nếu không xoá).
    apply_meta_patch(project_id, {"overlays": overlays}, remove=("timelineBaseline",))
    return item


@router.put("/api/projects/{project_id}/overlays")
def api_replace_overlays(project_id: str, body: list[TextOverlayIn]):
    """Thay cả list overlay (undo/redo)."""

    def apply(meta: dict) -> list[dict]:
        if not meta:
            raise HTTPException(404)
        for item in body:
            _validate_overlay(item, meta)
            _validate_logo(item)
        if sum(item.kind == "logo" for item in body) > 1:
            raise HTTPException(422, "Mỗi project chỉ có một logo")
        out = [item.model_dump() for item in body]
        meta["overlays"] = out
        meta.pop("timelineBaseline", None)
        return out

    return mutate_meta(project_id, apply)


@router.put("/api/projects/{project_id}/overlays/{overlay_id}")
def api_update_overlay(project_id: str, overlay_id: str, body: TextOverlayIn):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    _validate_overlay(body, meta)
    _validate_logo(body)
    overlays = meta.get("overlays") or []
    for i, item in enumerate(overlays):
        if item.get("id") == overlay_id:
            overlays[i] = body.model_dump()
            meta["overlays"] = overlays
            apply_meta_patch(project_id, {"overlays": overlays}, remove=("timelineBaseline",))
            return overlays[i]
    raise HTTPException(404, "Text overlay không tồn tại")


@router.delete("/api/projects/{project_id}/overlays/{overlay_id}")
def api_delete_overlay(project_id: str, overlay_id: str):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    overlays = [item for item in (meta.get("overlays") or []) if item.get("id") != overlay_id]
    meta["overlays"] = overlays
    apply_meta_patch(project_id, {"overlays": overlays}, remove=("timelineBaseline",))
    return {"ok": True}

