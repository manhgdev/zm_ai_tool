"""TTS Studio jobs — synthesize text/SRT, export mp3/zip, cancel."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.media import ffprobe_duration
from ..core.output_paths import item_output_folder, selected_or_default
from ..export.srt import SRT_STYLES, cues_from_parts, parse_srt, style_params, write_srt, _split_for_style, wrap_capcut_text
from . import audio_utils
from .engines.vieneu import parse_voice as parse_vieneu_voice
from .engines.vieneu import reference_cache_token, reset_client as reset_vieneu_client
from .manager import list_voices, tts_segment
from .text_split import split_sentences
from .voice_store import TTS_OUTPUT, TTS_TEMP, ensure_vieneu_dirs


def _job_dir(job_id: str) -> Path:
    """Thư mục job đã kiểm traversal — job_id có thể đến từ body.jobId của client."""
    from pipeline.core.config import safe_child

    path = safe_child(TTS_OUTPUT, job_id)
    if path is None:
        raise ValueError(f"job_id không hợp lệ: {job_id!r}")
    return path



def _job_fingerprint(
    *,
    text: str,
    srt_text: str,
    voice: str,
    lang: str,
    speed: float,
    volume: float,
    pitch: float,
    style: str,
    match_duration: str,
    keep_timeline: bool,
    auto_split: bool,
    gap_ms: int,
    normalize: bool = False,
    trim_silence: bool = False,
) -> str:
    """Same voice + text + settings → reuse job (không tạo lịch sử trùng)."""
    import hashlib

    raw = "|".join(
        [
            "v9",  # zmAI/clone: infer()+no ref_codes (babble guard; read only typed text)
            (text or "").strip(),
            (srt_text or "").strip(),
            (voice or "").strip(),
            reference_cache_token(voice or ""),
            (lang or "vi").strip(),
            f"{float(speed):.4f}",
            f"{float(volume):.4f}",
            f"{float(pitch):.2f}",
            (style or "tu_nhien").strip(),
            (match_duration or "none").strip(),
            "1" if keep_timeline else "0",
            "1" if auto_split else "0",
            str(int(gap_ms or 0)),
            "1" if normalize else "0",
            "1" if trim_silence else "0",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _find_cached_job(fp: str) -> dict[str, Any] | None:
    ensure_vieneu_dirs()
    if not TTS_OUTPUT.is_dir():
        return None
    for d in sorted(TTS_OUTPUT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        meta_p = d / "meta.json"
        wav = d / "audio.wav"
        if not meta_p.is_file() or not wav.is_file() or wav.stat().st_size < 64:
            continue
        try:
            m = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("fingerprint") != fp:
            continue
        jid = d.name
        m["id"] = jid
        return {
            "id": jid,
            "duration": float(m.get("duration") or ffprobe_duration(wav)),
            "audioUrl": f"/api/tts/studio/jobs/{jid}/audio.wav",
            "mp3Url": f"/api/tts/studio/jobs/{jid}/audio.mp3",
            "srtUrl": f"/api/tts/studio/jobs/{jid}/subs.srt",
            "zipUrl": f"/api/tts/studio/jobs/{jid}/bundle.zip",
            "meta": m,
            "cached": True,
        }
    return None


def _voice_display(voice: str, lang: str = "vi") -> str:
    """Human name for history (CapCut display_name, VieNeu label…)."""
    try:
        for v in list_voices(lang):
            if v.get("id") == voice:
                name = str(v.get("name") or voice)
                for p in ("CapCut · ", "VieNeu · Clone · ", "VieNeu · ", "ElevenLabs · ", "macOS · "):
                    if name.startswith(p):
                        name = name[len(p) :]
                return name
    except Exception:
        pass
    if voice.startswith("vn:clone:"):
        return voice[9:]
    if voice.startswith(("vn:", "cc:", "el:")):
        return voice.split(":", 1)[-1] if voice.startswith("el:") else voice.split(":", 1)[-1]
    # cc:type:rid → last not ideal; strip cc:
    if voice.startswith("cc:"):
        return voice[3:].split(":")[0]
    return voice

_jobs_lock = threading.Lock()
_cancel_flags: dict[str, bool] = {}
_running: dict[str, bool] = {}
_job_progress: dict[str, dict[str, Any]] = {}


def set_job_progress(job_id: str, current: int, total: int, message: str = "") -> None:
    """Cập nhật tiến độ phần trăm và số lượng thực tế cho job đang xử lý."""
    if not job_id:
        return
    with _jobs_lock:
        total_safe = max(1, total)
        current_safe = max(0, min(current, total_safe))
        pct = int(min(99, max(1, (current_safe / total_safe) * 100)))
        _job_progress[job_id] = {
            "current": current_safe,
            "total": total_safe,
            "pct": pct,
            "message": message or f"Đang xử lý {current_safe}/{total_safe} ({pct}%)",
        }


def set_job_progress_pct(
    job_id: str,
    pct: float,
    message: str = "",
    *,
    current: float | None = None,
    total: int | None = None,
) -> None:
    """Publish an absolute progress percent (1–99) without inventing fake ticks."""
    if not job_id:
        return
    with _jobs_lock:
        prev = dict(_job_progress.get(job_id) or {})
        total_safe = max(1, int(total if total is not None else prev.get("total") or 1))
        if current is None:
            current_safe = float(prev.get("current") or 0)
        else:
            current_safe = max(0.0, min(float(current), float(total_safe)))
        pct_safe = int(min(99, max(1, round(pct))))
        # Never move backwards within the same job (parallel chunks race).
        prev_pct = int(prev.get("pct") or 0)
        if pct_safe < prev_pct:
            pct_safe = prev_pct
        _job_progress[job_id] = {
            "current": current_safe,
            "total": total_safe,
            "pct": pct_safe,
            "message": message or str(prev.get("message") or f"Đang xử lý ({pct_safe}%)"),
        }


def get_job_progress(job_id: str) -> dict[str, Any]:
    """Lấy tiến độ thực tế hiện tại của job."""
    if not job_id:
        return {"current": 0, "total": 0, "pct": 0, "message": "", "done": False, "running": False}
    with _jobs_lock:
        prog = dict(_job_progress.get(
            job_id,
            {"current": 0, "total": 0, "pct": 0, "message": ""},
        ))
        is_running = job_id in _running
    prog["running"] = is_running
    prog["done"] = bool(prog.get("error")) or (not is_running and prog.get("pct", 0) >= 99)
    return prog


def set_job_error(job_id: str, error: Exception | str, *, message: str = "Tạo giọng thất bại.") -> None:
    """Keep a terminal error for the UI instead of silently losing a worker failure."""
    if not job_id:
        return
    with _jobs_lock:
        current = dict(_job_progress.get(job_id) or {})
        _job_progress[job_id] = {
            "current": int(current.get("current") or 0),
            "total": max(1, int(current.get("total") or 1)),
            "pct": max(1, min(99, int(current.get("pct") or 1))),
            "message": message,
            "error": str(error) or message,
        }
        _running.pop(job_id, None)


def set_job_complete(
    job_id: str,
    message: str = "Đã hoàn thành tạo giọng.",
    *,
    result_job_id: str = "",
    text: str = "",
) -> None:
    """Publish completion for the request ID, including cache-hit requests."""
    if not job_id:
        return
    with _jobs_lock:
        current = dict(_job_progress.get(job_id) or {})
        payload: dict[str, Any] = {
            "current": max(1, int(current.get("total") or current.get("current") or 1)),
            "total": max(1, int(current.get("total") or 1)),
            "pct": 99,
            "message": message,
            "resultJobId": result_job_id or job_id,
        }
        if text:
            payload["text"] = text
        _job_progress[job_id] = payload
        _running.pop(job_id, None)


def mark_cancel(job_id: str) -> None:
    """Chỉ set flag (gọi từ jobs.request_cancel — tránh đệ quy)."""
    with _jobs_lock:
        _cancel_flags[job_id] = True


def request_cancel(job_id: str) -> bool:
    """Huỷ job studio + kill subprocess TTS đã register dưới job_id."""
    with _jobs_lock:
        _cancel_flags[job_id] = True
        running = job_id in _running
    try:
        from pipeline.core.jobs import kill_job_processes

        kill_job_processes(job_id)
    except Exception:
        pass
    return True if running else True


def _is_cancelled(job_id: str) -> bool:
    with _jobs_lock:
        return bool(_cancel_flags.get(job_id))


def _engine_of(voice: str) -> str:
    """Bucket hiển thị: zmai | clone | vieneu | capcut | elevenlabs | system."""
    v = voice or ""
    parsed = parse_vieneu_voice(v)
    if parsed:
        kind, _ = parsed
        if kind == "reference":
            return "zmai"
        if kind == "clone":
            return "clone"
        return "vieneu"
    if v.startswith("vn:clone:"):
        return "clone"
    if v.startswith("vn:"):
        return "vieneu"
    if v.startswith("cc:"):
        return "capcut"
    if v.startswith("el:"):
        return "elevenlabs"
    if v.startswith("win:") or v == "system" or v.startswith("espeak:"):
        return "system"
    # bare zmAI reference id (no prefix)
    if v and not v.startswith(("cc:", "el:", "vn:", "win:")):
        return "zmai"
    return "system"


def _write_meta(job_dir: Path, meta: dict[str, Any]) -> None:
    (job_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _concat_wavs(parts: list[Path], out: Path, gap_ms: int = 0) -> float:
    if len(parts) == 1:
        shutil.copy2(parts[0], out)
        return ffprobe_duration(out)
    ensure_vieneu_dirs()
    list_f = TTS_TEMP / f"concat_{uuid.uuid4().hex[:8]}.txt"
    lines: list[str] = []
    silence: Path | None = None
    if gap_ms > 0:
        silence = TTS_TEMP / f"sil_{gap_ms}.wav"
        if not silence.is_file():
            subprocess.check_call(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r=48000:cl=mono",
                    "-t",
                    f"{gap_ms / 1000.0:.3f}",
                    str(silence),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    for i, p in enumerate(parts):
        lines.append(f"file '{p.resolve().as_posix()}'")
        if silence and i < len(parts) - 1:
            lines.append(f"file '{silence.resolve().as_posix()}'")
    list_f.write_text("\n".join(lines), encoding="utf-8")
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_f),
            "-c",
            "copy",
            str(out),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    list_f.unlink(missing_ok=True)
    return ffprobe_duration(out)


def _postprocess_part(path: Path, *, trim: bool, normalize: bool) -> None:
    """Apply Studio advanced audio options to one synthesized part."""
    if trim:
        audio_utils.trim_silence(path)
    if normalize:
        audio_utils.normalize_loudness(path)


def synth_text_job(
    *,
    text: str,
    voice: str,
    lang: str = "vi",
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: float = 0.0,
    style: str = "tu_nhien",
    match_duration: str = "none",
    title: str = "",
    auto_split: bool = True,
    gap_ms: int = 0,
    normalize: bool = False,
    trim_silence: bool = False,
    job_id: str | None = None,
) -> dict[str, Any]:
    ensure_vieneu_dirs()
    fp = _job_fingerprint(
        text=text,
        srt_text="",
        voice=voice,
        lang=lang,
        speed=speed,
        volume=volume,
        pitch=pitch,
        style=style,
        match_duration=match_duration,
        keep_timeline=True,
        auto_split=auto_split,
        gap_ms=gap_ms,
        normalize=normalize,
        trim_silence=trim_silence,
    )
    hit = _find_cached_job(fp)
    if hit:
        return hit
    job_id = job_id or uuid.uuid4().hex[:12]
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    wav = job_dir / "audio.wav"
    with _jobs_lock:
        _running[job_id] = True
        _cancel_flags[job_id] = False
    try:
        try:
            from pipeline.core.jobs import set_job_context

            set_job_context(job_id)
        except Exception:
            pass
        # auto_split: tách thêm theo .!? ; blank line luôn tách part riêng.
        chunks = split_sentences(text, max_chars=240, by_sentence=bool(auto_split))
        if not chunks:
            chunks = [text.strip() or "."]
        total_chunks = len(chunks)
        set_job_progress(job_id, 0, total_chunks, f"Bắt đầu tạo {total_chunks} câu…")
        import concurrent.futures
        engine_type = _engine_of(voice)
        from pipeline.core.accel import tts_local_workers
        from pipeline.core.resources import adaptive_workers

        is_local = engine_type in ("vieneu", "zmai", "clone", "system")
        if is_local:
            max_workers = tts_local_workers(None, tasks=total_chunks)
        else:
            max_workers = adaptive_workers(None, kind="network", cap=24, tasks=total_chunks)

        # Phase: model load (VieNeu) — only when state is actually cold/loading
        if engine_type == "vieneu":
            try:
                from pipeline.tts.engines import vieneu as vieneu_mod
                if vieneu_mod._load_state in ("cold", "loading"):
                    set_job_progress_pct(
                        job_id, 3, "Đang nạp model VieNeu vào GPU lần đầu…",
                        current=0, total=total_chunks,
                    )
            except Exception:
                pass

        part_paths: list[Path] = []
        for i in range(total_chunks):
            part_paths.append(job_dir / f"part_{i:03d}.wav")

        chunk_frac = [0.0] * total_chunks
        frac_lock = threading.Lock()

        def _publish_frac(message: str = "") -> None:
            with frac_lock:
                done_sum = sum(chunk_frac)
                # Synth band occupies up to 92%; concat phase uses 92–99.
                pct = min(92.0, max(1.0, (done_sum / max(1, total_chunks)) * 92.0))
                set_job_progress_pct(
                    job_id, pct, message,
                    current=done_sum, total=total_chunks,
                )

        def _process_chunk(i: int, chunk: str, part: Path):
            if _is_cancelled(job_id):
                return

            def _on_progress(frac: float) -> None:
                if _is_cancelled(job_id):
                    return
                with frac_lock:
                    chunk_frac[i] = max(chunk_frac[i], min(1.0, float(frac)))
                _publish_frac(f"Đang tạo câu {i + 1}/{total_chunks}…")

            tts_segment(
                chunk, voice, part, None, "none",
                lang=lang, speed=speed, volume=volume, pitch=pitch, style=style,
                cancel_check=lambda: _is_cancelled(job_id),
                on_progress=_on_progress,
            )
            _postprocess_part(part, trim=trim_silence, normalize=normalize)
            with frac_lock:
                chunk_frac[i] = 1.0
            _publish_frac(f"Đã xong câu {i + 1}/{total_chunks}…")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_chunk, i, chunks[i], part_paths[i]): i for i in range(total_chunks)}
            for future in concurrent.futures.as_completed(futures):
                if _is_cancelled(job_id):
                    raise RuntimeError("Job đã hủy")
                future.result()

        part_durs: list[float] = [ffprobe_duration(p) for p in part_paths]
        set_job_progress_pct(
            job_id, 95, "Đang ghép nối âm thanh và tạo phụ đề…",
            current=total_chunks, total=total_chunks,
        )
        gap = max(0, int(gap_ms)) / 1000.0
        # CapCut: cue ngắn (~42 ký tự), timeline ∝ audio từng part
        cues = cues_from_parts(
            chunks,
            part_durs,
            gap_sec=gap,
            max_chars=42,
            max_words=12,
        )
        if len(part_paths) == 1:
            shutil.copy2(part_paths[0], wav)
            part_paths[0].unlink(missing_ok=True)
            dur = part_durs[0] if part_durs else ffprobe_duration(wav)
        else:
            dur = _concat_wavs(part_paths, wav, gap_ms=max(0, int(gap_ms)))
            for p in part_paths:
                p.unlink(missing_ok=True)
        srt_path = job_dir / "subs.srt"
        write_srt(srt_path, cues, capcut=True)
        _ = match_duration
        meta = {
            "id": job_id,
            "title": (title or text[:48]).strip(),
            "voice": voice,
            "voiceName": _voice_display(voice, lang),
            "lang": lang,
            "duration": dur,
            "engine": _engine_of(voice),
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "audioFile": "audio.wav",
            "srtFile": "subs.srt",
            "text": text,
            "chunks": chunks,
            "partDurs": part_durs,
            "gapMs": gap_ms,
            "status": "done",
            "fingerprint": fp,
            "style": style,
            "speed": speed,
            "volume": volume,
            "pitch": pitch,
            "autoSplit": auto_split,
            "normalize": bool(normalize),
            "trimSilence": bool(trim_silence),
            "cueCount": len(cues),
        }
        _write_meta(job_dir, meta)
        prune_history(HISTORY_MAX)
        return {
            "id": job_id,
            "duration": dur,
            "audioUrl": f"/api/tts/studio/jobs/{job_id}/audio.wav",
            "mp3Url": f"/api/tts/studio/jobs/{job_id}/audio.mp3",
            "srtUrl": f"/api/tts/studio/jobs/{job_id}/subs.srt",
            "zipUrl": f"/api/tts/studio/jobs/{job_id}/bundle.zip",
            "meta": meta,
            "cached": False,
        }
    except Exception:
        if _is_cancelled(job_id) and parse_vieneu_voice(voice):
            reset_vieneu_client()
        if job_dir.is_dir() and not (job_dir / "audio.wav").is_file():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        with _jobs_lock:
            _running.pop(job_id, None)
            _cancel_flags.pop(job_id, None)


def synth_srt_job(
    *,
    srt_text: str,
    voice: str,
    lang: str = "vi",
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: float = 0.0,
    style: str = "tu_nhien",
    match_duration: str = "natural",
    keep_timeline: bool = True,
    title: str = "",
    gap_ms: int = 0,
    normalize: bool = False,
    trim_silence: bool = False,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Batch synth each SRT cue; fit duration with safe caps."""
    cues = parse_srt(srt_text)
    if not cues:
        raise ValueError("SRT rỗng hoặc không parse được")
    ensure_vieneu_dirs()
    effective_match = (
        "stretch" if keep_timeline and match_duration == "natural" else match_duration
    )
    fp = _job_fingerprint(
        text="",
        srt_text=srt_text,
        voice=voice,
        lang=lang,
        speed=speed,
        volume=volume,
        pitch=pitch,
        style=style,
        match_duration=effective_match,
        keep_timeline=keep_timeline,
        auto_split=False,
        gap_ms=gap_ms,
        normalize=normalize,
        trim_silence=trim_silence,
    )
    hit = _find_cached_job(fp)
    if hit:
        return hit
    job_id = job_id or uuid.uuid4().hex[:12]
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        _running[job_id] = True
        _cancel_flags[job_id] = False
    total_cues = len(cues)
    set_job_progress(job_id, 0, total_cues, f"Bắt đầu tạo {total_cues} đoạn SRT…")
    part_paths: list[Path] = []
    out_cues: list[dict] = []
    cursor = 0.0
    try:
        try:
            from pipeline.core.jobs import set_job_context

            set_job_context(job_id)
        except Exception:
            pass
            
        import concurrent.futures
        engine_type = _engine_of(voice)
        from pipeline.core.accel import tts_local_workers
        from pipeline.core.resources import adaptive_workers

        is_local = engine_type in ("vieneu", "zmai", "clone", "system")
        if is_local:
            max_workers = tts_local_workers(None, tasks=total_cues)
        else:
            max_workers = adaptive_workers(None, kind="network", cap=24, tasks=total_cues)

        if engine_type == "vieneu":
            try:
                from pipeline.tts.engines import vieneu as vieneu_mod
                if vieneu_mod._load_state in ("cold", "loading"):
                    set_job_progress_pct(
                        job_id, 3, "Đang nạp model VieNeu vào GPU lần đầu…",
                        current=0, total=total_cues,
                    )
            except Exception:
                pass
        
        match = effective_match if effective_match in ("none", "natural", "stretch", "preferVideo") else "stretch"
        use_match = "none" if match in ("none", "preferVideo") else match
        
        for i in range(total_cues):
            part_paths.append(job_dir / f"cue_{i:03d}.wav")

        cue_frac = [0.0] * total_cues
        frac_lock = threading.Lock()

        def _publish_frac(message: str = "") -> None:
            with frac_lock:
                done_sum = sum(cue_frac)
                pct = min(92.0, max(1.0, (done_sum / max(1, total_cues)) * 92.0))
                set_job_progress_pct(
                    job_id, pct, message,
                    current=done_sum, total=total_cues,
                )
            
        def _process_cue(i: int, cue: dict, part: Path):
            if _is_cancelled(job_id):
                return
            text = str(cue.get("text") or "").strip() or "…"
            slot = max(0.15, float(cue["end"]) - float(cue["start"]))
            target = slot if use_match != "none" else None

            def _on_progress(frac: float) -> None:
                if _is_cancelled(job_id):
                    return
                with frac_lock:
                    cue_frac[i] = max(cue_frac[i], min(1.0, float(frac)))
                _publish_frac(f"Đang tạo đoạn SRT {i + 1}/{total_cues}…")

            tts_segment(
                text, voice, part, target, use_match,
                lang=lang, speed=speed, volume=volume, pitch=pitch, style=style,
                cancel_check=lambda: _is_cancelled(job_id),
                on_progress=_on_progress,
            )
            _postprocess_part(part, trim=trim_silence, normalize=normalize)
            with frac_lock:
                cue_frac[i] = 1.0
            _publish_frac(f"Đã xong đoạn SRT {i + 1}/{total_cues}…")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_cue, i, cues[i], part_paths[i]): i for i in range(total_cues)}
            for future in concurrent.futures.as_completed(futures):
                if _is_cancelled(job_id):
                    raise RuntimeError("Job đã hủy")
                future.result()

        for i, cue in enumerate(cues):
            part = part_paths[i]
            text = str(cue.get("text") or "").strip() or "…"
            dur = float(ffprobe_duration(part) or 0.15)
            # Giữ nguyên text + timestamp SRT gốc (không tách CapCut)
            src_start = float(cue["start"])
            src_end = max(src_start, float(cue["end"]))
            if keep_timeline:
                start = src_start
                end = src_end
            else:
                start = cursor
                end = start + dur
                if gap_ms > 0:
                    end += gap_ms / 1000.0
                cursor = end
            out_cues.append(
                {
                    "start": start,
                    "end": end,
                    "text": text,  # giữ xuống dòng trong cue
                    "_dur": dur,
                    "_srcStart": src_start,
                    "_srcEnd": src_end,
                }
            )
        set_job_progress_pct(
            job_id, 95, "Đang xuất file âm thanh và phụ đề SRT…",
            current=total_cues, total=total_cues,
        )
        wav = job_dir / "audio.wav"
        if keep_timeline:
            # Mix theo đúng timestamp SRT; _mix_timeline chặn từng audio trong slot.
            _mix_timeline(
                part_paths,
                [
                    {
                        "start": float(c["_srcStart"]),
                        "end": float(c["_srcEnd"]),
                    }
                    for c in out_cues
                ],
                wav,
            )
        else:
            _concat_wavs(part_paths, wav, gap_ms=max(0, int(gap_ms)))
        for p in part_paths:
            p.unlink(missing_ok=True)
        # Cue xuất = đúng cấu trúc SRT đầu vào (1 cue TTS ↔ 1 cue SRT)
        source_cues = [
            {
                "start": float(c["_srcStart"]),
                "end": float(c["_srcEnd"]),
                "text": c["text"],
                "dur": float(c["_dur"]),
            }
            for c in out_cues
        ]
        # SRT xuất giữ nguyên timestamp/text đầu vào.
        if keep_timeline:
            export_cues = [
                {
                    "start": float(c["_srcStart"]),
                    "end": float(c["_srcEnd"]),
                    "text": c["text"],
                }
                for c in out_cues
            ]
        else:
            export_cues = [
                {
                    "start": float(c["start"]),
                    "end": float(c["end"]),
                    "text": c["text"],
                }
                for c in out_cues
            ]
        srt_path = job_dir / "subs.srt"
        # capcut=False: không wrap/tách text — giữ nguyên body SRT
        write_srt(srt_path, export_cues, capcut=False)
        # backup bản gốc (byte-for-byte parse-normalized)
        try:
            (job_dir / "source.srt").write_bytes(b"\xef\xbb\xbf" + srt_text.encode("utf-8"))
        except OSError:
            pass
        out_cues = export_cues
        dur = ffprobe_duration(wav)
        meta = {
            "id": job_id,
            "title": (
                title
                or (export_cues[0]["text"].replace("\n", " ")[:48] if export_cues else "SRT")
            ).strip(),
            "voice": voice,
            "voiceName": _voice_display(voice, lang),
            "lang": lang,
            "duration": dur,
            "engine": _engine_of(voice),
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "audioFile": "audio.wav",
            "srtFile": "subs.srt",
            "text": "\n".join(c["text"] for c in export_cues),
            "sourceCues": source_cues,
            "gapMs": gap_ms,
            "status": "done",
            "mode": "srt",
            "fingerprint": fp,
            "style": style,
            "speed": speed,
            "volume": volume,
            "pitch": pitch,
            "cueCount": len(export_cues),
            "keepTimeline": bool(keep_timeline),
            "matchDuration": match,
            "normalize": bool(normalize),
            "trimSilence": bool(trim_silence),
        }
        _write_meta(job_dir, meta)
        prune_history(HISTORY_MAX)
        return {
            "id": job_id,
            "duration": dur,
            "audioUrl": f"/api/tts/studio/jobs/{job_id}/audio.wav",
            "mp3Url": f"/api/tts/studio/jobs/{job_id}/audio.mp3",
            "srtUrl": f"/api/tts/studio/jobs/{job_id}/subs.srt",
            "zipUrl": f"/api/tts/studio/jobs/{job_id}/bundle.zip",
            "meta": meta,
            "cached": False,
        }
    except Exception:
        if _is_cancelled(job_id) and parse_vieneu_voice(voice):
            reset_vieneu_client()
        if job_dir.is_dir() and not (job_dir / "audio.wav").is_file():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        with _jobs_lock:
            _running.pop(job_id, None)
            _cancel_flags.pop(job_id, None)


