"""Chunked ASR with overlap and idempotent progress callbacks."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable


def hybrid_asr(audio: Path, engine: str, language: str, *, project_id: str | None = None, chunk_sec: float = 45.0, overlap_sec: float = 1.0, on_chunk: Callable[[int, list[dict[str, Any]]], None] | None = None) -> list[dict[str, Any]]:
    import soundfile as sf
    from pipeline.asr.whisper import asr_whisper

    samples, rate = sf.read(str(audio), dtype="float32", always_2d=True)
    step = max(1, int(chunk_sec * rate))
    overlap = max(0, int(overlap_sec * rate))
    out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="zm_ai_tool-hybrid-asr-") as raw:
        root = Path(raw)
        for index, center_start in enumerate(range(0, len(samples), step)):
            start = max(0, center_start - overlap)
            end = min(len(samples), center_start + step + overlap)
            part = root / f"{index}.wav"
            sf.write(part, samples[start:end], rate)
            rows = asr_whisper(part, language, workers=0, project_id=project_id)
            offset = start / rate
            mapped = [{**row, "start": offset + float(row.get("start") or 0), "end": offset + float(row.get("end") or 0)} for row in rows]
            # Drop overlap duplicates by text + nearly identical start.
            fresh = [row for row in mapped if not any(str(row.get("source")) == str(old.get("source")) and abs(float(row["start"]) - float(old.get("start") or 0)) < overlap_sec + .15 for old in out)]
            out.extend(fresh)
            if on_chunk:
                on_chunk(index, fresh)
    return sorted(out, key=lambda row: (float(row.get("start") or 0), float(row.get("end") or 0)))
