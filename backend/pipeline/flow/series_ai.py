"""AI-assisted Series drafting from a single topic.

This module deliberately creates only a reviewable draft.  Importing it into a
Series remains a separate user action, so an AI response can never silently
create or overwrite a project.
"""
from __future__ import annotations

import re

_BIBLE_RE = re.compile(r"^\s*#\s*BIBLE\s*:?\s*(.*)$", re.I)
_ANCHOR_RE = re.compile(r"^\s*#\s*ANCHOR\s*PROMPT\s*:?\s*(.*)$", re.I)
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


def split_anchor(text: str) -> tuple[str, str]:
    """Extract the optional `# ANCHOR PROMPT` block; return (remaining_text, anchor_prompt)."""
    remaining: list[str] = []
    anchor: list[str] = []
    in_anchor = False
    for line in text.splitlines():
        if _ANCHOR_RE.match(line):
            in_anchor = True
            tail = _ANCHOR_RE.match(line).group(1).strip()  # type: ignore[union-attr]
            if tail:
                anchor.append(tail)
            continue
        # Anchor block ends at the next # header
        if in_anchor and re.match(r"^\s*#", line):
            in_anchor = False
        (anchor if in_anchor else remaining).append(line)
    return "\n".join(remaining).strip(), "\n".join(anchor).strip()


def _prompt(
    topic: str,
    num_episodes: int | None = None,
    episode_duration: int | None = None,
    scene_duration: int | None = None,
    scene_continuity: bool = False,
) -> str:
    ep_constraint = (
        f"Create exactly {num_episodes} episode{'s' if num_episodes != 1 else ''}."
        if num_episodes else "Choose 1 to 5 episodes, sized to the topic."
    )
    dur_constraint = (
        f"Every scene must last exactly {scene_duration} seconds; timecodes span {scene_duration} seconds each."
        if scene_duration else "Each scene lasts 4, 6, 8 or 10 seconds, chosen to fit its action."
    )
    episode_dur_constraint = (
        f"Each episode should run for approximately {episode_duration} seconds in total."
        if episode_duration else "Choose an appropriate total duration for each episode."
    )
    scene_count_constraint = (
        "Use enough scenes to fill that episode duration; do not cap the episode at 8 scenes."
        if episode_duration else "Use 3 to 8 scenes per episode."
    )
    continuity_rule = (
        "CROSS-EPISODE CONTINUITY (mandatory): The END STATE of the last scene of every episode must be "
        "copy-pasted verbatim as the START STATE of the first scene of the next episode. "
        "Characters, camera angle, props, lighting and freeze-frame pose must be identical so the two "
        "episodes cut together seamlessly. Never reset the scene between episodes."
        if scene_continuity else ""
    )
    return f"""You are a professional visual-series planner for AI video generation. Plan a complete Series from this topic:

{topic.strip()}

Write every title, bible line and scene prompt in the same language as the topic. Return plain TXT only, with no markdown, code fences or commentary, in exactly this syntax:
# SERIES: concise series title
# BIBLE
Character: name — fixed face, body, clothes and colours
Setting: fixed locations and props
Style: art style, lighting, camera language
# ANCHOR PROMPT
A single concise image-generation prompt (1-3 sentences) describing the main character's exact visual appearance — face, body, clothing, colours, art style — optimised for an image model. No plot, no action, just appearance.
# TẬP 01 — episode title
001_[00.00_00.00-00.00_08.00] visual scene prompt
002_[00.00_08.00-00.00_14.00] visual scene prompt

{ep_constraint} {scene_count_constraint} Scene numbering restarts at 001 in every episode. {episode_dur_constraint} {dur_constraint} Timecodes (HH.MM_SS.cc) are continuous inside each episode and must end near the requested episode duration. Every scene prompt must include:
- START STATE: where characters/camera begin (matches the previous END when continuing)
- ACTION: what happens in this shot only
- END STATE: freeze-frame pose/set for the next shot to continue from
Keep the bible's character appearance, clothes, props, setting and art style identical in every scene. Do not restart the plot each scene.
{continuity_rule}"""


def draft_series(
    *, provider: str, model: str, topic: str, num_episodes: int | None = None,
    episode_duration: int | None = None, scene_duration: int | None = None,
    scene_continuity: bool = False,
) -> dict[str, str]:
    """Return `{"text", "bible", "anchor_prompt"}` drafted by any Chat provider/model."""
    from pipeline.automation.service import service as automation

    if not topic.strip():
        raise ValueError("SERIES_AI_TOPIC_REQUIRED")
    raw = automation._request_ephemeral_chat(
        _prompt(
            topic,
            num_episodes=num_episodes,
            episode_duration=episode_duration,
            scene_duration=scene_duration,
            scene_continuity=scene_continuity,
        ),
        {"textProvider": provider, "textModel": model}, "Series Draft",
    )
    cleaned = _clean_text(raw)
    # Extract anchor prompt before splitting bible
    cleaned, anchor_prompt = split_anchor(cleaned)
    text, bible = split_bible(cleaned)
    if not text:
        raise RuntimeError("SERIES_AI_EMPTY_RESPONSE")
    return {"text": text, "bible": bible, "anchor_prompt": anchor_prompt}


