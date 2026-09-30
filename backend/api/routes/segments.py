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
from pipeline.core.media import filter_complex_args, meta_baked_speed, meta_has_user_bake, video_size
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


class RetranscribeRangeIn(BaseModel):
    start: float
    end: float
    sourceLang: str = "auto"
    engine: str = "whisper"


@router.post("/api/projects/{project_id}/segments/retranscribe-range")
def api_retranscribe_range(project_id: str, body: RetranscribeRangeIn):
    """Run Whisper only on a selected timeline range and atomically replace overlaps.

    Returns 202 immediately — work runs in background thread.
    FE tracks progress via /status (running=True, step=asr).
    """
    from pipeline.asr import asr_whisper
    from pipeline.core.jobs import run_cmd

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    source = Path(str(meta.get("workVideo") or meta.get("videoPath") or ""))
    if not source.is_file():
        raise HTTPException(404, "Không tìm thấy video nguồn")
    duration = max(0.0, ffprobe_duration(source))
    start = max(0.0, min(float(body.start), duration))
    end = max(start, min(float(body.end), duration))
    if not math.isfinite(start) or not math.isfinite(end) or end - start < 0.15:
        raise HTTPException(422, "Vùng nhận dạng phải dài ít nhất 0.15 giây")

    source_lang = body.sourceLang or "auto"
    default_voice = str((meta.get("settings") or {}).get("defaultVoice") or "system")
    cache_dir = ensure_layout(project_id) / "cache"
    set_status(project_id, step="asr", progress=5, message=f"Nhận dạng lại {start:.2f}–{end:.2f}s…", running=True)

    # ponytail: spawn background — asr_whisper blocks 30s–3min
    def _run() -> None:
        wav = cache_dir / f"retranscribe_{start:.3f}_{end:.3f}.wav"
        try:
            run_cmd(project_id, [
                "ffmpeg", "-y", "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
                "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
            ])
            set_status(project_id, step="asr", progress=15, message=f"Đang nhận dạng {start:.2f}–{end:.2f}s…", running=True)
            fresh = asr_whisper(wav, source_lang, workers=0, project_id=project_id)
            for item in fresh:
                item["start"] = start + max(0.0, float(item.get("start") or 0))
                item["end"] = min(end, start + max(0.05, float(item.get("end") or 0)))
                item["translation"] = ""
                item["sourceSubtitle"] = str(item.get("source") or "")
                item["dubSubtitle"] = ""
                item["voice"] = default_voice

            def apply(current: dict) -> list[dict]:
                old = list(current.get("segments") or [])
                kept = [s for s in old if float(s.get("end") or 0) <= start or float(s.get("start") or 0) >= end]
                merged = sorted([*kept, *fresh], key=lambda s: (float(s.get("start") or 0), float(s.get("end") or 0)))
                for index, seg in enumerate(merged):
                    seg["index"] = index
                    seg.setdefault("id", f"seg-{uuid.uuid4().hex[:12]}")
                current["segments"] = merged
                current.pop("timelineBaseline", None)
                return merged

            mutate_meta(project_id, apply)
            set_status(project_id, step="asr", progress=100, message=f"Đã nhận dạng lại {len(fresh)} đoạn", running=False)
            from pipeline.core.project import append_job_event
            append_job_event(project_id, "ASR_CHUNK_READY", {"range": [start, end], "segments": fresh})
        except Exception as exc:
            set_status(project_id, step="asr", progress=0, message=f"Nhận dạng lại lỗi: {exc}", running=False)
        finally:
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass

    import threading as _threading
    _threading.Thread(target=_run, name=f"retranscribe-{project_id[:8]}", daemon=True).start()
    from fastapi.responses import Response
    return Response(status_code=202)


