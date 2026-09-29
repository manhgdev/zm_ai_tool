"""Review cache keyed by light fingerprint — not a full SHA of 10h files."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pipeline.clone_run.open_source import light_fingerprint
from pipeline.core.config import DATA

STAGES = (
    "metadata",
    "scenes",
    "transcript",
    "vision",
    "segments",
    "chapters",
    "story_graph",
    "script",
    "tts",
    "matching",
    "timeline",
    "render",
)

# regenerate invalidates from this stage onward
INVALIDATE_FROM = {
    'cloneCacheToken': 'tts',
    "durationSec": "script",
    "reviewPlanVersion": "script",
    "reviewModel": "story_graph",
    "reviewMode": "story_graph",
    "reviewProvider": "story_graph",
    "reviewMatchVersion": "matching",
    "style": "script",
    "language": "script",
    "sourceLang": "transcript",
    "spoiler": "script",
    "voice": "tts",
    "ratio": "matching",
    "buildMode": "matching",
    "cutMode": "matching",
    "keepSec": "matching",
    "skipSec": "matching",
    "chunkMinutes": "script",
    "narration": "script",
    "notes": "script",
    "genre": "script",
    "scriptStyle": "script",
    "pausePace": "matching",
    "originalAudioPct": "matching",
    "subtitle": "render",
    "captionMode": "render",
    "quality": "render",
}


def movie_root(source: Path) -> Path:
    root = DATA / "review_cache" / light_fingerprint(source)
    root.mkdir(parents=True, exist_ok=True)
    (root / "visual_analysis").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    return root


def clear_movie_cache(source: Path) -> dict[str, Any]:
    """Delete review_cache for this file so the next run rebuilds transcript/story."""
    if not source.is_file():
        raise FileNotFoundError(str(source))
    root = DATA / "review_cache" / light_fingerprint(source)
    if not root.exists():
        return {"ok": True, "cleared": False, "path": str(root)}
    shutil.rmtree(root)
    return {"ok": True, "cleared": True, "path": str(root)}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run_dir(root: Path, run_id: str) -> Path:
    d = root / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d
