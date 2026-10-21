"""Cookie normalization, Chrome profile copies, and runtime profile ownership."""
from __future__ import annotations

import errno
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import store
from .constants import (
    _FLOW_COOKIE_DOMAINS,
    _PROFILE_COPY_IGNORES,
    _PROFILE_IDENTITY_DEFAULT,
    _PROFILE_IDENTITY_ROOT,
)

_log = logging.getLogger("pipeline.flow.service")


def _account_cookie_records(value: Any) -> list[dict[str, Any]]:
    """Normalize saved cookie metadata without exposing cookie values."""
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if isinstance(raw, dict):
        raw = raw.get("cookies", raw.get("items", []))
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    # Chrome caps cookie lifetime at 400 days; stay just under it.
    persistent = time.time() + 390 * 86400
    for item in raw:
        if not isinstance(item, dict) or not item.get("name") or "value" not in item:
            continue
        cookie = {key: item[key] for key in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if key in item}
        try:
            has_expiry = float(cookie.get("expires", -1)) > 0
        except (TypeError, ValueError):
            has_expiry = False
        if not has_expiry:
            cookie["expires"] = persistent
        # Google SSO (__Secure-*) needs SameSite=None + Secure; missing sameSite
        # makes Playwright/Chrome drop the cookie and show the cookie-settings wall.
        if cookie.get("secure") and not cookie.get("sameSite"):
            cookie["sameSite"] = "None"
        if cookie.get("domain") or cookie.get("url"):
            result.append(cookie)
    return result


def _cookies_for_fresh_profile(value: Any) -> list[dict[str, Any]]:
    """Normalize jar for inject into an empty Chrome profile (F12 Cookie header style).

    Playwright persists Secure Google cookies as SameSite=Lax; re-adding those into
    a wiped profile hits Google's cookie-settings wall. F12 import has no sameSite
    and becomes None via ``_account_cookie_records`` — force that for secure Google
    domains at inject time only (stored jar metadata stays unchanged).
    """
    out: list[dict[str, Any]] = []
    for item in _live_flow_cookies(value):
        cookie = dict(item)
        domain = str(cookie.get("domain") or "").lower()
        if cookie.get("secure") and "google" in domain:
            cookie["sameSite"] = "None"
        out.append(cookie)
    return out


def _live_flow_cookies(value: Any) -> list[dict[str, Any]]:
    """Keep only unexpired Google/Flow cookies from the active account context."""
    now = time.time()
    result: list[dict[str, Any]] = []
    for cookie in _account_cookie_records(value):
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if not domain or not any(domain == allowed or domain.endswith("." + allowed) for allowed in _FLOW_COOKIE_DOMAINS):
            continue
        try:
            expires = float(cookie.get("expires", -1))
        except (TypeError, ValueError):
            expires = -1
        if expires > 0 and expires <= now:
            continue
        result.append(cookie)
    return result


def _profile_copy_ignore(_directory: str, names: list[str]) -> list[str]:
    """Exclude Chromium caches that are always live while Chrome is open."""
    return sorted(_PROFILE_COPY_IGNORES.intersection(names))


def _profile_copy_file(source: str, target: str) -> None:
    """Copy one profile file, tolerating a live Chrome handle on Windows."""
    try:
        shutil.copy2(source, target)
    except FileNotFoundError:
        # Chromium can delete a short-lived cache entry after enumeration.
        _log.debug("Flow skipped vanished profile file: %s", source)
        return
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        locked = winerror == 32 or exc.errno in (errno.EBUSY, errno.ETXTBSY) or (
            sys.platform == "win32" and exc.errno == errno.EACCES
        )
        if not locked:
            raise
        # Chrome may release a handle between directory enumeration and copy.
        # A short retry preserves fresh files without making profile cloning
        # depend on a clean shutdown of another Chrome session.
        for delay in (0.05, 0.15):
            time.sleep(delay)
            try:
                shutil.copy2(source, target)
                return
            except FileNotFoundError:
                _log.debug("Flow skipped vanished profile file: %s", source)
                return
            except OSError as retry_exc:
                retry_winerror = getattr(retry_exc, "winerror", None)
                retry_locked = retry_winerror == 32 or retry_exc.errno in (errno.EBUSY, errno.ETXTBSY) or (
                    sys.platform == "win32" and retry_exc.errno == errno.EACCES
                )
                if not retry_locked:
                    raise
        _log.debug("Flow skipped locked profile file: %s", source)


