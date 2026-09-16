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
from fastapi.responses import FileResponse, StreamingResponse
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
from pipeline import (
    DATA,
    PUBLIC_DATA,
    ensure_layout,
    ffprobe_duration,
    find_project_by_fp,
    hardware,
    list_voices,
    load_meta,
    mutate_meta,
    out_final,
    project_dir,
    request_cancel,
    run_dub,
    run_export,
    run_pipeline,
    save_meta,
    set_status,
    tts_cache_key,
    tts_segment,
    video_fingerprint,
)
from pipeline.core.config import REPO_ROOT, export_display_path
from pipeline.core.jobs import arm_job
from pipeline.core.media import meta_baked_speed, meta_has_user_bake, video_size
from pipeline.export.mux import (
    export_project_audio,
    find_cached_no_vocals,
    read_stem_progress,
    separate_no_vocals,
)
from pipeline.tts import engines_status

router = APIRouter()

# Aliases matching original routes_all names
_spawn = spawn
_serve_video_file = serve_video_file
_validate_overlay = validate_overlay
_validate_segment_editor_fields = validate_segment_editor_fields
_SEG_PRESERVE = SEG_PRESERVE


@router.get("/api/projects/{project_id}/events")
async def api_project_events(project_id: str, after: int = 0):
    """Small SSE stream used by chunked ASR/translation and resource installs."""
    if not load_meta(project_id):
        raise HTTPException(404)

    async def stream():
        import asyncio

        cursor = max(0, int(after))
        # Grace period: subprocess takes ~0.2s to start and flip running=True.
        # ponytail: 10 ticks × 0.3s = 3s window before declaring idle.
        idle_ticks = 0
        # A bounded stream works with proxies and lets the UI reconnect safely.
        for _ in range(100):
            meta = load_meta(project_id)
            events = [event for event in meta.get("jobEvents") or [] if isinstance(event, dict) and int(event.get("id") or 0) > cursor]
            for event in events:
                cursor = max(cursor, int(event.get("id") or 0))
                idle_ticks = 0
                yield f"id: {cursor}\nevent: {event.get('type', 'STATUS')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            status = meta.get("status") or {}
            if not status.get("running") and not events:
                idle_ticks += 1
                if idle_ticks >= 10:  # 3s grace period
                    yield ": idle\n\n"
                    return
            else:
                idle_ticks = 0
            await asyncio.sleep(0.3)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/projects/{project_id}/run")
def api_run(project_id: str, settings: Settings):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    dumped = settings.model_dump()
    # Tách: ô Preview (UI) ≠ cửa sổ lần chạy (full / Ns)
    run_raw = dumped.pop("runPreviewSec", None)
    if run_raw is None:
        run_sec = max(0, int(dumped.get("previewSec") or 0))
    else:
        run_sec = max(0, int(run_raw))
    ui_prev = max(0, int(dumped.get("previewSec") or 0))
    if ui_prev <= 0:
        ui_prev = max(0, int((meta.get("settings") or {}).get("previewSec") or 0)) or 20
    dumped["previewSec"] = ui_prev
    meta["settings"] = dumped
    # Cửa sổ clip làm việc / cache tag — không đụng ô Preview UI
    meta["previewSec"] = run_sec
    # Tốc độ khởi tạo luôn 1×; Khớp thời lượng không tự bake video.
    from pipeline.core.media import ensure_project_initial_playback_rate

    ensure_project_initial_playback_rate(meta, dumped)
    save_meta(project_id, meta)
    # Interactive Clone is a real queue job too.  This makes it visible in
    # Batch, shares the same device scheduler and lets one Cancel stop it.
    from pipeline.core.jobs import share_cancel
    from pipeline.queue.engine import enqueue_project_clone

    arm_job(project_id)
    source = str(meta.get("videoPath") or "")
    if not source:
        raise HTTPException(422, "Project không có video nguồn")
    job = enqueue_project_clone(project_id, source, {**dumped, "previewSec": run_sec})
    share_cancel(str(job["id"]), project_id)
    hint = f"Đã xếp hàng Preview {run_sec}s" if run_sec > 0 else "Đã xếp hàng dịch cả video"
    set_status(project_id, step="queued", progress=0, message=hint, running=True, error=None)
    return {"ok": True, "jobId": job["id"]}


