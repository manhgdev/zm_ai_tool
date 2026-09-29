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

from pipeline.translate import translate_segments


@router.post("/api/projects/{project_id}/segments/{seg_id}/preview-tts")
def api_preview_tts(project_id: str, seg_id: str, body: PreviewTtsIn):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Thiếu nội dung để đọc")
    settings = meta.get("settings") or {}
    lang = body.lang or settings.get("targetLang") or "vi"
    speed = min(2.0, max(0.5, float(body.speed)))
    root = ensure_layout(project_id)
    try:
        key = tts_cache_key(text, body.voice or "system", lang, f"none|speed={speed:g}")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    name = f"{key}.wav"
    wav = root / "tts" / name
    try:
        if wav.exists():
            dur = ffprobe_duration(wav)
        else:
            dur = _tts_segment_safe(text, body.voice or "system", wav, lang, speed)
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return {
        "audioUrl": f"/api/projects/{project_id}/tts/{name}?t={int(dur * 1000)}",
        "duration": dur,
    }


def _tts_segment_safe(text: str, voice: str, wav: Path, lang: str, speed: float) -> float:
    """Gọi tts_segment — trong frozen app chạy qua subprocess .venv-runtime để có torch."""
    import sys
    from pipeline.tts.manager import clone_route
    if clone_route(voice, lang) or not getattr(sys, "frozen", False):
        return tts_segment(text, voice, wav, None, "none", lang=lang, speed=speed)
    # ponytail: frozen app — torch/model chỉ có trong .venv-runtime, không trong bundle
    import json as _json
    import subprocess
    import tempfile
    from api.job_spawn import _job_python, _worker_environment
    from pathlib import Path as _Path
    _WORKER = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "p = json.loads(Path(sys.argv[1]).read_text())\n"
        "from pipeline.tts.manager import tts_segment\n"
        "dur = tts_segment(p['text'], p['voice'], Path(p['wav']), None, 'none', lang=p['lang'], speed=p['speed'])\n"
        "print(dur)\n"
    )
    try:
        py = _job_python()
    except RuntimeError as e:
        raise RuntimeError(f"Thiếu .venv-runtime — vào Thiết lập → Cài gói AI\n{e}") from e
    backend = _Path(__file__).resolve().parent.parent.parent
    with tempfile.TemporaryDirectory(prefix="vc-tts-") as td:
        td = _Path(td)
        payload = td / "p.json"
        worker = td / "w.py"
        payload.write_text(_json.dumps({"text": text, "voice": voice, "wav": str(wav), "lang": lang, "speed": speed}), encoding="utf-8")
        worker.write_text(_WORKER, encoding="utf-8")
        env = _worker_environment(backend)
        kw: dict = {"cwd": str(backend), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if sys.platform == "win32":
            kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        proc = subprocess.run([py, str(worker), str(payload)], **kw, timeout=60)
        if proc.returncode:
            err = (proc.stderr or b"").decode("utf-8", "replace")[-800:]
            raise RuntimeError(f"TTS subprocess lỗi (exit {proc.returncode}): {err}")
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        try:
            return float(out.splitlines()[-1])
        except (ValueError, IndexError):
            return float(ffprobe_duration(wav) or 0)


@router.post("/api/projects/{project_id}/segments/{seg_id}/retranslate")
def api_retranslate(project_id: str, seg_id: str, body: RetranslateIn):
    """Dịch lại 1 đoạn (không chạy pipeline full)."""
    from pipeline.translate import translate_segments

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    segs = meta.get("segments") or []
    seg = next((s for s in segs if s.get("id") == seg_id), None)
    if not seg:
        raise HTTPException(404, "Segment not found")
    settings = meta.get("settings") or {}
    source = (body.text or seg.get("source") or "").strip()
    if not source:
        raise HTTPException(400, "Thiếu chữ nguồn")
    target = body.targetLang or settings.get("targetLang") or "vi"
    if target in ("none", "off", "source", ""):
        # Giữ nguyên chữ nguồn — không gọi máy dịch
        tr = source
        seg["translation"] = tr
        seg.pop("audioFile", None)
        seg.pop("audioUrl", None)
        seg.pop("audioDuration", None)
        meta["segments"] = segs
        save_meta(project_id, meta)
        return {"translation": tr, "segment": seg}
    src_lang = body.sourceLang or settings.get("sourceLang") or "auto"
    eng = body.translator or settings.get("translator") or "google"
    try:
        out = translate_segments(
            [source],
            target,
            project_id=None,
            source_lang=src_lang,
            translator=str(eng),
            workers=1,
            ollama_mode=str(body.ollamaMode or settings.get("ollamaMode") or "cloud"),
            ollama_model=str(
                body.ollamaModel or settings.get("ollamaModel") or "minimax-m3:cloud"
            ),
            ollama_local_tier=str(
                body.ollamaLocalTier or settings.get("ollamaLocalTier") or "balanced"
            ),
            durations=[max(0.2, float(seg.get("end") or 0) - float(seg.get("start") or 0))],
        )
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    tr = (out[0] if out else "").strip() or source
    seg["translation"] = tr
    # invalidate TTS cache fields — user nghe lại sẽ gen mới
    seg.pop("audioFile", None)
    seg.pop("audioUrl", None)
    seg.pop("audioDuration", None)
    meta["segments"] = segs
    save_meta(project_id, meta)
    return {"translation": tr, "segment": seg}


@router.get("/api/projects/{project_id}/tts/{name}")
def api_tts(project_id: str, name: str):
    from pipeline.core.config import safe_child

    path = safe_child(ensure_layout(project_id) / "tts", name)
    if path is None or not path.is_file():
        raise HTTPException(404)
    st = path.stat()
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={
            "Cache-Control": "private, max-age=60",
            "ETag": f'"{st.st_mtime_ns:x}-{st.st_size:x}"',
        },
    )
