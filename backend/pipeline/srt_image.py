"""Render images and video clips to a prompt timeline with optional narration."""
from __future__ import annotations

import re
import json
import hashlib
import csv
import os
import random
import signal
import subprocess
import sys
import threading
import time
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pipeline.core.config import DATA, PUBLIC_DATA
from pipeline.core.output_paths import downloads_folder
from pipeline.core.jobs import kill_process_tree
from pipeline.core.media import _ff_bin, _has_ffmpeg_filter, h264_encoder_args, h264_hardware_encoder
from pipeline.drawing.jobs import (
    cancel as cancel_drawing_job,
    create_job as create_drawing_job,
    get_job as get_drawing_job,
    retry as retry_drawing_job,
    start_batch as start_drawing_batch,
)
from pipeline.export.fonts import _resolve_font_name

ROOT = DATA / "srt_image"
ROOT.mkdir(parents=True, exist_ok=True)
CACHE_ROOT = ROOT / "render-cache"
CACHE_INDEX = CACHE_ROOT / "index.json"
CACHE_VERSION = 2
DEFAULT_OUTPUT_RESOLUTION = "auto"
_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE_CONDITION = threading.Condition()
_CACHE_INFLIGHT: set[str] = set()
_JOBS: dict[str, dict[str, Any]] = {}
_PROCS: dict[str, subprocess.Popen] = {}
_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")
_SRT_RANGE = re.compile(
    r"(?m)^(\s*)(\d+:\d+:\d+[,.]\d+)(\s*-->\s*)(\d+:\d+:\d+[,.]\d+)(.*)$"
)
_TIMELINE_RANGE = re.compile(r"\[\s*([0-9:.,]+)\s*[-–—]\s*([0-9:.,]+)\s*\]")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
LOGO_RANDOM_POSITIONS = (
    (0.08, 0.10), (0.90, 0.62), (0.12, 0.60), (0.88, 0.12),
    (0.50, 0.68), (0.50, 0.08), (0.08, 0.68), (0.78, 0.38),
)


def _file_signature(value: str) -> dict | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        h.update(str(size).encode("utf-8"))
        if size <= 1024 * 1024:  # <= 1MB (text, srt, timeline, small images)
            h.update(path.read_bytes())
        else:
            with path.open("rb") as stream:
                h.update(stream.read(65536))
                stream.seek(max(0, size - 65536))
                h.update(stream.read(65536))
        return {
            "name": path.name,
            "size": size,
            "hash": h.hexdigest(),
        }
    except OSError:
        return None


