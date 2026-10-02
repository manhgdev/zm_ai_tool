"""CapCut cloud STT + translation for the standalone SRT exporter.

This is intentionally separate from local Whisper: a media file is uploaded to
CapCut, the ``cc_audio_subtitle_asr`` task is requested with translation
enabled, then its timed translated utterances are written as SRT by
``pipeline.srt_export``.  The request/device helpers are shared with the
existing CapCut TTS integration.
"""
from __future__ import annotations

import binascii
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx

from pipeline.tts.capcut import BASE, base_headers, common_query, compact_json, load_device, make_sign_header
from pipeline.core.config import DATA
from pipeline.core.media import _ff_bin

_VOD_REGION = "sdwdmwlll"
_VOD_SERVICE = "vod"
_LANGUAGES = {
    "auto": "auto", "vi": "vi-VN", "en": "en-US", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR",
}
# App endpoints normally report ``ret=0``.  CapCut's VOD transfer endpoints
# instead return ``ret=2000, errmsg=Success`` after accepting a binary part.
# Both are successful responses, despite using different response contracts.
_SUCCESS_CODES = {"", "0", "00", "2000"}
# A valid task id may be returned before CapCut's worker picks it up, but an
# empty query response for long periods is not a queued job.  Do not leave the
# editor stuck for the full 20-minute task deadline in that situation.
_MAX_MISSING_STATUS_POLLS = 15
CAPCUT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_CAPCUT_PROXY_TARGET_BYTES = 190 * 1024 * 1024
_AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
_CAPCUT_CACHE_DIR = DATA / "cache" / "capcut-stt"
_CAPCUT_RAW_DIR = DATA / "capcut-stt-raw"
_CAPCUT_CHUNK_SECONDS = 15 * 60
_CAPCUT_CHUNK_WORKERS = 2


class CapCutSttError(RuntimeError):
    """CapCut cannot complete a cloud subtitle/translation task."""


def _language(value: str, *, default: str) -> str:
    return _LANGUAGES.get((value or "").lower().split("-", 1)[0], value or default)


def _cancelled(check: Callable[[], bool] | None) -> None:
    if check and check():
        raise InterruptedError("Đã hủy xuất phụ đề CapCut")


def _json(client: httpx.Client, method: str, url: str, *, headers: dict[str, str], content: Any = None, timeout: float = 60) -> dict[str, Any]:
    response = client.request(method, url, headers=headers, content=content, timeout=timeout)
    try:
        data = response.json()
    except Exception as exc:
        raise CapCutSttError(f"CapCut trả HTTP {response.status_code} không phải JSON") from exc
    if response.status_code >= 400:
        raise CapCutSttError(f"CapCut HTTP {response.status_code}: {data}")
    ret = str(data.get("ret") or data.get("code") or "")
    message = str(data.get("errmsg") or data.get("message") or "")
    if ret not in _SUCCESS_CODES:
        raise CapCutSttError(f"CapCut lỗi {ret}: {message or data}")
    return data


def _post_task(client: httpx.Client, path: str, body: dict[str, Any], device: dict[str, Any], *, babi: dict[str, Any] | None, appid: bool) -> dict[str, Any]:
    body_text = compact_json(body)
    url = BASE + path + "?" + urlencode(common_query(device, babi, include_region=babi is not None))
    headers = base_headers(device, body_text, appid=appid)
    headers["sign"] = make_sign_header(url, str(device["appvr"]), headers["device-time"], str(device["tdid"]))
    return _json(client, "POST", url, headers=headers, content=body_text.encode("utf-8"))


def _file_hashes(path: Path, check: Callable[[], bool] | None) -> tuple[str, str]:
    md5 = hashlib.md5()
    crc = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            _cancelled(check)
            md5.update(block)
            crc = binascii.crc32(block, crc)
    return md5.hexdigest(), f"{crc & 0xffffffff:08x}"


