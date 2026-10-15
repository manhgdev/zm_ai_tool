"""Job dispatch, concurrency admission, and the generation worker lifecycle."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from pipeline.core.network import network_available

from . import store
from .constants import (
    _IMAGE_GENERATION_TIMEOUT_S,
    _JOB_AUTO_RETRY_MAX,
    _TERMINAL,
    _UNUSUAL_ACTIVITY_RESET_MAX,
    _VIDEO_GENERATION_TIMEOUT_S,
)
from .downloads import _video_download_quality
from .errors import (
    _FlowNetworkInterrupted,
    _FlowSessionGenerationChanged,
    _classify_visible_flow_error,
    _collapse_flow_login_error,
    _confirmed_login_wall_error,
    _is_automation_block_error,
    _is_flow_infra_interrupt,
    _is_flow_quota_exhausted,
    _network_ready_for_flow_job,
    _session_needs_login,
    _soft_retry_backoff_s,
)
from .models import (
    _catalog_section,
    _ensure_flow_credits_for_job,
    _flow_headless,
    _job_concurrency,
    _normalize_catalog_settings,
    _normalize_video_model,
    _video_ui_duration,
    _video_ui_resolution,
)
from .projects import _flow_project_changed_at, _flow_project_id
from .submission import _prompt_with_reference_strength, _wait_before_flow_submit

_log = logging.getLogger("pipeline.flow.service")


class FlowWorkerMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    def _run_sync(self, job_id: str) -> None:

        job = store.get_row("jobs", job_id)
        if not job:
            return
        with self._account_condition:
            latest = store.get_row("jobs", job_id) or job
            if latest.get("status") in _TERMINAL or job_id in self._running_jobs:
                return
            self._running_jobs.add(job_id)
        account_id = str(job["accountId"])
        account_snapshot = store.get_row("accounts", account_id) or {}
        if (
            account_snapshot.get("status") == "reconnect"
            and _confirmed_login_wall_error(str(account_snapshot.get("error") or ""))
            and not _is_automation_block_error(str(account_snapshot.get("error") or ""))
        ):
            # A confirmed login wall means the account has no reusable signed-in
            # session. Do not let a worker admitted just before that state
            # continue or spin through the queue.
            self._stop_account_after_cookie_logout(
                account_id, str(account_snapshot.get("error") or "FLOW_COOKIE_EXPIRED")
            )
            with self._account_condition:
                self._running_jobs.discard(job_id)
            return
        job_kind = str(job.get("kind") or "video")
        job_model = str((job.get("settings") or {}).get("model") or "")
        generation_token = str(job.get("generationToken") or "")
        order = int(job.get("queueOrder", job.get("inputIndex", 0)))
        with self._account_condition:
            def global_concurrency_limit() -> int:
                # Luồng chạy is a process-wide cap (not per account): N means at
                # most N jobs processing total, whether 1 or many accounts.
                values = [
                    _job_concurrency(row.get("settings") or {})
                    for row in store.list_rows("jobs")
                    if row.get("status") in {"queued", "processing"}
                ]
                return max(values or [_job_concurrency(job.get("settings") or {})])

            def next_queued_order() -> int | None:
                # store.list_rows only — self.jobs() under this lock starves delete_all notify_all.
                pending_orders = [
                    int(row.get("queueOrder")) for row in store.list_rows("jobs")
                    if row.get("accountId") == account_id
                    and row.get("status") == "queued"
                    and str(row.get("id") or "") not in self._cancelled
                    and str(row.get("queueOrder", "")).isdigit()
                ]
                return min(pending_orders) if pending_orders else None

            if account_id not in self._account_next_start:
                first_order = next_queued_order()
                self._account_next_start[account_id] = order if first_order is None else first_order
            while (
                (lambda q, mine: q is not None and mine > q)(
                    next_queued_order(),
                    int((store.get_row("jobs", job_id) or {}).get("queueOrder", order)),
                )  # lowest order first, retries included
                or sum(self._account_active.values()) >= global_concurrency_limit()
                or self._account_session_blocked(account_id)
                or self._session_warmup_wait(account_id)
                or self._account_suspended(account_id, job_kind, job_model)
            ):
                # Long daily/monthly quota: fail the queue. Short rate-limit
                # cooldowns wait here until suspendedUntil elapses.
                if self._account_quota_suspended(account_id, job_kind, job_model):
                    account_snapshot = store.get_row("accounts", account_id) or {}
                    store.patch_row("jobs", job_id, {
                        "status": "failed",
                        "stage": "failed",
                        "error": self._account_suspension_error(account_snapshot, job_kind, job_model),
                        "updatedAt": time.time(),
                    })
                    if order == self._account_next_start[account_id]:
                        self._account_next_start[account_id] += 1
                        self._account_condition.notify_all()
                    self._running_jobs.discard(job_id)
                    return
                if job_id in self._cancelled:
                    if order == self._account_next_start[account_id]:
                        self._account_next_start[account_id] += 1
                        self._account_condition.notify_all()
                    self._running_jobs.discard(job_id)
                    return
                # Deleted/cancelled jobs can leave gaps in queueOrder. Advance
                # to the first real queued row instead of sleeping forever.
                queued_order = next_queued_order()
                if queued_order is not None and self._account_next_start[account_id] != queued_order:
                    self._account_next_start[account_id] = queued_order
                    self._account_condition.notify_all()
                    continue
                # Shared account reconnect in progress — wait; do not start more
                # jobs that would each soft-refresh Bearer / open Chrome.
                self._account_condition.wait(timeout=1.0)
            if job_id in self._cancelled:
                self._account_next_start[account_id] += 1
                self._account_condition.notify_all()
                self._running_jobs.discard(job_id)
                return
            # Remove the admitted row from the queued set before another
            # waiter recalculates the first pending order.
            store.patch_row("jobs", job_id, {
                "status": "processing", "stage": "starting", "progress": 1,
                "sessionGeneration": self._session_generation(account_id),
                "updatedAt": time.time(),
            })
            self._account_next_start[account_id] += 1
            self._account_active[account_id] = self._account_active.get(account_id, 0) + 1
            self._account_condition.notify_all()
        # Retry only failures that happen before a generation is submitted.
        # Once Flow accepts a job, retrying can create a duplicate and charge
        # credits twice; media recovery owns all post-submit timeouts. A
        # RESULT_NOT_FOUND is the exception: Flow has no pending/completed
        # media, so submit a fresh generation automatically.
        # Hard stops only: real credits/quota/quality gates. Auth blips requeue
        # via shared reconnect; FLOW_GENERATION_REJECTED is soft-retried above.
        _HARD_ERROR = re.compile(
            r"FLOW_CREDITS_EMPTY|FLOW_CREDITS_INSUFFICIENT|FLOW_QUOTA_EXHAUSTED|FLOW_DOWNLOAD_QUALITY_UNAVAILABLE",
            re.I,
        )
        profile_ready = False
        try:
            if job_id in self._cancelled:
                return
            # Never launch entitlement sync or Chrome while the machine has no
            # external route.  The old path opened/closed Chrome on every
            # three-second retry during a Wi-Fi outage.
            _network_ready_for_flow_job(self, job_id)
            while True:
                try:
                    verified_account = self._verify_account_plan_before_enqueue(
                        account_id,
                        str(job.get("kind") or "video"),
                        dict(job.get("settings") or {}),
                    )
                except ValueError as verify_exc:
                    if _network_ready_for_flow_job(self, job_id, force=True):
                        continue
                    # Account offline / reconnecting — fail-fast, free the slot,
                    # shared reconnect owns login. Do not hold concurrency.
                    if _session_needs_login(verify_exc):
                        auth_err = _collapse_flow_login_error(str(verify_exc))
                        store.patch_row("jobs", job_id, {
                            "status": "action_required",
                            "stage": "action_required",
                            "error": auth_err,
                            "updatedAt": time.time(),
                        })
                        account_row = store.get_row("accounts", account_id) or {}
                        if account_row.get("status") not in {"connecting", "online"}:
                            store.patch_row("accounts", account_id, {
                                "status": "reconnect",
                                "error": auth_err,
                                "updatedAt": time.time(),
                            })
                        self._ensure_shared_reconnect(account_id, job_id=job_id, reason=auth_err)
                        self._log(
                            "warning",
                            "session_expired",
                            job_id=job_id,
                            account_id=account_id,
                            message="Phiên hết hạn — sẽ tự kết nối lại / Session expired — will auto-reconnect",
                        )
                        return
                    raise
                break
            credits = verified_account.get("credits")
            if job.get("kind", "video") == "video" and isinstance(credits, (int, float)) and int(credits) <= 0:
                raise ValueError("FLOW_CREDITS_EMPTY: Hết tín dụng — nạp thêm hoặc đợi reset (Out of Flow credits)")
            if verified_account.get("plan") == "Free" and str(job.get("kind") or "video") == "image" and dict(job.get("settings") or {}).get("model") == "Nano Banana Pro":
                raise ValueError("FLOW_PLAN_INSUFFICIENT: Tài khoản Free không hỗ trợ tạo ảnh Nano Banana Pro (chỉ khả dụng trên gói Pro/Plus/Ultra)")
            _ensure_flow_credits_for_job(
                verified_account,
                str(job.get("kind") or "video"),
                dict(job.get("settings") or {}),
            )
            # One run only for auth. On LOGIN_REQUIRED the job exits as
            # action_required and kicks a shared account reconnect — never
            # wait here for Chrome login (that blocked the whole queue).
            profile_ready = True
            asyncio.run(self._run(job_id, generation_token=generation_token))
            finished = store.get_row("jobs", job_id) or {}
            current_account = store.get_row("accounts", account_id) or {}
            if (
                current_account.get("status") == "reconnect"
                and _confirmed_login_wall_error(str(current_account.get("error") or ""))
                and not _is_automation_block_error(str(current_account.get("error") or ""))
            ):
                self._stop_account_after_cookie_logout(
                    account_id, str(current_account.get("error") or finished.get("error") or "FLOW_COOKIE_EXPIRED")
                )
                return
            finished_error = str(finished.get("error") or "")
            auth_error = _session_needs_login(Exception(finished_error)) and _is_automation_block_error(finished_error) and finished.get("status") in {
                "action_required", "failed",
            }
            if auth_error:
                self._ensure_shared_reconnect(
                    account_id,
                    job_id=job_id,
                    reason=finished_error,
                )

            # Project missing → recreate + retry up to _JOB_AUTO_RETRY_MAX times.
            finished = store.get_row("jobs", job_id) or {}
            for project_attempt in range(_JOB_AUTO_RETRY_MAX):
                finished = store.get_row("jobs", job_id) or finished
                project_error = (
                    finished.get("status") == "failed"
                    and not finished.get("mediaIds")
                    and not finished.get("outputs")
                    and "FLOW_PROJECT_NOT_FOUND" in str(finished.get("error") or "")
                )
                if not project_error:
                    break
                _log.warning(
                    "Flow project missing for %s; recovery %d/%d before retrying %s",
                    account_id, project_attempt + 1, _JOB_AUTO_RETRY_MAX, job_id,
                )
                job_kind = str(job.get("kind") or "video")
                old_project_id = _flow_project_id(
                    verified_account, job_kind, legacy_for_video=True,
                )
                # The Google session is still valid; only the project was
                # deleted. Headless recovery creates/selects a replacement
                # without opening the login flow or asking the user to connect again.
                asyncio.run(
                    self._try_headless_reconnect(
                        account_id, old_project_id, kind=job_kind,
                    )
                )
                refreshed = store.get_row("accounts", account_id) or {}
                if not (
                    refreshed.get("status") == "online"
                    and _flow_project_id(refreshed, job_kind, legacy_for_video=True)
                ):
                    break
                if job_id in self._cancelled or not store.get_row("jobs", job_id):
                    return
                verified_account = refreshed
                store.patch_row("jobs", job_id, {
                    "status": "queued", "stage": "queued", "progress": 0,
                    "error": None, "updatedAt": time.time(),
                })
                profile_ready = True
                asyncio.run(self._run(job_id, generation_token=generation_token))
                finished = store.get_row("jobs", job_id) or {}

            # Soft / reject / download → auto-retry up to _JOB_AUTO_RETRY_MAX
            # (unusual activity: one reset). Hard stops never enter this loop.
            # Siblings are never cancelled by another job's soft failure.
            for _auto_i in range(_JOB_AUTO_RETRY_MAX + _UNUSUAL_ACTIVITY_RESET_MAX):
                finished = store.get_row("jobs", job_id) or finished
                finished_error = str(finished.get("error") or "")
                rejected = "FLOW_GENERATION_REJECTED" in finished_error
                # Each job owns an isolated Chrome profile, so unusual activity
                # is retried in a fresh Chrome for this job only; siblings keep running.
                blocked = _is_automation_block_error(finished_error)
                download_recovery = bool(
                    finished.get("mediaIds")
                    and not finished.get("outputs")
                    and re.search(r"FLOW_DOWNLOAD_FAILED", finished_error, re.I)
                )
                raw_retry = finished.get("autoRetryCount")
                if raw_retry is None:
                    raw_retry = finished.get("generationRejectRetryCount")
                try:
                    retry_count = int(raw_retry or 0)
                except (TypeError, ValueError):
                    retry_count = 0
                try:
                    session_resets = int(finished.get("sessionResetRetryCount") or 0)
                except (TypeError, ValueError):
                    session_resets = 0
                soft_transient = (
                    not finished.get("submissionStartedAt")
                    or blocked
                    or download_recovery
                    or _is_flow_infra_interrupt(finished_error)
                    or re.search(
                        r"RESULT_NOT_FOUND|EMPTY_OUTPUT|GENERATION_TIMEOUT|FLOW_AUTOMATION_FAILED|"
                        r"FLOW_PROJECT_NOT_FOUND|FLOW_DOWNLOAD_FAILED|ERR_ABORTED|"
                        r"has been closed|Target closed|Connection closed",
                        finished_error,
                        re.I,
                    )
                )
                within_budget = (
                    session_resets < _UNUSUAL_ACTIVITY_RESET_MAX if blocked
                    else retry_count < _JOB_AUTO_RETRY_MAX
                )
                # Raw hạn mức copy (no FLOW_QUOTA_EXHAUSTED: prefix) must not
                # soft-retry the same Pro account — finally owns account/model fallback.
                quota_hard = _is_flow_quota_exhausted(finished_error) or bool(
                    _HARD_ERROR.search(finished_error)
                )
                should_auto_retry = (
                    finished.get("status") == "failed"
                    and soft_transient
                    and within_budget
                    and (
                        blocked
                        or download_recovery
                        or (not finished.get("mediaIds") and not finished.get("outputs"))
                    )
                    and (rejected or blocked or not quota_hard)
                )
                if not should_auto_retry:
                    break
                next_count = retry_count if blocked else retry_count + 1
                next_resets = session_resets + 1 if blocked else session_resets
                wait_s = _soft_retry_backoff_s(next_resets if blocked else next_count)
                self._log(
                    "warning", "job_auto_retry", job_id=job_id, account_id=account_id,
                    message=finished_error,
                    details={
                        "attempt": next_resets if blocked else next_count,
                        "max": _UNUSUAL_ACTIVITY_RESET_MAX if blocked else _JOB_AUTO_RETRY_MAX,
                        "backoffSeconds": wait_s,
                        "downloadOnly": download_recovery,
                        "freshChrome": True,
                    },
                )
                if job_id in self._cancelled or not store.get_row("jobs", job_id):
                    return
                recovering_timeout = bool(
                    re.search(r"GENERATION_TIMEOUT", finished_error, re.I)
                    and finished.get("submissionStartedAt")
                )
                patch: dict[str, Any] = {
                    "status": "processing",
                    "stage": "recovering" if recovering_timeout else "retrying",
                    "progress": 0,
                    # Keep reject/block errors visible during retry. Timeout
                    # recovery clears the sticky failed look until budget ends.
                    "error": None if recovering_timeout else finished_error,
                    "autoRetryCount": next_count,
                    "updatedAt": time.time(),
                }
                if rejected:
                    patch["generationRejectRetryCount"] = next_count
                if blocked:
                    patch["sessionResetRetryCount"] = next_resets
                if re.search(r"RESULT_NOT_FOUND|EMPTY_OUTPUT", finished_error, re.I) and not blocked:
                    self._release_media_ids(finished.get("mediaIds"))
                    patch.update({
                        "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "mediaIds": [], "resumeOnly": False,
                    })
                elif blocked:
                    # A browser/security wall does not prove that Flow
                    # discarded the server-side generation. Preserve every
                    # submission/media field and let the account coordinator
                    # reconcile it after cookie recovery.
                    patch.update({
                        "stage": "recovering_session",
                        "error": finished_error,
                        "recoveryNotice": {
                            "vi": "Tài khoản Flow đã đăng xuất — đang tự đăng nhập lại bằng cookie",
                            "en": "Flow account signed out — signing in again with the saved cookie",
                        },
                    })
                elif recovering_timeout:
                    # Recover the already-submitted result; do not clear and
                    # charge a duplicate generation.
                    patch["resumeOnly"] = True
                elif download_recovery:
                    # Keep mediaIds: retry downloads the existing Flow result only.
                    patch["resumeOnly"] = True
                store.patch_row("jobs", job_id, patch)
                if blocked:
                    # Keep one stable account row and one recovery operation.
                    # The coordinator marks the account as blocked, wipes only
                    # the browser profile, and requeues siblings after login.
                    store.patch_row("jobs", job_id, {
                        "status": "queued", "stage": "waiting_session_reset",
                        "progress": 0, "sessionResetRetryCount": next_resets,
                        "error": finished_error, "updatedAt": time.time(),
                    })
                    self._start_cookie_recovery(account_id, job_id=job_id, reason=finished_error)
                    break
                time.sleep(wait_s)
                if job_id in self._cancelled or not store.get_row("jobs", job_id):
                    return
                store.patch_row("jobs", job_id, {
                    "status": "queued",
                    "stage": "recovering" if recovering_timeout else "queued",
                    "progress": 0,
                    "error": None if recovering_timeout else finished_error,
                })
                profile_ready = True
                asyncio.run(self._run(job_id, generation_token=generation_token))
        except asyncio.CancelledError:
            pass  # Job was cancelled; store already updated by cancel()
        except Exception as exc:
            # Profile copy and worker bootstrap happen outside Flow's async
            # error boundary. Convert an unexpected failure here into a
            # terminal job state so the API/queue never leaves a zombie job or
            # an unhandled thread traceback (notably Windows profile races).
            current = store.get_row("jobs", job_id) or {}
            if current.get("status") not in _TERMINAL and job_id not in self._cancelled:
                if _session_needs_login(exc):
                    auth_err = _collapse_flow_login_error(str(exc))
                    store.patch_row(
                        "jobs",
                        job_id,
                        {
                            "status": "action_required",
                            "stage": "action_required",
                            "error": auth_err,
                            "updatedAt": time.time(),
                        },
                    )
                    account_row = store.get_row("accounts", account_id) or {}
                    if account_row.get("status") not in {"connecting", "online"}:
                        store.patch_row("accounts", account_id, {
                            "status": "reconnect",
                            "error": auth_err,
                            "updatedAt": time.time(),
                        })
                    # A dead cookie needs an explicit user Connect. Only an
                    # actual unusual-activity response is eligible for the
                    # automatic shared reconnect cycle.
                    if _is_automation_block_error(exc):
                        self._ensure_shared_reconnect(account_id, job_id=job_id, reason=auth_err)
                    self._log(
                        "warning",
                        "session_expired",
                        job_id=job_id,
                        account_id=account_id,
                        message="Phiên hết hạn — sẽ tự kết nối lại / Session expired — will auto-reconnect",
                    )
                else:
                    error = f"FLOW_WORKER_FAILED: {exc}"
                    store.patch_row(
                        "jobs",
                        job_id,
                        {
                            "status": "failed",
                            "stage": "worker" if profile_ready else "profile",
                            "error": error,
                            "updatedAt": time.time(),
                        },
                    )
                    self._log(
                        "error",
                        "job_failed",
                        job_id=job_id,
                        account_id=account_id,
                        message=error,
                        details={"stage": "worker" if profile_ready else "profile"},
                    )
        finally:
            with self._guard:
                self._active_browsers.pop(job_id, None)
            with self._account_condition:
                self._account_active[account_id] = max(0, self._account_active.get(account_id, 1) - 1)
                self._running_jobs.discard(job_id)
                self._account_condition.notify_all()

            current_job = store.get_row("jobs", job_id) or {}
            current_token = str(current_job.get("generationToken") or "")
            if current_job.get("status") == "queued" and self._outputs_ready_for_completion(
                current_job,
                account_plan=str((store.get_row("accounts", account_id) or {}).get("plan") or ""),
            ):
                self._finalize_existing_outputs(
                    job_id, current_job, account_id=account_id,
                    account_plan=str((store.get_row("accounts", account_id) or {}).get("plan") or ""),
                )
            elif current_job.get("status") == "queued":
                # Internal auto-retries intentionally keep the generation token;
                # always hand the queued row back to the scheduler after this
                # worker releases its slot.
                threading.Thread(target=self._run_sync, args=(job_id,), daemon=True, name=f"flow-job-retry-{job_id}").start()
            elif current_job.get("stage") == "model_fallback" and job_id not in self._cancelled:
                self.retry(job_id, {"accountId": account_id})
            elif current_job.get("status") in {"failed", "action_required"}:
                err_msg = str(current_job.get("error") or "Unknown error")
                if _session_needs_login(Exception(err_msg)):
                    self._requeue_auth_failed_jobs(account_id)
                settings = dict(current_job.get("settings") or {})
                quota_hit = _is_flow_quota_exhausted(err_msg)
                if quota_hit and not err_msg.startswith("FLOW_QUOTA_EXHAUSTED:"):
                    err_msg = _classify_visible_flow_error(err_msg) or err_msg
                    store.patch_row("jobs", job_id, {"error": err_msg, "updatedAt": time.time()})
                pro_quota = (
                    quota_hit
                    and str(current_job.get("kind") or "") == "image"
                    and str(settings.get("model") or "") == "Nano Banana Pro"
                    and not bool(current_job.get("quotaModelFallbackApplied"))
                )
                allow_account = bool(
                    current_job.get("allowAccountFallback", bool(current_job.get("randomAccount")))
                )
                if pro_quota:
                    # Pro hạn mức: always Nano Banana 2.1 on the same account
                    # (no account hop — Pro quota is per-model, not per-login).
                    self._apply_image_quota_fallback(job_id, err_msg)
                    if (store.get_row("jobs", job_id) or {}).get("stage") == "model_fallback":
                        self.retry(job_id, {"accountId": account_id})
                elif current_job.get("quotaModelFallbackApplied") and quota_hit:
                    # 2.1 still quota — try another account, else suspend.
                    store.patch_row("jobs", job_id, {"fallbackNotice": None, "updatedAt": time.time()})
                    if not (allow_account and self._try_fallback_account(job_id, account_id, err_msg)):
                        if str(current_job.get("kind") or "") == "image":
                            self.suspend_model(account_id, "image", str(settings.get("model") or "Nano Banana 2.1"), err_msg)
                        else:
                            self.suspend_account(account_id, err_msg, scope="video")
                elif allow_account and not _is_automation_block_error(err_msg):
                    # Explicit fallback remains available for non-session
                    # failures.  An unusual-activity response is handled by
                    # the shared same-account cookie reset coordinator above.
                    self._try_fallback_account(job_id, account_id, err_msg)
                elif re.search(r"FLOW_CREDITS_EMPTY", err_msg, re.I) or quota_hit:
                    if str(current_job.get("kind") or "") == "image" and quota_hit:
                        self.suspend_model(account_id, "image", str(settings.get("model") or "Nano Banana Pro"), err_msg)
                    else:
                        self.suspend_account(account_id, err_msg, scope="video")
            self._close_auto_session_if_idle(
                account_id,
                str(current_job.get("kind") or job.get("kind") or "video"),
            )

    async def _run(
        self,
        job_id: str,
        *,
        profile_dir: Path | None = None,
        headless_override: bool | None = None,
        generation_token: str = "",
    ) -> None:
        while True:
            current = store.get_row('jobs', job_id)
            if not current or current.get('status') in {'done', 'cancelled'}:
                return
            if generation_token and str(current.get('generationToken') or '') != generation_token:
                return
            await asyncio.to_thread(_network_ready_for_flow_job, self, job_id)
            try:
                await self._run_once(job_id, profile_dir=profile_dir,
                                     headless_override=headless_override, generation_token=generation_token)
                return
            except _FlowNetworkInterrupted:
                # _run_once has closed Chrome. Keep submission/media/output
                # identity; the existing recovery path must never resubmit it.
                await asyncio.to_thread(_network_ready_for_flow_job, self, job_id, force=True)
            except _FlowSessionGenerationChanged:
                store.patch_row("jobs", job_id, {
                    "status": "queued", "stage": "waiting_session_reset",
                    "error": None, "updatedAt": time.time(),
                })
                return

    async def _run_once(
        self,
        job_id: str,
        *,
        profile_dir: Path | None = None,
        headless_override: bool | None = None,
        generation_token: str = "",
    ) -> None:
        job = store.get_row("jobs", job_id)
        account = store.get_row("accounts", str(job.get("accountId"))) if job else None
        if not job or not account or job.get("status") in {"done", "cancelled"}:
            return
        expected_generation = int(job.get("sessionGeneration") or 0)
        current_generation = self._session_generation(str(account.get("id") or ""))
        if (
            account.get("status") == "reconnect"
            and _confirmed_login_wall_error(str(account.get("error") or ""))
            and not _is_automation_block_error(str(account.get("error") or ""))
        ):
            self._stop_account_after_cookie_logout(
                str(account.get("id") or ""), str(account.get("error") or "FLOW_COOKIE_EXPIRED")
            )
            self._check_cancel(job_id)
        if expected_generation and expected_generation != current_generation:
            store.patch_row("jobs", job_id, {
                "status": "queued", "stage": "waiting_session_reset",
                "error": None, "updatedAt": time.time(),
            })
            return
        if self._account_session_blocked(str(account.get("id") or "")):
            store.patch_row("jobs", job_id, {
                "status": "queued", "stage": "waiting_session_reset",
                "updatedAt": time.time(),
            })
            return
        # The 15-minute hold only stops siblings already waiting on Flow.
        # A new or retried job must actually run; failing here showed the
        # unusual-activity message before anything was submitted.
        if generation_token and str(job.get("generationToken") or "") != generation_token:
            return
        if job.get("forceNew"):
            # Create-new must never enter recovery. A completed job may still
            # carry stale submission metadata from an older attempt.
            reset = {
                "submissionStartedAt": None,
                "submissionProjectId": None,
                "baselineMediaIds": [],
                "baselineThumbs": [],
                "mediaIds": [],
                "resumeOnly": False,
                "forceNew": False,
                "stage": "submitting",
                "progress": 5,
                "updatedAt": time.time(),
            }
            store.patch_row("jobs", job_id, reset)
            job = {**job, **reset}
        browser = None
        session_refresh_task = None
        session_refresh_registered = False
        runtime_profile = None
        try:
            self._check_cancel(job_id)
            self._log("info", "job_started", job_id=job_id, account_id=account["id"], details={"kind": job["kind"]})
            job_kind = str(job.get("kind") or "video")
            project_id = str(job.get("submissionProjectId") or "") or _flow_project_id(account, job_kind, legacy_for_video=True)
            if account.get("status") != "online" or not project_id:
                raise RuntimeError("FLOW_LOGIN_REQUIRED: connect the Google Flow account first")
            from ._flow._api import FlowAPI
            from .browser import BrowserManager, free_local_port
            from ._flow._client import FlowClient
            # Keep the default queue invisible. Users can opt into a visible
            # Chrome window from Flow quick settings when debugging UI drift.
            headless = (
                headless_override
                if headless_override is not None
                else _flow_headless(job.get("settings") or {})
            )
            profile_slot = self._session_key(str(account["id"]), job_kind)
            self._log(
                "info", "browser_mode", job_id=job_id, account_id=account["id"],
                details={"kind": job_kind, "headless": bool(headless), "profileSlot": profile_slot, "manualSession": False},
            )
            # Same CDP spawn as Connect (not Playwright launch_persistent) so
            # packaged jobs share the signed-in Chrome surface and re-seed jar.
            runtime_dir = profile_dir or (
                self._clone_runtime_profile(str(account["id"]), job_id, job_kind)
                if job_kind in {"image", "video"}
                else store.profile_dir(account["id"])
            )
            browser = BrowserManager(
                headless=headless,
                profile_dir=runtime_dir,
                restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
                remote_debugging_port=free_local_port(),
            )
            browser._flow_session_generation = expected_generation
            browser._flow_started_at = time.time()
            self._check_cancel(job_id)
            with self._guard:
                self._active_browsers[job_id] = browser
            await browser.start()
            # Cookie logout can race with browser startup. Recheck after the
            # process is owned so the worker cannot proceed into Flow after
            # the account-wide stop marked this job cancelled.
            self._check_cancel(job_id)
            self._assert_session_generation(str(account["id"]), expected_generation)
            await self._seed_browser_cookies(account, browser)
            if job_kind in {"image", "video"}:
                if not getattr(browser, "cdp_url", None):
                    runtime_profile = Path(browser.profile_dir)
                session_refresh_task = self._start_session_refresh(
                    str(account["id"]), browser, project_id,
                )
                session_refresh_registered = True
            self._log("info", "browser_ready", job_id=job_id, account_id=account["id"])
            api = FlowAPI(browser, project_id=project_id, default_timeout_s=_VIDEO_GENERATION_TIMEOUT_S)
            # Seed Bearer captured at login — cloned headless profiles often cannot
            # mint ya29 themselves even when cookies are valid.
            seed = str(account.get("accessToken") or "")
            if seed.startswith("ya29."):
                api._bearer_token = seed
                api._bearer_token_ts = time.time()
                self._log("info", "bearer_seeded", job_id=job_id, account_id=account["id"])
            client = FlowClient(api, browser, project_id)
            settings = job.get("settings") or {}
            settings, catalog_changed = _normalize_catalog_settings(account, str(job["kind"]), settings)
            if job["kind"] == "video" and not _catalog_section(account, "video"):
                fallback_model = _normalize_video_model(settings.get("model"))
                catalog_changed = catalog_changed or fallback_model != settings.get("model")
                settings = {**settings, "model": fallback_model}
            if catalog_changed:
                store.patch_row("jobs", job_id, {"settings": settings, "updatedAt": time.time()})
                job = {**job, "settings": settings}
                self._log(
                    "info", "capability_settings_migrated", job_id=job_id,
                    account_id=account["id"], details={
                        "model": settings.get("model"), "ratio": settings.get("ratio"),
                        "duration": settings.get("duration"),
                    },
                )
            parallel_session = _job_concurrency(settings) > 1
            if bool(job.get("parallelSession")) != parallel_session:
                store.patch_row("jobs", job_id, {"parallelSession": parallel_session, "updatedAt": time.time()})
                job = {**job, "parallelSession": parallel_session}
            # Validate the persisted project before a fresh submission too.
            # A deleted Flow project must be replaced instead of surfacing a
            # raw 404 and stopping the new image/video job.
            # A lost-result failure means Flow has nothing left to recover.
            # GENERATION_TIMEOUT keeps resumeOnly + submissionStartedAt so the
            # next run polls the existing submit instead of charging again.
            recovery_error = str(job.get("error") or "")
            lost_result_retry = bool(
                re.search(r"FLOW_RESULT_NOT_FOUND|FLOW_EMPTY_OUTPUT|FLOW_AUTOMATION_FAILED", recovery_error, re.I)
                and not job.get("mediaIds")
                and not job.get("outputs")
                and not job.get("resumeOnly")
            )
            recovery_state = (
                str(job.get("stage") or "") in {"recovering", "recovering_session", "waiting_session_reset", "downloading", "reconciling"}
                or bool(job.get("mediaIds"))
                or bool(job.get("resumeOnly"))
                or bool(job.get("submissionStartedAt"))
            ) and not lost_result_retry
            if lost_result_retry:
                reset = {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "baselineThumbs": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "stage": "preparing",
                    "progress": 5,
                    "updatedAt": time.time(),
                }
                store.patch_row("jobs", job_id, reset)
                job = {**job, **reset}
            page = await browser.page()
            if not recovery_state:
                account = await self._ensure_account_project_page(
                    page, api=api, client=client, account=account, job_id=job_id, kind=job_kind,
                )
                project_id = str(client.project_id)
            await self._confirm_logged_in_session(page, str(account["id"]))
            submission_project_id = str(job.get("submissionProjectId") or "")
            project_changed_at = _flow_project_changed_at(account, job_kind)
            submitted_at = float(job.get("submissionStartedAt") or 0)
            submission_project_missing = bool(
                (submission_project_id and submission_project_id != project_id)
                or (
                    not submission_project_id
                    and submitted_at
                    and project_changed_at
                    and submitted_at < project_changed_at
                )
            )
            if submission_project_missing:
                if job.get("mediaIds") or job.get("outputs"):
                    raise RuntimeError(
                        "FLOW_DOWNLOAD_FAILED: original project is unavailable; preserving generated media"
                    )
                # A result submitted to a deleted project cannot appear in the
                # replacement project. Clear recovery identity and submit the
                # same local job once, instead of pretending it is still at 84%.
                reset = {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "stage": "resubmitting",
                    "progress": 3,
                    "updatedAt": time.time(),
                }
                store.patch_row("jobs", job_id, reset)
                job = {**job, **reset}
                self._log(
                    "warning",
                    "submission_project_missing",
                    job_id=job_id,
                    account_id=account["id"],
                    details={
                        "submissionProjectId": submission_project_id,
                        "currentProjectId": project_id,
                    },
                )
            store.patch_row("jobs", job_id, {"status": "processing", "stage": "submitting", "progress": 5, "updatedAt": time.time()})
            if recovery_state:
                store.patch_row("jobs", job_id, {"stage": "reconciling", "updatedAt": time.time()})
                page = await browser.page()
                account = await self._ensure_account_project_page(
                    page, api=api, client=client, account=account, job_id=job_id, kind=job_kind,
                )
                project_id = str(client.project_id)
                media_ids = list(job.get("mediaIds") or [])
                if not media_ids:
                    recovery_timeout = (
                        _VIDEO_GENERATION_TIMEOUT_S
                        if job["kind"] == "video"
                        else _IMAGE_GENERATION_TIMEOUT_S
                    )
                    media_ids = await self._recover_submitted_media(api, page, job, timeout_s=recovery_timeout)
                    job = {**job, "mediaIds": media_ids}
                outputs = []
                if job["kind"] == "video":
                    from ._flow._api import VideoJob
                    from ._flow._exceptions import GenerationError as FlowGenerationError
                    bearer_ok = str(getattr(api, "_bearer_token", "") or "").startswith("ya29.")
                    # Modern Flow SPA rarely emits ya29; aisandbox poll then 401s.
                    # Prefer UI tiles / fife URL from project payload over Bearer poll.
                    thumb_ids = [m for m in media_ids if str(m).startswith("flow-thumb:")]
                    uuid_ids = [m for m in media_ids if not str(m).startswith("flow-thumb:")]
                    if thumb_ids and not uuid_ids:
                        for index, media_id in enumerate(thumb_ids, 1):
                            self._check_cancel(job_id)
                            thumb = str(media_id)[len("flow-thumb:"):]
                            tile_index = await page.evaluate("""(target) => {
                                const tiles = [...document.querySelectorAll('flow-grid-tile-container')];
                                const clean = (u) => (u || '').split('?')[0].trim();
                                const targetClean = clean(target);
                                return tiles.findIndex(tile => {
                                    const image = tile.querySelector('.thumbnail');
                                    if (!image) return false;
                                    const src = image.currentSrc || image.src || '';
                                    return src === target || (targetClean && clean(src) === targetClean);
                                });
                            }""", thumb)
                            output = self._output_path(job, index, "mp4")
                            account_plan = str(account.get("plan") or "")
                            if int(tile_index) < 0:
                                # The SPA may have evicted the tile after a
                                # failed download. Reload the project once so
                                # the retry can use Flow's authenticated menu
                                # even when the project payload has no fifeUrl.
                                try:
                                    await page.reload(wait_until="domcontentloaded", timeout=20_000)
                                    await page.locator("flow-grid-tile-container").first.wait_for(timeout=10_000)
                                    tile_index = await page.evaluate("""(target) => {
                                        const clean = (u) => (u || '').split('?')[0].trim();
                                        const wanted = clean(target);
                                        return [...document.querySelectorAll('flow-grid-tile-container')].findIndex(tile => {
                                            const image = tile.querySelector('.thumbnail');
                                            const src = image?.currentSrc || image?.src || '';
                                            return src === target || (wanted && clean(src) === wanted);
                                        });
                                    }""", thumb)
                                except Exception:
                                    tile_index = -1
                            if int(tile_index) < 0:
                                # The completed media can disappear from the
                                # virtualized SPA grid while remaining in the
                                # authenticated project payload. Download it
                                # directly instead of submitting a new video.
                                await self._download_video_from_project_media(api, job, output, index)
                                outputs.append(str(output))
                                self._log("success", "output_downloaded_from_project_media", job_id=job_id,
                                          account_id=account["id"], details={"outputIndex": index, "tileMissing": True})
                                continue
                            quality = _video_download_quality(settings, account_plan)
                            try:
                                await self._download_video_via_flow_menu(page, int(tile_index), output, quality, job_id=job_id)
                            except Exception as menu_exc:
                                # Flow's edit-page menu changes frequently. The
                                # completed project payload is an authenticated
                                # fallback and must not trigger a new generation.
                                try:
                                    await self._download_video_from_project_media(api, job, output, index)
                                    self._log("warning", "video_download_menu_fallback", job_id=job_id,
                                              account_id=account["id"], message=str(menu_exc))
                                except Exception:
                                    raise menu_exc
                            outputs.append(str(output))
                            self._log("success", "output_downloaded", job_id=job_id,
                                      account_id=account["id"], details={"outputIndex": index, "quality": quality})
                    elif uuid_ids and bearer_ok:
                        try:
                            for index, media_id in enumerate(uuid_ids, 1):
                                self._check_cancel(job_id)
                                remote_job = VideoJob.__new__(VideoJob)
                                remote_job.media_name = media_id
                                remote_job.project_id = project_id
                                status = await api.wait_for_video(remote_job, timeout_s=_VIDEO_GENERATION_TIMEOUT_S)
                                output = self._output_path(job, index, "mp4")
                                await api.download(status.fife_url, output)
                                outputs.append(str(output))
                        except FlowGenerationError as poll_exc:
                            if "avoid reconnect storm" not in str(poll_exc) and not re.search(r"\b401\b|\b403\b", str(poll_exc)):
                                raise
                            self._log(
                                "warning", "video_poll_fallback_ui",
                                job_id=job_id, account_id=account["id"],
                                message="aisandbox poll unauthorized — falling back to Flow tiles",
                            )
                            outputs = []
                    if not outputs:
                        # No Bearer / poll failed: resolve fife URLs or download newest tiles.
                        for index, media_id in enumerate(uuid_ids, 1):
                            self._check_cancel(job_id)
                            fife = await self._resolve_video_fife_url(api, str(media_id))
                            if not fife:
                                continue
                            output = self._output_path(job, index, "mp4")
                            await api.download(fife, output)
                            outputs.append(str(output))
                        if not outputs:
                            count = max(1, min(4, int(settings.get("count", len(media_ids) or 1))))
                            outputs = await self._wait_and_download_flow_videos(
                                page, job, count, [], job_id, api=api,
                            )
                else:
                    await self._recover_submitted_media(api, page, job)
                    items = await self._find_existing_project_media(api, page, job, "image", len(media_ids))
                    by_id = {str(item['id']): item for item in items}
                    if any(media_id not in by_id for media_id in media_ids):
                        raise RuntimeError("FLOW_GENERATION_TIMEOUT: original image media is not available yet")
                    for index, media_id in enumerate(media_ids, 1):
                        self._check_cancel(job_id)
                        output = self._output_path(job, index, str(settings.get("format", "jpg")).lower())
                        await api.download(str(by_id[media_id]['src']), output)
                        outputs.append(str(output))
            elif job["kind"] == "video":
                source = next(iter(job.get("sourceFiles") or []), None)
                model = _normalize_video_model(settings.get("model"))
                ratio = str(settings.get("ratio") or "16:9")
                page = await browser.page()
                account = await self._ensure_account_project_page(
                    page, api=api, client=client, account=account, job_id=job_id, kind=job_kind,
                )
                project_id = str(client.project_id)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 8, "updatedAt": time.time()})
                # Populate the composer first so Flow exposes the quick-settings
                # controls, using the same order as image generation.
                if not await client._ui.fill_prompt(page, job["prompt"]):
                    raise RuntimeError("FLOW_UI_CHANGED: prompt editor was not found")
                await self._prepare_ui_model(page, "video", model, ui=client._ui)
                # Select count while the model quick-settings panel is still
                # open; changing resolution can close/rebuild that panel.
                count = max(1, min(4, int(settings.get("count", 1))))
                await self._set_flow_count(page, count)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 12, "updatedAt": time.time()})
                await self._prepare_ui_format(
                    page,
                    ratio,
                    _video_ui_duration(account, settings),
                    _video_ui_resolution(settings.get("resolution")) or None,
                    model,
                )
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 15, "updatedAt": time.time()})
                baseline_media = await self._project_media_elements(page)
                baseline_ids = {str(item.get("id")) for item in baseline_media if item.get("id")}
                if not baseline_ids:
                    try:
                        project_data = await api.get_project_data()
                        baseline_ids = {
                            str(media.get("name"))
                            for media in project_data.get("projectContents", {}).get("media", [])
                            if media.get("name")
                        }
                    except Exception:
                        baseline_ids = set()
                if source:
                    from ._flow._models import GenerationMode
                    if not await client._ui.switch_mode(page, GenerationMode.FRAME_TO_VIDEO):
                        raise RuntimeError(f"FLOW_UI_CHANGED: Frames mode is not available for {model}")
                extend_from = store.get_row("jobs", str(settings.get("extendFromJobId") or ""))
                remote = []
                media_items: list[dict[str, Any]] = []
                if extend_from and (extend_from.get("mediaIds") or []):
                    media_id = str(extend_from["mediaIds"][0])
                    try:
                        project_data = await api.get_project_data()
                    except Exception:
                        project_data = {}
                    media = next((item for item in project_data.get("projectContents", {}).get("media", []) if str(item.get("name") or "") == media_id), {})
                    workflow_id = str(media.get("workflowId") or "")
                    if not workflow_id:
                        raise RuntimeError("FLOW_EXTEND_WORKFLOW_MISSING: prior video has no workflow")
                    await self._remember_visible_errors(page, job_id)
                    store.patch_row("jobs", job_id, {"submissionStartedAt": time.time(), "submissionProjectId": project_id, "baselineMediaIds": sorted(baseline_ids), "baselineThumbs": []})
                    await _wait_before_flow_submit()
                    remote = [await client.extend_video(media_id, workflow_id, job["prompt"])]
                    media_ids = [item.media_name for item in remote]
                    self._log("success", "generation_submitted", job_id=job_id, account_id=account["id"], details={"model": model, "generationResolution": settings.get("resolution") or settings.get("videoResolution") or "", "mediaIds": media_ids})
                    store.patch_row("jobs", job_id, {"mediaIds": media_ids, "stage": "generating"})
                    await self._sync_credits(api, account["id"])
                    outputs = []
                    bearer_ok = str(getattr(api, "_bearer_token", "") or "").startswith("ya29.")
                    if bearer_ok:
                        try:
                            from ._flow._exceptions import GenerationError as FlowGenerationError
                            for output_index, _media_id in enumerate(media_ids, 1):
                                self._check_cancel(job_id)
                                status = await api.wait_for_video(
                                    remote[0],
                                    timeout_s=_VIDEO_GENERATION_TIMEOUT_S,
                                    on_poll=lambda _s, elapsed: store.patch_row(
                                        "jobs", job_id,
                                        # 90% is reserved for downloading; a
                                        # still-rendering API poll must never
                                        # appear complete in the queue.
                                        {"stage": "generating", "updatedAt": time.time()},
                                    ),
                                )
                                store.patch_row(
                                    "jobs", job_id,
                                    {"stage": "downloading", "updatedAt": time.time()},
                                )
                                output = self._output_path(job, output_index, "mp4")
                                await api.download(status.fife_url, output)
                                outputs.append(str(output))
                                self._log(
                                    "success", "output_downloaded",
                                    job_id=job_id, account_id=account["id"],
                                    details={"outputIndex": output_index, "path": str(output)},
                                )
                        except FlowGenerationError as poll_exc:
                            if "avoid reconnect storm" not in str(poll_exc) and not re.search(r"\b401\b|\b403\b", str(poll_exc)):
                                raise
                            outputs = []
                    if not outputs:
                        baseline_thumbs = await self._flow_tile_thumbs(page)
                        outputs = await self._wait_and_download_flow_videos(
                            page, job, max(1, len(media_ids)), baseline_thumbs, job_id, api=api,
                        )
                else:
                    baseline_thumbs = await self._flow_tile_thumbs(page)
                    await self._set_flow_count(page, count)
                    await asyncio.sleep(1)
                    if source:
                        # Typing into the composer empties the Start slot, so attach it last.
                        if not await client._ui.set_start_frame(page, source):
                            raise RuntimeError("FLOW_UI_CHANGED: start frame could not be uploaded")
                        self._log("info", "start_frame_set", job_id=job_id, account_id=account["id"], details={"source": Path(source).name})
                    await self._remember_visible_errors(page, job_id)
                    store.patch_row("jobs", job_id, {"submissionStartedAt": time.time(), "submissionProjectId": project_id, "baselineMediaIds": sorted(baseline_ids)})
                    await self._close_flow_settings_panel(page, client._ui)
                    await self._ensure_flow_prompt_ready(page, client._ui, job["prompt"])
                    await _wait_before_flow_submit()
                    try:
                        await self._click_flow_submit(
                            page, ui=client._ui, job_id=job_id, account_id=account["id"],
                        )
                    except RuntimeError as exc:
                        if str(exc).startswith("FLOW_SUBMIT_NOT_CONFIRMED:"):
                            self._clear_unsent_submission(job_id)
                        raise
                    store.patch_row("jobs", job_id, {"stage": "generating", "updatedAt": time.time()})
                    self._log("success", "generation_submitted", job_id=job_id, account_id=account["id"], details={"model": model, "generationResolution": settings.get("resolution") or settings.get("videoResolution") or ""})
                    # Flow's current UI submits via batchexecute (no aisandbox RPC to
                    # intercept) and finished video tiles only carry a thumbnail — no
                    # <video>/data-media-id — so tiles are the source of truth.
                    outputs = await self._wait_and_download_flow_videos(
                        page, job, count, baseline_thumbs, job_id, api=api,
                    )
                    asyncio.create_task(self._sync_credits(api, account["id"]))
            else:
                model = str(settings.get("model") or "Nano Banana 2")
                mode = str(job.get("mode") or "text")
                sources = list(job.get("sourceFiles") or [])
                if mode == "edit":
                    sources = sources[:1]
                elif mode == "reference":
                    sources = sources[:3]
                page = await browser.page()
                account = await self._ensure_account_project_page(
                    page, api=api, client=client, account=account, job_id=job_id, kind=job_kind,
                )
                project_id = str(client.project_id)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 8, "updatedAt": time.time()})
                prompt_text = _prompt_with_reference_strength(
                    job["prompt"], mode, settings.get("referenceStrength", 70),
                )
                # The migrated composer exposes its settings button only after
                # the composer has focus/content (Flow issue #749).
                if not await client._ui.fill_prompt(page, prompt_text):
                    raise RuntimeError("FLOW_UI_CHANGED: prompt editor was not found")
                count = max(1, min(4, int(settings.get("count", 1))))
                # Apply the selected image settings through the same visible
                # controls a user uses in the open Playwright session.
                await self._prepare_ui_model(page, "image", model, ui=client._ui)
                await self._prepare_ui_format(page, str(settings.get("ratio") or "16:9"))
                await client._ui.open_settings_panel(page)
                await self._set_flow_count(page, count)
                baseline_media = await self._project_media_elements(page)
                baseline_ids = {str(item.get("id")) for item in baseline_media if item.get("id")}
                uploaded_media_names: list[str] = []
                for source in sources:
                    if not await client._ui.add_prompt_image(page, source):
                        raise RuntimeError(f"FLOW_UI_CHANGED: could not upload source image ({Path(str(source)).name})")
                # Uploaded references become grid tiles a few seconds later; they
                # must join the baseline or the waiter returns them as the "output".
                upload_deadline = time.monotonic() + 15
                while sources and time.monotonic() < upload_deadline:
                    try:
                        for item in await self._project_media_elements(page):
                            mid = str(item.get("id") or "")
                            if mid and mid not in baseline_ids:
                                uploaded_media_names.append(mid)
                                baseline_ids.add(mid)
                    except Exception:
                        pass
                    if len(uploaded_media_names) >= len(sources):
                        break
                    await asyncio.sleep(0.5)
                if mode in {"edit", "reference"} and sources and not uploaded_media_names:
                    _log.warning(
                        "image_%s: upload reported ok but no new media ids yet; continuing with UI-attached refs",
                        mode,
                    )
                await self._remember_visible_errors(page, job_id)
                store.patch_row("jobs", job_id, {
                    "submissionStartedAt": time.time(),
                    "submissionProjectId": project_id,
                    "baselineMediaIds": sorted(baseline_ids),
                    "stage": "submitting",
                    "progress": 15,
                    "updatedAt": time.time(),
                })
                # Quick-settings left open can swallow the Create click (no RPC).
                await self._close_flow_settings_panel(page, client._ui)
                # Settings/mode steps above can blur or clear the composer; make
                # sure the prompt is present or Submit stays disabled.
                await self._ensure_flow_prompt_ready(page, client._ui, prompt_text)
                await _wait_before_flow_submit()
                media_items = []
                rpc_responses: list[Any] = []
                try:
                    rpc_responses = await self._click_flow_submit(
                        page, ui=client._ui, job_id=job_id, account_id=account["id"],
                    )
                except RuntimeError as exc:
                    if str(exc).startswith("FLOW_SUBMIT_NOT_CONFIRMED:"):
                        self._clear_unsent_submission(job_id)
                        raise
                    if not str(exc).startswith(("FLOW_UI_CHANGED:", "FLOW_SUBMIT_CLICK_FAILED:")):
                        raise
                    # A disabled composer does not prove the request failed.
                    # Require matching prompt metadata even for a single tab;
                    # another tab can have completed media in the same project.
                    current_job = store.get_row("jobs", job_id) or {}
                    media_items = await self._find_existing_project_media(
                        api, page, {**job, **current_job, "parallelSession": True,
                                    "recoveryOnly": False}, "image", count,
                    )
                    if len(media_items) < count:
                        # Nothing was submitted: do not leave a fake submission behind.
                        self._clear_unsent_submission(job_id)
                        raise
                    self._log(
                        "warning", "submit_error_completed_images_recovered",
                        job_id=job_id, account_id=account["id"], message=str(exc),
                    )
                store.patch_row("jobs", job_id, {
                    "stage": "generating", "updatedAt": time.time(),
                })
                self._log(
                    "success", "generation_submitted",
                    job_id=job_id, account_id=account["id"],
                    details={"model": model, "ratio": settings.get("ratio"), "count": count},
                )
                submitted_at = time.time()
                submitted_job = {
                    **job,
                    "submissionStartedAt": submitted_at,
                    "submissionProjectId": project_id,
                    "baselineMediaIds": sorted(baseline_ids),
                }
                recovered_media_ids: list[str] = []
                if not media_items:
                    try:
                        media_items = await self._wait_for_project_media(
                            page, baseline_ids, "image", count, job_id,
                            api=api, job=submitted_job, rpc_responses=rpc_responses,
                        )
                    except RuntimeError as wait_exc:
                        if "GENERATION_TIMEOUT" not in str(wait_exc):
                            raise
                        # Same-run recover: do not mark failed yet; Flow may
                        # still mount the billed result without a duplicate submit.
                        store.patch_row("jobs", job_id, {
                            "status": "processing",
                            "stage": "recovering",
                            "error": None,
                            "resumeOnly": True,
                            "submissionStartedAt": submitted_at,
                            "submissionProjectId": project_id,
                            "baselineMediaIds": sorted(baseline_ids),
                            "updatedAt": time.time(),
                        })
                        recovery_job = {
                            **submitted_job,
                            "resumeOnly": True,
                            "recoveryOnly": True,
                            "rpcMediaIds": list(
                                (store.get_row("jobs", job_id) or {}).get("rpcMediaIds") or []
                            ),
                        }
                        recovered_media_ids = await self._recover_submitted_media(
                            api, page, recovery_job, timeout_s=_IMAGE_GENERATION_TIMEOUT_S,
                        )
                        if not recovered_media_ids:
                            raise
                        media_items = await self._find_existing_project_media(
                            api, page,
                            {**recovery_job, "mediaIds": recovered_media_ids},
                            "image", count,
                        )
                if not media_items:
                    raise RuntimeError("FLOW_EMPTY_OUTPUT: Flow returned no completed images")
                if recovered_media_ids:
                    # _recover_submitted_media already claimed these ids.
                    media_ids = list(recovered_media_ids)[:count]
                else:
                    media_ids = self._claim_media_ids(
                        [str(item["id"]) for item in media_items], count,
                    )
                if len(media_ids) < count:
                    raise RuntimeError("FLOW_RESULT_NOT_FOUND: generated images were claimed by another job")
                media_items = [item for item in media_items if str(item["id"]) in set(media_ids)]
                store.patch_row("jobs", job_id, {"mediaIds": media_ids, "stage": "downloading"})
                # Download all images in parallel for speed
                fmt = str(settings.get("format", "jpg")).lower()
                async def _dl(image: dict, idx: int) -> str:
                    self._check_cancel(job_id)
                    out = self._output_path(job, idx, fmt)
                    await api.download(str(image["src"]), out)
                    self._log("success", "output_downloaded", job_id=job_id, account_id=account["id"], details={"outputIndex": idx, "path": str(out)})
                    return str(out)
                outputs = list(await asyncio.gather(*[_dl(img, i) for i, img in enumerate(media_items, 1)]))
                # Sync credits after download (non-blocking)
                asyncio.create_task(self._sync_credits(api, account["id"]))
            output_error = self._output_validation_error(outputs)
            if output_error:
                raise RuntimeError(output_error)
            if job["kind"] == "image":
                ratio_error = self._image_ratio_validation_error(outputs, str(settings.get("ratio") or ""))
                if ratio_error:
                    raise RuntimeError(ratio_error)
            # Xóa logo Flow ngay sau khi tải (cả ảnh mới lẫn ảnh khôi phục); Ultra không có logo nên bỏ qua.
            if settings.get("removeLogo") and str(account.get("plan") or "") != "Ultra":
                logo_error = await self._remove_flow_logos(job_id, outputs)
                if logo_error:
                    self._fail_logo_removal(job_id, outputs, logo_error, account_id=str(account["id"]))
                    return
            current = store.get_row("jobs", job_id) or {}
            if generation_token and str(current.get("generationToken") or "") != generation_token:
                # A retry can rotate the token after this worker downloaded and
                # cleaned the files.  Preserve that durable result instead of
                # allowing the stale worker to strand the row in ``queued``.
                self._finalize_existing_outputs(
                    job_id, current, account_id=str(account.get("id") or ""),
                    account_plan=str(account.get("plan") or ""),
                )
                return
            if current.get("status") == "cancelled" or job_id in self._cancelled:
                return
            self._assert_session_generation(str(account.get("id") or ""), expected_generation)
            store.patch_row("jobs", job_id, {
                "status": "done", "stage": "done", "progress": 100,
                "outputs": outputs, "error": None, "updatedAt": time.time(),
            })
            if job.get("seriesContext"):
                from . import series
                series.mark_job_complete(job, outputs)
            self._log("success", "job_completed", job_id=job_id, account_id=account["id"], details={"outputCount": len(outputs)})
        except asyncio.CancelledError:
            current = store.get_row("jobs", job_id) or {}
            if not generation_token or str(current.get("generationToken") or "") == generation_token:
                store.patch_row("jobs", job_id, {"status": "cancelled", "stage": "cancelled", "progress": 0, "updatedAt": time.time()})
            self._log("warning", "job_cancelled", job_id=job_id, account_id=account["id"])
        except Exception as exc:
            current = store.get_row("jobs", job_id) or {}
            if current.get("status") == "done":
                self._log(
                    "warning",
                    "late_worker_error_ignored",
                    job_id=job_id,
                    account_id=account["id"],
                    message=str(exc),
                )
                return
            self._check_cancel(job_id)
            # Probe can stay true during Wi-Fi flaps / Flow CDN lag while
            # Chrome still fails Page.goto or net::ERR_*. Wait and resume the
            # same job identity instead of marking failed or reconnecting.
            offline = not await asyncio.to_thread(network_available, force=True)
            if offline or _is_flow_infra_interrupt(exc):
                store.patch_row('jobs', job_id, {
                    'status': 'processing', 'stage': 'waiting_network',
                    'networkError': str(exc), 'updatedAt': time.time(),
                })
                raise _FlowNetworkInterrupted from exc
            needs_login = _session_needs_login(exc)
            automation_block = _is_automation_block_error(exc)
            error = str(exc)
            confirmed_wall = _confirmed_login_wall_error(exc)
            # Only a confirmed login wall or unusual activity may change the
            # account session state. Nested FLOW_LOGIN_REQUIRED / verification
            # prompts must not storm reconnect and look like a logout.
            if needs_login and not automation_block and confirmed_wall:
                account_id = str(account.get("id") or "")
                self._stop_account_after_cookie_logout(account_id, error)
                self._log(
                    "warning",
                    "session_cookie_check",
                    job_id=job_id,
                    account_id=account_id,
                    message="Cookie/phiên chưa đăng nhập — đang kết nối lại, các job khác chờ / Session not signed in — reconnecting, other jobs wait",
                )
                return
            if needs_login and not automation_block and not confirmed_wall:
                error = _collapse_flow_login_error(error)
            if not needs_login and _is_flow_quota_exhausted(error):
                # Always persist the coded form so soft-retry/finally see quota.
                if not error.startswith("FLOW_QUOTA_EXHAUSTED:"):
                    error = _classify_visible_flow_error(error) or error
            elif not needs_login and not error.startswith("FLOW_"):
                classified_error = _classify_visible_flow_error(error)
                if classified_error.startswith("FLOW_QUOTA_EXHAUSTED:"):
                    error = classified_error
            # Pro image + quota: do not downshift here. _run_sync finally
            # switches to Nano Banana 2.1 on the same account.
            # A normal submit/timeout/auth error belongs to this job only. Do
            # not take a healthy account offline unless Flow explicitly shows
            # unusual activity and the shared recovery path is required.
            action = "action_required" if needs_login and automation_block else "failed"
            failed_stage = (store.get_row("jobs", job_id) or {}).get("stage")
            store.patch_row("jobs", job_id, {
                "status": action, "stage": action, "error": error,
                "recoveryNotice": ({
                    "vi": "Tài khoản Flow đã đăng xuất — đang tự đăng nhập lại bằng cookie",
                    "en": "Flow account signed out — signing in again with the saved cookie",
                } if needs_login else None),
                "updatedAt": time.time(),
            })
            if needs_login and automation_block:
                # Mark the account as waiting for the shared reset. The worker
                # finally block must close its browser and persist the rotated
                # cookie before the coordinator wipes the profile.
                auth_err = _collapse_flow_login_error(str(exc))
                store.patch_row("accounts", account["id"], {
                    "status": "reconnect",
                    "resetState": "requested",
                    "resetRequestedAt": time.time(),
                    "error": auth_err,
                    "accessToken": None,
                    "accessTokenAt": None,
                    "updatedAt": time.time(),
                })
                self._log(
                    "warning",
                    "session_expired",
                    job_id=job_id,
                    account_id=account["id"],
                    message="Phiên hết hạn — đang mở đăng nhập lại / Session expired — opening re-login",
                )
            if job.get("seriesContext"):
                from . import series
                series.mark_job_error(job, str(exc))
            self._log("error", "job_failed", job_id=job_id, account_id=account["id"], message=str(exc), details={
                "stage": failed_stage,
                "kind": job.get("kind"),
                "model": (job.get("settings") or {}).get("model"),
                "ratio": (job.get("settings") or {}).get("ratio"),
                **({"duration": (job.get("settings") or {}).get("duration")} if job.get("kind") == "video" else {}),
            })
        finally:
            if browser:
                if session_refresh_registered:
                    await self._stop_session_refresh(f"{account['id']}:{id(browser)}", session_refresh_task)
                await self._close_job_browser(
                    browser,
                    job_id,
                    account_id=str(account["id"]),
                    runtime_profile=runtime_profile,
                )