def _validate_segment_editor_fields(body: SegmentIn, meta: dict) -> None:
    if body.videoSpeed is not None:
        if not math.isfinite(body.videoSpeed) or not 0.5 <= body.videoSpeed <= 2.0:
            raise HTTPException(422, "videoSpeed phải nằm trong khoảng 0.5–2.0")
    if body.ttsVolume is not None and (not math.isfinite(body.ttsVolume) or not 0 <= body.ttsVolume <= 200):
        raise HTTPException(422, "ttsVolume phải nằm trong khoảng 0–200")
    if body.ttsSpeed is not None and (not math.isfinite(body.ttsSpeed) or not 0.75 <= body.ttsSpeed <= 1.5):
        raise HTTPException(422, "ttsSpeed phải nằm trong khoảng 0.75–1.5")
    if body.fontSize is not None and body.fontSize != 0 and not 12 <= body.fontSize <= 240:
        raise HTTPException(422, "fontSize phải là 0 (tự động) hoặc 12–240 px")
    if body.bbox is None:
        return
    keys = {"x", "y", "w", "h"}
    if set(body.bbox) != keys:
        raise HTTPException(422, "bbox cần đủ x, y, w, h")
    x, y, bw, bh = (float(body.bbox[key]) for key in ("x", "y", "w", "h"))
    if not all(math.isfinite(value) for value in (x, y, bw, bh)) or bw <= 0 or bh <= 0:
        raise HTTPException(422, "bbox không hợp lệ")
    width, height = video_size(Path(meta["videoPath"]))
    if x < 0 or y < 0 or x + bw > width or y + bh > height:
        raise HTTPException(422, "bbox nằm ngoài khung video")
    body.bbox = {"x": x, "y": y, "w": bw, "h": bh}


@router.get("/api/projects/{project_id}/segments")
def api_segments(project_id: str):
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    return meta.get("segments") or []


@router.put("/api/projects/{project_id}/segments/{seg_id}")
def api_update_segment(project_id: str, seg_id: str, body: SegmentIn):
    def apply(meta: dict) -> dict:
        if not meta:
            raise HTTPException(404)
        _validate_segment_editor_fields(body, meta)
        segs = meta.get("segments") or []
        for i, s in enumerate(segs):
            if s["id"] == seg_id:
                incoming = body.model_dump()
                merged = {**s, **{k: v for k, v in incoming.items() if v is not None}}
                if "bbox" in body.model_fields_set and body.bbox is None:
                    merged.pop("bbox", None)
                if "coverBox" in body.model_fields_set and body.coverBox is None:
                    merged.pop("coverBox", None)
                if "captionBox" in body.model_fields_set and body.captionBox is None:
                    merged.pop("captionBox", None)
                if "captionLayout" in body.model_fields_set and body.captionLayout is None:
                    merged.pop("captionLayout", None)
                if incoming.get("layout") is None and s.get("layout"):
                    merged["layout"] = s["layout"]
                if incoming.get("dub") is None and "dub" in s:
                    merged["dub"] = s["dub"]
                _sync_changed_segment_text(merged, s)
                segs[i] = merged
                meta["segments"] = segs
                meta.pop("timelineBaseline", None)
                return merged
        raise HTTPException(404, "Segment not found")

    return mutate_meta(project_id, apply)


@router.put("/api/projects/{project_id}/segments")
def api_replace_segments(project_id: str, body: list[SegmentIn]):
    """Thay cả list segment (split / duplicate / delete từ editor)."""

    def apply(meta: dict) -> list[dict]:
        if not meta:
            raise HTTPException(404)
        for item in body:
            _validate_segment_editor_fields(item, meta)
            if not math.isfinite(item.start) or not math.isfinite(item.end) or item.end <= item.start:
                raise HTTPException(422, "Thời gian segment không hợp lệ")
        old_by_id = {
            str(s.get("id")): s
            for s in (meta.get("segments") or [])
            if isinstance(s, dict) and s.get("id")
        }
        ordered = sorted(body, key=lambda s: (s.start, s.end, s.id))
        out: list[dict] = []
        # Reset OCR gửi bbox/captionLayout=null — không restore từ prev
        _clearable = ("bbox", "coverBox", "captionBox", "captionLayout", "bboxInherited", "bboxDetected")
        for i, item in enumerate(ordered):
            raw = item.model_dump(exclude_none=False)
            dumped = {k: v for k, v in raw.items() if v is not None}
            dumped["index"] = i
            explicit_clear = {k for k in _clearable if k in raw and raw[k] is None}
            for k in explicit_clear:
                dumped.pop(k, None)
            prev = old_by_id.get(str(dumped.get("id") or ""))
            if prev:
                for k in _SEG_PRESERVE:
                    if k in explicit_clear:
                        continue
                    if k not in dumped and prev.get(k) is not None:
                        dumped[k] = prev[k]
            _sync_changed_segment_text(dumped, prev or {})
            out.append(dumped)
        meta["segments"] = out
        # Edits đổi timeline — baseline bake cũ không còn đúng
        meta.pop("timelineBaseline", None)
        return out

    return mutate_meta(project_id, apply)


