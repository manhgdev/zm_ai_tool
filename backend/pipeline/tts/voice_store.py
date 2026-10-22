"""VieNeu voice registry paths under backend/data/voices/vieneu."""
from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from ..core.config import DATA, PUBLIC_DATA, SERVER_ROOT

VIENEU_ROOT = DATA / "voices" / "vieneu"
PRESETS_DIR = VIENEU_ROOT / "presets"
CLONED_DIR = VIENEU_ROOT / "cloned"
CACHE_DIR = VIENEU_ROOT / "cache"
VOICES_JSON = VIENEU_ROOT / "voices.json"
# SDK embeddings/codes — never overwrite the app clone registry
SDK_VOICES_JSON = VIENEU_ROOT / "sdk_voices.json"
SHARED_REFERENCE_ROOT = SERVER_ROOT / "resources" / "voice-ref"
REFERENCE_ROOT = (DATA / "voice-ref") if os.environ.get("ZM_AI_TOOL_WEB_WORKER") == "1" else SHARED_REFERENCE_ROOT
REFERENCE_VOICES_JSON = REFERENCE_ROOT / "voices.json"
TTS_OUTPUT = PUBLIC_DATA / "tts_output"
TTS_TEMP = DATA / "tts_temp"

VOICE_TAGS = (
    "👨 Nam",
    "👩 Nữ",
    "🏔️ Miền Bắc",
    "🌴 Miền Nam",
    "👶 Trẻ em",
    "👴 Người già",
    "⭐ Review",
    "📜 Đọc thơ",
    "📰 Tin tức",
    "📢 Quảng cáo",
)
_VOICE_TAG_SET = frozenset(VOICE_TAGS)

VOICE_LANGUAGES = ("vi", "en", "zh", "ja", "ko", "th", "id", "es", "fr", "de", "pt")
_VOICE_LANGUAGE_SET = frozenset(VOICE_LANGUAGES)
_LANGUAGE_ALIASES = {
    "vn": "vi",
    "vie": "vi",
    "vi-vn": "vi",
    "en-us": "en",
    "en-gb": "en",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "cmn": "zh",
}


