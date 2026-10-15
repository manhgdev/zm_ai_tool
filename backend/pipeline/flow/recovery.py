"""Shared account reconnect and bounded session recovery."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from typing import Any

from . import store
from .constants import _ACCOUNT_RECOVERY_MAX, _PROJECT_RE, _TERMINAL
from .errors import (
    _collapse_flow_login_error,
    _confirmed_login_wall_error,
    _is_automation_block_error,
    _is_flow_quota_exhausted,
    _session_needs_login,
)
from .models import _detect_plan
from .projects import _flow_project_field, _flow_project_id

_log = logging.getLogger("pipeline.flow.service")


class FlowRecoveryMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    def _stop_account_after_cookie_logout(self, account_id: str, reason: str) -> int:
        """Stop account jobs when recovery has no verified session to reuse."""
        error = _collapse_flow_login_error(reason or "FLOW_COOKIE_EXPIRED")
        account = store.get_row("accounts", account_id) or {}
        notice = (
            {
                "vi": "Tài khoản Cookie đã đăng xuất — job đã dừng. Cập nhật Cookie rồi bấm Kết nối.",
                "en": "The Cookie account signed out — jobs were stopped. Update the Cookie, then select Connect.",
            }
            if str(account.get("authMethod") or "chrome") == "cookie"
            else {
                "vi": "Không tự khôi phục được phiên Flow — job đã dừng. Hãy bấm Kết nối.",
                "en": "Flow could not recover the session automatically — jobs were stopped. Select Connect.",
            }
        )
        job_ids: set[str] = set()
        for row in store.list_rows("jobs"):
            if str(row.get("accountId") or "") != account_id or row.get("status") in _TERMINAL:
                continue
            job_id = str(row.get("id") or "")
            if job_id:
                job_ids.add(job_id)
            store.patch_row("jobs", job_id, {
                "status": "action_required",
                "stage": "action_required",
                "error": error,
                "recoveryNotice": notice,
                "updatedAt": time.time(),
            })
        with self._account_condition:
            self._cancelled.update(job_ids)
            self._account_condition.notify_all()
        self._kill_job_browsers(job_ids)
        store.patch_row("accounts", account_id, {
            "status": "reconnect",
            "resetState": "action_required",
            "error": error,
            "accessToken": None,
            "accessTokenAt": None,
            "updatedAt": time.time(),
        })
        if job_ids:
            self._log(
                "warning", "cookie_logout_stopped_jobs", account_id=account_id,
                message=f"Stopped {len(job_ids)} Flow jobs after Cookie logout",
            )
        return len(job_ids)

    def _start_cookie_recovery(self, account_id: str, *, job_id: str | None = None, reason: str = "") -> None:
        """Run one bounded recovery operation using the saved Google login."""
        with self._guard:
            lock = self._account_recovery_locks.setdefault(account_id, threading.Lock())
        if not lock.acquire(blocking=False):
            joined_job = store.get_row("jobs", job_id) if job_id else None
            if joined_job and joined_job.get("status") not in _TERMINAL:
                store.patch_row("jobs", job_id, {
                    "stage": "waiting_session_reset",
                    "recoveryNotice": {
                        "vi": "Tài khoản Flow đã đăng xuất — đang tự đăng nhập lại bằng cookie",
                        "en": "Flow account signed out — signing in again with the saved cookie",
                    }, "updatedAt": time.time(),
                })
            self._log("info", "session_recovery_joined", job_id=job_id or "", account_id=account_id,
                      message="Account recovery already running; job joined the shared operation")
            return
        with self._guard:
            reset_lock = self._account_reset_locks.setdefault(account_id, threading.Lock())
        if not reset_lock.acquire(blocking=False):
            lock.release()
            self._log("info", "session_recovery_joined", job_id=job_id or "", account_id=account_id,
                      message="Manual reset already owns the account lifecycle")
            return

        account = store.get_row("accounts", account_id) or {}
        now = time.time()
        attempt = int(account.get("resetAttemptCount") or 0) + 1
        if attempt > _ACCOUNT_RECOVERY_MAX:
            store.patch_row("accounts", account_id, {
                "status": "reconnect", "resetState": "action_required",
                "error": "FLOW_SESSION_EXPIRED: automatic recovery limit reached",
                "resetAttemptCount": attempt, "updatedAt": now,
            })
            for row in store.list_rows("jobs"):
                if str(row.get("accountId") or "") == account_id and row.get("status") not in _TERMINAL:
                    store.patch_row("jobs", str(row.get("id") or ""), {
                        "status": "action_required", "stage": "action_required",
                        "error": "FLOW_SESSION_EXPIRED: automatic recovery limit reached",
                        "recoveryNotice": {
                            "vi": "Không tự khôi phục được phiên Flow",
                            "en": "Flow session could not be recovered automatically",
                        }, "updatedAt": now,
                    })
            lock.release()
            reset_lock.release()
            return
        # The native Chrome profile can still be signed in without a JSON jar.
        # Let reconnect verify it before declaring authentication lost.
        store.patch_row("accounts", account_id, {
            "status": "reconnect", "resetState": "requested",
            "resetAttemptCount": attempt,
            "resetRequestedAt": now, "resetReason": reason or "automatic_cookie_recovery",
            "error": _collapse_flow_login_error(reason or "FLOW_SESSION_EXPIRED"), "updatedAt": now,
        })
        for row in store.list_rows("jobs"):
            if str(row.get("accountId") or "") != account_id or row.get("status") in _TERMINAL:
                continue
            patch = {"recoveryNotice": {
                "vi": "Tài khoản Flow đã đăng xuất — đang tự đăng nhập lại bằng cookie",
                "en": "Flow account signed out — signing in again with the saved cookie",
            }, "updatedAt": now}
            if row.get("status") == "processing":
                patch.update({"stage": "recovering_session"})
            store.patch_row("jobs", str(row.get("id") or ""), patch)
        self._clear_session_ready(account_id)
        self._log("warning", "session_recovery_started", job_id=job_id or "", account_id=account_id,
                  message="Cookie-only Flow session recovery started")

        def run() -> None:
            deferred = False
            try:
                self._reset_account_session_locked(
                    account_id,
                    open_ui_on_failure=False,
                    keep_running_jobs=True,
                )
            except Exception as exc:
                message = str(exc) or "FLOW_SESSION_EXPIRED"
                if "FLOW_PROFILE_BUSY" in message:
                    # A manual/connect/sync owner is still releasing the
                    # profile. Keep the cohort waiting and retry after the
                    # lifecycle lock clears; this is not an auth failure.
                    deferred = True
                    store.patch_row("accounts", account_id, {
                        "status": "reconnect", "resetState": "requested",
                        "resetAttemptCount": max(0, attempt - 1),
                        "error": _collapse_flow_login_error(reason or "FLOW_SESSION_EXPIRED"),
                        "updatedAt": time.time(),
                    })
                    for row in store.list_rows("jobs"):
                        if str(row.get("accountId") or "") != account_id or row.get("status") in _TERMINAL:
                            continue
                        store.patch_row("jobs", str(row.get("id") or ""), {
                            "stage": "waiting_session_reset",
                            "recoveryNotice": {
                                "vi": "Đang chờ phiên Flow hiện tại đóng rồi tự kết nối lại bằng cookie",
                                "en": "Waiting for the current Flow session to close before cookie reconnect",
                            }, "updatedAt": time.time(),
                        })
                    self._log("info", "session_recovery_deferred", job_id=job_id or "", account_id=account_id,
                              message="Flow profile is busy; recovery will retry after the session closes")
                else:
                    store.patch_row("accounts", account_id, {
                        "status": "reconnect", "resetState": "action_required",
                        "error": "FLOW_SESSION_EXPIRED: " + message,
                        "updatedAt": time.time(),
                    })
                    for row in store.list_rows("jobs"):
                        if str(row.get("accountId") or "") == account_id and row.get("status") not in _TERMINAL:
                            store.patch_row("jobs", str(row.get("id") or ""), {
                                "status": "action_required", "stage": "action_required",
                                "error": "FLOW_SESSION_EXPIRED: " + message,
                                "recoveryNotice": {
                                    "vi": "Không tự khôi phục được phiên Flow",
                                    "en": "Flow session could not be recovered automatically",
                                }, "updatedAt": time.time(),
                            })
                    self._log("error", "session_recovery_failed", job_id=job_id or "", account_id=account_id,
                              message=message)
            finally:
                reset_lock.release()
                lock.release()
                self._wake_account_waiters(account_id)
                if deferred:
                    retry = threading.Timer(
                        1.0,
                        self._start_cookie_recovery,
                        kwargs={"account_id": account_id, "job_id": job_id, "reason": reason},
                    )
                    retry.daemon = True
                    retry.start()

        threading.Thread(target=run, daemon=True, name=f"flow-recover-{account_id}").start()

    def _ensure_shared_reconnect(
        self,
        account_id: str,
        *,
        job_id: str | None = None,
        reason: str = "",
        interactive: bool | None = None,
    ) -> None:
        """Start at most one reconnect/login for this account; never block the caller.

        Jobs that hit 401/LOGIN_REQUIRED call this and free their concurrency
        slot immediately. Other queued jobs wait at admission until the account
        is online again — they must not each soft-refresh Chrome.
        """
        if interactive is not True and not _is_automation_block_error(reason) and not _session_needs_login(reason):
            return
        account = store.get_row("accounts", account_id) or {}
        if not account:
            return
        # A repeated auth failure after replay needs an explicit user retry.
        job = (store.get_row("jobs", job_id) or {}) if job_id else {}
        if interactive is not True and job.get("authRetryCount", 0):
            return
        if interactive is not True and _session_needs_login(reason):
            # A confirmed Google login wall cannot be repaired by replaying a
            # saved jar. Keep the account row, stop its cohort, and require a
            # user Connect. An unusual-activity response is different: its
            # still-live cookie goes through the shared same-account reset.
            if not _is_automation_block_error(reason) and _confirmed_login_wall_error(reason):
                self._stop_account_after_cookie_logout(account_id, reason)
                return
            # Logout/unusual-activity recovery uses the same coordinator and
            # saved Google profile. It never opens an interactive login wall.
            self._start_cookie_recovery(account_id, job_id=job_id, reason=reason)
            return
        with self._guard:
            if account_id in self._connecting_accounts:
                return
        # Mark reconnect so admission blocks other workers on this account.
        if account.get("status") == "online":
            store.patch_row("accounts", account_id, {
                "status": "reconnect",
                "error": _collapse_flow_login_error(
                    reason or account.get("error") or "FLOW_LOGIN_REQUIRED"
                ),
                "updatedAt": time.time(),
            })
        # Automatic reconnects must stay in the background. A login wall
        # cannot be repaired without user credentials, so the headless login
        # will fail fast with FLOW_LOGIN_VERIFICATION_REQUIRED instead of
        # unexpectedly opening a Chrome window.
        force_interactive = False
        if interactive is not None:
            force_interactive = bool(interactive)
        try:
            connect_kwargs = {"force_interactive": force_interactive}
            if interactive is not True:
                connect_kwargs["headless"] = True
            if interactive is True:
                connect_kwargs["allow_session_close"] = True
            self.connect(account_id, **connect_kwargs)
        except KeyError:
            return
        except RuntimeError as exc:
            if "FLOW_PROFILE_BUSY" not in str(exc):
                raise
            # Another Flow session is still closing/connecting. Leave the
            # account in reconnect state; the queue admission path will retry
            # once the profile lock is released instead of leaking a traceback.
            self._log(
                "info", "session_reconnect_deferred", job_id=job_id,
                account_id=account_id,
                message="Flow profile is busy; reconnect deferred until the session is free",
            )
            return
        with self._account_condition:
            self._account_condition.notify_all()
        self._log(
            "warning",
            "session_reconnect_started",
            job_id=job_id,
            account_id=account_id,
            message=(
                "Đang mở Chrome đăng nhập lại Flow (chung 1 lần / account) — "
                "các job khác chờ / Opening Chrome to re-login Flow once; other jobs wait"
                if force_interactive else
                "Đang thử kết nối lại phiên Flow (headless) / Attempting Flow headless reconnect"
            ),
        )

    def _wake_account_waiters(self, account_id: str) -> None:
        """Unblock queued jobs waiting on shared account reconnect."""
        _ = account_id
        with self._account_condition:
            self._account_condition.notify_all()

    def _requeue_auth_failed_jobs(self, account_id: str) -> int:
        """After shared login succeeds, resume jobs that failed only on auth."""
        account = store.get_row("accounts", account_id) or {}
        if account.get("status") != "online" or not (
            _flow_project_id(account, "image", legacy_for_video=True)
            or _flow_project_id(account, "video")
        ):
            return 0
        self._wake_account_waiters(account_id)
        restarted = 0
        for job in store.list_rows("jobs"):
            if str(job.get("accountId") or "") != account_id:
                continue
            if job.get("status") != "action_required":
                continue
            job_error = str(job.get("error") or "")
            if not (_session_needs_login(Exception(job_error)) or _is_automation_block_error(job_error)):
                continue
            job_id = str(job.get("id") or "")
            if not job_id:
                continue
            with self._account_condition:
                if job_id in self._running_jobs or job.get("authRetryCount", 0):
                    continue
                # Logout stops workers through this set, but a successful
                # reconnect must release that stop. User-cancelled rows have
                # status=cancelled and were excluded above.
                self._cancelled.discard(job_id)
                store.patch_row("jobs", job_id, {
                    "authRetryCount": 1,
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0,
                    "error": None,
                    "recoveryNotice": None,
                    "updatedAt": time.time(),
                })
            threading.Thread(
                target=self._run_sync,
                args=(job_id,),
                daemon=True,
                name=f"flow-reauth-{job_id}",
            ).start()
            restarted += 1
        if restarted:
            _log.info("Requeued %d auth-failed Flow jobs for account %s", restarted, account_id)
            self._log(
                "success",
                "session_reconnect_ok",
                account_id=account_id,
                message=(
                    f"Đã kết nối lại — chạy lại {restarted} job / "
                    f"Reconnected — retrying {restarted} jobs"
                ),
            )
        return restarted

    def _try_fallback_account(self, job_id: str, failed_account_id: str, reason: str) -> bool:
        """Attempt to fallback a failed/blocked job to another eligible online account."""
        # Completed media belongs to its original account/project. Switching
        # accounts here would regenerate it rather than retry the download.
        existing = store.get_row("jobs", job_id) or {}
        if existing.get("mediaIds") or existing.get("outputs"):
            return False
        job = store.get_row("jobs", job_id)
        if not job or job.get("status") in {"done", "cancelled"} or job_id in self._cancelled:
            return False
        # An unusual-activity response must stay on this account so the
        # shared cookie reset can preserve its project and submission identity.
        if _is_automation_block_error(reason) or not job.get(
            "allowAccountFallback", bool(job.get("randomAccount"))
        ):
            return False

        kind = str(job.get("kind") or "image")
        settings = dict(job.get("settings") or {})
        model = settings.get("model")
        # Quota is scoped to the affected image model. Video keeps its
        # existing account-level cooldown and other account faults retain the
        # normal account fallback behavior.
        if kind == "image" and _is_flow_quota_exhausted(reason):
            self.suspend_model(failed_account_id, kind, str(model or ""), reason)
        elif kind == "video" and _is_flow_quota_exhausted(reason):
            self.suspend_account(failed_account_id, reason, scope="video")
        else:
            # Only suspend for errors that are genuinely account-level faults.
            # GENERATION_REJECTED / unknown reasons already return early in suspend_account.
            self.suspend_account(failed_account_id, reason)
        tried = list(job.get("triedAccountIds") or [failed_account_id])
        if failed_account_id not in tried:
            tried.append(failed_account_id)

        alt = self._pick_eligible_account(kind=kind, model=model, exclude_ids=tried)
        if not alt:
            return False

        alt_id = str(alt.get("id"))
        alt_label = str(alt.get("label") or alt_id)
        next_tried = list(dict.fromkeys(tried + [alt_id]))

        with self._account_condition:
            order = self._account_next_order.get(alt_id)
            if order is None:
                persisted_orders = [
                    int(row.get("queueOrder")) for row in store.list_rows("jobs")
                    if row.get("accountId") == alt_id and str(row.get("queueOrder", "")).isdigit()
                ]
                order = max(persisted_orders, default=-1) + 1
            self._account_next_order[alt_id] = order + 1

        store.patch_row("jobs", job_id, {
            "accountId": alt_id,
            "queueOrder": order,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "error": None,
            "autoRetryCount": 0,
            "generationRejectRetryCount": 0,
            "sessionResetRetryCount": 0,
            "triedAccountIds": next_tried,
            "updatedAt": time.time(),
        })

        self._log(
            "warning",
            "account_fallback",
            job_id=job_id,
            account_id=alt_id,
            message=(
                f"Tự động chuyển sang tài khoản {alt_label} sau sự cố tài khoản {failed_account_id}: {reason} / "
                f"Auto-fallback to account {alt_label} after issue on {failed_account_id}: {reason}"
            ),
        )

        threading.Thread(
            target=self._run_sync,
            args=(job_id,),
            daemon=True,
            name=f"flow-job-{job_id}-fallback",
        ).start()
        return True

    def _stop_account_after_automation_block(self, account_id: str, exclude_job_id: str) -> int:
        """Cancel queued/running work on an account Flow has flagged as automated."""
        stopped = 0
        for row in store.list_rows("jobs"):
            job_id = str(row.get("id") or "")
            if (job_id == exclude_job_id or str(row.get("accountId") or "") != account_id
                    or row.get("status") not in {"queued", "processing"}):
                continue
            self.cancel(job_id)
            stopped += 1
        with self._account_condition:
            self._account_condition.notify_all()
        return stopped

    async def _try_headless_reconnect(
        self,
        account_id: str,
        project_id: str,
        *,
        kind: str = "image",
    ) -> bool:
        """Attempt a quick headless session to verify the saved cookie is still valid.

        Returns True and patches the account to 'online' on success.
        Returns False silently on any failure so the caller falls back to
        the visible interactive flow.

        ponytail: short 30-s timeout; failure is expected and safe.
        """
        from .browser import BrowserManager, FLOW_BASE_URL, free_local_port
        browser = None
        try:
            account = store.get_row("accounts", account_id) or {}
            browser = BrowserManager(
                headless=True,
                profile_dir=store.profile_dir(account_id),
                restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
                remote_debugging_port=free_local_port(),
            )
            browser._flow_session_generation = self._session_generation(account_id)
            browser._flow_started_at = time.time()
            await browser.start()
            await self._seed_browser_cookies(account, browser)
            # Another worker may already have replaced a deleted project while
            # this worker was waiting for the shared browser profile.
            persisted_account = store.get_row("accounts", account_id) or {}
            persisted_project_id = _flow_project_id(
                persisted_account, kind, legacy_for_video=True,
            )
            if persisted_project_id and persisted_project_id != project_id:
                project_id = persisted_project_id
            page = await browser.page()
            target_url = (
                f"https://flow.google.com/project/{project_id}"
                if project_id
                else FLOW_BASE_URL
            )
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
            except Exception as exc:
                # ponytail: one retry only; ERR_ABORTED = redirect/parallel navigation interrupted it.
                if not re.search(r"ERR_ABORTED|frame was detached|interrupted by another navigation", str(exc), re.I):
                    raise
                await asyncio.sleep(1.5)
                page = await browser.page()
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
            # Flow is a SPA: goto() can finish while the address bar still
            # contains the deleted project, then redirect to /404 shortly
            # afterwards. Never accept the stored ID before that redirect has
            # settled.
            await asyncio.sleep(2)
            settled_url = str(page.url or "")
            project_missing = bool(
                project_id
                and ("/404" in settled_url or project_id not in settled_url)
            )

            # Try wait_for_url first (most reliable when session is valid).
            confirmed_id = ""
            match = _PROJECT_RE.search(page.url)
            if match and not project_missing:
                confirmed_id = match.group(1)
            else:
                try:
                    await page.wait_for_url(
                        lambda url: bool(_PROJECT_RE.search(url)),
                        timeout=5_000,
                    )
                    match = _PROJECT_RE.search(page.url)
                    if match and not project_missing:
                        confirmed_id = match.group(1)
                except Exception:
                    pass

            if not confirmed_id:
                # Fallback: give Google a few more seconds then try the lobby link.
                await asyncio.sleep(2)
                match = _PROJECT_RE.search(page.url)
                if match and not project_missing:
                    confirmed_id = match.group(1)
            if not confirmed_id and not project_missing:
                # No deleted-id case: open an existing lobby project if present.
                if "accounts.google.com" not in page.url and "flow.google.com" in page.url:
                    links = await page.locator('a[href*="/project/"]').all()
                    if links:
                        href = await links[0].get_attribute("href") or ""
                        m = _PROJECT_RE.search(href)
                        if m:
                            confirmed_id = m.group(1)

            if not confirmed_id:
                # Missing/deleted saved project → always create a clean replacement
                # so jobs do not land in an unrelated gallery.
                confirmed_id = await self._create_flow_project_ui(page, avoid_project_id=project_id)

            if confirmed_id and confirmed_id != project_id and confirmed_id not in str(page.url or ""):
                # A project link exposes its ID without opening it. Open it
                # before accepting it, otherwise the next job would retain
                # the deleted ID and immediately fail at /404 again.
                await page.goto(
                    f"https://flow.google.com/project/{confirmed_id}",
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
                await asyncio.sleep(2)

            if not confirmed_id or confirmed_id not in str(page.url or ""):
                return False
            current_url = str(page.url or "").lower()
            if re.search(r"accounts\.google\.com|/about|/login|/signin", current_url):
                return False
            authenticated = await page.evaluate("""() => Boolean(
                document.querySelector('flow-root, flow-app, [aria-label*="Flow" i], [data-testid*="flow" i],
                    a[href*="/project/"], button, textarea, input)
                || window.WIZ_global_data?.SNlM0e
                || window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email
            )""")
            if not authenticated:
                return False
            email = await page.evaluate("() => window.WIZ_global_data?.oPEP7c || window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email || ''")
            credits = None
            credits_synced_at = None
            detected_plan = None
            access_token = ""
            try:
                from ._flow._api import FlowAPI
                api = FlowAPI(browser, project_id=confirmed_id)
                credit_info = await api.get_credits()
                credits = int(credit_info.credits)
                credits_synced_at = time.time()
                detected_plan = _detect_plan(credit_info)
                if getattr(credit_info, "email", ""):
                    email = credit_info.email or email
                try:
                    access_token = await api._get_bearer_token(required=False)
                except Exception:
                    access_token = ""
            except Exception:
                pass
            now = time.time()
            account = store.get_row("accounts", account_id) or {}
            project_field = _flow_project_field(kind)
            patch: dict[str, Any] = {
                "status": "online",
                "email": email or account.get("email", ""),
                "credits": credits,
                "creditsSyncedAt": credits_synced_at,
                "connectedAt": now,
                "updatedAt": now,
                "error": None,
            }
            patch[project_field] = confirmed_id
            # ``projectId`` remains the legacy/image project used by old
            # clients. A video reconnect must never replace it.
            if kind == "image":
                patch["projectId"] = confirmed_id
            if access_token and str(access_token).startswith("ya29."):
                patch["accessToken"] = access_token
                patch["accessTokenAt"] = now
            if project_id and confirmed_id != project_id:
                patch["previousProjectId"] = project_id
                changed_at = time.time()
                patch[f"{project_field}ChangedAt"] = changed_at
                if kind == "image":
                    patch["projectChangedAt"] = changed_at
            if detected_plan:
                patch["plan"] = detected_plan
            if not await self._persist_browser_cookies(account_id, browser):
                return False
            store.patch_row("accounts", account_id, patch)
            self._log(
                "success",
                "account_connected",
                account_id=account_id,
                details={
                    "projectId": confirmed_id,
                    "projectKind": kind,
                    "credits": credits,
                    "plan": detected_plan,
                    "headless": True,
                    "bearer": bool(access_token),
                },
            )
            return True
        except Exception as exc:
            _log.debug("Headless reconnect probe failed for %s: %s", account_id, exc)
            return False
        finally:
            if browser is not None:
                try:
                    await browser.stop()
                except Exception:
                    pass

    def _apply_image_quota_fallback(self, job_id: str, error: str) -> None:
        with self._account_condition:
            current = store.get_row("jobs", job_id) or {}
            if not current or current.get("status") == "cancelled" or job_id in self._cancelled:
                return
            account_id = current["accountId"]
            event = {
                "id": uuid.uuid4().hex, "jobId": job_id, "accountId": account_id,
                "randomAccount": bool(current.get("randomAccount")),
                "fromModel": "Nano Banana Pro", "toModel": "Nano Banana 2.1",
                "rawError": error, "createdAt": time.time(),
            }
            notice = {
                "vi": "Nano Banana Pro hết hạn mức — đã chuyển sang Nano Banana 2.1",
                "en": "Nano Banana Pro quota reached — switched to Nano Banana 2.1",
            }
            for row in store.list_rows("jobs"):
                is_current = row["id"] == job_id
                if not is_current and not (
                    row.get("accountId") == account_id and row.get("status") == "queued"
                    and row.get("kind") == "image"
                    and (row.get("settings") or {}).get("model") == "Nano Banana Pro"
                ):
                    continue
                patch = {"settings": {**dict(row.get("settings") or {}), "model": "Nano Banana 2.1"},
                         "fallbackNotice": notice, "updatedAt": time.time()}
                if is_current:
                    patch.update({"quotaModelFallbackApplied": True, "modelFallback": event,
                                  "fallbackNotice": notice,
                                  "status": "processing", "stage": "model_fallback", "error": None})
                store.patch_row("jobs", row["id"], patch)
            # Keep the account online: only the exhausted Pro image model is
            # quarantined.  Nano Banana 2.1 and all video models remain eligible.
            self.suspend_model(account_id, "image", "Nano Banana Pro", error)
            # Retain the event for audit/API compatibility; the account card
            # no longer renders it as a separate Pro-quota warning.
            store.patch_row("accounts", account_id, {"lastModelFallback": event, "updatedAt": time.time()})
            self._log("warning", "image_model_fallback", job_id=job_id, account_id=account_id,
                      message=error, details=event)