def _mix_timeline(parts: list[Path], cues: list[dict], out: Path) -> None:
    """Place each part at cue.start with silence padding (absolute timeline)."""
    if not parts:
        raise ValueError("no parts")
    total = max(float(c["end"]) for c in cues) + 0.05
    # filter complex: adelay per segment then amix
    # simpler: build with anullsrc + adelay concat via ffmpeg filter_complex
    inputs: list[str] = ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=mono"]
    filters: list[str] = []
    mix_ins = ["[0:a]"]
    for i, p in enumerate(parts):
        inputs.extend(["-i", str(p)])
        delay_ms = int(max(0.0, float(cues[i]["start"])) * 1000)
        slot = max(0.01, float(cues[i]["end"]) - float(cues[i]["start"]))
        filters.append(
            f"[{i + 1}:a]atrim=duration={slot:.3f},adelay={delay_ms}|{delay_ms}[a{i}]"
        )
        mix_ins.append(f"[a{i}]")
    n = len(parts) + 1
    filters.append(
        f"{''.join(mix_ins)}amix=inputs={n}:duration=longest:dropout_transition=0,volume={n}[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        str(out),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rebuild_srt(job_id: str, srt_style: str = "hard") -> Path:
    """Re-segment existing job into a different SRT style (no TTS re-run)."""
    if srt_style not in SRT_STYLES:
        raise ValueError(f"Unknown style: {srt_style}")
    job_dir = _job_dir(job_id)
    meta_p = job_dir / "meta.json"
    if not meta_p.is_file():
        raise FileNotFoundError("meta.json missing")
    m = json.loads(meta_p.read_text(encoding="utf-8"))

    sp = style_params(srt_style)
    # ponytail: cache per style so we don't rebuild every download
    cached = job_dir / f"subs_{srt_style}.srt"
    if cached.is_file():
        return cached

    src_cues = m.get("sourceCues")
    chunks = m.get("chunks")
    part_durs = m.get("partDurs")
    gap_ms = m.get("gapMs", 0)
    duration = float(m.get("duration") or 0)

    if src_cues and m.get("mode") == "srt" and srt_style == "hard":
        # Job từ SRT + style hard: trả đúng cue gốc (không tách lại)
        cues = [
            {
                "start": float(c["start"]),
                "end": float(c["end"]),
                "text": str(c.get("text") or "").strip() or "…",
            }
            for c in src_cues
        ]
        write_srt(cached, cues, capcut=False)
        return cached
    if src_cues:
        # SRT job + style CapCut khác: tách hiển thị từ source cue
        cues: list[dict] = []
        for c in src_cues:
            text = str(c.get("text") or "").strip() or "…"
            seg_start = float(c["start"])
            seg_dur = max(0.08, float(c.get("dur") or (float(c["end"]) - seg_start)))
            pieces = _split_for_style(text, sp)
            weights = [max(1, len(s)) for s in pieces]
            tw = sum(weights) or 1
            acc = 0.0
            for j, s in enumerate(pieces):
                share = seg_dur * (weights[j] / tw) if j < len(pieces) - 1 else max(0.06, seg_dur - acc)
                cues.append({
                    "start": seg_start + acc,
                    "end": seg_start + acc + max(0.06, share),
                    "text": wrap_capcut_text(s, max_line=sp.wrap_line, max_lines=2),
                })
                acc += max(0.06, share)
    elif chunks and part_durs and len(chunks) == len(part_durs):
        # Text job path
        gap = max(0, int(gap_ms)) / 1000.0
        cues = cues_from_parts(chunks, part_durs, gap_sec=gap, style=srt_style)
    elif chunks and duration > 0:
        # Fallback: old job without partDurs — spread evenly
        total_chars = sum(max(1, len(c)) for c in chunks) or 1
        fake_durs = [duration * (max(1, len(c)) / total_chars) for c in chunks]
        gap = max(0, int(gap_ms)) / 1000.0
        cues = cues_from_parts(chunks, fake_durs, gap_sec=gap, style=srt_style)
    else:
        # Last resort: single cue
        text = str(m.get("text") or "…")
        cues = [{"start": 0.0, "end": max(0.5, duration), "text": text}]

    write_srt(cached, cues, capcut=True, wrap_line=sp.wrap_line)
    return cached


def ensure_wav(job_id: str) -> Path:
    job_dir = _job_dir(job_id)
    wav = job_dir / "audio.wav"
    if not wav.is_file():
        raise FileNotFoundError("audio.wav missing")
    marker = job_dir / ".quicktime-wav-ready"
    if marker.is_file() and marker.stat().st_mtime >= wav.stat().st_mtime:
        return wav
    temp = job_dir / ".audio-quicktime.wav"
    try:
        subprocess.check_call(
            [
                "ffmpeg", "-y", "-i", str(wav), "-map", "0:a:0", "-vn",
                "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(temp),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        temp.replace(wav)
        marker.write_text("pcm_s16le/48000/mono\n", encoding="ascii")
    finally:
        temp.unlink(missing_ok=True)
    return wav


def ensure_mp3(job_id: str) -> Path:
    wav = ensure_wav(job_id)
    mp3 = wav.with_suffix(".mp3")
    if mp3.is_file() and mp3.stat().st_mtime >= wav.stat().st_mtime:
        return mp3
    subprocess.check_call(
        [
            "ffmpeg", "-y", "-i", str(wav), "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", "44100", "-codec:a", "libmp3lame", "-qscale:a", "2",
            "-id3v2_version", "3", str(mp3),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return mp3


def publish_job_outputs(job_id: str, output_dir: str = "", output_format: str = "wav48") -> Path:
    """Publish one TTS job into a stable user-selected root/job-id folder under ZM_AI_TOOL/text-to-speech."""
    target = item_output_folder(selected_or_default("tts", output_dir), job_id)
    job_dir = _job_dir(job_id)
    source_srt = job_dir / "subs.srt"
    if source_srt.is_file():
        shutil.copy2(source_srt, target / "subtitles.srt")
    source_srt_original = job_dir / "source.srt"
    if source_srt_original.is_file():
        shutil.copy2(source_srt_original, target / "source.srt")

    wav_file = ensure_wav(job_id)
    mp3_file = ensure_mp3(job_id)

    if wav_file.is_file():
        shutil.copy2(wav_file, target / "audio.wav")
    if mp3_file.is_file():
        shutil.copy2(mp3_file, target / "audio.mp3")

    if output_format == "wav16" and wav_file.is_file():
        try:
            subprocess.check_call(
                [
                    "ffmpeg", "-y", "-i", str(wav_file), "-map", "0:a:0", "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target / "audio_16k.wav"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    bundle = ensure_zip(job_id)
    if bundle.is_file():
        shutil.copy2(bundle, target / "bundle.zip")
    meta_path = job_dir / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["publishedDir"] = str(target)
            meta["outputDir"] = str(target.parent)
            _write_meta(job_dir, meta)
        except (OSError, ValueError, TypeError):
            pass
    return target


def published_job_output_dir(job_id: str) -> Path:
    """Return the persisted user-selected output folder, publishing legacy jobs once.

    If portable state moved, treat an internal old path as stale and publish it
    under the current output root instead of recreating the old app directory.
    """
    job_dir = _job_dir(job_id)
    meta_path = job_dir / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            saved = str(meta.get("publishedDir") or "").strip()
            if saved:
                target = Path(saved).expanduser()
                try:
                    is_stale = False
                    for old in filter(None, os.environ.get("ZM_AI_TOOL_PREVIOUS_HOME", "").split(os.pathsep)):
                        try:
                            target.resolve().relative_to((Path(old) / "output").resolve())
                            is_stale = True
                            break
                        except ValueError:
                            pass
                except (ValueError, OSError):
                    is_stale = False
                if not is_stale:
                    target.mkdir(parents=True, exist_ok=True)
                    return target
        except (OSError, ValueError, TypeError):
            pass
    return publish_job_outputs(job_id)


def ensure_zip(job_id: str, srt_style: str = "hard") -> Path:
    job_dir = _job_dir(job_id)
    wav = job_dir / "audio.wav"
    if not wav.is_file():
        raise FileNotFoundError("audio.wav missing")

    # Get the SRT file for requested style
    if srt_style != "hard":
        try:
            srt = rebuild_srt(job_id, srt_style)
        except Exception:
            srt = job_dir / "subs.srt"
    else:
        srt = job_dir / "subs.srt"

    if not srt.is_file():
        meta_p = job_dir / "meta.json"
        text = ""
        chunks: list[str] = []
        dur = ffprobe_duration(wav)
        if meta_p.is_file():
            try:
                m = json.loads(meta_p.read_text(encoding="utf-8"))
                text = str(m.get("text") or "")
                ch = m.get("chunks")
                if isinstance(ch, list) and ch:
                    chunks = [str(x).strip() for x in ch if str(x).strip()]
            except Exception:
                text = ""
        if chunks and len(chunks) > 1:
            total_chars = sum(max(1, len(c)) for c in chunks) or 1
            cues = []
            cursor = 0.0
            for i, c in enumerate(chunks):
                share = dur * (len(c) / total_chars) if i < len(chunks) - 1 else max(0.05, dur - cursor)
                cues.append({"start": cursor, "end": cursor + max(0.05, share), "text": c})
                cursor += max(0.05, share)
            write_srt(srt, cues)
        else:
            write_srt(srt, [{"start": 0.0, "end": max(0.5, dur), "text": text or "…"}])

    mp3 = job_dir / "audio.mp3"
    if not mp3.is_file():
        try:
            ensure_mp3(job_id)
        except Exception:
            pass

    suffix = f"_{srt_style}" if srt_style != "hard" else ""
    zpath = job_dir / f"bundle{suffix}.zip"
    # ponytail: reuse zip if newer than inputs — avoids truncate-while-serve Content-Length race
    inputs = [p for p in (wav, srt, mp3) if p.is_file()]
    if zpath.is_file() and inputs:
        zt = zpath.stat().st_mtime
        if all(zt >= p.stat().st_mtime for p in inputs):
            return zpath

    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(wav, "audio.wav")
        zf.write(srt, "subs.srt")
        if mp3.is_file():
            zf.write(mp3, "audio.mp3")
    tmp.replace(zpath)
    return zpath


HISTORY_MAX = 50


def prune_history(keep: int = HISTORY_MAX) -> int:
    """Xóa job cũ trên đĩa, chỉ giữ `keep` bản mới nhất (theo mtime)."""
    ensure_vieneu_dirs()
    if not TTS_OUTPUT.is_dir() or keep < 0:
        return 0
    dirs = sorted(
        (d for d in TTS_OUTPUT.iterdir() if d.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for d in dirs[keep:]:
        try:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except Exception:
            pass
    return removed


def list_history(limit: int = HISTORY_MAX) -> list[dict[str, Any]]:
    ensure_vieneu_dirs()
    prune_history(HISTORY_MAX)
    items: list[dict[str, Any]] = []
    if not TTS_OUTPUT.is_dir():
        return items
    for d in sorted(TTS_OUTPUT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        meta_p = d / "meta.json"
        if not meta_p.is_file():
            continue
        try:
            m = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        jid = d.name
        m["id"] = jid
        m["audioUrl"] = f"/api/tts/studio/jobs/{jid}/audio.wav"
        m["mp3Url"] = f"/api/tts/studio/jobs/{jid}/audio.mp3"
        m["srtUrl"] = f"/api/tts/studio/jobs/{jid}/subs.srt"
        m["zipUrl"] = f"/api/tts/studio/jobs/{jid}/bundle.zip"
        items.append(m)
        if len(items) >= limit:
            break
    return items