def _cache_path(md5: str, source_lang: str, target_lang: str) -> Path:
    key = "|".join((md5, _language(source_lang, default="auto"), _language(target_lang, default="vi-VN")))
    return _CAPCUT_CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def _load_cached_cues(md5: str, source_lang: str, target_lang: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    try:
        cached = json.loads(_cache_path(md5, source_lang, target_lang).read_text(encoding="utf-8"))
        source, translated = cached.get("source"), cached.get("translated")
        if isinstance(source, list) and source and isinstance(translated, list):
            return source, translated
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def _write_cached_cues(md5: str, source_lang: str, target_lang: str, source: list[dict[str, Any]], translated: list[dict[str, Any]]) -> None:
    path = _cache_path(md5, source_lang, target_lang)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"source": source, "translated": translated}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # ponytail: cache must never make a successful CapCut result fail; retry next run if disk is unavailable.
        pass


def _save_raw_payload(task: dict[str, Any], *, response: dict[str, Any], md5: str,
                      source_lang: str, target_lang: str,
                      request: dict[str, Any], filename: str) -> None:
    """Archive the unfiltered result before parsing; never archive task tokens.

    Each cloud completion (including each audio chunk) gets a separate file.
    Keep payload strings unchanged, even when they contain invalid JSON.
    """
    now = dt.datetime.now(dt.timezone.utc)
    key = _cache_path(md5, source_lang, target_lang).stem
    path = _CAPCUT_RAW_DIR / key / f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    # The query envelope is retained as returned, except the task token which
    # is a credential for polling and must never be written to disk.
    safe_response = json.loads(json.dumps(response, ensure_ascii=False))
    for item in ((safe_response.get("data") or {}).get("tasks") or []):
        if isinstance(item, dict):
            item.pop("token", None)
    record = {
        "saved_at": now.isoformat(), "source_md5": md5, "filename": filename,
        "source_lang": source_lang, "target_lang": target_lang,
        "task_id": task.get("id"), "request": request,
        "response": safe_response,
        "payload": task.get("payload"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        logging.getLogger(__name__).info("CapCut raw payload saved: %s", path)
    except OSError:
        # ponytail: preserve the usable transcript on disk failure; report the
        # archive failure rather than claiming the original was saved.
        logging.getLogger(__name__).warning("Cannot save CapCut raw payload: %s", path, exc_info=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _hmac(key: str | bytes, value: str | bytes) -> bytes:
    return hmac.new(key.encode() if isinstance(key, str) else key, value.encode() if isinstance(value, str) else value, hashlib.sha256).digest()


def _aws_auth(method: str, url: str, payload: bytes, credentials: dict[str, Any], timestamp: str) -> str:
    day = timestamp[:8]
    scope = f"{day}/{_VOD_REGION}/{_VOD_SERVICE}/aws4_request"
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(pairs))
    token = str(credentials["session_token"])
    headers = f"x-amz-date:{timestamp}\nx-amz-security-token:{token}\n"
    signed = "x-amz-date;x-amz-security-token"
    canonical = "\n".join((method, urlsplit(url).path, query, headers, signed, hashlib.sha256(payload).hexdigest()))
    to_sign = "\n".join(("AWS4-HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = _hmac(_hmac(_hmac(_hmac("AWS4" + str(credentials["secret_access_key"]), day), _VOD_REGION), _VOD_SERVICE), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={credentials['access_key_id']}/{scope}, SignedHeaders={signed}, Signature={signature}"


def _vod_headers(method: str, url: str, payload: bytes, credentials: dict[str, Any], device: dict[str, Any]) -> dict[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return {
        "Authorization": _aws_auth(method, url, payload, credentials, stamp),
        "Date": now.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "X-Amz-Date": stamp, "X-Amz-Expires": "31536000", "X-Amz-Security-Token": str(credentials["session_token"]),
        "User-Agent": f"BDFileUpload({int(time.time() * 1000)})", "accept-encoding": "identity",
        "tdid": str(device["tdid"]), "pf": str(device["pf"]),
        "store-country-code": str(device["loc"]).lower(), "store-country-code-src": "did",
    }


def _probe_duration_seconds(path: Path) -> float:
    try:
        result = subprocess.run(
            [_ff_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        value = float(result.stdout.strip() or 0)
        return value if value > 0 else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _split_audio_chunks(
    path: Path, directory: Path, check: Callable[[], bool] | None,
    progress: Callable[[str], None] | None,
) -> list[tuple[Path, float]]:
    """Extract compact mono audio once, then split it into CapCut-safe chunks."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "chunk-%04d.m4a"
    command = [
        _ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k",
        "-f", "segment", "-segment_time", str(_CAPCUT_CHUNK_SECONDS), "-reset_timestamps", "1",
        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart", str(target),
    ]
    if progress:
        progress("CapCut: đang tách audio thành các đoạn ngắn… / splitting audio into short chunks…")
    try:
        _cancelled(check)
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        _cancelled(check)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapCutSttError(f"CAPCUT_CHUNK_PREP_FAILED: không thể tách audio / unable to split audio: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()[-1] if result.stderr else ""
        raise CapCutSttError(f"CAPCUT_CHUNK_PREP_FAILED: không thể tách audio / unable to split audio{f': {detail}' if detail else ''}")
    chunks: list[tuple[Path, float]] = []
    for chunk in sorted(directory.glob("chunk-*.m4a")):
        duration = _probe_duration_seconds(chunk)
        if duration <= 0 or chunk.stat().st_size <= 0:
            raise CapCutSttError(f"CAPCUT_CHUNK_PREP_FAILED: đoạn audio không hợp lệ / invalid audio chunk: {chunk.name}")
        if chunk.stat().st_size > CAPCUT_MAX_UPLOAD_BYTES:
            raise CapCutSttError(
                f"CAPCUT_CHUNK_TOO_LARGE: đoạn audio vẫn vượt giới hạn / audio chunk still exceeds "
                f"the {CAPCUT_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
            )
        chunks.append((chunk, duration))
    if not chunks:
        raise CapCutSttError("CAPCUT_CHUNK_PREP_FAILED: không tìm thấy audio để nhận dạng / no audio stream to transcribe")
    return chunks


def _offset_cues(cues: list[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    return [
        {**cue, "start": round(float(cue["start"]) + offset, 3), "end": round(float(cue["end"]) + offset, 3)}
        for cue in cues
    ]


def _transcribe_chunked_audio(
    path: Path, source_lang: str, target_lang: str, *, require_translation: bool = True,
    force_refresh: bool = False,
    cancelled: Callable[[], bool] | None = None, progress: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run CapCut on short audio chunks and restore source-media timecodes."""
    with tempfile.TemporaryDirectory(prefix="capcut-chunks-") as raw_directory:
        chunks = _split_audio_chunks(path, Path(raw_directory), cancelled, progress)
        # AAC/M4A duration includes encoder priming (typically ~20–60ms).
        # Segment boundaries use the requested timeline, so derive offsets
        # from that boundary instead of accumulating probe durations.
        offsets = [index * float(_CAPCUT_CHUNK_SECONDS) for index in range(len(chunks))]

        def transcribe(index: int, chunk: Path) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
            _cancelled(cancelled)
            if progress:
                progress(f"CapCut: đang nhận dạng đoạn {index + 1}/{len(chunks)}… / transcribing chunk {index + 1}/{len(chunks)}…")
            source, translated = transcribe_and_translate(
                chunk, source_lang, target_lang, require_translation=require_translation,
                force_refresh=force_refresh,
                cancelled=cancelled, progress=progress,
            )
            return index, source, translated

        completed: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]] = []
        workers = min(_CAPCUT_CHUNK_WORKERS, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(transcribe, index, chunk) for index, (chunk, _) in enumerate(chunks)]
            for future in as_completed(futures):
                _cancelled(cancelled)
                completed.append(future.result())

    source_result: list[dict[str, Any]] = []
    translated_result: list[dict[str, Any]] = []
    for index, source, translated in sorted(completed):
        source_result.extend(_offset_cues(source, offsets[index]))
        translated_result.extend(_offset_cues(translated, offsets[index]))
    return source_result, translated_result


def _create_media_proxy(path: Path, check: Callable[[], bool] | None, progress: Callable[[str], None] | None) -> Path:
    """Create a compact CapCut-compatible proxy for media over 200 MiB."""
    is_audio = path.suffix.lower() in _AUDIO_EXTENSIONS
    suffix = ".m4a" if is_audio else ".mp4"
    fd, raw_target = tempfile.mkstemp(prefix="capcut-upload-", suffix=suffix)
    os.close(fd)
    target = Path(raw_target)
    duration = _probe_duration_seconds(path)
    if is_audio:
        command = [
            _ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", str(target),
        ]
    else:
        # Reserve headroom for container metadata and audio.  The bitrate is
        # derived from duration so long recordings stay below CapCut's limit.
        # If probing is unavailable, choose a conservative one-hour estimate
        # rather than producing a short-clip bitrate that can exceed the cap.
        seconds = duration if duration > 0 else 3600.0
        audio_bps = 96_000
        overhead_bps = 24_000
        video_bps = int((_CAPCUT_PROXY_TARGET_BYTES * 8 / seconds) - audio_bps - overhead_bps)
        video_bps = max(96_000, min(2_000_000, video_bps))
        command = [
            _ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:v:0?", "-map", "0:a:0?", "-vf", "scale=1280:-2:force_original_aspect_ratio=decrease",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(video_bps),
            "-maxrate", str(video_bps), "-bufsize", str(video_bps * 2),
            "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-movflags", "+faststart", str(target),
    ]
    if progress:
        progress("CapCut: media vượt 200 MB, đang tạo bản proxy nén… / media exceeds 200 MB; creating a compressed proxy…")
    try:
        _cancelled(check)
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        _cancelled(check)
        if result.returncode or not target.is_file() or target.stat().st_size <= 0:
            detail = (result.stderr or "").strip().splitlines()[-1] if result.stderr else ""
            raise CapCutSttError(
                f"CAPCUT_PROXY_FAILED: không thể nén media / unable to compress media"
                f"{f': {detail}' if detail else ''}"
            )
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _prepare_upload_media(
    path: Path, check: Callable[[], bool] | None, progress: Callable[[str], None] | None,
) -> tuple[Path, Path | None]:
    """Return the upload path and an optional temporary proxy to clean up."""
    if not path.is_file():
        raise CapCutSttError("Không tìm thấy video để gửi CapCut")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CapCutSttError(f"Không đọc được media để gửi CapCut: {exc}") from exc
    if size <= CAPCUT_MAX_UPLOAD_BYTES:
        return path, None
    try:
        proxy = _create_media_proxy(path, check, progress)
    except InterruptedError:
        raise
    except CapCutSttError:
        raise
    except Exception as exc:
        raise CapCutSttError(f"CAPCUT_PROXY_FAILED: không thể nén media / unable to compress media: {exc}") from exc
    try:
        proxy_size = proxy.stat().st_size
    except OSError as exc:
        proxy.unlink(missing_ok=True)
        raise CapCutSttError("CAPCUT_PROXY_FAILED: không đọc được bản proxy / unable to read compressed proxy") from exc
    if proxy_size > CAPCUT_MAX_UPLOAD_BYTES:
        proxy.unlink(missing_ok=True)
        raise CapCutSttError(
            f"CAPCUT_MEDIA_TOO_LARGE: media sau khi nén vẫn vượt giới hạn / "
            f"compressed media still exceeds the {CAPCUT_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit "
            f"(original {size / (1024 * 1024):.1f} MB, compressed {proxy_size / (1024 * 1024):.1f} MB)"
        )
    return proxy, proxy


def _upload(path: Path, client: httpx.Client, device: dict[str, Any], check: Callable[[], bool] | None, progress: Callable[[str], None] | None) -> tuple[str, str, int]:
    upload_path, temporary = _prepare_upload_media(path, check, progress)
    try:
        return _upload_once(upload_path, client, device, check, progress)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _upload_once(path: Path, client: httpx.Client, device: dict[str, Any], check: Callable[[], bool] | None, progress: Callable[[str], None] | None) -> tuple[str, str, int]:
    if not path.is_file():
        raise CapCutSttError("Không tìm thấy video để gửi CapCut")
    if progress:
        progress("CapCut: đang chuẩn bị video…")
    md5, crc32 = _file_hashes(path, check)
    _cancelled(check)
    sign = _post_task(client, "/lv/v1/upload_sign", {"biz": "cc_pc_text_recognize", "key_version": "v5"}, device, babi=None, appid=True)
    credentials = sign.get("data") or {}
    required = ("domain", "access_key_id", "secret_access_key", "session_token", "space_name")
    if any(not credentials.get(key) for key in required):
        raise CapCutSttError("CapCut không cấp quyền upload video")
    base = f"https://{credentials['domain']}/top/v1?"
    apply_url = base + urlencode({"Action": "ApplyUploadInner", "SpaceName": credentials["space_name"], "UseQuic": "false", "Version": "2020-11-19", "device_platform": "win"})
    apply = _json(client, "GET", apply_url, headers=_vod_headers("GET", apply_url, b"", credentials, device))
    try:
        node = apply["Result"]["InnerUploadAddress"]["UploadNodes"][0]
        store = node["StoreInfos"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise CapCutSttError("CapCut trả dữ liệu upload không hợp lệ") from exc
    if progress:
        progress("CapCut: đang tải video lên…")
    transfer_url = f"https://{node['UploadHost']}/upload/v1/{store['StoreUri']}?" + urlencode({"uploadid": store["UploadID"], "part_number": "0", "phase": "transfer"})
    transfer_headers = {"Authorization": store["Auth"], "X-Upload-Content-CRC32": crc32, "User-Agent": "BDFileUpload", "accept-encoding": "identity"}
    with path.open("rb") as stream:
        _json(client, "POST", transfer_url, headers=transfer_headers, content=stream, timeout=900)
    _cancelled(check)
    finish_url = f"https://{node['UploadHost']}/upload/v1/{store['StoreUri']}?" + urlencode({"uploadmode": "part", "phase": "finish", "uploadid": store["UploadID"]})
    _json(client, "POST", finish_url, headers={"Authorization": store["Auth"], "User-Agent": "BDFileUpload", "accept-encoding": "identity"}, content=f"0:{crc32}".encode())
    commit_url = base + urlencode({"Action": "CommitUploadInner", "SpaceName": credentials["space_name"], "Version": "2020-11-19", "device_platform": "win"})
    commit_body = compact_json({"Functions": [{"Input": {"SnapshotTime": 0.0}, "Name": "Snapshot"}], "SessionKey": node["SessionKey"]}).encode()
    committed = _json(client, "POST", commit_url, headers=_vod_headers("POST", commit_url, commit_body, credentials, device), content=commit_body, timeout=120)
    try:
        result = committed["Result"]["Results"][0]
        duration = int(float((result.get("VideoMeta") or {}).get("Duration") or 0) * 1000)
        return str(result.get("Vid") or node.get("Vid")), str((result.get("VideoMeta") or {}).get("Md5") or md5), duration
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CapCutSttError("CapCut không xác nhận video đã tải") from exc


def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CapCutSttError("CapCut trả phụ đề không hợp lệ") from exc
    return payload if isinstance(payload, dict) else {}


def subtitle_cues(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize CapCut STT payload while tolerating current field aliases."""
    rows = payload.get("utterances") or payload.get("captions") or payload.get("subtitles") or []
    source: list[dict[str, Any]] = []
    translated: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("source_text") or item.get("original_text") or "").strip()
        start = float(item.get("start_time", item.get("start", 0)) or 0) / (1000 if "start_time" in item else 1)
        end = float(item.get("end_time", item.get("end", 0)) or 0) / (1000 if "end_time" in item else 1)
        if not text or end <= start:
            continue
        cue = {"start": start, "end": end, "text": text}
        source.append(cue)
        # The live CapCut ASR task uses ``translation_text``.  Older payloads
        # have used the aliases that follow, so retain all of them.
        target = str(item.get("translation_text") or item.get("translation") or item.get("translated_text") or item.get("target_text") or item.get("translate_text") or "").strip()
        if target:
            translated.append({**cue, "text": target})
    return source, translated


def _task_progress(task: dict[str, Any]) -> int | None:
    """Read CapCut's real task percentage, if this API deployment supplies it."""
    for key in ("progress", "percent", "percentage", "task_progress"):
        value = task.get(key)
        if value in (None, ""):
            continue
        try:
            # The live endpoint currently returns integer 0..100.  Keep the
            # bounds defensive so a malformed upstream response never shows
            # an impossible percentage in a job log.
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            continue
    return None


def _poll_progress_message(status: str, elapsed_sec: float, poll_count: int, percent: int | None = None) -> str:
    """Show CapCut's own percentage when present; never synthesize one."""
    state = {
        "queueing": "đang xếp hàng trên CapCut",
        "queued": "đang xếp hàng trên CapCut",
        "pending": "đang chờ CapCut xử lý",
        "processing": "CapCut đang nhận dạng và dịch",
        "running": "CapCut đang nhận dạng và dịch",
        "success": "CapCut đã hoàn tất",
    }.get((status or "").lower(), "đang chờ phản hồi từ CapCut")
    cloud_percent = f" · {percent}%" if percent is not None else ""
    return f"CapCut: {state}{cloud_percent} · đã chờ {max(0, round(elapsed_sec))}s · kiểm tra #{max(1, poll_count)}"


def _task_status(query: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return a valid task and status, or an empty status for a stale reply."""
    rows = ((query.get("data") or {}).get("tasks") or [])
    task = rows[0] if rows and isinstance(rows[0], dict) else {}
    raw = task.get("status") or task.get("task_status") or task.get("state") or ""
    status = str(raw).strip().lower()
    # VOD/common-task deployments use these equivalent terminal labels.
    # The live common-task API currently reports ``succeed`` (not
    # ``success``) when subtitle recognition has finished.  Treat all of the
    # terminal-success spellings equivalently before reading its payload.
    status = {
        "done": "success",
        "completed": "success",
        "succeed": "success",
        "failure": "failed",
    }.get(status, status)
    return task, status


def transcribe_and_translate(path: Path, source_lang: str, target_lang: str, *, require_translation: bool = True, force_refresh: bool = False, cancelled: Callable[[], bool] | None = None, progress: Callable[[str], None] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Upload one video, ask CapCut STT to translate it, and return timed cues."""
    if not path.is_file():
        raise CapCutSttError("Không tìm thấy video để gửi CapCut")
    source_md5, _ = _file_hashes(path, cancelled)
    cached = _load_cached_cues(source_md5, source_lang, target_lang)
    if cached and not force_refresh and (not require_translation or cached[1]):
        if progress:
            progress("CapCut: dùng kết quả đã lưu, không tải video lên…")
        return cached
    if path.stat().st_size > CAPCUT_MAX_UPLOAD_BYTES:
        source, translated = _transcribe_chunked_audio(
            path, source_lang, target_lang, require_translation=require_translation,
            force_refresh=force_refresh,
            cancelled=cancelled, progress=progress,
        )
        _write_cached_cues(source_md5, source_lang, target_lang, source, translated)
        return source, translated
    device = load_device()
    with httpx.Client(trust_env=False, follow_redirects=True) as client:
        vid, md5, duration = _upload(path, client, device, cancelled, progress)
        _cancelled(cancelled)
        if progress:
            progress("CapCut: đang nhận dạng và dịch…")
        babi = {"feature_entrance": "editor", "feature_entrance_detail": "editor-elements-captions-subtitle_recognition", "feature_key": "subtitle_recognition", "scenario": "video_editor"}
        cap_json = {"adjust_endtime": 200, "audio": vid, "audio_type": "vid", "caption_type": 0, "client_request_id": str(uuid.uuid4()), "duration": max(1, duration), "enable_cache": not force_refresh, "enter_from": "asr", "language": _language(source_lang, default="auto"), "max_lines": 1, "md5": md5, "pack_options": {"need_attribute": True}, "songs_info": [{"end_time": max(1, duration) - 10.334, "id": "", "start_time": 0}], "translation_language": _language(target_lang, default="vi-VN"), "use_translation": True, "words_per_line": 15}
        created = _post_task(client, "/lv/v1/common_task/new", {"bind_id": str(uuid.uuid4()).upper(), "can_queue": True, "enter_from": "asr", "tasks": [{"context": str(uuid.uuid4()), "payload": compact_json({"cap_json": cap_json}), "req_key": "cc_audio_subtitle_asr", "task_version": "v3"}]}, device, babi=babi, appid=False)
        tasks = ((created.get("data") or {}).get("tasks") or [])
        if not tasks or not tasks[0].get("id") or not tasks[0].get("token"):
            raise CapCutSttError("CapCut không tạo được tác vụ dịch phụ đề")
        task_id, token = str(tasks[0]["id"]), str(tasks[0]["token"])
        started = time.monotonic()
        deadline = started + 20 * 60
        polls = 0
        missing_status_polls = 0
        last_report_at = -6.0
        last_status = ""
        last_percent: int | None = None
        while time.monotonic() < deadline:
            _cancelled(cancelled)
            query = _post_task(client, "/lv/v1/common_task/query", {"tasks": [{"bind_id": "", "id": task_id, "req_key": "cc_audio_subtitle_asr", "task_version": "v3", "token": token}]}, device, babi=None, appid=False)
            task, status = _task_status(query)
            polls += 1
            elapsed = time.monotonic() - started
            if not status:
                missing_status_polls += 1
                if progress and (elapsed - last_report_at >= 6.0):
                    progress(_poll_progress_message("", elapsed, polls))
                    last_report_at = elapsed
                if missing_status_polls >= _MAX_MISSING_STATUS_POLLS:
                    raise CapCutSttError(
                        "CapCut không trả trạng thái tác vụ sau 30 giây. Hãy thử lại sau ít phút."
                    )
                time.sleep(2)
                continue
            missing_status_polls = 0
            cloud_percent = _task_progress(task)
            # Polling is every two seconds; log at a readable six-second
            # cadence, whenever CapCut moves state, or when its real
            # percentage changes.  The last condition prevents 23% → 99%
            # from being hidden between the six-second status updates.
            if progress and (status != last_status or cloud_percent != last_percent or elapsed - last_report_at >= 6.0):
                progress(_poll_progress_message(status, elapsed, polls, cloud_percent))
                last_report_at, last_status, last_percent = elapsed, status, cloud_percent
            if status == "success":
                _save_raw_payload(
                    task, response=query, md5=source_md5, source_lang=source_lang, target_lang=target_lang,
                    request=cap_json, filename=path.name,
                )
                source, translated = subtitle_cues(_task_payload(task))
                if not source:
                    raise CapCutSttError("CapCut không trả câu phụ đề có timecode")
                if require_translation and not translated:
                    raise CapCutSttError("CapCut không trả bản dịch cho ngôn ngữ đã chọn")
                _write_cached_cues(source_md5, source_lang, target_lang, source, translated)
                return source, translated
            if status in {"failed", "error", "cancelled"}:
                raise CapCutSttError(f"CapCut dịch phụ đề thất bại: {task.get('message') or task}")
            time.sleep(2)
    raise CapCutSttError("CapCut dịch phụ đề quá thời gian chờ")
