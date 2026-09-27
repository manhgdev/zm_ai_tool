"""In-memory download jobs + yt-dlp worker."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pipeline.core.config import DATA, PUBLIC_DATA, SERVER_ROOT
from pipeline.core.output_paths import downloads_folder
from pipeline.core.jobs import kill_process_tree
from pipeline.core.executables import ytdlp_command

_DEFAULT_DOWNLOAD_ROOT = downloads_folder("download-video")
_PREF_PATH = DATA / "download_root.json"
_JOBS_CACHE = DATA / "download_jobs.json"
_root_lock = threading.Lock()
DOWNLOAD_ROOT: Path = _DEFAULT_DOWNLOAD_ROOT


def _default_root() -> Path:
    return _DEFAULT_DOWNLOAD_ROOT.resolve()


def _load_pref_path() -> Path | None:
    try:
        if not _PREF_PATH.is_file():
            return None
        data = json.loads(_PREF_PATH.read_text(encoding="utf-8"))
        raw = str(data.get("path") or "").strip()
        if not raw:
            return None
        selected = Path(raw).expanduser()
        # Migrate the former built-in default; retain genuinely custom paths.
        if selected == Path.home() / "Downloads" / "download-video":
            return None
        return selected
    except Exception:
        return None


def _save_pref_path(path: Path) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    _PREF_PATH.write_text(
        json.dumps({"path": str(path.resolve())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_download_root() -> Path:
    with _root_lock:
        return DOWNLOAD_ROOT


def ensure_download_dirs() -> Path:
    """Tạo thư mục lưu download khi app start / trước job (idempotent)."""
    global DOWNLOAD_ROOT
    with _root_lock:
        pref = _load_pref_path()
        root = (pref if pref is not None else _DEFAULT_DOWNLOAD_ROOT).expanduser()
        try:
            root = root.resolve()
        except OSError:
            root = _default_root()
        root.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_ROOT = root
        return DOWNLOAD_ROOT


def set_download_root(path: str) -> dict[str, str]:
    """Đổi thư mục lưu — tạo nếu chưa có, ghi preference."""
    global DOWNLOAD_ROOT
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("Đường dẫn trống")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = _default_root() / p
    # chặn path quá kỳ quặc
    if len(str(p)) > 480:
        raise ValueError("Đường dẫn quá dài")
    try:
        p.mkdir(parents=True, exist_ok=True)
        resolved = p.resolve()
        if not resolved.is_dir():
            raise ValueError("Không phải thư mục")
        # thử ghi
        probe = resolved / ".vc_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        raise ValueError(f"Không tạo/ghi được thư mục: {e}") from e
    _save_pref_path(resolved)
    with _root_lock:
        DOWNLOAD_ROOT = resolved
    return download_root_info()


def reset_download_root() -> dict[str, str]:
    global DOWNLOAD_ROOT
    try:
        if _PREF_PATH.is_file():
            _PREF_PATH.unlink()
    except OSError:
        pass
    root = _default_root()
    root.mkdir(parents=True, exist_ok=True)
    with _root_lock:
        DOWNLOAD_ROOT = root
    return download_root_info()


def download_root_info() -> dict[str, str]:
    root = ensure_download_dirs()
    default = _default_root()
    is_default = root.resolve() == default
    try:
        rel = str(root.relative_to(SERVER_ROOT)).replace("\\", "/")
    except ValueError:
        rel = str(root)
    return {
        "path": str(root.resolve()),
        "display": str(root.resolve()),
        "relative": rel,
        "defaultPath": str(default),
        "isDefault": is_default,
    }


def reveal_download_root() -> dict[str, Any]:
    root = ensure_download_dirs()
    import platform

    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", str(root)])
        elif system == "Darwin":
            subprocess.Popen(["open", str(root)])
        else:
            subprocess.Popen(["xdg-open", str(root)])
    except OSError as e:
        raise RuntimeError(str(e)) from e
    return {"ok": True, "path": str(root.resolve())}


# tạo ngay khi load module (dev import) + startup app gọi lại
ensure_download_dirs()

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_PROCS: dict[str, subprocess.Popen] = {}
_persist_timer: threading.Timer | None = None

_QUALITIES = frozenset({"best", "2160", "1440", "1080", "720", "480", "audio"})
_FORMATS = frozenset({"mp4", "mkv", "webm", "mp3"})

_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_to_disk(j: dict[str, Any]) -> dict[str, Any]:
    """Serialize job (bỏ process); giữ path để F5 / restart còn mở file."""
    st = j.get("status")
    # job đang chạy khi process chết → coi như error (không resume yt-dlp)
    if st in ("queued", "running"):
        st = "error"
        msg = "Gián đoạn (server reload) — chạy lại nếu cần"
        prog = 0
    else:
        msg = j.get("message")
        prog = j.get("progress", 0)
    return {
        "id": j["id"],
        "url": j["url"],
        "title": j.get("title"),
        "quality": j.get("quality", "best"),
        "format": j.get("format", "mp4"),
        "status": st,
        "progress": prog,
        "message": msg,
        "outputPath": j.get("outputPath"),
        "fileName": j.get("fileName"),
        "_absPath": j.get("_absPath"),
        "_dir": j.get("_dir"),
        "_opts": j.get("_opts") or {},
        "log": list(j.get("log") or [])[-40:],
        "createdAt": j.get("createdAt") or _now(),
    }


def _persist_jobs_now() -> None:
    with _LOCK:
        items = [_job_to_disk(j) for j in _JOBS.values()]
    items.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    items = items[:100]
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = _JOBS_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jobs": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_JOBS_CACHE)
    except OSError:
        pass


def _schedule_persist() -> None:
    global _persist_timer
    with _LOCK:
        if _persist_timer is not None:
            try:
                _persist_timer.cancel()
            except Exception:
                pass

        def _fire():
            global _persist_timer
            _persist_jobs_now()
            with _LOCK:
                _persist_timer = None

        _persist_timer = threading.Timer(0.4, _fire)
        _persist_timer.daemon = True
        _persist_timer.start()


def _load_jobs_from_disk() -> None:
    if not _JOBS_CACHE.is_file():
        return
    try:
        data = json.loads(_JOBS_CACHE.read_text(encoding="utf-8"))
        raw = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return
    except Exception:
        return
    loaded: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        jid = str(item["id"])
        st = item.get("status") or "error"
        if st in ("queued", "running"):
            st = "error"
            item = {
                **item,
                "status": "error",
                "progress": 0,
                "message": item.get("message") or "Gián đoạn (server reload)",
            }
        # file mất → đánh error
        abs_p = item.get("_absPath")
        if st == "done" and abs_p and not Path(str(abs_p)).is_file():
            st = "error"
            item = {
                **item,
                "status": "error",
                "progress": 0,
                "message": "File đã bị xóa",
                "outputPath": None,
                "fileName": None,
                "_absPath": None,
            }
        loaded[jid] = {
            "id": jid,
            "url": str(item.get("url") or ""),
            "title": item.get("title"),
            "quality": item.get("quality") or "best",
            "format": item.get("format") or "mp4",
            "status": st,
            "progress": int(item.get("progress") or 0),
            "message": item.get("message"),
            "outputPath": item.get("outputPath"),
            "fileName": item.get("fileName"),
            "_absPath": item.get("_absPath"),
            "_dir": item.get("_dir"),
            "_opts": item.get("_opts") or {},
            "log": list(item.get("log") or [])[-40:],
            "createdAt": item.get("createdAt") or _now(),
        }
    with _LOCK:
        _JOBS.clear()
        _JOBS.update(loaded)


_load_jobs_from_disk()


def _ytdlp_bin() -> list[str]:
    return ytdlp_command() or ["yt-dlp"]


def _platform_subtitle_selection(bin_: str | list[str], url: str) -> tuple[list[str], bool]:
    """Pick one vi/en caption from metadata; manual captions win over automatic ones."""
    try:
        proc = subprocess.run(
            [*([bin_] if isinstance(bin_, str) else bin_), "--no-playlist", "--ignore-config", "--skip-download", "--dump-single-json", url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode:
            return [], False
        data = json.loads(proc.stdout)
        manual = data.get("subtitles") if isinstance(data, dict) else None
        automatic = data.get("automatic_captions") if isinstance(data, dict) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], False
    for available, is_automatic in ((manual, False), (automatic, True)):
        if not isinstance(available, dict):
            continue
        for wanted in ("vi", "en"):
            exact = next((lang for lang in available if lang.lower() == wanted), None)
            variant = next((lang for lang in available if lang.lower().split("-", 1)[0] == wanted), None)
            if exact or variant:
                return [exact or variant], is_automatic
    return [], False


def _srt_timestamp(ms: int) -> str:
    ms = max(0, int(ms))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def _json3_to_srt(source: Path) -> Path | None:
    """Write one non-overlapping SRT from a single provider JSON3 subtitle track."""
    try:
        events = json.loads(source.read_text(encoding="utf-8")).get("events", [])
    except (OSError, ValueError, AttributeError):
        return None
    cues: list[tuple[int, int, str]] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        text = "".join(str(part.get("utf8") or "") for part in event.get("segs", []) if isinstance(part, dict))
        # Auto-caption sound labels (e.g. [âm nhạc], [music]) are not dialogue.
        text = re.sub(r"\[[^\]\n]{1,80}\]", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        if text:
            start = int(event.get("tStartMs") or 0)
            cues.append((start, start + max(1, int(event.get("dDurationMs") or 0)), text))
    if not cues:
        return None
    lines: list[str] = []
    for number, (start, end, text) in enumerate(cues, 1):
        if number < len(cues):
            end = min(end, cues[number][0])
        lines.extend((str(number), f"{_srt_timestamp(start)} --> {_srt_timestamp(max(start + 1, end))}", text, ""))
    target = source.with_suffix(".srt")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _format_for(
    quality: str,
    *,
    merge_av: bool = True,
    prefer_free: bool = False,
    optimize_size: bool = False,
) -> str:
    """Build yt-dlp -f string from UI checkboxes."""
    if quality == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"

    try:
        cap = int(quality) if quality != "best" else 0
    except ValueError:
        cap = 0

    # Tối ưu dung lượng: lấy bản nhỏ hơn trong khoảng cap (hoặc best→720)
    if optimize_size and cap <= 0:
        cap = 720
    if optimize_size and cap > 0:
        # ưu tiên height gần cap nhưng bitrate thấp / progressive
        long = int(round(cap * 16 / 9))
        if merge_av:
            return (
                f"bv*[height<={cap}][width<={long}]+ba[ext=m4a]/"
                f"bv*[height<={cap}]+ba/"
                f"b[height<={cap}]/worst[height<={cap}]/b"
            )
        return (
            f"b[height<={cap}][ext=mp4]/"
            f"b[height<={cap}]/"
            f"worst[height<={cap}]/b"
        )

    free_v = "bv*[vcodec^=vp9]/bv*[vcodec^=av01]/bv*" if prefer_free else "bv*"
    free_b = "b[vcodec^=vp9]/b[vcodec^=av01]/b" if prefer_free else "b"

    if cap <= 0:
        if merge_av:
            return f"{free_v}+ba/{free_b}/b"
        # progressive 1 file — không tách stream
        return f"{free_b}[ext=mp4]/{free_b}/bv*+ba/b"

    long = int(round(cap * 16 / 9))
    if merge_av:
        return (
            f"{free_v}[width<={cap}][height<={long}]+ba[ext=m4a]/"
            f"{free_v}[height<={cap}][width<={long}]+ba/"
            f"{free_v}[height<={cap}]+ba/"
            f"{free_b}[height<={cap}]/b"
        )
    # không ghép: ưu tiên 1 file progressive
    return (
        f"{free_b}[height<={cap}][ext=mp4]/"
        f"{free_b}[width<={cap}][ext=mp4]/"
        f"{free_b}[height<={cap}]/"
        f"bv*[height<={cap}]+ba/b"
    )


def _host_folder(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        host = host.removeprefix("www.")
        host = re.sub(r"[^\w.\-]+", "_", host) or "other"
        return host[:64]
    except Exception:
        return "other"


def _public_job(j: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": j["id"],
        "url": j["url"],
        "title": j.get("title"),
        "quality": j["quality"],
        "format": j.get("format", "mp4"),
        "status": j["status"],
        "progress": j.get("progress", 0),
        "message": j.get("message"),
        "outputPath": j.get("outputPath"),
        "createdAt": j["createdAt"],
        "log": list(j.get("log") or [])[-40:],
    }
    if j.get("status") == "done" and j.get("fileName"):
        out["downloadUrl"] = f"/api/download/jobs/{j['id']}/file"
    return out


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        items = sorted(_JOBS.values(), key=lambda x: x["createdAt"], reverse=True)
        return [_public_job(j) for j in items[:100]]


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        return _public_job(j) if j else None


def get_job_file(job_id: str) -> Path | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j or j.get("status") != "done":
            return None
        p = j.get("_absPath")
        if not p:
            return None
        path = Path(p)
        return path if path.is_file() else None


def reveal_job_file(job_id: str) -> dict[str, Any]:
    """Mở file / hiện trong Explorer (máy chạy backend)."""
    import platform

    path = get_job_file(job_id)
    if not path:
        raise FileNotFoundError("Chưa có file")
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as e:
        raise RuntimeError(str(e)) from e
    return {"ok": True, "path": str(path.resolve())}


def cancel_job(job_id: str) -> bool:
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return False
        if j["status"] in ("done", "error"):
            return True
        j["status"] = "error"
        j["message"] = "Đã hủy"
        j["progress"] = 0
        _append_log_unlocked(j, "Đã hủy bởi người dùng")
        proc = _PROCS.pop(job_id, None)
    if proc and proc.poll() is None:
        kill_process_tree(proc)
    _schedule_persist()
    _publish("download.job.updated", job_id, get_job(job_id))
    return True


def clear_done_jobs() -> int:
    """Làm sạch mọi job done/error — list + file trên disk."""
    with _LOCK:
        ids = [k for k, j in _JOBS.items() if j.get("status") in ("done", "error")]
    n = 0
    for k in ids:
        if delete_job(k, delete_files=True):
            n += 1
    _schedule_persist()
    return n


def clear_job_logs() -> int:
    """Clear diagnostics while retaining download jobs and their output files."""
    with _LOCK:
        for job in _JOBS.values():
            job["log"] = []
        count = len(_JOBS)
    _schedule_persist()
    return count


def delete_job(job_id: str, *, delete_files: bool = True) -> bool:
    """Xóa hẳn 1 job: khỏi list, kill process, xóa thư mục file (+ parent host rỗng)."""
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return False
        proc = _PROCS.pop(job_id, None)
        job_dir = j.get("_dir")
        abs_file = j.get("_absPath")
        del _JOBS[job_id]
    if proc is not None and proc.poll() is None:
        try:
            kill_process_tree(proc)
            proc.wait(timeout=5)
        except Exception:
            kill_process_tree(proc)
    if delete_files:
        if abs_file:
            try:
                fp = Path(abs_file)
                if fp.is_file():
                    fp.unlink(missing_ok=True)
            except OSError:
                pass
        if job_dir:
            try:
                p = Path(job_dir)
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                parent = p.parent
                root = get_download_root()
                if parent.resolve() != root.resolve() and parent.is_dir():
                    if not any(parent.iterdir()):
                        parent.rmdir()
            except OSError:
                pass
    _schedule_persist()
    return True


def _append_log_unlocked(j: dict[str, Any], line: str) -> None:
    log = j.setdefault("log", [])
    log.append(line[:500])
    if len(log) > 80:
        del log[:-80]


def _append_log(job_id: str, line: str) -> None:
    with _LOCK:
        j = _JOBS.get(job_id)
        if j:
            _append_log_unlocked(j, line)


def _normalize_urls(url: str | None = None, urls: list[str] | None = None) -> list[str]:
    raw: list[str] = []
    if urls:
        for u in urls:
            raw.extend(re.split(r"[\r\n]+", str(u or "")))
    if url:
        raw.extend(re.split(r"[\r\n]+", str(url)))
    out: list[str] = []
    seen: set[str] = set()
    for line in raw:
        for candidate in re.split(r"(?=https?://)", line.strip(), flags=re.I):
            u = candidate.strip()
            if not u or u.startswith("#") or not re.match(r"^https?://", u, re.I):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
    return out


def _opts_from(kwargs: dict[str, Any]) -> dict[str, Any]:
    fmt = str(kwargs.get("format") or "mp4").lower()
    if fmt not in _FORMATS:
        fmt = "mp4"
    return {
        "format": fmt,
        "writeSubs": bool(kwargs.get("writeSubs", False)),
        "writeInfoJson": bool(kwargs.get("writeInfoJson", False)),
        "writeThumbnail": bool(kwargs.get("writeThumbnail", False)),
        # mặc định bật ghép (DASH YouTube cần merge)
        "mergeAv": bool(kwargs.get("mergeAv", True)),
        "preferFreeFormats": bool(kwargs.get("preferFreeFormats", False)),
        "folderBySource": bool(kwargs.get("folderBySource", False)),
    }


def start_jobs(
    url: str | None = None,
    quality: str = "best",
    urls: list[str] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    q = (quality or "best").strip().lower()
    if q not in _QUALITIES:
        raise ValueError(f"quality phải là một trong: {', '.join(sorted(_QUALITIES))}")
    opts = _opts_from(kwargs)
    if q == "audio":
        opts["format"] = "mp3"
    parsed = _normalize_urls(url, urls)
    if not parsed:
        raise ValueError("Cần ít nhất một URL http(s) hợp lệ (mỗi dòng một link)")
    return [start_job(u, q, **opts) for u in parsed]


def start_job(url: str, quality: str = "best", **kwargs: Any) -> dict[str, Any]:
    url = (url or "").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        raise ValueError("URL không hợp lệ")
    q = (quality or "best").strip().lower()
    if q not in _QUALITIES:
        raise ValueError(f"quality phải là một trong: {', '.join(sorted(_QUALITIES))}")
    opts = _opts_from(kwargs)
    if q == "audio":
        opts["format"] = "mp3"

    job_id = uuid.uuid4().hex[:12]
    root = ensure_download_dirs()
    if opts["folderBySource"]:
        job_dir = root / _host_folder(url) / job_id
    else:
        job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job: dict[str, Any] = {
        "id": job_id,
        "url": url,
        "title": None,
        "quality": q,
        "format": opts["format"],
        "status": "queued",
        "progress": 0,
        "message": "Đang xếp hàng…",
        "outputPath": None,
        "fileName": None,
        "_absPath": None,
        "_dir": str(job_dir),
        "_opts": opts,
        "log": ["Đã xếp hàng"],
        "createdAt": _now(),
    }
    with _LOCK:
        _JOBS[job_id] = job
    _publish("download.job.created", job_id, _public_job(job))
    _schedule_persist()

    def wrap():
        try:
            _run_ytdlp(job_id)
        except Exception as e:
            with _LOCK:
                j = _JOBS.get(job_id)
                if j and j["status"] not in ("done", "error"):
                    j["status"] = "error"
                    j["message"] = str(e)[:400]
                    _append_log_unlocked(j, f"Lỗi: {e}")
            _schedule_persist()

    threading.Thread(target=wrap, daemon=True, name=f"dl-{job_id}").start()
    return _public_job(job)


def _patch(job_id: str, **kw: Any) -> None:
    public: dict[str, Any] | None = None
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return
        j.update(kw)
        public = _public_job(j)
    _publish("download.job.updated", job_id, public)
    # progress spam → debounce; terminal states flush soon
    if kw.get("status") in ("done", "error", "queued"):
        _schedule_persist()
    elif "progress" in kw or "message" in kw or "title" in kw:
        _schedule_persist()


def _publish(event_type: str, job_id: str, payload: dict[str, Any] | None) -> None:
    try:
        from pipeline.core.realtime import realtime
        realtime.publish("download", event_type, job_id, payload)
    except Exception:
        return


def _run_ytdlp(job_id: str) -> None:
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j or j["status"] == "error":
            return
        url = j["url"]
        quality = j["quality"]
        job_dir = Path(j["_dir"])
        opts = dict(j.get("_opts") or {})

    merge_av = bool(opts.get("mergeAv", True))
    prefer_free = bool(opts.get("preferFreeFormats", False))
    # "Tối ưu dung lượng" map cùng preferFreeFormats từ UI cũ; nếu có key riêng thì dùng
    optimize = bool(opts.get("optimizeSize", prefer_free))
    write_subs = bool(opts.get("writeSubs", False))
    write_meta = bool(opts.get("writeInfoJson", False))
    write_thumb = bool(opts.get("writeThumbnail", False))
    merge_fmt = str(opts.get("format") or "mp4")
    if merge_fmt == "mp3":
        quality = "audio"

    _patch(job_id, status="running", progress=1, message="Đang lấy thông tin…")
    _append_log(job_id, f"Bắt đầu: {url}")
    _append_log(
        job_id,
        "opts: "
        + ", ".join(
            [
                f"q={quality}",
                f"fmt={merge_fmt}",
                f"merge={merge_av}",
                f"subs={write_subs}",
                f"meta={write_meta}",
                f"thumb={write_thumb}",
                f"size={optimize}",
                f"free={prefer_free}",
            ]
        ),
    )

    bin_ = _ytdlp_bin()
    subtitle_langs, automatic_subs = _platform_subtitle_selection(bin_, url) if write_subs else ([], False)
    if write_subs:
        _append_log(
            job_id,
            f"Phụ đề nền tảng: {', '.join(subtitle_langs) if subtitle_langs else 'không có (bỏ qua)'}"
            + (" · tự động" if automatic_subs else ""),
        )
    out_tmpl = str(job_dir / "%(title).80B [%(id)s].%(ext)s")
    fmt = _format_for(
        quality,
        merge_av=merge_av and quality != "audio",
        prefer_free=prefer_free and not optimize,
        optimize_size=optimize,
    )

    cmd: list[str] = [
        *bin_,
        "--no-playlist",
        "--newline",
        "--ignore-config",
        "-f",
        fmt,
        "-o",
        out_tmpl,
        "--print-json",
        "--no-simulate",
        "--no-mtime",
    ]

    if quality == "audio" or merge_fmt == "mp3":
        cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0" if not optimize else "5"])
    else:
        out_ext = merge_fmt if merge_fmt in ("mp4", "mkv", "webm") else "mp4"
        if merge_av:
            cmd.extend(["--merge-output-format", out_ext])
            # ép remux container khi cần
            if out_ext == "mp4":
                cmd.extend(["--remux-video", "mp4"])
        else:
            # progressive: vẫn giới hạn container nếu được
            cmd.extend(["--remux-video", out_ext])
        if prefer_free:
            cmd.append("--prefer-free-formats")
        if optimize:
            # giới hạn bitrate thô (yt-dlp format sort)
            cmd.extend(["-S", "res,br,size"])

    if subtitle_langs:
        cmd.extend(
            [
                # A platform can rate-limit one subtitle language while the media is fine.
                "--ignore-errors",
                "--write-subs",
                "--sub-langs",
                ",".join(subtitle_langs),
                "--sub-format",
                "json3/srt",
            ]
        )
        if automatic_subs:
            cmd.append("--write-auto-subs")
        # JSON3 is downloaded for YouTube auto-captions then converted locally
        # to SRT. ffmpeg cannot mux JSON3, so keep it as a sidecar file.
    if write_meta:
        cmd.extend(["--write-info-json", "--write-description"])
        if merge_fmt in ("mp4", "mkv", "m4a"):
            cmd.append("--embed-metadata")
    if write_thumb:
        cmd.extend(["--write-thumbnail", "--convert-thumbnails", "jpg"])
        if merge_fmt in ("mp4", "mkv", "m4a"):
            cmd.append("--embed-thumbnail")

    cmd.append(url)
    _append_log(job_id, "cmd: " + " ".join(cmd[:24]) + (" …" if len(cmd) > 24 else ""))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(job_dir),
        )
    except FileNotFoundError:
        _patch(
            job_id,
            status="error",
            message="Không tìm thấy yt-dlp. Cài: pip install yt-dlp",
        )
        _append_log(job_id, "yt-dlp không có trên PATH")
        return

    with _LOCK:
        _PROCS[job_id] = proc

    last_meta: dict | None = None
    assert proc.stdout is not None
    for line in proc.stdout:
        with _LOCK:
            cur = _JOBS.get(job_id)
            if not cur or cur["status"] == "error":
                kill_process_tree(proc)
                break
        line = line.rstrip()
        if not line:
            continue
        m = _PROGRESS_RE.search(line)
        if m:
            pct = min(99, float(m.group(1)))
            _patch(job_id, progress=pct, message=f"Đang tải {pct:.0f}%")
            continue
        if line.startswith("{"):
            try:
                last_meta = json.loads(line)
                title = last_meta.get("title")
                if title:
                    _patch(job_id, title=str(title)[:200])
                    _append_log(job_id, f"Title: {title}")
            except json.JSONDecodeError:
                _append_log(job_id, line[:200])
        else:
            if any(k in line.lower() for k in ("error", "warning", "destination", "merging")):
                _append_log(job_id, line[:300])

    code = proc.wait()
    with _LOCK:
        _PROCS.pop(job_id, None)
        cur = _JOBS.get(job_id)
        if not cur:
            return
        if cur["status"] == "error":
            return

    if code != 0:
        _patch(job_id, status="error", message=f"yt-dlp exit {code}", progress=0)
        _append_log(job_id, f"exit {code}")
        return

    # JSON3 is the one original provider track. Its SRT rendition represents
    # the on-screen rolling display; create a non-overlapping SRT for editing.
    for raw_subtitle in job_dir.glob("*.json3"):
        created = _json3_to_srt(raw_subtitle)
        if created:
            _append_log(job_id, f"Đã xuất phụ đề SRT: {created.name}")

    media_ext = {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".flac"}
    files = [p for p in job_dir.iterdir() if p.is_file() and p.suffix.lower() in media_ext]
    if not files:
        files = [
            p
            for p in job_dir.iterdir()
            if p.is_file() and p.suffix.lower() not in {".json", ".vtt", ".srt", ".jpg", ".webp", ".png"}
        ]
    if not files:
        _patch(job_id, status="error", message="Không thấy file sau khi tải")
        _append_log(job_id, "Không thấy file output")
        return

    best = max(files, key=lambda p: p.stat().st_size)
    root = get_download_root()
    try:
        rel = str(best.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = f"{job_id}/{best.name}"
    _patch(
        job_id,
        status="done",
        progress=100,
        message="Xong",
        title=cur.get("title") or best.stem,
        outputPath=rel,
        fileName=best.name,
        _absPath=str(best.resolve()),
    )
    _append_log(job_id, f"Xong: {best.name}")
