"""Domain API routes."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
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

from pipeline.tts import voice_store
from pipeline.tts.engines import vieneu as vieneu_engine
from pipeline.tts.voice_store import TTS_OUTPUT, ensure_vieneu_dirs


@router.post("/api/tts/studio/synthesize")
def api_tts_studio_synth(body: StudioSynthIn):
    """TTS Studio: text or SRT batch → data/tts_output/{jobId}/.

    Trả job_id ngay, chạy nền — FE poll /jobs/{job_id}/progress.
    ponytail: synth_srt_job mất vài phút, không block uvicorn.
    """
    import threading as _threading
    import uuid as _uuid
    from pipeline.tts.studio import set_job_complete, set_job_error, set_job_progress, synth_srt_job, synth_text_job

    srt_text = (body.srtText or "").strip()
    text = (body.text or "").strip()
    if not srt_text and not text:
        raise HTTPException(400, "Thiếu nội dung hoặc SRT")
    selected_voice = body.speaker_id or body.voice or "system"
    # Pre-generate job_id để FE có thể poll progress ngay
    job_id = body.jobId or _uuid.uuid4().hex[:12]
    set_job_progress(job_id, 1, 100, "Đang khởi tạo TTS…")

    def _run() -> None:
        try:
            if srt_text:
                result = synth_srt_job(
                    srt_text=srt_text,
                    voice=selected_voice,
                    lang=body.lang or "vi",
                    speed=float(body.speed or 1.0),
                    volume=float(body.volume or 1.0),
                    pitch=float(body.pitch or 0.0),
                    style=body.style or "tu_nhien",
                    match_duration=body.matchDuration or "natural",
                    keep_timeline=bool(body.keepTimeline),
                    title=body.title or "",
                    gap_ms=int(body.gapMs or 0),
                    job_id=job_id,
                )
            else:
                result = synth_text_job(
                    text=text,
                    voice=selected_voice,
                    lang=body.lang or "vi",
                    speed=float(body.speed or 1.0),
                    volume=float(body.volume or 1.0),
                    pitch=float(body.pitch or 0.0),
                    style=body.style or "tu_nhien",
                    match_duration=body.matchDuration or "none",
                    title=body.title or "",
                    auto_split=bool(body.autoSplit),
                    gap_ms=int(body.gapMs or 0),
                    job_id=job_id,
                )
            jid = result.get("id") or result.get("job_id")
            if jid:
                try:
                    from pipeline.tts.studio import publish_job_outputs
                    publish_job_outputs(str(jid), body.outputDir, body.outputFormat)
                except Exception as pub_err:
                    import logging
                    logging.getLogger(__name__).warning("publish_job_outputs failed: %s", pub_err)
            # Cache hits return an older completed job id. The browser still
            # polls the new request id, so publish its terminal state too.
            set_job_complete(job_id, result_job_id=str(jid or job_id))
        except Exception as e:
            set_job_error(job_id, e)
            import logging
            logging.getLogger(__name__).error("tts_studio_synth background error: %s", e)

    _threading.Thread(target=_run, name=f"studio-synth-{job_id[:8]}", daemon=True).start()
    # Trả job_id ngay — FE dùng GET /jobs/{job_id}/progress để theo dõi
    return {"id": job_id, "job_id": job_id, "running": True}


@router.get("/api/tts/studio/history")
def api_tts_studio_history():
    from pipeline.tts.studio import list_history

    return list_history(50)


@router.get("/api/tts/studio/jobs/{job_id}/progress")
def api_tts_studio_job_progress(job_id: str):
    from pipeline.tts.studio import get_job_progress

    return get_job_progress(job_id)


def _job_dir(job_id: str) -> Path:
    """Thư mục job đã kiểm traversal — job_id đến từ URL path param."""
    from pipeline.core.config import safe_child

    path = safe_child(TTS_OUTPUT, job_id)
    if path is None:
        raise HTTPException(400, "job_id không hợp lệ")
    return path


def _tts_job_artifact(job_id: str, kind: str, style: str = "hard") -> Path:
    """Build and return one TTS output artifact for desktop reveal actions."""
    from pipeline.export.srt import SRT_STYLES
    from pipeline.tts.studio import ensure_mp3, ensure_wav, ensure_zip, rebuild_srt

    try:
        from pipeline.tts.studio import published_job_output_dir

        published_dir = published_job_output_dir(job_id)

        if kind == "wav":
            target = published_dir / "audio.wav"
            if target.is_file():
                return target
            return ensure_wav(job_id)
        if kind == "mp3":
            target = published_dir / "audio.mp3"
            if target.is_file():
                return target
            return ensure_mp3(job_id)
        if style not in SRT_STYLES:
            raise HTTPException(400, f"style phải là một trong: {', '.join(SRT_STYLES)}")
        if kind == "srt":
            target = published_dir / ("subtitles.srt" if style == "hard" else f"subtitles_{style}.srt")
            if not target.is_file():
                shutil.copy2(rebuild_srt(job_id, style), target)
            return target
        if kind == "zip":
            target = published_dir / ("bundle.zip" if style == "hard" else f"bundle_{style}.zip")
            if not target.is_file():
                shutil.copy2(ensure_zip(job_id, srt_style=style), target)
            return target
    except Exception:
        pass

    if kind == "wav":
        return ensure_wav(job_id)
    if kind == "mp3":
        return ensure_mp3(job_id)
    if style not in SRT_STYLES:
        raise HTTPException(400, f"style phải là một trong: {', '.join(SRT_STYLES)}")
    if kind == "srt":
        return rebuild_srt(job_id, style)
    if kind == "zip":
        return ensure_zip(job_id, srt_style=style)
    raise HTTPException(400, "Loại file TTS không hợp lệ")


def _reveal_local_file(path: Path) -> None:
    """Select the artifact in Finder/Explorer, or open its directory on Linux."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/api/tts/studio/jobs/{job_id}/reveal/{kind}")
