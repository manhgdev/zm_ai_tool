"""Project layout, meta.json, cache keys, fingerprints."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .config import DATA, PUBLIC_DATA

T = TypeVar("T")

_meta_locks: dict[str, threading.RLock] = {}
_meta_locks_guard = threading.Lock()


def normalize_project_tracks(meta: dict[str, Any]) -> dict[str, Any]:
    """Upgrade legacy `segments` in-memory without dropping existing projects.

    A cue remains the common timing anchor, while source and dub are explicit
    independently-renderable fields. Keeping legacy aliases lets old clients
    and export jobs continue to work during the migration.
    """
    for cue in meta.get("segments") or []:
        if not isinstance(cue, dict):
            continue
        cue.setdefault("sourceSubtitle", str(cue.get("source") or ""))
        cue.setdefault("dubSubtitle", str(cue.get("translation") or ""))
        cue.setdefault("source", str(cue.get("sourceSubtitle") or ""))
        cue.setdefault("translation", str(cue.get("dubSubtitle") or ""))
    settings = meta.setdefault("settings", {})
    if isinstance(settings, dict):
        settings.setdefault("sourceSubtitleVisible", False)
        settings.setdefault("dubSubtitleVisible", True)
        settings.setdefault("subtitleExportTrack", "dub")
        settings.setdefault("colorAdjust", {})
        settings.setdefault("lutAssetId", "")
        settings.setdefault("translationReviewMode", "manual")
        settings.setdefault("translationReviewTranslator", "ollama")
        settings.setdefault("blurBandMode", "auto")
        if settings.get("blurBandMode") is None:
            settings["blurBandMode"] = "auto"
    meta.setdefault("trackSchema", 2)
    return meta


def _meta_lock(project_id: str) -> threading.RLock:
    with _meta_locks_guard:
        lock = _meta_locks.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _meta_locks[project_id] = lock
        return lock

def project_dir(project_id: str) -> Path:
    p = PUBLIC_DATA / project_id
    legacy = DATA / project_id
    if not p.exists() and legacy.is_dir():
        old_root = str(legacy.resolve())
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(p))
        meta_path = p / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))

                def remap(value: Any) -> Any:
                    if isinstance(value, str) and value.startswith(old_root):
                        return str(p.resolve()) + value[len(old_root) :]
                    if isinstance(value, list):
                        return [remap(x) for x in value]
                    if isinstance(value, dict):
                        return {k: remap(v) for k, v in value.items()}
                    return value

                meta_path.write_text(
                    json.dumps(remap(meta), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except (OSError, json.JSONDecodeError):
                pass
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_layout(project_id: str) -> Path:
    """
    data/<id>/
      source.*  meta.json
      cache/audio.wav  cache/frames/  cache/asr.json
      tts/<hash>.wav
      out/burned.mp4  out/final.mp4
    """
    root = project_dir(project_id)
    (root / "cache").mkdir(exist_ok=True)
    (root / "tts").mkdir(exist_ok=True)
    (root / "out").mkdir(exist_ok=True)
    # migrate flat leftovers once
    moves = [
        (root / "audio.wav", root / "cache" / "audio_full.wav"),
        (root / "cache" / "audio.wav", root / "cache" / "audio_full.wav"),
        (root / "burned.mp4", root / "out" / "burned.mp4"),
        (root / "output.mp4", root / "out" / "final.mp4"),
    ]
    for src, dst in moves:
        if src.exists() and not dst.exists():
            src.replace(dst)
    for old_name, new_name in (("frames", "frames_full"),):
        frames_old = root / old_name
        frames_mid = root / "cache" / old_name
        frames_new = root / "cache" / new_name
        if frames_old.is_dir() and not frames_new.exists():
            frames_old.rename(frames_new)
        elif frames_mid.is_dir() and not frames_new.exists():
            frames_mid.rename(frames_new)
    return root


def cache_audio(project_id: str, tag: str = "full") -> Path:
    return ensure_layout(project_id) / "cache" / f"audio_{tag}.wav"


def cache_frames(project_id: str, tag: str = "full") -> Path:
    return ensure_layout(project_id) / "cache" / f"frames_{tag}"


def cache_asr_path(project_id: str, tag: str | None = None) -> Path:
    name = f"asr_{tag}.json" if tag else "asr.json"
    return ensure_layout(project_id) / "cache" / name


def out_burned(project_id: str) -> Path:
    return ensure_layout(project_id) / "out" / "burned.mp4"


def out_final(project_id: str) -> Path:
    return ensure_layout(project_id) / "out" / "final.mp4"


def preview_tag(preview_sec: int) -> str:
    return f"p{int(preview_sec)}" if preview_sec > 0 else "full"


def _speed_key_tag(speed: float) -> str:
    """s1 khi 1× (giữ cache cũ); s080/s115… khi ASR chạy trên file đã bake."""
    s = max(0.5, min(2.0, float(speed or 1.0)))
    return "s1" if abs(s - 1.0) < 0.001 else f"s{int(round(s * 100)):03d}"


def audio_cache_tag(preview_sec: int, match_duration: str, speed: float = 1.0) -> str:
    """Tag wav theo preview + tốc độ file thật sự được ASR (preferVideo bake trước)."""
    _ = match_duration
    return f"{preview_tag(preview_sec)}_{_speed_key_tag(speed)}"


def resolve_project_video(meta: dict[str, Any], project_id: str) -> Path:
    """Clip đang làm việc: khớp previewSec — full không trả clip preview_Ns ngắn."""
    source = Path(meta["videoPath"])
    preview_sec = max(0, int(meta.get("previewSec") or 0))
    work = str(meta.get("workVideo") or "")
    if work:
        wp = Path(work)
        if wp.is_file():
            name = wp.name.lower()
            is_preview_file = "preview_" in name
            if preview_sec <= 0:
                # Full: bỏ workVideo nếu là cắt preview ngắn
                if not is_preview_file:
                    return wp
            else:
                # Preview Ns: chỉ nhận file cùng Ns (preview_20… / bake cùng tag)
                from pipeline.core.media import preview_clip_matches

                if preview_clip_matches(name, preview_sec) or not is_preview_file:
                    return wp
    if preview_sec > 0:
        cached = ensure_layout(project_id) / "cache" / f"preview_{preview_sec}.mp4"
        if cached.is_file():
            return cached
    return source

def video_fingerprint(path: Path) -> str:
    """Full-file sha256 — head/tail 2MB collided when two clips share size + ends."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def find_project_by_fp(fp: str) -> str | None:
    for root in (PUBLIC_DATA, DATA):
        for p in root.iterdir():
            if not p.is_dir() or p.name.startswith("_") or p.name.startswith("."):
                continue
            meta_path = p / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("sourceFp") == fp and Path(meta.get("videoPath") or "").exists():
                if root == DATA:
                    project_dir(p.name)
                return p.name
    return None