def _sync_changed_segment_text(segment: dict, previous: dict) -> None:
    """A new translation invalidates audio/layout, including compound children."""
    if segment.get("source") != previous.get("source"):
        segment["sourceSubtitle"] = str(segment.get("source") or "")
    if segment.get("translation") != previous.get("translation"):
        segment["dubSubtitle"] = str(segment.get("translation") or "")
        for field in ("audioFile", "audioUrl", "audioDuration", "captionLayout"):
            segment.pop(field, None)
    previous_children = {child.get("id"): child for child in previous.get("compoundChildren") or []}
    for child in segment.get("compoundChildren") or []:
        _sync_changed_segment_text(child, previous_children.get(child.get("id")) or {})


@router.post("/api/projects/{project_id}/segments/compound")
def api_create_compound(project_id: str, body: CompoundClipIn):
    """Tạo compound clip (CapCut Alt+G).

    - Giữ từng câu + TTS trong children (relative time)
    - 1 shell trên timeline [t0,t1]; optional 1 WAV mix cho preview
    - Đổi tốc độ bake scale shell — children scale theo, không lệch
    """
    from pipeline.core.jobs import run_cmd

    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    ids = [str(x).strip() for x in (body.segmentIds or []) if str(x).strip()]
    if len(ids) < 2:
        raise HTTPException(400, "Cần chọn ≥2 clip để ghép (Alt+G)")
    segs: list[dict] = list(meta.get("segments") or [])
    by_id = {str(s.get("id")): s for s in segs if isinstance(s, dict) and s.get("id")}
    picked = [dict(by_id[i]) for i in ids if i in by_id]
    if len(picked) < 2:
        raise HTTPException(400, "Không tìm thấy đủ clip đã chọn")

    ordered = sorted(picked, key=lambda s: (float(s.get("start") or 0), float(s.get("end") or 0)))
    t0 = float(ordered[0].get("start") or 0)
    t1 = max(float(s.get("end") or 0) for s in ordered)
    if t1 <= t0:
        t1 = t0 + 0.15
    span = t1 - t0

    # Children: thời gian tương đối trong compound (0 = đầu shell)
    children: list[dict] = []
    for s in ordered:
        ch = dict(s)
        st = float(ch.get("start") or 0)
        en = float(ch.get("end") or st)
        ch["start"] = max(0.0, st - t0)
        ch["end"] = max(ch["start"] + 0.05, en - t0)
        if ch.get("coverStart") is not None:
            try:
                ch["coverStart"] = max(0.0, float(ch["coverStart"]) - t0)
            except (TypeError, ValueError):
                pass
        if ch.get("coverEnd") is not None:
            try:
                ch["coverEnd"] = max(0.0, float(ch["coverEnd"]) - t0)
            except (TypeError, ValueError):
                pass
        ch.pop("groupId", None)
        children.append(ch)

    root = ensure_layout(project_id)
    tts_dir = root / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    # Mix TTS theo timeline compound (delay = start relative) — 1 file preview/export
    clips: list[tuple[Path, float]] = []
    from pipeline.core.config import safe_child

    for ch in children:
        name = str(ch.get("audioFile") or f"{ch.get('id')}.wav")
        # audioFile đến từ client — chặn traversal trước khi đọc/mix
        p = safe_child(tts_dir, name)
        if p is not None and p.is_file() and p.stat().st_size > 64:
            clips.append((p, float(ch.get("start") or 0)))

    shell_id = f"cmp_{uuid.uuid4().hex[:12]}"
    mixed_name = f"{shell_id}.wav"
    mixed_path = tts_dir / mixed_name
    audio_dur = span
    has_mix = False
    if clips:
        inputs: list[str] = []
        filters: list[str] = []
        labels: list[str] = []
        for i, (wav, st) in enumerate(clips):
            inputs += ["-i", str(wav)]
            delay_ms = max(0, int(round(st * 1000)))
            filters.append(
                f"[{i}:a]aformat=sample_rates=44100:channel_layouts=mono,"
                f"adelay={delay_ms}|{delay_ms},apad[a{i}]"
            )
            labels.append(f"[a{i}]")
        n = len(labels)
        filters.append(
            "".join(labels)
            + f"amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0,"
            f"atrim=0:{span:.3f},asetpts=PTS-STARTPTS[aout]"
        )
        fc = root / "cache" / f"cmp_{shell_id}_fc.txt"
        fc.parent.mkdir(parents=True, exist_ok=True)
        fc.write_text(";\n".join(filters) + "\n", encoding="utf-8")
        try:
            run_cmd(
                project_id,
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    *inputs,
                    *filter_complex_args(fc.read_text(encoding="utf-8")),
                    "-map", "[aout]", "-c:a", "pcm_s16le", str(mixed_path),
                ],
            )
            has_mix = mixed_path.is_file() and mixed_path.stat().st_size > 64
            if has_mix:
                audio_dur = float(ffprobe_duration(mixed_path) or span)
        except Exception:
            has_mix = False
        finally:
            try:
                fc.unlink(missing_ok=True)
            except OSError:
                pass

    # Shell meta only — timeline UI ẩn caption/TTS (CapCut Alt+G: chỉ còn video)
    n_ch = len(children)
    shell: dict[str, Any] = {
        "id": shell_id,
        "index": 0,
        "start": t0,
        "end": t1,
        "source": f"[Compound ×{n_ch}]",
        "translation": "",
        "voice": str(ordered[0].get("voice") or ""),
        "layout": str(ordered[0].get("layout") or "horizontal"),
        "dub": True if has_mix else bool(ordered[0].get("dub")),
        "isCompound": True,
        "compoundChildren": children,
        "coverStart": t0,
        "coverEnd": t1,
        "captionLayout": None,
        "videoSpeed": 1.0,
        "groupId": None,
    }
    if has_mix:
        shell["audioFile"] = mixed_name
        shell["audioUrl"] = f"/api/projects/{project_id}/tts/{mixed_name}?t={int(audio_dur * 1000)}"
        shell["audioDuration"] = audio_dur

    drop_ids = {str(s.get("id")) for s in ordered}
    next_segs: list[dict] = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        if str(s.get("id") or "") in drop_ids:
            continue
        next_segs.append(s)
    next_segs.append(shell)
    next_segs.sort(key=lambda s: (float(s.get("start") or 0), float(s.get("end") or 0)))
    for i, s in enumerate(next_segs):
        s["index"] = i

    meta["segments"] = next_segs
    # Job dài (ffmpeg amix) — chỉ ghi segments + baseline, không nuốt thay đổi khác
    apply_meta_patch(project_id, {"segments": next_segs}, remove=("timelineBaseline",))
    return {
        "ok": True,
        "mode": "compound",
        "compoundId": shell_id,
        "mergedId": shell_id,
        "start": t0,
        "end": t1,
        "childCount": n_ch,
        "audioFile": shell.get("audioFile"),
        "audioUrl": shell.get("audioUrl"),
        "audioDuration": shell.get("audioDuration"),
        "segments": next_segs,
    }


