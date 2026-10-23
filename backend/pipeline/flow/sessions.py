"""Account browser sessions, periodic refresh, reset, and cleanup."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

from . import store
from .auth import _browser_at_login_wall
from .constants import _SESSION_REFRESH_INTERVAL_S
from .errors import _FlowSessionGenerationChanged, _session_needs_login
from .models import _flow_headless
from .profiles import (
    _cookies_for_fresh_profile,
    _wipe_profile_keep_login,
)

_log = logging.getLogger("pipeline.flow.service")


class FlowSessionsMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    @staticmethod
    def _session_key(account_id: str, kind: str) -> str:
        return f"{account_id}:{kind if kind in {'image', 'video'} else 'image'}"

    def _account_session_keys(self, account_id: str) -> list[str]:
        return [f"{account_id}:account", *[self._session_key(account_id, kind) for kind in ("image", "video")]]

    def _reap_closed_account_sessions(self, account_id: str) -> None:
        """Release stale reservations only after Chrome and its profile owner exit."""
        from .browser import chrome_profile_running, profile_lock

        with self._guard:
            for key in self._account_session_keys(account_id):
                session = self._manual_sessions.get(key)
                if not isinstance(session, dict):
                    continue
                if session.get("opening") and time.monotonic() - session["startedAt"] < 30:
                    continue
                profile_dir = getattr(session.get("browser"), "profile_dir", None)
                profile = Path(profile_dir) if isinstance(profile_dir, (str, os.PathLike)) else Path(store.profile_dir(account_id))
                # A closed manual Chrome may still be finishing stop(). Free
                # its slot now; the next BrowserManager waits on profile_lock.
                # The old owner's finally block only removes its own slot.
                if not chrome_profile_running(profile):
                    self._manual_sessions.pop(key, None)
                    self._auto_close_sessions.discard(key)
            started = self._connecting_started_at.get(account_id)
            if started is not None and time.monotonic() - started >= 30:
                profile = Path(store.profile_dir(account_id))
                if not profile_lock(profile).locked() and not chrome_profile_running(profile):
                    self._connecting_accounts.discard(account_id)
                    self._connecting_started_at.pop(account_id, None)

    async def _refresh_running_session(self, account_id: str, browser: Any, project_id: str) -> bool:
        """Refresh auth through the current Flow page without navigation."""
        with self._guard:
            lock = self._session_refresh_locks.setdefault(account_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return True
        try:
            now = time.monotonic()
            if now - self._session_refresh_at.get(account_id, -_SESSION_REFRESH_INTERVAL_S) < _SESSION_REFRESH_INTERVAL_S:
                return True
            self._session_refresh_at[account_id] = now
            try:
                page = await browser.page()
                result = await page.evaluate("""async () => {
                    try {
                        const response = await fetch(location.href, {
                            credentials: 'include', cache: 'no-store',
                            signal: AbortSignal.timeout(15000),
                        });
                        return {status: response.status, url: response.url};
                    } catch (error) {
                        return {status: 0, url: location.href, error: String(error)};
                    }
                }""")
                status = int((result or {}).get("status") or 0)
                url = str((result or {}).get("url") or "")
                target = urlsplit(url)
                if target.hostname == "accounts.google.com" or (
                    target.hostname == "flow.google.com"
                    and any(part in {"about", "login", "signin"} for part in target.path.split("/"))
                ):
                    raise RuntimeError("FLOW_LOGIN_REQUIRED: periodic session refresh reached login")
                if status == 401:
                    return True  # An API response alone does not invalidate the browser login.
                if not await self._persist_browser_cookies(account_id, browser):
                    # Keep the last verified snapshot. A transiently unreadable
                    # context must not turn a still-live Flow page into logout.
                    self._log(
                        "warning", "session_cookie_sync_unverified", account_id=account_id,
                        message="Periodic Flow refresh succeeded but the live cookie snapshot was not verified",
                    )
                return True
            except Exception as exc:
                if _session_needs_login(exc):
                    self._log("warning", "session_refresh_expired", account_id=account_id,
                              message="Flow session refresh detected expired authentication")
                    should_reconnect = True
                    with self._guard:
                        if should_reconnect and account_id in self._session_refresh_reconnects:
                            should_reconnect = False
                        elif should_reconnect:
                            self._session_refresh_reconnects.add(account_id)
                    if should_reconnect:
                        await asyncio.to_thread(
                            self._ensure_shared_reconnect,
                            account_id,
                            reason="FLOW_SESSION_EXPIRED: periodic session refresh",
                            interactive=False,
                        )
                    return False
                return True
        finally:
            lock.release()

    def _start_session_refresh(self, account_id: str, browser: Any, project_id: str) -> asyncio.Task | None:
        account_id = f"{account_id}:{id(browser)}"
        existing = self._session_refresh_tasks.get(account_id)
        if existing and not existing.done():
            self._session_refresh_refs[account_id] = self._session_refresh_refs.get(account_id, 0) + 1
            return None
        async def loop() -> None:
            try:
                while True:
                    await asyncio.sleep(_SESSION_REFRESH_INTERVAL_S)
                    if not await self._refresh_running_session(account_id.rsplit(':', 1)[0], browser, project_id):
                        return
            except asyncio.CancelledError:
                return
        task = asyncio.create_task(loop())
        self._session_refresh_tasks[account_id] = task
        self._session_refresh_refs[account_id] = 1
        return task

    async def _stop_session_refresh(self, account_id: str, task: asyncio.Task | None) -> None:
        refs = max(0, self._session_refresh_refs.get(account_id, 1) - 1)
        self._session_refresh_refs[account_id] = refs
        if refs:
            return
        task = task or self._session_refresh_tasks.get(account_id)
        if task is None:
            self._session_refresh_tasks.pop(account_id, None)
            self._session_refresh_refs.pop(account_id, None)
            self._session_refresh_reconnects.discard(account_id)
            return
        task.cancel()
        self._session_refresh_tasks.pop(account_id, None)
        self._session_refresh_refs.pop(account_id, None)
        self._session_refresh_reconnects.discard(account_id)
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _session_for(self, account_id: str, kind: str) -> Any:
        """Read the new kind slot, accepting the pre-split image slot once."""
        key = self._session_key(account_id, kind)
        value = self._manual_sessions.get(key)
        if value is None and kind == "image":
            return self._manual_sessions.get(account_id)
        return value

    def _close_auto_sessions(self, account_id: str) -> None:
        """Close job-owned (auto) Chrome sessions of this account and wait for release."""
        closed = False
        for kind in ("image", "video"):
            key = self._session_key(account_id, kind)
            with self._guard:
                session = self._manual_sessions.get(key)
                browser = session.get("browser") if isinstance(session, dict) and key in self._auto_close_sessions else None
            if browser is None:
                continue
            closed = True
            try:
                browser.kill_now()
            except Exception:
                pass
            for _ in range(50):
                with self._guard:
                    if key not in self._manual_sessions:
                        break
                time.sleep(0.2)
        if closed:
            time.sleep(1.0)

    def reset_account_session(
        self,
        account_id: str,
        *,
        open_ui_on_failure: bool = False,
        keep_running_jobs: bool = False,
        cookies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Clear Flow browsing state; cookie imports also replace Google login.

        The account row and project IDs stay in place. Without new cookies, the
        native Google identity is retained and the saved jar bootstraps missing auth.

        ``keep_running_jobs``: processing jobs keep their runtime clones and
        shared project ids while the root profile is refreshed for new work.
        Explicitly imported ``cookies`` replace native auth in a clean profile.
        """
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        with self._guard:
            reset_lock = self._account_reset_locks.setdefault(account_id, threading.Lock())
        if not reset_lock.acquire(blocking=False):
            raise RuntimeError("FLOW_PROFILE_BUSY")
        try:
            return self._reset_account_session_locked(
                account_id,
                open_ui_on_failure=open_ui_on_failure,
                keep_running_jobs=keep_running_jobs,
                cookies=cookies,
            )
        except RuntimeError as exc:
            current = store.get_row("accounts", account_id) or {}
            if cookies is not None and current.get("resetState") == "draining":
                store.patch_row("accounts", account_id, {
                    "status": "reconnect", "resetState": "action_required",
                    "error": str(exc), "updatedAt": time.time(),
                })
            raise
        finally:
            reset_lock.release()

    def _reset_account_session_locked(
        self,
        account_id: str,
        *,
        open_ui_on_failure: bool = False,
        keep_running_jobs: bool = False,
        cookies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        if cookies is not None:
            cookies = _cookies_for_fresh_profile(cookies)
            if not cookies:
                raise RuntimeError("FLOW_COOKIE_INVALID")
        self._reap_closed_account_sessions(account_id)
        self._close_auto_sessions(account_id)
        with self._guard:
            if any(key in self._manual_sessions for key in self._account_session_keys(account_id)) or account_id in self._connecting_accounts or account_id in self._syncing_accounts:
                raise RuntimeError("FLOW_PROFILE_BUSY")
        from .browser import BrowserManager, chrome_profile_running, profile_lock
        profile = Path(store.profile_dir(account_id))
        if not profile_lock(profile).locked() and chrome_profile_running(profile):
            BrowserManager(headless=True, profile_dir=profile).kill_now()
        with self._guard:
            generation = self._account_session_generation.get(account_id, int(account.get("sessionGeneration") or 0)) + 1
            self._account_session_generation[account_id] = generation
        now = time.time()
        store.patch_row("accounts", account_id, {
            "resetState": "draining", "sessionGeneration": generation,
            "resetRequestedAt": now, "resetReason": "cookie_replacement" if cookies is not None else "manual_or_auto",
            "updatedAt": now,
        })
        # Make processing jobs explicitly recoverable.  Their submission and
        # media identity remain untouched; only the browser lifecycle changes.
        for job in store.list_rows("jobs"):
            if job.get("accountId") == account_id and job.get("status") == "processing":
                store.patch_row("jobs", str(job.get("id")), {
                    "stage": "recovering_session", "sessionGeneration": generation,
                    "updatedAt": now,
                })
                with self._guard:
                    active_browser = self._active_browsers.get(str(job.get("id") or ""))
                if active_browser is not None:
                    try:
                        active_browser.kill_now()
                    except Exception:
                        pass
        with self._account_condition:
            self._account_condition.notify_all()
        busy = lambda: not keep_running_jobs and any(
            job.get("accountId") == account_id and job.get("status") == "processing" for job in self.jobs()
        )
        if busy():
            store.patch_row("accounts", account_id, {"resetState": "draining", "updatedAt": time.time()})
            deadline = time.monotonic() + 20.0
            while busy() and time.monotonic() < deadline:
                with self._account_condition:
                    self._account_condition.wait(timeout=0.25)
        # Runtime profiles can continue independently; preserve their
        # submission identity if the drain timed out.
        if busy():
            keep_running_jobs = True
        self._close_auto_sessions(account_id)
        with self._guard:
            if any(key in self._manual_sessions for key in self._account_session_keys(account_id)) or account_id in self._connecting_accounts:
                raise RuntimeError("FLOW_PROFILE_BUSY")
        if busy():
            raise RuntimeError("Account still has active Flow jobs")
        # Only a verified live browser may export cookies. Old runtime DBs
        # contain profile-bound encrypted values and must never merge back.
        runtime_root = store.root() / "runtime-profiles" / account_id
        disposable = [self._session_profile_dir(account_id, kind) for kind in ("image", "video")]
        live_ids = {
            str(job.get("id") or "")
            for job in self.jobs()
            if job.get("accountId") == account_id and job.get("status") == "processing"
        } if keep_running_jobs else set()
        if runtime_root.is_dir():
            disposable.extend(child for child in runtime_root.iterdir() if child.is_dir() and child.name not in live_ids)
        with ExitStack() as locks:
            if cookies is not None:
                # Block new session reservations and acquire every closed profile
                # before deleting anything. A partial/busy wipe must not mix jars.
                locks.enter_context(self._guard)
                if any(key in self._manual_sessions for key in self._account_session_keys(account_id)) or account_id in self._connecting_accounts or account_id in self._syncing_accounts:
                    raise RuntimeError("FLOW_PROFILE_BUSY")
                for target in [profile, *disposable]:
                    lock = profile_lock(target)
                    if not lock.acquire(blocking=False):
                        raise RuntimeError("FLOW_PROFILE_BUSY")
                    locks.callback(lock.release)
                    if chrome_profile_running(target):
                        BrowserManager(headless=True, profile_dir=target).kill_now()
                        if chrome_profile_running(target):
                            raise RuntimeError("FLOW_PROFILE_BUSY")
            try:
                for target in disposable:
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=cookies is None)
                if runtime_root.is_dir() and (not keep_running_jobs or not any(runtime_root.iterdir())):
                    shutil.rmtree(runtime_root, ignore_errors=cookies is None)
                if cookies is not None:
                    # Replacing cookies is a new login, not a merge with the old
                    # Google device identity, native cookie DB or site storage.
                    shutil.rmtree(profile)
                    store.patch_row("accounts", account_id, {"cookie": cookies})
                else:
                    with profile_lock(profile):
                        _wipe_profile_keep_login(profile)
            except OSError:
                if cookies is not None:
                    raise RuntimeError("FLOW_PROFILE_BUSY") from None
                raise
        patch = {
            "status": "connecting", "error": None, "suspendedUntil": None, "suspendReason": None,
            "accessToken": None, "refreshToken": None,
            "updatedAt": time.time(),
            "resetState": "reconnecting", "sessionGeneration": generation,
        }
        store.patch_row("accounts", account_id, patch)
        self._clear_session_ready(account_id)
        self._log(
            "info", "account_session_reset", account_id=account_id,
            message=("Đã tạo hồ sơ Chrome sạch để nhập cookie mới / Created a clean Chrome profile for replacement cookies"
                     if cookies is not None else "Đã xoá dữ liệu Flow và giữ đăng nhập Google / Cleared Flow data and kept the Google login"),
        )
        try:
            connect_kwargs: dict[str, Any] = {
                "headless": True,
                "open_ui_on_failure": open_ui_on_failure,
            }
            self.connect(account_id, **connect_kwargs)
        except RuntimeError:
            # Let the recovery coordinator defer a profile lifecycle race
            # instead of reporting a false successful reset or stranding the
            # account in ``connecting`` without a login worker.
            raise
        result = self._public_account(store.get_row("accounts", account_id) or account)
        with self._account_condition:
            self._account_condition.notify_all()
        return result

    def open_account_session(
        self,
        account_id: str,
        *,
        kind: str = "image",
        fullscreen: bool = True,
        page_zoom: float = 1.0,
        auto_close: bool = False,
        headless: bool = False,
    ) -> dict[str, Any]:
        """Open the saved Playwright profile for manual or owned job use."""
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        self._reap_closed_account_sessions(account_id)
        kind = kind if kind in {"image", "video"} else "image"
        cookie_auth = str(account.get("authMethod") or "chrome") == "cookie"
        session_key = self._session_key(account_id, kind) if auto_close else f"{account_id}:account"
        # A job-opened session keeps its original headless mode; replace it
        # when this run asks for the other mode (retry popup checkbox).
        stale = None
        with self._guard:
            current = self._manual_sessions.get(session_key)
            if (
                isinstance(current, dict) and session_key in self._auto_close_sessions
                and current.get("headless") is not None and current["headless"] != bool(headless)
                # Never replace a session that just opened: concurrently
                # starting jobs may be attaching to it right now.
                and time.monotonic() - float(current.get("startedAt") or 0) >= 30
                and not any(
                    getattr(value, "cdp_url", None) == current.get("cdp_url")
                    for value in self._active_browsers.values()
                )
            ):
                stale = current.get("browser")
        if stale is not None:
            try:
                stale.kill_now()
            except Exception:
                pass
            for _ in range(50):
                with self._guard:
                    if session_key not in self._manual_sessions:
                        break
                time.sleep(0.2)
            time.sleep(1.5)  # let Chrome release the profile before relaunching
        with self._guard:
            if account_id in self._connecting_accounts or account_id in self._syncing_accounts:
                raise RuntimeError("FLOW_PROFILE_BUSY")
            if session_key in self._manual_sessions:
                if auto_close:
                    self._auto_close_sessions.add(session_key)
                return {"opened": True}
            reservation = {"opening": True, "startedAt": time.monotonic()}
            self._manual_sessions[session_key] = reservation
            if auto_close:
                self._auto_close_sessions.add(session_key)

        def run_session() -> None:
            browser = None
            try:
                from .browser import BrowserManager, FLOW_BASE_URL, free_local_port
                cdp_port = free_local_port()
                session_profile = (
                    self._ensure_session_profile(account_id, kind) if auto_close
                    else Path(store.profile_dir(account_id))
                )
                browser = BrowserManager(
                    headless=headless,
                    profile_dir=session_profile,
                    google_login_compat=True,
                    fullscreen=fullscreen,
                    page_zoom=page_zoom,
                    remote_debugging_port=cdp_port,
                    restore_last_session=cookie_auth,
                )
                browser._flow_session_generation = self._session_generation(account_id)
                browser._flow_started_at = time.time()
                async def open_and_wait() -> None:
                    await browser.start()
                    # Bootstrap an empty profile only. Reinjecting an older jar
                    # into a live Chrome profile can replace rotated auth cookies.
                    live = store.get_row("accounts", account_id) or account
                    seeded = await self._seed_browser_cookies(live, browser)
                    self._log(
                        "info", "open_session_browser_ready", account_id=account_id,
                        details={"profile": str(session_profile), "seededFromJar": seeded},
                    )
                    with self._guard:
                        self._manual_sessions[session_key] = {
                            "browser": browser,
                            "cdp_url": browser.cdp_url,
                            "headless": bool(headless),
                            "startedAt": time.monotonic(),
                        }
                    page = await browser.page()
                    # Same as Connect: open Flow home (jobs navigate to their
                    # own project), then save any new cookies and mark the
                    # account online. Do not send Chrome accounts through
                    # ServiceLogin first.
                    await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=30_000)
                    signed_in = await self._confirm_open_session(browser, account_id, page)
                    if not signed_in and not seeded and await _browser_at_login_wall(browser):
                        if await self._seed_browser_cookies(live, browser):
                            await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=30_000)
                            signed_in = await self._confirm_open_session(browser, account_id, page)
                    self._log(
                        "info" if signed_in else "warning", "open_session_auth_check",
                        account_id=account_id,
                        message="Open session authentication check passed" if signed_in else "Open session authentication check failed",
                        details={"url": str(page.url or ""), "signedIn": signed_in},
                    )
                    if not signed_in and not headless and await _browser_at_login_wall(browser):
                        # Dead session: go straight to Google sign-in, like Connect.
                        email = str(account.get("email") or "").strip()
                        if urlsplit(str(page.url or "")).hostname != "accounts.google.com":
                            await page.goto(
                                "https://accounts.google.com/ServiceLogin?service=wise"
                                f"&continue={quote(FLOW_BASE_URL, safe='')}&login_hint={quote(email, safe='')}",
                                wait_until="domcontentloaded", timeout=30_000,
                            )
                        await self._fill_google_login_email(page, email)
                    last_cookie_sync = time.monotonic()
                    while not page.is_closed() and browser.context.pages:
                        # Until the user finishes a login wall, poll quickly so
                        # signing in here works like Connect.
                        if time.monotonic() - last_cookie_sync >= (60 if signed_in else 2):
                            last_cookie_sync = time.monotonic()
                            confirmed = await self._confirm_open_session(browser, account_id, page)
                            if confirmed and not signed_in:
                                self._log("success", "account_connected", account_id=account_id,
                                          details={"via": "open_session"})
                                self._requeue_auth_failed_jobs(account_id)
                            signed_in = signed_in or confirmed
                        await asyncio.sleep(1)
                async def run_browser() -> None:
                    try:
                        await open_and_wait()
                    finally:
                        if browser is not None:
                            await self._persist_browser_cookies(account_id, browser)
                            try:
                                await asyncio.wait_for(browser.stop(), timeout=8.0)
                            except Exception:
                                pass
                asyncio.run(run_browser())
            except Exception as exc:
                _log.warning("manual Flow session failed for %s: %s", account_id, exc)
            finally:
                with self._guard:
                    current = self._manual_sessions.get(session_key)
                    if current is reservation or (isinstance(current, dict) and current.get("browser") is browser):
                        self._manual_sessions.pop(session_key, None)
                        self._auto_close_sessions.discard(session_key)

        threading.Thread(target=run_session, daemon=True, name=f"flow-manual-{account_id}-{kind}").start()
        return {"opened": True}

    def connect(
        self,
        account_id: str,
        *,
        force_interactive: bool = False,
        cookies: list[dict[str, Any]] | None = None,
        allow_session_close: bool = False,
        headless: bool = False,
        open_ui_on_failure: bool = False,
    ) -> dict[str, Any]:
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        if cookies is not None:
            return self.reset_account_session(
                account_id, cookies=cookies, open_ui_on_failure=open_ui_on_failure,
            )
        self._reap_closed_account_sessions(account_id)
        auto_browser = None
        with self._guard:
            busy_keys = self._account_session_keys(account_id)
            busy_key = next((key for key in busy_keys if key in self._manual_sessions), None)
            if busy_key:
                if not allow_session_close and busy_key not in self._auto_close_sessions:
                    raise RuntimeError("FLOW_PROFILE_BUSY")
                session = self._manual_sessions.get(busy_key)
                auto_browser = session.get("browser") if isinstance(session, dict) else None
                self._manual_sessions.pop(busy_key, None)
                self._auto_close_sessions.discard(busy_key)
            if account_id in self._connecting_accounts:
                return self._public_account(account)
            self._connecting_accounts.add(account_id)
            self._connecting_started_at[account_id] = time.monotonic()
            patch = {"status": "connecting", "error": None, "updatedAt": time.time()}
            store.patch_row("accounts", account_id, patch)
        if auto_browser is not None:
            try:
                auto_browser.kill_now()
            except Exception:
                pass
        self._log("info", "account_connecting", account_id=account_id)
        threading.Thread(
            target=lambda: asyncio.run(self._login(
                account_id,
                force_interactive=force_interactive,
                cookies=cookies,
                headless=headless,
                open_ui_on_failure=open_ui_on_failure,
            )),
            daemon=True,
            name=f"flow-login-{account_id}",
        ).start()
        return self._public_account(store.get_row("accounts", account_id) or account)

    def _auto_headless(self, account_id: str) -> bool:
        """True when this account's active/queued jobs run with 'Mở Chrome khi chạy' off."""
        rows = [
            row for row in store.list_rows("jobs")
            if row.get("accountId") == account_id and row.get("status") in {"queued", "processing", "action_required"}
        ]
        return any(_flow_headless(row.get("settings") or {}) for row in rows)

    def _mark_session_ready(self, account_id: str) -> None:
        """Allow the rest of this account's queue to open Chrome."""
        if not account_id:
            return
        with self._account_condition:
            self._account_session_ready.add(account_id)
            self._account_condition.notify_all()

    def _clear_session_ready(self, account_id: str) -> None:
        if not account_id:
            return
        with self._account_condition:
            self._account_session_ready.discard(account_id)
            self._account_condition.notify_all()

    def _session_warmup_wait(self, account_id: str) -> bool:
        """True while the first job is still proving this account is signed in.

        Parallel Chromes must not start until that check succeeds; a dead
        cookie would otherwise fail the whole batch halfway.
        """
        if account_id in self._account_session_ready:
            return False
        if account_id in self._connecting_accounts:
            return True
        return self._account_active.get(account_id, 0) >= 1

    def _account_session_blocked(self, account_id: str) -> bool:
        """True while a shared reconnect/login is in progress for this account."""
        if account_id in self._connecting_accounts:
            return True
        account = store.get_row("accounts", account_id) or {}
        # A failed login leaves the persisted status as ``reconnect`` after
        # _connecting_accounts is cleared. Do not leave queued jobs waiting
        # forever; their admission check will start one fresh reconnect.
        return account.get("status") == "connecting" or account.get("resetState") in {
            "requested", "draining", "resetting", "reconnecting",
        }

    def _session_generation(self, account_id: str) -> int:
        account = store.get_row("accounts", account_id) or {}
        with self._guard:
            value = self._account_session_generation.get(account_id, int(account.get("sessionGeneration") or 0))
            self._account_session_generation[account_id] = value
            return value

    def _assert_session_generation(self, account_id: str, expected: int) -> None:
        if expected and self._session_generation(account_id) != expected:
            raise _FlowSessionGenerationChanged(
                f"FLOW_SESSION_GENERATION_CHANGED: account={account_id}"
            )

    def _kill_job_browser(self, job_id: str) -> None:
        with self._guard:
            browser = self._active_browsers.get(str(job_id))
        if browser is None:
            return
        try:
            browser.kill_now()
        except Exception as exc:
            # Cleanup must never abort bulk deletion or kill its worker thread.
            self._log("warning", "browser_kill_failed", job_id=str(job_id), message=str(exc))

    def _close_auto_session_if_idle(self, account_id: str, kind: str = "image") -> None:
        """Close a session opened implicitly by prompt generation.

        Explicitly opened account sessions stay under the user's control. An
        implicit image-generation session is owned by the queue and must be
        reaped after the last queued/processing job, including failures.
        """
        if kind not in {"image", "video"}:
            return
        session_key = self._session_key(account_id, kind)
        with self._guard:
            if session_key not in self._auto_close_sessions and not (kind == "image" and account_id in self._auto_close_sessions):
                return
            session = self._session_for(account_id, kind)
            if not isinstance(session, dict):
                return
            if any(
                str(row.get("accountId") or "") == account_id
                and str(row.get("kind") or "image") == kind
                and row.get("status") in {"queued", "processing"}
                for row in store.list_rows("jobs")
            ):
                return
            self._auto_close_sessions.discard(session_key)
            if kind == "image":
                self._auto_close_sessions.discard(account_id)
            browser = session.get("browser")
        if browser is None:
            return
        try:
            # The owner browser runs its own Playwright event loop in the
            # session thread. kill_now is deliberately synchronous and avoids
            # crossing that loop; the owner thread then performs its normal
            # BrowserManager.stop() cleanup.
            browser.kill_now()
            self._log(
                "info",
                "auto_session_closed",
                account_id=account_id,
                message="Prompt session closed after the account queue became idle",
            )
        except Exception as exc:
            self._log(
                "warning",
                "auto_session_close_failed",
                account_id=account_id,
                message=str(exc),
            )

    async def _close_job_browser(
        self,
        browser: Any,
        job_id: str,
        *,
        account_id: str | None = None,
        runtime_profile: Path | None = None,
    ) -> None:
        """Close an automatic job browser without leaving Chrome orphaned.

        External CDP attach (user's manual Chrome) may only disconnect.
        Job-owned Playwright launches and Flow-spawned Chrome processes are
        safe to force-kill via ``kill_now``.
        """
        # Chrome rotates Google cookies while the job runs. Save them to the
        # accounts.json jar before the context dies so reset/reconnect can
        # re-seed a clean profile (same as add-via-cookie).
        if account_id:
            if not await self._persist_browser_cookies(account_id, browser):
                self._log(
                    "warning",
                    "job_cookie_save_failed",
                    job_id=job_id,
                    account_id=account_id,
                    message="Could not persist Google cookies before closing job Chrome",
                )

        def _may_force_kill() -> bool:
            if getattr(browser, "_owns_chrome_process", False):
                return True
            return not getattr(browser, "cdp_url", None)

        try:
            await asyncio.wait_for(browser.stop(), timeout=8.0)
            if _may_force_kill():
                # Persistent Chromium can outlive Playwright's context close
                # for a short time. Reap the job-owned profile process too.
                await asyncio.to_thread(browser.kill_now)
            return
        except Exception as exc:
            self._log(
                "warning",
                "browser_cleanup_failed",
                job_id=job_id,
                message=str(exc),
                details={"cdpAttached": bool(getattr(browser, "cdp_url", None))},
            )
        # Never kill an external CDP attach: it belongs to the user's manual
        # account session. Owned spawn / job profiles may be force-killed.
        if _may_force_kill():
            try:
                await asyncio.to_thread(browser.kill_now)
            except Exception as exc:
                self._log(
                    "warning",
                    "browser_force_cleanup_failed",
                    job_id=job_id,
                    message=str(exc),
                )

    def _kill_job_browsers(self, job_ids: set[str]) -> None:
        """Issue all browser kills concurrently so bulk delete stays fast."""
        if not job_ids:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(job_ids))) as pool:
            # _kill_job_browser is defensive; consume all futures so one
            # unexpected executor error cannot escape this background thread.
            for future in [pool.submit(self._kill_job_browser, job_id) for job_id in job_ids]:
                try:
                    future.result()
                except Exception as exc:
                    self._log("warning", "bulk_browser_kill_failed", message=str(exc))
