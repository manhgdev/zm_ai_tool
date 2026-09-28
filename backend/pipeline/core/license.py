"""ZM Tool license check and local activation state."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from pipeline.core.config import DATA, ensure_data_dirs

API_BASE = "https://api.zm.io.vn/key"
APP_PATH = "zm_tool"
LICENSE_FILE = DATA / "license.json"
_CACHE_SECONDS = 30 * 60.0
# Offline cache tối đa 72h: sau đó phải verify lại với server dù server có down.
_OFFLINE_MAX_SECONDS = 72 * 3600.0
_cache_lock = threading.Lock()
_request_lock = threading.Lock()
_cache_at = 0.0
_cache: dict[str, Any] | None = None

# ponytail: HMAC key gắn với machine node (MAC address) để license.json
# không thể copy từ máy này sang máy khác. uuid.getnode() = stdlib, no deps.
def _machine_sig_key() -> bytes:
    import uuid
    node = uuid.getnode()
    return f"zm-tool-license-{node}-2025".encode()


_SIG_KEY = _machine_sig_key()


def _sign(payload: dict[str, Any]) -> str:
    """Tính HMAC-SHA256 của canonical JSON để phát hiện tamper."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hmac.new(_SIG_KEY, canonical.encode(), hashlib.sha256).hexdigest()


def _verify_sig(data: dict[str, Any]) -> bool:
    sig = data.pop("_sig", None)
    if not sig:
        return False
    expected = _sign(data)
    data["_sig"] = sig  # restore
    return hmac.compare_digest(sig, expected)


def _preload_cache_from_disk() -> None:
    """Đọc last_valid từ disk ngay khi module load — tránh flap window lúc startup."""
    global _cache, _cache_at
    try:
        data = json.loads((DATA / "license.json").read_text(encoding="utf-8"))
        # Kiểm signature trước khi tin tưởng disk cache
        if not _verify_sig(dict(data)):
            return
        lv = data.get("last_valid")
        if isinstance(lv, dict) and lv.get("valid"):
            # Kiểm TTL: offline cache không dùng quá _OFFLINE_MAX_SECONDS
            saved_at = float(lv.get("saved_at") or 0)
            if saved_at > 0 and time.time() - saved_at > _OFFLINE_MAX_SECONDS:
                return  # cache quá cũ, không load
            _cache = dict(lv)
            # TTL ngắn hơn (15 phút) để sớm verify lại với server
            _cache_at = time.monotonic() - _CACHE_SECONDS / 2
    except Exception:
        pass


_preload_cache_from_disk()


def _masked(key: str) -> str:
    if len(key) <= 6:
        return "•" * len(key)
    return f"{key[:3]}{'•' * min(8, len(key) - 6)}{key[-3:]}"


def _read_key() -> str:
    try:
        data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
        return str(data.get("key") or "").strip()
    except (OSError, ValueError, TypeError):
        return ""


def _read_last_valid() -> dict[str, Any] | None:
    """Return last known-valid status from disk (used when API server is down).

    Reject nếu:
    - Không có HMAC signature (bị sửa tay)
    - Đã quá _OFFLINE_MAX_SECONDS kể từ lần verify thực tế cuối
    - key expiresAt đã qua
    """
    try:
        data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
        # Verify signature
        if not _verify_sig(dict(data)):
            return None
        lv = data.get("last_valid")
        if not isinstance(lv, dict) or not lv.get("valid"):
            return None
        # Kiểm TTL offline
        saved_at = float(lv.get("saved_at") or 0)
        if saved_at > 0 and time.time() - saved_at > _OFFLINE_MAX_SECONDS:
            return None
        # Kiểm expiresAt nếu có (key hết hạn thì không dùng offline cache)
        expires_at = lv.get("expiresAt")
        if expires_at:
            try:
                import datetime
                exp = datetime.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if exp.timestamp() < time.time():
                    return None
            except Exception:
                pass
        return lv
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save_key(key: str, last_valid: dict[str, Any] | None = None) -> None:
    ensure_data_dirs()
    payload: dict[str, Any] = {"key": key}
    if last_valid:
        # Ghi timestamp để tính tuổi offline cache
        lv_with_ts = {**last_valid, "saved_at": time.time()}
        payload["last_valid"] = lv_with_ts
    else:
        # Preserve existing last_valid if already saved
        try:
            existing = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
            if existing.get("last_valid"):
                payload["last_valid"] = existing["last_valid"]
        except (OSError, ValueError, TypeError):
            pass
    # Ký payload trước khi lưu
    payload["_sig"] = _sign({k: v for k, v in payload.items() if k != "_sig"})
    tmp = Path(f"{LICENSE_FILE}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LICENSE_FILE)


def _clear_disk_cache(key: str) -> None:
    """Xoá last_valid khỏi disk khi server xác nhận key không hợp lệ.
    Giữ lại key để UI vẫn hiện thông báo lỗi đúng.
    """
    try:
        payload: dict[str, Any] = {"key": key}
        payload["_sig"] = _sign(payload)
        tmp = Path(f"{LICENSE_FILE}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(LICENSE_FILE)
    except OSError:
        pass


def _request(action: str, key: str, retries: int = 3) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZM-Tool/1.0",
        "Origin": "https://zm.io.vn",
        "Referer": "https://zm.io.vn/",
    }
    # httpx có thể parse NO_PROXY chứa IPv6 trần ``::1`` thành port ``:1``.
    # License API là HTTPS cố định nên kết nối trực tiếp, không phụ thuộc proxy máy.
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with httpx.Client(trust_env=False, timeout=15.0) as client:
                response = client.post(
                    f"{API_BASE}/{action}", json={"key": key}, headers=headers
                )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                detail = payload.get("data") or payload.get("message") or "Key không hợp lệ"
                raise RuntimeError(str(detail))
            if isinstance(payload.get("data"), dict) and payload["data"].get("apps") is not None:
                return payload["data"]
            return payload
        except RuntimeError:
            raise  # lỗi logic (key sai) — không retry
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.0)
    raise last_exc  # type: ignore[misc]