def _render_cache_key(job: dict) -> str:
    """Fingerprint every source and render option without reading large media files."""
    payload = {
        "version": CACHE_VERSION,
        "images": [_file_signature(value) for value in job.get("images", [])],
        "timeline": _file_signature(str(job.get("timeline") or "")),
        "audio": _file_signature(str(job.get("audio") or "")),
        "srt": _file_signature(str(job.get("srt") or "")),
        "watermark": _file_signature(str(job.get("watermark") or "")),
        "options": job.get("options") or {},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_cache_index() -> dict:
    try:
        value = json.loads(CACHE_INDEX.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache_index(index: dict) -> None:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_INDEX.with_suffix(".tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CACHE_INDEX)


def _cached_render(key: str) -> Path | None:
    with _CACHE_LOCK:
        entry = _read_cache_index().get(key)
        path = CACHE_ROOT / str(entry.get("file", "")) if isinstance(entry, dict) else None
        return path if path and path.is_file() else None


def _store_cached_render(key: str, source: Path) -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    target = CACHE_ROOT / f"{key}.mp4"
    temporary = CACHE_ROOT / f".{key}.tmp.mp4"
    shutil.copy2(source, temporary)
    temporary.replace(target)
    with _CACHE_LOCK:
        index = _read_cache_index()
        index[key] = {"file": target.name, "size": target.stat().st_size, "createdAt": int(time.time())}
        _write_cache_index(index)
    return target


def _claim_render_cache(job_id: str, key: str) -> tuple[Path | None, bool]:
    """Return a hit, or reserve this key; identical concurrent jobs wait for its owner."""
    while True:
        cached = _cached_render(key)
        if cached:
            return cached, False
        with _CACHE_CONDITION:
            if key not in _CACHE_INFLIGHT:
                _CACHE_INFLIGHT.add(key)
                return None, True
            _CACHE_CONDITION.wait(timeout=.25)
        if (get_job(job_id) or {}).get("status") == "cancelled":
            return None, False


def _release_render_cache(key: str) -> None:
    with _CACHE_CONDITION:
        _CACHE_INFLIGHT.discard(key)
        _CACHE_CONDITION.notify_all()


def _seconds(value: str) -> float:
    match = _TIME.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Mốc SRT không hợp lệ: {value}")
    h, m, s, ms = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def parse_srt_times(path: Path) -> list[tuple[float, float]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    times: list[tuple[float, float]] = []
    for line in text.splitlines():
        if "-->" not in line:
            continue
        left, right = line.split("-->", 1)
        start, end = _seconds(left), _seconds(right.split()[0])
        if end > start:
            times.append((start, end))
    if not times:
        raise ValueError("File SRT không có mốc thời gian hợp lệ")
    return times


def _format_srt_time(seconds: float) -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def shift_srt(path: Path, output: Path, offset: float) -> Path:
    text = path.read_text(encoding="utf-8-sig", errors="replace")

    def replace(match: re.Match[str]) -> str:
        start = _format_srt_time(_seconds(match.group(2)) + offset)
        end = _format_srt_time(_seconds(match.group(4)) + offset)
        return f"{match.group(1)}{start}{match.group(3)}{end}{match.group(5)}"

    shifted, count = _SRT_RANGE.subn(replace, text)
    if not count:
        raise ValueError("File SRT không có mốc thời gian hợp lệ")
    blocks = re.split(r"(\r?\n[ \t]*\r?\n)", shifted)
    for index in range(0, len(blocks), 2):
        lines = blocks[index].splitlines()
        timeline_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timeline_index >= 0 and timeline_index + 1 < len(lines):
            # ponytail: join bằng khoảng trắng để tận dụng hết chiều ngang màn hình.
            # Nếu để \N sẽ bị xuống dòng quá sớm theo file SRT gốc.
            text_line = " ".join(line.strip() for line in lines[timeline_index + 1:] if line.strip())
            blocks[index] = "\n".join([*lines[:timeline_index + 1], text_line])
    output.write_text("".join(blocks), encoding="utf-8")
    return output


def _timeline_seconds(value: str) -> float:
    normalized = value.strip().replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return float(normalized)
    dot_parts = normalized.split(".")
    if ":" not in normalized and len(dot_parts) == 4:
        hours, minutes, seconds, centiseconds = dot_parts
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(centiseconds) / 100
    parts = normalized.split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    if len(parts) != 3:
        raise ValueError(f"Mốc timeline không hợp lệ: {value}")
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_timeline_times(path: Path) -> list[tuple[float, float]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    times: list[tuple[float, float]] = []
    for left, right in _TIMELINE_RANGE.findall(text):
        start, end = _timeline_seconds(left), _timeline_seconds(right)
        if end > start:
            times.append((start, end))
    if not times:
        raise ValueError("File tạo ảnh không có timeline hợp lệ")
    return times


def parse_timing_cues_detailed(path: Path) -> list[dict[str, Any]]:
    """Read prompt, subtitle, table, or structured timeline with full cue metadata."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")

    def _fmt_tc(s: float, e: float) -> str:
        def _hms(v: float) -> str:
            h = int(v // 3600)
            m = int((v % 3600) // 60)
            sec = v % 60
            if h > 0:
                return f"{h:02d}:{m:02d}:{sec:05.2f}"
            return f"{m:02d}:{sec:05.2f}"
        return f"{_hms(s)} - {_hms(e)}"

    def _build_cues(items: list[tuple[object, object, str, str]]) -> list[dict[str, Any]]:
        cues: list[dict[str, Any]] = []
        for left, right, label, expected in items:
            try:
                start, end = _timeline_seconds(str(left)), _timeline_seconds(str(right))
            except (TypeError, ValueError):
                continue
            if end > start:
                idx = len(cues) + 1
                cues.append({
                    "index": idx,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(end - start, 3),
                    "timecode": _fmt_tc(start, end),
                    "label": label.strip() if label else "",
                    "expected_name": expected.strip() if expected else f"{idx:03d}.*",
                })
        if not cues:
            raise ValueError("Timeline không có mốc thời gian hợp lệ")
        return cues

    def prompt_lines() -> list[dict[str, Any]]:
        items: list[tuple[object, object, str, str]] = []
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            match = _TIMELINE_RANGE.search(line_str)
            if match:
                left, right = match.group(1), match.group(2)
                prefix_match = re.match(r"^([a-zA-Z0-9_\-\.]+?)\s*\[", line_str)
                expected = prefix_match.group(1) if prefix_match else ""
                items.append((left, right, line_str, expected))
        return _build_cues(items)

    def arrow() -> list[dict[str, Any]]:
        items: list[tuple[object, object, str, str]] = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "-->" in line:
                left, right = line.split("-->", 1)
                sub_lines: list[str] = []
                for j in range(i + 1, min(i + 4, len(lines))):
                    if not lines[j].strip() or "-->" in lines[j]:
                        break
                    sub_lines.append(lines[j].strip())
                label = " ".join(sub_lines)
                items.append((left.strip(), right.strip().split()[0], label, ""))
        return _build_cues(items)

    def ass() -> list[dict[str, Any]]:
        items: list[tuple[object, object, str, str]] = []
        for line in text.splitlines():
            if line.lstrip().lower().startswith("dialogue:"):
                fields = line.split(":", 1)[1].split(",", 9)
                if len(fields) >= 3:
                    text_field = fields[9] if len(fields) >= 10 else ""
                    clean_text = re.sub(r"\{.*?\}", "", text_field).strip()
                    items.append((fields[1], fields[2], clean_text, ""))
        return _build_cues(items)

    def table(delimiter: str) -> list[dict[str, Any]]:
        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
        if not rows:
            raise ValueError("Timeline bảng trống")
        header = [re.sub(r"[^a-z]", "", cell.lower()) for cell in rows[0]]
        start_aliases = {"start", "starttime", "from", "in"}
        end_aliases = {"end", "endtime", "to", "out"}
        name_aliases = {"name", "file", "filename", "media", "image", "video"}
        label_aliases = {"prompt", "text", "description", "title", "caption", "scene"}

        start_index = next((i for i, v in enumerate(header) if v in start_aliases), None)
        end_index = next((i for i, v in enumerate(header) if v in end_aliases), None)
        name_index = next((i for i, v in enumerate(header) if v in name_aliases), None)
        label_index = next((i for i, v in enumerate(header) if v in label_aliases), None)

        data = rows[1:] if start_index is not None and end_index is not None else rows
        start_index, end_index = start_index or 0, end_index or 1

        items: list[tuple[object, object, str, str]] = []
        for row in data:
            if len(row) > max(start_index, end_index):
                exp = row[name_index] if name_index is not None and len(row) > name_index else ""
                lbl = row[label_index] if label_index is not None and len(row) > label_index else ""
                items.append((row[start_index], row[end_index], lbl, exp))
        return _build_cues(items)

    def structured() -> list[dict[str, Any]]:
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = next((payload[key] for key in ("scenes", "items", "segments", "cues", "timeline", "data") if isinstance(payload.get(key), list)), [payload])
        if not isinstance(payload, list):
            raise ValueError("JSON timeline phải là danh sách")
        items: list[tuple[object, object, str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            start = next((item[k] for k in ("start", "startTime", "start_time", "from", "in") if k in item), None)
            end = next((item[k] for k in ("end", "endTime", "end_time", "to", "out") if k in item), None)
            lbl = str(next((item[k] for k in ("prompt", "text", "description", "caption", "scene") if k in item), "") or "")
            exp = str(next((item[k] for k in ("name", "file", "filename", "image", "video") if k in item), "") or "")
            items.append((start, end, lbl, exp))
        return _build_cues(items)

    def lrc() -> list[dict[str, Any]]:
        matches = list(re.finditer(r"(?m)^\s*\[(\d+):(\d+(?:[.,]\d+)?)\](.*)$", text))
        items: list[tuple[object, object, str, str]] = []
        for i in range(len(matches) - 1):
            m1, m2 = matches[i], matches[i + 1]
            t1 = int(m1.group(1)) * 60 + float(m1.group(2).replace(",", "."))
            t2 = int(m2.group(1)) * 60 + float(m2.group(2).replace(",", "."))
            lbl = m1.group(3).strip()
            items.append((t1, t2, lbl, ""))
        return _build_cues(items)

    suffix = path.suffix.lower()
    preferred = {
        ".srt": arrow, ".vtt": arrow, ".ass": ass, ".ssa": ass,
        ".csv": lambda: table(","), ".tsv": lambda: table("\t"),
        ".json": structured, ".lrc": lrc,
    }.get(suffix)
    parsers = ([preferred] if preferred else []) + [
        prompt_lines, arrow, ass, structured,
        lambda: table(","), lambda: table("\t"), lrc,
    ]
    for parser in parsers:
        try:
            return parser()
        except (ValueError, TypeError, IndexError, json.JSONDecodeError, csv.Error):
            continue
    raise ValueError("File không có timeline hợp lệ")


def parse_timing_times(path: Path) -> list[tuple[float, float]]:
    """Read common prompt, subtitle, table, and structured timeline formats."""
    return [(cue["start"], cue["end"]) for cue in parse_timing_cues_detailed(path)]


def select_cues_for_media(
    cues: list[tuple[float, float]], media_count: int, allow_missing: bool = False,
) -> list[tuple[float, float]]:
    if media_count >= len(cues):
        return cues
    if not allow_missing:
        raise ValueError(f"Thiếu ảnh/video: cần ít nhất {len(cues)} file, hiện có {media_count}")
    return cues[:media_count]


def preview_media_window(
    media: list[Path], durations: list[float], preview_seconds: float, speed: float,
) -> tuple[list[Path], list[float]]:
    if preview_seconds <= 0:
        return media, durations
    remaining = preview_seconds * speed
    selected_media: list[Path] = []
    selected_durations: list[float] = []
    for source, duration in zip(media, durations):
        if remaining <= 0:
            break
        selected_media.append(source)
        selected_durations.append(min(duration, remaining))
        remaining -= duration
    return selected_media, selected_durations


def media_duration(path: Path, image_duration: float = 5.0) -> float:
    """Return a natural clip duration, with a stable default for still images."""
    if not is_video(path):
        return image_duration
    try:
        result = subprocess.run(
            [_ff_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15, check=True,
        )
        duration = float(json.loads(result.stdout).get("format", {}).get("duration") or 0)
        if duration > 0:
            return duration
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    return image_duration


def sequential_media_times(media: list[Path], image_duration: float = 5.0) -> list[tuple[float, float]]:
    """Build a no-timeline sequence: clips keep duration; images last five seconds."""
    start = 0.0
    cues: list[tuple[float, float]] = []
    for source in media:
        end = start + max(0.04, media_duration(source, image_duration))
        cues.append((start, end))
        start = end
    return cues


def image_resolution(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [_ff_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=15, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    return width - width % 2, height - height % 2


def _output_resolution(options: dict, first_media: Path) -> tuple[int, int]:
    resolution = str(options.get("resolution", DEFAULT_OUTPUT_RESOLUTION))
    if resolution != "auto":
        return tuple(int(value) for value in resolution.split("x", 1))
    source_width, source_height = image_resolution(first_media)
    scale = 1080 / min(source_width, source_height)
    return (
        max(2, round(source_width * scale / 2) * 2),
        max(2, round(source_height * scale / 2) * 2),
    )


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _inpaint_still_logo(source: Path, dl: dict[str, Any], work: Path, index: int) -> Path:
    """Reconstruct a logo patch on stills before the final video encode."""
    import cv2
    import numpy as np
    image = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"Cannot decode logo input: {source.name}")
    height, width = image.shape[:2]
    x = max(0, min(width - 1, round(float(dl.get("x", 82)) / 100 * width)))
    y = max(0, min(height - 1, round(float(dl.get("y", 94)) / 100 * height)))
    w = max(2, min(width - x, round(float(dl.get("w", 16)) / 100 * width)))
    h = max(2, min(height - y, round(float(dl.get("h", 4)) / 100 * height)))
    # Keep the mask tight; a large feathered delogo rectangle is what causes
    # visible blur around the watermark.
    mask = np.zeros((height, width), dtype=np.uint8)
    pad = max(1, round(min(width, height) * 0.003))
    cv2.rectangle(mask, (max(0, x - pad), max(0, y - pad)),
                  (min(width - 1, x + w + pad), min(height - 1, y + h + pad)), 255, -1)
    restored = cv2.inpaint(image, mask, max(2, min(7, round(min(width, height) * 0.004))), cv2.INPAINT_TELEA)
    target = work / f"delogo-still-{index:05d}.png"
    ok, encoded = cv2.imencode('.png', restored)
    if not ok:
        raise RuntimeError(f"Cannot encode repaired image: {source.name}")
    encoded.tofile(target)
    return target


def _prepare_media_inputs(
    job_id: str, media: list[Path], durations: list[float], work: Path,
    allow_missing: bool,
) -> tuple[list[Path], list[float]]:
    """Validate source media and make WebP inputs decoder-safe for FFmpeg.

    Flow downloads are not guaranteed to be complete when a job starts.  A
    truncated WebP can otherwise survive all preparation steps and fail late
    inside FFmpeg's concat decoder.  Invalid inputs are either reported before
    rendering or skipped together with their duration when the user enabled
    the existing ``allowMissingMedia`` option.
    """
    normalized: list[Path] = []
    kept_durations: list[float] = []
    webp_dir = work / "media-normalized"
    for index, (source, duration) in enumerate(zip(media, durations)):
        reason = ""
        target = source
        try:
            if not source.is_file() or source.stat().st_size <= 0:
                raise ValueError("file không tồn tại hoặc rỗng")
            if is_video(source):
                # Video validation remains delegated to FFmpeg, but an empty
                # file is rejected here so the job fails with a useful cause.
                pass
            else:
                from PIL import Image

                # verify() checks container integrity without decoding the
                # pixels; reopen/load catches truncated payloads as well.
                with Image.open(source) as image:
                    image.verify()
                with Image.open(source) as image:
                    for frame in range(getattr(image, "n_frames", 1)):
                        image.seek(frame)
                        image.load()
                    if source.suffix.lower() == ".webp":
                        webp_dir.mkdir(parents=True, exist_ok=True)
                        target = webp_dir / f"media_{index:05d}.png"
                        image.seek(0)
                        mode = "RGBA" if "A" in image.getbands() else "RGB"
                        image.convert(mode).save(target, format="PNG")
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__

        if reason:
            message = f"MEDIA_INVALID: media không hợp lệ / invalid media {source.name}: {reason}"
            if allow_missing:
                _log(job_id, f"Bỏ qua media lỗi / Skipping invalid media: {source.name} · {reason}")
                continue
            raise RuntimeError(message)
        normalized.append(target)
        kept_durations.append(duration)

    if not normalized:
        raise RuntimeError("MEDIA_INVALID: Không còn ảnh/video hợp lệ để ghép / no valid image or video remains")
    return normalized, kept_durations


def _encoder_args(use_gpu: bool, crf: int, *, intermediate: bool = False) -> list[str]:
    """Encoder args.  intermediate=True uses faster presets for throwaway segments."""
    if use_gpu:
        # ponytail: fast=True → p3/veryfast cho segment trung gian, p5/fast cho final
        return h264_encoder_args(quality=crf, fast=intermediate)
    preset = "veryfast" if intermediate else "fast"
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]


def _run_stage(job_id: str, cmd: list[str]) -> None:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0,
    )
    with _LOCK:
        _PROCS[job_id] = proc
    _, stderr = proc.communicate()
    with _LOCK:
        _PROCS.pop(job_id, None)
    if (get_job(job_id) or {}).get("status") == "cancelled":
        raise InterruptedError
    if proc.returncode:
        detail = "\n".join(stderr.splitlines()[-8:])
        _log(job_id, f"FFmpeg lỗi:\n{detail}")
        raise RuntimeError(f"FFmpeg kết thúc với mã {proc.returncode}: {detail}")


def _has_logo_region(source: Path, x: int, y: int, w: int, h: int) -> bool:
    """Kiểm tra vùng (x,y,w,h) có đủ phức tạp (variance) để là logo không.
    ponytail: heuristic đơn giản — vùng trơn = không logo → skip delogo."""
    try:
        from PIL import Image
        import numpy as np
        img_exts = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp"}
        if source.suffix.lower() not in img_exts:
            return True  # video → assume có logo, safe hơn
        with Image.open(source) as im:
            iw, ih = im.size
            cx, cy = min(x, iw - 1), min(y, ih - 1)
            cw, ch = min(w, iw - cx), min(h, ih - cy)
            if cw < 4 or ch < 4:
                return False
            crop = im.crop((cx, cy, cx + cw, cy + ch)).convert("L")
            arr = np.array(crop, dtype=float)
            return float(arr.std()) > 8  # std > 8 → có nội dung (logo/text)
    except Exception:
        return True  # lỗi → safe: áp delogo


def _prepare_video_segments(
    job_id: str, media: list[Path], durations: list[float], work: Path,
    width: int, height: int, fps: int, crf: int, use_gpu: bool, zoom: str = "off",
    delogo_prefix: str = "", gpu_encoder: str | None = None,
) -> list[Path]:
    # Parse delogo params 1 lần để check per-file
    dl_params: tuple[int, int, int, int] | None = None
    if delogo_prefix:
        import re as _re
        m = _re.search(r"x=(\d+):y=(\d+):w=(\d+):h=(\d+)", delogo_prefix)
        if m:
            dl_params = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    base_vf_no_dl = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}"
    )
    base_vf_dl = f"{delogo_prefix}{base_vf_no_dl}"

    # ponytail: pre-compute mỗi segment trước khi song song hóa — random variant
    # phải tuần tự để tránh 2 cảnh liên tiếp dùng cùng hướng Ken Burns.
    segment_plans: list[tuple[int, Path, float, str, bool]] = []
    last_variant = -1
    for index, (source, duration) in enumerate(zip(media, durations)):
        variant = random.randrange(16)
        if variant == last_variant:
            variant = (variant + random.randrange(1, 16)) % 16
        last_variant = variant
        motion = _motion_filter(zoom, width, height, fps, variant, duration)
        # Delogo was explicitly requested: apply it to every valid image/video
        # instead of silently skipping images whose logo region looks flat.
        use_dl = bool(delogo_prefix)
        chosen_vf = base_vf_dl if use_dl else base_vf_no_dl
        vf = f"{chosen_vf}{',' + motion if motion else ''},format=yuv420p"
        segment_plans.append((index, source, duration, vf, use_dl))

    total = len(segment_plans)
    # ponytail: session limit per encoder type
    # - NVENC (consumer): max 2-3 concurrent encode sessions
    # - VideoToolbox (Apple) / AMF (AMD) / QSV (Intel): no hard limit → parallel OK
    # - CPU libx264: each instance uses own cores → parallel with cap
    enc = (gpu_encoder or "").lower()
    if not use_gpu:
        workers = min(4, max(1, os.cpu_count() or 2))
    elif "nvenc" in enc:
        workers = 2  # NVENC consumer limit
    else:
        # VideoToolbox / AMF / QSV — safe to parallelize up to cpu_count/2
        workers = min(max(1, (os.cpu_count() or 4) // 2), 6)
    segments: list[Path | None] = [None] * total
    completed = 0

    def _encode_one(plan: tuple[int, Path, float, str, bool]) -> tuple[int, Path]:
        idx, source, duration, vf, did_dl = plan
        if did_dl:
            _log(job_id, f"Clip {idx + 1}: delogo ✓ · {source.name}")
        _log(job_id, f"Chuẩn bị clip {idx + 1}/{total}: {source.name} ({duration:.2f}s)")
        output = work / f"segment_{idx:05d}.mp4"
        cmd = [_ff_bin("ffmpeg"), "-y"]
        cmd += ["-stream_loop", "-1"] if is_video(source) else ["-loop", "1"]
        cmd += ["-i", str(source), "-t", f"{duration:.3f}", "-vf", vf, "-an"]
        cmd += _encoder_args(use_gpu, crf, intermediate=True)
        cmd += ["-movflags", "+faststart", str(output)]
        _run_stage(job_id, cmd)
        return idx, output

    if workers > 1:
        _log(job_id, f"Song song {workers} luồng encode segment")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_encode_one, plan): plan[0] for plan in segment_plans}
        for future in as_completed(futures):
            if (get_job(job_id) or {}).get("status") == "cancelled":
                pool.shutdown(wait=False, cancel_futures=True)
                raise InterruptedError
            idx, output = future.result()
            segments[idx] = output
            completed += 1
            _update(job_id, status="processing", progress=round(completed / total * 35, 1))

    return [s for s in segments if s is not None]


def create_job(
    name: str, work: Path, images: list[Path], audio: Path | None,
    timeline: Path | None, srt: Path | None, options: dict, watermark: Path | None = None,
    output_target: Path | None = None,
) -> dict:
    job_id = uuid.uuid4().hex[:10]
    output = output_target or downloads_folder("subtitle-image") / f"{Path(name).stem or 'ghep-anh-srt'}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id, "name": name, "status": "queued", "progress": 0,
        "error": "", "outputSize": 0, "output": str(output), "work": str(work),
        "logs": [f"[{time.strftime('%H:%M:%S')}] Đã tạo job: {name}"],
        "images": [str(p) for p in images], "audio": str(audio) if audio else "",
        "timeline": str(timeline) if timeline else "", "srt": str(srt) if srt else "", "watermark": str(watermark) if watermark else "",
        "options": options,
    }
    with _LOCK:
        _JOBS[job_id] = job
    return dict(job)


def list_jobs() -> list[dict]:
    with _LOCK:
        return [dict(job) for job in _JOBS.values()]


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _update(job_id: str, **values: Any) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(values)


def _log(job_id: str, message: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        logs = job.setdefault("logs", [])
        logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        del logs[:-200]


def _publish_render(job_id: str, source: Path, name: str) -> Path:
    """Expose a completed SRT render in the shared Rendered tab catalogue."""
    exports = PUBLIC_DATA / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    published = exports / f"srt-{job_id}.mp4"
    shutil.copy2(source, published)
    published.with_suffix(".json").write_text(
        json.dumps({"name": Path(name).stem or "Ghép ảnh/video SRT", "projectId": "srt"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return published


def output_path(job_id: str) -> Path | None:
    job = get_job(job_id)
    path = Path(job["output"]) if job else None
    return path if job and job["status"] == "done" and path and path.is_file() else None


def cancel(job_id: str) -> bool:
    with _LOCK:
        if job_id not in _JOBS:
            return False
        _JOBS[job_id]["status"] = "cancelled"
        proc = _PROCS.get(job_id)
    if proc:
        kill_process_tree(proc.pid)
    return True


def pause(job_id: str, paused: bool) -> bool:
    with _LOCK:
        proc = _PROCS.get(job_id)
        job = _JOBS.get(job_id)
    if not proc or not job or proc.poll() is not None:
        return False
    if sys.platform == "win32":
        import ctypes
        access = 0x0800
        handle = ctypes.windll.kernel32.OpenProcess(access, False, proc.pid)
        if not handle:
            return False
        try:
            fn = ctypes.windll.ntdll.NtSuspendProcess if paused else ctypes.windll.ntdll.NtResumeProcess
            if fn(handle) != 0:
                return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        os.kill(proc.pid, signal.SIGSTOP if paused else signal.SIGCONT)
    _update(job_id, status="paused" if paused else "processing")
    return True


def _srt_ts_to_ass(ts: str) -> str:
    """'01:23:45,678' hoặc '01:23:45.678' → '1:23:45.67' (ASS centiseconds)"""
    ts = ts.replace(".", ",")
    h, m, s_ms = ts.split(":")
    s, ms = s_ms.split(",")
    return f"{int(h)}:{m}:{s}.{ms[:2]}"


def _ffmpeg_subtitle(path: Path, font_name: str = "system", font_size: int = 8, margin_bottom: int = 18,
                     bg_style: str = "solid", text_color: str = "#ffffff", bg_color: str = "#000000",
                     opacity: int = 55) -> str:
    """Generate ASS từ SRT với inline \\blur tags — Gaussian blur = CSS textShadow.
    force_style trên SRT không hỗ trợ inline tags; cần ASS file thực.
    Font: fontsdir (đã xác nhận hoạt động với DirectWrite).
    """
    # ponytail: Bold flag phải khớp nameID=2 (subfamily) trong TTF.
    # libass dùng flag này để tìm variant — sai flag → pick sai file hoặc fallback.
    # sub=Bold → Bold=1; sub=Regular → Bold=0
    _FONT_INFO: dict[str, tuple[str, str, int]] = {
        "system":   ("NotoSans-Bold.ttf",        "Noto Sans",         1),  # sub=Bold
        "segoe":    ("Inter-Bold.ttf",           "Inter",             0),  # sub=Regular
        "arial":    ("Arimo-Bold.ttf",           "Arimo",             0),  # sub=Regular
        "bold":     ("ArchivoBlack-Regular.ttf", "Archivo SemiBold",  0),  # sub=Regular
        "helvetica":("Roboto-Bold.ttf",          "Roboto",            0),  # sub=Regular
        "verdana":  ("OpenSans-Bold.ttf",        "Open Sans",         0),  # sub=Regular
        "tahoma":   ("Carlito-Bold.ttf",         "Carlito",           1),  # sub=Bold
        "trebuchet":("FiraSans-Bold.ttf",        "Fira Sans",         1),  # sub=Bold
        "rounded":  ("Nunito-Bold.ttf",          "Nunito ExtraLight", 0),  # sub=Regular
        "impact":   ("Anton-Regular.ttf",        "Anton",             0),  # sub=Regular
        "georgia":  ("Merriweather-Bold.ttf",    "Merriweather Light",0),  # sub=Regular
        "times":    ("Tinos-Bold.ttf",           "Tinos",             1),  # sub=Bold
        "palatino": ("Literata-Bold.ttf",        "Literata",          0),  # sub=Regular
        "garamond": ("EBGaramond-Bold.ttf",      "EB Garamond",       0),  # sub=Regular
        "courier":  ("CourierPrime-Bold.ttf",    "Cousine",           1),  # sub=Bold
        "mono":     ("NotoSansMono-Bold.ttf",    "Noto Sans Mono",    0),  # sub=Regular
        "comic":    ("ComicNeue-Bold.ttf",       "Patrick Hand",      0),  # sub=Regular
        "cjk":      ("NotoSansSC-Bold.ttf",      "Noto Sans SC Thin", 0),  # sub=Regular
        "meiryo":   ("NotoSansJP-Bold.ttf",      "Noto Sans JP Thin", 0),  # sub=Regular
        "malgun":   ("NotoSansKR-Bold.ttf",      "Noto Sans KR Thin", 0),  # sub=Regular
    }
    _ttf, real_font, bold_flag = _FONT_INFO.get(font_name.lower(), _FONT_INFO["system"])
    fonts_dir = (Path(__file__).parent.parent.parent / "frontend" / "public" / "fonts").resolve()

    # ── Màu chữ (ASS BBGGRR little-endian) ───────────────────────────────────
    tc = text_color.lstrip("#")
    if len(tc) != 6: tc = "ffffff"
    tr, tg, tb = int(tc[0:2], 16), int(tc[2:4], 16), int(tc[4:6], 16)
    ass_primary = f"&H00{tb:02X}{tg:02X}{tr:02X}"

    # ── ASS Style + inline override tags theo bg_style ────────────────────────
    # CSS textShadow '0 2px 4px rgba(0,0,0,0.9)' = subtle dark glow phía sau chữ
    # ASS tương đương: Shadow=1 với ShadowColour tối — chữ vẫn sắc nét như CSS
    # KHÔNG dùng \blur — nó blur toàn bộ glyph (cả fill), làm chữ mờ hơn preview
    if bg_style in ("solid", "box"):
        bc = bg_color.lstrip("#")
        if len(bc) != 6: bc = "000000"
        br, bg_r, bb = int(bc[0:2], 16), int(bc[2:4], 16), int(bc[4:6], 16)
        a = 255 - max(0, min(255, int(255 * opacity / 100)))
        ass_bg = f"&H{a:02X}{bb:02X}{bg_r:02X}{br:02X}"
        pad = 2 if bg_style == "box" else 1.2
        # BorderStyle=3: nền box; Shadow=1 + dark colour = subtle drop shadow
        style_line = (
            f"Style: Default,{real_font},{font_size},"
            f"{ass_primary},&H00FFFFFF,{ass_bg},{ass_bg},"
            f"{bold_flag},0,0,0,100,100,0,0,3,{pad},1,2,20,20,{margin_bottom},0"
        )
    else:
        # none: outline mỏng tối + shadow nhẹ = CSS textShadow
        style_line = (
            f"Style: Default,{real_font},{font_size},"
            f"{ass_primary},&H00FFFFFF,&H1A000000,&H1A000000,"
            f"{bold_flag},0,0,0,100,100,0,0,1,1,1,2,20,20,{margin_bottom},0"
        )
    # Không inline override — style definition đã đủ
    inline_tag = ""

    # ── ASS header ────────────────────────────────────────────────────────────
    header = (
        "[Script Info]\r\n"
        "ScriptType: v4.00+\r\n"
        "PlayResX: 384\r\n"
        "PlayResY: 288\r\n"
        "WrapStyle: 0\r\n\r\n"
        "[V4+ Styles]\r\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
        f"{style_line}\r\n\r\n"
        "[Events]\r\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    )

    # ── Parse SRT → ASS Dialogue events ───────────────────────────────────────
    srt_text = path.read_text(encoding="utf-8-sig", errors="replace")
    events: list[str] = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})",
                line.strip(),
            )
            if m:
                start = _srt_ts_to_ass(m.group(1))
                end   = _srt_ts_to_ass(m.group(2))
                text  = r"\N".join(l.strip() for l in lines[i + 1:] if l.strip())
                text  = re.sub(r"<[^>]+>", "", text)  # strip HTML tags
                events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{inline_tag}{text}")
                break

    ass_path = path.with_suffix(".sub.ass")
    ass_path.write_bytes((header + "\r\n".join(events) + "\r\n").encode("utf-8-sig"))

    # ── FFmpeg filter ─────────────────────────────────────────────────────────
    ass_ff   = ass_path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
    fonts_ff = str(fonts_dir).replace("\\", "/").replace(":", r"\:")
    return f"subtitles=filename='{ass_ff}':fontsdir='{fonts_ff}'"


def _parse_srt_blocks(srt_path: Path) -> list[tuple[float, float, str]]:
    text = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})",
                line.strip(),
            )
            if m:
                start = _seconds(m.group(1).replace(",", "."))
                end = _seconds(m.group(2).replace(",", "."))
                cue_text = "\n".join(l.strip() for l in lines[i + 1:] if l.strip())
                cue_text = re.sub(r"<[^>]+>", "", cue_text)
                if end > start and cue_text.strip():
                    cues.append((start, end, cue_text.strip()))
                break
    return cues


def _build_subtitle_overlay_concat(
    srt_path: Path,
    work: Path,
    width: int,
    height: int,
    font_name: str = "system",
    font_size: int = 8,
    margin_bottom: int = 18,
    bg_style: str = "solid",
    text_color: str = "#ffffff",
    bg_color: str = "#000000",
    opacity: int = 55,
) -> Path:
    """Render high-resolution subtitle PNG sequence for native FFmpeg overlay (zero libass dependency)."""
    from PIL import Image, ImageDraw, ImageFont
    from pipeline.export.fonts import _resolve_font_name

    cues = _parse_srt_blocks(srt_path)
    frames_dir = work / "subtitle_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # 1. Blank transparent frame
    blank_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    blank_path = frames_dir / "blank.png"
    blank_img.save(blank_path, format="PNG")

    if not cues:
        concat_path = frames_dir / "subtitles.ffconcat"
        concat_path.write_text("ffconcat version 1.0\nfile 'blank.png'\n", encoding="utf-8")
        return concat_path

    # 2. Font mapping
    _FONT_INFO: dict[str, str] = {
        "system": "NotoSans-Bold.ttf",
        "segoe": "Inter-Bold.ttf",
        "arial": "Arimo-Bold.ttf",
        "bold": "ArchivoBlack-Regular.ttf",
        "helvetica": "Roboto-Bold.ttf",
        "verdana": "OpenSans-Bold.ttf",
        "tahoma": "Carlito-Bold.ttf",
        "trebuchet": "FiraSans-Bold.ttf",
        "rounded": "Nunito-Bold.ttf",
        "impact": "Anton-Regular.ttf",
        "georgia": "Merriweather-Bold.ttf",
        "times": "Tinos-Bold.ttf",
        "palatino": "Literata-Bold.ttf",
        "garamond": "EBGaramond-Bold.ttf",
        "courier": "CourierPrime-Bold.ttf",
        "mono": "NotoSansMono-Bold.ttf",
        "comic": "ComicNeue-Bold.ttf",
        "cjk": "NotoSansSC-Bold.ttf",
        "meiryo": "NotoSansJP-Bold.ttf",
        "malgun": "NotoSansKR-Bold.ttf",
    }
    ttf_name = _FONT_INFO.get(font_name.lower(), "NotoSans-Bold.ttf")
    font_file = _resolve_font_name(ttf_name) or _resolve_font_name("NotoSans-Bold.ttf") or _resolve_font_name("Arial.ttf")

    # Font size relative to ASS 288p base
    px_font_size = max(12, int(font_size * (height / 288)))
    px_margin_bottom = int(margin_bottom * (height / 288))

    font = None
    if font_file:
        try:
            font = ImageFont.truetype(font_file, px_font_size)
        except Exception:
            font = None
    if font is None:
        font = ImageFont.load_default()

    # Colors
    tc = text_color.lstrip("#")
    if len(tc) != 6:
        tc = "ffffff"
    tr, tg, tb = int(tc[0:2], 16), int(tc[2:4], 16), int(tc[4:6], 16)
    text_rgba = (tr, tg, tb, 255)

    bc = bg_color.lstrip("#")
    if len(bc) != 6:
        bc = "000000"
    br, bgr, bb = int(bc[0:2], 16), int(bc[2:4], 16), int(bc[4:6], 16)
    bg_alpha = max(0, min(255, int(255 * opacity / 100)))
    bg_rgba = (br, bgr, bb, bg_alpha)

    # Render each cue
    for i, (start, end, cue_text) in enumerate(cues):
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Wrap text lines
        raw_lines = [l.strip() for l in cue_text.replace("\\N", "\n").splitlines() if l.strip()]
        lines: list[str] = []
        max_line_width = int(width * 0.88)
        for raw_line in raw_lines:
            words = raw_line.split()
            cur = ""
            for w in words:
                test = f"{cur} {w}".strip()
                bbox = draw.textbbox((0, 0), test, font=font)
                if bbox[2] - bbox[0] > max_line_width and cur:
                    lines.append(cur)
                    cur = w
                else:
                    cur = test
            if cur:
                lines.append(cur)

        if not lines:
            img.save(frames_dir / f"cue_{i:05d}.png", format="PNG")
            continue

        # Collect line metrics with exact visual glyph bounding boxes
        line_data: list[dict[str, Any]] = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            l_min, t_min, r_max, b_max = bbox
            line_w = max(1, r_max - l_min)
            line_h = max(1, b_max - t_min)
            line_data.append({
                "text": line,
                "left": l_min,
                "top": t_min,
                "w": line_w,
                "h": line_h,
            })

        line_spacing = max(2, int(px_font_size * 0.25))
        total_text_h = sum(d["h"] for d in line_data) + line_spacing * (len(line_data) - 1)
        max_w = max(d["w"] for d in line_data)

        pad_x = max(12, int(px_font_size * 0.45))
        pad_y = max(8, int(px_font_size * 0.30))

        box_w = max_w + pad_x * 2
        box_h = total_text_h + pad_y * 2

        box_left = max(0, (width - box_w) // 2)
        box_right = min(width, box_left + box_w)
        box_bottom = min(height, height - px_margin_bottom)
        box_top = max(0, box_bottom - box_h)

        if bg_style in ("solid", "box"):
            radius = int(px_font_size * 0.22) if bg_style == "box" else 0
            if radius > 0:
                draw.rounded_rectangle([box_left, box_top, box_right, box_bottom], radius=radius, fill=bg_rgba)
            else:
                draw.rectangle([box_left, box_top, box_right, box_bottom], fill=bg_rgba)

        cur_line_visual_top = box_top + pad_y
        stroke_w = max(1, px_font_size // 10) if bg_style == "none" else 0
        stroke_color = (0, 0, 0, 230) if bg_style == "none" else None

        for d in line_data:
            draw_x = (width - d["w"]) // 2 - d["left"]
            draw_y = cur_line_visual_top - d["top"]
            if stroke_w > 0:
                draw.text((draw_x, draw_y), d["text"], font=font, fill=text_rgba, stroke_width=stroke_w, stroke_fill=stroke_color)
            else:
                draw.text((draw_x, draw_y), d["text"], font=font, fill=text_rgba)
            cur_line_visual_top += d["h"] + line_spacing

        img.save(frames_dir / f"cue_{i:05d}.png", format="PNG")

    # Build ffconcat
    concat_lines = ["ffconcat version 1.0"]
    cur_time = 0.0
    for i, (start, end, _) in enumerate(cues):
        if start > cur_time + 0.02:
            gap = start - cur_time
            concat_lines.append("file 'blank.png'")
            concat_lines.append(f"duration {gap:.3f}")
        dur = max(0.04, end - start)
        concat_lines.append(f"file 'cue_{i:05d}.png'")
        concat_lines.append(f"duration {dur:.3f}")
        cur_time = end
    concat_lines.append("file 'blank.png'")

    concat_path = frames_dir / "subtitles.ffconcat"
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    return concat_path







def _logo_step_axis(step: str, axis: int) -> str:
    index = f"mod({step},{len(LOGO_RANDOM_POSITIONS)})"
    expression = f"{LOGO_RANDOM_POSITIONS[-1][axis]:.4f}"
    for position_index in range(len(LOGO_RANDOM_POSITIONS) - 2, -1, -1):
        expression = f"if(eq({index},{position_index}),{LOGO_RANDOM_POSITIONS[position_index][axis]:.4f},{expression})"
    return expression


def _logo_position(x_percent: float = 88, y_percent: float = 88,
                   moving: bool = False, cycle: float = 6,
                   safe_margin: float = 4) -> tuple[str, str]:
    if moving:
        margin = max(0, min(20, float(safe_margin))) / 100
        step = f"floor(t/{max(0.5, cycle):.3f})"
        x = f"W*{margin:.4f}+max(0,W-w-W*{margin * 2:.4f})*({_logo_step_axis(step, 0)})"
        y = f"H*{margin:.4f}+max(0,H*0.75-h-H*{margin * 2:.4f})*({_logo_step_axis(step, 1)})"
        return x, y
    x = max(0, min(100, float(x_percent))) / 100
    y = max(0, min(100, float(y_percent))) / 100
    return f"min(W-w,max(0,W*{x:.4f}))", f"min(H-h,max(0,H*{y:.4f}))"


def _text_logo_position(x_percent: float = 88, y_percent: float = 88,
                        moving: bool = False, cycle: float = 6,
                        safe_margin: float = 4) -> tuple[str, str]:
    if moving:
        margin = max(0, min(20, float(safe_margin))) / 100
        step = f"floor(t/{max(0.5, cycle):.3f})"
        x = f"W*{margin:.4f}+max(0,W-tw-W*{margin * 2:.4f})*({_logo_step_axis(step, 0)})"
        # Phụ đề nằm dưới: logo ngẫu nhiên chỉ dùng 75% chiều cao phía trên.
        y = f"H*{margin:.4f}+max(0,H*0.75-th-H*{margin * 2:.4f})*({_logo_step_axis(step, 1)})"
        return x, y
    x = max(0, min(100, float(x_percent))) / 100
    y = max(0, min(100, float(y_percent))) / 100
    return f"min(W-tw,max(0,W*{x:.4f}))", f"min(H-th,max(0,H*{y:.4f}))"


def _text_logo_filter(logo: dict, height: int) -> str:
    raw = str(logo.get("text") if logo.get("source") == "text" else logo.get("icon", "★"))
    text = raw.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'").replace("%", r"\%")
    opacity = max(5, min(100, float(logo.get("opacity", 85)))) / 100
    font_size = max(6, min(160, int(logo.get("fontSize") or 10)))
    color = str(logo.get("color") or "#ffffff")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = "#ffffff"
    visible = max(0.5, float(logo.get("visibleSec") or 4))
    hidden = max(0, float(logo.get("hiddenSec") or 2))
    fade = min(max(0, float(logo.get("fadeSec") or 0.5)), visible / 2)
    cycle = visible + hidden
    moving = logo.get("motion") == "random"
    x, y = _text_logo_position(
        logo.get("x", 88), logo.get("y", 88), moving, cycle, logo.get("safeMargin", 4),
    )
    enable = ""
    if logo.get("scope") == "range":
        start = max(0, float(logo.get("start", 0)))
        end = max(start, float(logo.get("end", start)))
        enable = f":enable='between(t,{start:.3f},{end:.3f})'"
    font_option = ""
    if sys.platform == "win32":
        font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arialbd.ttf"
        if not font.is_file():
            font = font.with_name("arial.ttf")
        if font.is_file():
            escaped_font = font.as_posix().replace(":", r"\:").replace("'", r"\'")
            font_option = f"fontfile='{escaped_font}':"
    alpha = f"{opacity:.3f}"
    if moving:
        local = f"mod(t,{cycle:.3f})"
        if fade > 0:
            alpha = (
                f"if(lt({local},{visible:.3f}),{opacity:.3f}*"
                f"min(1,min({local}/{fade:.3f},({visible:.3f}-{local})/{fade:.3f})),0)"
            )
        else:
            alpha = f"if(lt({local},{visible:.3f}),{opacity:.3f},0)"
    return (
        f"drawtext={font_option}text='{text}':fontsize={font_size}:"
        f"fontcolor={color}:shadowcolor=black@0.85:shadowx=2:shadowy=2:"
        f"alpha='{alpha}':x='{x}':y='{y}'{enable}"
    )


def _render_logo_asset(logo: dict, work: Path) -> Path:
    """Render a text/icon logo once so FFmpeg does not require drawtext."""
    from PIL import Image, ImageColor, ImageDraw, ImageFont

    text = str(logo.get("text") if logo.get("source") == "text" else logo.get("icon", "★"))
    font_size = max(6, min(160, int(logo.get("fontSize") or 10)))
    font_path = _resolve_font_name("NotoSans-Bold.ttf")
    font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    color = str(logo.get("color") or "#ffffff")
    try:
        fill = ImageColor.getrgb(color)
    except ValueError:
        fill = (255, 255, 255)
    probe = Image.new("RGBA", (1, 1))
    bounds = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font, stroke_width=0)
    padding = 4
    width = max(1, bounds[2] - bounds[0]) + padding * 2 + 2
    height = max(1, bounds[3] - bounds[1]) + padding * 2 + 2
    output = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(output)
    origin = (padding - bounds[0], padding - bounds[1])
    draw.text((origin[0] + 2, origin[1] + 2), text, font=font, fill=(0, 0, 0, 217))
    draw.text(origin, text, font=font, fill=(*fill, 255))
    target = work / "logo-generated.png"
    output.save(target)
    return target


def _motion_filter(
    mode: str, width: int, height: int, fps: int = 30,
    variant: int = 0, duration: float = 8,
) -> str:
    """Ken Burns motion with frame-evaluated expressions for smooth movement."""
    mode = str(mode or "off")
    if mode == "off":
        return ""
    size = f"{width}x{height}"
    if mode == "zoomIn":
        return (
            "scale=iw*4:ih*4,"
            "zoompan=z='min(1.06,1+on*0.00020)':"
            "x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
            f"d=1:s={size}:fps={fps}"
        )
    if mode == "zoomOut":
        return (
            "scale=iw*4:ih*4,"
            "zoompan=z='max(1,1.06-on*0.00020)':"
            "x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
            f"d=1:s={size}:fps={fps}"
        )
    axis = {
        "left": ("(iw-iw/zoom)*min(1,on/240)", "(ih-ih/zoom)/2"),
        "right": ("(iw-iw/zoom)*(1-min(1,on/240))", "(ih-ih/zoom)/2"),
        "up": ("(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-min(1,on/240))"),
        "down": ("(iw-iw/zoom)/2", "(ih-ih/zoom)*min(1,on/240)"),
    }.get(mode)
    if axis:
        return (
            "scale=iw*4:ih*4,"
            f"zoompan=z='min(1.06,1+on*0.00020)':x='{axis[0]}':y='{axis[1]}':"
            f"d=1:s={size}:fps={fps}"
        )
    # random: one continuous Ken Burns camera move per scene.
    frames = max(2, round(duration * fps) - 1)
    progress = f"min(1,on/{frames})"
    travel = f"(0.5-0.5*cos(PI*{progress}))"
    directions = (
        (f"(iw-iw/zoom)*{travel}", "(ih-ih/zoom)/2"),
        (f"(iw-iw/zoom)*(1-{travel})", "(ih-ih/zoom)/2"),
        ("(iw-iw/zoom)/2", f"(ih-ih/zoom)*{travel}"),
        ("(iw-iw/zoom)/2", f"(ih-ih/zoom)*(1-{travel})"),
        (f"(iw-iw/zoom)*{travel}", f"(ih-ih/zoom)*{travel}"),
        (f"(iw-iw/zoom)*(1-{travel})", f"(ih-ih/zoom)*{travel}"),
        (f"(iw-iw/zoom)*{travel}", f"(ih-ih/zoom)*(1-{travel})"),
        (f"(iw-iw/zoom)*(1-{travel})", f"(ih-ih/zoom)*(1-{travel})"),
    )
    x, y = directions[variant % len(directions)]
    zoom_curve = (
        f"1.12-0.12*{travel}"
        if variant % 2 == 0
        else f"1+0.09*{travel}"
    )
    return (
        "scale=iw*4:ih*4,"
        f"zoompan=z='{zoom_curve}':"
        f"x='{x}':y='{y}':"
        f"d=1:s={size}:fps={fps}"
    )


DRAWING_CACHE_ROOT = CACHE_ROOT / "drawing"


def _drawing_cache_key(source: Path, d_opts: dict) -> str:
    sig = _file_signature(str(source)) or {}
    payload = {"source": sig, "options": d_opts}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _drawing_video_sources(job_id: str, media: list[Path], durations: list[float], options: dict, work: Path) -> list[Path]:
    drawing = options.get("drawing") if isinstance(options.get("drawing"), dict) else {}
    if not drawing.get("enabled"):
        return media
    rendered = list(media)
    DRAWING_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    drawing_jobs: dict[str, tuple[int, Path, str]] = {}
    cached_count = 0
    for index, (source, duration) in enumerate(zip(media, durations), start=1):
        if is_video(source):
            continue
        d_opts = {
            "duration": max(2, min(60, duration)), "fps": int(options.get("fps", 30)),
            "resolution": drawing.get("resolution", "1080p"), "mode": drawing.get("mode", "hand"),
            "tool": drawing.get("tool", "pencil"), "detail": drawing.get("detail", 72),
            "thickness": drawing.get("thickness", 2), "strokeOrder": drawing.get("strokeOrder", "natural"),
        }
        ckey = _drawing_cache_key(source, d_opts)
        cached_video = DRAWING_CACHE_ROOT / f"{ckey}.mp4"
        target = work / f"drawing_{index:05d}.mp4"
        if cached_video.is_file() and cached_video.stat().st_size > 0:
            try:
                shutil.copy2(cached_video, target)
                rendered[index - 1] = target
                cached_count += 1
                continue
            except OSError:
                pass
        drawing_job = create_drawing_job(source.name, source, d_opts)
        drawing_jobs[drawing_job["id"]] = (index, source, ckey)
    if cached_count > 0:
        _log(job_id, f"Tái sử dụng {cached_count}/{len(durations)} ảnh vẽ tay từ cache")
    if not drawing_jobs:
        return rendered
    workers = start_drawing_batch(list(drawing_jobs))
    _log(job_id, f"Đang vẽ {len(drawing_jobs)} ảnh · tự động {workers} luồng, co/giãn theo CPU/RAM")
    pending = set(drawing_jobs)
    retry_counts: dict[str, int] = {}
    while pending:
        if (get_job(job_id) or {}).get("status") == "cancelled":
            for drawing_job_id in pending:
                cancel_drawing_job(drawing_job_id)
            return rendered
        for drawing_job_id in list(pending):
            state = get_drawing_job(drawing_job_id)
            if state and state.get("status") not in {"done", "error", "cancelled"}:
                continue
            index, source, ckey = drawing_jobs[drawing_job_id]
            if not state or state.get("status") != "done":
                error = (state or {}).get('error') or 'job bị hủy'
                # OpenCV/FFmpeg can transiently fail to open an MP4 writer when
                # several drawing workers start together on Windows. Retry only
                # this image once; do not discard successful sibling renders.
                retries = retry_counts.get(drawing_job_id, 0)
                if retries < 1:
                    _log(job_id, f"Retry vẽ ảnh {source.name} sau lỗi codec: {error}")
                    retry_counts[drawing_job_id] = retries + 1
                    if retry_drawing_job(drawing_job_id):
                        continue
                for remaining_id in pending - {drawing_job_id}:
                    cancel_drawing_job(remaining_id)
                raise RuntimeError(f"Không vẽ được ảnh {source.name}: {error}")
            target = work / f"drawing_{index:05d}.mp4"
            shutil.copy2(state["output"], target)
            cached_video = DRAWING_CACHE_ROOT / f"{ckey}.mp4"
            try:
                shutil.copy2(state["output"], cached_video)
            except OSError:
                pass
            rendered[index - 1] = target
            pending.remove(drawing_job_id)
            completed = len(drawing_jobs) - len(pending)
            _log(job_id, f"Đã vẽ ảnh {completed}/{len(drawing_jobs)}: {source.name}")
            _update(job_id, status="processing", progress=round(completed / len(drawing_jobs) * 30, 1))
        if pending:
            time.sleep(.25)
    return rendered


def run(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    cache_key = ""
    owns_cache_key = False
    try:
        cache_key = _render_cache_key(job)
        cached, owns_cache_key = _claim_render_cache(job_id, cache_key)
        if cached:
            output = Path(job["output"])
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, output)
            published = _publish_render(job_id, output, str(job.get("name") or ""))
            _update(job_id, status="done", progress=100, outputSize=output.stat().st_size)
            _log(job_id, f"Hoàn thành (từ cache): {output} ({output.stat().st_size / 1_048_576:.1f} MB)")
            return
        if not owns_cache_key:
            return
        _log(job_id, "Đang đọc timeline và kiểm tra media…")
        media = [Path(p) for p in job["images"]]
        timeline = str(job.get("timeline") or "")
        cues = parse_timing_times(Path(timeline)) if timeline else sequential_media_times(media)
        if timeline and len(media) < len(cues) and job["options"].get("allowMissingMedia"):
            _log(job_id, f"Thiếu {len(cues) - len(media)} media: bỏ qua các cảnh timeline không có file tương ứng")
        cues = select_cues_for_media(cues, len(media), bool(job["options"].get("allowMissingMedia")))
        durations = [
            max(0.04, (cues[i + 1][0] if i + 1 < len(cues) else end) - start)
            for i, (start, end) in enumerate(cues)
        ]
        opts = job["options"]
        work = Path(job["work"])
        work.mkdir(parents=True, exist_ok=True)
        media, durations = _prepare_media_inputs(
            job_id, media, durations, work, bool(opts.get("allowMissingMedia")),
        )
        speed = max(25, min(400, float(opts.get("speed", 100)))) / 100
        preview = max(0, min(120, float(opts.get("previewSeconds", 0))))
        media, durations = preview_media_window(media, durations, preview, speed)
        if preview:
            _log(job_id, f"Preview {preview:g}s: chỉ chuẩn bị {len(media)} media đầu tiên")
        dl = opts.get("delogo") if isinstance(opts.get("delogo"), dict) else {}
        repaired_stills = bool(dl.get('enabled')) and all(not is_video(p) for p in media)
        if repaired_stills:
            media = [_inpaint_still_logo(p, dl, work, i) for i, p in enumerate(media)]
            _log(job_id, "Logo: inpaint ảnh nguồn trước Drawing / Inpaint source images before Drawing")
        media = _drawing_video_sources(job_id, media, durations, opts, work)
        width, height = _output_resolution(opts, media[0])
        fps = max(1, min(60, int(opts.get("fps", 30))))
        crf = max(14, min(32, int(opts.get("crf", 20))))
        gpu_encoder = h264_hardware_encoder() if opts.get("encoder", "auto") != "cpu" else None
        use_gpu = gpu_encoder is not None
        _log(
            job_id,
            f"Đầu vào: {len(media)} media · {len(cues)} cảnh · {width}x{height} · {fps} FPS",
        )
        if not timeline:
            _log(job_id, "Không có timeline: ghép tuần tự, giữ thời lượng clip và 5 giây mỗi ảnh")
        _log(job_id, f"Encoder: {gpu_encoder or 'CPU libx264'} · quality {crf}")
        zoom_mode = str(opts.get("zoom", "off"))
        # ponytail: delogo — xóa watermark AI trước scale/zoom, tính trên frame gốc
        delogo_prefix = ""
        dl = opts.get("delogo") if isinstance(opts.get("delogo"), dict) else {}
        if dl.get("enabled") and not repaired_stills:
            src_w, src_h = image_resolution(media[0])
            dx = max(0, round(float(dl.get("x", 82)) / 100 * src_w))
            dy = max(0, round(float(dl.get("y", 94)) / 100 * src_h))
            dw = max(10, round(float(dl.get("w", 16)) / 100 * src_w))
            dh = max(10, round(float(dl.get("h", 4)) / 100 * src_h))
            delogo_prefix = f"delogo=x={dx}:y={dy}:w={dw}:h={dh},"
            _log(job_id, f"Delogo: {dw}×{dh} tại ({dx},{dy}) trên {src_w}×{src_h}")
            if all(not is_video(path) for path in media):
                # Still images were already reconstructed with inpaint; a
                # second delogo pass would blur the repaired pixels again.
                delogo_prefix = ""
        # ponytail: drawing videos và ảnh tĩnh khi zoom=off không cần encode segment trung gian,
        # áp dụng delogo trực tiếp trong final pass.
        all_raw_still = all(not is_video(p) for p in media)
        is_drawing = bool(opts.get("drawing", {}).get("enabled")) if isinstance(opts.get("drawing"), dict) else False
        need_segments = (
            zoom_mode != "off"
            or (not all_raw_still and not is_drawing)
        )
        sources = (
            _prepare_video_segments(
                job_id, media, durations, work, width, height, fps, crf, use_gpu, zoom_mode,
                delogo_prefix, gpu_encoder,
            )
            if need_segments else media
        )
        concat = work / "media.ffconcat"
        lines = ["ffconcat version 1.0"]
        for source, duration in zip(sources, durations):
            escaped = source.resolve().as_posix().replace("'", r"'\''")
            lines.append(f"file '{escaped}'")
            if sources is media and not is_video(source):
                lines.append(f"duration {duration:.3f}")
        if sources is media and not is_video(media[len(durations) - 1]):
            lines.append(f"file '{media[len(durations) - 1].resolve().as_posix()}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

        volume = max(0, min(300, float(opts.get("volume", 100)))) / 100
        subtitle_font = str(opts.get("subtitleFontFamily", "system"))
        subtitle_size = max(6, min(120, int(opts.get("subtitleSize", 8))))
        subtitle_margin = max(0, min(1000, int(opts.get("subtitleMargin", 34))))
        # Facebook's portrait chrome occupies more of the lower safe area.
        # Keep the rendered caption aligned with the Facebook live preview.
        if str(opts.get("targetPlatform", "")) == "facebook":
            subtitle_margin = min(1000, subtitle_margin + 16)
        subtitle_offset = max(-3600, min(3600, float(opts.get("subtitleOffset", 0))))
        _raw_bg = opts.get("subtitleBackground", "solid")
        # ponytail: backward compat — old payloads sent 0/1; new sends none/solid/box
        if _raw_bg in (0, "0", False):
            subtitle_background = "none"
        elif _raw_bg in (1, "1", True):
            subtitle_background = "solid"
        else:
            subtitle_background = str(opts.get("subtitleBackground", "solid"))
        subtitle_color = str(opts.get("subtitleColor", "#ffffff"))
        subtitle_bg_color = str(opts.get("subtitleBgColor", "#000000"))
        subtitle_opacity = max(0, min(100, int(opts.get("subtitleOpacity", 55))))
        zoom_filter = ""
        if str(opts.get("zoom", "off")) != "off":
            _log(job_id, f"Zoom: {opts.get('zoom')} · chuyển động nội suy theo thời gian")
        # ponytail: khi có segments (zoom on), delogo đã chạy trong segment → không cần lại
        seg_delogo = delogo_prefix if sources is media else ""
        base_vf = (
            f"{seg_delogo}"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}"
            + (f",{zoom_filter}" if zoom_filter else "")
        )
        cmd = [_ff_bin("ffmpeg"), "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
        next_input_idx = 1

        audio_index = None
        if job["audio"]:
            audio_index = next_input_idx
            next_input_idx += 1
            cmd += ["-i", job["audio"]]

        logo = opts.get("logo") if isinstance(opts.get("logo"), dict) else {}
        logo_asset = None
        if logo.get("enabled"):
            if logo.get("source") == "image" and job.get("watermark"):
                logo_asset = Path(job["watermark"])
            elif logo.get("source") in {"text", "icon"}:
                logo_asset = _render_logo_asset(logo, work)
        watermark_index = None
        if logo_asset is not None:
            watermark_index = next_input_idx
            next_input_idx += 1
            cmd += ["-loop", "1", "-i", str(logo_asset)]

        speed_filter = f"setpts=PTS/{speed:.6f}" if abs(speed - 1) > 0.001 else ""
        subtitle_filter = ""
        subtitle_overlay_index = None
        if job["srt"]:
            shifted_srt = shift_srt(
                Path(job["srt"]), work / "subtitles-prepared.srt", subtitle_offset,
            )
            if _has_ffmpeg_filter("subtitles"):
                subtitle_filter = _ffmpeg_subtitle(
                    shifted_srt, subtitle_font, subtitle_size, subtitle_margin, subtitle_background,
                    subtitle_color, subtitle_bg_color, subtitle_opacity,
                )
                _log(
                    job_id,
                    f"Phụ đề: cỡ {subtitle_size} · lề dưới {subtitle_margin} · "
                    f"lệch {subtitle_offset:g}s · nền {subtitle_background} (ASS filter)",
                )
            else:
                _log(
                    job_id,
                    f"Phụ đề: cỡ {subtitle_size} · lề dưới {subtitle_margin} · "
                    f"lệch {subtitle_offset:g}s · nền {subtitle_background} (PIL Overlay)",
                )
                subs_concat = _build_subtitle_overlay_concat(
                    shifted_srt, work, width, height, subtitle_font, subtitle_size, subtitle_margin,
                    subtitle_background, subtitle_color, subtitle_bg_color, subtitle_opacity,
                )
                subtitle_overlay_index = next_input_idx
                next_input_idx += 1
                cmd += ["-f", "concat", "-safe", "0", "-i", str(subs_concat)]

        # ── Unified filter_complex graph ──────────────────────────────────
        def _chain(*parts: str) -> str:
            tokens: list[str] = []
            for item in parts:
                if not item:
                    continue
                for token in item.split(","):
                    clean = token.strip()
                    if clean:
                        tokens.append(clean)
            return ",".join(tokens)

        graph_steps: list[str] = []
        current_v = "[0:v]"

        # 1. Base scale + delogo + speed
        base_chain = _chain(seg_delogo, f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", f"fps={fps}", zoom_filter, speed_filter)
        graph_steps.append(f"{current_v}{base_chain}[v_base]")
        current_v = "[v_base]"

        # 2. Watermark / Logo
        if watermark_index is not None:
            opacity = max(5, min(100, float(logo.get("opacity", 85)))) / 100
            scale = max(2, min(30, float(logo.get("size", 8)))) / 100
            moving_logo = logo.get("motion") == "random"
            visible = max(0.5, float(logo.get("visibleSec") or 4))
            hidden = max(0, float(logo.get("hiddenSec") or 2))
            cycle = visible + hidden
            x, y = _logo_position(
                logo.get("x", 88), logo.get("y", 88), moving_logo, cycle, logo.get("safeMargin", 4),
            )
            enable_parts = [f"lt(mod(t,{cycle:.3f}),{visible:.3f})"] if moving_logo else []
            if logo.get("scope") == "range":
                start = max(0, float(logo.get("start", 0)))
                end = max(start, float(logo.get("end", start)))
                enable_parts.append(f"between(t,{start:.3f},{end:.3f})")
            enable = f":enable='{'*'.join(enable_parts)}'" if enable_parts else ""
            logo_scale = (
                f"scale=-1:{max(12, round(height * scale))}"
                if logo.get("source") == "image" else ""
            )
            wm_chain = _chain(logo_scale, "format=rgba", f"colorchannelmixer=aa={opacity:.3f}")
            graph_steps.append(f"[{watermark_index}:v]{wm_chain}[wm]")
            graph_steps.append(
                f"{current_v}[wm]overlay=x='{x}':y='{y}':shortest=1{enable}[v_wm]"
            )
            current_v = "[v_wm]"

        # 3. Subtitle (ASS filter hoặc PIL Overlay)
        if subtitle_filter:
            sub_chain = _chain(subtitle_filter)
            graph_steps.append(f"{current_v}{sub_chain}[v_sub]")
            current_v = "[v_sub]"
        elif subtitle_overlay_index is not None:
            graph_steps.append(f"{current_v}[{subtitle_overlay_index}:v]overlay=format=auto:shortest=1[v_sub]")
            current_v = "[v_sub]"

        # 4. Final pixel format
        graph_steps.append(f"{current_v}format=yuv420p[vout]")

        cmd += ["-filter_complex", ";".join(graph_steps), "-map", "[vout]"]
        cmd += _encoder_args(use_gpu, crf)
        if audio_index is not None:
            cmd += ["-map", f"{audio_index}:a:0", "-af", f"volume={volume:.3f},atempo={speed:.6f}",
                    "-c:a", "aac", "-b:a", "192k", "-shortest"]
        if preview:
            cmd += ["-t", str(preview)]
        if opts.get("removeMetadata"):
            cmd += ["-map_metadata", "-1", "-metadata", "encoder="]
        cmd += ["-movflags", "+faststart", job["output"]]
        _update(job_id, status="processing", progress=1)
        _log(job_id, f"Bắt đầu render: {Path(job['output']).name}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0,
        )
        with _LOCK:
            _PROCS[job_id] = proc
        total = sum(durations) / speed
        assert proc.stderr
        stderr_tail: list[str] = []
        last_logged_percent = -10
        for line in proc.stderr:
            clean = line.strip()
            if clean:
                stderr_tail.append(clean)
                del stderr_tail[:-30]
            match = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
            if match:
                current = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
                base = 35 if sources is not media else 1
                progress = min(99, round(base + current / total * (99 - base), 1))
                _update(job_id, progress=progress)
                bucket = int(progress // 10) * 10
                if bucket > last_logged_percent:
                    last_logged_percent = bucket
                    _log(job_id, f"Đang render: {progress:.1f}% · {current:.1f}/{total:.1f}s")
        code = proc.wait()
        with _LOCK:
            _PROCS.pop(job_id, None)
        if get_job(job_id).get("status") == "cancelled":
            return
        if code or not Path(job["output"]).is_file():
            detail = "\n".join(stderr_tail[-12:])
            raise RuntimeError(f"FFmpeg kết thúc với mã {code}\n{detail}")
        path = Path(job["output"])
        try:
            _store_cached_render(cache_key, path)
        except OSError:
            # ponytail: cache là tối ưu tùy chọn; lỗi ghi cache không được làm hỏng video đã render.
            pass
        published = _publish_render(job_id, path, str(job.get("name") or ""))
        _update(job_id, status="done", progress=100, outputSize=path.stat().st_size)
        _log(job_id, f"Hoàn thành: {path} ({path.stat().st_size / 1_048_576:.1f} MB)")
    except Exception as exc:
        with _LOCK:
            _PROCS.pop(job_id, None)
        if (get_job(job_id) or {}).get("status") != "cancelled":
            _update(job_id, status="error", error=str(exc))
            _log(job_id, f"LỖI: {exc}")
    finally:
        if owns_cache_key and cache_key:
            _release_render_cache(cache_key)


def start(job_id: str) -> None:
    threading.Thread(target=run, args=(job_id,), name=f"srt-image-{job_id}", daemon=True).start()
