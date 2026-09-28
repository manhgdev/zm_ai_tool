"""AI-assisted Series drafting from a single topic.

This module deliberately creates only a reviewable draft.  Importing it into a
Series remains a separate user action, so an AI response can never silently
create or overwrite a project.
"""
from __future__ import annotations

import re

_BIBLE_RE = re.compile(r"^\s*#\s*BIBLE\s*:?\s*(.*)$", re.I)
_EPISODE_RE = re.compile(r"^\s*#\s*TẬP\s*\d+", re.I)


def _clean_text(text: str) -> str:
    text = (text or "").strip()
    fenced = re.search(r"```(?:txt|text|markdown)?\s*([\s\S]*?)```", text, re.I)
    return (fenced.group(1) if fenced else text).strip()


def split_bible(text: str) -> tuple[str, str]:
    """Split the `# BIBLE` block (up to the first `# TẬP`) out of the Series TXT."""
    script: list[str] = []
    bible: list[str] = []
    in_bible = False
    for line in text.splitlines():
        match = _BIBLE_RE.match(line)
        if match:
            in_bible = True
            if match.group(1).strip():
                bible.append(match.group(1).strip())
            continue
        if in_bible and _EPISODE_RE.match(line):
            in_bible = False
        (bible if in_bible else script).append(line)
    return "\n".join(script).strip(), "\n".join(bible).strip()


def _prompt(topic: str, num_episodes: int | None = None, scene_duration: int | None = None) -> str:
    ep_constraint = (
        f"Create exactly {num_episodes} episode{'s' if num_episodes != 1 else ''}."
        if num_episodes else "Choose 1 to 5 episodes, sized to the topic."
    )
    dur_constraint = (
        f"Every scene must last exactly {scene_duration} seconds; timecodes span {scene_duration} seconds each."
        if scene_duration else "Each scene lasts 4, 6, 8 or 10 seconds, chosen to fit its action."
    )
    return f"""You are a professional visual-series planner for AI video generation. Plan a complete Series from this topic:

{topic.strip()}

Write every title, bible line and scene prompt in the same language as the topic. Return plain TXT only, with no markdown, code fences or commentary, in exactly this syntax:
# SERIES: concise series title
# BIBLE
Character: name — fixed face, body, clothes and colours
Setting: fixed locations and props
Style: art style, lighting, camera language
# TẬP 01 — episode title
001_[00.00_00.00-00.00_08.00] visual scene prompt
002_[00.00_08.00-00.00_14.00] visual scene prompt

{ep_constraint} Use 3 to 8 scenes per episode. Scene numbering restarts at 001 in every episode. {dur_constraint} Timecodes (HH.MM_SS.cc) are continuous inside each episode. Every scene prompt must include:
- START STATE: where characters/camera begin (matches the previous END when continuing)
- ACTION: what happens in this shot only
- END STATE: freeze-frame pose/set for the next shot to continue from
Keep the bible's character appearance, clothes, props, setting and art style identical in every scene. Do not restart the plot each scene."""


def draft_series(*, provider: str, model: str, topic: str, num_episodes: int | None = None, scene_duration: int | None = None) -> dict[str, str]:
    """Return `{"text", "bible"}` drafted by any Chat provider/model."""
    from pipeline.automation.service import service as automation

    if not topic.strip():
        raise ValueError("SERIES_AI_TOPIC_REQUIRED")
    raw = automation._request_ephemeral_chat(
        _prompt(topic, num_episodes=num_episodes, scene_duration=scene_duration),
        {"textProvider": provider, "textModel": model}, "Series Draft",
    )
    text, bible = split_bible(_clean_text(raw))
    if not text:
        raise RuntimeError("SERIES_AI_EMPTY_RESPONSE")
    return {"text": text, "bible": bible}
