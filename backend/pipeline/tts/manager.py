"""TTS manager — route voices to engines; public list / synth helpers."""
from __future__ import annotations

import hashlib
import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..core.config import EL_ADAM
from ..core.media import ffprobe_duration
from . import audio_utils
from .engines import vieneu as vieneu_engine
from .engines import system as system_engine
from .eleven import (
    EL_MODEL,
    EL_TTS_VER,
    _el_keys,
    _el_lang_code,
    _el_tts,
    _el_voice_id,
    _el_voice_options,
)
from . import capcut as capcut_client
from .schemas import PREFIX_CAPCUT, PREFIX_ELEVEN, PREFIX_VIENEU, VIENEU_TTS_VER

CC_TTS_VER = "cc6-final-trim-leading-silence"
_VOICES_JSON = Path(__file__).resolve().parent / "voices_capcut.json"
_cc_voices_cache: list[dict[str, Any]] | None = None


def _capcut_voice_metadata(voice: dict[str, Any]) -> dict[str, str]:
    voice_type = str(voice.get("voice_type") or "").lower()
    display_name = str(voice.get("display_name") or "").lower()
    gender = "female" if "female" in voice_type else "male" if "male" in voice_type else ""
    # ponytail: categories are only claimed when the provider name says so;
    # replace this small scan if CapCut starts returning structured labels.
    categories = (
        ("review", "review"),
        ("bản tin", "tin_tuc"),
        ("đọc thơ", "doc_tho"),
        ("quảng cáo", "quang_cao"),
        ("giọng bé", "tre_em"),
    )
    category = next((value for marker, value in categories if marker in display_name), "")
    from .voice_store import normalize_voice_language

    return {
        "language": normalize_voice_language(voice.get("lang") or voice.get("lan") or ""),
        "gender": gender,
        "category": category,
    }


def _cc_parse(voice: str) -> tuple[str, str] | None:
    if not voice or not voice.startswith(PREFIX_CAPCUT):
        return None
    rest = voice[3:]
    voice_type, sep, resource_id = rest.rpartition(":")
    if not sep or not voice_type or not resource_id:
        return None
    return voice_type, resource_id