def inherit_voice(seg_voice: str | None, default: str) -> str:
    v = (seg_voice or "").strip()
    if not v or v == "system":
        return default or "system"
    return v


def asr_cache_key(settings: dict[str, Any], source_fp: str, speed: float = 1.0) -> str:
    engine = settings.get("engine", "paddleocr")
    src = settings.get("sourceLang", "auto")
    prev = int(settings.get("previewSec") or 0)
    # o20: quét cả nhãn ngang ở 10–22% phía trên khung.
    # a17: preserve spaces when rebuilding Whisper word groups.
    ver = "o20" if engine in ("paddleocr", "screen") else "a17"
    # speed = tốc độ file thật sự ASR (chỉ khác 1× sau khi người dùng bake).
    subtitle = str(settings.get("subtitleSource") or "") if engine == "subtitle" else ""
    return f"{engine}|{src}|{subtitle}|{source_fp}|p{prev}|{ver}|{_speed_key_tag(speed)}"


def trans_cache_key(settings: dict[str, Any]) -> str:
    # g5: Ollama mode/model/tier là một phần nội dung đầu ra, không dùng lẫn cache.
    eng = str(settings.get("translator") or "google")
    ollama = ""
    if eng == "ollama":
        ollama = "|".join(
            (
                str(settings.get("ollamaMode") or "cloud"),
                str(settings.get("ollamaModel") or "minimax-m3:cloud"),
                str(settings.get("ollamaLocalTier") or "balanced"),
            )
        )
    return f"{eng}|{settings.get('targetLang', 'vi')}|{ollama}|g5"


