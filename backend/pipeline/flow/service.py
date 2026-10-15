"""Persistent-account Google Flow service composed from task-specific modules."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

# Re-export legacy helpers and constants so existing service imports keep working.
from . import store
from .accounts import FlowAccountsMixin
from .auth import FlowAuthMixin, _browser_at_login_wall
from .constants import (
    FLOW_LOG_LIMIT,
    POST_PROGRESS_END,
    POST_PROGRESS_START,
    _ACCOUNT_RECOVERY_MAX,
    _COOLDOWN_CREDITS_S,
    _COOLDOWN_QUOTA_S,
    _COOLDOWN_RATELIMIT_S,
    _DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT,
    _DEFAULT_VIDEO_MODEL,
    _DISABLED_VIDEO_MODELS,
    _DURATION_UNITS,
    _FLOW_CONTROL_SELECTOR,
    _FLOW_COOKIE_DOMAINS,
    _FLOW_INFRA_HARD_STOP,
    _FLOW_INFRA_INTERRUPT,
    _FLOW_PLANS,
    _FLOW_SUBMIT_DELAY_MAX_S,
    _FLOW_SUBMIT_DELAY_MIN_S,
    _GENERATION_WAIT_CAP,
    _IMAGE_GENERATION_TIMEOUT_S,
    _IMAGE_UI_MODELS,
    _JOB_AUTO_RETRY_MAX,
    _LOGIN_LEAF_CODES,
    _MAX_CONCURRENT_JOBS_PER_ACCOUNT,
    _PLAN_SYNC_TTL_S,
    _PROFILE_COPY_IGNORES,
    _PROFILE_IDENTITY_DEFAULT,
    _PROFILE_IDENTITY_ROOT,
    _PROJECT_MIGRATION_RECOVERY_WINDOW_S,
    _PROJECT_RE,
    _SESSION_REFRESH_INTERVAL_S,
    _SOFT_RETRY_BACKOFF_CAP_S,
    _TERMINAL,
    _UNUSUAL_ACTIVITY_RESET_MAX,
    _VIDEO_DONE_STATUSES,
    _VIDEO_FAIL_STATUSES,
    _VIDEO_GENERATION_TIMEOUT_S,
    _VIDEO_UI_MODELS,
)
from .controls import (
    FlowControlsMixin,
    _click_settings_pill,
    _disable_flow_agent_mode,
    _dismiss_flow_whats_new,
    _duration_pattern,
    _flow_control_is_selected,
    _flow_control_selected_from_attrs,
    _flow_settings_panel_open,
    _is_settings_trigger,
    _mode_tab_icon,
    _open_flow_settings_panel,
)
from .downloads import (
    FlowDownloadsMixin,
    _pick_video_menu_item,
    _video_download_qualities_for_plan,
    _video_download_quality,
)
from .errors import (
    _FlowNetworkInterrupted,
    _FlowSessionGenerationChanged,
    _classify_visible_flow_error,
    _collapse_flow_login_error,
    _confirmed_login_wall_error,
    _flow_credit_blocked,
    _is_automation_block_error,
    _is_flow_infra_interrupt,
    _is_flow_quota_exhausted,
    _network_ready_for_flow_job,
    _redact_flow_diag_url,
    _rpc_public_error,
    _session_needs_login,
    _soft_retry_backoff_s,
)
from .media import (
    FlowMediaMixin,
    _await_with_job_progress,
    _captured_image_items,
    _captured_video_ids,
    _flow_generation_progress,
    _image_fife_url,
    _media_created_timestamp,
    _media_prompt_matches,
    _video_fife_url,
    _video_media_ready,
    _video_media_status,
)
from .models import (
    _capability_entry,
    _catalog_section,
    _clamp_image_resolution,
    _clean_flow_model_name,
    _detect_plan,
    _ensure_flow_credits_for_job,
    _flow_headless,
    _flow_job_credit_cost,
    _image_ui_resolution,
    _job_concurrency,
    _match_model_choice,
    _normalize_catalog_settings,
    _normalize_image_request_settings,
    _normalize_video_model,
    _official_flow_video_cost,
    _video_model_entry,
    _video_ui_duration,
    _video_ui_resolution,
)
from .outputs import FlowOutputsMixin, _selected_flow_folder
from .profiles import (
    FlowProfilesMixin,
    _account_cookie_records,
    _cookie_identity,
    _cookies_for_fresh_profile,
    _live_flow_cookies,
    _merge_cookie_records,
    _profile_copy_file,
    _profile_copy_ignore,
    _seed_stored_cookies,
    _wipe_profile_keep_login,
)
from .projects import (
    FlowProjectsMixin,
    _flow_project_changed_at,
    _flow_project_field,
    _flow_project_id,
)
from .queue import FlowQueueMixin
from .recovery import FlowRecoveryMixin
from .sessions import FlowSessionsMixin
from .submission import (
    FlowSubmissionMixin,
    _flow_submit_button_score,
    _image_input_type_for_mode,
    _patch_batch_generate_image_inputs,
    _prompt_with_reference_strength,
    _wait_before_flow_submit,
)
from .worker import FlowWorkerMixin


class FlowService(
    FlowProfilesMixin,
    FlowAccountsMixin,
    FlowSessionsMixin,
    FlowAuthMixin,
    FlowRecoveryMixin,
    FlowProjectsMixin,
    FlowControlsMixin,
    FlowSubmissionMixin,
    FlowMediaMixin,
    FlowDownloadsMixin,
    FlowOutputsMixin,
    FlowQueueMixin,
    FlowWorkerMixin,
):
    """Own shared state; task-specific mixins implement the existing service API."""

    def __init__(self) -> None:
        # Keep a newly entered password available for the first connect. The
        # durable copy lives in the account store so reconnect can survive an
        # app restart; this cache is only a short-lived hand-off to _login().
        self._login_passwords: dict[str, str] = {}
        self._account_active: dict[str, int] = {}
        # Monotonic dispatch cursor per account.  Threads are created quickly
        # for bulk jobs, so relying on OS scheduling makes prompts start in a
        # random order even when concurrency is set to 16.
        self._account_next_order: dict[str, int] = {}
        self._account_next_start: dict[str, int] = {}
        self._connecting_accounts: set[str] = set()
        self._connecting_started_at: dict[str, float] = {}
        self._account_session_ready: set[str] = set()
        self._syncing_accounts: set[str] = set()  # guard concurrent credit syncs
        self._plan_sync_locks: dict[str, threading.Lock] = {}
        self._project_recovery_locks: dict[str, threading.Lock] = {}
        self._active_browsers: dict[str, Any] = {}
        self._manual_sessions: dict[str, Any] = {}
        self._auto_close_sessions: set[str] = set()
        self._running_jobs: set[str] = set()
        self._claimed_media_ids: set[str] = set()
        self._claimed_error_tiles: set[str] = set()
        self._cancelled: set[str] = set()
        self._guard = threading.RLock()
        self._account_condition = threading.Condition(self._guard)
        # Set to True during delete_all_jobs to stop enqueue from creating new jobs mid-loop
        self._stop_enqueue: bool = False
        self._random_rr_counter: int = 0
        self._session_refresh_tasks: dict[str, asyncio.Task] = {}
        self._session_refresh_locks: dict[str, threading.Lock] = {}
        self._session_refresh_refs: dict[str, int] = {}
        self._session_refresh_reconnects: set[str] = set()
        self._session_refresh_at: dict[str, float] = {}
        # One lifecycle operation per account.  Runtime job profiles are
        # independent, but admission must still stop while the canonical
        # profile is being replaced.
        self._account_reset_locks: dict[str, threading.Lock] = {}
        self._account_session_generation: dict[str, int] = {}
        # Automatic cookie-only recovery is one operation per account.  The
        # lock is deliberately separate from the manual reset lock so a
        # worker that discovers the same Flow logout can join the operation
        # instead of creating another profile/account row.
        self._account_recovery_locks: dict[str, threading.Lock] = {}
        # Serialize queue-wide persistence commands. Browser cleanup remains
        # asynchronous, but no enqueue/retry write may race delete/cancel.
        self._bulk_queue_lock = threading.RLock()
        self._bulk_queue_epoch = 0
        self._bulk_operations: dict[str, dict[str, Any]] = {}

    def logs(self) -> list[dict[str, Any]]:
        rows = []
        for row in store.list_rows("logs"):
            item = dict(row)
            details = dict(item.get("details") or {})
            if not details.get("accountEmail") and item.get("accountId"):
                account = store.get_row("accounts", str(item["accountId"])) or {}
                email = str(account.get("email") or "").strip()
                if email:
                    details["accountEmail"] = email
            item["details"] = details
            rows.append(item)
        return sorted(rows, key=lambda row: row.get("createdAt", 0), reverse=True)[:FLOW_LOG_LIMIT]

    def clear_logs(self) -> None:
        for row in store.list_rows("logs"):
            store.delete_row("logs", str(row.get("id") or ""))

    def _log(
        self,
        level: str,
        event: str,
        *,
        job_id: str = "",
        account_id: str = "",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        log_details = dict(details or {})
        if account_id and "accountEmail" not in log_details:
            account = store.get_row("accounts", str(account_id)) or {}
            email = str(account.get("email") or "").strip()
            if email:
                log_details["accountEmail"] = email
        store.put_row("logs", {
            "id": uuid.uuid4().hex[:16],
            "level": level,
            "event": event,
            "jobId": job_id,
            "accountId": account_id,
            "message": message,
            "details": log_details,
            "createdAt": time.time(),
        })
        rows = store.list_rows("logs")
        if len(rows) <= FLOW_LOG_LIMIT:
            return
        excess = sorted(rows, key=lambda row: row.get("createdAt", 0))[:-FLOW_LOG_LIMIT]
        store.delete_rows("logs", {str(row.get("id") or "") for row in excess if row.get("id")})

    def start(self) -> None:
        """Stop work left over from the previous app session; the user presses Run again.

        Closing the app kills every browser/worker, so queued/processing rows can no longer be
        running. They become cancelled+stage ``stopped`` (retryable) rather than auto-restarting.
        """
        for job in self.jobs():
            if job.get("status") == "processing" and job.get("stage") == "removing_logo" and self._outputs_exist(job.get("outputs")):
                self._fail_logo_removal(job["id"], list(job.get("outputs")), "interrupted by app restart")
            elif job.get("status") in {"queued", "processing"}:
                store.patch_row("jobs", job["id"], {"status": "cancelled", "stage": "stopped", "progress": 0, "error": None, "updatedAt": time.time()})
            elif job.get("status") == "done" and not self._outputs_exist(job.get("outputs")):
                store.patch_row("jobs", job["id"], {
                    "status": "failed",
                    "stage": "failed",
                    "progress": 0,
                    "error": "FLOW_EMPTY_OUTPUT: generation finished without a downloaded media file",
                    "updatedAt": time.time(),
                })


service = FlowService()
