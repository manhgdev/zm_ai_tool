"""Google login, cookie verification, and live cookie persistence."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any
from urllib.parse import urlsplit

from . import store
from .constants import _PROJECT_RE
from .errors import _soft_retry_backoff_s
from .models import _detect_plan
from .profiles import (
    _account_cookie_records,
    _cookies_for_fresh_profile,
    _live_flow_cookies,
    _seed_stored_cookies,
)

_log = logging.getLogger("pipeline.flow.service")


async def _browser_at_login_wall(browser: Any) -> bool:
    """Require a browser redirect, not an API error, as login evidence."""
    try:
        page = await browser.page()
        url = urlsplit(str(page.url or ""))
        return url.hostname == "accounts.google.com" or (
            url.hostname == "flow.google.com"
            and any(part in {"about", "login", "signin"} for part in url.path.split("/"))
        )
    except Exception:
        return False


class FlowAuthMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    async def _confirm_logged_in_session(self, page, account_id: str) -> None:
        """Fail closed if this Chrome landed on Google login instead of Flow."""
        url = str(getattr(page, "url", "") or "")
        host = urlsplit(url).hostname or ""
        if host == "accounts.google.com" or "/about" in url:
            raise RuntimeError(
                f"FLOW_COOKIE_EXPIRED: Google session is not signed in ({url})"
            )
        if host == "flow.google.com":
            self._mark_session_ready(account_id)

    async def _confirm_open_session(self, browser, account_id: str, page) -> bool:
        """Connect the account from an open Flow window after proving auth."""
        pages = [page, *[item for item in (getattr(browser.context, "pages", None) or []) if item is not page]]
        flow_page = next((
            item for item in pages
            if urlsplit(str(getattr(item, "url", "") or "")).hostname == "flow.google.com"
            and "/about" not in str(getattr(item, "url", "") or "")
        ), None)
        if flow_page is None:
            return False
        if not await self._persist_browser_cookies(account_id, browser):
            return False
        current = store.get_row("accounts", account_id) or {}
        store.patch_row("accounts", account_id, {
            "status": "online",
            "error": None,
            "resetState": "ready",
            "updatedAt": time.time(),
        })
        self._mark_session_ready(account_id)
        if not (current.get("imageProjectId") or current.get("projectId")):
            try:
                await self._ensure_account_project(flow_page, account_id)
            except Exception:
                pass  # the login itself succeeded; keep the account online
        return True

    async def _seed_browser_cookies(self, account: dict[str, Any], browser: Any) -> bool:
        """Restore missing/rejected native auth without replacing a live session."""
        jar = _cookies_for_fresh_profile(account.get("cookie"))
        if not jar:
            return False
        try:
            live = _live_flow_cookies(await browser.context.cookies())
        except Exception:
            return False
        # ponytail: Runtime profile clones (job Chrome) always need the
        # verified jar.  Their native Cookies DB is a copy that may be stale
        # (Google rotated during login Chrome shutdown) or DBSC-bound to the
        # original Chrome process.  add_cookies only merges; Chrome's native
        # cookie store can still shadow injected values.  Clear all Google/Flow
        # cookies first so the verified jar is the sole authority.
        runtime_clone = "runtime-profiles" in str(getattr(browser, "profile_dir", ""))
        if runtime_clone:
            try:
                await browser.context.clear_cookies()
            except Exception:
                pass
        elif any(cookie["name"] in {"SID", "__Secure-1PSID", "__Secure-3PSID"} for cookie in live):
            return False
        try:
            await browser.context.add_cookies(jar)
        except Exception:
            raise RuntimeError("FLOW_COOKIE_IMPORT_FAILED") from None
        return True

    async def _persist_browser_cookies(self, account_id: str, browser: Any) -> bool:
        """Export only live Google/Flow cookies from this account's verified session."""
        current = store.get_row("accounts", account_id)
        if not current:
            return False
        generation = getattr(browser, "_flow_session_generation", None)
        if isinstance(generation, int) and generation != self._session_generation(account_id):
            return False
        started_at = getattr(browser, "_flow_started_at", None)
        if isinstance(started_at, (int, float)) and started_at < float(current.get("connectedAt") or 0):
            return False
        try:
            context = browser.context
            for page in context.pages:
                url = urlsplit(str(page.url or ""))
                if url.hostname != "flow.google.com" or any(
                    part in {"about", "login", "signin"} for part in url.path.split("/")
                ):
                    continue
                auth = await page.evaluate("""() => ({
                    authenticated: Boolean(window.WIZ_global_data?.SNlM0e ||
                        window.WIZ_global_data?.oPEP7c ||
                        window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email),
                    email: window.WIZ_global_data?.oPEP7c ||
                        window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email || ''
                })""")
                if not isinstance(auth, dict) or auth.get("authenticated") is not True:
                    continue
                expected = str(current.get("email") or "").strip().lower()
                actual = str(auth.get("email") or "").strip().lower()
                if expected and actual and actual != expected:
                    return False
                break
            else:
                return False
            fresh = _live_flow_cookies(await context.cookies())
        except Exception:
            return False
        if not fresh:
            return False
        with self._guard:
            current = store.get_row("accounts", account_id)
            if not current or (isinstance(generation, int) and generation != self._session_generation(account_id)):
                return False
            if isinstance(started_at, (int, float)) and started_at < float(current.get("connectedAt") or 0):
                return False
            # A verified live snapshot replaces stale cookie names; an empty or
            # unverified snapshot above always leaves the last good jar intact.
            store.patch_row("accounts", account_id, {"cookie": fresh, "updatedAt": time.time()})
        return True

    async def prepare_account_cookies(self, account_id: str) -> dict[str, Any]:
        """Check Flow sign-in once and save a verified live cookie snapshot."""
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        label = str(account.get("label") or account_id)
        result = {
            "id": account_id, "label": label, "ok": False, "code": "FLOW_PREPARE_FAILED",
            "cookiesUpdated": 0,
        }
        session_open = any(
            key in self._manual_sessions for key in self._account_session_keys(account_id)
        )
        if session_open or account_id in self._connecting_accounts or self._account_active.get(account_id, 0) > 0:
            # A running job already proved this account. Do not open a second
            # Chrome or rewrite its cookies.
            result.update(ok=True, code="SESSION_IN_USE")
            return result
        # Connect / open-session already proved this process-local session.
        if account.get("status") == "online" and account_id in self._account_session_ready:
            result.update(ok=True, code="SESSION_READY")
            return result
        try:
            return await self._probe_account_cookies(account, result)
        finally:
            if result["code"] == "FLOW_COOKIE_EXPIRED":
                # Same as pressing Connect: open Google sign-in for this account.
                try:
                    self.connect(account_id, headless=False)
                    result["loginOpened"] = True
                except RuntimeError:
                    pass

    async def _probe_account_cookies(
        self, account: dict[str, Any], result: dict[str, Any],
    ) -> dict[str, Any]:
        account_id = str(account["id"])
        browser = None
        try:
            from .browser import BrowserManager, FLOW_BASE_URL, free_local_port
            browser = BrowserManager(
                headless=True,
                profile_dir=store.profile_dir(account_id),
                restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
                remote_debugging_port=free_local_port(),
            )
            browser._flow_session_generation = self._session_generation(account_id)
            browser._flow_started_at = time.time()
            await browser.start()
            seeded = await self._seed_browser_cookies(account, browser)
            page = await browser.page()
            await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=25_000)
            await asyncio.sleep(3)
            if not seeded and await _browser_at_login_wall(browser):
                if await self._seed_browser_cookies(account, browser):
                    await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=25_000)
                    await asyncio.sleep(3)
            url = str(page.url or "")
            host = urlsplit(url).hostname or ""
            login_wall = host == "accounts.google.com" or "/about" in url
            if login_wall:
                # Report the dead session honestly; stored cookies stay untouched.
                if str(account.get("authMethod") or "chrome") == "cookie":
                    self._stop_account_after_cookie_logout(account_id, "FLOW_COOKIE_EXPIRED")
                else:
                    store.patch_row("accounts", account_id, {
                        "status": "reconnect",
                        "error": "FLOW_COOKIE_EXPIRED",
                        "updatedAt": time.time(),
                    })
                self._clear_session_ready(account_id)
                result.update(code="FLOW_COOKIE_EXPIRED")
                return result
            if not await self._persist_browser_cookies(account_id, browser):
                result.update(code="FLOW_COOKIE_SESSION_UNVERIFIED")
                return result
            store.patch_row("accounts", account_id, {
                "status": "online",
                "error": None,
                "updatedAt": time.time(),
            })
            self._mark_session_ready(account_id)
            if not (account.get("imageProjectId") or account.get("projectId")):
                try:
                    await self._ensure_account_project(page, account_id)
                except Exception:
                    pass  # the session is valid; keep the account online
            saved = store.get_row("accounts", account_id) or {}
            result.update(ok=True, code="SESSION_OK", cookiesUpdated=len(saved.get("cookie") or []))
            return result
        except Exception:
            result["code"] = "FLOW_PREPARE_FAILED"
            return result
        finally:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.stop(), timeout=8.0)
                except Exception:
                    pass

    async def _fill_google_login_email(self, page, email: str) -> bool:
        """Submit the saved email once the Google identifier page appears."""
        if not email or urlsplit(str(page.url or "")).hostname != "accounts.google.com":
            return False
        try:
            email_input = page.locator('input[type="email"], input[name="identifier"]').first
            if not await email_input.is_visible():
                return False
            current = (await email_input.input_value()).strip()
            if current and current.casefold() != email.casefold():
                return False
            if not current:
                await email_input.fill(email)
            await page.locator("#identifierNext, button:has-text('Next'), button:has-text('Tiếp theo')").first.click()
            return True
        except Exception:
            return False

    async def _login(self, account_id: str, *, force_interactive: bool = False, cookies: list[dict[str, Any]] | None = None, headless: bool = False, open_ui_on_failure: bool = False, force_visible: bool = False) -> None:
        """Open a visible Chrome window to authenticate and sync the Flow account.

        Authenticate at Flow home without selecting or creating a project.
        Preserve saved project IDs for the generation workers.
        """
        if headless:
            force_interactive = False  # automatic reconnect with 'open Chrome' off: never show a window
        account = store.get_row("accounts", account_id) or {}
        existing_project_id = str(account.get("projectId") or "")
        cookie_auth = bool(cookies) or str(account.get("authMethod") or "chrome") == "cookie"
        if cookies:
            cookies = _account_cookie_records(cookies)  # persistent expiry, see _account_cookie_records
        if not cookies and _seed_stored_cookies({**account, "id": account_id}):
            cookies = _account_cookie_records(account.get("cookie"))
        # Fresh / empty profiles need F12-style SameSite=None; safe to apply whenever injecting.
        if cookies:
            cookies = _cookies_for_fresh_profile(cookies)

        browser = None
        login_ok = False
        with self._guard:
            self._login_passwords.pop(account_id, None)
        password = ""
        # The account-page Connect action starts headless and only requests a
        # visible fallback after the saved session cannot authenticate.
        headless_login = False if (force_visible or force_interactive) else (cookie_auth or headless)
        try:
            from .browser import BrowserManager, FLOW_BASE_URL, free_local_port
            browser = BrowserManager(
                headless=headless_login,
                profile_dir=store.profile_dir(account_id),
                google_login_compat=True,
                fullscreen=True,
                page_zoom=1.0,
                # Cookie import needs session restore so Chrome keeps injected
                # cookies across the first navigation (add-account-via-cookie path).
                restore_last_session=bool(cookies) or cookie_auth,
                remote_debugging_port=free_local_port(),
            )
            browser._flow_session_generation = self._session_generation(account_id)
            browser._flow_started_at = time.time()
            # Linux without a display normalizes visible requests to headless.
            headless_login = browser.headless
            await browser.start()
            seeded = bool(cookies)
            if cookies:
                try:
                    await browser.context.add_cookies(cookies)
                except Exception:
                    # Playwright validation can include cookie values in its error.
                    raise RuntimeError("FLOW_COOKIE_IMPORT_FAILED") from None
            else:
                seeded = await self._seed_browser_cookies(account, browser)
            page = await browser.page()

            # Theo dõi khi user đóng Chrome để thoát loop ngay lập tức
            _browser_closed = False
            def _on_browser_disconnected():
                nonlocal _browser_closed
                _browser_closed = True
            try:
                browser._cdp_browser.on("disconnected", lambda: _on_browser_disconnected())
            except Exception:
                try:
                    browser.context.browser.on("disconnected", lambda: _on_browser_disconnected())
                except Exception:
                    pass

            existing_email = str(account.get("email") or "").strip()
            from urllib.parse import quote

            start_url = "https://flow.google.com/"
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)

            async def _is_flow_authenticated() -> tuple[bool, str]:
                try:
                    url = str(page.url or "")
                    if urlsplit(url).hostname != "flow.google.com" or "/about" in url:
                        return False, ""
                    m = _PROJECT_RE.search(url)
                    # A project URL is a candidate, not proof of login. The
                    # credits request below verifies the session before online.
                    if m:
                        return True, m.group(1)
                    has_auth = await page.evaluate(
                        "() => Boolean(window.WIZ_global_data?.SNlM0e || window.WIZ_global_data?.oPEP7c || window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email)"
                    )
                    if not has_auth:
                        return False, ""
                    m = _PROJECT_RE.search(url)
                    return True, m.group(1) if m else ""
                except Exception:
                    return False, ""

            email_submitted = await self._fill_google_login_email(page, existing_email)

            # Fast-detect: only if already authenticated and project is open
            await asyncio.sleep(2.0)
            is_auth, project_id = await _is_flow_authenticated()
            if not is_auth and not seeded and await _browser_at_login_wall(browser):
                if await self._seed_browser_cookies(account, browser):
                    await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(2.0)
                    is_auth, project_id = await _is_flow_authenticated()

            # Nếu chưa đăng nhập và bị redirect vào accounts.google.com → chờ user login rồi tự navigate về Flow
            if not is_auth:
                if headless_login and not is_auth and (
                    urlsplit(str(page.url or "")).hostname == "accounts.google.com"
                    or "/about" in str(page.url or "")
                ):
                    raise RuntimeError("FLOW_LOGIN_VERIFICATION_REQUIRED")
                if "/about" in str(page.url or ""):
                    # Reuse Google's login hint and return to the Flow lobby.
                    await page.goto(
                        "https://accounts.google.com/ServiceLogin?service=wise"
                        f"&continue={quote(start_url, safe='')}&login_hint={quote(existing_email, safe='')}",
                        wait_until="domcontentloaded", timeout=30_000,
                    )
                elif "accounts.google.com" in str(page.url or ""):
                    # Đã ở trang đăng nhập Google — không cần navigate thêm
                    pass
                elif not is_auth:
                    # Chưa login, thử navigate đến Flow login
                    try:
                        await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass

                # Interactive loop: wait for user to finish signing in in visible Chrome
                deadline = time.monotonic() + (90 if headless_login else 600)
                while time.monotonic() < deadline:
                    try:
                        if _browser_closed or page.is_closed():
                            break  # Chrome đóng bởi user
                        _ = page.url
                    except Exception:
                        break  # Chrome closed by user

                    if not email_submitted:
                        email_submitted = await self._fill_google_login_email(page, existing_email)
                    is_auth, pid = await _is_flow_authenticated()
                    if headless_login and not is_auth and (
                        urlsplit(str(page.url or "")).hostname == "accounts.google.com"
                        or "/about" in str(page.url or "")
                    ):
                        raise RuntimeError("FLOW_LOGIN_VERIFICATION_REQUIRED")
                    if is_auth:
                        # Login verifies the account at the lobby. Project
                        # selection/creation belongs only to generation.
                        break
                    await asyncio.sleep(1.5)

            if not is_auth:
                if headless_login:
                    raise RuntimeError("FLOW_LOGIN_VERIFICATION_REQUIRED")
                # Phân biệt user tắt Chrome vs timeout thực sự
                try:
                    is_closed = _browser_closed or page.is_closed()
                except Exception:
                    is_closed = True
                if is_closed:
                    raise RuntimeError("Chrome bị đóng trước khi đăng nhập xong")
                raise RuntimeError("Login timed out or Chrome was closed before sign-in completed")

            # Connect can finish at the Flow lobby with no project in the URL.
            # The Image/Video account dropdown only lists online accounts that
            # already have a project, so a new connect would succeed and then
            # vanish from "Tài khoản tạo ảnh". Bind a gallery now, but never
            # replace IDs generation already saved.
            project_id = await self._ensure_account_project(page, account_id, project_id)

            # Verify session, capture Bearer (jobs need ya29; credits alone are not enough),
            # then close Chrome before cloning the profile for queued jobs.
            from ._flow._api import FlowAPI
            _log.info("_login: fetching credits for project=%s", project_id)
            login_api = FlowAPI(browser, project_id=project_id or existing_project_id)
            credit_info = await asyncio.wait_for(
                login_api.get_credits(),
                timeout=30.0,
            )
            credits = int(credit_info.credits)
            credits_synced_at = time.time()
            detected_plan = _detect_plan(credit_info)
            email = getattr(credit_info, "email", "")
            if not email:
                email = await page.evaluate(
                    "() => window.WIZ_global_data?.oPEP7c || window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email || ''"
                )

            access_token = ""
            try:
                # Soft capture only — Flow SPA often hides ya29 from CDP. Credits
                # + project page already prove the Google session; do not fail
                # connect (that caused Chrome open/refresh loops).
                access_token = await login_api._get_bearer_token(required=False)
            except Exception as bearer_exc:
                _log.debug("_login: optional Bearer capture skipped: %s", bearer_exc)

            now = time.time()
            patch: dict[str, Any] = {
                "status": "online",
                "resetState": "ready",
                "resetAttemptCount": 0,
                "email": email or (store.get_row("accounts", account_id) or {}).get("email", ""),
                "credits": credits,
                "creditsSyncedAt": credits_synced_at,
                "planStatus": "verified" if detected_plan else "unknown",
                "planSource": "flow_credits_api" if detected_plan else "",
                "planSyncedAt": credits_synced_at,
                "flowTier": str(getattr(credit_info, "tier", "") or "") if credit_info else "",
                "flowSku": str(getattr(credit_info, "sku", "") or "") if credit_info else "",
                "flowServiceTier": str(getattr(credit_info, "service_tier", "") or "") if credit_info else "",
                "connectedAt": now,
                "updatedAt": now,
                "error": None,
            }
            stored_row = store.get_row("accounts", account_id) or {}
            if project_id:
                if not str(stored_row.get("projectId") or "").strip():
                    patch["projectId"] = project_id
                if not str(stored_row.get("imageProjectId") or "").strip():
                    patch["imageProjectId"] = project_id
            if email and not str(stored_row.get("email") or "").strip():
                # Cookie accounts may be added without an email; adopt the one Flow reports.
                patch["label"] = str(email).split("@", 1)[0].strip() or stored_row.get("label")
            if access_token and str(access_token).startswith("ya29."):
                patch["accessToken"] = access_token
                patch["accessTokenAt"] = now
            if detected_plan:
                patch["plan"] = detected_plan
            # Persist the verified browser jar before publishing ``online``.
            # A later reset/open-session depends on this canonical source.
            if not await self._persist_browser_cookies(account_id, browser):
                raise RuntimeError("FLOW_COOKIE_SAVE_FAILED: Could not save Google cookies after sign-in")
            login_ok = True
        except Exception as exc:
            login_ok = False
            detail = str(exc).replace(password, "[redacted]") if password else str(exc)
            login_wall = await _browser_at_login_wall(browser)
            cookie_expired = bool(cookie_auth and login_wall)
            if cookie_expired:
                self._stop_account_after_cookie_logout(account_id, "FLOW_COOKIE_EXPIRED")
            store.patch_row("accounts", account_id, {
                "status": "reconnect",
                "resetState": "action_required",
                "error": "FLOW_COOKIE_EXPIRED" if cookie_expired else detail,
                "updatedAt": time.time(),
            })
            self._log("error", "account_connect_failed", account_id=account_id, message=detail)
        finally:
            password = ""
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.stop(), timeout=8.0)
                except Exception:
                    login_ok = False
                    store.patch_row("accounts", account_id, {
                        "status": "reconnect", "resetState": "action_required",
                        "error": "FLOW_PROFILE_BUSY", "updatedAt": time.time(),
                    })
            try:
                from pipeline.core.desktop_window import request_desktop_foreground

                request_desktop_foreground()
            except Exception:
                pass
            # Clone only after Chrome released the profile — otherwise cookies are incomplete
            # and every job raises "Could not obtain Bearer token" in a loop.
            if login_ok:
                for kind in ("image", "video"):
                    shutil.rmtree(self._session_profile_dir(account_id, kind), ignore_errors=True)
                if account.get("resetState") == "reconnecting":
                    # Keep admission closed until the promised post-login delay.
                    await asyncio.sleep(_soft_retry_backoff_s(1))
                store.patch_row("accounts", account_id, patch)
                self._mark_session_ready(account_id)
                self._log("success", "account_connected", account_id=account_id,
                          details={"projectId": project_id, "credits": credits,
                                   "plan": detected_plan, "bearer": bool(access_token)})
            with self._guard:
                self._connecting_accounts.discard(account_id)
                self._connecting_started_at.pop(account_id, None)
            self._wake_account_waiters(account_id)
            if login_ok:
                self._requeue_auth_failed_jobs(account_id)
        # Automatic/headless recovery must never turn an expired cookie into a
        # hidden interactive login wait. Manual Connect may still request a
        # visible fallback explicitly.
        open_window = headless_login and not login_ok and open_ui_on_failure
        if open_window:
            self._log("warning", "headless_reconnect_open_ui", account_id=account_id,
                      message="Headless reconnect failed; opening Chrome")
            with self._guard:
                self._connecting_accounts.add(account_id)
            store.patch_row("accounts", account_id, {"status": "connecting", "error": None, "updatedAt": time.time()})
            await self._login(account_id, force_interactive=True, headless=False, force_visible=True)