def _read_meta_file(path: Path) -> dict[str, Any]:
    # ponytail: subprocess có thể đang atomic-replace meta.json → retry ngắn nếu lỗi
    for attempt in range(3):
        raw = path.read_text(encoding="utf-8")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            if attempt < 2:
                time.sleep(0.025)
                continue
            # last attempt: lấy JSON object đầu tiên nếu file bị truncate
            try:
                obj, _end = json.JSONDecoder().raw_decode(raw.lstrip())
            except json.JSONDecodeError:
                raise
        if not isinstance(obj, dict):
            raise json.JSONDecodeError("meta root must be object", raw, 0)
        return obj
    raise json.JSONDecodeError("meta read failed after retries", "", 0)


def _write_meta_file(path: Path, meta: dict[str, Any]) -> None:
    payload = json.dumps(meta, ensure_ascii=False, indent=2)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt >= 9:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_meta(project_id: str) -> dict[str, Any]:
    path = project_dir(project_id) / "meta.json"
    with _meta_lock(project_id):
        if not path.exists():
            return {}
        try:
            return normalize_project_tracks(_read_meta_file(path))
        except json.JSONDecodeError:
            # recovery: ghi lại bản sạch dưới cùng lock
            try:
                obj, _end = json.JSONDecoder().raw_decode(path.read_text(encoding="utf-8").lstrip())
            except json.JSONDecodeError:
                raise
            if isinstance(obj, dict):
                _write_meta_file(path, obj)
                return normalize_project_tracks(obj)
            raise


def save_meta(project_id: str, meta: dict[str, Any]) -> None:
    path = project_dir(project_id) / "meta.json"
    with _meta_lock(project_id):
        _write_meta_file(path, normalize_project_tracks(meta))


def apply_meta_patch(
    project_id: str,
    patch: dict[str, Any],
    *,
    remove: tuple[str, ...] = (),
) -> None:
    """Ghi ĐÚNG các key job này thay đổi lên bản meta mới nhất trên đĩa.

    Job dài (bake tốc độ, compound, dịch lại) load meta lúc bắt đầu rồi chạy
    ffmpeg vài phút; save cả snapshot cũ sẽ nuốt mất mọi thay đổi lưu trong
    lúc đó (PUT segments/overlays, status). Patch theo key thì không.
    """

    def fn(meta: dict[str, Any]) -> None:
        meta.update(patch)
        for key in remove:
            meta.pop(key, None)

    mutate_meta(project_id, fn)


def mutate_meta(project_id: str, fn: Callable[[dict[str, Any]], T]) -> T:
    """Read-modify-write atomic — tránh race khi nhiều PUT segment."""
    path = project_dir(project_id) / "meta.json"
    with _meta_lock(project_id):
        meta: dict[str, Any] = normalize_project_tracks(_read_meta_file(path)) if path.exists() else {}
        out = fn(meta)
        _write_meta_file(path, meta)
        return out


def _sanitize_status_text(raw: Any, *, limit: int = 280) -> str:
    """Không lưu argv ffmpeg / filter_complex dài vào meta (phình popup UI)."""
    if raw is None:
        return ""
    t = str(raw).strip()
    if not t:
        return ""
    low = t.lower()
    if "winerror 206" in low or "filename or extension is too long" in low:
        return "PATH Windows quá dài (WinError 206). App đã loại đường dẫn trùng; không cần cài lại gói AI."
    if (
        "Command '[" in t
        or 'Command "[' in t
        or "-filter_complex" in t
        or "between(t" in t
    ):
        import re

        m = re.search(r"exit status (-?\d+)", t, re.I) or re.search(r"exit (\d+)", t, re.I)
        code = m.group(1) if m else "?"
        if "ffmpeg" in low:
            return f"ffmpeg thất bại (exit {code}). Xem log backend."
        return f"Lệnh thất bại (exit {code})."
    return t if len(t) <= limit else t[: limit - 1] + "…"


