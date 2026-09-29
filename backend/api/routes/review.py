"""Review Phim — normal generate uses the same pipeline as batch."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from pipeline.queue.engine import enqueue, list_jobs

router = APIRouter()

DEFAULT_REVIEW_VOICE = "cc:BV074_streaming:7102355709945188865"


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: str
    durationSec: float = 60
    style: str = "normal"
    reviewMode: Literal["llm", "cloud", "translate"] = "llm"
    reviewModel: str = "auto"
    reviewCloudModel: str = "gemini-2.5-flash"
    reviewProvider: Literal["gemini", "grok", "openai", "deepseek", "openrouter", "groq", "nvidia", "mistral"] = "gemini"
    recognitionEngine: Literal["whisper", "capcut"] = "whisper"
    sourceLang: str = "auto"
    language: str = "vi"
    voice: str = DEFAULT_REVIEW_VOICE
    spoiler: str = "none"
    ratio: str = "16:9"
    originalAudioPct: float = 18
    subtitle: bool = True
    quality: str = "1080p"
    outputDir: str = ""
    headless: bool = True
    naming: str = "{name}_review"


@router.post("/api/review/generate")
def api_review_generate(body: ReviewIn):
    if not (body.source or "").strip():
        raise HTTPException(422, "Thiếu file phim")
    settings: dict[str, Any] = body.model_dump()
    jobs = enqueue("review", [body.source], settings, recursive=False)
    return {"ok": True, "job": jobs[0] if jobs else None}


@router.get("/api/review/status")
def api_review_status(jobId: str = ""):
    snap = list_jobs()
    if not jobId:
        return snap
    job = next((j for j in snap.get("jobs") or [] if j.get("id") == jobId), None)
    if not job:
        raise HTTPException(404)
    return job


@router.get("/api/review/diagnostics")
def api_review_diagnostics():
    from pipeline.gpu.manager import diagnostics
    from pipeline.review.llm import list_ollama_models, pick_llm
    from pipeline.tts import engines_status

    models = list_ollama_models()
    return {
        **diagnostics(),
        "tts": engines_status(),
        "ollamaModels": models,
        "llm": pick_llm(models),
        "vision": pick_llm(models, prefer_vision=True),
        "asr": "faster-whisper base",
        "embedding": "lexical (no extra model)",
    }


class ReviewCacheIn(BaseModel):
    source: str


@router.post("/api/review/clear-cache")
def api_review_clear_cache(body: ReviewCacheIn):
    from pathlib import Path

    from pipeline.review.cache import clear_movie_cache

    src = Path(str(body.source or "").strip())
    if not str(src) or str(src).startswith("http"):
        raise HTTPException(422, "Cần file video trên máy để xóa cache")
    try:
        return clear_movie_cache(src)
    except FileNotFoundError:
        raise HTTPException(422, "Không tìm thấy file video")
    except OSError as exc:
        raise HTTPException(500, str(exc)) from exc
