"""Standalone subtitle export jobs for audio, video and caption files."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from pipeline.asr.whisper import asr_whisper
from pipeline.core.config import DATA
from pipeline.core.executables import ytdlp_command
from pipeline.core.output_paths import selected_or_default
from pipeline.core.media import extract_audio
from pipeline.core.artifact_cache import ArtifactCache
from pipeline.export.srt import SRT_STYLES, _split_for_style, parse_srt, style_params, wrap_capcut_text, write_subtitle
from pipeline.mt.api import translate_segments

ROOT = DATA / "srt_export"
_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_CACHE = ArtifactCache("srt-export", version=2)


def _update(job_id: str, **values: Any) -> None:
    updated: dict[str, Any] | None = None
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(values)
            updated = dict(_JOBS[job_id])
    if updated is not None:
        _publish("srt-export.job.updated", job_id, updated)


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        return sorted(_JOBS.values(), key=lambda job: float(job["createdAt"]), reverse=True)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        return _JOBS.get(job_id)


def create_job(filename: str, input_path: Path | None, source_kind: str, *, source_url: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:10]
    work = ROOT / job_id
    work.mkdir(parents=True)
    copied = work / f"input{input_path.suffix.lower()}" if input_path else None
    if input_path and copied:
        shutil.move(str(input_path), copied)
    job = {
        "id": job_id, "filename": filename, "sourceKind": source_kind,
        "status": "queued", "progress": 0, "message": "Đang chờ xử lý",
        "error": None, "createdAt": time.time(), "outputDir": str(work),
        "inputPath": str(copied) if copied else "", "sourceUrl": source_url,
        "options": options or {}, "files": [], "cancelled": False,
    }
    with _LOCK:
        _JOBS[job_id] = job
    _publish("srt-export.job.created", job_id, job)
    return job


def cancel_job(job_id: str) -> bool:
    job = get_job(job_id)
    if not job:
        return False
    _update(job_id, cancelled=True, status="cancelled", message="Đã hủy")
    return True


def _publish(event_type: str, job_id: str, payload: dict[str, Any] | None) -> None:
    try:
        from pipeline.core.realtime import realtime
        realtime.publish("srt-export", event_type, job_id, payload)
    except Exception:
        return


def _caption_cues(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = parse_srt(raw)
    if cues:
        return cues
    lines = [line.strip() for line in raw.splitlines() if line.strip() and line.strip().upper() != "WEBVTT"]
    return [{"start": index * 3.0, "end": index * 3.0 + 3.0, "text": line} for index, line in enumerate(lines)]


def _styled(cues: list[dict[str, Any]], style: str) -> list[dict[str, Any]]:
    params = style_params(style)
    result: list[dict[str, Any]] = []
    for cue in cues:
        start, end = float(cue["start"]), max(float(cue["end"]), float(cue["start"]) + 0.06)
        pieces = _split_for_style(str(cue.get("text") or ""), params)
        max_pieces = max(1, int((end - start) / 0.06))
        if len(pieces) > max_pieces:
            # ponytail: a very short original cue cannot safely host many 60ms captions.
            pieces = pieces[: max_pieces - 1] + [" ".join(pieces[max_pieces - 1 :])]
        weights = [max(1, len(piece)) for piece in pieces]
        total = sum(weights) or 1
        cursor = start
        for index, piece in enumerate(pieces):
            duration = (end - start) * weights[index] / total if index < len(pieces) - 1 else end - cursor
            next_cursor = max(cursor + 0.06, cursor + duration)
            result.append({"start": cursor, "end": min(end, next_cursor), "text": wrap_capcut_text(piece, params.wrap_line)})
            cursor = next_cursor
    return result


def _pick_platform_language(info: dict[str, Any], preferred: str = "auto") -> tuple[str, str] | None:
    """Return (language, source type), preferring creator subtitles over auto captions."""
    pools = (("phụ đề có sẵn", info.get("subtitles") or {}), ("phụ đề tự động", info.get("automatic_captions") or {}))
    wanted = (preferred or "auto").lower()
    for label, pool in pools:
        keys = [str(key) for key in pool if key and key != "live_chat"]
        if not keys:
            continue
        if wanted != "auto":
            exact = next((key for key in keys if key.lower() == wanted), None)
            base = next((key for key in keys if key.lower().split("-", 1)[0] == wanted.split("-", 1)[0]), None)
            if exact or base:
                return exact or base, label
            continue
        for priority in ("vi", "en"):
            match = next((key for key in keys if key.lower().split("-", 1)[0] == priority), None)
            if match:
                return match, label
        return keys[0], label
    return None


def _platform_subtitles(work: Path, url: str, source_lang: str) -> tuple[list[dict[str, Any]] | None, str]:
    ytdlp = ytdlp_command()
    if not ytdlp:
        return None, "Không tìm thấy yt-dlp"
    meta = subprocess.run([*ytdlp, "--dump-single-json", "--skip-download", "--no-playlist", url], capture_output=True, text=True, timeout=90)
    if meta.returncode:
        return None, (meta.stderr.strip().splitlines()[-1] if meta.stderr.strip() else "Không đọc được phụ đề của nền tảng")
    info = json.loads(meta.stdout)
    picked = _pick_platform_language(info, source_lang)
    if not picked:
        return None, "Video không có phụ đề phù hợp"
    language, label = picked
    template = work / "platform.%(ext)s"
    result = subprocess.run([
        *ytdlp, "--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs",
        "--sub-langs", language, "--sub-format", "srt/vtt/best", "--convert-subs", "srt",
        "-o", str(template), url,
    ], capture_output=True, text=True, timeout=180)
    captions = sorted(work.glob("platform*.srt"))
    if result.returncode or not captions:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Không tải được phụ đề"
        return None, detail
    return _caption_cues(captions[0]), f"Đã lấy {label} ({language}), không cần Whisper"


def _platform_audio(work: Path, url: str) -> Path:
    ytdlp = ytdlp_command()
    if not ytdlp:
        raise RuntimeError("Chưa cài yt-dlp để đọc URL nền tảng")
    result = subprocess.run([*ytdlp, "--no-playlist", "-f", "bestaudio/best", "-o", str(work / "input.%(ext)s"), url], capture_output=True, text=True, timeout=900)
    sources = [path for path in work.glob("input.*") if path.suffix.lower() != ".part"]
    if result.returncode or not sources:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Không tải được audio từ nền tảng"
        raise RuntimeError(detail)
    return sources[0]


def _translated_cues(cues: list[dict[str, Any]], translated: list[str]) -> list[dict[str, Any]]:
    result = []
    for cue, text in zip(cues, translated):
        result.append({**cue, "text": str(text or cue.get("text") or "").strip()})
    return result


def _write_outputs(work: Path, cues: list[dict[str, Any]], prefix: str) -> list[str]:
    files: list[str] = []
    for style in SRT_STYLES:
        name = f"{prefix}-{style}.srt"
        write_subtitle(work / name, _styled(cues, style), "srt", capcut=False)
        files.append(name)
    write_subtitle(work / f"{prefix}.vtt", _styled(cues, "hard"), "vtt", capcut=False)
    write_subtitle(work / f"{prefix}.txt", cues, "txt")
    return files + [f"{prefix}.vtt", f"{prefix}.txt"]


def _zip_outputs(work: Path, files: list[str], *, bilingual: bool = False, target_lang: str = "") -> None:
    with zipfile.ZipFile(work / "subtitles-all.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            folder = ""
            if bilingual:
                folder = "phu-de-goc" if name.startswith("subtitles-source-") else f"ban-dich-{target_lang}"
            archive.write(work / name, f"{folder}/{name}" if folder else name)


def _publish_outputs(work: Path, files: list[str], output_dir: str) -> str:
    target = selected_or_default("subtitle-export", output_dir)
    for name in files:
        source = work / name
        if source.is_file():
            shutil.copy2(source, target / name)
    return str(target)


def _run(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    try:
        _update(job_id, status="processing", progress=5, message="Đang đọc đầu vào")
        work = Path(job["outputDir"])
        source = Path(job["inputPath"]) if job.get("inputPath") else None
        options = job.get("options") or {}
        cache_key = _CACHE.key(
            inputs=[source] if source else [], settings=options,
            values={"sourceKind": job["sourceKind"], "sourceUrl": job.get("sourceUrl") or ""},
        )
        cached_files = _CACHE.files(cache_key)
        if cached_files:
            targets = {name: work / name for name in cached_files}
            if _CACHE.restore(cache_key, targets):
                files = list(cached_files)
                published_dir = _publish_outputs(work, files, str(options.get("outputDir") or ""))
                _update(job_id, status="done", progress=100, message=f"Đã xuất {len(files)} file · Cache", files=files, publishedDir=published_dir)
                return
        platform_note = ""
        if job["sourceKind"] == "platform":
            cues, platform_note = _platform_subtitles(work, job["sourceUrl"], str(job.get("options", {}).get("sourceLang") or "auto"))
            if cues:
                _update(job_id, progress=55, message=platform_note)
            else:
                _update(job_id, progress=10, message="Nền tảng không có subtitle phù hợp — đang dùng Whisper")
                source = _platform_audio(work, job["sourceUrl"])
        elif job["sourceKind"] == "caption":
            assert source is not None
            cues = _caption_cues(source)
        capcut_translated: list[dict[str, Any]] | None = None
        if job["sourceKind"] == "media" and str(options.get("recognitionEngine") or "whisper") == "capcut":
            assert source is not None
            from pipeline.capcut_stt import transcribe_and_translate
            _update(job_id, progress=12, message="CapCut: đang gửi video")
            cues, capcut_translated = transcribe_and_translate(
                source, str(options.get("sourceLang") or "auto"), str(options.get("targetLang") or "vi"),
                cancelled=lambda: bool((get_job(job_id) or {}).get("cancelled")),
                progress=lambda message: _update(job_id, progress=45, message=message),
            )
            _update(job_id, progress=70, message="CapCut: đã dịch phụ đề")
        elif job["sourceKind"] == "media" or (job["sourceKind"] == "platform" and not cues):
            assert source is not None
            _update(job_id, progress=12, message="Đang tách audio")
            wav = source.with_suffix(".wav")
            extract_audio(source, wav)
            if get_job(job_id).get("cancelled"):
                return
            _update(job_id, progress=25, message="Whisper đang nhận dạng")
            segments = asr_whisper(wav, str(options.get("sourceLang") or "auto"), workers=int(options.get("workers") or 0))
            cues = [{"start": row["start"], "end": row["end"], "text": row["source"]} for row in segments]
        if not cues:
            raise RuntimeError("Không tìm thấy nội dung phụ đề")
        if get_job(job_id).get("cancelled"):
            return
        mode = str(options.get("outputMode") or "original")
        translated_cues: list[dict[str, Any]] | None = None
        if mode != "original":
            if capcut_translated is not None:
                translated_cues = capcut_translated
            else:
                _update(job_id, progress=65, message="Đang dịch phụ đề")
                translated = translate_segments(
                    [str(cue.get("text") or "") for cue in cues], str(options.get("targetLang") or "vi"),
                    source_lang=str(options.get("sourceLang") or "auto"), translator=str(options.get("translator") or "google"),
                    workers=int(options.get("workers") or 2), ollama_mode=str(options.get("ollamaMode") or "cloud"),
                    ollama_model=str(options.get("ollamaModel") or "minimax-m3:cloud"), ollama_local_tier=str(options.get("ollamaLocalTier") or "balanced"),
                    durations=[max(0.1, float(cue["end"]) - float(cue["start"])) for cue in cues],
                )
                translated_cues = _translated_cues(cues, translated)
        _update(job_id, progress=75, message="Đang tạo các định dạng phụ đề")
        if mode == "translated":
            assert translated_cues is not None
            files = _write_outputs(work, translated_cues, "subtitles")
        elif mode == "bilingual":
            assert translated_cues is not None
            lang = "".join(char for char in str(options.get("targetLang") or "translated") if char.isalnum() or char in "-_") or "translated"
            files = _write_outputs(work, cues, "subtitles-source")
            files.extend(_write_outputs(work, translated_cues, f"subtitles-{lang}"))
        else:
            files = _write_outputs(work, cues, "subtitles")
        _zip_outputs(work, files, bilingual=mode == "bilingual", target_lang=lang if mode == "bilingual" else "")
        files.append("subtitles-all.zip")
        try:
            _CACHE.store(cache_key, {name: work / name for name in files})
        except OSError:
            pass
        published_dir = _publish_outputs(work, files, str(options.get("outputDir") or ""))
        suffix = f" · {platform_note}" if platform_note else ""
        _update(job_id, status="done", progress=100, message=f"Đã xuất {len(files)} file{suffix}", files=files, publishedDir=published_dir)
    except Exception as exc:
        if not get_job(job_id).get("cancelled"):
            _update(job_id, status="error", error=str(exc), message="Xuất phụ đề thất bại")


def start(job_id: str) -> None:
    threading.Thread(target=_run, args=(job_id,), name=f"srt-export-{job_id}", daemon=True).start()