def set_status(project_id: str, **kwargs: Any) -> None:
    def apply(meta: dict[str, Any]) -> None:
        status = meta.get("status") or {
            "step": "video",
            "progress": 0,
            "message": "",
            "running": False,
        }
        clean = dict(kwargs)
        if "message" in clean and clean["message"] is not None:
            clean["message"] = _sanitize_status_text(clean["message"])
        if "error" in clean and clean["error"] is not None:
            clean["error"] = _sanitize_status_text(clean["error"])
        status.update(clean)
        if "error" in kwargs and kwargs["error"] is None:
            status.pop("error", None)
        # Sửa lỗi cũ đã lưu trong meta (popup vẫn dài sau F5)
        if status.get("error"):
            status["error"] = _sanitize_status_text(status["error"])
        if status.get("message"):
            status["message"] = _sanitize_status_text(status["message"])
        meta["status"] = status

    mutate_meta(project_id, apply)


def append_job_event(project_id: str, event_type: str, payload: dict[str, Any]) -> int:
    """Persist a compact, monotonic job event for polling/SSE clients.

    Keeping the last 500 events makes reconnect idempotent without needing an
    external broker; job outputs are still the authoritative project metadata.
    """
    result: dict[str, int] = {}

    def apply(meta: dict[str, Any]) -> None:
        events = [item for item in meta.get("jobEvents") or [] if isinstance(item, dict)]
        next_id = int(events[-1].get("id") or 0) + 1 if events else 1
        events.append({"id": next_id, "type": event_type, "payload": payload, "at": time.time()})
        meta["jobEvents"] = events[-500:]
        result["id"] = next_id

    mutate_meta(project_id, apply)
    return result["id"]


def _rm_path(path: Path, errors: list[str], label: str) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        elif path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
    except OSError as e:
        errors.append(f"{label}: {e}")


def _clear_dir_contents(path: Path, errors: list[str], label: str) -> None:
    if not path.is_dir():
        return
    for child in list(path.iterdir()):
        _rm_path(child, errors, f"{label}/{child.name}")


# Mục xóa cache (user chọn checkbox). Video nguồn không bao giờ xóa.
CACHE_CLEAR_KEYS = (
    "ocr",
    "whisper",
    "subtitle",
    "translation",
    "audio",
    "tts",
    "preview",
    "render",
    "temp",
    "backend",
    "frontend",
    "jobs",
    "covers",  # bbox / captionLayout / vùng che
)


def _norm_clear_parts(parts: list[str] | None) -> set[str]:
    if not parts:
        return set(CACHE_CLEAR_KEYS)
    out = {str(p).strip().lower() for p in parts if str(p).strip()}
    return out & set(CACHE_CLEAR_KEYS) if out else set(CACHE_CLEAR_KEYS)


def _clear_cache_dir_matches(
    cache_dir: Path,
    pred,
    errors: list[str],
    deleted: list[str],
    label: str,
) -> None:
    if not cache_dir.is_dir():
        return
    for child in list(cache_dir.iterdir()):
        try:
            if pred(child):
                _rm_path(child, errors, f"{label}/{child.name}")
                deleted.append(f"{label}/{child.name}")
        except OSError as e:
            errors.append(f"{label}/{child.name}: {e}")


