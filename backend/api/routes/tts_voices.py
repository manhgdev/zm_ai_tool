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

from pipeline.tts.engines import vieneu as vieneu_engine


@router.get("/api/voices")
def api_voices(lang: str = "vi"):
    return list_voices(lang)


@router.get("/api/tts/voices/{voice_id}/preview")
def api_tts_voice_preview(voice_id: str):
    path = vieneu_engine.preview_path(voice_id)
    if not path and str(voice_id).startswith("zmt:"):
        # First preview of an online ZMTTS voice: materialize the demo WAV locally.
        try:
            vieneu_engine._ensure_remote_reference(voice_id)
            path = vieneu_engine.preview_path(voice_id)
        except Exception as exc:
            raise HTTPException(404, str(exc) or "Không tải được audio mẫu") from exc
    if not path:
        raise HTTPException(404, "Giọng này không có audio mẫu")
    suffix = path.suffix.lower()
    media = "audio/wav" if suffix == ".wav" else "audio/mpeg" if suffix == ".mp3" else "application/octet-stream"
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        content_disposition_type="inline",
    )


@router.get("/api/tts/status")
def api_tts_status():
    """TTS Studio — engine status (VieNeu / CapCut / EL / system)."""
    try:
        return engines_status()
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/tts/warm")
def api_tts_warm():
    """Warm-load VieNeu when opening /text-to-speech — non-blocking."""
    import sys

    from pipeline.tts.engines import vieneu as vieneu_engine

    installed = bool(vieneu_engine.available())
    load_state = getattr(vieneu_engine, "_load_state", "cold")
    mode = vieneu_engine.current_mode()

    def _payload(state: str) -> dict:
        return {"ok": True, "loadState": state, "installed": installed, "mode": mode}

    # Already warm / in progress — do not spawn another load thread (UI remount spam).
    if installed and load_state == "ready":
        if getattr(sys, "frozen", False):
            try:
                from pipeline.tts.engines import vieneu_frozen

                if vieneu_frozen.has_ready_worker(mode=mode):
                    return _payload("ready")
            except Exception:
                return _payload("ready")
        else:
            return _payload("ready")
    if installed and load_state == "loading":
        return _payload("loading")

    if installed:
        with vieneu_engine._lock:
            if vieneu_engine._load_state == "cold":
                vieneu_engine._load_state = "loading"

        def _run() -> None:
            try:
                vieneu_engine.warm()
            except Exception:
                pass

        threading.Thread(target=_run, name="tts-warm", daemon=True).start()
    return _payload(getattr(vieneu_engine, "_load_state", "cold"))


class VieNeuModelIn(BaseModel):
    mode: str


@router.post("/api/tts/vieneu/model")
def api_tts_vieneu_model(body: VieNeuModelIn):
    """Select VieNeu model mode (v3turbo | v3nano) and warm-load in background."""
    from pipeline.tts.engines import vieneu as vieneu_engine

    try:
        mode = vieneu_engine.set_mode(body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    def _run() -> None:
        try:
            vieneu_engine.warm()
        except Exception:
            pass

    threading.Thread(target=_run, name="tts-mode-warm", daemon=True).start()
    return {
        "ok": True,
        "mode": mode,
        "model": vieneu_engine.status().get("model"),
        "models": vieneu_engine.model_catalog(selected=mode),
        "loadState": getattr(vieneu_engine, "_load_state", "loading"),
    }

