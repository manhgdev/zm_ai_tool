"""Cloud-assisted Series TXT drafting.

This module deliberately creates only a reviewable TXT draft.  Importing it
into a Series remains a separate user action, so an AI response can never
silently create or overwrite a project.
"""
from __future__ import annotations

import re

from pipeline.core.app_config import provider_api_keys, provider_credentials
from pipeline.mt.cloud import _gemini_generate, _openai_compatible_chat

_PROVIDERS = {"openai", "gemini", "openrouter", "grok"}


def _clean_text(text: str) -> str:
    text = (text or "").strip()
    fenced = re.search(r"```(?:txt|text|markdown)?\s*([\s\S]*?)```", text, re.I)
    return (fenced.group(1) if fenced else text).strip()


def _prompt(idea: str, episodes: int, scenes_per_episode: int) -> str:
    return f"""You are a professional visual-series planner. Write a Vietnamese Series plan as plain TXT, with no markdown explanation or code fences.

Creative brief:
{idea.strip()}

Return exactly this syntax:
# SERIES: concise series title
# TẬP 01 — episode title
001_[00.00_00.00-00.00_08.00] visual scene prompt

Create exactly {episodes} episode(s), each with exactly {scenes_per_episode} scene(s). Scene numbering restarts at 001 in every episode. Timecodes must be continuous 8-second blocks inside each episode. Every scene prompt must include:
- START STATE: where characters/camera begin (matches previous END when continuing)
- ACTION: what happens in this 8-second shot only
- END STATE: freeze-frame pose/set for the next shot to continue from
Preserve locked character appearance, clothes, props, setting and cartoon art style. Do not restart the plot each scene. Do not add any text outside the requested TXT."""

def draft_script(*, provider: str, idea: str, episodes: int, scenes_per_episode: int) -> str:
    provider = (provider or "").strip().lower()
    if provider not in _PROVIDERS:
        raise ValueError("SERIES_CLOUD_PROVIDER_UNSUPPORTED")
    if not idea.strip():
        raise ValueError("SERIES_CLOUD_IDEA_REQUIRED")
    if not 1 <= episodes <= 10 or not 1 <= scenes_per_episode <= 10:
        raise ValueError("SERIES_CLOUD_SIZE_INVALID")

    credentials = provider_credentials(provider)
    keys = provider_api_keys(provider)
    prompt = _prompt(idea, episodes, scenes_per_episode)
    if provider == "gemini":
        result = _gemini_generate(
            base_url=credentials["baseUrl"], api_keys=keys, model=credentials["model"],
            prompt=prompt, timeout=180.0, max_output_tokens=12_000,
        )
    else:
        result = _openai_compatible_chat(
            base_url=credentials["baseUrl"], api_keys=keys, model=credentials["model"],
            prompt=prompt, timeout=180.0, max_output_tokens=12_000,
            system_msg="Return only valid Series TXT. Do not use markdown fences.",
        )
    result = _clean_text(result)
    if not result:
        raise RuntimeError("SERIES_CLOUD_EMPTY_RESPONSE")
    return result