def clear_project_cache(
    project_id: str,
    parts: list[str] | None = None,
) -> dict[str, Any]:
    """Xóa cache project — chỉ khi user bấm «Xóa cache». Giữ source video + settings."""
    from pipeline.core.jobs import clear_job, request_cancel

    want = _norm_clear_parts(parts)
    all_on = want == set(CACHE_CLEAR_KEYS)
    errors: list[str] = []
    deleted: list[str] = []
    root = project_dir(project_id)
    if not root.is_dir():
        return {
            "ok": False,
            "deleted": [],
            "errors": ["project not found"],
            "partial": True,
            "parts": sorted(want),
        }

    if "jobs" in want or all_on:
        try:
            request_cancel(project_id)
            deleted.append("jobs/cancel")
        except Exception as e:
            errors.append(f"cancel: {e}")
        try:
            clear_job(project_id)
            deleted.append("jobs")
        except Exception as e:
            errors.append(f"clear_job: {e}")

    meta = load_meta(project_id) or {}
    video_path = str(meta.get("videoPath") or "")
    source_name = Path(video_path).name if video_path else ""
    cache_dir = root / "cache"

    if "tts" in want:
        tts = root / "tts"
        if tts.exists():
            _clear_dir_contents(tts, errors, "tts")
            deleted.append("tts")
            tts.mkdir(exist_ok=True)

    if "render" in want:
        out = root / "out"
        if out.exists():
            _clear_dir_contents(out, errors, "out")
            deleted.append("out")
            out.mkdir(exist_ok=True)
        for exp_root in (PUBLIC_DATA / "exports", DATA / "exports"):
            try:
                if not exp_root.is_dir():
                    continue
                easy = exp_root / f"{project_id}.mp4"
                if easy.is_file():
                    _rm_path(easy, errors, f"exports/{easy.name}")
                    deleted.append(f"exports/{easy.name}")
            except OSError as e:
                errors.append(f"exports: {e}")

    if cache_dir.is_dir():
        if "ocr" in want:
            _clear_cache_dir_matches(
                cache_dir,
                lambda p: p.is_dir()
                and (
                    "frame" in p.name.lower()
                    or "ocr" in p.name.lower()
                )
                or (
                    p.is_file()
                    and (
                        "ocr" in p.name.lower()
                        or p.name.lower().startswith("boxes")
                    )
                ),
                errors,
                deleted,
                "cache",
            )
        elif "covers" in want:
            # Bbox/cover là dữ liệu dẫn xuất từ OCR. Nếu chỉ bỏ bbox trong
            # meta nhưng giữ các file này, lần mở/rerun sau có thể nạp lại
            # đúng vùng cũ, khiến preview trông như "bbox/blur bị lặp".
            _clear_cache_dir_matches(
                cache_dir,
                lambda p: p.is_file()
                and any(token in p.name.lower() for token in ("bbox", "boxes", "cover", "layout")),
                errors,
                deleted,
                "cache",
            )
        if "whisper" in want or "subtitle" in want:
            _clear_cache_dir_matches(
                cache_dir,
                lambda p: p.is_file()
                and (
                    p.name.lower().startswith("asr")
                    or p.suffix.lower() == ".json"
                    and "asr" in p.name.lower()
                ),
                errors,
                deleted,
                "cache",
            )
        if "audio" in want:
            _clear_cache_dir_matches(
                cache_dir,
                lambda p: p.is_file()
                and p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".flac")
                or (p.is_dir() and "audio" in p.name.lower()),
                errors,
                deleted,
                "cache",
            )
        if "preview" in want:
            _clear_cache_dir_matches(
                cache_dir,
                lambda p: p.is_file()
                and (
                    "preview" in p.name.lower()
                    or p.suffix.lower() in (".mp4", ".webm", ".mkv")
                ),
                errors,
                deleted,
                "cache",
            )
        if "backend" in want or all_on:
            # phần còn lại trong cache/
            for child in list(cache_dir.iterdir()):
                _rm_path(child, errors, f"cache/{child.name}")
                deleted.append(f"cache/{child.name}")
            cache_dir.mkdir(exist_ok=True)

    if "temp" in want or all_on:
        keep_names = {"meta.json", "cache", "tts", "out"}
        if source_name:
            keep_names.add(source_name)
        for child in list(root.iterdir()):
            name = child.name
            if name in keep_names:
                continue
            if name.startswith("source.") or name == "source":
                continue
            if name.startswith("meta.json"):
                continue
            _rm_path(child, errors, name)
            deleted.append(name)

    scrub_segments = (
        all_on
        or "subtitle" in want
        or "translation" in want
        or "covers" in want
        or "whisper" in want
        or "ocr" in want
    )
    scrub_trans_cache = all_on or "translation" in want or "subtitle" in want
    scrub_tts_meta = "tts" in want or all_on
    scrub_covers_only = "covers" in want and not (
        all_on or "subtitle" in want or "translation" in want or "whisper" in want
    )

    def scrub(m: dict[str, Any]) -> None:
        if video_path and Path(video_path).is_file():
            m["videoPath"] = video_path

        if "backend" in want or all_on:
            m["cache"] = {}
        elif "whisper" in want or "ocr" in want or "subtitle" in want:
            c = dict(m.get("cache") or {})
            if "whisper" in want or "subtitle" in want:
                c.pop("asrKey", None)
            if "translation" in want or "subtitle" in want:
                c.pop("transKey", None)
            m["cache"] = c

        if scrub_trans_cache:
            m.pop("translationCaches", None)

        if scrub_covers_only:
            segs = m.get("segments") or []
            for s in segs:
                if not isinstance(s, dict):
                    continue
                s.pop("bbox", None)
                s.pop("bboxInherited", None)
                s.pop("bboxDetected", None)
                s.pop("captionLayout", None)
            m["segments"] = segs
            m.pop("timelineBaseline", None)
            m.pop("logoDetection", None)
        elif scrub_segments:
            m["segments"] = []
            m.pop("timelineBaseline", None)
            if all_on or "ocr" in want or "covers" in want:
                m.pop("logoDetection", None)

        if "covers" in want or "ocr" in want or all_on:
            # Đây là cache OCR đã chốt cho auto blur, không phải vùng blur
            # thủ công do người dùng kéo. Giữ manual region theo cam kết
            # "settings dự án không bị xóa", nhưng buộc auto OCR tạo lại
            # từ dữ liệu mới sau khi người dùng chạy nhận dạng.
            settings = dict(m.get("settings") or {})
            settings.pop("blurBandAutoRegion", None)
            settings.pop("blurBandAutoRegionVersion", None)
            m["settings"] = settings

        if scrub_tts_meta:
            segs = m.get("segments") or []
            for s in segs:
                if not isinstance(s, dict):
                    continue
                s.pop("audioFile", None)
                s.pop("audioUrl", None)
                s.pop("audioDuration", None)
                s.pop("videoSpeed", None)
            if segs:
                m["segments"] = segs

        if "preview" in want or all_on:
            m.pop("workVideo", None)
            m.pop("workDuration", None)
            m.pop("bakedSpeed", None)
            m.pop("bakedPreferVideo", None)
            m.pop("userBake", None)

        m.pop("forceTts", None)
        m.pop("runCache", None)
        m.pop("checkpoint", None)

        if "render" in want or all_on:
            for k in ("outputRel", "outputPath", "exportPath"):
                m.pop(k, None)

        msg = (
            "Đã xóa toàn bộ cache — video nguồn giữ nguyên"
            if all_on
            else f"Đã xóa: {', '.join(sorted(want))}"
        )
        m["status"] = {
            "step": "video" if scrub_segments and not scrub_covers_only else (m.get("status") or {}).get("step") or "video",
            "progress": 100,
            "message": msg,
            "running": False,
        }

    try:
        mutate_meta(project_id, scrub)
        deleted.append("meta")
    except Exception as e:
        errors.append(f"meta: {e}")

    return {
        "ok": len(errors) == 0,
        "partial": len(errors) > 0,
        "deleted": deleted,
        "errors": errors,
        "parts": sorted(want),
        "clearedSegments": scrub_segments and not scrub_covers_only,
        "clearedCovers": "covers" in want or scrub_segments,
        "clearedTts": scrub_tts_meta,
        "clearedFrontend": "frontend" in want or all_on,
        "message": (
            ("Đã xóa toàn bộ cache." if all_on else f"Đã xóa {len(want)} mục.")
            if not errors
            else "Một số cache chưa được xóa."
        ),
    }
