"""Account metadata, suspension rules, credit sync, and account selection."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
import time
import uuid
from typing import Any

from . import store
from .auth import _browser_at_login_wall
from .constants import (
    _COOLDOWN_CREDITS_S,
    _COOLDOWN_QUOTA_S,
    _COOLDOWN_RATELIMIT_S,
    _DURATION_UNITS,
    _FLOW_CONTROL_SELECTOR,
    _FLOW_PLANS,
    _PLAN_SYNC_TTL_S,
    _TERMINAL,
)
from .controls import (
    _disable_flow_agent_mode,
    _dismiss_flow_whats_new,
    _flow_control_is_selected,
    _mode_tab_icon,
)
from .errors import _collapse_flow_login_error, _is_automation_block_error, _session_needs_login
from .models import (
    _capability_entry,
    _clean_flow_model_name,
    _detect_plan,
    _match_model_choice,
    _official_flow_video_cost,
)
from .projects import _flow_project_id

_log = logging.getLogger("pipeline.flow.service")


class FlowAccountsMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    @staticmethod
    def _public_account(account: dict[str, Any] | None) -> dict[str, Any]:
        """Return account metadata safe to send to the browser/API client."""
        public = dict(account or {})
        public.pop("password", None)
        public.pop("cookie", None)
        return public

    def accounts(self) -> list[dict[str, Any]]:
        rows = store.list_rows("accounts")
        # A visible login is process-local. Do not leave a stale connecting
        # badge after an app restart; the saved profile remains untouched.
        for row in rows:
            self._migrate_legacy_image_quota_suspension(row)
            account_id = str(row.get("id") or "")
            if row.get("status") == "connecting" and account_id not in self._connecting_accounts:
                patch = {"status": "reconnect", "error": None, "updatedAt": time.time()}
                store.patch_row("accounts", account_id, patch)
                row.update(patch)
        return [self._public_account(row) for row in rows]

    def save_account(self, payload: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        existing = store.get_row("accounts", account_id or "") or {}
        email = str(payload.get("email") or existing.get("email") or "").strip()
        if email and "@" not in email:
            email = f"{email}@gmail.com"
        email_name = email.split("@", 1)[0].strip()
        account_key = account_id or uuid.uuid4().hex[:12]
        row = {
            **existing,
            "id": account_key,
            "label": email_name or str(payload.get("label") or existing.get("label") or f"Flow account {account_key[:4]}").strip(),
            "email": email,
            "authMethod": str(payload.get("authMethod") or existing.get("authMethod") or "chrome"),
            "plan": str(payload.get("plan") or existing.get("plan") or "Free"),
            "projectId": str(payload.get("projectId") or existing.get("projectId") or "").strip(),
            "status": existing.get("status", "reconnect"),
            "credits": existing.get("credits"),
            "creditsSyncedAt": existing.get("creditsSyncedAt"),
            "planStatus": existing.get("planStatus", "unknown"),
            "planSource": existing.get("planSource", ""),
            "planSyncedAt": existing.get("planSyncedAt"),
            "flowTier": existing.get("flowTier", ""),
            "flowSku": existing.get("flowSku", ""),
            "flowServiceTier": existing.get("flowServiceTier", ""),
            "capabilityCatalog": existing.get("capabilityCatalog"),
            "capabilityStatus": existing.get("capabilityStatus", "unknown"),
            "capabilitySyncedAt": existing.get("capabilitySyncedAt"),
            "capabilityError": existing.get("capabilityError", ""),
            "suspendedUntil": existing.get("suspendedUntil"),
            "suspendReason": existing.get("suspendReason"),
            "isDefault": bool(payload.get("isDefault", existing.get("isDefault", not self.accounts()))),
            "createdAt": existing.get("createdAt", now),
            "updatedAt": now,
        }
        if row["isDefault"]:
            for account in self.accounts():
                if account["id"] != row["id"]:
                    store.patch_row("accounts", account["id"], {"isDefault": False})
        row.pop("password", None)
        saved = store.put_row("accounts", row)
        return self._public_account(saved)

    @staticmethod
    def _model_suspension_key(kind: str, model: str) -> str:
        return f"{str(kind or '').strip().lower()}:{str(model or '').strip()}"

    def _migrate_legacy_image_quota_suspension(self, account: dict[str, Any]) -> dict[str, Any]:
        """Move old Pro image quota cooldowns off the whole account.

        Older versions stored every quota cooldown in ``suspendedUntil``.  A
        Pro image fallback already records the affected model, so preserve its
        remaining cooldown under a model key and release the account-wide
        quarantine.  This is intentionally idempotent for existing JSON rows.
        """
        event = account.get("lastModelFallback")
        reason = str(account.get("suspendReason") or "")
        if not isinstance(event, dict) or str(event.get("fromModel") or "") != "Nano Banana Pro":
            return account
        if str(event.get("toModel") or "") != "Nano Banana 2.1" or not re.search(
            r"FLOW_QUOTA_EXHAUSTED|quota|hạn mức|usage limit|generation limit|daily limit|monthly limit",
            reason,
            re.I,
        ):
            return account
        account_id = str(account.get("id") or "")
        if not account_id:
            return account
        try:
            until = float(account.get("suspendedUntil") or 0)
        except (TypeError, ValueError):
            until = 0.0
        suspensions = dict(account.get("modelSuspensions") or {})
        if until > time.time():
            suspensions.setdefault(self._model_suspension_key("image", "Nano Banana Pro"), {
                "until": until,
                "reason": reason,
            })
        patch = {
            "suspendedUntil": None,
            "suspendReason": None,
            "suspensionScope": None,
            "lastModelFallback": None,
            "modelSuspensions": suspensions,
            "updatedAt": time.time(),
        }
        store.patch_row("accounts", account_id, patch)
        account.update(patch)
        return account

    def _active_model_suspension(self, account: dict[str, Any], kind: str, model: str) -> dict[str, Any] | None:
        key = self._model_suspension_key(kind, model)
        entry = (account.get("modelSuspensions") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        try:
            if float(entry.get("until") or 0) > time.time():
                return entry
        except (TypeError, ValueError):
            pass
        return None

    def suspend_model(
        self,
        account_id: str,
        kind: str,
        model: str,
        reason: str,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Quarantine one Flow model while leaving the account usable otherwise."""
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        if duration_seconds is None:
            duration_seconds = _COOLDOWN_QUOTA_S
        if not duration_seconds:
            return self._public_account(account)
        until = time.time() + duration_seconds
        suspensions = dict(account.get("modelSuspensions") or {})
        suspensions[self._model_suspension_key(kind, model)] = {
            "until": until,
            "reason": str(reason or "").strip(),
        }
        store.patch_row("accounts", account_id, {
            "modelSuspensions": suspensions,
            "updatedAt": time.time(),
        })
        self._log(
            "warning",
            "model_suspended",
            account_id=account_id,
            message=(
                f"Model {model} của tài khoản bị tạm cách ly đến "
                f"{time.strftime('%H:%M %d/%m', time.localtime(until))} / "
                f"Flow model {model} quarantined until {time.strftime('%H:%M %d/%m', time.localtime(until))}"
            ),
            details={"kind": kind, "model": model, "until": until, "reason": reason},
        )
        return self._public_account(store.get_row("accounts", account_id))

    def clear_model_suspension(self, account_id: str, kind: str, model: str) -> dict[str, Any]:
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        suspensions = dict(account.get("modelSuspensions") or {})
        suspensions.pop(self._model_suspension_key(kind, model), None)
        store.patch_row("accounts", account_id, {"modelSuspensions": suspensions, "updatedAt": time.time()})
        return self._public_account(store.get_row("accounts", account_id))

    def suspend_account(
        self,
        account_id: str,
        reason: str,
        duration_seconds: float | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Tạm cách ly tài khoản trong một khoảng thời gian dài."""
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        if duration_seconds is None:
            r = str(reason)
            # Unusual activity is per-Chrome (each job has its own profile) and
            # is retried in a fresh Chrome; it must not quarantine the account.
            if re.search(r"FLOW_QUOTA_EXHAUSTED|quota|hết lượt|hạn mức|usage limit|generation limit|daily limit|monthly limit", r, re.I):
                duration_seconds = _COOLDOWN_QUOTA_S
            elif re.search(r"HTTP\s*429|rate.?limit|too many requests", r, re.I):
                duration_seconds = _COOLDOWN_RATELIMIT_S
            elif re.search(r"FLOW_CREDITS_EMPTY|CREDITS_EMPTY|out of credits|hết tín|insufficient credit", r, re.I):
                duration_seconds = _COOLDOWN_CREDITS_S
            elif re.search(r"GENERATION_REJECTED|FLOW_GENERATION_REJECTED", r, re.I):
                return self._public_account(account)  # content/prompt issue — not an account fault, no suspension
            elif re.search(r"LOGIN_REQUIRED|FLOW_LOGIN_REQUIRED", r, re.I):
                return self._public_account(account)  # session issue — _ensure_shared_reconnect() owns this
            else:
                return self._public_account(account)  # unrecognised automation error — do not penalise account
        if not duration_seconds:
            return self._public_account(account)  # caller passed 0 explicitly
        until = time.time() + duration_seconds
        patch = {
            "suspendedUntil": until,
            "suspendReason": str(reason or "").strip(),
            "suspensionScope": str(scope or "account").strip() or "account",
            "updatedAt": time.time(),
        }
        store.patch_row("accounts", account_id, patch)
        label = account.get("label", account_id)
        until_str = time.strftime("%H:%M %d/%m", time.localtime(until))
        # Extract the short code prefix (e.g. "FLOW_AUTOMATION_BLOCKED") from reason
        code = str(reason or "").split(":")[0].strip() or "SUSPENDED"
        self._log(
            "warning",
            "account_suspended",
            account_id=account_id,
            message=(
                f"Tài khoản {label} bị tạm cách ly đến {until_str} ({code}) / "
                f"Account {label} quarantined until {until_str} ({code})"
            ),
            details={"suspendedUntil": until, "reason": reason, "duration": duration_seconds},
        )
        return self._public_account(store.get_row("accounts", account_id))

    def clear_account_suspension(self, account_id: str) -> dict[str, Any]:
        """Gỡ bỏ đánh dấu cách ly tài khoản."""
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        store.patch_row(
            "accounts",
            account_id,
            {
                "suspendedUntil": None,
                "suspendReason": None,
                "suspensionScope": None,
                "updatedAt": time.time(),
            },
        )
        self._log(
            "info",
            "account_unsuspended",
            account_id=account_id,
            message=(
                f"Đã gỡ tạm cách ly cho tài khoản {account.get('label', account_id)} / "
                f"Suspension cleared for account {account.get('label', account_id)}"
            ),
        )
        return self._public_account(store.get_row("accounts", account_id))

    def delete_account(self, account_id: str) -> bool:
        if any(key in self._manual_sessions for key in self._account_session_keys(account_id)):
            raise RuntimeError("FLOW_PROFILE_BUSY")
        if any(job.get("accountId") == account_id and job.get("status") not in _TERMINAL for job in self.jobs()):
            raise RuntimeError("Account still has active Flow jobs")
        removed = store.delete_row("accounts", account_id)
        if removed:
            with self._guard:
                self._login_passwords.pop(account_id, None)
            shutil.rmtree(store.root() / "profiles" / account_id, ignore_errors=True)
        return removed

    def _account_quota_suspended(self, account_id: str, kind: str | None = None, model: str | None = None) -> bool:
        """Return true while Flow has told us this account reached its quota."""
        account = store.get_row("accounts", account_id) or {}
        self._migrate_legacy_image_quota_suspension(account)
        if kind == "image" and model:
            return bool(self._active_model_suspension(account, kind, model))
        suspended_until = account.get("suspendedUntil")
        if kind == "image" and str(account.get("suspensionScope") or "account") == "video":
            return False
        try:
            return bool(suspended_until and float(suspended_until) > time.time()) and bool(
                re.search(r"FLOW_QUOTA_EXHAUSTED|quota|hạn mức|usage limit|generation limit|daily limit|monthly limit", str(account.get("suspendReason") or ""), re.I)
            )
        except (TypeError, ValueError):
            return False

    def _account_suspended(self, account_id: str, kind: str | None = None, model: str | None = None) -> bool:
        """Return true while any account-level Flow cooldown is active."""
        account = store.get_row("accounts", account_id) or {}
        self._migrate_legacy_image_quota_suspension(account)
        if kind == "image" and model and self._active_model_suspension(account, kind, model):
            return True
        # Credits are a video-only constraint. Keep the account usable for
        # image generation while the video credit cooldown is active.
        reason = str(account.get("suspendReason") or "")
        if re.search(r"FLOW_CREDITS_EMPTY|CREDITS_EMPTY|out of credits|hết tín|insufficient credit", reason, re.I):
            return False
        # Quota belongs to the media/model that exhausted it. A video quota
        # must not block image work, and image quotas are stored per model.
        if kind == "image" and str(account.get("suspensionScope") or "account") == "video" and re.search(
            r"FLOW_QUOTA_EXHAUSTED|quota|hạn mức|usage limit|generation limit|daily limit|monthly limit",
            reason,
            re.I,
        ):
            return False
        # A visible Playwright session is the user's own verified Flow session.
        # Reuse it for automation cooldowns (the cooldown was caused by the
        # isolated browser path), while retaining quota suspensions.
        has_manual = any(key in self._manual_sessions for key in self._account_session_keys(account_id))
        has_user_session = any(
            key in self._manual_sessions and key not in self._auto_close_sessions
            for key in self._account_session_keys(account_id)
        )
        if has_manual and has_user_session:
            reason = str(account.get("suspendReason") or "")
            if re.search(r"FLOW_AUTOMATION_BLOCKED|FLOW_GENERATION_TIMEOUT", reason, re.I):
                return False
        try:
            return bool(account.get("suspendedUntil") and float(account["suspendedUntil"]) > time.time())
        except (TypeError, ValueError):
            return False

    def _account_suspension_error(
        self,
        account: dict[str, Any],
        kind: str | None = None,
        model: str | None = None,
    ) -> str:
        if kind and model:
            model_entry = self._active_model_suspension(account, kind, model)
            if model_entry:
                try:
                    until_text = time.strftime("%H:%M %d/%m", time.localtime(float(model_entry.get("until"))))
                except (TypeError, ValueError, OverflowError):
                    until_text = "later"
                return (
                    f"FLOW_QUOTA_EXHAUSTED: Model {model} đang tạm cách ly đến {until_text} / "
                    f"Flow model {model} is temporarily quarantined until {until_text}."
                )
        reason = str(account.get("suspendReason") or "").strip()
        code = reason.split(":", 1)[0].strip() or "FLOW_ACCOUNT_COOLDOWN"
        until = account.get("suspendedUntil")
        try:
            until_text = time.strftime("%H:%M %d/%m", time.localtime(float(until)))
        except (TypeError, ValueError, OverflowError):
            until_text = "later"
        if code == "FLOW_AUTOMATION_BLOCKED":
            return (
                f"FLOW_AUTOMATION_BLOCKED: Tài khoản Flow đang tạm cách ly đến {until_text}; "
                "hãy chờ cooldown rồi chạy thủ công trực tiếp trên Flow."
            )
        if code == "FLOW_QUOTA_EXHAUSTED":
            return f"FLOW_QUOTA_EXHAUSTED: Tài khoản Flow đã đạt hạn mức sử dụng đến {until_text}."
        return f"{code}: Tài khoản Flow đang tạm cách ly đến {until_text}."

    async def sync_credits_for_account(self, account_id: str) -> dict[str, Any]:
        """Fetch fresh credits from Google Flow using the existing browser profile.

        Uses a headless Chrome session with the already-saved login profile — no
        interactive sign-in needed.  Returns the updated account row.
        """
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        # Credits are account-wide; prefer the legacy/image project and fall
        # back to the dedicated video project for accounts created by video.
        project_id = (
            _flow_project_id(account, "image", legacy_for_video=True)
            or _flow_project_id(account, "video")
        )
        if account.get("status") != "online" or not project_id:
            raise RuntimeError("FLOW_LOGIN_REQUIRED: account must be connected before syncing credits")
        # Guard: skip if Chrome is already open for this account (connect in progress)
        with self._guard:
            if any(key in self._manual_sessions for key in self._account_session_keys(account_id)):
                raise RuntimeError("FLOW_PROFILE_BUSY")
            if account_id in self._connecting_accounts:
                _log.info("sync_credits_for_account: skip %s — connect in progress", account_id)
                return self._public_account(account)
            if account_id in self._syncing_accounts:
                _log.info("sync_credits_for_account: skip %s — already syncing", account_id)
                return self._public_account(account)
            self._syncing_accounts.add(account_id)
        from ._flow._api import FlowAPI
        from ._flow._exceptions import AuthError as FlowAuthError
        from .browser import BrowserManager, FLOW_BASE_URL, free_local_port
        browser = BrowserManager(
            headless=True,
            profile_dir=store.profile_dir(account_id),
            restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
            remote_debugging_port=free_local_port(),
        )
        browser._flow_session_generation = self._session_generation(account_id)
        browser._flow_started_at = time.time()
        try:
            try:
                await browser.start()
            except Exception as start_exc:
                # SingletonLock: previous Chrome left a stale lock file. Clean it
                # and retry once — only safe when the original process is gone.
                if "ProcessSingleton" in str(start_exc) or "SingletonLock" in str(start_exc):
                    singleton_lock = store.profile_dir(account_id) / "SingletonLock"
                    if singleton_lock.exists():
                        _log.warning("sync: removing stale SingletonLock for %s", account_id)
                        try:
                            singleton_lock.unlink()
                        except OSError:
                            pass
                    browser = BrowserManager(
                        headless=True,
                        profile_dir=store.profile_dir(account_id),
                        restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
                        remote_debugging_port=free_local_port(),
                    )
                    browser._flow_session_generation = self._session_generation(account_id)
                    browser._flow_started_at = time.time()
                    await browser.start()  # raises if still fails
                else:
                    raise
            await self._seed_browser_cookies(account, browser)
            page = await browser.page()
            if project_id not in str(getattr(page, "url", "") or ""):
                await page.goto(
                    f"{FLOW_BASE_URL}/project/{project_id}",
                    wait_until="domcontentloaded", timeout=30_000,
                )
            if await _browser_at_login_wall(browser):
                raise RuntimeError("FLOW_COOKIE_EXPIRED: Flow redirected to the Google login wall")
            api = FlowAPI(browser, project_id=project_id)
            _log.info("sync_credits_for_account: fetching credits project=%s account=%s", project_id, account_id)
            credit_info = await asyncio.wait_for(api.get_credits(), timeout=30.0)
            detected_plan = _detect_plan(credit_info)
            capability_catalog = None
            capability_error = ""
            try:
                capability_catalog = await asyncio.wait_for(
                    self._read_capability_catalog(browser, project_id, detected_plan), timeout=45.0,
                )
            except Exception as catalog_exc:
                capability_error = str(catalog_exc)
                _log.warning("Flow capability sync failed for %s: %s", account_id, catalog_exc)
            patch: dict[str, Any] = {
                "credits": int(credit_info.credits),
                "creditsSyncedAt": time.time(),
                "planStatus": "verified" if detected_plan else "unknown",
                "planSource": "flow_credits_api",
                "planSyncedAt": time.time(),
                "flowTier": str(getattr(credit_info, "tier", "") or ""),
                "flowSku": str(getattr(credit_info, "sku", "") or ""),
                "flowServiceTier": str(getattr(credit_info, "service_tier", "") or ""),
                "updatedAt": time.time(),
                "status": "online",
                "error": None,
                "suspendedUntil": None,
                "suspendReason": None,
            }
            if capability_catalog:
                patch.update({
                    "capabilityCatalog": capability_catalog,
                    "capabilityStatus": "verified",
                    "capabilitySyncedAt": capability_catalog["syncedAt"],
                    "capabilityError": "",
                })
            else:
                patch.update({
                    "capabilityStatus": "stale" if account.get("capabilityCatalog") else "unknown",
                    "capabilityError": capability_error,
                })
            if getattr(credit_info, "email", ""):
                patch["email"] = credit_info.email
            if detected_plan:
                patch["plan"] = detected_plan
            else:
                patch["plan"] = "Free"
            # Credits/capabilities were read from the verified Flow page. Save
            # the final live cookie snapshot before publishing the account as
            # online so any rotation during sync is retained.
            if not await self._persist_browser_cookies(account_id, browser):
                raise RuntimeError("FLOW_COOKIE_SESSION_UNVERIFIED: live Flow cookies were not verified")
            store.patch_row("accounts", account_id, patch)
            self._log(
                "info",
                "account_plan_verified" if detected_plan else "account_plan_unknown",
                account_id=account_id,
                details={
                    "plan": detected_plan,
                    "tier": str(getattr(credit_info, "tier", "") or ""),
                    "sku": str(getattr(credit_info, "sku", "") or ""),
                    "serviceTier": str(getattr(credit_info, "service_tier", "") or ""),
                    "credits": int(credit_info.credits),
                },
            )
        except Exception as exc:
            if (isinstance(exc, FlowAuthError) or _session_needs_login(exc)) and await _browser_at_login_wall(browser):
                # Token expired or cookies invalid — mark as needing reconnect.
                _log.warning("sync_credits_for_account: auth error for %s: %s", account_id, exc)
                if str(account.get("authMethod") or "chrome") == "cookie":
                    self._stop_account_after_cookie_logout(account_id, "FLOW_COOKIE_EXPIRED")
                else:
                    store.patch_row("accounts", account_id, {
                        "status": "reconnect",
                        "resetState": "action_required",
                        "error": "FLOW_COOKIE_EXPIRED",
                        "updatedAt": time.time(),
                    })
            raise
        finally:
            with self._guard:
                self._syncing_accounts.discard(account_id)
            try:
                await browser.stop()
            except Exception:
                pass
        return self._public_account(store.get_row("accounts", account_id) or account)

    async def _read_capability_catalog(self, browser, project_id: str, plan: str | None = None) -> dict[str, Any]:
        """Read the model/ratio/duration controls actually exposed to this account."""
        from .browser import FLOW_BASE_URL

        page = await browser.page()
        if project_id not in str(page.url or ""):
            await page.goto(
                f"{FLOW_BASE_URL}/project/{project_id}",
                wait_until="domcontentloaded", timeout=30_000,
            )
        await _dismiss_flow_whats_new(page)
        await _disable_flow_agent_mode(page)
        await page.wait_for_selector(".settings-trigger-button", state="visible", timeout=15_000)
        controls = page.locator(_FLOW_CONTROL_SELECTOR)
        model_credit_costs: dict[str, int] = {}

        async def model_selector():
            selectors = page.locator(
                'button:has(.model-select-trigger-content), '
                'button[aria-haspopup="menu"]'
            )
            for index in range(await selectors.count()):
                candidate = selectors.nth(index)
                if await candidate.is_visible():
                    aria_label = await candidate.get_attribute("aria-label") or ""
                    text = (await candidate.inner_text()).strip()
                    if re.search(
                        r"chọn nhóm mô hình|select model|model group|model selector|"
                        r"nano banana|imagen|veo|omni",
                        f"{aria_label} {text}",
                        re.I,
                    ):
                        return candidate
            return None

        async def ensure_panel() -> None:
            if await model_selector() is not None:
                return
            triggers = page.locator(".settings-trigger-button")
            for index in range(await triggers.count()):
                candidate = triggers.nth(index)
                if await candidate.is_visible():
                    await candidate.click(force=True)
                    await asyncio.sleep(0.5)
                    break
            if await model_selector() is None:
                raise RuntimeError("FLOW_CAPABILITY_SYNC_FAILED: model selector was not found")

        async def switch_kind(kind: str) -> None:
            await ensure_panel()
            icon = _mode_tab_icon(kind)
            target = None
            for index in range(await controls.count()):
                candidate = controls.nth(index)
                if not await candidate.is_visible():
                    continue
                text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip()).lower()
                matches_kind = (
                    text.startswith(icon)
                    or (kind == "image" and bool(re.search(r"\bimage\b|hình ảnh", text)))
                    or (kind == "video" and bool(re.search(r"\bvideo\b", text)))
                )
                if matches_kind:
                    target = candidate
                    break
            if target is None:
                raise RuntimeError(f"FLOW_CAPABILITY_SYNC_FAILED: {kind} mode was not found")
            if not await _flow_control_is_selected(target):
                await target.click(force=True)
                await asyncio.sleep(0.5)
            await ensure_panel()

        async def current_model_name() -> str:
            selector = await model_selector()
            return _clean_flow_model_name(await selector.inner_text()) if selector is not None else ""

        async def list_model_names() -> list[str]:
            selector = await model_selector()
            if selector is None:
                return []
            await selector.click(force=True)
            await asyncio.sleep(0.25)
            result: list[str] = []
            options = page.locator('[role="menuitem"], [role="option"]')
            for index in range(await options.count()):
                option = options.nth(index)
                if not await option.is_visible() or await option.is_disabled():
                    continue
                label = option.locator(".label")
                raw_name = await label.inner_text() if await label.count() else await option.inner_text()
                name = _clean_flow_model_name(raw_name)
                if name and name not in result:
                    result.append(name)
                # Some Flow builds expose the authoritative price beside the
                # model name in the menu, while the tRPC price catalogue is
                # unavailable. Preserve that UI value for the account JSON.
                option_text = re.sub(r"\s+", " ", await option.inner_text()).strip()
                cost_match = re.search(r"(?<!\d)(\d{1,4})\s*(?:credits?|t[ií]n\s*d[uụ]ng)(?!\w)", option_text, re.I)
                if name and cost_match:
                    model_credit_costs[name] = int(cost_match.group(1))
            return result

        async def select_model(name: str) -> None:
            selector = await model_selector()
            if selector is None:
                raise RuntimeError("FLOW_CAPABILITY_SYNC_FAILED: model selector disappeared")
            if _match_model_choice(name, await selector.inner_text()):
                return
            options = page.locator('[role="menuitem"], [role="option"]')
            if not any([await options.nth(index).is_visible() for index in range(await options.count())]):
                await selector.click(force=True)
                await asyncio.sleep(0.5)
            for index in range(await options.count()):
                option = options.nth(index)
                if not await option.is_visible() or await option.is_disabled():
                    continue
                label = option.locator(".label")
                raw_name = await label.inner_text() if await label.count() else await option.inner_text()
                if _clean_flow_model_name(raw_name) == name:
                    await option.click(force=True)
                    await asyncio.sleep(0.35)
                    return
            await page.keyboard.press("Escape")
            raise RuntimeError(f"FLOW_CAPABILITY_SYNC_FAILED: model disappeared: {name}")

        async def control_snapshot() -> list[tuple[str, bool]]:
            snapshot: list[tuple[str, bool]] = []
            seen_text: set[str] = set()
            for index in range(await controls.count()):
                control = controls.nth(index)
                if not await control.is_visible() or await control.is_disabled():
                    continue
                text = (await control.inner_text()).strip()
                snapshot.append((text, await _flow_control_is_selected(control)))
                seen_text.add(re.sub(r"\s+", " ", text).lower())
            # Omni duration controls have also appeared as plain buttons. Read
            # those explicitly so a capability sync does not erase 6s/8s/10s
            # from the account catalog before a job is submitted.
            duration_controls = page.locator(
                'button, [role="option"], [role="menuitem"], [data-value]'
            ).filter(has_text=re.compile(rf"(?<!\d)\d{{1,3}}\s*{_DURATION_UNITS}(?!\w)", re.I))
            for index in range(await duration_controls.count()):
                control = duration_controls.nth(index)
                if not await control.is_visible() or await control.is_disabled():
                    continue
                text = (await control.inner_text()).strip()
                key = re.sub(r"\s+", " ", text).lower()
                if key not in seen_text:
                    snapshot.append((text, await _flow_control_is_selected(control)))
                    seen_text.add(key)
            return snapshot

        catalog: dict[str, Any] = {
            "version": 1, "source": "flow_ui", "syncedAt": time.time(),
            "pricingPlan": str(plan or "Free"),
        }
        original_kind = "video"
        for index in range(await controls.count()):
            control = controls.nth(index)
            if await control.is_visible() and await _flow_control_is_selected(control):
                text = re.sub(r"\s+", " ", (await control.inner_text()).strip()).lower()
                if text.startswith("image") or "hình ảnh" in text:
                    original_kind = "image"
                    break
                if text.startswith("videocam") or re.search(r"\bvideo\b", text):
                    break

        for kind in ("image", "video"):
            await switch_kind(kind)
            original_model = await current_model_name()
            names = await list_model_names()
            models = []
            for name in names:
                await select_model(name)
                models.append(_capability_entry(name, await control_snapshot()))
            if original_model and any(item["name"] == original_model for item in models):
                await select_model(original_model)
            catalog[kind] = {
                "defaultModel": original_model if any(item["name"] == original_model for item in models) else (models[0]["name"] if models else ""),
                "models": models,
            }
            if kind == "video":
                for entry in models:
                    cost = model_credit_costs.get(str(entry.get("name") or ""))
                    if cost is not None:
                        entry["creditCost"] = cost
                    elif not entry.get("creditCost"):
                        fallback_cost = _official_flow_video_cost(
                            str(entry.get("name") or ""), plan,
                            str(entry.get("defaultDuration") or "8"),
                            str(entry.get("defaultResolution") or "720p"),
                        )
                        if fallback_cost is not None:
                            entry["creditCost"] = fallback_cost
        # Credit cost is account-specific and is not rendered in the model
        # selector. Merge the live API catalog when available; never invent a
        # universal cost for every account/plan.
        try:
            from ._flow._api import FlowAPI
            config = await FlowAPI(browser, project_id=project_id).get_video_model_config()
            # tRPC deployments have returned both the unwrapped payload and
            # result/data/json envelopes. Normalize all observed shapes before
            # reading costs; otherwise the capability catalog silently loses
            # creditCost while the balance itself still syncs successfully.
            payload: Any = config
            for key in ("result", "data", "json"):
                if isinstance(payload, dict) and isinstance(payload.get(key), dict):
                    payload = payload[key]
            video_models = payload.get("videoModels", []) if isinstance(payload, dict) else []
            for entry in catalog.get("video", {}).get("models", []):
                name = str(entry.get("name") or "")
                match = next((item for item in video_models if isinstance(item, dict) and (
                    str(item.get("displayName") or item.get("name") or "") == name
                    or _match_model_choice(name, str(item.get("displayName") or item.get("name") or ""))
                )), None)
                if match and match.get("creditCost") is not None:
                    entry["creditCost"] = int(match["creditCost"])
        except Exception as exc:
            _log.debug("Flow credit-cost catalog unavailable: %s", exc)
        await switch_kind(original_kind)
        if not catalog["image"]["models"] or not catalog["video"]["models"]:
            raise RuntimeError("FLOW_CAPABILITY_SYNC_FAILED: Flow returned an empty model catalog")
        return catalog

    def _pick_eligible_account(
        self,
        kind: str = "image",
        model: str | None = None,
        exclude_ids: set[str] | list[str] | None = None,
        extra_counts: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Pick an online account with balanced round-robin and least workload."""
        exclude_set = {str(x) for x in (exclude_ids or []) if str(x)}
        accounts = store.list_rows("accounts")
        eligible: list[dict[str, Any]] = []
        for acc in accounts:
            self._migrate_legacy_image_quota_suspension(acc)
            acc_id = str(acc.get("id") or "")
            if not acc_id or acc_id in exclude_set:
                continue
            if acc.get("status") != "online" or not _flow_project_id(acc, kind, legacy_for_video=True):
                continue
            suspended_until = acc.get("suspendedUntil")
            if suspended_until:
                try:
                    account_quota = bool(re.search(
                        r"FLOW_QUOTA_EXHAUSTED|quota|hạn mức|usage limit|generation limit|daily limit|monthly limit",
                        str(acc.get("suspendReason") or ""),
                        re.I,
                    ))
                    if float(suspended_until) > time.time() and not (
                        kind == "image" and account_quota and str(acc.get("suspensionScope") or "account") == "video"
                    ):
                        continue
                except (ValueError, TypeError):
                    pass
            if self._active_model_suspension(acc, kind, str(model or "")):
                continue
            credits = acc.get("credits")
            if kind == "video" and isinstance(credits, (int, float)) and int(credits) <= 0:
                continue
            # Gói Free không hỗ trợ tạo ảnh Nano Banana Pro
            if kind == "image" and model == "Nano Banana Pro" and acc.get("plan") == "Free":
                continue
            eligible.append(acc)
        if not eligible:
            return None

        # Stable sort by id for fair deterministic rotation
        eligible.sort(key=lambda a: str(a.get("id") or ""))

        # Count active/queued jobs per account so workload distributes evenly
        job_counts: dict[str, int] = {}
        try:
            job_rows = store.list_rows("jobs")
        except Exception:
            job_rows = []
        for r in job_rows:
            if isinstance(r, dict) and r.get("status") in {"queued", "processing"}:
                a_id = str(r.get("accountId") or "")
                job_counts[a_id] = job_counts.get(a_id, 0) + 1

        extras = extra_counts or {}
        loads = {
            str(a.get("id")): self._account_active.get(str(a.get("id")), 0)
            + job_counts.get(str(a.get("id")), 0)
            + extras.get(str(a.get("id")), 0)
            for a in eligible
        }
        min_load = min(loads.values())
        best_candidates = [a for a in eligible if loads[str(a.get("id"))] == min_load]

        with self._guard:
            rr_index = getattr(self, "_random_rr_counter", 0)
            chosen = best_candidates[rr_index % len(best_candidates)]
            self._random_rr_counter = rr_index + 1
        return chosen

    def _verify_account_plan_before_enqueue(
        self,
        account_id: str,
        kind: str = "video",
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Refresh Flow entitlement before every generation entry point.

        ``enqueue`` is synchronous and is also called by series/automation
        workers, so run the existing async sync routine in a short-lived loop.
        A stale stored plan must never authorize a new job.
        """
        account = store.get_row("accounts", account_id)
        if not account:
            raise ValueError("FLOW_ACCOUNT_NOT_FOUND: Flow account was not found")
        if account.get("status") != "online" or not _flow_project_id(account, kind, legacy_for_video=True):
            # Only an explicit unusual-activity response owns the automatic
            # reconnect lifecycle. Other account/session errors stay scoped to
            # this job and must not take an otherwise-online account offline.
            reason = _collapse_flow_login_error(str(account.get("error") or "FLOW_LOGIN_REQUIRED"))
            if _is_automation_block_error(reason):
                self._ensure_shared_reconnect(account_id, reason=reason)
            raise ValueError(reason)
        # A known zero balance is a deterministic admission failure. Check it
        # before opening a browser/sync attempt so an expired session cannot
        # mask the real user-facing error as FLOW_SESSION_EXPIRED.
        known_credits = account.get("credits")
        if kind == "video" and isinstance(known_credits, (int, float)) and int(known_credits) <= 0:
            raise ValueError(
                "FLOW_CREDITS_EMPTY: Hết tín dụng — nạp thêm hoặc đợi reset (Out of Flow credits)"
            )
        # Keep automatic verification, but do not launch a second browser for
        # every job in a batch. A fresh verified snapshot is safe to reuse for
        # five minutes; expired/unknown snapshots still force a live sync.
        synced_at = float(account.get("creditsSyncedAt") or 0)
        catalog_fresh = account.get("capabilityStatus") == "verified" and bool(account.get("capabilityCatalog"))
        if (
            account.get("planStatus") == "verified"
            and account.get("plan") in _FLOW_PLANS
            and catalog_fresh
            and synced_at > 0
            and time.time() - synced_at < _PLAN_SYNC_TTL_S
        ):
            return account
        try:
            refreshed = asyncio.run(self.sync_credits_for_account(account_id))
        except Exception as exc:
            message = str(exc)
            if "FLOW_SESSION_EXPIRED" in message or _session_needs_login(exc):
                raise ValueError(f"FLOW_SESSION_EXPIRED: {message}") from exc
            raise ValueError(f"FLOW_PLAN_SYNC_FAILED: {message}") from exc
        if refreshed.get("planStatus") != "verified" or refreshed.get("plan") not in _FLOW_PLANS:
            raise ValueError("FLOW_PLAN_UNKNOWN: Không xác định được gói Flow; hãy đồng bộ lại")
        return refreshed

    def sync_all_credits(self) -> list[dict[str, Any]]:
        """Fire-and-forget credit sync for every online account.

        Spawns one background thread per online account and returns immediately
        with the current (pre-sync) account list.  The frontend's normal polling
        will pick up the updated balances on the next GET /accounts or /jobs.
        """
        online = [
            a for a in self.accounts()
            if a.get("status") == "online"
            and _flow_project_id(a, "image", legacy_for_video=True)
        ]
        for account in online:
            account_id = account["id"]
            threading.Thread(
                target=lambda aid=account_id: asyncio.run(self.sync_credits_for_account(aid)),
                daemon=True,
                name=f"flow-sync-{account_id}",
            ).start()
        return self.accounts()

    async def _sync_credits(self, api, account_id: str) -> None:
        """Refresh the persisted balance without turning a successful job into a failure."""
        try:
            credit_info = await api.get_credits()
            browser = getattr(api, "_bm", None)
            if browser is not None and not await self._persist_browser_cookies(account_id, browser):
                self._log(
                    "warning", "session_cookie_sync_unverified", account_id=account_id,
                    message="Flow credit sync succeeded but the live cookie snapshot was not verified",
                )
            store.patch_row("accounts", account_id, {
                "credits": int(credit_info.credits),
                "creditsSyncedAt": time.time(),
                "updatedAt": time.time(),
            })
        except Exception:
            # Credit reporting is secondary to generation. A later job or
            # reconnect can refresh it if Google's balance endpoint is busy.
            pass