@router.post("/api/projects/{project_id}/segments/{seg_id}/uncompound")
def api_uncompound(project_id: str, seg_id: str):
    """Tháo compound (CapCut ungroup compound) — restore children ra timeline."""
    meta = load_meta(project_id)
    if not meta:
        raise HTTPException(404)
    segs: list[dict] = list(meta.get("segments") or [])
    shell = next((s for s in segs if isinstance(s, dict) and str(s.get("id")) == seg_id), None)
    if not shell or not shell.get("isCompound"):
        raise HTTPException(400, "Không phải compound clip")
    children = shell.get("compoundChildren") or []
    if not isinstance(children, list) or not children:
        raise HTTPException(400, "Compound không có children")
    t0 = float(shell.get("start") or 0)
    restored: list[dict] = []
    for ch in children:
        if not isinstance(ch, dict):
            continue
        item = dict(ch)
        st = float(item.get("start") or 0)
        en = float(item.get("end") or st)
        item["start"] = t0 + st
        item["end"] = t0 + en
        if item.get("coverStart") is not None:
            try:
                item["coverStart"] = t0 + float(item["coverStart"])
            except (TypeError, ValueError):
                pass
        if item.get("coverEnd") is not None:
            try:
                item["coverEnd"] = t0 + float(item["coverEnd"])
            except (TypeError, ValueError):
                pass
        item.pop("isCompound", None)
        item.pop("compoundChildren", None)
        restored.append(item)

    next_segs = [s for s in segs if str(s.get("id")) != seg_id]
    next_segs.extend(restored)
    next_segs.sort(key=lambda s: (float(s.get("start") or 0), float(s.get("end") or 0)))
    for i, s in enumerate(next_segs):
        if isinstance(s, dict):
            s["index"] = i
    meta["segments"] = next_segs
    apply_meta_patch(project_id, {"segments": next_segs}, remove=("timelineBaseline",))
    return {"ok": True, "segments": next_segs, "restored": len(restored)}