def _cookie_identity(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or ""),
        str(cookie.get("path") or "/"),
    )


def _wipe_profile_keep_login(profile: Path) -> None:
    """Delete all browsing data (storage, caches, history, sessions) but keep the Google login."""
    if not profile.is_dir():
        return
    items = [item for item in profile.iterdir() if item.name not in _PROFILE_IDENTITY_ROOT]
    default = profile / "Default"
    if default.is_dir():
        items += [item for item in default.iterdir() if item.name not in _PROFILE_IDENTITY_DEFAULT]
    for item in items:
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def _seed_stored_cookies(account: dict[str, Any]) -> bool:
    """An empty account profile bootstraps from its stored cookie jar.

    A Chrome-login profile that still has its cookie DB must not get the JSON
    copy: duplicate rows (http vs https source) can shadow a fresh login.
    """
    if not _account_cookie_records(account.get("cookie")):
        return False
    profile = Path(store.profile_dir(str(account.get("id") or "")))
    return not any(
        path.is_file() and path.stat().st_size > 0
        for relative in ("Default/Network/Cookies", "Default/Cookies")
        for path in (profile / relative,)
    )


def _merge_cookie_records(stored: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Update stored cookies with newer values and add new names. Never drop a stored cookie."""
    merged = {_cookie_identity(item): item for item in stored if item.get("name")}
    for item in fresh:
        if not item.get("name"):
            continue
        merged[_cookie_identity(item)] = item
    return list(merged.values())


class FlowProfilesMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    @staticmethod
    def _session_profile_dir(account_id: str, kind: str) -> Path:
        """Use one persistent Chrome profile per account and media kind."""
        return store.root() / "profiles" / f"{account_id}-{kind}"

    def _ensure_session_profile(self, account_id: str, kind: str) -> Path:
        target = self._session_profile_dir(account_id, kind)
        base = Path(store.profile_dir(account_id))
        if base.exists():
            # Login/reconnect writes fresh Google cookies to the canonical
            # account profile. Refresh each media profile before opening its
            # Chrome session so image/video jobs do not reuse stale cookies.
            # The target is disposable; retaining its old Cookie DB can make
            # Chrome ignore the freshly imported canonical jar.
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(
                base,
                target,
                ignore=_profile_copy_ignore,
                copy_function=_profile_copy_file,
                dirs_exist_ok=True,
            )
        return target

    def _clone_runtime_profile(self, account_id: str, job_id: str, kind: str = "image") -> Path | None:
        from .browser import profile_lock
        # Cookie databases can only be copied once Chrome releases this profile.
        with profile_lock(store.profile_dir(account_id)):
            self._check_cancel(job_id)
            return self._clone_closed_profile(account_id, job_id)

    def _clone_closed_profile(self, account_id: str, job_id: str) -> Path:
        """Copy login state into an isolated, cache-free profile for one job."""
        source = store.profile_dir(account_id)
        target = store.root() / "runtime-profiles" / account_id / job_id
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A user can remove an account profile (or Chrome can remove an
        # ephemeral file) between profile_dir() and copytree().  An empty
        # isolated profile is safe: Flow will report LOGIN_REQUIRED and the
        # account can be connected again instead of killing the worker thread.
        if not source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            return target
        try:
            shutil.copytree(
                source,
                target,
                ignore=_profile_copy_ignore,
                copy_function=_profile_copy_file,
                dirs_exist_ok=True,
            )
        except FileNotFoundError:
            # The profile directory itself can disappear after the initial
            # is_dir() check (account removal or Chrome cleanup race).
            target.mkdir(parents=True, exist_ok=True)
            _log.warning(
                "Flow profile disappeared while copying account=%s job=%s; continuing with empty profile",
                account_id,
                job_id,
            )
        except shutil.Error as exc:
            # copytree may encounter a file deleted during traversal. Keep the
            # stable files already copied and let Flow's auth check decide
            # whether a reconnect is required; propagate real permission or
            # I/O failures so they remain visible in the job status.
            errors = getattr(exc, "args", [[]])[0]
            if not errors or not all(
                "No such file or directory" in str(item) or "Errno 2" in str(item)
                for item in errors
            ):
                raise
            _log.warning(
                "Flow profile changed while copying account=%s job=%s; continuing with stable files",
                account_id,
                job_id,
            )
        # A job needs Google's native identity, not copied Flow app/cache state.
        _wipe_profile_keep_login(target)
        return target