@router.post("/api/projects/{project_id}/ocr-translate")
def api_run_ocr_translate(project_id: str, settings: Settings):
    if not load_meta(project_id):
        raise HTTPException(404)
    from pipeline.ocr.translate_track import claim_ocr_translate, run_ocr_translate_track
    if not claim_ocr_translate(project_id):
        raise HTTPException(409, "OCR Translator đang chạy cho project này")
    arm_job(project_id)
    # Publish a running state before returning. Without this, the editor's
    # first status poll can read the previous completed job and dismiss the
    # OCR progress popup while the worker thread is only being spawned.
    set_status(
        project_id,
        step="asr",
        progress=1,
        message="Đang khởi tạo OCR Translator…",
        running=True,
        error=None,
    )
    _spawn(run_ocr_translate_track, project_id, settings.model_dump())
    return {"ok": True}


@router.post("/api/projects/{project_id}/dub")
def api_dub(project_id: str, settings: Settings):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    old_default = (meta.get("settings") or {}).get("defaultVoice") or ""
    force_tts = bool(settings.forceTts)
    dumped = settings.model_dump()
    dumped.pop("forceTts", None)
    meta["settings"] = dumped
    default = settings.defaultVoice
    segs = meta.get("segments") or []
    uniq = {(s.get("voice") or "").strip() for s in segs}
    uniq.discard("")
    uniq.discard("system")
    # đồng bộ default: inherit cũ, hoặc cả loạt cùng 1 giọng ≠ default (vd. Adam còn sót)
    batch = len(uniq) <= 1 and (not uniq or default not in uniq)
    for seg in segs:
        v = (seg.get("voice") or "").strip()
        if batch or not v or v == "system" or v == old_default:
            seg["voice"] = default
        if force_tts:
            # Xóa trỏ cache — run_dub gen lại; file .wav xóa trong run_dub
            seg.pop("audioFile", None)
            seg.pop("audioUrl", None)
            seg.pop("audioDuration", None)
            seg.pop("videoSpeed", None)
    if force_tts:
        meta["forceTts"] = True
    save_meta(project_id, meta)
    arm_job(project_id)
    set_status(
        project_id,
        step="dub",
        progress=1,
        message="Queued… (gen lại TTS)" if force_tts else "Queued…",
        running=True,
        error=None,
    )
    _spawn(run_dub, project_id)
    return {"ok": True}


@router.post("/api/projects/{project_id}/cancel")
def api_cancel(project_id: str):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    st = meta.get("status") or {}
    cur_step = str(st.get("step") or "video")
    # Luôn set cancel flag (kể cả Queued trước begin_job)
    request_cancel(project_id)
    if not st.get("running"):
        # UI có thể đang optimistic running — vẫn ghi cancelled
        set_status(
            project_id,
            step=cur_step if cur_step in ("asr", "translate", "dub", "export") else "video",
            progress=0,
            message="Đã huỷ",
            running=False,
            error="cancelled",
        )
        return {"ok": True}
    msg = {
        "dub": "Đã huỷ lồng tiếng",
        "export": "Đã huỷ xuất bản",
        "asr": "Đã huỷ",
        "translate": "Đã huỷ",
    }.get(cur_step, "Đã huỷ")
    set_status(
        project_id,
        step=cur_step if cur_step in ("asr", "translate", "dub", "export") else "video",
        progress=0,
        message=msg,
        running=False,
        error="cancelled",
    )
    return {"ok": True}


