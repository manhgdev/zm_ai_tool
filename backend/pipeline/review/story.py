"""Hierarchical scene → block → chapter → story graph. Never one giant prompt."""
from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from pipeline.mt.text import _lang_name
from pipeline.core.jobs import check_cancel
from pipeline.review.llm import generate_json

BLOCK = 75
CHAPTER = 4
# Gemini's quota is shared by all requests on the API key.  Story blocks are
# independent, but sending several large JSON prompts at once makes a single
# temporary Gemini transport/rate-limit failure abort the whole Review.
CLOUD_STORY_WORKERS = 3
GEMINI_STORY_WORKERS = 1
TIMELINE_BLOCKS = 14
TIMELINE_MIN_SEC = 45.0
TIMELINE_MAX_SEC = 130.0


def story_workers(model: str | None) -> int:
    """Ceiling for story LLM concurrency. Local models may elastic-scale up to this."""
    name = str(model or "").lower()
    if name.startswith("cloud:gemini:"):
        return GEMINI_STORY_WORKERS
    # Groq free/developer limits are shared across concurrent Review blocks;
    # serialize these short calls so one transient 429/transport failure does
    # not abort the entire story graph.
    if name.startswith("cloud:groq:"):
        return 1
    if name.startswith("cloud:"):
        return CLOUD_STORY_WORKERS
    match = re.search(r"(\d+(?:\.\d+)?)b\b", name)
    if not match:
        return 6
    billions = float(match.group(1))
    # Caps only — local Ollama uses adaptive idle scaling beneath these.
    if billions <= 4:
        return 8
    if billions <= 9:
        return 8
    if billions <= 14:
        return 4
    return 2


def story_pool_fixed(model: str | None) -> bool:
    """Cloud stays fixed (rate limits); local Ollama elastically follows machine idle."""
    return str(model or "").lower().startswith("cloud:")


