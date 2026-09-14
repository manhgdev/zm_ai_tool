"""Periodic cleanup for generated public files."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import PUBLIC_DATA

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


def _retention_days() -> int:
    raw = os.environ.get("ZM_AI_TOOL_PUBLIC_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        days = int(raw)
    except ValueError:
        days = 0
    if days < 1:
        logger.warning(
            "Invalid ZM_AI_TOOL_PUBLIC_RETENTION_DAYS=%r; using %d",
            raw,
            DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return days


def _is_expired(mtime: float, cutoff: float) -> bool:
    return mtime < cutoff


# Chỉ dọn file SINH RA được (tái tạo bằng cách chạy lại). Video nguồn, meta.json,
# giọng clone, TTS đã tổng hợp… là dữ liệu người dùng — không bao giờ tự xóa.
_PURGEABLE_DIR_NAMES = {"cache", "tmp", "temp", "frames", "preview"}
_PURGEABLE_SUFFIXES = {".log"}


def _is_purgeable(path: Path, root: Path) -> bool:
    """True khi file nằm trong thư mục cache/tmp (tái tạo được) hoặc là log."""
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return False
    if path.suffix.lower() in _PURGEABLE_SUFFIXES:
        return True
    return any(part.lower() in _PURGEABLE_DIR_NAMES for part in rel_parts[:-1])


def cleanup_public_files(
    root: Path = PUBLIC_DATA,
    *,
    retention_days: int | None = None,
    now: float | None = None,
) -> tuple[int, int]:
    """Delete expired files recursively, leaving the directory structure intact."""
    days = retention_days if retention_days is not None else _retention_days()
    cutoff = (time.time() if now is None else now) - days * 24 * 60 * 60
    deleted = skipped = 0

    if not root.is_dir():
        logger.info(
            "Public cleanup: deleted=0 skipped=0 retention_days=%d (directory missing)",
            days,
        )
        return deleted, skipped

    def count_walk_error(_error: OSError) -> None:
        nonlocal skipped
        skipped += 1

    for current, dirs, files in os.walk(root, followlinks=False, onerror=count_walk_error):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
        for name in files:
            path = current_path / name
            try:
                if (
                    path.is_symlink()
                    or not _is_purgeable(path, root)
                    or not _is_expired(path.stat().st_mtime, cutoff)
                ):
                    skipped += 1
                    continue
                path.unlink()
                deleted += 1
            except OSError:
                skipped += 1

    logger.info(
        "Public cleanup: deleted=%d skipped=%d retention_days=%d",
        deleted,
        skipped,
        days,
    )
    return deleted, skipped


def run_public_cleanup_periodically() -> None:
    """Run once at startup, then daily while the API process is alive."""
    while True:
        try:
            cleanup_public_files()
        except Exception:
            logger.exception("Public cleanup scan failed")
        time.sleep(CLEANUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    check_now = 1_000_000.0
    check_cutoff = check_now - DEFAULT_RETENTION_DAYS * 24 * 60 * 60
    assert _is_expired(check_cutoff - 0.001, check_cutoff)
    assert not _is_expired(check_cutoff, check_cutoff)
    print("cleanup age check passed")
