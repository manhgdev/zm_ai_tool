"""Job persistence, enqueue, cancellation, deletion, and retry operations."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import store
from .constants import POST_PROGRESS_START, _IMAGE_UI_MODELS, _TERMINAL, _VIDEO_UI_MODELS
from .models import (
    _catalog_section,
    _ensure_flow_credits_for_job,
    _flow_headless,
    _job_concurrency,
    _normalize_catalog_settings,
    _normalize_image_request_settings,
    _normalize_video_model,
)
from .projects import _flow_project_id

_log = logging.getLogger("pipeline.flow.service")


class FlowQueueMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    def jobs(self) -> list[dict[str, Any]]:
        # Queue order is FIFO: the first prompt stays at the top and is the
        # first job resumed after an app restart.  History can still sort by
        # timestamp in the UI when a newest-first view is appropriate.
        # Hot path: never run legacy folder migration here — with hundreds of
        # jobs it rewrites jobs.json / hits disk and starves DELETE / cancel.
        account_status = {
            str(acc.get("id") or ""): str(acc.get("status") or "")
            for acc in store.list_rows("accounts")
        }
        rows: list[dict[str, Any]] = []
        for row in sorted(store.list_rows("jobs"), key=lambda item: item.get("createdAt", 0)):
            item = dict(row)
            status = str(item.get("status") or "")
            stage = str(item.get("stage") or "")
            if status in {"done", "failed", "cancelled"}:
                item["fallbackNotice"] = None
                item["recoveryNotice"] = None
            elif (
                item.get("recoveryNotice")
                and stage not in {"recovering_session", "waiting_session_reset", "action_required"}
                and account_status.get(str(item.get("accountId") or "")) == "online"
            ):
                item["recoveryNotice"] = None
            settings = item.get("settings")
            if isinstance(settings, dict) and str(settings.get("outputDir") or "").strip():
                try:
                    item["displayOutputFolder"] = str(self._display_output_folder(item))
                    item["outputFolder"] = str(self._output_folder(item, create=False))
                except OSError:
                    raw = str(settings.get("outputDir") or "")
                    item["outputFolder"] = raw
                    item["displayOutputFolder"] = raw
            rows.append(item)
        return rows

    def delete_job(self, job_id: str) -> bool:
        job = store.get_row("jobs", job_id)
        if not job:
            return False
        if job.get("status") not in _TERMINAL:
            with self._account_condition:
                self._cancelled.add(job_id)
                self._account_condition.notify_all()
        with self._bulk_queue_lock:
            if self._stop_enqueue:
                return False
            removed = store.delete_row("jobs", job_id)
        if removed:
            # DELETE must not wait on a stuck Playwright/Chrome process.
            threading.Thread(
                target=self._kill_job_browser,
                args=(job_id,),
                daemon=True,
                name="flow-delete-kill",
            ).start()
            def _cleanup_deleted_job() -> None:
                for raw_output in job.get("outputs") or []:
                    try:
                        Path(str(raw_output)).unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    folder = self._output_folder(job, create=False)
                    remaining = {
                        Path(str(row.get("outputFolder") or "")).resolve()
                        for row in store.list_rows("jobs")
                        if str(row.get("outputFolder") or "").strip()
                    }
                    if folder.resolve() not in remaining and folder.is_dir():
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
            threading.Thread(target=_cleanup_deleted_job, daemon=True, name="flow-delete-cleanup").start()
        return removed

    def cancel_all(self) -> int:
        with self._bulk_queue_lock:
            ids = {str(job['id']) for job in store.list_rows('jobs') if job.get('status') not in _TERMINAL}
            with self._account_condition:
                self._cancelled.update(ids)
                self._account_condition.notify_all()
            threading.Thread(target=self._kill_job_browsers, args=(ids,), daemon=True, name="flow-bulk-kill").start()
            count = store.cancel_active_jobs(ids, time.time())
            self._bulk_queue_epoch += 1
        return count

    def cancel_output_folder_jobs(self, output_dir: str, kind: str = "") -> int:
        """Cancel only queued or running jobs stored in one Flow folder."""
        selected = str(output_dir or "").strip()
        if not selected:
            return 0
        ids = set()
        for job in self.jobs():
            if str((job.get("settings") or {}).get("outputDir") or "").strip() != selected:
                continue
            if kind and str(job.get("kind") or "") != kind:
                continue
            if job.get("status") not in _TERMINAL:
                ids.add(str(job["id"]))
        if not ids:
            return 0
        with self._account_condition:
            self._cancelled.update(ids)
            self._account_condition.notify_all()
        return store.cancel_active_jobs(ids, time.time())

    def delete_all_jobs(self) -> int:
        self._stop_enqueue = True  # Prevent enqueue from creating more jobs mid-loop
        self._bulk_queue_lock.acquire()
        try:
            selected = store.list_rows("jobs")
            ids = {str(job["id"]) for job in selected}
            if not ids:
                return 0
            output_paths = [Path(str(raw)) for job in selected for raw in job.get("outputs") or []]
            # Mark cancelled without waiting on the worker condition — set updates
            # are enough for in-flight checks; notify is best-effort below.
            self._cancelled.update(ids)
            threading.Thread(target=self._kill_job_browsers, args=(ids,), daemon=True, name="flow-delete-all-kill").start()
            # Wipe DB first so F5 / GET see an empty queue even if notify is delayed.
            removed = store.delete_rows("jobs", ids)
            for acc in store.list_rows("accounts"):
                lane_reset = {k: None for k in acc if str(k).startswith("laneProject_")}
                if lane_reset:
                    store.patch_row("accounts", str(acc.get("id") or ""), lane_reset)
            self._bulk_queue_epoch += 1
            acquired = self._account_condition.acquire(timeout=0.5)
            if acquired:
                try:
                    self._account_condition.notify_all()
                finally:
                    self._account_condition.release()
            # Catch jobs that slipped through before _stop_enqueue took effect
            straggler = store.list_rows("jobs")
            if straggler:
                straggler_ids = {str(job["id"]) for job in straggler}
                self._cancelled.update(straggler_ids)
                store.delete_rows("jobs", straggler_ids)
                acquired = self._account_condition.acquire(timeout=0.5)
                if acquired:
                    try:
                        self._account_condition.notify_all()
                    finally:
                        self._account_condition.release()
            # Delete output files in background so the API response is instant
            def _cleanup_files():
                with ThreadPoolExecutor(max_workers=min(8, max(1, len(output_paths)))) as pool:
                    list(pool.map(lambda p: p.unlink(missing_ok=True), output_paths))
                # Prefer remembered absolute folders from the job rows; avoid
                # _output_folder() which can probe ~/Downloads on every row.
                unique_folders: set[Path] = set()
                for job in selected + removed:
                    raw_folder = str(job.get("outputFolder") or "").strip()
                    if raw_folder:
                        unique_folders.add(Path(raw_folder))
                    else:
                        try:
                            unique_folders.add(self._output_folder(job, create=False))
                        except OSError:
                            pass
                remaining = {
                    Path(str(j.get("outputFolder") or "")).resolve()
                    for j in store.list_rows("jobs")
                    if str(j.get("outputFolder") or "").strip()
                }
                for folder in unique_folders:
                    try:
                        resolved = folder.resolve()
                        if resolved not in remaining and folder.is_dir():
                            shutil.rmtree(folder, ignore_errors=True)
                        elif folder.is_dir():
                            folder.rmdir()
                    except OSError:
                        pass
            threading.Thread(target=_cleanup_files, daemon=True, name="flow-delete-cleanup").start()
            return len(removed)
        finally:
            self._bulk_queue_lock.release()
            self._stop_enqueue = False

    def delete_output_folder_jobs(self, output_dir: str, kind: str = "") -> int:
        """Delete only the jobs and real artifacts belonging to one Flow folder."""
        selected = str(output_dir or "").strip()
        if not selected:
            return 0
        # Lightweight list — avoid self.jobs() (folder migrate / path resolve) on the request path.
        matched = [
            job for job in store.list_rows("jobs")
            if str((job.get("settings") or {}).get("outputDir") or "").strip() == selected
            and (not kind or str(job.get("kind") or "") == kind)
        ]
        if not matched:
            return 0
        raw_folder = str(matched[0].get("outputFolder") or "").strip()
        try:
            folder = Path(raw_folder) if raw_folder else self._output_folder(matched[0], create=False)
        except OSError:
            folder = Path(raw_folder) if raw_folder else Path()
        ids = {str(job["id"]) for job in matched}
        output_paths = [Path(str(raw)) for job in matched for raw in job.get("outputs") or []]
        self._cancelled.update(ids)
        threading.Thread(target=self._kill_job_browsers, args=(ids,), daemon=True, name="flow-folder-kill").start()
        removed = store.delete_rows("jobs", ids)
        from pipeline.core.output_paths import safe_output_part
        folder_key = safe_output_part(selected, "results")
        prefixes = (
            (f"laneProject_{safe_output_part(kind)}_{folder_key}_",)
            if kind else (f"laneProject_image_{folder_key}_", f"laneProject_video_{folder_key}_")
        )
        for acc in store.list_rows("accounts"):
            lane_reset = {k: None for k in acc if any(str(k).startswith(p) for p in prefixes)}
            if lane_reset:
                store.patch_row("accounts", str(acc.get("id") or ""), lane_reset)
        acquired = self._account_condition.acquire(timeout=0.5)
        if acquired:
            try:
                self._account_condition.notify_all()
            finally:
                self._account_condition.release()
        # File + folder cleanup in background so the API returns as soon as DB is clean.
        def _cleanup_files():
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(output_paths)))) as pool:
                list(pool.map(lambda p: p.unlink(missing_ok=True), output_paths))
            for job in removed:
                for raw in job.get("outputs") or []:
                    try:
                        Path(str(raw)).unlink(missing_ok=True)
                    except OSError:
                        pass
            if not folder.parts:
                return
            remaining = {
                Path(str(j.get("outputFolder") or "")).resolve()
                for j in store.list_rows("jobs")
                if str(j.get("outputFolder") or "").strip()
            }
            try:
                if folder.resolve() not in remaining and folder.is_dir():
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass
        threading.Thread(target=_cleanup_files, daemon=True, name="flow-folder-delete-cleanup").start()
        return len(removed)

    def enqueue(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        # Capture the queue epoch before doing validation/profile path work.
        # A concurrent cancel/delete must be able to invalidate this request
        # before its final batch write, rather than letting stale rows appear
        # after the bulk operation has completed.
        with self._bulk_queue_lock:
            if self._stop_enqueue:
                raise ValueError("FLOW_QUEUE_BULK_BUSY: Queue bulk operation is in progress")
            enqueue_epoch = self._bulk_queue_epoch
        prompts = [str(value).strip() for value in payload.get("prompts", []) if str(value).strip()]
        if not prompts:
            raise ValueError("Prompts are required")

        account_id = str(payload.get("accountId") or "").strip()
        kind = str(payload.get("kind") or "video")
        mode = str(payload.get("mode") or "text")
        settings = dict(payload.get("settings") or {})
        settings["headless"] = _flow_headless(settings)
        if kind == "image":
            # Image controls have dedicated UI fields; make the API boundary
            # authoritative so Prompt and quick settings cannot diverge.
            settings = _normalize_image_request_settings(settings)
        model = settings.get("model")

        is_random = account_id in {"random", "auto", ""}
        # A concrete account selection is intentional: do not silently move
        # the job to another account unless the caller explicitly opts in.
        # Random/auto selection keeps the historical account distribution and
        # fallback behavior by default.
        allow_fallback = bool(payload.get("allowAccountFallback", is_random))

        if is_random:
            chosen = self._pick_eligible_account(kind=kind, model=model)
            if not chosen:
                if kind == "image" and model == "Nano Banana Pro":
                    raise ValueError(
                        "FLOW_PLAN_INSUFFICIENT: Không có tài khoản Pro/Plus/Ultra nào online còn tín dụng để tạo ảnh Nano Banana Pro (tài khoản Free không hỗ trợ)."
                    )
                raise ValueError("FLOW_NO_ONLINE_ACCOUNTS: Không có tài khoản Flow nào đang online còn tín dụng.")
            account_id = str(chosen.get("id"))
            account = chosen
        else:
            account = store.get_row("accounts", account_id) or {}
            if not account:
                raise ValueError("Prompts and a valid Flow account are required")
            if account.get("status") != "online" or not _flow_project_id(account, kind, legacy_for_video=True):
                raise ValueError("FLOW_LOGIN_REQUIRED: Connect the selected Flow account before generating.")
            if self._account_suspended(account_id, kind, str(model or "")):
                if allow_fallback:
                    alt = self._pick_eligible_account(kind=kind, model=model, exclude_ids=[account_id])
                    if alt:
                        account_id = str(alt.get("id"))
                        account = alt
                    else:
                        raise ValueError(self._account_suspension_error(account, kind, str(model or "")))
                else:
                    raise ValueError(self._account_suspension_error(account, kind, str(model or "")))
            if account.get("plan") == "Free" and kind == "image" and model == "Nano Banana Pro":
                alt = self._pick_eligible_account(kind=kind, model=model, exclude_ids=[account_id])
                if alt:
                    self._log(
                        "info",
                        "account_fallback_plan",
                        account_id=alt["id"],
                        message=f"Tài khoản {account.get('label')} là Free không hỗ trợ Nano Banana Pro; tự động fallback sang {alt.get('label')}",
                    )
                    account_id = str(alt.get("id"))
                    account = alt
                else:
                    raise ValueError(
                        "FLOW_PLAN_INSUFFICIENT: Tài khoản Free không hỗ trợ tạo ảnh Nano Banana Pro. Vui lòng chọn gói Pro/Plus/Ultra hoặc đổi sang model Nano Banana 2."
                    )

        settings["concurrency"] = _job_concurrency(settings)
        input_type = str(payload.get("inputType") or "prompt").lower()
        if input_type not in {"prompt", "txt", "csv", "json"}:
            input_type = "prompt"
        source_files = list(payload.get("sourceFiles") or [])
        series_context = dict(payload.get("seriesContext") or {})
        # Queue creation must stay fast. Entitlement sync opens Chrome and can
        # take minutes; doing it here made the UI look stuck and prevented the
        # user from seeing/cancelling the complete batch. The worker verifies
        # the account immediately before submitting each job.
        # Credits gate video only; image eligibility still respects plan/quota.
        credits = account.get("credits")
        if kind == "video" and isinstance(credits, (int, float)) and int(credits) <= 0:
            raise ValueError("FLOW_CREDITS_EMPTY: Hết tín dụng — nạp thêm hoặc đợi reset (Out of Flow credits)")

        settings, _ = _normalize_catalog_settings(account, kind, settings)
        _ensure_flow_credits_for_job(account, kind, settings)

        if kind == "image":
            if not _catalog_section(account, kind) and str(settings.get("model") or "Nano Banana 2") not in _IMAGE_UI_MODELS:
                raise ValueError(f"Unsupported Flow image model: {settings.get('model')}")
            if mode != "text" and not source_files:
                raise ValueError("Image edit/reference mode requires at least one source image")
            if mode == "edit" and len(source_files) > 1:
                source_files = source_files[:1]
            elif mode == "reference" and len(source_files) > 3:
                source_files = source_files[:3]
            try:
                settings["referenceStrength"] = max(0, min(100, int(settings.get("referenceStrength", 70))))
            except (TypeError, ValueError):
                settings["referenceStrength"] = 70
        else:
            if not _catalog_section(account, kind):
                settings["model"] = _normalize_video_model(settings.get("model"))
            if not _catalog_section(account, kind) and settings["model"] not in _VIDEO_UI_MODELS:
                raise ValueError(f"Unsupported Flow video model: {settings.get('model')}")
        created = []
        pending: list[dict] = []
        now = time.time()
        extra_counts: dict[str, int] = {}
        for index, prompt in enumerate(prompts, 1):
            if self._stop_enqueue:
                # delete-all fired mid-submission — stop creating jobs immediately
                break
            job_input_index = int(payload.get("inputIndex") or series_context.get("sceneIndex") or index)
            if is_random:
                picked = self._pick_eligible_account(kind=kind, model=model, extra_counts=extra_counts)
                job_account = picked or account
                job_account_id = str(job_account.get("id"))
                extra_counts[job_account_id] = extra_counts.get(job_account_id, 0) + 1
            else:
                job_account = account
                job_account_id = account_id

            with self._account_condition:
                order = self._account_next_order.get(job_account_id)
                if order is None:
                    # Lightweight rows only — never call self.jobs() under this lock
                    # (jobs() migrates folders / resolves paths and blocks delete-all).
                    persisted_orders = [
                        int(row.get("queueOrder")) for row in store.list_rows("jobs")
                        if row.get("accountId") == job_account_id and str(row.get("queueOrder", "")).isdigit()
                    ]
                    order = max(persisted_orders, default=-1) + 1
                self._account_next_order[job_account_id] = order + 1
            job = {
                "id": uuid.uuid4().hex[:12], "inputIndex": job_input_index, "kind": kind,
                "queueOrder": order,
                "mode": mode, "prompt": prompt, "accountId": job_account_id,
                "inputType": input_type,
                # Each queued row owns an immutable-at-enqueue snapshot. The
                # worker may migrate or retry one row without changing the
                # settings used by later prompts in the same sequential batch.
                "settings": deepcopy(settings), "sourceFiles": list(source_files),
                "seriesContext": deepcopy(series_context),
                "status": "queued", "stage": "queued", "progress": 0, "mediaIds": [], "outputs": [],
                "startedAt": None, "finishedAt": None, "elapsedSeconds": 0.0,
                "fallbackNotice": None, "recoveryNotice": None,
                "generationRejectRetryCount": 0,
                "generationToken": uuid.uuid4().hex,
                "autoRetryCount": 0,
                "allowAccountFallback": allow_fallback,
                "randomAccount": is_random,
                "triedAccountIds": [job_account_id],
                "error": None, "createdAt": now, "updatedAt": now,
            }
            job["outputFolder"] = str(self._output_folder(job, create=False))
            job["displayOutputFolder"] = str(self._display_output_folder(job))
            pending.append(job)

        # Batch insert: one read + one write for all N jobs (O(N) vs O(N²) for N put_row calls)
        with self._bulk_queue_lock:
            if self._stop_enqueue or enqueue_epoch != self._bulk_queue_epoch:
                raise ValueError("FLOW_QUEUE_BULK_BUSY: Queue bulk operation is in progress")
            store.put_rows("jobs", pending)

        for job in pending:
            if series_context:
                from . import series
                series.register_job(job)
            self._log("info", "job_queued", job_id=job["id"], account_id=job["accountId"], details={"kind": job["kind"], "inputIndex": job["inputIndex"]})
            created.append(job)
            threading.Thread(target=self._run_sync, args=(job["id"],), daemon=True, name=f"flow-job-{job['id']}").start()
        return created

    def _check_cancel(self, job_id: str) -> None:
        if job_id in self._cancelled:
            raise asyncio.CancelledError

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._account_condition:
            self._cancelled.add(job_id)
            self._account_condition.notify_all()
        # Kill the owned job browser before acknowledging cancellation. The
        # old background-only kill let a Playwright call keep generating after
        # the UI already showed the job as cancelled.
        self._kill_job_browser(job_id)
        job = store.patch_row("jobs", job_id, {"status": "cancelled", "stage": "cancelled", "progress": 0, "updatedAt": time.time()})
        if job:
            self._log("warning", "job_cancel_requested", job_id=job_id, account_id=str(job.get("accountId") or ""))
        return job

    def retry_many(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Requeue many jobs with one jobs.json transaction.

        The generation workers remain unchanged; this only removes the
        request-per-row and JSON-write-per-row overhead of the queue action.
        """
        operation_id = uuid.uuid4().hex[:12]
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        logo_only_job_ids: set[str] = set()
        with self._bulk_queue_lock:
            if self._stop_enqueue:
                raise ValueError("FLOW_QUEUE_BULK_BUSY: Queue bulk operation is in progress")
            account_rows = {str(row.get("id")): row for row in store.list_rows("accounts") if row.get("id")}
            rows = {str(row.get("id")): row for row in store.list_rows("jobs") if row.get("id")}
            patches: dict[str, dict[str, Any]] = {}
            for item in items:
                job_id = str(item.get("jobId") or item.get("id") or "").strip()
                existing = rows.get(job_id)
                if not existing:
                    rejected.append({"jobId": job_id, "error": "FLOW_JOB_NOT_FOUND"})
                    continue
                try:
                    overrides = dict(item)
                    requested_account_id = str(overrides.get("accountId") or existing.get("accountId") or "").strip()
                    account_id = requested_account_id
                    kind = str(existing.get("kind") or "video")
                    settings = dict(existing.get("settings") or {})
                    settings.update({
                        key: value for key, value in dict(overrides.get("settings") or {}).items()
                        if value not in (None, "")
                    })
                    logo_failed = str(existing.get("error") or "").startswith("FLOW_LOGO_REMOVE_FAILED")
                    explicit_logo_only = bool(overrides.get("logoOnly"))
                    outputs_on_disk = self._outputs_exist(existing.get("outputs"))
                    if explicit_logo_only and not outputs_on_disk:
                        raise ValueError("FLOW_LOGO_NO_OUTPUTS: Downloaded files are missing; cannot remove logo only")
                    # Batch retry must preserve the single-job logo-only path:
                    # it operates on downloaded files and must never submit a
                    # new Flow generation or clear the existing outputs.
                    if (explicit_logo_only or (logo_failed and outputs_on_disk)) and outputs_on_disk:
                        account = account_rows.get(str(existing.get("accountId") or "")) or {}
                        if str(account.get("plan") or "") == "Ultra":
                            raise ValueError("FLOW_LOGO_SKIPPED_ULTRA: Ultra accounts have no Flow logo to remove")
                        settings["removeLogo"] = True
                        patches[job_id] = {
                            "status": "processing", "stage": "removing_logo",
                            "progress": POST_PROGRESS_START, "error": None,
                            "settings": settings, "updatedAt": time.time(),
                        }
                        logo_only_job_ids.add(job_id)
                        accepted.append({"jobId": job_id, "accountId": str(existing.get("accountId") or "")})
                        continue
                    if kind == "image":
                        requested = dict(overrides.get("settings") or {})
                        for key, image_key in (("model", "imageModel"), ("ratio", "imageRatio"), ("resolution", "imageResolution"), ("count", "imageCount")):
                            if requested.get(key) not in (None, ""):
                                settings[image_key] = requested[key]
                            elif requested.get(image_key) in (None, "") and settings.get(key) not in (None, ""):
                                settings[image_key] = settings[key]
                        settings = _normalize_image_request_settings(settings)
                    model = settings.get("model")
                    if account_id in {"random", "auto", ""}:
                        chosen = self._pick_eligible_account(kind=kind, model=model)
                        account = chosen or {}
                        account_id = str(account.get("id") or "")
                    else:
                        account = account_rows.get(account_id) or {}
                    if not account or account.get("status") != "online" or not _flow_project_id(account, kind, legacy_for_video=True):
                        raise ValueError("FLOW_LOGIN_REQUIRED: Connect the selected Flow account before retrying.")
                    suspended = self._account_suspended(account_id, kind, str(model or ""))
                    if suspended and not bool(overrides.get("manual")):
                        raise ValueError(self._account_suspension_error(account, kind, str(model or "")))
                    settings, _ = _normalize_catalog_settings(account, kind, settings)
                    fresh = bool(overrides.get("fresh"))
                    force_new = bool(fresh and existing.get("status") == "done" and not existing.get("error"))
                    recover_submission = bool(
                        not force_new
                        and existing.get("submissionStartedAt")
                        and not existing.get("mediaIds")
                        and not existing.get("outputs")
                        and re.search(r"FLOW_GENERATION_TIMEOUT|no completed\s+(?:image|video)\s+media appeared after submit", str(existing.get("error") or ""), re.I)
                    ) or bool(
                        kind == "video"
                        and existing.get("mediaIds")
                        and not existing.get("outputs")
                        and re.search(r"FLOW_DOWNLOAD_FAILED", str(existing.get("error") or ""), re.I)
                    )
                    if bool(overrides.get("manual")) and requested_account_id not in {"random", "auto", ""} and suspended:
                        if kind == "image" and model:
                            self.clear_model_suspension(account_id, kind, str(model))
                        else:
                            self.clear_account_suspension(account_id)
                    recovery_fields = {"resumeOnly": True} if recover_submission else {
                        "resumeOnly": False, "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "baselineThumbs": [], "mediaIds": [],
                    }
                    queue_order = int(existing.get("queueOrder")) if str(existing.get("queueOrder", "")).isdigit() else int(existing.get("inputIndex", 0))
                    patches[job_id] = {
                        **recovery_fields,
                        "status": "queued", "stage": "queued", "progress": 0,
                        "queueOrder": queue_order, "error": None, "outputs": [],
                        "recoveryNotice": None,
                        **({"fallbackNotice": None} if bool(overrides.get("manual")) else {}),
                        "accountId": account_id, "settings": settings,
                        "generationToken": uuid.uuid4().hex, "mode": str(overrides.get("mode") or existing.get("mode") or "text"),
                        "generationRejectRetryCount": 0, "autoRetryCount": 0, "authRetryCount": 0,
                        "sessionResetRetryCount": 0, "forceNew": force_new, "logoDone": [],
                        "updatedAt": time.time(),
                    }
                    accepted.append({"jobId": job_id, "accountId": account_id})
                except Exception as exc:
                    rejected.append({"jobId": job_id, "error": str(exc)})
            changed = store.patch_rows("jobs", patches)
            self._bulk_queue_epoch += 1
            for row in changed:
                account_id = str(row.get("accountId") or "")
                if account_id and str(row.get("queueOrder", "")).isdigit():
                    self._account_next_order[account_id] = max(
                        self._account_next_order.get(account_id, 0), int(row["queueOrder"]) + 1,
                    )
            with self._account_condition:
                self._account_condition.notify_all()
        self._bulk_operations[operation_id] = {
            "id": operation_id, "status": "completed", "acceptedCount": len(changed),
            "rejectedCount": len(rejected), "createdAt": time.time(),
        }
        for row in changed:
            self._cancelled.discard(str(row.get("id") or ""))
            row_id = str(row.get("id") or "")
            if row_id in logo_only_job_ids:
                threading.Thread(
                    target=lambda jid=row_id: asyncio.run(self._logo_only_retry(jid)),
                    daemon=True, name=f"flow-logo-{row_id}",
                ).start()
                continue
            self._release_media_ids(rows.get(row_id, {}).get("mediaIds"))
            threading.Thread(target=self._run_sync, args=(row_id,), daemon=True, name=f"flow-job-{row_id}").start()
        return {
            "operationId": operation_id, "status": "completed", "acceptedCount": len(changed),
            "rejectedCount": len(rejected), "rejected": rejected,
            "jobs": changed,
        }

    def retry(self, job_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
        existing = store.get_row("jobs", job_id)
        if not existing:
            return None
        with self._bulk_queue_lock:
            if self._stop_enqueue:
                raise ValueError("FLOW_QUEUE_BULK_BUSY: Queue bulk operation is in progress")
            retry_epoch = self._bulk_queue_epoch
        overrides = overrides or {}
        logo_failed = str(existing.get("error") or "").startswith("FLOW_LOGO_REMOVE_FAILED")
        explicit_logo_only = bool(overrides.get("logoOnly"))
        outputs_on_disk = self._outputs_exist(existing.get("outputs"))
        # Explicit logoOnly from UI must never fall through into full _run_sync.
        if explicit_logo_only and not outputs_on_disk:
            raise ValueError(
                "FLOW_LOGO_NO_OUTPUTS: Downloaded files are missing; cannot remove logo only"
            )
        logo_only = explicit_logo_only or (logo_failed and outputs_on_disk)
        if logo_only and outputs_on_disk:
            # Downloaded media already exists: only the logo step (no Flow submit).
            # Keep logoDone so already-cleaned files are skipped.
            account = store.get_row("accounts", str(existing.get("accountId") or "")) or {}
            if str(account.get("plan") or "") == "Ultra":
                raise ValueError(
                    "FLOW_LOGO_SKIPPED_ULTRA: Ultra accounts have no Flow logo to remove"
                )
            with self._bulk_queue_lock:
                if self._stop_enqueue or retry_epoch != self._bulk_queue_epoch:
                    raise ValueError("FLOW_QUEUE_BULK_BUSY: Queue bulk operation is in progress")
                if not store.get_row("jobs", job_id):
                    return None
                self._cancelled.discard(job_id)
                # Mark Del Logo so queue meta updates; logoDone on success hides Xoá logo.
                settings = dict(existing.get("settings") or {})
                settings["removeLogo"] = True
                job = store.patch_row("jobs", job_id, {
                    "status": "processing",
                    "stage": "removing_logo",
                    "progress": POST_PROGRESS_START,
                    "error": None,
                    "settings": settings,
                    "updatedAt": time.time(),
                })
            self._log("info", "job_retry_logo_only", job_id=job_id, account_id=str(existing.get("accountId") or ""))
            threading.Thread(
                target=lambda: asyncio.run(self._logo_only_retry(job_id)), daemon=True, name=f"flow-logo-{job_id}",
            ).start()
            return job
        manual_retry = bool(overrides.get("manual"))
        download_recovery = bool(
            existing.get("kind") == "video"
            and existing.get("mediaIds")
            and not existing.get("outputs")
            and re.search(r"FLOW_DOWNLOAD_FAILED", str(existing.get("error") or ""), re.I)
        )
        # A completed Flow video must never be regenerated just because the
        # UI sent a stale fresh flag; retrying this error means download only.
        force_new = bool(overrides.get("fresh")) and not download_recovery and bool(
            existing.get("status") == "done" and not existing.get("error")
        )
        # A job stopped by closing the app (see start()) was already submitted:
        # resume it, never submit a duplicate generation.
        resume_stopped = bool(
            not force_new
            and existing.get("status") == "cancelled"
            and existing.get("stage") == "stopped"
            and not existing.get("outputs")
            and (existing.get("submissionStartedAt") or existing.get("mediaIds"))
        )
        # A timeout can be reported before Flow finishes rendering. Retrying
        # that job should resume the persisted submission and claim its late
        # media, rather than submitting a duplicate generation. Explicit
        # ``fresh`` keeps the old create-new behavior.
        recover_submission = bool(
            not force_new
            and existing.get("submissionStartedAt")
            and not existing.get("mediaIds")
            and not existing.get("outputs")
            and re.search(
                r"FLOW_GENERATION_TIMEOUT|no completed\s+(?:image|video)\s+media appeared after submit",
                str(existing.get("error") or ""), re.I,
            )
        ) or download_recovery or resume_stopped
        requested_account_id = str(overrides.get("accountId") or existing.get("accountId") or "").strip()
        account_id = requested_account_id
        settings = dict(existing.get("settings") or {})
        # Keep falsey booleans (headless/removeLogo); only drop null/empty string.
        settings.update({
            key: value for key, value in dict(overrides.get("settings") or {}).items()
            if value not in (None, "")
        })
        mode = str(existing.get("mode") or "text")
        requested_mode = str(overrides.get("mode") or "").strip()
        if requested_mode in {"text", "edit", "reference", "frame"}:
            mode = requested_mode
        # Prefer explicit top-level headless from the dialog; else settings{}
        # already merged above. Never invent True when the client omitted it.
        if "headless" in overrides:
            settings["headless"] = _flow_headless({"headless": overrides["headless"]})
        elif "headless" in dict(overrides.get("settings") or {}):
            settings["headless"] = _flow_headless(settings)
        kind = str(existing.get("kind") or "video")
        if kind == "image":
            # Popup values are canonical for this retry. Mirror them into the
            # image-prefixed fields before normalization so stale snapshot
            # fields cannot overwrite a newly selected model/ratio/resolution.
            requested_settings = dict(overrides.get("settings") or {})
            for key, image_key in (("model", "imageModel"), ("ratio", "imageRatio"), ("resolution", "imageResolution"), ("count", "imageCount")):
                if requested_settings.get(key) not in (None, ""):
                    settings[image_key] = requested_settings[key]
                elif requested_settings.get(image_key) in (None, "") and settings.get(key) not in (None, ""):
                    settings[image_key] = settings[key]
            settings = _normalize_image_request_settings(settings)
        model = settings.get("model")

        if account_id in {"random", "auto", ""}:
            chosen = self._pick_eligible_account(kind=kind, model=model)
            if chosen:
                account = chosen
                account_id = str(chosen["id"])
            else:
                account = {}
        else:
            account = store.get_row("accounts", account_id) or {}
            if account.get("status") != "online" or not _flow_project_id(account, kind, legacy_for_video=True):
                raise ValueError("FLOW_LOGIN_REQUIRED: Connect the selected Flow account before retrying.")
            if account.get("plan") == "Free" and kind == "image" and model == "Nano Banana Pro":
                alt = self._pick_eligible_account(kind=kind, model=model, exclude_ids=[account_id])
                if alt:
                    account = alt
                    account_id = str(alt["id"])

        if not account:
            raise ValueError("FLOW_NO_ONLINE_ACCOUNTS: No eligible connected Flow account is available.")
        if (
            manual_retry
            and requested_account_id not in {"random", "auto", ""}
            and self._account_suspended(account_id, kind, str(model or ""))
        ):
            # Only the explicit UI retry may override a cooldown. Automatic
            # retries and internal model fallbacks must keep the quarantine.
            if kind == "image" and model:
                self.clear_model_suspension(account_id, kind, str(model))
            else:
                self.clear_account_suspension(account_id)
        settings, _ = _normalize_catalog_settings(account, kind, settings)
        if existing.get("kind") == "video" and not _catalog_section(account, "video"):
            settings["model"] = _normalize_video_model(settings.get("model"))
        with self._account_condition:
            last_error_signature = str(existing.get("lastFlowErrorSignature") or "")
            if last_error_signature:
                self._claimed_error_tiles.discard(last_error_signature)
            orders = [
                int(row.get("queueOrder")) for row in store.list_rows("jobs")
                if row.get("accountId") == account_id and str(row.get("queueOrder", "")).isdigit()
            ]
            order = max(orders, default=-1) + 1
            # Retries keep their original place so lower numbers run first.
            if str(existing.get("queueOrder", "")).isdigit():
                order = int(existing["queueOrder"])
            self._account_next_order[account_id] = max(self._account_next_order.get(account_id, 0), max(orders, default=-1) + 2)
            self._cancelled.discard(job_id)
            # Preserve the original submission only for a timed-out job with
            # no result yet. Other retries still create a fresh generation.
            recovery_fields = (
                {
                    "resumeOnly": True,
                }
                if recover_submission
                else {
                    "resumeOnly": False,
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "baselineThumbs": [],
                    "mediaIds": [],
                }
            )
            self._release_media_ids(existing.get("mediaIds"))
            with self._bulk_queue_lock:
                if self._stop_enqueue or retry_epoch != self._bulk_queue_epoch:
                    raise ValueError("FLOW_QUEUE_BULK_BUSY: Queue bulk operation is in progress")
                if not store.get_row("jobs", job_id):
                    return None
                job = store.patch_row("jobs", job_id, {
                    **recovery_fields,
                    "status": "queued", "stage": "queued", "progress": 0,
                    "queueOrder": order, "error": None, "outputs": [], "updatedAt": time.time(),
                    "recoveryNotice": None,
                    **({"fallbackNotice": None} if manual_retry else {}),
                    "accountId": account_id, "settings": settings,
                    "generationToken": uuid.uuid4().hex,
                    "mode": mode,
                    "generationRejectRetryCount": 0,
                    "autoRetryCount": 0,
                    "authRetryCount": 0,
                    "sessionResetRetryCount": 0,
                    "forceNew": force_new,
                    "logoDone": [],
                })
            self._account_condition.notify_all()
        if job:
            if job.get("seriesContext"):
                try:
                    from . import series
                    series.register_job(job)
                except Exception as exc:
                    _log.debug("series.register_job on retry failed: %s", exc)
            self._log("info", "job_retry", job_id=job_id, account_id=str(job.get("accountId") or ""))
            threading.Thread(target=self._run_sync, args=(job_id,), daemon=True, name=f"flow-job-{job_id}").start()
        return job