def api_tts_studio_reveal(job_id: str, kind: str, style: str = "hard"):
    """Desktop app: create the requested artifact then reveal it locally."""
    try:
        path = _tts_job_artifact(job_id, kind, style)
    except FileNotFoundError:
        raise HTTPException(404, "Không thấy kết quả TTS") from None
    _reveal_local_file(path)
    return {"ok": True, "path": str(path.resolve()), "kind": kind}


@router.get("/api/tts/studio/jobs/{job_id}/audio.wav")
def api_tts_studio_audio(job_id: str, download: int = 0):
    """Phát inline (mặc định). ?download=1 → tải file."""
    from pipeline.tts.studio import ensure_wav

    try:
        path = ensure_wav(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Không thấy audio")
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"{job_id}.wav",
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/api/tts/studio/jobs/{job_id}/audio.mp3")
def api_tts_studio_mp3(job_id: str, download: int = 0):
    from pipeline.tts.studio import ensure_mp3

    _job_dir(job_id)
    try:
        path = ensure_mp3(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Không thấy audio") from None
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"{job_id}.mp3",
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/api/tts/studio/jobs/{job_id}/subs.srt")
def api_tts_studio_srt(job_id: str, style: str = "hard"):
    from pipeline.export.srt import SRT_STYLES
    from pipeline.tts.studio import rebuild_srt

    if style not in SRT_STYLES:
        raise HTTPException(400, f"style phải là một trong: {', '.join(SRT_STYLES)}")
    job_dir = _job_dir(job_id)
    if style == "hard":
        path = job_dir / "subs.srt"
        if not path.is_file():
            try:
                from pipeline.tts.studio import ensure_zip
                ensure_zip(job_id)
            except Exception:
                pass
            path = job_dir / "subs.srt"
    else:
        try:
            path = rebuild_srt(job_id, style)
        except FileNotFoundError:
            raise HTTPException(404, "Không thấy job") from None
    if not path.is_file():
        raise HTTPException(404, "Không thấy SRT")
    return FileResponse(path, media_type="application/x-subrip", filename=f"{job_id}.srt")


@router.get("/api/tts/studio/jobs/{job_id}/bundle.zip")
def api_tts_studio_zip(job_id: str, style: str = "hard"):
    from pipeline.export.srt import SRT_STYLES
    from pipeline.tts.studio import ensure_zip

    if style not in SRT_STYLES:
        raise HTTPException(400, f"style phải là một trong: {', '.join(SRT_STYLES)}")
    try:
        path = ensure_zip(job_id, srt_style=style)
    except FileNotFoundError:
        raise HTTPException(404, "Không thấy job") from None
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return FileResponse(path, media_type="application/zip", filename=f"{job_id}.zip")


@router.post("/api/tts/studio/jobs/{job_id}/cancel")
def api_tts_studio_cancel(job_id: str):
    from pipeline.tts.studio import request_cancel

    ok = request_cancel(job_id)
    return {"ok": True, "cancelled": ok}


@router.delete("/api/tts/studio/jobs/{job_id}")
def api_tts_studio_delete(job_id: str):
    path = _job_dir(job_id)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    return {"ok": True}


@router.post("/api/tts/studio/clone")
async def api_tts_studio_clone(
    name: str = "",
    transcript: str = "",
    tags: str = "[]",
    file: UploadFile = File(...),
):
    """Clone giọng VieNeu từ file ref (3–8s).

    ponytail: clone_voice blocking → dùng asyncio.to_thread để không block event loop.
    """
    import asyncio
    if not name.strip():
        raise HTTPException(400, "Thiếu tên giọng")
    try:
        parsed_tags = json.loads(tags)
        clean_tags = voice_store.normalize_voice_tags(parsed_tags, strict=True)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    ensure_vieneu_dirs()
    ext = Path(file.filename or "ref.wav").suffix or ".wav"
    tmp = DATA / f"_clone_{uuid.uuid4().hex}{ext}"
    try:
        with tmp.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        voice = await asyncio.to_thread(
            vieneu_engine.clone_voice,
            name.strip(),
            tmp,
            transcript=(transcript or "").strip(),
            tags=clean_tags,
        )
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    finally:
        tmp.unlink(missing_ok=True)
    return voice


def _studio_voice_payload(entry: dict) -> dict:
    eid = str(entry.get("id") or "")
    name = str(entry.get("name") or eid)
    eng = str(entry.get("engine") or "")
    tags = voice_store.normalize_voice_tags(entry.get("tags"))
    language = voice_store.normalize_voice_language(entry.get("language"))
    favorite = bool(entry.get("favorite"))
    if eng == "clone" or eid.startswith("vn:clone:"):
        cid = eid.removeprefix("vn:clone:")
        return {
            "id": f"vn:clone:{cid}",
            "name": f"VieNeu · Clone · {name}",
            "engine": "clone",
            "tags": tags,
            "language": language,
            "favorite": favorite,
        }
    return {
        "id": eid,
        "name": name,
        "engine": "zmai",
        "type": "zmAI",
        "tags": tags,
        "language": language,
        "favorite": favorite,
    }


@router.post("/api/tts/studio/voices/bulk-move")
def api_tts_studio_voices_bulk_move(body: VoiceBulkMoveIn):
    """Chuyển nhiều giọng zmAI ↔ clone trong một request."""
    from pipeline.tts import voice_store

    target = (body.target or "").strip().lower()
    if target not in ("zmai", "clone"):
        raise HTTPException(400, "target phải là 'zmai' hoặc 'clone'")
    if not body.voiceIds:
        raise HTTPException(400, "Cần ít nhất một voiceId")
    if len(body.voiceIds) > 200:
        raise HTTPException(400, "Tối đa 200 voiceId mỗi lần")

    voice_ids = [voice_id.strip() for voice_id in body.voiceIds]
    if any(not voice_id for voice_id in voice_ids):
        raise HTTPException(400, "voiceId không được trống")
    # Không chạy cùng một phép chuyển hai lần nếu client gửi ID trùng.
    voice_ids = list(dict.fromkeys(voice_ids))
    result = voice_store.move_voice_engines(voice_ids, target)
    return {
        "target": target,
        "successes": [
            {"voiceId": item["voiceId"], "voice": _studio_voice_payload(item["voice"])}
            for item in result["successes"]
        ],
        "failures": result["failures"],
    }


@router.patch("/api/tts/studio/clone/{voice_id}")
def api_tts_studio_clone_rename(voice_id: str, body: CloneRenameIn):
    from pipeline.tts import voice_store

    cid = voice_id.removeprefix("vn:clone:").strip()
    if not cid:
        raise HTTPException(400, "Thiếu id giọng")
    entry = voice_store.rename_cloned(cid, (body.name or "").strip())
    if not entry:
        raise HTTPException(404, "Không tìm thấy giọng clone")
    return {
        "id": f"vn:clone:{entry['id']}",
        "name": f"VieNeu · Clone · {entry['name']}",
    }


@router.delete("/api/tts/studio/clone/{voice_id}")
def api_tts_studio_clone_delete(voice_id: str):
    from pipeline.tts import voice_store

    cid = voice_id.removeprefix("vn:clone:").strip()
    if not cid:
        raise HTTPException(400, "Thiếu id giọng")
    if not voice_store.remove_cloned(cid):
        raise HTTPException(404, "Không tìm thấy giọng clone")
    return {"ok": True}


@router.patch("/api/tts/studio/voices/{voice_id:path}")
def api_tts_studio_voice_patch(voice_id: str, body: VoicePatchIn):
    """Đổi tên và/hoặc chuyển engine (zmAI ↔ clone)."""
    from pipeline.tts import voice_store

    vid = (voice_id or "").strip()
    if not vid:
        raise HTTPException(400, "Thiếu id giọng")

    name = (body.name or "").strip() if body.name is not None else None
    engine = (body.engine or "").strip().lower() if body.engine is not None else None
    tags_supplied = "tags" in body.model_fields_set
    language_supplied = "language" in body.model_fields_set
    favorite_supplied = "favorite" in body.model_fields_set
    favorite = bool(body.favorite) if favorite_supplied else None
    try:
        tags = voice_store.normalize_voice_tags(body.tags, strict=True) if tags_supplied else None
        language = (
            voice_store.normalize_voice_language(body.language, strict=True)
            if language_supplied
            else None
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if (
        name is None
        and not engine
        and not tags_supplied
        and not language_supplied
        and not favorite_supplied
    ):
        raise HTTPException(400, "Cần name, tags, language, favorite và/hoặc engine")
    if body.name is not None and not name:
        raise HTTPException(400, "Tên giọng không được trống")

    try:
        if engine:
            entry = voice_store.move_voice_engine(vid, engine)
            # Metadata update after moving because the id may change.
            new_id = str(entry["id"])
            if name is not None or tags_supplied or language_supplied or favorite_supplied:
                if new_id.startswith("vn:clone:") or entry.get("engine") == "clone":
                    updated = voice_store.update_cloned(
                        new_id.removeprefix("vn:clone:"),
                        name=name,
                        tags=tags,
                        language=language,
                        favorite=favorite,
                    )
                    if updated:
                        entry = {**updated, "id": f"vn:clone:{updated['id']}", "engine": "clone"}
                else:
                    updated = voice_store.update_reference(
                        new_id,
                        name=name,
                        tags=tags,
                        language=language,
                        favorite=favorite,
                    )
                    if updated:
                        entry = {**updated, "engine": "zmai", "type": "zmAI"}
            return _studio_voice_payload(entry)

        # metadata only
        if vid.startswith("vn:clone:"):
            cid = vid.removeprefix("vn:clone:").strip()
            entry = voice_store.update_cloned(
                cid, name=name, tags=tags, language=language, favorite=favorite
            )
            if not entry:
                raise HTTPException(404, "Không tìm thấy giọng clone")
            return _studio_voice_payload({**entry, "id": f"vn:clone:{entry['id']}", "engine": "clone"})

        entry = voice_store.update_reference(
            vid, name=name, tags=tags, language=language, favorite=favorite
        )
        if entry:
            return _studio_voice_payload({**entry, "engine": "zmai"})
        # fallback: bare clone id
        entry = voice_store.update_cloned(
            vid, name=name, tags=tags, language=language, favorite=favorite
        )
        if not entry:
            raise HTTPException(404, "Không tìm thấy giọng")
        return _studio_voice_payload({**entry, "id": f"vn:clone:{entry['id']}", "engine": "clone"})
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.put("/api/tts/studio/voices/{voice_id:path}/audio")
async def api_tts_studio_voice_audio(voice_id: str, file: UploadFile = File(...)):
    """Replace a local voice reference, normalizing the upload to mono WAV."""
    vid = (voice_id or "").strip()
    if not vid:
        raise HTTPException(400, "Thiếu id giọng")
    ext = Path(file.filename or "voice.wav").suffix or ".wav"
    source = DATA / f"_voice_source_{uuid.uuid4().hex}{ext}"
    normalized = DATA / f"_voice_source_{uuid.uuid4().hex}.wav"
    try:
        with source.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(source), "-ac", "1", "-ar", "48000", str(normalized)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        entry = voice_store.replace_voice_audio(vid, normalized)
        return {"ok": True, "id": vid, "name": entry.get("name") or vid}
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError) as e:
        raise HTTPException(400, "File audio không hợp lệ hoặc FFmpeg chưa sẵn sàng") from e
    finally:
        source.unlink(missing_ok=True)
        normalized.unlink(missing_ok=True)


@router.delete("/api/tts/studio/voices/{voice_id:path}")
def api_tts_studio_voice_delete(voice_id: str):
    """Xóa giọng clone (hard) hoặc ẩn giọng zmAI (soft)."""
    from pipeline.tts import voice_store

    vid = (voice_id or "").strip()
    if not vid:
        raise HTTPException(400, "Thiếu id giọng")
    if vid.startswith("vn:clone:"):
        cid = vid.removeprefix("vn:clone:").strip()
        if not voice_store.remove_cloned(cid):
            raise HTTPException(404, "Không tìm thấy giọng clone")
        return {"ok": True}
    if voice_store.remove_reference(vid):
        return {"ok": True}
    if voice_store.remove_cloned(vid):
        return {"ok": True}
    raise HTTPException(404, "Không tìm thấy giọng")