@router.post("/api/projects/{project_id}/export")
def api_export(project_id: str, payload: ExportPayload):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    # UI checkbox phải thắng meta cũ; previewSec ô Preview ≠ độ dài lần dịch
    dumped = payload.model_dump(exclude={"segments", "exportEndSec", "exportStartSec", "renderName"}, exclude_none=False)
    meta["pendingRenderName"] = payload.renderName.strip()
    if payload.segments is not None:
        # List từ editor = source of truth (không merge giữ meta cũ → lệch WYSIWYG)
        ordered = sorted(payload.segments, key=lambda s: (s.start, s.end, s.id))
        out: list[dict] = []
        for i, item in enumerate(ordered):
            d = item.model_dump(exclude_none=False)
            # Migrate pre-timeline/CapCut cues that did not include an editor
            # id.  Renderer, TTS and later edits all require a persistent id.
            d["id"] = str(d.get("id") or uuid.uuid4().hex)
            if d.get("bbox") is None:
                d.pop("bbox", None)
            if d.get("captionLayout") is None:
                d.pop("captionLayout", None)
            d["index"] = i
            out.append(d)
        meta["segments"] = out
    if payload.exportEndSec is not None:
        if not math.isfinite(payload.exportEndSec) or payload.exportEndSec <= 0:
            raise HTTPException(422, "exportEndSec phải lớn hơn 0")
        meta["exportEndSec"] = float(payload.exportEndSec)
    else:
        meta.pop("exportEndSec", None)
    if payload.exportStartSec is not None:
        if not math.isfinite(payload.exportStartSec) or payload.exportStartSec < 0:
            raise HTTPException(422, "exportStartSec phải lớn hơn hoặc bằng 0")
        meta["exportStartSec"] = float(payload.exportStartSec)
    else:
        meta.pop("exportStartSec", None)
    run_preview = max(0, int(meta.get("previewSec") or 0))
    ui_prev = max(0, int(dumped.get("previewSec") or 0))
    if ui_prev <= 0:
        ui_prev = max(0, int((meta.get("settings") or {}).get("previewSec") or 0)) or 20
    dumped["previewSec"] = ui_prev
    # Settings editor thắng hoàn toàn (mask/font/cover/burn…)
    # Ngoại lệ: blurBandAutoRegion được backend lưu sau review phase.
    # Frontend xóa nó khỏi in-memory sau review (để reset preview), nhưng
    # backend vừa tính xong → preserve nếu request không mang region mới hơn.
    old_settings = meta.get("settings") or {}
    if (
        not dumped.get("blurBandAutoRegion")
        and int(old_settings.get("blurBandAutoRegionVersion") or 0) == 1
        and old_settings.get("blurBandAutoRegion")
    ):
        dumped["blurBandAutoRegion"] = old_settings["blurBandAutoRegion"]
        dumped["blurBandAutoRegionVersion"] = 1
    meta["settings"] = dumped
    # xuất theo clip lần dịch gần nhất (0 = full), không theo ô Preview
    meta["previewSec"] = run_preview
    save_meta(project_id, meta)
    hint = "full" if run_preview <= 0 else f"preview {run_preview}s"
    arm_job(project_id)
    set_status(
        project_id,
        step="export",
        progress=1,
        message=f"Đang xuất ({hint})…",
        running=True,
        error=None,
    )
    _spawn(run_export, project_id)
    return {
        "ok": True,
        "url": f"/api/projects/{project_id}/output",
        "path": export_display_path(project_dir(project_id) / "out" / "final.mp4"),
        "exports": export_display_path(PUBLIC_DATA / "exports" / f"{project_id}.mp4"),
    }


@router.get("/api/projects/{project_id}/output")
def api_output(project_id: str, download: bool = False):
    path = out_final(project_id)
    if not path.exists():
        legacy = project_dir(project_id) / "output.mp4"
        easy = PUBLIC_DATA / "exports" / f"{project_id}.mp4"
        if legacy.exists():
            path = legacy
        elif easy.exists():
            path = easy
        else:
            raise HTTPException(404)
    name = f"zm-ai-tool-{project_id}.mp4"
    # download=1 → attachment; mặc định inline để trình duyệt phát được
    if download:
        return FileResponse(path, filename=name, media_type="video/mp4")
    return FileResponse(path, media_type="video/mp4", content_disposition_type="inline")


