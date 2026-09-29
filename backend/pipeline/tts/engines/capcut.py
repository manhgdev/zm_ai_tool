"""CapCut TTS engine wrapper."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .. import capcut as capcut_client
from ..schemas import PREFIX_CAPCUT

_VOICES_JSON = Path(__file__).resolve().parents[1] / "voices_capcut.json"
_cache: list[dict[str, Any]] | None = None


def list_voices(lang: str | None = None) -> list[dict[str, str]]:
    global _cache
    if _cache is None:
        if not _VOICES_JSON.is_file():
            _cache = []
        else:
            _cache = json.loads(_VOICES_JSON.read_text(encoding="utf-8"))
    # auto / trống → hiện all; còn lại lọc theo lan
    raw = (lang or "").strip().lower()
    prefer = "" if raw in ("", "auto", "all", "*") else raw.split("-")[0]
    aliases: set[str] = set()
    if prefer:
        aliases.add(prefer)
        if prefer in ("ja", "jp"):
            aliases.update({"ja", "jp"})
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for v in _cache:
        lan = (v.get("lan") or "").lower()
        if aliases and lan not in aliases:
            continue
        vt = v.get("voice_type") or ""
        rid = str(v.get("resource_id") or "")
        name = v.get("display_name") or vt
        if not (vt and rid):
            continue
        vid = f"{PREFIX_CAPCUT}{vt}:{rid}"
        # ponytail: same vt+rid can appear twice with corrupt display names
        if vid in seen:
            continue
        seen.add(vid)
        out.append(
            {
                "id": vid,
                "name": f"CapCut · {name}",
                "engine": "capcut",
                "type": "capcut",
                "language": (v.get("lang") or v.get("lan") or "").lower().split("-", 1)[0],
            }
        )
    return out


def parse(voice: str) -> tuple[str, str] | None:
    if not voice or not voice.startswith(PREFIX_CAPCUT):
        return None
    rest = voice[3:]
    voice_type, sep, resource_id = rest.rpartition(":")
    if not sep or not voice_type or not resource_id:
        return None
    return voice_type, resource_id


def synthesize(text: str, voice: str, out_wav: Path) -> None:
    parsed = parse(voice)
    if not parsed:
        raise ValueError(f"Not CapCut voice: {voice}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    mp3 = out_wav.with_suffix(".mp3")
    capcut_client.synthesize_mp3(text or ".", parsed[0], parsed[1], mp3)
    # CapCut's generated MP3s consistently contain ~70–120 ms of digital
    # silence before the first phoneme.  TTS clips are scheduled at the cue
    # start, so keeping that padding makes voice audibly arrive after caption.
    # Remove only initial silence; do not touch pauses inside spoken text.
    subprocess.check_call(
        [
            "ffmpeg", "-y", "-i", str(mp3), "-map", "0:a:0",
            "-af", "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB",
            "-acodec", "pcm_s16le", str(out_wav),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    mp3.unlink(missing_ok=True)
