"""TTS: zmAI Local + CapCut + ElevenLabs + system (say/espeak)."""
from __future__ import annotations

from .eleven import EL_MODEL, EL_TTS_VER, _el_lang_code, _el_voice_id
from .manager import (
    CC_TTS_VER,
    engines_status,
    list_voices,
    resolve_voice,
    synthesize_raw,
    tts_cache_key,
    tts_segment,
)

# studio helpers (optional import path)
try:
    from .studio import list_history, request_cancel, synth_srt_job, synth_text_job
except Exception:  # pragma: no cover
    list_history = request_cancel = synth_srt_job = synth_text_job = None  # type: ignore

__all__ = [
    "CC_TTS_VER",
    "EL_MODEL",
    "EL_TTS_VER",
    "_el_lang_code",
    "_el_voice_id",
    "engines_status",
    "list_voices",
    "resolve_voice",
    "synthesize_raw",
    "tts_cache_key",
    "tts_segment",
    "list_history",
    "request_cancel",
    "synth_srt_job",
    "synth_text_job",
]
