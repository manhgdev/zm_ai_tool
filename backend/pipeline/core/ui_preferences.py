"""Persistent, non-sensitive desktop UI preferences."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import DATA

_PREFERENCES_PATH = DATA / "ui_preferences.json"
_SUPPORTED_LOCALES = {"vi", "en"}
_MAX_STORAGE_ITEM_BYTES = 512_000
_MAX_STORAGE_BYTES = 2_000_000


def _installer_locale() -> str | None:
    if sys.platform != 'win32':
        return None
    import winreg
    # The second key also covers Setup versions released before InstallerLocale.
    for key, name in (
        (r'Software\ZM_AI_TOOL', 'InstallerLocale'),
        (r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8B8A31D0-2BC3-4D90-9CE4-64491974DF42}_is1', 'Inno Setup: Selected Language'),
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
            if value in _SUPPORTED_LOCALES:
                return value
        except OSError:
            continue
    return None


def _installer_locale_revision() -> str | None:
    if sys.platform != 'win32':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\ZM_AI_TOOL', 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
            value, _ = winreg.QueryValueEx(handle, 'InstallerLocaleRevision')
        return value if isinstance(value, str) and value else None
    except OSError:
        return None


def load_ui_preferences() -> dict[str, object]:
    try:
        saved = json.loads(_PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    locale = saved.get("locale")
    installer_locale = _installer_locale()
    revision = (_installer_locale_revision() or f'legacy:{installer_locale}') if installer_locale else None
    if installer_locale and revision != saved.get('installerLocaleRevision'):
        # A newer interactive Setup choice wins once, including over an old
        # English preference. Later app choices retain this receipt on save.
        locale = installer_locale
    storage = saved.get("storage")
    output_root = saved.get("outputRoot")
    return {
        "locale": locale if locale in _SUPPORTED_LOCALES else installer_locale,
        "installerLocaleRevision": revision or saved.get('installerLocaleRevision'),
        "storage": storage if isinstance(storage, dict) else {},
        "outputRoot": str(output_root) if output_root else None,
    }


def save_ui_preferences(
    *, locale: str | None = None, storage: dict[str, str] | None = None,
    output_root: str | None = None,
) -> dict[str, object]:
    current = load_ui_preferences()
    if locale is not None:
        if locale not in _SUPPORTED_LOCALES:
            raise ValueError("Unsupported locale")
        current["locale"] = locale
    if storage is not None:
        clean: dict[str, str] = {}
        total = 0
        for key, value in storage.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            size = len(value.encode("utf-8"))
            if size > _MAX_STORAGE_ITEM_BYTES:
                continue
            total += len(key.encode("utf-8")) + size
            if total > _MAX_STORAGE_BYTES:
                break
            clean[key] = value
        current["storage"] = clean
    if output_root is not None:
        current["outputRoot"] = output_root or None  # empty string → clear
    _PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PREFERENCES_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return current


def load_output_root() -> Path | None:
    """Return user-chosen output root from preferences, or None if not set."""
    raw = str(load_ui_preferences().get("outputRoot") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else None


def save_output_root(path: Path) -> None:
    """Persist user's chosen output root (must be an absolute path)."""
    save_ui_preferences(output_root=str(path))


def clear_output_root() -> None:
    """Reset output root to the platform default."""
    save_ui_preferences(output_root="")