def _capcut_tts(text: str, voice_type: str, resource_id: str, out_wav: Path) -> None:
    mp3 = out_wav.with_suffix(".mp3")
    capcut_client.synthesize_mp3(text or ".", voice_type, resource_id, mp3)
    subprocess.check_call(
        ["ffmpeg", "-y", "-i", str(mp3), "-acodec", "pcm_s16le", str(out_wav)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    mp3.unlink(missing_ok=True)


def _load_capcut_voices() -> list[dict[str, Any]]:
    global _cc_voices_cache
    if _cc_voices_cache is not None:
        return _cc_voices_cache
    if not _VOICES_JSON.is_file():
        _cc_voices_cache = []
        return []
    import json

    _cc_voices_cache = json.loads(_VOICES_JSON.read_text(encoding="utf-8"))
    return _cc_voices_cache


def _cc_voice_options(lang: str | None = None) -> list[dict[str, str]]:
    # auto / trống → hiện all CapCut; còn lại lọc theo lan
    raw = (lang or "").strip().lower()
    prefer = "" if raw in ("", "auto", "all", "*") else raw.split("-")[0]
    aliases: set[str] = set()
    if prefer:
        aliases.add(prefer)
        if prefer == "ja":
            aliases.add("jp")
        if prefer == "jp":
            aliases.add("ja")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for v in _load_capcut_voices():
        lan = (v.get("lan") or "").lower()
        if aliases and lan not in aliases:
            continue
        vt = v.get("voice_type") or ""
        rid = str(v.get("resource_id") or "")
        name = v.get("display_name") or vt
        if not vt or not rid:
            continue
        vid = f"{PREFIX_CAPCUT}{vt}:{rid}"
        # ponytail: voices_capcut.json once listed same vt+rid twice (Icathian/Male China)
        if vid in seen:
            continue
        seen.add(vid)
        out.append(
            {
                "id": vid,
                "name": f"CapCut · {name}",
                "engine": "capcut",
                "type": "capcut",
                "description": f"Giọng đám mây CapCut · {v.get('lang') or v.get('lan') or 'đa ngôn ngữ'}",
                **_capcut_voice_metadata(v),
            }
        )
    return out


def _parse_say_voices() -> list[tuple[str, str, str]]:
    if platform.system() != "Darwin":
        return []
    try:
        raw = subprocess.check_output(["say", "-v", "?"], text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        left = line.split("#", 1)[0].rstrip()
        m = re.search(r"\s([a-z]{2}_[A-Z]{2})\s*$", left)
        if not m:
            continue
        locale = m.group(1)
        raw_name = left[: m.start()].strip()
        say_id = raw_name.split(" (", 1)[0].strip()
        if not say_id or say_id in seen:
            continue
        seen.add(say_id)
        out.append((say_id, locale, f"{say_id} ({locale})"))
    return out


def list_voices(lang: str | None = None) -> list[dict[str, Any]]:
    voices: list[dict[str, Any]] = []
    # VieNeu first for Vietnamese local
    voices.extend(vieneu_engine.list_voices(lang))
    voices.extend(_cc_voice_options(lang))
    voices.extend(_el_voice_options())
    voices.extend(system_engine.list_voices(lang))
    # Auto includes the online ZMTTS catalog too; do not truncate it before
    # the selector can show the voices that are available on demand.
    return voices[:500]


def resolve_voice(voice: str, lang: str = "vi") -> str:
    if vieneu_engine.parse_voice(voice):
        return voice
    if _cc_parse(voice):
        return voice
    el = _el_voice_id(voice)
    if el:
        return f"{PREFIX_ELEVEN}{el}"
    if voice and voice != "system":
        return voice.split(" (", 1)[0].strip()
    prefer = (lang or "vi").split("-")[0].lower()
    # prefer VieNeu for VI if available
    if prefer == "vi" and vieneu_engine.available():
        vn = vieneu_engine.list_voices("vi")
        if vn:
            return vn[0]["id"]
    if _el_keys():
        return f"{PREFIX_ELEVEN}{EL_ADAM}"
    parsed = _parse_say_voices()
    for say_id, locale, _ in parsed:
        if locale.lower().startswith(prefer):
            return say_id
    for fallback in ("Linh", "Samantha", "Alex"):
        if any(s[0] == fallback for s in parsed):
            return fallback
    return voice if voice and voice != "system" else "Samantha"


def tts_cache_key(text: str, voice: str, lang: str, match: str) -> str:
    code = _el_lang_code(lang, text)
    if vieneu_engine.parse_voice(voice):
        ver, model = VIENEU_TTS_VER, "vieneu-v3turbo"
    elif voice.startswith(PREFIX_CAPCUT):
        ver, model = CC_TTS_VER, "capcut"
    else:
        ver, model = EL_TTS_VER, EL_MODEL
    ref_token = vieneu_engine.reference_cache_token(voice)
    raw = f"{text.strip()}|{voice}|{lang}|{match}|{model}|{code}|{ver}|{ref_token}".encode()
    return hashlib.sha1(raw).hexdigest()[:20]


def synthesize_raw(
    text: str,
    voice: str,
    out_wav: Path,
    lang: str = "vi",
    *,
    style: str = "tu_nhien",
) -> None:
    """Write wav for voice (no duration fit)."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_voice(voice, lang)
    vn = vieneu_engine.parse_voice(resolved)
    if vn:
        vieneu_engine.synthesize(text, resolved, out_wav, style=style)
        return
    cc = _cc_parse(resolved)
    el = _el_voice_id(resolved)
    if cc:
        _capcut_tts(text, cc[0], cc[1], out_wav)
    elif el:
        _el_tts(text, el, out_wav, lang=lang)
    else:
        system_engine.synthesize(text, resolved, out_wav, lang=lang)


def tts_segment(
    text: str,
    voice: str,
    out_wav: Path,
    target_sec: float | None,
    match: str,
    lang: str = "vi",
    *,
    force_refit: bool = False,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: float = 0.0,
    style: str = "tu_nhien",
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> float:
    """Synth (if needed) + optional speed/volume/pitch + duration fit.

    Long text: VieNeu handles chunking via max_chars; studio layer may pre-split.
    """
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    has_file = out_wav.exists() and out_wav.stat().st_size > 128
    if not has_file:
        if force_refit:
            force_refit = False
        resolved = resolve_voice(voice, lang)
        if vieneu_engine.parse_voice(resolved):
            vieneu_engine.synthesize(
                text,
                resolved,
                out_wav,
                style=style,
                cancel_check=cancel_check,
                on_progress=on_progress,
            )
        else:
            if on_progress:
                on_progress(0.05)
            synthesize_raw(text, voice, out_wav, lang=lang, style=style)
            if on_progress:
                on_progress(1.0)
        if abs(speed - 1.0) > 0.02 or abs(volume - 1.0) > 0.02 or abs(pitch) > 0.1:
            audio_utils.apply_playback(
                out_wav, speed=speed, volume=volume, pitch_semitones=pitch
            )

    duration = audio_utils.fit_duration(
        out_wav, target_sec, match, force_refit=force_refit
    )
    # Playback/fit can pass through FFmpeg's atempo filter, which may add
    # padding again.  Only trim CapCut clips: this preserves intentional
    # leading room from other providers.
    if _cc_parse(resolve_voice(voice, lang)):
        duration = audio_utils.trim_leading_silence(out_wav)
    return duration


def engines_status() -> dict[str, Any]:
    return {
        "vieneu": vieneu_engine.status(),
        "capcut": {"id": "capcut", "name": "CapCut TTS", "local": False, "ready": True},
        "elevenlabs": {
            "id": "elevenlabs",
            "name": "ElevenLabs",
            "local": False,
            "ready": bool(_el_keys()),
            "message": "" if _el_keys() else "Thiếu API key",
        },
        "system": {
            "id": "system",
            "name": "System",
            "local": True,
            "ready": True,
            "device": platform.system(),
        },
    }