@router.post("/api/projects/{project_id}/open-output")
def api_open_output(project_id: str):
    """Mở video đã xuất bằng trình phát mặc định."""
    import platform
    import subprocess

    from pipeline.core.project import load_meta

    meta = load_meta(project_id) or {}
    # Tìm file video đã xuất
    for raw in (meta.get("exportCopy"), meta.get("outputPath")):
        value = str(raw or "").strip()
        if not value:
            continue
        p = Path(value)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if p.is_file():
            system = platform.system()
            try:
                if system == "Darwin":
                    subprocess.Popen(["open", str(p)])
                elif system == "Windows":
                    import os
                    os.startfile(str(p))
                else:
                    subprocess.Popen(["xdg-open", str(p)])
            except OSError as e:
                raise HTTPException(500, str(e)) from e
            return {"ok": True, "path": str(p.resolve())}
    raise HTTPException(404, "Chưa có file xuất")


@router.post("/api/projects/{project_id}/reveal-output")
def api_reveal_output(project_id: str):
    """Mở Finder/Explorer tại file đã xuất (local app)."""
    import platform
    import subprocess

    exports = PUBLIC_DATA / project_id / "exports"
    _meta = {}
    try:
        from pipeline.core.project import load_meta
        _meta = load_meta(project_id) or {}
        _custom = str(_meta.get("exportOutputDir") or "").strip()
        if _custom and Path(_custom).is_dir():
            # exportOutputDir is persisted as the final render directory.
            # Older projects stored the parent and need the slug fallback;
            # never append the slug blindly (that caused <slug>/<slug>).
            direct = Path(_custom)
            exports = direct
            try:
                from pipeline.orchestrate.export_job import _project_slug
                nested = direct / _project_slug(_meta)
                if not any(direct.glob("*")) and nested.is_dir():
                    exports = nested
            except Exception:
                pass
    except Exception:
        pass

    import re
    render_name = str(_meta.get("lastRenderName") or _meta.get("pendingRenderName") or "").strip() or f"Render {project_id}"
    safe_name = re.sub(r'[^\w\s-]', '', render_name).strip()
    safe_name = re.sub(r'[-\s]+', '-', safe_name)
    if not safe_name:
        safe_name = project_id

    # Prefer the exact file recorded by export_job; render names may contain
    # Unicode/punctuation that is normalized differently by legacy metadata.
    exact_paths: list[Path] = []
    # `outputPath` is the intermediate file in project/out; reveal the
    # published export copy first so Explorer opens the folder the user sees.
    for raw in (_meta.get("outputRel"), _meta.get("exportCopy"), _meta.get("outputPath")):
        value = str(raw or "").strip()
        if not value:
            continue
        p = Path(value)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if p.is_file():
            exact_paths.append(p)

    export_dirs = [exports]
    try:
        from pipeline.orchestrate.export_job import _project_slug
        legacy_exports = PUBLIC_DATA / "exports" / _project_slug(_meta)
        if legacy_exports not in export_dirs:
            export_dirs.append(legacy_exports)
    except Exception:
        pass
    candidate_names = [
        exports / f"{safe_name}.mp4",
        exports / f"{safe_name}.mp3",
        exports / f"{safe_name}.wav",
        exports / f"{safe_name}.aac",
        exports / f"{safe_name}.srt",
        exports / f"{safe_name}.gif",
        exports / f"{project_id}.mp4",
    ]
    names = [p.name for p in candidate_names]
    candidates = exact_paths + [directory / name for directory in export_dirs for name in names]
    path = next((p for p in candidates if p.exists()), None)

    # Fallback: mở thư mục exports
    if path is None:
        path = exports if exports.exists() else None
    if path is None:
        raise HTTPException(404, "Chưa có file xuất")

    system = platform.system()
    try:
        if system == "Darwin":
            if path.is_dir():
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["open", "-R", str(path)])
        elif system == "Windows":
            if path.is_dir():
                subprocess.Popen(["explorer", str(path)])
            else:
                subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            target = path if path.is_dir() else path.parent
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "path": str(path.resolve())}
