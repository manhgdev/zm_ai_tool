"""Resolve native drag-drop paths for the Srt Image project form."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

_SIV_KINDS = frozenset({"media", "audio", "timeline", "srt"})
_ALLOWED_SUFFIX = {
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"},
    "timeline": {".txt", ".srt", ".vtt", ".ass", ".ssa", ".csv", ".tsv", ".json", ".lrc"},
    "srt": {".srt"},
}


def siv_drop_kind(target: object) -> str:
    """Walk serialized DOM target parents for data-siv-drop."""
    node: object = target
    for _ in range(12):
        if not isinstance(node, dict):
            return ""
        attrs = node.get("attributes") or {}
        if isinstance(attrs, dict):
            kind = str(attrs.get("data-siv-drop") or attrs.get("dataSivDrop") or "").strip()
            if kind in _SIV_KINDS:
                return kind
        node = node.get("parentNode") or node.get("parentElement") or node.get("parent") or {}
    return ""


def take_dnd_path(files: object, dnd_paths: object) -> str:
    """Pick absolute path from pywebview drop files + leftover _dnd_state paths.

    Cocoa stores directory drops as (parent_basename, full_dir_path), so matching
    only on item[0] == File.name fails for folders. Also match Path(full).name.
    Mutates dnd_paths in place when a leftover entry is consumed.
    """
    entries = dnd_paths if isinstance(dnd_paths, list) else []
    file_rows = list(files) if isinstance(files, list) else []

    def _consume(match: tuple | list) -> str:
        path = unquote(str(match[1] if len(match) > 1 else ""))
        try:
            entries.remove(match)
        except ValueError:
            pass
        return path

    for row in file_rows:
        if not isinstance(row, dict):
            continue
        direct = str(row.get("pywebviewFullPath") or "").strip()
        if direct:
            return unquote(direct)
        name = unquote(str(row.get("name") or "").strip())
        if not name:
            continue
        for item in list(entries):
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            stored_name = unquote(str(item[0]))
            stored_path = unquote(str(item[1]))
            leaf = Path(stored_path.rstrip("/\\")).name
            if stored_name == name or leaf == name:
                return _consume(item)

    # Folder drops sometimes leave paths in _dnd_state with no FileList match.
    if len(entries) == 1 and isinstance(entries[0], (tuple, list)) and len(entries[0]) > 1:
        return _consume(entries[0])
    return ""


def resolve_siv_drop_path(kind: str, raw_path: str) -> str:
    """Map a dropped filesystem path to the value the Srt Image form needs."""
    path = str(raw_path or "").strip()
    if not path or kind not in _SIV_KINDS:
        return ""
    candidate = Path(path)
    if kind == "media":
        if candidate.is_dir():
            return str(candidate)
        if candidate.is_file() and candidate.parent.is_dir():
            return str(candidate.parent)
        return ""
    if not candidate.is_file() or candidate.suffix.lower() not in _ALLOWED_SUFFIX[kind]:
        return ""
    return str(candidate)