def normalize_voice_language(raw: Any, *, strict: bool = False) -> str:
    """Canonical language code for voice metadata (vi, en, …). Empty if unset."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    if not isinstance(raw, str):
        if strict:
            raise ValueError("language phải là chuỗi")
        return ""
    key = raw.strip().lower().replace("_", "-")
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    base = key.split("-", 1)[0]
    if base in _VOICE_LANGUAGE_SET:
        return base
    if strict:
        raise ValueError(f"language không hợp lệ: {raw}")
    return ""


def normalize_voice_tags(tags: Any, *, strict: bool = False) -> list[str]:
    """Deduplicate canonical tags; reject request data, filter legacy storage."""
    if tags is None:
        return []
    if not isinstance(tags, (list, tuple)):
        if strict:
            raise ValueError("tags phải là danh sách")
        return []
    out: list[str] = []
    invalid: list[str] = []
    for raw in tags:
        tag = raw.strip() if isinstance(raw, str) else ""
        if tag not in _VOICE_TAG_SET:
            if strict:
                invalid.append(str(raw))
            continue
        if tag not in out:
            out.append(tag)
    if invalid:
        raise ValueError(f"Tag không hợp lệ: {', '.join(invalid)}")
    return out


def ensure_vieneu_dirs() -> Path:
    legacy_output = DATA / "tts_output"
    if legacy_output.is_dir():
        TTS_OUTPUT.mkdir(parents=True, exist_ok=True)
        for child in legacy_output.iterdir():
            dest = TTS_OUTPUT / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))
    for d in (PRESETS_DIR, CLONED_DIR, CACHE_DIR, TTS_OUTPUT, TTS_TEMP):
        d.mkdir(parents=True, exist_ok=True)
    if not VOICES_JSON.is_file():
        VOICES_JSON.write_text(
            json.dumps({"version": 1, "cloned": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return VIENEU_ROOT


def _canonicalize_language_field(item: dict[str, Any]) -> bool:
    """Normalize language → short code (vi, en). Return True if changed."""
    if "language" not in item:
        return False
    raw = item.get("language")
    clean = normalize_voice_language(raw)
    if clean == (raw if isinstance(raw, str) else ""):
        return False
    if clean:
        item["language"] = clean
    else:
        item.pop("language", None)
    return True


def _read_cloned_raw() -> list[dict[str, Any]]:
    ensure_vieneu_dirs()
    try:
        data = json.loads(VOICES_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    # SDK format accidentally written here has "presets", no "cloned"
    if "cloned" not in data and "presets" in data:
        return []
    out: list[dict[str, Any]] = []
    dirty = False
    for item in data.get("cloned") or []:
        if isinstance(item, dict) and item.get("id") and item.get("ref"):
            item["tags"] = normalize_voice_tags(item.get("tags"))
            if _canonicalize_language_field(item):
                dirty = True
            out.append(item)
    if dirty:
        try:
            save_cloned(out)
        except Exception:
            pass
    return out


def _recover_orphan_wavs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """WAV còn trên đĩa nhưng mất khỏi registry (bug save_voices cũ)."""
    known_refs = {str(x.get("ref") or "").replace("\\", "/") for x in items}
    known_ids = {str(x.get("id") or "") for x in items}
    changed = False
    for wav in sorted(CLONED_DIR.glob("*.wav")):
        rel = f"cloned/{wav.name}"
        if rel in known_refs:
            continue
        stem = wav.stem
        if stem in known_ids:
            # cùng id nhưng ref lệch → gắn lại file
            for x in items:
                if x.get("id") == stem:
                    x["ref"] = rel
                    changed = True
                    break
            continue
        items.append(
            {
                "id": stem,
                "name": stem.replace("_", " ").strip() or stem,
                "ref": rel,
                "tags": [],
            }
        )
        known_refs.add(rel)
        known_ids.add(stem)
        changed = True
    if changed:
        save_cloned(items)
    return items


_ENGINE_NAME_PREFIX = re.compile(
    r"^(?:(?:VieNeu|zmAI)\s*[·•.\-]\s*(?:Clone\s*[·•.\-]\s*)?|"
    r"CapCut\s*[·•.\-]\s*|ElevenLabs\s*[·•.\-]\s*|macOS\s*[·•.\-]\s*)+",
    re.IGNORECASE,
)


def clean_display_name(name: str, *, fallback: str = "clone") -> str:
    """Bỏ prefix engine lặp (VieNeu · Clone · …) trước khi lưu/hiển thị."""
    s = re.sub(r"\s+", " ", (name or "").strip())
    while True:
        nxt = _ENGINE_NAME_PREFIX.sub("", s).strip(" ·•.-")
        nxt = re.sub(r"\s+", " ", nxt).strip()
        if nxt == s:
            break
        s = nxt
    return s or fallback


def load_cloned() -> list[dict[str, Any]]:
    items = _recover_orphan_wavs(_read_cloned_raw())
    # ponytail: scrub stacked "VieNeu · Clone ·" already saved into registry
    changed = False
    for x in items:
        raw = str(x.get("name") or "")
        cleaned = clean_display_name(raw, fallback=str(x.get("id") or "clone"))
        if cleaned != raw:
            x["name"] = cleaned
            changed = True
    if changed:
        save_cloned(items)
    return items


def _read_reference_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = [item for item in data if isinstance(item, dict) and item.get("id")]
    for item in out:
        item["tags"] = normalize_voice_tags(item.get("tags"))
        _canonicalize_language_field(item)
    return out


def _read_reference_raw() -> list[dict[str, Any]]:
    out = _read_reference_file(REFERENCE_VOICES_JSON)
    if REFERENCE_ROOT != SHARED_REFERENCE_ROOT:
        # Read the shared library on every request, including existing accounts.
        # Account uploads/metadata remain private; never snapshot the library.
        shared = _read_reference_file(SHARED_REFERENCE_ROOT / "voices.json")
        out = list({item["id"]: item for item in [*shared, *out]}.values())
    return out


def save_reference_voices(items: list[dict[str, Any]]) -> None:
    if REFERENCE_ROOT != SHARED_REFERENCE_ROOT:
        shared = {item["id"]: item for item in _read_reference_file(SHARED_REFERENCE_ROOT / "voices.json")}
        items = [item for item in items if item != shared.get(item.get("id"))]
    REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    REFERENCE_VOICES_JSON.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_reference_voices() -> list[dict[str, Any]]:
    """Only expose visible zmAI voices that have a usable reference file."""
    return [
        item
        for item in _read_reference_raw()
        if item.get("engine") == "vieneu"
        and item.get("type") == "zmAI"
        and not item.get("hidden")
        and reference_path(item).is_file()
    ]


def get_reference_voice(voice_id: str) -> dict[str, Any] | None:
    return next((x for x in load_reference_voices() if x.get("id") == voice_id), None)


def reference_path(item: dict[str, Any]) -> Path:
    path = REFERENCE_ROOT / str(item.get("ref_file") or "")
    if REFERENCE_ROOT != SHARED_REFERENCE_ROOT and not path.is_file():
        for shared in _read_reference_file(SHARED_REFERENCE_ROOT / "voices.json"):
            if shared["id"] == item.get("id") and shared.get("ref_file") == item.get("ref_file"):
                return SHARED_REFERENCE_ROOT / str(shared.get("ref_file") or "")
    return path


def replace_voice_audio(voice_id: str, source_wav: Path) -> dict[str, Any]:
    """Atomically replace the local reference audio for a zmAI or cloned voice."""
    vid = (voice_id or "").strip()
    clone_id = vid.removeprefix("vn:clone:")
    clone = next((x for x in load_cloned() if x.get("id") == clone_id), None)
    reference = get_reference_voice(vid)
    entry = clone or reference
    if not entry:
        raise KeyError(f"Không tìm thấy giọng '{vid}'")
    ref_name = str(entry.get("ref") if clone else entry.get("ref_file") or "")
    if not ref_name:
        raise FileNotFoundError("Giọng không có file tham chiếu")
    root = VIENEU_ROOT if clone else REFERENCE_ROOT
    target = (root / ref_name).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("Đường dẫn file giọng không hợp lệ")
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source_wav, pending)
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)
    return entry


def rename_reference(voice_id: str, new_name: str) -> dict[str, Any] | None:
    return update_reference(voice_id, name=new_name)


def update_reference(
    voice_id: str,
    *,
    name: str | None = None,
    tags: list[str] | None = None,
    language: str | None = None,
    favorite: bool | None = None,
) -> dict[str, Any] | None:
    clean_name = clean_display_name(name or "", fallback="") if name is not None else None
    if name is not None and not clean_name:
        return None
    clean_tags = normalize_voice_tags(tags, strict=True) if tags is not None else None
    clean_language = (
        normalize_voice_language(language, strict=True) if language is not None else None
    )
    items = _read_reference_raw()
    hit: dict[str, Any] | None = None
    for x in items:
        if x.get("id") == voice_id and not x.get("hidden"):
            if clean_name is not None:
                x["name"] = clean_name
                x["label"] = clean_name
            if clean_tags is not None:
                x["tags"] = clean_tags
            if clean_language is not None:
                x["language"] = clean_language
            if favorite is not None:
                x["favorite"] = bool(favorite)
            hit = x
            break
    if not hit:
        return None
    save_reference_voices(items)
    return hit


def remove_reference(voice_id: str) -> bool:
    """Soft-delete zmAI voice (hidden=true) — giữ WAV nguồn."""
    items = _read_reference_raw()
    hit = False
    for x in items:
        if x.get("id") == voice_id and not x.get("hidden"):
            x["hidden"] = True
            hit = True
            break
    if not hit:
        return False
    save_reference_voices(items)
    return True


def move_voice_engine(voice_id: str, target: str) -> dict[str, Any]:
    """Chuyển giọng giữa bucket zmAI ↔ clone. Trả về entry đích (id API-ready)."""
    tgt = (target or "").strip().lower()
    if tgt not in ("zmai", "clone"):
        raise ValueError("target phải là 'zmai' hoặc 'clone'")

    # --- clone → zmAI ---
    if voice_id.startswith("vn:clone:") or (
        not get_reference_voice(voice_id) and any(x.get("id") == voice_id for x in load_cloned())
    ):
        cid = voice_id.removeprefix("vn:clone:").strip()
        if tgt == "clone":
            entry = next((x for x in load_cloned() if x.get("id") == cid), None)
            if not entry:
                raise KeyError(f"Không tìm thấy giọng clone '{cid}'")
            return {
                **entry,
                "id": f"vn:clone:{entry['id']}",
                "name": entry.get("name") or cid,
                "engine": "clone",
            }
        src_item = next((x for x in load_cloned() if x.get("id") == cid), None)
        if not src_item:
            raise KeyError(f"Không tìm thấy giọng clone '{cid}'")
        src = VIENEU_ROOT / str(src_item.get("ref") or "")
        if not src.is_file():
            raise FileNotFoundError(f"Thiếu file clone: {src}")
        display = str(src_item.get("name") or cid).strip() or cid
        existing_ids = {str(x.get("id") or "") for x in _read_reference_raw()}
        new_id = cid if cid not in existing_ids else make_clone_id(display, existing_ids)
        ref_file = f"{new_id}.wav"
        dest = REFERENCE_ROOT / ref_file
        REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        items = _read_reference_raw()
        # unhide / replace same id if previously soft-deleted
        items = [x for x in items if x.get("id") != new_id]
        entry = {
            **{k: v for k, v in src_item.items() if k not in {"id", "name", "ref"}},
            "id": new_id,
            "name": display,
            "label": display,
            "type": "zmAI",
            "engine": "vieneu",
            "mode": "reference",
            "language": normalize_voice_language(src_item.get("language")) or "",
            "ref_file": ref_file,
            "ref_text": "",
            "hidden": False,
            "favorite": False,
            "tags": normalize_voice_tags(src_item.get("tags")),
        }
        items.append(entry)
        save_reference_voices(items)
        remove_cloned(cid)
        return {**entry, "id": new_id, "name": display, "engine": "zmai", "type": "zmAI"}

    # --- zmAI → clone ---
    if tgt == "zmai":
        item = get_reference_voice(voice_id)
        if not item:
            raise KeyError(f"Không tìm thấy giọng zmAI '{voice_id}'")
        return {
            **item,
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "engine": "zmai",
            "type": "zmAI",
        }
    item = get_reference_voice(voice_id)
    if not item:
        raise KeyError(f"Không tìm thấy giọng zmAI '{voice_id}'")
    src = reference_path(item)
    if not src.is_file():
        raise FileNotFoundError(f"Thiếu file reference: {src}")
    display = str(item.get("name") or voice_id).strip() or voice_id
    existing = {str(x.get("id") or "") for x in load_cloned()}
    safe = make_clone_id(display, existing)
    dest = CLONED_DIR / f"{safe}.wav"
    ensure_vieneu_dirs()
    shutil.copy2(src, dest)
    metadata = {
        k: v
        for k, v in item.items()
        if k not in {"id", "name", "label", "ref_file", "engine", "type", "mode", "hidden"}
    }
    entry = add_cloned(
        safe,
        display,
        f"cloned/{safe}.wav",
        tags=normalize_voice_tags(item.get("tags")),
        metadata=metadata,
    )
    remove_reference(voice_id)
    return {**entry, "id": f"vn:clone:{safe}", "name": display, "engine": "clone"}


def move_voice_engines(voice_ids: list[str], target: str) -> dict[str, list[dict[str, Any]]]:
    """Chuyển nhiều giọng, giữ kết quả từng item để caller báo lỗi một phần."""
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for voice_id in voice_ids:
        try:
            successes.append({"voiceId": voice_id, "voice": move_voice_engine(voice_id, target)})
        except Exception as exc:
            failures.append(
                {
                    "voiceId": voice_id,
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                }
            )
    return {"successes": successes, "failures": failures}


def save_cloned(items: list[dict[str, Any]]) -> None:
    ensure_vieneu_dirs()
    VOICES_JSON.write_text(
        json.dumps({"version": 1, "cloned": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def make_clone_id(name: str, existing: set[str] | None = None) -> str:
    """ASCII-ish stable id; unique against existing registry ids."""
    raw = unicodedata.normalize("NFKD", (name or "").strip())
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    slug = re.sub(r"[^\w\-]+", "_", raw, flags=re.ASCII).strip("_").lower() or "clone"
    slug = slug[:40]
    taken = existing if existing is not None else {str(x.get("id") or "") for x in load_cloned()}
    if slug not in taken:
        return slug
    for _ in range(8):
        cand = f"{slug}_{uuid.uuid4().hex[:4]}"
        if cand not in taken:
            return cand
    return f"{slug}_{uuid.uuid4().hex[:8]}"


def add_cloned(
    voice_id: str,
    name: str,
    ref_rel: str,
    *,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = load_cloned()
    items = [x for x in items if x.get("id") != voice_id]
    entry = {
        **(metadata or {}),
        "id": voice_id,
        "name": clean_display_name(name, fallback=voice_id),
        "ref": ref_rel,
        "tags": normalize_voice_tags(tags, strict=True),
    }
    items.append(entry)
    save_cloned(items)
    return entry


def rename_cloned(voice_id: str, new_name: str) -> dict[str, Any] | None:
    return update_cloned(voice_id, name=new_name)


def update_cloned(
    voice_id: str,
    *,
    name: str | None = None,
    tags: list[str] | None = None,
    language: str | None = None,
    favorite: bool | None = None,
) -> dict[str, Any] | None:
    clean_name = clean_display_name(name or "", fallback="") if name is not None else None
    if name is not None and not clean_name:
        return None
    clean_tags = normalize_voice_tags(tags, strict=True) if tags is not None else None
    clean_language = (
        normalize_voice_language(language, strict=True) if language is not None else None
    )
    items = load_cloned()
    hit: dict[str, Any] | None = None
    for x in items:
        if x.get("id") == voice_id:
            if clean_name is not None:
                x["name"] = clean_name
            if clean_tags is not None:
                x["tags"] = clean_tags
            if clean_language is not None:
                x["language"] = clean_language
            if favorite is not None:
                x["favorite"] = bool(favorite)
            hit = x
            break
    if not hit:
        return None
    save_cloned(items)
    return hit


def remove_cloned(voice_id: str) -> bool:
    items = load_cloned()
    nxt = [x for x in items if x.get("id") != voice_id]
    if len(nxt) == len(items):
        return False
    save_cloned(nxt)
    ref = next((x.get("ref") for x in items if x.get("id") == voice_id), None)
    if ref:
        p = VIENEU_ROOT / str(ref)
        if p.is_file():
            p.unlink(missing_ok=True)
    return True