def status_from_payload(payload: dict[str, Any], key: str = "") -> dict[str, Any]:
    apps = payload.get("apps") if isinstance(payload.get("apps"), list) else []
    app = next((item for item in apps if item.get("path") == APP_PATH), None)
    if not payload.get("status"):
        message = "Key đã bị khóa"
    elif not app:
        message = "Key không có quyền sử dụng ZM Tool"
    elif not app.get("status"):
        message = "Quyền sử dụng ZM Tool đã bị khóa"
    else:
        remaining = int(app.get("remaining_day") or 0)
        message = "Đã kích hoạt" if remaining == -1 or remaining > 0 else "Key đã hết hạn"

    remaining = int(app.get("remaining_day") or 0) if app else 0
    valid = bool(
        payload.get("status")
        and app
        and app.get("status")
        and (remaining == -1 or remaining > 0)
    )
    return {
        "valid": valid,
        "configured": bool(key),
        "keyMasked": _masked(key) if key else "",
        "remainingDay": remaining,
        "expiresAt": app.get("expires_at") if app else None,
        "activationLimit": int(app.get("activation_limit") or 0) if app else 0,
        "message": message,
    }


def license_status(*, force: bool = False) -> dict[str, Any]:
    global _cache, _cache_at
    key = _read_key()
    if not key:
        return {
            "valid": False,
            "configured": False,
            "keyMasked": "",
            "remainingDay": 0,
            "expiresAt": None,
            "activationLimit": 0,
            "message": "Chưa nhập key kích hoạt",
        }
    with _cache_lock:
        if not force and _cache and time.monotonic() - _cache_at < _CACHE_SECONDS:
            return dict(_cache)
    with _request_lock:
        # React StrictMode/F5 can issue two status calls together; the second one
        # reuses the result created by the first instead of hitting the key API.
        with _cache_lock:
            if not force and _cache and time.monotonic() - _cache_at < _CACHE_SECONDS:
                return dict(_cache)
        try:
            result = status_from_payload(_request("checkkey", key), key)
            if result.get("valid"):
                # Key hợp lệ: lưu disk cache
                _save_key(key, result)
            else:
                # Server xác nhận key INVALID (revoke, hết hạn, bị khoá)
                # → xoá last_valid khỏi disk ngay để tránh offline bypass
                _clear_disk_cache(key)
        except Exception as exc:
            # Network/server lỗi — nếu cache RAM còn valid thì giữ
            with _cache_lock:
                if _cache and _cache.get("valid"):
                    _cache_at = time.monotonic()
                    return dict(_cache)
            # Không có cache RAM — thử lấy last_valid từ disk
            disk_valid = _read_last_valid()
            if disk_valid:
                disk_valid = dict(disk_valid)
                disk_valid["message"] = disk_valid.get("message", "") + " (offline cache)"
                with _cache_lock:
                    _cache = disk_valid
                    _cache_at = time.monotonic()
                return disk_valid
            result = {
                "valid": False,
                "configured": True,
                "keyMasked": _masked(key),
                "remainingDay": 0,
                "expiresAt": None,
                "activationLimit": 0,
                "message": f"Không thể kiểm tra key: {exc}",
                # Transient server/network failures must never become a
                # cached "not activated" result.
                "transient": True,
            }
        if not result.get("transient"):
            with _cache_lock:
                _cache = dict(result)
                _cache_at = time.monotonic()
    return result


def activate_license(key: str) -> dict[str, Any]:
    global _cache, _cache_at
    key = key.strip()
    if not key:
        raise ValueError("Vui lòng nhập key")
    if key == _read_key():
        current = license_status(force=True)
        if current["valid"]:
            return current

    checked = status_from_payload(_request("checkkey", key), key)
    if not checked["valid"]:
        raise ValueError(checked["message"])
    if checked["activationLimit"] <= 0:
        raise ValueError("Key đã hết lượt kích hoạt")

    activated = status_from_payload(_request("activate", key), key)
    if not activated["valid"]:
        raise ValueError(activated["message"])
    _save_key(key, activated)
    with _cache_lock:
        _cache = dict(activated)
        _cache_at = time.monotonic()
    return activated


def deactivate_license() -> dict[str, Any]:
    """Remove this computer's saved key without changing the key server."""
    global _cache, _cache_at
    try:
        LICENSE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Không thể xoá key đã lưu: {exc}") from exc
    with _cache_lock:
        _cache = None
        _cache_at = 0.0
    return license_status(force=True)


def license_cached_valid() -> bool:
    return bool(license_status().get("valid"))
