"""Locate optional command-line tools from shells, GUI apps, and bundles."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _bundled_candidates(name: str) -> list[Path]:
    ext = ".exe" if sys.platform == "win32" else ""
    filename = f"{name}{ext}"
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None) or os.environ.get("ZM_AI_TOOL_MEIPASS")
    if meipass:
        root = Path(meipass)
        roots.extend((root, root.parent / "Frameworks"))
    executable = Path(sys.executable).resolve()
    roots.extend((executable.parent, executable.parent.parent / "Frameworks"))
    return [root / filename for root in roots]


def _ytdlp_candidates() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [
            Path("/opt/homebrew/bin/yt-dlp"),
            Path("/usr/local/bin/yt-dlp"),
            home / ".pyenv/shims/yt-dlp",
            home / ".local/bin/yt-dlp",
            *sorted((home / "Library/Python").glob("*/bin/yt-dlp"), reverse=True),
        ]
    if sys.platform == "win32":
        return [
            home / "AppData/Roaming/Python/Scripts/yt-dlp.exe",
            home / "AppData/Local/Programs/Python/Scripts/yt-dlp.exe",
            home / "scoop/shims/yt-dlp.exe",
        ]
    return [
        Path("/usr/local/bin/yt-dlp"),
        Path("/usr/bin/yt-dlp"),
        Path("/snap/bin/yt-dlp"),
        home / ".local/bin/yt-dlp",
        home / ".pyenv/shims/yt-dlp",
    ]


def find_ytdlp() -> str | None:
    """Return an executable yt-dlp path even with Finder's minimal PATH."""
    found = shutil.which("yt-dlp")
    if found:
        return found.strip()
    for path in [*_bundled_candidates("yt-dlp"), *_ytdlp_candidates()]:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def ytdlp_command() -> list[str] | None:
    """Return a runnable yt-dlp command, including the copy embedded in APP."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--yt-dlp-cli"]
    found = find_ytdlp()
    return [found] if found else None