def build_story(
    visuals: list[dict[str, Any]],
    *,
    transcript: list[dict[str, Any]] | None = None,
    language: str = "vi",
    model: str | None = None,
    on_progress: Callable[[str, int, int, int], None] | None = None,
    title: str = "",
    job_id: str | None = None,
) -> dict[str, Any]:
    if not visuals:
        return {
            "blocks": [],
            "chapters": [],
            "movie_context": {},
            "story_graph": {},
        }
    timeline_blocks = _timeline_blocks(transcript or [], visuals)
    chunk_size = max(60, len(visuals) // 3 + 1)
    scene_blocks = [visuals[i : i + chunk_size] for i in range(0, len(visuals), chunk_size)] or [visuals]
    blocks: list[Any] = timeline_blocks or scene_blocks
    cap = min(len(blocks), story_workers(model))
    fixed = story_pool_fixed(model)
    block_summaries = _parallel_summaries(
        [chunk for chunk in blocks if chunk],
        (
            lambda chunk: _summarize_timeline_block(chunk, language, model=model, title=title, job_id=job_id)
            if timeline_blocks
            else _summarize_block(chunk, language, model=model, title=title, job_id=job_id)
        ),
        stage="blocks",
        on_progress=on_progress,
        workers=cap,
        fixed=fixed,
        cancel_check=(lambda: check_cancel(job_id)) if job_id else None,
    )
    chapters = []
    for i, b in enumerate(block_summaries):
        chapters.append({
            "index": i,
            "scene_ids": b.get("scene_ids") or [],
            "start": b.get("start") or 0,
            "end": b.get("end") or 0,
            "summary": b.get("summary") or "",
            "characters": b.get("characters") or [],
            "events": b.get("events") or [],
            "themes": [],
        })
    check_cancel(job_id)
    context = _compile_movie_context(chapters, language, model=model, title=title, job_id=job_id)
    graph = _story_graph(block_summaries, chapters, visuals, context)
    return {
        "blocks": block_summaries,
        "chapters": chapters,
        "movie_context": context,
        "story_graph": graph,
    }


def _timeline_blocks(
    transcript: list[dict[str, Any]], visuals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group timed captions into chronological story beats.

    Captions/ASR carry the actual plot and their timestamps are the reliable
    link back to source footage.  Do not use keyframes as a substitute for
    dialogue evidence: a text-led Review needs readable, causal story beats.
    """
    rows = sorted(
        [
            row for row in transcript
            if str(row.get("text") or "").strip()
            and float(row.get("end") or row.get("start") or 0) >= float(row.get("start") or 0)
        ],
        key=lambda row: float(row.get("start") or 0),
    )
    if not rows:
        return []
    start_all = float(rows[0].get("start") or 0)
    end_all = max(float(row.get("end") or row.get("start") or start_all) for row in rows)
    target_sec = max(TIMELINE_MIN_SEC, min(TIMELINE_MAX_SEC, (end_all - start_all) / TIMELINE_BLOCKS))
    blocks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        start = float(current[0].get("start") or 0)
        end = max(float(row.get("end") or row.get("start") or start) for row in current)
        scene_ids = [
            int(scene.get("scene_id") or 0)
            for scene in visuals
            if float(scene.get("end") or 0) >= start and float(scene.get("start") or 0) <= end
        ]
        blocks.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "scene_ids": scene_ids,
            "text": " ".join(str(row.get("text") or "").strip() for row in current),
        })
        current, current_chars = [], 0

    for row in rows:
        text = str(row.get("text") or "").strip()
        row_end = float(row.get("end") or row.get("start") or 0)
        if current:
            block_start = float(current[0].get("start") or 0)
            should_split = (
                row_end - block_start >= target_sec and current_chars >= 260
            ) or current_chars >= 2_800
            if should_split:
                flush()
        current.append(row)
        current_chars += len(text) + 1
    flush()
    # Tiny tail beats lack enough context to summarize well; merge them into
    # the prior beat while preserving chronology.
    if len(blocks) > 1 and len(str(blocks[-1].get("text") or "")) < 120:
        tail = blocks.pop()
        prev = blocks[-1]
        prev["end"] = tail["end"]
        prev["text"] = f"{prev['text']} {tail['text']}".strip()
        prev["scene_ids"] = list(dict.fromkeys([*(prev.get("scene_ids") or []), *(tail.get("scene_ids") or [])]))
    return blocks


def _parallel_summaries(
    items: list[Any],
    summarize: Callable[[Any], dict[str, Any]],
    *,
    stage: str,
    on_progress: Callable[[str, int, int, int], None] | None,
    workers: int,
    fixed: bool = False,
    cancel_check: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Run summaries with elastic pool when local; cloud keeps a fixed cap."""
    if not items:
        return []
    from pipeline.core.resources import run_with_adaptive_workers

    # Local Ollama: kind=gpu so idle CPU/GPU/RAM can raise concurrency under `workers`.
    # Cloud: fixed network pool (rate limits / key rotation handle pressure).
    return run_with_adaptive_workers(
        items,
        summarize,
        kind="network" if fixed else "gpu",
        requested=workers if fixed else 0,
        cap=workers,
        thread_name_prefix=f"review-{stage}",
        on_progress=(
            (lambda done, total, workers: on_progress(stage, done, total, workers))
            if on_progress
            else None
        ),
        cancel_check=cancel_check,
    )


def _safe_float(val: Any, default: float = 0.4) -> float:
    try:
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val or "").strip()
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        return float(m.group(1)) if m else default
    except Exception:
        return default


def _summarize_block(
    scenes: list[dict[str, Any]], language: str, *, model: str | None = None, title: str = "", job_id: str | None = None,
) -> dict[str, Any]:
    ids = [s["scene_id"] for s in scenes]
    # Transcript is first-class evidence.  A scene description is only a
    # fallback for silent footage and must not override spoken facts.
    blob = " | ".join(f"#{s['scene_id']} {s.get('transcript') or s.get('description') or ''}" for s in scenes)[:2000]
    name = _lang_name(language)
    parsed = generate_json(
        f"Video title: {title or 'Unknown'}. "
        f"Summarize these consecutive movie scenes in {name}. "
        f"Write summary and names in {name} only; source dialogue may be another language. "
        "JSON keys: summary, characters, events, importance (0-1). Keep scene_ids.\n"
        "GROUNDING: Treat the supplied scene text as the complete evidence. Do not add objects, food, weapons,"
        " locations, actions, characters, or plot events that are not explicitly supported by it. If evidence is"
        " sparse, use a neutral summary instead of guessing.\n" + blob,
        model=model,
        job_id=job_id,
    )
    if not isinstance(parsed, dict):
        parsed = {
            "summary": " ".join((s.get("description") or "")[:80] for s in scenes[:6]),
            "characters": [],
            "events": [],
            "importance": max((s.get("plot_score") or 0) for s in scenes),
        }
    return {
        "scene_ids": ids,
        "start": scenes[0]["start"],
        "end": scenes[-1]["end"],
        "summary": str(parsed.get("summary") or "")[:500],
        "characters": list(parsed.get("characters") or []),
        "events": list(parsed.get("events") or []),
        "importance": _safe_float(parsed.get("importance"), 0.4),
    }


def _summarize_timeline_block(
    block: dict[str, Any], language: str, *, model: str | None = None, title: str = "", job_id: str | None = None,
) -> dict[str, Any]:
    """Turn one contiguous caption range into concrete review evidence."""
    start = float(block.get("start") or 0)
    end = float(block.get("end") or start)
    text = str(block.get("text") or "").strip()
    name = _lang_name(language)
    parsed = generate_json(
        f"Video title: {title or 'Unknown'}. You are preparing evidence for a compelling YouTube movie review in {name}.\n"
        f"This is one chronological caption range ({start:.1f}s–{end:.1f}s).\n"
        "Return JSON keys: summary, characters, events, importance (0-1).\n"
        "Write a concrete causal plot beat, not a generic genre description or a line-by-line transcript. "
        "State only facts supported by the captions: what changes, who reacts, what conflict/stake appears, and why it matters. "
        "If a name/action is unclear, keep it neutral rather than inventing it. Keep summary concise but specific.\n"
        f"CAPTIONS:\n{text[:3200]}",
        model=model,
        job_id=job_id,
    )
    if not isinstance(parsed, dict):
        parsed = {
            "summary": text[:700],
            "characters": [],
            "events": [],
            "importance": 0.55 if len(text) > 240 else 0.35,
        }
    return {
        "scene_ids": list(block.get("scene_ids") or []),
        "start": start,
        "end": end,
        "summary": str(parsed.get("summary") or text[:700])[:700],
        "characters": list(parsed.get("characters") or []),
        "events": list(parsed.get("events") or []),
        "importance": _safe_float(parsed.get("importance"), 0.4),
    }


def _summarize_chapter(
    blocks: list[dict[str, Any]], language: str, *, index: int, model: str | None = None, title: str = "",
) -> dict[str, Any]:
    blob = " ".join(b.get("summary") or "" for b in blocks)[:1500]
    name = _lang_name(language)
    parsed = generate_json(
        f"Video title: {title or 'Unknown'}. "
        f"Summarize this sequence of scene blocks into a single chapter summary in {name}. "
        "Combine only the supplied events. Do not add objects, locations, characters, or actions absent from the"
        " evidence; describe uncertain details neutrally. JSON keys: title, summary, characters, importance (0-1).\n" + blob,
        model=model,
    )
    if not isinstance(parsed, dict):
        parsed = {"summary": blob[:400], "characters": [], "events": [], "themes": []}
    scene_ids: list[int] = []
    for b in blocks:
        scene_ids.extend(b.get("scene_ids") or [])
    return {
        "index": index,
        "scene_ids": scene_ids,
        "start": blocks[0]["start"] if blocks else 0,
        "end": blocks[-1]["end"] if blocks else 0,
        "summary": str(parsed.get("summary") or "")[:1000],
        "characters": list(parsed.get("characters") or []),
        "events": list(parsed.get("events") or []),
        "themes": list(parsed.get("themes") or []),
    }


def _compile_movie_context(
    chapters: list[dict[str, Any]], language: str, *, model: str | None = None, title: str = "", job_id: str | None = None,
) -> dict[str, Any]:
    blob = "\n".join(f"Ch{c['index']}: {c.get('summary')}" for c in chapters)[:4000]
    name = _lang_name(language)
    parsed = generate_json(
        f"Video title: {title or 'Unknown'}. "
        f"Analyze these chronological chapter summaries and compile a global movie context in {name}.\n"
        "Use only the supplied summaries; never invent concrete props, locations, actions, or people. "
        "JSON format: {logline, themes:[], tone:[], spoiler_outline, characters:[{name, role, description}]}.\n" + blob,
        model=model,
        job_id=job_id,
    )
    if not isinstance(parsed, dict):
        parsed = {
            "logline": "",
            "themes": [],
            "tone": "dramatic",
            "spoiler_outline": "",
            "characters": [],
        }
    return parsed


def _story_graph(
    blocks: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    visuals: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    ranked = sorted(visuals, key=lambda s: float(s.get("plot_score") or 0), reverse=True)
    highlights = ranked[:12]
    climax = ranked[:3]
    events = []
    for i, block in enumerate(sorted(blocks, key=lambda item: float(item.get("start") or 0))):
        events.append({
            "event_id": f"evt_{i:03d}",
            "summary": block.get("summary") or "",
            "characters": block.get("characters") or [],
            "scene_ids": block.get("scene_ids") or [],
            "start": block.get("start") or 0,
            "end": block.get("end") or 0,
            "importance": _safe_float(block.get("importance"), 0.0),
            "spoiler_level": 0,
        })
    chars: list[str] = []
    for c in chapters:
        for name in c.get("characters") or []:
            if name and name not in chars:
                chars.append(str(name))
    return {
        "characters": chars or list(context.get("characters") or []),
        "relationships": [],
        "acts": [{"index": c["index"], "summary": c.get("summary"), "scene_ids": c.get("scene_ids")} for c in chapters],
        "events": events,
        "themes": list(context.get("themes") or []),
        "conflicts": [],
        "highlights": [h["scene_id"] for h in highlights],
        "climax": [h["scene_id"] for h in climax],
        "ending": [visuals[-1]["scene_id"]] if visuals else [],
    }