def _prompt_more(
    bible: str,
    last_scene_prompt: str,
    next_episode_index: int,
    num_episodes: int = 1,
    episode_duration: int | None = None,
    scene_duration: int | None = None,
    scene_continuity: bool = False,
) -> str:
    ep_count = f"exactly {num_episodes} episode{'s' if num_episodes != 1 else ''}"
    dur_constraint = (
        f"Every scene must last exactly {scene_duration} seconds; timecodes span {scene_duration} seconds each."
        if scene_duration else "Each scene lasts 4, 6, 8 or 10 seconds, chosen to fit its action."
    )
    episode_dur_constraint = (
        f"Each episode should run for approximately {episode_duration} seconds in total."
        if episode_duration else "Choose an appropriate total duration for each episode."
    )
    scene_count_constraint = (
        "Use enough scenes to fill that episode duration; do not cap the episode at 8 scenes."
        if episode_duration else "Use 3 to 8 scenes per episode."
    )
    continuity_rule = (
        "CROSS-EPISODE CONTINUITY (mandatory): The END STATE of the last scene of every episode must be "
        "copy-pasted verbatim as the START STATE of the first scene of the next episode. "
        "Characters, camera angle, props, lighting and freeze-frame pose must be identical so the two "
        "episodes cut together seamlessly. Never reset the scene between episodes."
        if scene_continuity else ""
    )
    start_idx = next_episode_index
    return f"""You are a professional visual-series planner for AI video generation. You are CONTINUING an existing series.

EXISTING SERIES BIBLE (character appearance, setting and style — must stay IDENTICAL in every new scene):
{bible.strip()}

LAST SCENE of episode {next_episode_index - 1} (your first new scene must continue from this END STATE):
{last_scene_prompt.strip()}

Write {ep_count} that continue the story from that exact END STATE. The episodes must be numbered starting at {start_idx:02d}.

Write every title and scene prompt in the same language as the bible. Return plain TXT only — no markdown, no code fences, no commentary — in EXACTLY this syntax:
# TẬP {start_idx:02d} — episode title
001_[HH.MM_SS.cc-HH.MM_SS.cc] visual scene prompt with START STATE / ACTION / END STATE
002_[HH.MM_SS.cc-HH.MM_SS.cc] visual scene prompt with START STATE / ACTION / END STATE

{scene_count_constraint} Scene numbering restarts at 001 in every episode. {episode_dur_constraint} {dur_constraint} Timecodes (HH.MM_SS.cc) are continuous inside each episode. Every scene prompt must include:
- START STATE: where characters/camera begin (for the first scene of episode {start_idx:02d} this MUST match the END STATE above verbatim)
- ACTION: what happens in this shot only
- END STATE: freeze-frame pose/set for the next shot to continue from
Keep the bible's character appearance, clothes, props, setting and art style identical in every scene. Do NOT include a # SERIES or # BIBLE header — only the # TẬP lines.
{continuity_rule}"""


def draft_more_episodes(
    *, provider: str, model: str,
    bible: str, last_scene_prompt: str,
    next_episode_index: int,
    num_episodes: int = 1,
    episode_duration: int | None = None,
    scene_duration: int | None = None,
    scene_continuity: bool = False,
) -> str:
    """Return raw TXT containing only the new # TẬP blocks (no SERIES/BIBLE header)."""
    from pipeline.automation.service import service as automation

    if not bible.strip() and not last_scene_prompt.strip():
        raise ValueError("SERIES_AI_CONTEXT_REQUIRED")
    raw = automation._request_ephemeral_chat(
        _prompt_more(
            bible=bible,
            last_scene_prompt=last_scene_prompt,
            next_episode_index=next_episode_index,
            num_episodes=num_episodes,
            episode_duration=episode_duration,
            scene_duration=scene_duration,
            scene_continuity=scene_continuity,
        ),
        {"textProvider": provider, "textModel": model}, "Series Extend",
    )
    text = _clean_text(raw)
    # Strip any accidental SERIES/BIBLE header the model may have added
    lines = [ln for ln in text.splitlines() if not re.match(r"^\s*#\s*(SERIES|BIBLE)\s*[:\-]?", ln, re.I)]
    return "\n".join(lines).strip()

