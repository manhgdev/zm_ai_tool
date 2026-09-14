"""Stable user-visible output folders, grouped by application tab."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


APP_OUTPUT_ROOT_NAME = "ZM_AIO_TOOL"
_WRITABLE_OUTPUT_ROOTS: set[str] = set()
_OUTPUT_SUBFOLDERS: dict[str, tuple[str, ...]] = {
    "video-clone": ("clone",),
    "clone": ("clone",),
    "film": ("review",),
    "review": ("review",),
    "flow": ("flow",),
    "flow-video": ("flow", "video"),
    "flow-image": ("flow", "image"),
    "download-video": ("download-video",),
    "tts": ("text-to-speech",),
    "subtitle-export": ("subtitles", "export"),
    "subtitle-image": ("subtitles", "image-video"),
    "drawing": ("drawing",),
    "cleaner": ("cleaner",),
    "batch": ("batch",),
    "automation": ("automation",),
}


def safe_output_part(value: object, fallback: str = "output", *, max_length: int = 96) -> str:
    """Return one filesystem-safe name component for generated outputs."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or fallback)).strip(" .-")
    return (safe or fallback)[:max_length]


def nested_output_folder(
    root: Path,
    group: object,
    item_id: object,
    *,
    create: bool = True,
) -> Path:
    """Build ``root/group/item-id`` consistently for generated artifacts."""
    folder = root / safe_output_part(group, "results") / safe_output_part(item_id, "item")
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def item_output_folder(root: Path, item_id: object, *, create: bool = True) -> Path:
    """Build ``root/item-id`` for an explicitly selected output root."""
    folder = root / safe_output_part(item_id, "item")
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_writable_output_root(folder: Path) -> Path:
    """Create an output root and verify real write access before it is selected."""
    folder = folder.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(os.path.abspath(str(folder)))
    if key not in _WRITABLE_OUTPUT_ROOTS:
        with tempfile.NamedTemporaryFile(prefix=".zmaio-write-", dir=folder):
            pass
        _WRITABLE_OUTPUT_ROOTS.add(key)
    return folder


def app_output_root() -> Path:
    """User-visible output root, resolved in priority order:

    1. outputRoot saved in ui_preferences.json (user picked once via Settings)
    2. ZM_AI_TOOL_OUTPUT_ROOT env var (set by launcher per platform:
       Windows = portable state root/output, macOS = ~/Downloads/ZM_AIO_TOOL)
    3. Hard fallback: ~/Downloads/ZM_AIO_TOOL
    """
    from .ui_preferences import load_output_root  # ponytail: lazy to avoid circular at import
    saved = load_output_root()
    if saved:
        try:
            return ensure_writable_output_root(saved)
        except OSError:
            pass
    env = (os.environ.get("ZM_AI_TOOL_OUTPUT_ROOT") or "").strip()
    if env:
        try:
            return ensure_writable_output_root(Path(env))
        except OSError:
            pass
    folder = Path.home() / "Downloads" / APP_OUTPUT_ROOT_NAME
    return ensure_writable_output_root(folder)


def downloads_folder(tab: str) -> Path:
    """Return one feature subfolder inside the shared APP output root."""
    key = str(tab or "clone").strip().lower()
    parts = _OUTPUT_SUBFOLDERS.get(key, (safe_output_part(key, "clone"),))
    folder = app_output_root().joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def selected_or_default(tab: str, selected: str = "") -> Path:
    """Honor an explicit desktop choice; otherwise use the shared Downloads tree."""
    raw = selected.strip()
    folder = Path(raw).expanduser() if raw else downloads_folder(tab)
    if raw and not folder.is_absolute():
        safe_parts = [safe_output_part(part) for part in folder.parts if part not in {"", ".", ".."}]
        folder = downloads_folder(tab).joinpath(*safe_parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
