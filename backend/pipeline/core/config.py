"""Shared paths, .env load, ElevenLabs voice preset."""
from __future__ import annotations

import os
from pathlib import Path

# backend/ is ZM_AI_TOOL_HOME; repo root = SERVER_ROOT.parent
SERVER_ROOT = Path(
    os.environ.get("ZM_AI_TOOL_HOME")

    or Path(__file__).resolve().parents[2]
)
REPO_ROOT = SERVER_ROOT.parent
# Private: API keys, clone references, temporary uploads.
DATA = Path(os.environ.get("ZM_AI_TOOL_DATA") or SERVER_ROOT / "data")
# Public: project video/audio and completed TTS jobs (Vite/static at /data/*).
PUBLIC_DATA = Path(
    os.environ.get("ZM_AI_TOOL_PUBLIC_DATA")

    or SERVER_ROOT / "public"
)

# ponytail: không mkdir lúc import — PyInstaller build sẽ tạo backend/public trống
def ensure_data_dirs() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC_DATA.mkdir(parents=True, exist_ok=True)


def sanitize_httpx_no_proxy(env: dict | None = None) -> None:
    """httpx parse IPv6 trần ``::1`` trong NO_PROXY thành port ``:1`` → Whisper/HF chết."""
    target = os.environ if env is None else env
    broken = {"::1", "::1/128", "[::1]", "[::1]/128"}
    for name in ("NO_PROXY", "no_proxy"):
        raw = target.get(name)
        if not raw:
            continue
        cleaned = ",".join(
            part.strip() for part in str(raw).split(",") if part.strip() not in broken
        )
        target[name] = cleaned


def safe_child(base: Path, name: str) -> Path | None:
    """Path con hợp lệ trong base — chặn traversal (.., /, \\, tên tuyệt đối).

    Windows coi cả '\\' là separator, và uvicorn percent-decode '%5C' TRƯỚC khi
    routing, nên path param có thể chứa '..\\..\\'. Trả None khi không an toàn.
    """
    raw = (name or "").strip()
    if not raw or raw in (".", ".."):
        return None
    if "/" in raw or "\\" in raw or "\x00" in raw:
        return None
    candidate = base / raw
    try:
        resolved = candidate.resolve()
        base_resolved = base.resolve()
    except OSError:
        return None
    if resolved != base_resolved and base_resolved not in resolved.parents:
        return None
    return candidate


def export_display_path(path: Path) -> str:
    """Đường dẫn hiển thị: app desktop = full path; dev = backend/public/… trong repo."""
    resolved = path.resolve()
    if os.environ.get("ZM_AI_TOOL_DESKTOP") == "1":
        return str(resolved)
    try:
        return str(resolved.relative_to(REPO_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


EL_ADAM = "pNInz6obpgDQGcFmaJgB"
# ponytail: key thường thiếu voices_read → preset rộng; API 2s nếu được phép
_EL_PRESET = [
    (EL_ADAM, "Adam"),
    ("21m00Tcm4TlvDq8ikWAM", "Rachel"),
    ("AZnzlk1XvdvUeBnXmlld", "Domi"),
    ("EXAVITQu4vr4xnSDxMaL", "Bella"),
    ("ErXwobaYiN019PkySvjV", "Antoni"),
    ("MF3mGyEYCl7XYWbV9V6O", "Elli"),
    ("TxGEqnHWrfWFTfGW9XjX", "Josh"),
    ("VR6AewLTigWG4xSOukaG", "Arnold"),
    ("pMsXgVXv3BLzUgSXRplE", "Serena"),
    ("yoZ06aMxZJJ28mfd3POQ", "Sam"),
    ("ThT5KcBeYPX3keUQqHPh", "Dorothy"),
    ("2EiwWnXFnvU8HyyO9idq", "Clyde"),
    ("CYw3kZ02Hs0563khs1Fj", "Dave"),
    ("D38z5RcWu1voky8WS1ja", "Fin"),
    ("LcfcDJNUP1GaeZkokFn8", "Emily"),
    ("N2lVS1w4EtoT3dr4eOWO", "Callum"),
    ("ODq5zmih8GrVes37Dizd", "Patrick"),
    ("SOYHLrjzK2X1ezoPC6cr", "Harry"),
    ("TX3LPaxmHKxFdv7VOQHJ", "Liam"),
    ("XB0fDUnXU5powFXDhCwa", "Charlotte"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda"),
    ("Yko7PKHZNXotIFUBG7I9", "Matthew"),
    ("ZQe5CZNOzWyzPSCw5dgu", "James"),
    ("Zlb1dXrM653N07WRdFW3", "Joseph"),
    ("bVMeCyTHy58xNoL34h3p", "Jeremy"),
    ("flq6f7yk4E4fJM5XTYuZ", "Michael"),
    ("g5CIjZEefAph4nQFvHAz", "Ethan"),
    ("iP95p4xoKVk53GoZ742B", "Chris"),
    ("jBpfuIE2acCO8z3wKNLl", "Gigi"),
    ("jsCqWAovWfOLnJxsPWaX", "Freya"),
    ("nPczCjzI2devNBz1zQrb", "Brian"),
    ("oWAxZDx7w5VEj9dCyTzz", "Grace"),
    ("onwK4e9ZLuTAKqWW03F9", "Daniel"),
    ("pFZP5JQG7SspSbDNFt8N", "Lily"),
    ("pqHfZKP75CvOlQylNvMs", "Bill"),
    ("t0jbNlBVZ17f02VDIeMI", "Jessie"),
    ("z9fAnlkpzviPz146aGWa", "Glinda"),
    ("zcAOhNBS3c14rBihAFp1", "Giovanni"),
    ("zrHiDhphv9ZnVXBqCLjz", "Mimi"),
]
