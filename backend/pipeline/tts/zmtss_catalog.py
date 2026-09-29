"""Online ZMTTS voice catalog and on-demand reference download.

The catalog lives in the public ZMTTS GitHub repository. Audio references are
not bundled with this application: a reference is fetched and converted only
when its voice is synthesized for the first time, then reused locally.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.config import DATA

RAW_BASE_URL = "https://raw.githubusercontent.com/manhgdev/ZMTTS/main"
MANIFEST_URL = f"{RAW_BASE_URL}/data/voices.json"
CACHE_PATH = DATA / "voices" / "zmtss_catalog.json"
CACHE_TTL_SECONDS = 15 * 60
VOICE_ID_PREFIX = "zmt:"

_LANGUAGES = {
    "tiếng việt": "vi", "tieng viet": "vi", "tiếng anh": "en", "tieng anh": "en",
    "tiếng trung": "zh", "tieng trung": "zh", "tiếng nhật": "ja", "tieng nhat": "ja",
    "tiếng hàn": "ko", "tieng han": "ko", "tiếng thái": "th", "tieng thai": "th",
    "tiếng indonesia": "id", "tieng indonesia": "id", "tiếng tây ban nha": "es", "tieng tay ban nha": "es",
    "tiếng pháp": "fr", "tieng phap": "fr", "tiếng đức": "de", "tieng duc": "de",
    "tiếng bồ đào nha": "pt", "tieng bo dao nha": "pt", "tiếng ý": "it", "tieng y": "it",
    "tiếng ấn độ": "hi", "tieng an do": "hi",
}


def language_code(value: Any) -> str:
    return _LANGUAGES.get(str(value or "").strip().lower(), "")


def _safe_repo_path(value: Any) -> str | None:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or path.suffix.lower() not in {".mp3", ".wav"}:
        return None
    return path.as_posix()


def _valid_voices(payload: Any) -> list[dict[str, Any]]:
    raw = payload.get("voices") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        voice_id = str(item.get("id") or "").strip()
        audio = _safe_repo_path(item.get("audio"))
        if voice_id and audio and len(voice_id) <= 160:
            result.append({**item, "id": voice_id, "audio": audio})
    return result


def _read_cache() -> tuple[float, list[dict[str, Any]]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return float(data.get("fetchedAt") or 0), _valid_voices(data)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, []


def _save_cache(voices: list[dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    pending = CACHE_PATH.with_name(f".{CACHE_PATH.name}.tmp")
    pending.write_text(json.dumps({"fetchedAt": time.time(), "voices": voices}, ensure_ascii=False), encoding="utf-8")
    pending.replace(CACHE_PATH)


def voices(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return a cached catalog; retain the last good catalog when offline."""
    fetched_at, cached = _read_cache()
    if cached and not force_refresh and time.time() - fetched_at < CACHE_TTL_SECONDS:
        return cached
    try:
        request = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "zmAI-TTS/1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            fresh = _valid_voices(json.loads(response.read().decode("utf-8")))
        if fresh:
            _save_cache(fresh)
            return fresh
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return cached


def get(voice_id: str) -> dict[str, Any] | None:
    raw_id = (voice_id or "").removeprefix(VOICE_ID_PREFIX)
    return next((item for item in voices() if item["id"] == raw_id), None)


def remote_url(item: dict[str, Any]) -> str:
    audio = _safe_repo_path(item.get("audio"))
    if not audio:
        raise ValueError("Đường dẫn audio ZMTTS không hợp lệ")
    return f"{RAW_BASE_URL}/{audio}"


def local_filename(item: dict[str, Any]) -> str:
    digest = hashlib.sha256(str(item["id"]).encode("utf-8")).hexdigest()[:16]
    return f"zmt-{digest}.wav"


def download_reference(item: dict[str, Any], destination: Path) -> None:
    """Download one demo then normalize it to the WAV format required by VieNeu."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Multiple preview/synthesis requests can materialize the same ZMTTS voice
    # concurrently. Per-destination temp names prevent one request from
    # deleting or replacing another request's in-progress files.
    token = uuid.uuid4().hex
    source = destination.with_name(f".{destination.name}.{token}.download")
    # Keep the final extension so ffmpeg can infer the WAV muxer. A suffix of
    # only `.tmp` makes ffmpeg reject the output before normalization starts.
    pending = destination.with_name(f".{destination.stem}.{token}.tmp{destination.suffix}")
    try:
        request = urllib.request.Request(remote_url(item), headers={"User-Agent": "zmAI-TTS/1"})
        with urllib.request.urlopen(request, timeout=30) as response, source.open("wb") as handle:
            total = 0
            while chunk := response.read(256 * 1024):
                total += len(chunk)
                if total > 12 * 1024 * 1024:
                    raise RuntimeError("Audio mẫu ZMTTS vượt quá kích thước cho phép")
                handle.write(chunk)
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(pending)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0 or not pending.is_file() or pending.stat().st_size < 1024:
            detail = (result.stderr or "Không thể đọc audio mẫu").strip()[-600:]
            raise RuntimeError(f"Không chuẩn hoá audio mẫu ZMTTS: {detail}")
        pending.replace(destination)
    except OSError as exc:
        raise RuntimeError(f"Không tải được giọng ZMTTS từ GitHub: {exc}") from exc
    finally:
        source.unlink(missing_ok=True)
        pending.unlink(missing_ok=True)
