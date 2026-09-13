"""App-level cloud API config (translate + TTS keys)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import DATA
from .output_paths import app_output_root

_CONFIG_PATH = DATA / "app_config.json"

# provider → (env_key, default_base, default_model)
PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "env": "OPENAI_API_KEY",
        "base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "label": "OpenAI",
    },
    "gemini": {
        "env": "GEMINI_API_KEY",
        "base": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3.1-flash-lite",
        "label": "Gemini",
    },
    "deepseek": {
        "env": "DEEPSEEK_API_KEY",
        "base": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "label": "DeepSeek",
    },
    "openrouter": {
        "env": "OPENROUTER_API_KEY",
        "base": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.5-flash",
        "label": "OpenRouter",
    },
    "grok": {
        "env": "XAI_API_KEY",
        "base": "https://api.x.ai/v1",
        "model": "grok-3-mini",
        "label": "Grok",
    },
    "groq": {
        "env": "GROQ_API_KEY",
        "base": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-20b",
        "label": "Groq",
    },
    "nvidia": {
        "env": "NVIDIA_API_KEY",
        "base": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/riva-translate-4b-instruct-v2",
        "label": "NVIDIA NIM",
    },
}


def _default_cloud() -> dict[str, dict[str, str]]:
    return {
        pid: {
            "apiKey": "", "apiKeys": "", "baseUrl": meta["base"], "model": meta["model"],
        }
        for pid, meta in PROVIDERS.items()
    }


def _default_tts() -> dict[str, Any]:
    return {
        "elevenlabs": {
            "apiKeys": "",  # comma-separated, same as ELEVENLABS_API_KEYS
            "label": "ElevenLabs",
            "env": "ELEVENLABS_API_KEYS",
        }
    }


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return ("•" * max(4, len(key) - 4)) + key[-4:]
    return f"{key[:6]}••••••••{key[-4:]}"


def _clean_keys(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null", "undefined"} else text


def load_app_config() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
    cloud = _default_cloud()
    saved = raw.get("cloud") if isinstance(raw.get("cloud"), dict) else {}
    for pid, meta in PROVIDERS.items():
        block = saved.get(pid) if isinstance(saved.get(pid), dict) else {}
        file_keys = _clean_keys(block.get("apiKeys") or block.get("apiKey"))
        env_keys = _clean_keys(os.environ.get(meta["env"]))
        keys = file_keys if ("apiKeys" in block or "apiKey" in block) else (file_keys or env_keys)
        saved_model = str(block.get("model") or "").strip()
        if pid == "nvidia" and saved_model == "meta/llama-3.1-8b-instruct":
            saved_model = meta["model"]
        if saved_model in {"gemini-2.0-flash", "google/gemini-2.0-flash-001"}:
            saved_model = meta["model"]
        cloud[pid] = {
            "apiKey": next((key.strip() for key in keys.split(",") if key.strip()), ""),
            "apiKeys": keys,
            "baseUrl": str(block.get("baseUrl") or meta["base"]).strip() or meta["base"],
            "model": saved_model or meta["model"],
        }

    tts = _default_tts()
    saved_tts = raw.get("tts") if isinstance(raw.get("tts"), dict) else {}
    el_block = saved_tts.get("elevenlabs") if isinstance(saved_tts.get("elevenlabs"), dict) else {}
    file_keys = str(el_block.get("apiKeys") or "").strip()
    env_keys = (os.environ.get("ELEVENLABS_API_KEYS") or "").strip()
    tts["elevenlabs"]["apiKeys"] = file_keys if "apiKeys" in el_block else (file_keys or env_keys)

    return {"cloud": cloud, "tts": tts}


def save_app_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge patch into app_config.json; explicit ``apiKeys=""`` clears keys."""
    cur = load_app_config()
    cloud_in = patch.get("cloud") if isinstance(patch.get("cloud"), dict) else {}
    for pid in PROVIDERS:
        if pid not in cloud_in or not isinstance(cloud_in[pid], dict):
            continue
        b = cloud_in[pid]
        prev = cur["cloud"][pid]
        prev_raw = str(prev.get("apiKeys") or prev.get("apiKey") or "")
        prev_keys = [k.strip() for k in prev_raw.split(",") if k.strip()]
        if b.get("keys") is not None:
            resolved_keys: list[str] = []
            for item in b["keys"]:
                item_str = str(item or "").strip()
                if not item_str:
                    continue
                if item_str.startswith("__keep:") and item_str.endswith("__"):
                    try:
                        idx = int(item_str[7:-2])
                        if 0 <= idx < len(prev_keys):
                            resolved_keys.append(prev_keys[idx])
                    except ValueError:
                        pass
                elif not (item_str.startswith("•") or item_str == "(đã lưu)"):
                    resolved_keys.append(item_str)
            key = ", ".join(resolved_keys)
        elif b.get("apiKeys") is not None:
            key = _clean_keys(b["apiKeys"])
            if key.startswith("•") or key == "(đã lưu)":
                key = prev["apiKey"]
        elif b.get("apiKey") is not None:
            key = _clean_keys(b["apiKey"])
            if key.startswith("•") or key == "(đã lưu)":
                key = prev["apiKey"]
        else:
            key = _clean_keys(prev.get("apiKeys") or prev["apiKey"])
        base = str(b.get("baseUrl") or prev["baseUrl"]).strip() or prev["baseUrl"]
        model = str(b.get("model") or prev["model"]).strip() or prev["model"]
        cur["cloud"][pid] = {"apiKey": next((x.strip() for x in key.split(",") if x.strip()), ""), "apiKeys": key, "baseUrl": base, "model": model}

    tts_in = patch.get("tts") if isinstance(patch.get("tts"), dict) else {}
    el_in = tts_in.get("elevenlabs") if isinstance(tts_in.get("elevenlabs"), dict) else None
    if el_in is not None:
        prev_raw = str(cur["tts"]["elevenlabs"].get("apiKeys") or "")
        prev_keys = [k.strip() for k in prev_raw.split(",") if k.strip()]
        if el_in.get("keys") is not None:
            resolved_keys: list[str] = []
            for item in el_in["keys"]:
                item_str = str(item or "").strip()
                if not item_str:
                    continue
                if item_str.startswith("__keep:") and item_str.endswith("__"):
                    try:
                        idx = int(item_str[7:-2])
                        if 0 <= idx < len(prev_keys):
                            resolved_keys.append(prev_keys[idx])
                    except ValueError:
                        pass
                elif not (item_str.startswith("•") or item_str == "(đã lưu)"):
                    resolved_keys.append(item_str)
            cur["tts"]["elevenlabs"]["apiKeys"] = ", ".join(resolved_keys)
        else:
            keys = str(el_in.get("apiKeys") if "apiKeys" in el_in else prev_raw).strip()
            if keys.startswith("•") or keys == "(đã lưu)":
                keys = prev_raw
            cur["tts"]["elevenlabs"]["apiKeys"] = keys

    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    to_write = {
        "cloud": {
            pid: {
                "apiKey": cur["cloud"][pid]["apiKey"],
                "apiKeys": cur["cloud"][pid].get("apiKeys", cur["cloud"][pid]["apiKey"]),
                "baseUrl": cur["cloud"][pid]["baseUrl"],
                "model": cur["cloud"][pid]["model"],
            }
            for pid in PROVIDERS
        },
        "tts": {
            "elevenlabs": {
                "apiKeys": cur["tts"]["elevenlabs"]["apiKeys"],
            }
        },
    }
    _CONFIG_PATH.write_text(
        json.dumps(to_write, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cur


def public_app_config() -> dict[str, Any]:
    """Keys masked for UI."""
    cfg = load_app_config()
    is_desktop = os.environ.get("VIDEO_CLONE_DESKTOP") == "1"
    out_cloud: dict[str, Any] = {}
    for pid, meta in PROVIDERS.items():
        b = cfg["cloud"][pid]
        key = b["apiKey"]
        parts = [x.strip() for x in str(b.get("apiKeys") or key).split(",") if x.strip()]
        out_cloud[pid] = {
            "apiKey": _mask_key(key),
            "apiKeySet": bool(key),
            "apiKeys": ", ".join(_mask_key(x) for x in parts),
            "rawKeys": parts,
            "keyCount": len(parts),
            "baseUrl": b["baseUrl"],
            "model": b["model"],
            "label": meta["label"],
            "env": meta["env"],
        }

    el_keys_raw = str(cfg["tts"]["elevenlabs"].get("apiKeys") or "")
    parts = [k.strip() for k in el_keys_raw.split(",") if k.strip()]
    masked_parts = [_mask_key(k) for k in parts]
    return {
        "cloud": out_cloud,
        "tts": {
            "elevenlabs": {
                "apiKeys": ", ".join(masked_parts),
                "apiKeySet": bool(parts),
                "rawKeys": parts,
                "keyCount": len(parts),
                "label": "ElevenLabs",
                "env": "ELEVENLABS_API_KEYS",
            }
        },
        # Bản đóng gói / launcher — file đã trên máy, không cần «Tải xuống»
        "desktop": is_desktop,
        # Dynamic: reflects user-chosen root or platform default.
        # Windows portable: APP_ROOT/output | macOS: ~/Downloads/ZM_AIO_TOOL
        "desktopOutputRoot": str(app_output_root()),
    }


def provider_credentials(provider: str) -> dict[str, str]:
    pid = (provider or "").lower().strip()
    if pid not in PROVIDERS:
        raise RuntimeError(f"Provider không hỗ trợ: {provider}")
    b = load_app_config()["cloud"][pid]
    if not b["apiKey"]:
        env = PROVIDERS[pid]["env"]
        raise RuntimeError(
            f"Chưa có API key {PROVIDERS[pid]['label']}. "
            f"Mở Cấu hình → API dịch cloud, hoặc set {env} trong backend/.env"
        )
    return dict(b)


def provider_api_keys(provider: str) -> list[str]:
    cred = provider_credentials(provider)
    keys = [key.strip() for key in str(cred.get("apiKeys") or cred["apiKey"]).split(",") if key.strip()]
    return keys or [cred["apiKey"]]


def elevenlabs_api_keys() -> list[str]:
    """Keys from app_config.json first, else ELEVENLABS_API_KEYS env."""
    cfg = load_app_config()
    raw = str(cfg.get("tts", {}).get("elevenlabs", {}).get("apiKeys") or "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if keys:
        return keys
    env = (os.environ.get("ELEVENLABS_API_KEYS") or "").strip()
    return [k.strip() for k in env.split(",") if k.strip()]
