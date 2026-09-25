"""Persistent-account Google Flow worker built around flow-py."""
from __future__ import annotations

import asyncio
import logging
import os
import re
try:
    from ._flow._models import GenerationMode as _GenerationMode
except Exception:  # ponytail: graceful if flow lib version lacks this
    _GenerationMode = None
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from pipeline.core.config import PUBLIC_DATA
from pipeline.core.output_paths import safe_output_part, selected_or_default
from . import store

# Match both legacy labs.google/fx/tools/flow/project/<id> and new flow.google.com/project/<id>
_PROJECT_RE = re.compile(r"^(?:https://(?:flow\.google\.com|labs\.google)(?::443)?)?(?:/fx/tools/flow|/flow)?/project/([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:[/?#]|$)")
_TERMINAL = {"done", "failed", "cancelled", "action_required"}
_DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT = 8
_MAX_CONCURRENT_JOBS_PER_ACCOUNT = 50
_PROJECT_MIGRATION_RECOVERY_WINDOW_S = 600
_PROFILE_COPY_IGNORES = {
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "GraphiteDawnCache", "GPUPersistentCache", "ShaderCache", "GrShaderCache",
    "Crashpad", "SingletonCookie", "SingletonLock", "SingletonSocket", "LOCK",
    # Chrome writes this file while starting and can remove it during shutdown;
    # it is not part of the Google session state needed by Flow.
    "RunningChromeVersion",
}
_IMAGE_UI_MODELS = {"Nano Banana Pro", "Nano Banana 2", "Nano Banana 2 Lite"}
_VIDEO_UI_MODELS = {
    "Omni 1.1 Flash", "Veo 3.1 - Lite", "Veo 3.1 - Fast",
    "Veo 3.1 - Quality", "Veo 3.1 - Lite [Lower Priority]",
}
# Temporarily disabled upstream; remove this entry to re-enable when Flow restores it.
_DISABLED_VIDEO_MODELS = {"Veo 3.1 - Lite [Lower Priority]"}
_DEFAULT_VIDEO_MODEL = "Veo 3.1 - Fast"


def _normalize_video_model(value: Any) -> str:
    """Migrate saved jobs away from models Flow has temporarily removed."""
    model = str(value or _DEFAULT_VIDEO_MODEL).strip()
    if model == "Omni Flash":
        model = "Omni 1.1 Flash"
    model = model.replace("Veo 3.1 Fast", _DEFAULT_VIDEO_MODEL).replace(
        "Veo 3.1 Quality", "Veo 3.1 - Quality",
    )
    return _DEFAULT_VIDEO_MODEL if model in _DISABLED_VIDEO_MODELS else model


def _flow_submit_button_score(text: str = "", aria_label: str = "") -> int:
    """Score the current Flow submit control across locales and UI builds."""
    text_value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    aria_value = re.sub(r"\s+", " ", str(aria_label or "")).strip().lower()
    if re.search(r"bắt đầu tạo|start creating|start generation|generate now", aria_value):
        return 100
    if text_value == "arrow_forward" or "arrow_forward" in text_value:
        return 90
    if re.search(r"\b(create|generate|submit|run)\b", aria_value):
        return 80
    if re.search(r"\b(create|generate|submit|run)\b", text_value):
        return 70
    return 0


def _media_prompt_matches(media: dict[str, Any], prompt: str) -> bool:
    """Match a Flow media record to the exact prompt that created it."""
    expected = re.sub(r'\s+', ' ', str(prompt or '').strip()).casefold()
    if not expected:
        return False
    request_data = ((media or {}).get("mediaMetadata") or {}).get("requestData") or {}
    for item in request_data.get("promptInputs") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("textInput") or "").strip()
        if not text:
            parts = ((item.get("structuredPrompt") or {}).get("parts") or [])
            text = " ".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()
        normalized = re.sub(r'\s+', ' ', text).strip().casefold()
        if normalized == expected or expected in normalized or normalized in expected:
            return True
    for key in ('prompt', 'promptText', 'textPrompt'):
        normalized = re.sub(r'\s+', ' ', str(media.get(key) or '')).strip().casefold()
        if normalized and (normalized == expected or expected in normalized or normalized in expected):
            return True
    return False


def _media_created_timestamp(media: dict[str, Any]) -> float:
    raw = str(((media or {}).get("mediaMetadata") or {}).get("createTime") or "").strip()
    if not raw:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _captured_video_ids(response: dict[str, Any]) -> list[str]:
    """Return stable media IDs from either observed Flow video response shape."""
    values: list[str] = []
    for job in response.get("jobs") or []:
        media = job.get("mediaId") or {}
        value = media.get("mediaName") or media.get("name")
        if value:
            values.append(str(value))
    for media in response.get("media") or []:
        value = media.get("name") or media.get("mediaName")
        if value:
            values.append(str(value))
    return list(dict.fromkeys(values))


def _captured_image_items(response: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize both current and legacy batchGenerateImages response shapes."""
    values: list[dict[str, str]] = []
    for media in response.get("media") or []:
        generated = (media.get("image") or {}).get("generatedImage") or {}
        media_id = media.get("name") or media.get("mediaName")
        src = generated.get("fifeUrl") or media.get("fifeUrl")
        if media_id and src:
            values.append({"id": str(media_id), "src": str(src)})
    for generated in response.get("generatedImages") or []:
        media_id = generated.get("mediaName") or generated.get("name")
        src = generated.get("fifeUrl") or generated.get("fife_url")
        if media_id and src:
            values.append({"id": str(media_id), "src": str(src)})
    return list({item["id"]: item for item in values}.values())


def _detect_plan(credit_info: Any) -> str | None:
    """Map Flow's Credits object to 'Pro' or 'Ultra'.

    Google Flow exposes ``userPaygateTier`` (e.g. PAYGATE_TIER_ONE/TWO)
    and ``sku`` (e.g. labs_pro_monthly, labs_ultra_monthly).  We try both
    fields so the detection stays robust across API versions.
    Returns None when we cannot determine the tier (caller keeps existing).
    """
    tier = str(getattr(credit_info, "tier", "") or "").lower()
    sku  = str(getattr(credit_info, "sku",  "") or "").lower()
    service_tier = str(getattr(credit_info, "service_tier", "") or "").lower()
    combined = re.sub(r"[^a-z0-9]+", "_", f"{tier} {sku} {service_tier}")
    if "ultra" in combined or "tier_two" in combined or "tier_2" in combined:
        return "Ultra"
    if "pro" in combined or "tier_one" in combined or "tier_1" in combined:
        return "Pro"
    if "tier_0" in combined or "tier_3" in combined or "free" in combined or "standard" in combined:
        return "Free"
    return None



def _job_concurrency(settings: dict[str, Any]) -> int:
    try:
        value = int(settings.get("concurrency", _DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT))
    except (TypeError, ValueError):
        value = _DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT
    return max(1, min(_MAX_CONCURRENT_JOBS_PER_ACCOUNT, value))


def _match_model_choice(requested: str, text: str) -> bool:
    req = requested.strip().lower()
    t = re.sub(r"\s+", " ", text).strip().lower()
    if "lower priority" in req:
        return "lower priority" in t or "lower" in t
    if "lite" in req:
        return "lite" in t and "lower" not in t
    if "fast" in req:
        return "fast" in t
    if "quality" in req:
        return "quality" in t
    if "omni" in req or "flash" in req:
        return "omni" in t or "flash" in t
    if req in t:
        return True
    if "pro" in req:
        return "pro" in t
    if "2" in req:
        return ("2" in t or "banana 2" in t) and "lite" not in t and "pro" not in t
    if "nano banana" in req:
        return "nano" in t or "banana" in t or "imagen" in t
    return False


def _clean_flow_model_name(value: str) -> str:
    """Strip UI-only emoji/material icons while preserving Flow's model name."""
    text = re.sub(r"\barrow_drop_down\b", "", str(value or ""), flags=re.I)
    return re.sub(r"^[^\w]+", "", re.sub(r"\s+", " ", text).strip()).strip()


def _capability_entry(name: str, controls: list[tuple[str, bool]]) -> dict[str, Any]:
    """Build one serializable model capability from visible Flow controls."""
    ratios: list[str] = []
    durations: list[str] = []
    resolutions: list[str] = []
    selected_ratio = selected_duration = selected_resolution = ""
    for raw_text, selected in controls:
        text = re.sub(r"\s+", " ", str(raw_text or "")).strip()
        ratio_match = re.search(r"(?<!\d)(\d{1,2}:\d{1,2})(?!\d)", text)
        duration_match = re.search(rf"(?<!\d)(\d{{1,3}})\s*{_DURATION_UNITS}(?!\w)", text, re.I)
        # ponytail: match both "720p"/"1080p" style AND "1K"/"2K"/"4K" style
        resolution_match = re.search(r"(?<!\d)(\d{3,4}p|[1-9]\d{0,1}[Kk])(?!\w)", text, re.I)
        if ratio_match and ratio_match.group(1) not in ratios:
            ratios.append(ratio_match.group(1))
            if selected:
                selected_ratio = ratio_match.group(1)
        if duration_match and duration_match.group(1) not in durations:
            durations.append(duration_match.group(1))
            if selected:
                selected_duration = duration_match.group(1)
        if resolution_match:
            resolution = resolution_match.group(1).lower()
            if resolution not in resolutions:
                resolutions.append(resolution)
            if selected:
                selected_resolution = resolution
    return {
        "name": _clean_flow_model_name(name),
        "ratios": ratios,
        "durations": durations,
        "resolutions": resolutions,
        "defaultRatio": selected_ratio or (ratios[0] if ratios else ""),
        "defaultDuration": selected_duration or (durations[0] if durations else ""),
        "defaultResolution": selected_resolution or (resolutions[0] if resolutions else ""),
    }


def _catalog_section(account: dict[str, Any], kind: str) -> dict[str, Any]:
    catalog = account.get("capabilityCatalog")
    if not isinstance(catalog, dict) or account.get("capabilityStatus") != "verified":
        return {}
    section = catalog.get(kind)
    return section if isinstance(section, dict) else {}


def _normalize_catalog_settings(
    account: dict[str, Any], kind: str, settings: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Select only model/ratio/duration values verified for this account."""
    section = _catalog_section(account, kind)
    models = [item for item in section.get("models", []) if isinstance(item, dict) and item.get("name")]
    if not models:
        return dict(settings), False
    requested = str(settings.get("model") or "")
    selected = next((item for item in models if item["name"] == requested), None)
    if selected is None:
        selected = next((item for item in models if _match_model_choice(requested, str(item["name"]))), None)
    if selected is None:
        default_model = str(section.get("defaultModel") or "")
        selected = next((item for item in models if item["name"] == default_model), models[0])
    normalized = dict(settings)
    normalized["model"] = str(selected["name"])
    ratios = [str(value) for value in selected.get("ratios", []) if str(value)]
    if ratios and str(normalized.get("ratio") or "") not in ratios:
        normalized["ratio"] = str(selected.get("defaultRatio") or ratios[0])
    durations = [str(value) for value in selected.get("durations", []) if str(value)]
    if kind == "video" and durations and str(normalized.get("duration") or "") not in durations:
        normalized["duration"] = str(selected.get("defaultDuration") or durations[0])
    resolutions = [str(value).lower() for value in selected.get("resolutions", []) if str(value)]
    requested_resolution = str(normalized.get("resolution") or "").lower()
    if resolutions and requested_resolution not in resolutions:
        normalized["resolution"] = str(selected.get("defaultResolution") or resolutions[0])
    return normalized, normalized != settings


def _mode_tab_icon(kind: str) -> str:
    """Material symbols used by Flow's language-independent mode tabs."""
    return "image" if kind == "image" else "videocam"


# Flow changed the generation settings controls from ``role=tab`` to
# ``role=radio``.  Keep both selectors so existing and current builds work.
_FLOW_CONTROL_SELECTOR = '[role="tab"], [role="radio"]'
_DURATION_UNITS = r"(?:s|sec(?:ond)?s?|giây)"


def _duration_pattern(duration: str) -> re.Pattern[str]:
    """Match Flow duration labels in English and Vietnamese."""
    return re.compile(
        rf"(?<!\d){re.escape(str(duration).strip())}\s*{_DURATION_UNITS}(?!\w)",
        re.IGNORECASE,
    )


def _flow_control_selected_from_attrs(
    *, aria_selected: str | None, aria_checked: str | None, data_state: str | None,
) -> bool:
    """Return whether a Flow tab/radio reports the selected state."""
    return (
        aria_selected == "true"
        or aria_checked == "true"
        or data_state == "checked"
    )


def _selected_flow_folder(selected: Path, kind: str) -> Path:
    """Map a picker path to one concrete Flow kind folder.

    Desktop pickers return the actual absolute directory. Keep that path
    intact, adding the image/video segment exactly once for the canonical
    ``.../flow`` layout instead of silently remapping it to the default home
    directory.
    """
    parts = selected.parts
    lowered = [part.casefold() for part in parts]
    kind_lower = kind.casefold()
    for index, part in enumerate(lowered):
        if part != "flow" or index + 1 >= len(parts):
            continue
        next_part = lowered[index + 1]
        flow_root = Path(*parts[: index + 1])
        tail = parts[index + 2 :]
        if next_part in {"image", "video"}:
            if next_part == kind_lower:
                return selected
            return flow_root / kind / Path(*tail) if tail else flow_root / kind
        break
    if selected.name.casefold() == "flow":
        return selected / kind
    if selected.parent.name.casefold() == "flow":
        return selected.parent / kind / selected.name
    return selected / kind


async def _flow_control_is_selected(control) -> bool:
    """Read selection state across Flow's old and new control markup."""
    try:
        return _flow_control_selected_from_attrs(
            aria_selected=await control.get_attribute("aria-selected"),
            aria_checked=await control.get_attribute("aria-checked"),
            data_state=await control.get_attribute("data-state"),
        )
    except Exception:
        return False


async def _await_with_job_progress(
    awaitable,
    job_id: str,
    *,
    timeout_s: int,
    start: int = 20,
    ceiling: int = 75,
):
    """Await a Flow response while keeping an accepted job visibly alive."""
    task = asyncio.create_task(awaitable)
    started = time.monotonic()
    try:
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=2.0)
            if done:
                break
            elapsed = time.monotonic() - started
            progress = min(ceiling, start + int(elapsed / max(1, timeout_s) * (ceiling - start)))
            current = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            store.patch_row("jobs", job_id, {
                "stage": "generating",
                "progress": max(current, progress),
                "updatedAt": time.time(),
            })
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _is_settings_trigger(text: str, aria_label: str = "") -> bool:
    """Identify the generation settings pill without clicking grid settings."""
    text = str(text or "")
    aria_label = str(aria_label or "")
    combined = f"{aria_label} {text}"
    if re.search(r"\bx[1-4]\b", text, re.IGNORECASE):
        return True
    if re.search(r"\b(?:Nano Banana|Imagen|Veo|Video|Image|H\u00ecnh \u1ea3nh|16:9|9:16|1:1)\b", combined, re.IGNORECASE):
        return True
    return bool(re.search(
        r"(?:điều kiện kích hoạt|generation settings|trigger settings)",
        combined,
        re.IGNORECASE,
    ))


def _session_needs_login(error: Exception) -> bool:
    """Identify failures that require the visible Google re-login flow."""
    return bool(re.search(
        r"LOGIN_REQUIRED|SESSION_EXPIRED|session.*expired|cookies.*expired|\b401\b|recaptcha|accounts\.google\.com|flow\.google\.com/about|/about|not signed in|unauthenticated|authentication required",
        str(error),
        re.I,
    ))


async def _click_settings_pill(pill, tabs) -> bool:
    """Open Flow settings, retrying with a forced click for Windows DPI."""
    for force in (False, True):
        try:
            await pill.click(force=force)
        except Exception:
            if force:
                return False
            continue
        await asyncio.sleep(.35)
        for index in range(await tabs.count()):
            if await tabs.nth(index).is_visible():
                return True
    return False


async def _open_flow_settings_panel(page, pill, tabs, ui=None) -> bool:
    """Prefer Playwright clicks, then reuse flow-py's DOM-click fallback."""
    if pill is not None and await _click_settings_pill(pill, tabs):
        return True
    if ui is None:
        return False
    try:
        return bool(await ui.open_settings_panel(page))
    except Exception:
        return False


class FlowService:
    def __init__(self) -> None:
        self._account_active: dict[str, int] = {}
        # Monotonic dispatch cursor per account.  Threads are created quickly
        # for bulk jobs, so relying on OS scheduling makes prompts start in a
        # random order even when concurrency is set to 16.
        self._account_next_order: dict[str, int] = {}
        self._account_next_start: dict[str, int] = {}
        self._connecting_accounts: set[str] = set()
        self._syncing_accounts: set[str] = set()  # guard concurrent credit syncs
        self._running_jobs: set[str] = set()
        self._claimed_media_ids: set[str] = set()
        self._claimed_error_tiles: set[str] = set()
        self._cancelled: set[str] = set()
        self._guard = threading.RLock()
        self._account_condition = threading.Condition(self._guard)

    def _claim_media_ids(self, candidates: list[str], expected_count: int) -> list[str]:
        """Atomically assign project media so concurrent jobs cannot share one output."""
        with self._guard:
            persisted = {
                str(media_id)
                for job in self.jobs()
                for media_id in (job.get("mediaIds") or [])
                if str(media_id)
            }
            used = persisted | self._claimed_media_ids
            selected = [media_id for media_id in candidates if media_id not in used][:expected_count]
            if len(selected) == expected_count:
                self._claimed_media_ids.update(selected)
                return selected
            return []

    def accounts(self) -> list[dict[str, Any]]:
        rows = store.list_rows("accounts")
        # A visible login is process-local. Do not leave a stale connecting
        # badge after an app restart; the saved profile remains untouched.
        for row in rows:
            account_id = str(row.get("id") or "")
            if row.get("status") == "connecting" and account_id not in self._connecting_accounts:
                patch = {"status": "reconnect", "error": None, "updatedAt": time.time()}
                store.patch_row("accounts", account_id, patch)
                row.update(patch)
        return rows

    def jobs(self) -> list[dict[str, Any]]:
        # Queue order is FIFO: the first prompt stays at the top and is the
        # first job resumed after an app restart.  History can still sort by
        # timestamp in the UI when a newest-first view is appropriate.
        rows: list[dict[str, Any]] = []
        for row in sorted(store.list_rows("jobs"), key=lambda item: item.get("createdAt", 0)):
            item = self._migrate_legacy_kind_output_folder(dict(row))
            # The queue must expose the same concrete folder used by the
            # worker; the saved setting can legitimately be just "test".
            settings = item.get("settings")
            if isinstance(settings, dict) and str(settings.get("outputDir") or "").strip():
                output_dir = str(settings.get("outputDir") or "")
                # The queue must always report the same on-disk folder used
                # by the worker, including in the browser build.
                item["displayOutputFolder"] = str(self._display_output_folder(item))
                item["outputFolder"] = str(self._output_folder(item, create=False))
            rows.append(item)
        return rows

    def _migrate_legacy_kind_output_folder(self, job: dict[str, Any]) -> dict[str, Any]:
        """Move legacy flat Flow outputs into ``flow/<kind>/<user-name>``."""
        settings = job.get("settings")
        kind = str(job.get("kind") or "")
        if not isinstance(settings, dict) or kind not in {"video", "image"}:
            return job
        original = str(settings.get("outputDir") or "").strip()
        normalized = original.replace("\\", "/").rstrip("/")
        suffixes = (f"/{kind}", f"-{kind}")
        base = next((normalized[: -len(suffix)] for suffix in suffixes if normalized.endswith(suffix)), normalized)
        if not base:
            return job
        migrated = dict(job)
        migrated_settings = dict(settings)
        migrated_settings["outputDir"] = base
        migrated["settings"] = migrated_settings
        target_folder = self._output_folder(migrated)
        migrated_outputs = []
        source_folders: set[Path] = set()
        for raw_output in job.get("outputs") or []:
            output = Path(str(raw_output))
            target = target_folder / output.name
            if output != target and output.is_file():
                source_folders.add(output.parent)
                if not target.exists():
                    shutil.move(str(output), str(target))
            migrated_outputs.append(str(target if target.exists() else output))
        for source_folder in source_folders:
            try:
                source_folder.rmdir()
            except OSError:
                pass
        migrated["outputs"] = migrated_outputs
        migrated["outputFolder"] = str(target_folder)
        if (
            original != base
            or migrated_outputs != list(job.get("outputs") or [])
            or (bool(job.get("outputFolder")) and str(job.get("outputFolder")) != str(target_folder))
        ):
            store.patch_row("jobs", str(job.get("id") or ""), {
                "settings": migrated_settings,
                "outputs": migrated_outputs,
                "outputFolder": str(target_folder),
            })
        return migrated

    def logs(self) -> list[dict[str, Any]]:
        return sorted(store.list_rows("logs"), key=lambda row: row.get("createdAt", 0), reverse=True)[:1000]

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
        store.put_row("logs", {
            "id": uuid.uuid4().hex[:16],
            "level": level,
            "event": event,
            "jobId": job_id,
            "accountId": account_id,
            "message": message,
            "details": details or {},
            "createdAt": time.time(),
        })

    def start(self) -> None:
        """Resume work that was queued or interrupted by an app restart."""
        for job in self.jobs():
            if job.get("status") in {"queued", "processing"}:
                store.patch_row("jobs", job["id"], {"status": "queued", "stage": "queued", "progress": 0, "updatedAt": time.time()})
                threading.Thread(target=self._run_sync, args=(job["id"],), daemon=True, name=f"flow-resume-{job['id']}").start()
            elif job.get("status") == "done" and not self._outputs_exist(job.get("outputs")):
                store.patch_row("jobs", job["id"], {
                    "status": "failed",
                    "stage": "failed",
                    "progress": 0,
                    "error": "FLOW_EMPTY_OUTPUT: generation finished without a downloaded media file",
                    "updatedAt": time.time(),
                })

    @staticmethod
    def _outputs_exist(outputs: Any) -> bool:
        """Return true only when every declared output is a readable non-empty file.

        A Flow job must never become ``done`` merely because a media id or a
        download URL was returned.  The final file check is deliberately kept
        at the service boundary so it covers both image and video workers.
        """
        paths = [Path(str(value)) for value in (outputs or []) if str(value).strip()]
        if not paths:
            return False
        try:
            return all(path.is_file() and path.stat().st_size > 0 for path in paths)
        except OSError:
            return False

    @classmethod
    def _output_validation_error(cls, outputs: Any) -> str | None:
        paths = [Path(str(value)) for value in (outputs or []) if str(value).strip()]
        if not paths:
            return "FLOW_EMPTY_OUTPUT: no output file was downloaded"
        for path in paths:
            try:
                if not path.is_file():
                    return f"FLOW_OUTPUT_MISSING: {path}"
                if path.stat().st_size <= 0:
                    return f"FLOW_OUTPUT_EMPTY: {path}"
            except OSError as exc:
                return f"FLOW_OUTPUT_UNREADABLE: {path}: {exc}"
        return None

    def save_account(self, payload: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        existing = store.get_row("accounts", account_id or "") or {}
        row = {
            **existing,
            "id": account_id or uuid.uuid4().hex[:12],
            "label": str(payload.get("label") or existing.get("label") or "Flow account").strip(),
            "email": str(payload.get("email") or existing.get("email") or "").strip(),
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
            "isDefault": bool(payload.get("isDefault", existing.get("isDefault", not self.accounts()))),
            "createdAt": existing.get("createdAt", now),
            "updatedAt": now,
        }
        if row["isDefault"]:
            for account in self.accounts():
                if account["id"] != row["id"]:
                    store.patch_row("accounts", account["id"], {"isDefault": False})
        return store.put_row("accounts", row)

    def delete_account(self, account_id: str) -> bool:
        if any(job.get("accountId") == account_id and job.get("status") not in _TERMINAL for job in self.jobs()):
            raise RuntimeError("Account still has active Flow jobs")
        removed = store.delete_row("accounts", account_id)
        if removed:
            shutil.rmtree(store.root() / "profiles" / account_id, ignore_errors=True)
        return removed

    def delete_job(self, job_id: str) -> bool:
        job = store.get_row("jobs", job_id)
        if not job:
            return False
        if job.get("status") not in _TERMINAL:
            with self._account_condition:
                self._cancelled.add(job_id)
                self._account_condition.notify_all()
            store.patch_row("jobs", job_id, {"status": "cancelled", "stage": "cancelled", "progress": 0, "updatedAt": time.time()})
        # Delete artifacts first. Keep the row if Windows locks a file so the
        # user can retry; never report success while output files remain.
        for raw_output in job.get('outputs') or []:
            output = Path(str(raw_output))
            if output.is_file():
                output.unlink()
        removed = store.delete_row("jobs", job_id)
        if removed:
            # Output folders can now be shared by multiple prompts. Only remove
            # artifacts belonging to this job, then remove the folder if empty.
            for raw_output in job.get("outputs") or []:
                try:
                    output = Path(str(raw_output))
                    if output.is_file():
                        output.unlink()
                except OSError:
                    pass
            try:
                self._output_folder(job, create=False).rmdir()
            except OSError:
                pass
        return removed

    def cancel_all(self) -> int:
        ids = {str(job['id']) for job in store.list_rows('jobs') if job.get('status') not in _TERMINAL}
        with self._account_condition:
            self._cancelled.update(ids)
            self._account_condition.notify_all()
        count = store.cancel_active_jobs(ids, time.time())
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
        ids = {str(job['id']) for job in store.list_rows('jobs')}
        with self._account_condition:
            self._cancelled.update(ids)
            self._account_condition.notify_all()
        selected = [job for job in store.list_rows('jobs') if str(job['id']) in ids]
        output_paths = [Path(str(raw)) for job in selected for raw in job.get('outputs') or []]
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(output_paths)))) as pool:
            list(pool.map(lambda path: path.unlink(missing_ok=True), output_paths))
        removed = store.delete_rows('jobs', ids)
        folders = [self._output_folder(job, create=False) for job in removed]
        for job in removed:
            for raw in job.get('outputs') or []:
                try:
                    Path(str(raw)).unlink(missing_ok=True)
                except OSError:
                    pass
        for folder in folders:
            try:
                folder.rmdir()
            except OSError:
                pass
        return len(removed)

    def delete_output_folder_jobs(self, output_dir: str, kind: str = "") -> int:
        """Delete only the jobs and real artifacts belonging to one Flow folder."""
        selected = str(output_dir or "").strip()
        if not selected:
            return 0
        matched = [
            job for job in self.jobs()
            if str((job.get("settings") or {}).get("outputDir") or "").strip() == selected
            and (not kind or str(job.get("kind") or "") == kind)
        ]
        if not matched:
            return 0
        folder = self._output_folder(matched[0], create=False)
        ids = {str(job["id"]) for job in matched}
        with self._account_condition:
            self._cancelled.update(ids)
            self._account_condition.notify_all()
        for job in matched:
            for raw in job.get("outputs") or []:
                try:
                    Path(str(raw)).unlink(missing_ok=True)
                except OSError:
                    pass
        removed = store.delete_rows("jobs", ids)
        count = len(removed)
        for job in removed:
            for raw in job.get("outputs") or []:
                try:
                    Path(str(raw)).unlink(missing_ok=True)
                except OSError:
                    pass
        # Remove untracked remnants too (for example an interrupted download),
        # but only after restricting the operation to the matched output path.
        # Never remove another job's outputs when folders are shared.
        shared = any(self._output_folder(job, create=False).resolve() == folder.resolve()
                     for job in store.list_rows('jobs'))
        if not shared and folder.is_dir():
            shutil.rmtree(folder)
        return count

    def connect(self, account_id: str) -> dict[str, Any]:
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        with self._guard:
            if account_id in self._connecting_accounts:
                return account
            self._connecting_accounts.add(account_id)
            store.patch_row("accounts", account_id, {"status": "connecting", "error": None, "updatedAt": time.time()})
        self._log("info", "account_connecting", account_id=account_id)
        threading.Thread(target=lambda: asyncio.run(self._login(account_id)), daemon=True, name=f"flow-login-{account_id}").start()
        return store.get_row("accounts", account_id) or account

    async def sync_credits_for_account(self, account_id: str) -> dict[str, Any]:
        """Fetch fresh credits from Google Flow using the existing browser profile.

        Uses a headless Chrome session with the already-saved login profile — no
        interactive sign-in needed.  Returns the updated account row.
        """
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        project_id = str(account.get("projectId") or "")
        if account.get("status") != "online" or not project_id:
            raise RuntimeError("FLOW_LOGIN_REQUIRED: account must be connected before syncing credits")
        # Guard: skip if Chrome is already open for this account (connect in progress)
        with self._guard:
            if account_id in self._connecting_accounts:
                _log.info("sync_credits_for_account: skip %s — connect in progress", account_id)
                return account
            if account_id in self._syncing_accounts:
                _log.info("sync_credits_for_account: skip %s — already syncing", account_id)
                return account
            self._syncing_accounts.add(account_id)
        from ._flow._api import FlowAPI
        from ._flow._exceptions import AuthError as FlowAuthError
        from .browser import BrowserManager
        browser = BrowserManager(headless=True, profile_dir=store.profile_dir(account_id))
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
                    browser = BrowserManager(headless=True, profile_dir=store.profile_dir(account_id))
                    await browser.start()  # raises if still fails
                else:
                    raise
            api = FlowAPI(browser, project_id=project_id)
            _log.info("sync_credits_for_account: fetching credits project=%s account=%s", project_id, account_id)
            credit_info = await asyncio.wait_for(api.get_credits(), timeout=30.0)
            detected_plan = _detect_plan(credit_info)
            capability_catalog = None
            capability_error = ""
            try:
                capability_catalog = await asyncio.wait_for(
                    self._read_capability_catalog(browser, project_id), timeout=45.0,
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
            if isinstance(exc, FlowAuthError) or _session_needs_login(exc):
                # Token expired or cookies invalid — mark as needing reconnect.
                _log.warning("sync_credits_for_account: auth error for %s: %s", account_id, exc)
                store.patch_row("accounts", account_id, {
                    "status": "reconnect",
                    "error": f"FLOW_SESSION_EXPIRED: {exc}",
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
        return store.get_row("accounts", account_id) or account

    async def _read_capability_catalog(self, browser, project_id: str) -> dict[str, Any]:
        """Read the model/ratio/duration controls actually exposed to this account."""
        from .browser import FLOW_BASE_URL

        page = await browser.page()
        if project_id not in str(page.url or ""):
            await page.goto(
                f"{FLOW_BASE_URL}/project/{project_id}",
                wait_until="domcontentloaded", timeout=30_000,
            )
        await page.wait_for_selector(".settings-trigger-button", state="visible", timeout=15_000)
        controls = page.locator(_FLOW_CONTROL_SELECTOR)

        async def model_selector():
            selectors = page.locator("button:has(.model-select-trigger-content)")
            for index in range(await selectors.count()):
                candidate = selectors.nth(index)
                if await candidate.is_visible():
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
            for index in range(await controls.count()):
                control = controls.nth(index)
                if not await control.is_visible() or await control.is_disabled():
                    continue
                snapshot.append(((await control.inner_text()).strip(), await _flow_control_is_selected(control)))
            return snapshot

        catalog: dict[str, Any] = {"version": 1, "source": "flow_ui", "syncedAt": time.time()}
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
        await switch_kind(original_kind)
        if not catalog["image"]["models"] or not catalog["video"]["models"]:
            raise RuntimeError("FLOW_CAPABILITY_SYNC_FAILED: Flow returned an empty model catalog")
        return catalog

    def _verify_account_plan_before_enqueue(self, account_id: str) -> dict[str, Any]:
        """Refresh Flow entitlement before every generation entry point.

        ``enqueue`` is synchronous and is also called by series/automation
        workers, so run the existing async sync routine in a short-lived loop.
        A stale stored plan must never authorize a new job.
        """
        account = store.get_row("accounts", account_id)
        if not account:
            raise ValueError("FLOW_PLAN_SYNC_REQUIRED: Flow account was not found")
        if account.get("status") != "online" or not account.get("projectId"):
            # A queued job may start before the asynchronous account refresh
            # finishes, or after Flow has dropped only the project. Reuse the
            # saved browser session first; ask for manual work only if that
            # session can no longer restore an online project.
            restored = asyncio.run(
                self._try_headless_reconnect(account_id, str(account.get("projectId") or ""))
            )
            account = store.get_row("accounts", account_id) or account
            if not restored or account.get("status") != "online" or not account.get("projectId"):
                raise ValueError(
                    "FLOW_LOGIN_REQUIRED: Không thể tự đồng bộ tài khoản Flow; hãy kết nối lại trong Cài đặt"
                )
        try:
            refreshed = asyncio.run(self.sync_credits_for_account(account_id))
        except Exception as exc:
            message = str(exc)
            if "FLOW_SESSION_EXPIRED" in message or _session_needs_login(exc):
                raise ValueError(f"FLOW_SESSION_EXPIRED: {message}") from exc
            raise ValueError(f"FLOW_PLAN_SYNC_FAILED: {message}") from exc
        if refreshed.get("planStatus") != "verified" or refreshed.get("plan") not in {"Free", "Pro", "Ultra"}:
            raise ValueError("FLOW_PLAN_UNKNOWN: Không xác định được gói Flow; hãy đồng bộ lại")
        return refreshed


    def sync_all_credits(self) -> list[dict[str, Any]]:
        """Fire-and-forget credit sync for every online account.

        Spawns one background thread per online account and returns immediately
        with the current (pre-sync) account list.  The frontend's normal polling
        will pick up the updated balances on the next GET /accounts or /jobs.
        """
        online = [a for a in self.accounts() if a.get("status") == "online" and a.get("projectId")]
        for account in online:
            account_id = account["id"]
            threading.Thread(
                target=lambda aid=account_id: asyncio.run(self.sync_credits_for_account(aid)),
                daemon=True,
                name=f"flow-sync-{account_id}",
            ).start()
        return self.accounts()



    async def _login(self, account_id: str) -> None:
        """Open a visible Chrome window to authenticate and sync the Flow account.

        If the account already has a saved projectId (was previously connected),
        first tries a fast headless probe.  Only opens visible Chrome when the
        cookie is no longer valid.
        """
        account = store.get_row("accounts", account_id) or {}
        existing_project_id = str(account.get("projectId") or "")

        # Fast-path: try headless first when there's a saved session
        if existing_project_id:
            try:
                ok = await self._try_headless_reconnect(account_id, existing_project_id)
                if ok:
                    with self._guard:
                        self._connecting_accounts.discard(account_id)
                    return
            except Exception as probe_exc:
                _log.debug("_login headless probe failed for %s, falling back to visible: %s", account_id, probe_exc)
            # The saved project may have been deleted. Start at Flow home so
            # the authenticated session can select/create a replacement.
            existing_project_id = ""
            store.patch_row("accounts", account_id, {
                "status": "reconnect", "projectId": "", "error": "FLOW_PROJECT_NOT_FOUND",
                "updatedAt": time.time(),
            })

        browser = None
        try:
            from .browser import BrowserManager, FLOW_BASE_URL
            browser = BrowserManager(headless=False, profile_dir=store.profile_dir(account_id))
            await browser.start()
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

            if existing_project_id:
                # Tài khoản cũ: login_hint + continue về project cụ thể
                project_url = f"https://flow.google.com/project/{existing_project_id}"
                start_url = (
                    f"https://accounts.google.com/ServiceLogin"
                    f"?service=wise&continue={quote(project_url, safe='')}"
                    f"&login_hint={quote(existing_email, safe='')}"
                )
            else:
                # Tài khoản mới: login_hint + continue về Flow home
                start_url = (
                    f"https://accounts.google.com/ServiceLogin"
                    f"?service=wise&continue={quote('https://flow.google.com/', safe='')}"
                    f"&login_hint={quote(existing_email, safe='')}"
                )
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)

            async def _is_flow_authenticated() -> tuple[bool, str]:
                try:
                    url = str(page.url or "")
                    if "accounts.google.com" in url or "/about" in url:
                        return False, ""
                    has_auth = await page.evaluate(
                        "() => Boolean(window.WIZ_global_data?.SNlM0e || window.WIZ_global_data?.oPEP7c)"
                    )
                    if not has_auth:
                        return False, ""
                    m = _PROJECT_RE.search(url)
                    return True, m.group(1) if m else ""
                except Exception:
                    return False, ""

            # Điền email vào ô input và click Next tự động
            try:
                if "accounts.google.com" in str(page.url or ""):
                    email_input = page.locator('input[type="email"], input[name="identifier"]').first
                    await email_input.wait_for(state="visible", timeout=5_000)
                    current_val = await email_input.input_value()
                    if not current_val.strip():
                        await email_input.fill(existing_email)
                    next_btn = page.locator("#identifierNext, button:has-text('Next'), button:has-text('Tiếp theo')").first
                    await next_btn.wait_for(state="visible", timeout=3_000)
                    await next_btn.click()
            except Exception:
                pass

            # Fast-detect: only if already authenticated and project is open
            await asyncio.sleep(2.0)
            is_auth, project_id = await _is_flow_authenticated()

            # Nếu chưa đăng nhập và bị redirect vào accounts.google.com → chờ user login rồi tự navigate về Flow
            if not is_auth:
                if "/about" in str(page.url or ""):
                    # Trang giới thiệu Flow → click Sign In để vào Google login
                    try:
                        sign_in_link = page.locator('a:has-text("Sign in"), a[href*="ServiceLogin"]').first
                        if await sign_in_link.count() > 0:
                            await sign_in_link.click()
                            await asyncio.sleep(2.0)
                    except Exception:
                        pass
                elif "accounts.google.com" in str(page.url or ""):
                    # Đã ở trang đăng nhập Google — không cần navigate thêm
                    pass
                else:
                    # Chưa login, thử navigate đến Flow login
                    try:
                        await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass

                # Interactive loop: wait for user to finish signing in in visible Chrome
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    try:
                        if _browser_closed or page.is_closed():
                            break  # Chrome đóng bởi user
                        _ = page.url
                    except Exception:
                        break  # Chrome closed by user

                    is_auth, pid = await _is_flow_authenticated()
                    if is_auth:
                        if pid:
                            project_id = pid
                            break
                        # Authenticated on Flow, but might be on lobby/hub page
                        links = await page.locator('a[href*="/project/"]').all()
                        if links:
                            href = await links[0].get_attribute("href") or ""
                            m = _PROJECT_RE.search(href)
                            if m:
                                project_id = m.group(1)
                                try:
                                    await page.goto(
                                        f"https://flow.google.com/project/{project_id}",
                                        wait_until="domcontentloaded",
                                        timeout=15_000,
                                    )
                                except Exception:
                                    pass
                                break
                        else:
                            create = page.get_by_role("button", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I)).first
                            if await create.count():
                                try:
                                    await create.click(force=True)
                                    await page.wait_for_url(lambda url: bool(_PROJECT_RE.search(url)), timeout=15_000)
                                    m = _PROJECT_RE.search(page.url)
                                    if m:
                                        project_id = m.group(1)
                                        break
                                except Exception:
                                    pass
                    await asyncio.sleep(1.5)

            if not project_id:
                # Phân biệt user tắt Chrome vs timeout thực sự
                try:
                    _ = page.url
                    is_closed = False
                except Exception:
                    is_closed = True
                if is_closed:
                    raise RuntimeError("Chrome bị đóng trước khi đăng nhập xong")
                raise RuntimeError("Login timed out or Chrome was closed before sign-in completed")

            # Verify session and fetch credits
            from ._flow._api import FlowAPI
            _log.info("_login: fetching credits for project=%s", project_id)
            credit_info = await asyncio.wait_for(
                FlowAPI(browser, project_id=project_id).get_credits(),
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

            patch: dict[str, Any] = {
                "status": "online",
                "projectId": project_id,
                "email": email or (store.get_row("accounts", account_id) or {}).get("email", ""),
                "credits": credits,
                "creditsSyncedAt": credits_synced_at,
                "planStatus": "verified" if detected_plan else "unknown",
                "planSource": "flow_credits_api" if detected_plan else "",
                "planSyncedAt": credits_synced_at,
                "flowTier": str(getattr(credit_info, "tier", "") or "") if credit_info else "",
                "flowSku": str(getattr(credit_info, "sku", "") or "") if credit_info else "",
                "flowServiceTier": str(getattr(credit_info, "service_tier", "") or "") if credit_info else "",
                "updatedAt": time.time(),
                "error": None,
            }
            if detected_plan:
                patch["plan"] = detected_plan
            store.patch_row("accounts", account_id, patch)
            self._log("success", "account_connected", account_id=account_id, details={"projectId": project_id, "credits": credits, "plan": detected_plan})
        except Exception as exc:
            store.patch_row("accounts", account_id, {"status": "reconnect", "error": str(exc), "updatedAt": time.time()})
            self._log("error", "account_connect_failed", account_id=account_id, message=str(exc))
        finally:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.stop(), timeout=8.0)
                except Exception:
                    pass
            try:
                from pipeline.core.desktop_window import request_desktop_foreground

                request_desktop_foreground()
            except Exception:
                pass
            with self._guard:
                self._connecting_accounts.discard(account_id)


    async def _try_headless_reconnect(self, account_id: str, project_id: str) -> bool:
        """Attempt a quick headless session to verify the saved cookie is still valid.

        Returns True and patches the account to 'online' on success.
        Returns False silently on any failure so the caller falls back to
        the visible interactive flow.

        ponytail: short 30-s timeout; failure is expected and safe.
        """
        from .browser import BrowserManager, FLOW_BASE_URL
        browser = None
        try:
            browser = BrowserManager(headless=True, profile_dir=store.profile_dir(account_id))
            await browser.start()
            # Another worker may already have replaced a deleted project while
            # this worker was waiting for the shared browser profile.
            persisted_project_id = str((store.get_row("accounts", account_id) or {}).get("projectId") or "")
            if persisted_project_id and persisted_project_id != project_id:
                project_id = persisted_project_id
            page = await browser.page()
            target_url = (
                f"https://flow.google.com/project/{project_id}"
                if project_id
                else FLOW_BASE_URL
            )
            await page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
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
            if not confirmed_id:
                # Try following a project link from the lobby.
                if "accounts.google.com" not in page.url and ("flow.google.com" in page.url or "labs.google" in page.url):
                    links = await page.locator('a[href*="/project/"]').all()
                    if links:
                        href = await links[0].get_attribute("href") or ""
                        m = _PROJECT_RE.search(href)
                        if m and m.group(1) != project_id:
                            confirmed_id = m.group(1)

            if not confirmed_id and project_id:
                # The saved project was deleted. Always create a replacement:
                # selecting another existing project would mix unrelated jobs.
                await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                await asyncio.sleep(2)
                new_project = page.get_by_role(
                    "button", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I),
                ).first
                if not await new_project.count():
                    new_project = page.get_by_role(
                        "link", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I),
                    ).first
                if await new_project.count():
                    await new_project.click(force=True)
                    try:
                        await page.wait_for_url(
                            lambda url: bool(_PROJECT_RE.search(url)) and project_id not in url,
                            timeout=15_000,
                        )
                    except Exception:
                        # Some Flow builds open a naming dialog before creating.
                        create = page.get_by_role(
                            "button", name=re.compile(r"^create$|^tạo$|^tạo dự án$", re.I),
                        ).first
                        if await create.count():
                            await create.click(force=True)
                            await page.wait_for_url(
                                lambda url: bool(_PROJECT_RE.search(url)) and project_id not in url,
                                timeout=15_000,
                            )
                    match = _PROJECT_RE.search(page.url)
                    if match and match.group(1) != project_id:
                        confirmed_id = match.group(1)

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
            authenticated = await page.evaluate("() => Boolean(window.WIZ_global_data?.SNlM0e || window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email)")
            if not authenticated:
                return False
            email = await page.evaluate("() => window.WIZ_global_data?.oPEP7c || window.__NEXT_DATA__?.props?.pageProps?.session?.user?.email || ''")
            credits = None
            credits_synced_at = None
            detected_plan = None
            try:
                from ._flow._api import FlowAPI
                credit_info = await FlowAPI(browser, project_id=confirmed_id).get_credits()
                credits = int(credit_info.credits)
                credits_synced_at = time.time()
                detected_plan = _detect_plan(credit_info)
                if getattr(credit_info, "email", ""):
                    email = credit_info.email or email
            except Exception:
                pass
            account = store.get_row("accounts", account_id) or {}
            patch: dict[str, Any] = {
                "status": "online",
                "projectId": confirmed_id,
                "email": email or account.get("email", ""),
                "credits": credits,
                "creditsSyncedAt": credits_synced_at,
                "updatedAt": time.time(),
                "error": None,
            }
            if project_id and confirmed_id != project_id:
                patch["previousProjectId"] = project_id
                patch["projectChangedAt"] = time.time()
            if detected_plan:
                patch["plan"] = detected_plan
            store.patch_row("accounts", account_id, patch)
            self._log("success", "account_connected", account_id=account_id, details={"projectId": confirmed_id, "credits": credits, "plan": detected_plan, "headless": True})
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



    def enqueue(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        prompts = [str(value).strip() for value in payload.get("prompts", []) if str(value).strip()]
        account_id = str(payload.get("accountId") or "")
        if not prompts or not store.get_row("accounts", account_id):
            raise ValueError("Prompts and a valid Flow account are required")
        settings = dict(payload.get("settings") or {})
        settings["concurrency"] = _job_concurrency(settings)
        kind = str(payload.get("kind") or "video")
        mode = str(payload.get("mode") or "text")
        input_type = str(payload.get("inputType") or "prompt").lower()
        if input_type not in {"prompt", "txt", "csv", "json"}:
            input_type = "prompt"
        source_files = list(payload.get("sourceFiles") or [])
        series_context = dict(payload.get("seriesContext") or {})
        # Queue creation must stay fast. Entitlement sync opens Chrome and can
        # take minutes; doing it here made the UI look stuck and prevented the
        # user from seeing/cancelling the complete batch. The worker verifies
        # the account immediately before submitting each job.
        account = store.get_row("accounts", account_id) or {}
        if account.get("plan") == "Free":
            if kind == "video":
                raise ValueError("Tài khoản gói thường chỉ hỗ trợ tạo ảnh (Free accounts only support image generation)")
            if settings.get("model") == "Nano Banana Pro":
                settings["model"] = "Nano Banana 2"

        settings, _ = _normalize_catalog_settings(account, kind, settings)

        if kind == "image":
            if not _catalog_section(account, kind) and str(settings.get("model") or "Nano Banana 2") not in _IMAGE_UI_MODELS:
                raise ValueError(f"Unsupported Flow image model: {settings.get('model')}")
            if mode != "text" and not source_files:
                raise ValueError("Image edit/reference mode requires at least one source image")
        else:
            if not _catalog_section(account, kind):
                settings["model"] = _normalize_video_model(settings.get("model"))
            if not _catalog_section(account, kind) and settings["model"] not in _VIDEO_UI_MODELS:
                raise ValueError(f"Unsupported Flow video model: {settings.get('model')}")
        created = []
        for index, prompt in enumerate(prompts, 1):
            now = time.time()
            job_input_index = int(payload.get("inputIndex") or series_context.get("sceneIndex") or index)
            with self._account_condition:
                order = self._account_next_order.get(account_id)
                if order is None:
                    persisted_orders = [
                        int(row.get("queueOrder")) for row in self.jobs()
                        if row.get("accountId") == account_id and str(row.get("queueOrder", "")).isdigit()
                    ]
                    order = max(persisted_orders, default=-1) + 1
                self._account_next_order[account_id] = order + 1
            job = {
                "id": uuid.uuid4().hex[:12], "inputIndex": job_input_index, "kind": kind,
                "queueOrder": order,
                "mode": mode, "prompt": prompt, "accountId": account_id,
                "inputType": input_type,
                "settings": settings, "sourceFiles": source_files,
                "seriesContext": series_context,
                "status": "queued", "stage": "queued", "progress": 0, "mediaIds": [], "outputs": [],
                "generationRejectRetryCount": 0,
                "error": None, "createdAt": now, "updatedAt": now,
            }
            job["outputFolder"] = str(self._output_folder(job, create=False))
            job["displayOutputFolder"] = str(self._display_output_folder(job))
            store.put_row("jobs", job)
            if series_context:
                from . import series
                series.register_job(job)
            self._log("info", "job_queued", job_id=job["id"], account_id=account_id, details={"kind": job["kind"], "inputIndex": job_input_index})
            created.append(job)
            threading.Thread(target=self._run_sync, args=(job["id"],), daemon=True, name=f"flow-job-{job['id']}").start()
        return created

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
        concurrency = _job_concurrency(job.get("settings") or {})
        order = int(job.get("queueOrder", job.get("inputIndex", 0)))
        with self._account_condition:
            def next_queued_order() -> int | None:
                pending_orders = [
                    int(row.get("queueOrder")) for row in self.jobs()
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
                order != self._account_next_start[account_id]
                or self._account_active.get(account_id, 0) >= concurrency
            ):
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
                "updatedAt": time.time(),
            })
            self._account_next_start[account_id] += 1
            self._account_active[account_id] = self._account_active.get(account_id, 0) + 1
        # Retry only failures that happen before a generation is submitted.
        # Once Flow accepts a job, retrying can create a duplicate and charge
        # credits twice; media recovery owns all post-submit timeouts.
        _HARD_ERROR = re.compile(
            r"LOGIN_REQUIRED|GENERATION_FAILED|GENERATION_REJECTED|FLOW_EMPTY_OUTPUT|FLOW_GENERATION_TIMEOUT|FLOW_PROJECT_NOT_FOUND|FLOW_RESULT_NOT_FOUND",
            re.I,
        )
        profile_ready = False
        try:
            if job_id in self._cancelled:
                return
            verified_account = self._verify_account_plan_before_enqueue(account_id)
            if verified_account.get("plan") == "Free" and job.get("kind") == "video":
                raise ValueError("Tài khoản gói thường chỉ hỗ trợ tạo ảnh (Free accounts only support image generation)")
            for auth_attempt in range(2):
                runtime_profile: Path | None = None
                try:
                    runtime_profile = self._clone_runtime_profile(account_id, job_id)
                    profile_ready = True
                    asyncio.run(self._run(job_id, profile_dir=runtime_profile))
                finally:
                    if runtime_profile is not None:
                        shutil.rmtree(runtime_profile, ignore_errors=True)
                finished = store.get_row("jobs", job_id) or {}
                auth_error = finished.get("status") == "action_required" and _session_needs_login(Exception(str(finished.get("error") or "")))
                if not auth_error or auth_attempt:
                    break
                project_id = str(verified_account.get("projectId") or account_id)
                _log.warning("Flow session expired for %s; attempting automatic reconnect", account_id)
                if not asyncio.run(self._try_headless_reconnect(account_id, project_id)):
                    break
                store.patch_row("jobs", job_id, {
                    "status": "queued", "stage": "queued", "progress": 0,
                    "error": None, "updatedAt": time.time(),
                })
                _log.info("Automatic Flow reconnect succeeded; resuming job %s", job_id)

            # Check whether _run marked the job as a transient failure → auto-retry once
            finished = store.get_row("jobs", job_id) or {}
            project_error = finished.get("status") == "failed" and "FLOW_PROJECT_NOT_FOUND" in str(finished.get("error") or "")
            if project_error:
                _log.warning("Flow project missing for %s; creating a replacement project from the saved session before retrying %s", account_id, job_id)
                old_project_id = str(verified_account.get("projectId") or "")
                # The Google session is still valid; only the project was
                # deleted. Headless recovery creates/selects a replacement
                # without opening the login flow or asking the user to connect again.
                asyncio.run(self._try_headless_reconnect(account_id, old_project_id))
                refreshed = store.get_row("accounts", account_id) or {}
                if refreshed.get("status") == "online" and refreshed.get("projectId"):
                    store.patch_row("jobs", job_id, {"status": "queued", "stage": "queued", "progress": 0, "error": None, "updatedAt": time.time()})
                    runtime_profile2: Path | None = None
                    try:
                        runtime_profile2 = self._clone_runtime_profile(account_id, job_id)
                        profile_ready = True
                        asyncio.run(self._run(job_id, profile_dir=runtime_profile2))
                    finally:
                        if runtime_profile2 is not None:
                            shutil.rmtree(runtime_profile2, ignore_errors=True)
                    finished = store.get_row("jobs", job_id) or {}
            finished_error = str(finished.get("error") or "")
            rejected = "FLOW_GENERATION_REJECTED" in finished_error
            rejection_retry_count = int(finished.get("generationRejectRetryCount") or 0)
            should_auto_retry = finished.get("status") == "failed" and (
                (rejected and rejection_retry_count < 1)
                or (not rejected and not _HARD_ERROR.search(finished_error))
            )
            if should_auto_retry:
                _log.warning(
                    "auto-retry job %s after transient failure: %s",
                    job_id, finished.get("error"),
                )
                if rejected:
                    store.patch_row("jobs", job_id, {
                        "status": "processing",
                        "stage": "retrying",
                        "progress": 0,
                        "error": None,
                        "generationRejectRetryCount": rejection_retry_count + 1,
                        "updatedAt": time.time(),
                    })
                time.sleep(3)
                store.patch_row("jobs", job_id, {"status": "queued", "stage": "queued", "progress": 0, "error": None, "outputs": []})
                runtime_profile2: Path | None = None
                profile_ready = False
                try:
                    runtime_profile2 = self._clone_runtime_profile(account_id, job_id)
                    profile_ready = True
                    asyncio.run(self._run(job_id, profile_dir=runtime_profile2))
                finally:
                    if runtime_profile2 is not None:
                        shutil.rmtree(runtime_profile2, ignore_errors=True)
        except Exception as exc:
            # Profile copy and worker bootstrap happen outside Flow's async
            # error boundary. Convert an unexpected failure here into a
            # terminal job state so the API/queue never leaves a zombie job or
            # an unhandled thread traceback (notably Windows profile races).
            current = store.get_row("jobs", job_id) or {}
            if current.get("status") not in _TERMINAL:
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
            with self._account_condition:
                self._account_active[account_id] = max(0, self._account_active.get(account_id, 1) - 1)
                self._running_jobs.discard(job_id)
                self._account_condition.notify_all()

    def _clone_runtime_profile(self, account_id: str, job_id: str) -> Path:
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
                ignore=lambda _directory, names: sorted(_PROFILE_COPY_IGNORES.intersection(names)),
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
        return target

    async def _prepare_video_mode(self, page, model: str) -> None:
        """Open Flow's live settings popover and verify Video mode.

        flow-py's JavaScript click can race the localized Agent UI and leave
        Image mode selected. Use trusted Playwright clicks against the controls
        observed in the current account instead.
        """
        model_pills = page.locator('button[aria-haspopup="menu"]')
        visible_pill = None
        for index in range(await model_pills.count() - 1, -1, -1):
            candidate = model_pills.nth(index)
            label = (await candidate.inner_text()).strip()
            # x[1-4]\b (no leading \b): matches both "x1" and "crop_16_9x1"
            if await candidate.is_visible() and re.search(r"x[1-4]\b", label):
                visible_pill = candidate
                break
        if visible_pill is None:
            raise RuntimeError("FLOW_UI_CHANGED: generation settings control was not found")
        await visible_pill.click()
        await asyncio.sleep(1.0)
        # Tab "Video" — dùng regex để catch cả localized variants; fallback force-click nếu is_visible() fail (Windows DPI)
        video_tab_loc = page.locator('[role="tab"]').filter(has=page.locator('text=/^Video/i'))
        if await video_tab_loc.count() == 0:
            # Thử regex rộng hơn: bất kỳ tab nào KHÔNG phải Image/Hình/S/M/L/Landscape/Portrait/Square
            all_tabs = page.locator('[role="tab"]')
            await all_tabs.first.wait_for(state="attached", timeout=10_000)
            video_tab_loc = all_tabs.filter(has=page.locator('text=/^Video/i'))
        if await video_tab_loc.count() == 0:
            tab_texts = []
            all_tabs2 = page.locator('[role="tab"]')
            for i in range(min(10, await all_tabs2.count())):
                try:
                    tab_texts.append((await all_tabs2.nth(i).inner_text()).strip())
                except Exception:
                    pass
            raise RuntimeError(f"FLOW_UI_CHANGED: Video mode tab was not found — tabs={tab_texts}")
        # Force click — is_visible() unreliable trên Windows DPI
        await video_tab_loc.first.click(force=True)
        await asyncio.sleep(0.6)

        setting_pills = page.locator('button[aria-haspopup="menu"]')
        video_summary = setting_pills.filter(has_text=re.compile(r"Video", re.I)).last
        if await video_summary.count() == 0 or not await video_summary.is_visible():
            raise RuntimeError("FLOW_MODE_MISMATCH: Flow did not switch from Image to Video")

        model_buttons = setting_pills.filter(has_text=re.compile(r"Omni|Veo", re.I))
        if await model_buttons.count() == 0:
            raise RuntimeError("FLOW_UI_CHANGED: video model selector was not found")
        model_button = model_buttons.last
        current_model = (await model_button.inner_text()).strip()
        if not _match_model_choice(model, current_model):
            await model_button.click()
            await asyncio.sleep(0.25)
            choices = page.locator('[role="menuitem"], [role="option"]')
            selected = False
            for index in range(await choices.count()):
                choice = choices.nth(index)
                text = (await choice.inner_text()).strip()
                if await choice.is_visible() and _match_model_choice(model, text):
                    await choice.click()
                    selected = True
                    break
            if not selected:
                raise RuntimeError(f"FLOW_MODEL_UNAVAILABLE: {model}")

    async def _prepare_ui_model(self, page, kind: str, model: str, *, ui=None) -> None:  # noqa: C901
        """Select a current Flow UI model for models without a stable REST key.

        Strategy:
        1. Ensure settings panel is open via pill click (x[1-4] or any visible pill).
        2. Switch to the correct mode tab (Image/Video).
        3. Click the model family selector and pick the right option.
        """
        current_url = str(page.url or "")
        if "accounts.google.com" in current_url or "/about" in current_url or "flow.google.com/about" in current_url:
            raise RuntimeError(
                f"FLOW_LOGIN_REQUIRED: Google session expired or redirected to {current_url}; please reconnect the account in Settings"
            )

        try:
            await page.wait_for_selector('button', timeout=30_000, state="attached")
        except Exception:
            _log.warning("_prepare_ui_model: settings pill not found within 30s — proceeding anyway")

        family = re.compile(r"Nano Banana|Imagen", re.I) if kind == "image" else re.compile(r"Omni|Veo", re.I)
        mode_pattern = (
            re.compile(r"Image|H\u00ecnh \u1ea3nh", re.I) if kind == "image"
            else re.compile(r"Video", re.I)
        )
        controls = page.locator(_FLOW_CONTROL_SELECTOR)

        async def _visible_mode_tab():
            loc = controls.filter(has_text=mode_pattern)
            # 1. Prefer truly visible tab
            for index in range(await loc.count()):
                candidate = loc.nth(index)
                if await candidate.is_visible():
                    return candidate
            # 2. Fallback: Windows DPI can make is_visible() unreliable — use first attached tab
            if await loc.count() > 0:
                _log.warning("_visible_mode_tab: %s tab not visible but attached — DPI fallback", kind)
                return loc.first
            icon_tabs = controls.filter(
                has=page.locator("i", has_text=re.compile(rf"^{_mode_tab_icon(kind)}$", re.I)),
            )
            for index in range(await icon_tabs.count()):
                candidate = icon_tabs.nth(index)
                if await candidate.is_visible():
                    return candidate
            if await icon_tabs.count() > 0:
                _log.warning("_visible_mode_tab: %s icon tab not visible but attached — DPI fallback", kind)
                return icon_tabs.first
            return None

        async def _model_pill_already_visible() -> bool:
            loc = page.locator('button[aria-haspopup="menu"]').filter(has_text=family)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    return True
            return False

        async def _model_pill_matches(target_model: str) -> bool:
            """True when the visible model pill already shows the desired model."""
            loc = page.locator('.settings-trigger-button, .settings-summary, button[aria-haspopup="menu"]').filter(has_text=family)
            for index in range(await loc.count()):
                candidate = loc.nth(index)
                if await candidate.is_visible():
                    text = (await candidate.inner_text()).strip()
                    if _match_model_choice(target_model, text):
                        return True
            return False

        # Fast path: if the pill already shows the correct model, settings are already
        # applied — skip opening the settings panel (avoids the flaky pill-click flow
        # that errors when the same settings are re-selected).
        if await _model_pill_matches(model):
            _log.info("_prepare_ui_model: %s model pill already correct — skipping settings panel", kind)
            return

        # ------------------------------------------------------------------
        # Step 2: switch to the correct mode tab (Image / Video)
        # ------------------------------------------------------------------
        # Focus prompt input first so clicking the settings pill opens the
        # settings panel instead of the reference-image media picker.
        try:
            ed = page.locator('textarea, [contenteditable="true"][role="textbox"], div[contenteditable]').first
            if await ed.count() > 0:
                await ed.click()
                await asyncio.sleep(0.3)
        except Exception:
            pass

        switched = False
        if ui is not None:
            try:
                from ._flow._models import GenerationMode
                gen_mode = GenerationMode.IMAGE if kind == "image" else GenerationMode.VIDEO
                switched = await ui.switch_mode(page, gen_mode)
            except Exception:
                switched = False

        if not switched and not await _model_pill_already_visible():
            # Fallback: find the tab by text and click it.
            mode_tab = await _visible_mode_tab()
            if mode_tab is None:
                for attempt in range(3):
                    try:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass

                    # Critical: focus the prompt textarea before clicking the settings
                    # pill.  In the current Flow UI the pill only opens the mode/model/
                    # aspect panel when the prompt input already has focus; otherwise it
                    # opens the reference-image media picker.
                    try:
                        ed = page.locator('textarea, [contenteditable="true"][role="textbox"], div[contenteditable]').first
                        if await ed.count() > 0:
                            await ed.click()
                            await asyncio.sleep(0.4)
                    except Exception:
                        pass

                    pills = page.locator("button, [role='button']")
                    trigger = None
                    trigger_deadline = time.monotonic() + 8
                    while trigger is None and time.monotonic() < trigger_deadline:
                        for index in range(await pills.count() - 1, -1, -1):
                            candidate = pills.nth(index)
                            try:
                                role = await candidate.get_attribute("role")
                                if role in {"tab", "radio"} or not await candidate.is_visible():
                                    continue
                                text = (await candidate.inner_text()).strip()
                                aria_label = await candidate.get_attribute("aria-label") or ""
                                if _is_settings_trigger(text, aria_label):
                                    trigger = candidate
                                    break
                            except Exception:
                                continue
                        if trigger is None:
                            await asyncio.sleep(0.25)
                    if trigger is None:
                        _log.warning("_prepare_ui_model attempt %d: no settings pill found", attempt + 1)
                    opened = await _open_flow_settings_panel(page, trigger, controls, ui)
                    if not opened:
                        _log.warning(
                            "_prepare_ui_model attempt %d: settings pill did not open panel",
                            attempt + 1,
                        )
                    await asyncio.sleep(1.2)
                    mode_tab = await _visible_mode_tab()
                    if mode_tab is not None:
                        break

                    tab_texts: list[str] = []
                    for idx in range(min(10, await controls.count())):
                        try:
                            tab_texts.append((await controls.nth(idx).inner_text()).strip())
                        except Exception:
                            pass
                    _log.warning(
                        "_prepare_ui_model attempt %d: no %s tab visible; tabs=%s",
                        attempt + 1, kind, tab_texts,
                    )

            if mode_tab is None:
                current_url = str(page.url or "")
                if "accounts.google.com" in current_url or "/about" in current_url or "flow.google.com/about" in current_url:
                    raise RuntimeError(
                        f"FLOW_LOGIN_REQUIRED: Google session expired or redirected to {current_url}; please reconnect the account in Settings"
                    )
                if await _model_pill_already_visible():
                    _log.info(
                        "_prepare_ui_model: %s tab not found but model pill present — skipping",
                        kind,
                    )
                else:
                    raise RuntimeError(f"FLOW_UI_CHANGED: {kind} mode tab was not found")
            elif not await _flow_control_is_selected(mode_tab):
                await mode_tab.click(force=True)
                await asyncio.sleep(0.6)



        # ------------------------------------------------------------------
        # Step 3: find and click model family selector, pick the right option
        # ------------------------------------------------------------------
        selectors = page.locator('button[aria-haspopup="menu"]').filter(has_text=family)
        selector = None
        for index in range(await selectors.count() - 1, -1, -1):
            candidate = selectors.nth(index)
            if await candidate.is_visible():
                selector = candidate
                break
        if selector is None:
            raise RuntimeError(f"FLOW_UI_CHANGED: {kind} model selector was not found")
        if not _match_model_choice(model, await selector.inner_text()):
            await selector.click()
            await asyncio.sleep(0.5)
            choices = page.locator('[role="menuitem"], [role="option"]')
            selected = False
            for index in range(await choices.count()):
                choice = choices.nth(index)
                text = (await choice.inner_text()).strip()
                if await choice.is_visible() and _match_model_choice(model, text):
                    await choice.click()
                    selected = True
                    break
            if not selected:
                visible_texts = []
                for index in range(await choices.count()):
                    choice = choices.nth(index)
                    if await choice.is_visible():
                        visible_texts.append((await choice.inner_text()).strip())
                raise RuntimeError(f"FLOW_MODEL_UNAVAILABLE: {model} — available: {visible_texts}")


    async def _prepare_ui_format(
        self,
        page,
        ratio: str,
        duration: str | None = None,
        resolution: str | None = None,
    ) -> None:
        """Select current numeric Flow tabs and verify the requested format.

        Recent Flow builds label aspect tabs ``16:9``/``9:16``/``1:1`` rather
        than the older Landscape/Portrait/Square labels used by flow-py.
        """
        async def visible_tab(label: str | re.Pattern[str]):
            pattern = label if hasattr(label, "search") else re.compile(re.escape(label))
            matches = page.locator(_FLOW_CONTROL_SELECTOR).filter(
                has_text=pattern
            )
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                if await candidate.is_visible():
                    return candidate
            return None

        ratio_label = str(ratio or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{1,2}", ratio_label):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid aspect ratio {ratio_label!r}")
        # Fast path: if the trigger button pill already shows this ratio, skip clicking
        pill_loc = page.locator(".settings-trigger-button, .settings-summary")
        for i in range(await pill_loc.count()):
            candidate = pill_loc.nth(i)
            if await candidate.is_visible():
                text = await candidate.inner_text()
                safe_ratio = ratio_label.replace(":", "_")
                if ratio_label in text or f"crop_{safe_ratio}" in text:
                    if duration is None and not resolution:
                        _log.info("_prepare_ui_format: ratio %s already selected on pill", ratio_label)
                        return
        ratio_tab = None
        for attempt in range(4):
            ratio_tab = await visible_tab(ratio_label)
            if ratio_tab is not None:
                break
            # Model selection can leave a transient menu open or close the
            # settings popover. Focus textarea (so pill opens settings not media picker),
            # then reopen via pill click.
            if attempt:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
            # Focus textarea trước khi click pill
            try:
                ed = page.locator("div[contenteditable]").first
                if await ed.count() > 0:
                    await ed.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass
            trigger = None
            # 1. Pill x[1-4] (matches "x1" và "crop_16_9x1")
            buttons = page.locator("button")
            for index in range(await buttons.count() - 1, -1, -1):
                candidate = buttons.nth(index)
                text = (await candidate.inner_text()).strip()
                if (
                    await candidate.is_visible()
                    and re.search(r"x[1-4]\b", text)
                    and await candidate.get_attribute("role") != "tab"
                ):
                    trigger = candidate
                    break
            # 2. Fallback: bất kỳ pill aria-haspopup visible
            if trigger is None:
                pills = page.locator('button[aria-haspopup="menu"]')
                for index in range(await pills.count() - 1, -1, -1):
                    candidate = pills.nth(index)
                    if await candidate.is_visible():
                        trigger = candidate
                        break
            if trigger is not None:
                await trigger.click()
            await asyncio.sleep(0.75)
        if ratio_tab is None:
            raise RuntimeError(f"FLOW_UI_CHANGED: aspect ratio {ratio_label} was not found")
        if not await _flow_control_is_selected(ratio_tab):
            await ratio_tab.click(force=True)
            await asyncio.sleep(0.3)
        if not await _flow_control_is_selected(ratio_tab):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: aspect ratio {ratio_label} was not selected")


        if duration is not None:
            duration_value = str(duration).strip()
            if not re.fullmatch(r"\d{1,3}", duration_value):
                raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid duration {duration_value!r}")
            duration_label = f"{duration_value}s"
            duration_tab = await visible_tab(_duration_pattern(duration_value))
            if duration_tab is not None:
                if not await _flow_control_is_selected(duration_tab):
                    await duration_tab.click(force=True)
                    await asyncio.sleep(0.3)
                if not await _flow_control_is_selected(duration_tab):
                    raise RuntimeError(f"FLOW_SETTING_MISMATCH: duration {duration_label} was not selected")
            else:
                # A hidden control only supports Flow's default duration. Never
                # silently run a different duration than the one requested.
                if duration_value != "8":
                    raise RuntimeError(f"FLOW_SETTING_MISMATCH: duration {duration_label} was not found")
                _log.info("_prepare_ui_format: duration %s control is hidden; using Flow model default", duration_label)

        if resolution:
            resolution_value = str(resolution).strip().lower()
            if not re.fullmatch(r"\d{3,4}p", resolution_value):
                raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid resolution {resolution!r}")
            resolution_tab = await visible_tab(re.compile(rf"(?<!\d){re.escape(resolution_value)}(?!\w)", re.I))
            if resolution_tab is None:
                _log.info("_prepare_ui_format: resolution %s control is hidden; using Flow model default", resolution_value)
                return
            if not await _flow_control_is_selected(resolution_tab):
                await resolution_tab.click(force=True)
                await asyncio.sleep(0.3)
            if not await _flow_control_is_selected(resolution_tab):
                raise RuntimeError(f"FLOW_SETTING_MISMATCH: resolution {resolution_value} was not selected")

    async def _click_flow_submit(self, page) -> None:
        """Click the submit control used by the current Flow project page.

        Flow moved from an English ``Create`` button to a localized Material
        icon button (``Bắt đầu tạo``/``arrow_forward``).  The upstream
        flow-py client only checks the old label and then waits for a network
        endpoint that no longer exists.  Failing at the click boundary keeps
        a UI mismatch visible instead of leaving a job at 5% for five minutes.
        """
        buttons = page.locator("button, [role='button']")
        candidates: list[tuple[int, int, Any]] = []
        for index in range(await buttons.count()):
            candidate = buttons.nth(index)
            try:
                if not await candidate.is_visible() or await candidate.is_disabled():
                    continue
                score = _flow_submit_button_score(
                    await candidate.inner_text(),
                    await candidate.get_attribute("aria-label") or "",
                )
                if score:
                    candidates.append((score, index, candidate))
            except Exception:
                continue
        if not candidates:
            raise RuntimeError(
                "FLOW_UI_CHANGED: submit button was not found or is disabled; "
                "refresh the Flow project and retry"
            )
        _, _, button = max(candidates, key=lambda item: (item[0], item[1]))
        try:
            try:
                await button.hover(timeout=2_000)
                await asyncio.sleep(0.4)
            except Exception:
                pass
            await button.click(force=True, timeout=8_000)
        except Exception as exc:
            raise RuntimeError(f"FLOW_SUBMIT_CLICK_FAILED: {exc}") from exc

    async def _set_flow_count(self, page, count: int) -> None:
        """Select the generation count in either the old tab or new radio UI."""
        desired = max(1, min(4, int(count or 1)))
        # Fast path: check if visible settings pill already shows desired count
        pill_loc = page.locator(".settings-trigger-button, .settings-summary")
        for i in range(await pill_loc.count()):
            candidate = pill_loc.nth(i)
            if await candidate.is_visible():
                text = await candidate.inner_text()
                if f"x{desired}" in text:
                    _log.info("_set_flow_count: count x%d already selected on pill", desired)
                    return
        target = page.locator(_FLOW_CONTROL_SELECTOR).filter(
            has_text=re.compile(rf"^\s*x{desired}\s*$", re.IGNORECASE),
        )
        for index in range(await target.count()):
            candidate = target.nth(index)
            if not await candidate.is_visible():
                continue
            if not await _flow_control_is_selected(candidate):
                await candidate.click(force=True)
                await asyncio.sleep(0.25)
            if await _flow_control_is_selected(candidate):
                return
        if desired == 1:
            _log.info("_set_flow_count: x1 is default, proceeding")
            return
        raise RuntimeError(f"FLOW_UI_CHANGED: generation count x{desired} was not found")

    async def _project_media_elements(self, page) -> list[dict[str, Any]]:
        """Read generated media tiles from Flow's current Angular DOM."""
        try:
            items = await page.evaluate(
                """() => [...document.querySelectorAll(
                    'img[data-media-id], video[data-media-id], [data-media-id] img, [data-media-id] video'
                )].map(element => ({
                    id: element.getAttribute('data-media-id') || element.closest('[data-media-id]')?.getAttribute('data-media-id') || '',
                    tag: element.tagName.toLowerCase(),
                    src: element.currentSrc || element.src || '',
                    width: element.naturalWidth || 0,
                    height: element.naturalHeight || 0,
                    readyState: element.readyState || 0,
                })).filter(item => item.id && item.src)"""
            )
            return [item for item in (items or []) if isinstance(item, dict)]
        except Exception:
            return []

    async def _find_existing_project_media(
        self,
        api,
        page,
        job: dict[str, Any],
        kind: str,
        count: int,
    ) -> list[dict[str, Any]]:
        """Recover a request that succeeded while the old interceptor timed out."""
        try:
            data = await api.get_project_data()
        except Exception:
            data = {}
        submission_time = float(job.get("submissionStartedAt") or 0)
        # Recovery mode: submissionStartedAt is known → use time filter, not prompt match.
        # Pre-check mode: no submissionStartedAt → require prompt match to avoid false positives.
        recovery_mode = bool(submission_time or job.get("mediaIds") or job.get("resumeOnly"))
        minimum_time = (submission_time - 30) if submission_time else (float(job.get("createdAt") or 0) - 60)
        records: list[dict[str, Any]] = []
        used_ids = set()
        if not job.get("mediaIds") and (job.get("resumeOnly") or job.get("submissionStartedAt")):
            used_ids = {str(mid) for row in self.jobs() if row.get("id") != job.get("id") for mid in row.get("mediaIds") or []}
            used_ids.update(self._claimed_media_ids)
        for media in data.get("projectContents", {}).get("media", []):
            if not isinstance(media, dict) or not media.get("name"):
                continue
            if str(media['name']) in used_ids:
                continue
            if kind == "image" and not media.get("image"):
                continue
            if kind == "video" and not media.get("video"):
                continue
            if kind == "video":
                status = str(
                    (((media.get("mediaMetadata") or {}).get("mediaStatus") or {}).get("mediaGenerationStatus"))
                    or ""
                )
                if status and status not in {
                    "MEDIA_GENERATION_STATUS_COMPLETE",
                    "MEDIA_GENERATION_STATUS_SUCCESS",
                    "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                }:
                    continue
            media_ids = set(job.get("mediaIds") or [])
            if not media_ids and str(media['name']) in set(job.get("baselineMediaIds") or []):
                continue
            if media_ids and str(media['name']) not in media_ids:
                continue
            if not media_ids and not recovery_mode and not _media_prompt_matches(media, str(job.get("prompt") or "")):
                continue
            created_at = _media_created_timestamp(media)
            if not media_ids and created_at and created_at < minimum_time:
                continue
            records.append(media)
        if not records:
            # The current Angular Flow UI can show completed tiles while its
            # project-data endpoint still returns an empty media list. DOM
            # recovery is safe only for an already-submitted request: baseline
            # and global claims keep concurrent jobs from taking old/duplicate
            # outputs.
            if not (job.get("mediaIds") or job.get("resumeOnly") or job.get("submissionStartedAt")):
                return []
            expected_tag = "img" if kind == "image" else "video"
            baseline_ids = {str(media_id) for media_id in job.get("baselineMediaIds") or []}
            account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
            project_changed_at = float(account.get("projectChangedAt") or 0)
            # Jobs queued around a forced project replacement share a new,
            # initially empty gallery. Concurrent submissions can make one
            # worker snapshot another worker's fresh output as its baseline;
            # global media claims are authoritative during this short cohort.
            if (
                project_changed_at
                and float(job.get("createdAt") or 0)
                <= project_changed_at + _PROJECT_MIGRATION_RECOVERY_WINDOW_S
            ):
                baseline_ids.clear()
            required_ids = {str(media_id) for media_id in job.get("mediaIds") or []}
            found: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in await self._project_media_elements(page):
                media_id = str(item.get("id") or "")
                src = str(item.get("src") or "")
                if (
                    not media_id
                    or media_id in seen
                    or media_id in used_ids
                    or media_id in baseline_ids
                    or (required_ids and media_id not in required_ids)
                    or item.get("tag") != expected_tag
                    or not src.startswith("http")
                ):
                    continue
                if kind == "image" and int(item.get("width") or 0) <= 0:
                    continue
                seen.add(media_id)
                found.append(item)
                if len(found) >= max(1, int(count or 1)):
                    break
            return found
        by_id = {
            str(item.get("id")): item
            for item in await self._project_media_elements(page)
            if item.get("id") and str(item.get("src") or "").startswith("http")
        }
        # Project data is authoritative for identity; the DOM supplies the
        # authenticated, downloadable media URL.
        found: list[dict[str, Any]] = []
        for media in sorted(records, key=lambda item: _media_created_timestamp(item)):
            item = by_id.get(str(media.get("name")))
            if kind in {'image', 'video'}:
                image = media.get(kind) or {}
                url = str(image.get('fifeUrl') if isinstance(image, dict) else image or '')
                if url.startswith('https://'):
                    item = {'id': str(media['name']), 'tag': 'img' if kind == 'image' else 'video', 'src': url}
            if item:
                found.append(item)
            if len(found) >= max(1, int(count or 1)):
                break
        return found

    async def _recover_submitted_media(self, api, page, job, timeout_s=900):
        """Poll the original project and persist ownership before downloading."""
        count = len(job.get("mediaIds") or []) or max(1, min(4, int((job.get("settings") or {}).get("count", 1))))
        deadline = time.monotonic() + timeout_s
        empty_checks = 0
        while time.monotonic() < deadline:
            self._check_cancel(job["id"])
            items = await self._find_existing_project_media(api, page, job, job["kind"], count)
            if len(items) >= count:
                ids = list(job.get("mediaIds") or [])
                if not ids:
                    ids = self._claim_media_ids([str(item['id']) for item in items], count)
                if ids:
                    store.patch_row("jobs", job["id"], {"mediaIds": ids, "stage": "downloading", "progress": 90, "updatedAt": time.time()})
                    return ids
            flow_error = await self._claim_visible_flow_error(page, job, count)
            if flow_error:
                store.patch_row("jobs", job["id"], {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "progress": 0,
                    "updatedAt": time.time(),
                })
                raise RuntimeError(f"FLOW_GENERATION_REJECTED: {flow_error}")
            empty_checks += 1
            if empty_checks >= 6 and page is not None:
                try:
                    has_pending = bool(await page.evaluate("""() =>
                        [...document.querySelectorAll('flow-grid-tile-container')].some(tile =>
                            !tile.querySelector('[data-media-id]') && Boolean(
                                tile.querySelector('[role="progressbar"], mat-progress-spinner, mat-spinner, .loading, .generating')
                                || /(?:generating|đang tạo|processing|%)/i.test(tile.innerText || '')
                            )
                        )
                    """))
                except Exception:
                    has_pending = True
                if not has_pending:
                    store.patch_row("jobs", job["id"], {
                        "submissionStartedAt": None,
                        "submissionProjectId": None,
                        "baselineMediaIds": [],
                        "mediaIds": [],
                        "resumeOnly": False,
                        "progress": 0,
                        "updatedAt": time.time(),
                    })
                    raise RuntimeError(
                        "FLOW_RESULT_NOT_FOUND: Flow has no pending or completed result for this submission"
                    )
            store.patch_row("jobs", job["id"], {"stage": "recovering", "progress": 84, "updatedAt": time.time()})
            await asyncio.sleep(5)
        raise RuntimeError("FLOW_GENERATION_TIMEOUT: original result is not ready; retry continues recovery")

    async def _claim_visible_flow_error(
        self,
        page,
        job: dict[str, Any],
        tile_limit: int,
    ) -> str:
        """Claim one visible failed Flow tile so concurrent jobs do not share it."""
        if page is None:
            return ""
        try:
            errors = await page.evaluate("""(limit) =>
                [...document.querySelectorAll('flow-grid-tile-container')]
                    .slice(0, Math.max(1, limit || 1))
                    .map((tile, index) => {
                        const error = tile.querySelector('.error-message');
                        if (!error || error.getClientRects().length === 0) return null;
                        return {
                            index,
                            text: (error.innerText || '').trim().replace(/\\s+/g, ' '),
                        };
                    })
                    .filter(Boolean)
            """, max(1, int(tile_limit or 1)))
        except Exception:
            return ""
        account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
        project_id = str(account.get("projectId") or "")
        current_job = store.get_row("jobs", str(job.get("id") or "")) or job
        current_submission_at = float(current_job.get("submissionStartedAt") or 0)
        last_rejected_submission_at = float(current_job.get("lastGenerationRejectedSubmissionAt") or 0)
        rejection_retry_count = int(current_job.get("generationRejectRetryCount") or 0)
        with self._guard:
            for item in errors or []:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                signature = f"{project_id}:{int(item.get('index') or 0)}:{text}"
                if signature in self._claimed_error_tiles:
                    # The same gallery position/text can fail again on the one
                    # permitted retry. Accept it only for that job's newer
                    # submission; concurrent first attempts still cannot share
                    # one failed tile.
                    if not (
                        rejection_retry_count >= 1
                        and current_submission_at > last_rejected_submission_at
                    ):
                        continue
                self._claimed_error_tiles.add(signature)
                store.patch_row("jobs", str(job.get("id") or ""), {
                    "lastFlowErrorSignature": signature,
                    "lastGenerationRejectedSubmissionAt": current_submission_at,
                    "updatedAt": time.time(),
                })
                return text
        return ""

    async def _wait_for_project_media(
        self,
        page,
        baseline_ids: set[str],
        kind: str,
        expected_count: int,
        job_id: str,
        timeout_s: int = 900,
        api=None,
        job: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Wait for media tiles after a current Flow Angular submit."""
        deadline = time.monotonic() + timeout_s
        next_project_check = 0.0
        store.patch_row("jobs", job_id, {
            "stage": "generating",
            "progress": max(20, int((store.get_row("jobs", job_id) or {}).get("progress") or 0)),
            "updatedAt": time.time(),
        })
        while time.monotonic() < deadline:
            self._check_cancel(job_id)
            items = await self._project_media_elements(page)
            if api is not None and job is not None and time.monotonic() >= next_project_check:
                next_project_check = time.monotonic() + 4
                fresh_job = store.get_row("jobs", job_id) or job
                recovered = await self._find_existing_project_media(api, page, fresh_job, kind, expected_count)
                recovered = [item for item in recovered if str(item['id']) not in baseline_ids]
                if len(recovered) >= max(1, expected_count):
                    return recovered[:max(1, expected_count)]
            fresh: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in items:
                if str(item.get('id')) in seen:
                    continue
                seen.add(str(item.get('id')))
                if item.get('tag') != ('img' if kind == 'image' else 'video'):
                    continue
                if str(item.get("id")) in baseline_ids:
                    continue
                src = str(item.get("src") or "")
                if not src.startswith("http"):
                    continue
                if kind == "image" and not src.startswith("https://"):
                    continue
                fresh.append(item)
            if len(fresh) >= max(1, expected_count):
                return fresh[:max(1, expected_count)]
            flow_error = await self._claim_visible_flow_error(
                page,
                job or (store.get_row("jobs", job_id) or {"id": job_id}),
                expected_count,
            )
            if flow_error:
                store.patch_row("jobs", job_id, {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "progress": 0,
                    "updatedAt": time.time(),
                })
                raise RuntimeError(f"FLOW_GENERATION_REJECTED: {flow_error}")
            elapsed = timeout_s - max(0.0, deadline - time.monotonic())
            current_progress = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            store.patch_row("jobs", job_id, {
                "stage": "generating",
                # Keep progress moving while Flow renders. Reserve 90–100 for
                # download and final file validation so long renders do not
                # look frozen at 70–75% or trigger a duplicate retry.
                "progress": max(current_progress, min(88, 20 + int(elapsed / max(1, timeout_s) * 68))),
                "updatedAt": time.time(),
            })
            await asyncio.sleep(1)
        raise RuntimeError(
            f"FLOW_GENERATION_TIMEOUT: no completed {kind} media appeared after submit"
        )

    async def _wait_and_download_flow_videos(
        self,
        page,
        job: dict[str, Any],
        count: int,
        baseline_text: str,
        job_id: str,
        timeout_s: int = 900,
    ) -> list[str]:
        """Wait for newly generated Flow video tiles to complete and download MP4s directly."""
        deadline = time.monotonic() + timeout_s
        expected_count = max(1, min(4, int(count or 1)))
        started = False
        while time.monotonic() < deadline:
            self._check_cancel(job_id)
            flow_error = await self._claim_visible_flow_error(page, job, expected_count)
            if flow_error:
                store.patch_row("jobs", job_id, {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "progress": 0,
                    "updatedAt": time.time(),
                })
                raise RuntimeError(f"FLOW_GENERATION_REJECTED: {flow_error}")
            info = await page.evaluate("""(expCount) => {
                const tiles = Array.from(document.querySelectorAll('flow-grid-tile-container')).slice(0, expCount);
                if (!tiles.length) return null;
                return tiles.map(t => {
                    const text = (t.innerText || '').trim().replace(/\\s+/g, ' ');
                    const hasThumb = !!t.querySelector('.thumbnail');
                    const pctMatch = text.match(/(\\d+)%/);
                    const pct = pctMatch ? parseInt(pctMatch[1], 10) : -1;
                    const hasError = /lỗi|thất bại|failed|error|rejected|hoạt động bất thường|không thành công/i.test(text);
                    return { text, hasThumb, pct, hasError };
                });
            }""", expected_count)
            if not info:
                await asyncio.sleep(2)
                continue
            top_text = info[0].get("text", "")
            if not started:
                prompt_sub = str(job.get("prompt") or "")[:15].lower()
                if top_text != baseline_text or prompt_sub in top_text.lower():
                    started = True
            if started:
                pcts = [item["pct"] for item in info if item.get("pct", -1) != -1]
                if pcts:
                    avg_pct = sum(pcts) // len(pcts)
                    store.patch_row("jobs", job_id, {
                        "stage": "generating",
                        "progress": max(20, min(90, avg_pct)),
                        "updatedAt": time.time(),
                    })
                all_done = all(item.get("hasThumb") and item.get("pct", -1) == -1 for item in info)
                if all_done and len(info) >= expected_count:
                    break
            await asyncio.sleep(1.5)
        else:
            raise RuntimeError(f"FLOW_GENERATION_TIMEOUT: videos did not complete within {timeout_s}s")

        store.patch_row("jobs", job_id, {"stage": "downloading", "progress": 90, "updatedAt": time.time()})
        outputs: list[str] = []
        for output_index in range(1, expected_count + 1):
            self._check_cancel(job_id)
            output = self._output_path(job, output_index, "mp4")
            output.parent.mkdir(parents=True, exist_ok=True)
            tile = page.locator("flow-grid-tile-container").nth(output_index - 1)
            thumb = tile.locator(".thumbnail").first
            await thumb.click()
            await asyncio.sleep(1.5)
            dl_btn = page.locator('button[aria-label*="Tải"], button:has-text("download"), button[aria-label*="download"]').first
            await dl_btn.click()
            await asyncio.sleep(1)
            item_res = page.locator('[role="menuitem"]:has-text("720p"), [role="menuitem"]:has-text("1080p")').first
            async with page.expect_download(timeout=30_000) as dl_info:
                await item_res.click()
            dl = await dl_info.value
            await dl.save_as(str(output))
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.8)
            outputs.append(str(output))
            self._log("success", "output_downloaded", job_id=job_id, account_id=str(job.get("accountId") or ""), details={"outputIndex": output_index, "path": str(output)})
        return outputs

    async def _wait_for_project_videos(
        self,
        api,
        baseline_ids: set[str],
        expected_count: int,
        job_id: str,
        timeout_s: int = 900,
    ) -> list[str]:
        """Resolve new Omni/Veo media IDs from project data.

        Omni Flash can return an empty ``jobs`` array from the legacy video
        endpoint interceptor. Project data remains authoritative and includes
        the generated video's stable media ID and status.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._check_cancel(job_id)
            try:
                data = await api.get_project_data()
            except Exception as e:
                _log.debug("_wait_for_project_videos: get_project_data error: %s", e)
                data = {}
            completed: list[tuple[str, str]] = []
            for media in data.get("projectContents", {}).get("media", []):
                media_id = str(media.get("name") or "")
                if not media_id or media_id in baseline_ids or "video" not in media:
                    continue
                metadata = media.get("mediaMetadata") or {}
                status = str((metadata.get("mediaStatus") or {}).get("mediaGenerationStatus") or "")
                if status in {"MEDIA_GENERATION_STATUS_FAILED", "MEDIA_GENERATION_STATUS_REJECTED"}:
                    raise RuntimeError(f"FLOW_GENERATION_FAILED: {media_id} ({status})")
                if status in {
                    "MEDIA_GENERATION_STATUS_COMPLETE",
                    "MEDIA_GENERATION_STATUS_SUCCESS",
                    "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                }:
                    completed.append((str(metadata.get("createTime") or ""), media_id))
            if not completed and hasattr(api, "_bm"):
                try:
                    page = await api._bm.page()
                    items = await self._project_media_elements(page)
                    for item in items:
                        mid = str(item.get("id") or "")
                        if mid and mid not in baseline_ids and str(item.get("src") or "").startswith("http"):
                            completed.append(("", mid))
                except Exception:
                    pass
            if completed:
                completed.sort()
                claimed = self._claim_media_ids(
                    [media_id for _, media_id in completed],
                    expected_count,
                )
                if claimed:
                    return claimed
            store.patch_row("jobs", job_id, {
                "stage": "generating",
                "progress": min(90, 20 + int((timeout_s - (deadline - time.monotonic())) / 12)),
                "updatedAt": time.time(),
            })
            await asyncio.sleep(5)
        raise RuntimeError("FLOW_GENERATION_TIMEOUT: no completed video appeared in project data")

    async def _sync_credits(self, api, account_id: str) -> None:
        """Refresh the persisted balance without turning a successful job into a failure."""
        try:
            credit_info = await api.get_credits()
            store.patch_row("accounts", account_id, {
                "credits": int(credit_info.credits),
                "creditsSyncedAt": time.time(),
                "updatedAt": time.time(),
            })
        except Exception:
            # Credit reporting is secondary to generation. A later job or
            # reconnect can refresh it if Google's balance endpoint is busy.
            pass

    async def _run(self, job_id: str, *, profile_dir: Path | None = None) -> None:
        job = store.get_row("jobs", job_id)
        account = store.get_row("accounts", str(job.get("accountId"))) if job else None
        if not job or not account:
            return
        browser = None
        try:
            self._log("info", "job_started", job_id=job_id, account_id=account["id"], details={"kind": job["kind"]})
            if account.get("status") != "online" or not account.get("projectId"):
                raise RuntimeError("FLOW_LOGIN_REQUIRED: connect the Google Flow account first")
            from ._flow._api import FlowAPI
            from .browser import BrowserManager
            from ._flow._client import FlowClient
            # Generation uses the already-authenticated persistent profile in
            # background mode. Only the explicit account-connect flow opens a
            # visible Chrome window for interactive Google sign-in.
            browser = BrowserManager(headless=True, profile_dir=profile_dir or store.profile_dir(account["id"]))
            await browser.start()
            self._log("info", "browser_ready", job_id=job_id, account_id=account["id"])
            api = FlowAPI(browser, project_id=account["projectId"], default_timeout_s=600)
            client = FlowClient(api, browser, account["projectId"])
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
            submission_project_id = str(job.get("submissionProjectId") or "")
            project_changed_at = float(account.get("projectChangedAt") or 0)
            submitted_at = float(job.get("submissionStartedAt") or 0)
            submission_project_missing = bool(
                (submission_project_id and submission_project_id != account["projectId"])
                or (
                    not submission_project_id
                    and submitted_at
                    and project_changed_at
                    and submitted_at < project_changed_at
                )
            )
            if submission_project_missing:
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
                        "currentProjectId": account["projectId"],
                    },
                )
            store.patch_row("jobs", job_id, {"status": "processing", "stage": "submitting", "progress": 5, "updatedAt": time.time()})
            if job.get("submissionStartedAt") or job.get("mediaIds") or job.get("resumeOnly"):
                page = await browser.page()
                await client._ensure_project_page(page)
                media_ids = list(job.get("mediaIds") or [])
                if not media_ids:
                    media_ids = await self._recover_submitted_media(api, page, job)
                    job = {**job, "mediaIds": media_ids}
                outputs = []
                if job["kind"] == "video":
                    from ._flow._api import VideoJob
                    for index, media_id in enumerate(media_ids, 1):
                        self._check_cancel(job_id)
                        remote_job = VideoJob.__new__(VideoJob)
                        remote_job.media_name = media_id
                        remote_job.project_id = account["projectId"]
                        status = await api.wait_for_video(remote_job, timeout_s=900)
                        output = self._output_path(job, index, "mp4")
                        await api.download(status.fife_url, output)
                        outputs.append(str(output))
                else:
                    await self._recover_submitted_media(api, page, job)
                    items = await self._find_existing_project_media(api, page, job, "image", len(media_ids))
                    by_id = {str(item['id']): item for item in items}
                    if any(media_id not in by_id for media_id in media_ids):
                        raise RuntimeError("FLOW_GENERATION_TIMEOUT: original image media is not available yet")
                    for index, media_id in enumerate(media_ids, 1):
                        self._check_cancel(job_id)
                        output = self._output_path(job, index, str(settings.get("format", "png")).lower())
                        await api.download(str(by_id[media_id]['src']), output)
                        outputs.append(str(output))
            elif job["kind"] == "video":
                source = next(iter(job.get("sourceFiles") or []), None)
                model = _normalize_video_model(settings.get("model"))
                ratio = str(settings.get("ratio") or "16:9")
                page = await browser.page()
                await client._ensure_project_page(page)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 8, "updatedAt": time.time()})
                await self._prepare_ui_model(page, "video", model, ui=client._ui)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 12, "updatedAt": time.time()})
                await self._prepare_ui_format(
                    page,
                    ratio,
                    str(settings.get("duration") or "8"),
                    str(settings.get("resolution") or ""),
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
                # If we have a start image, switch to FRAME_TO_VIDEO NOW while the
                # settings panel is still open from _prepare_ui_model above.
                # switch_mode.open_settings_panel will see the panel as already open
                # and skip the unreliable JS click; it then Playwright-clicks the
                # "Frames" tab which does trigger React events correctly.
                # This must happen BEFORE the no-ops below so generate_video does not
                # re-attempt the switch and inadvertently close the panel.
                if source:
                    from ._flow._models import GenerationMode
                    await client._ui.switch_mode(page, GenerationMode.FRAME_TO_VIDEO)
                    if not await client._ui.upload_image(page, source):
                        raise RuntimeError("FLOW_UI_CHANGED: start image upload control was not found")
                extend_from = store.get_row("jobs", str(settings.get("extendFromJobId") or ""))
                count = max(1, min(4, int(settings.get("count", 1))))
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
                    store.patch_row("jobs", job_id, {"submissionStartedAt": time.time(), "submissionProjectId": account["projectId"], "baselineMediaIds": sorted(baseline_ids)})
                    remote = [await client.extend_video(media_id, workflow_id, job["prompt"])]
                    media_ids = [item.media_name for item in remote]
                    self._log("success", "generation_submitted", job_id=job_id, account_id=account["id"], details={"model": model, "mediaIds": media_ids})
                    store.patch_row("jobs", job_id, {"mediaIds": media_ids, "stage": "generating", "progress": 20})
                    await self._sync_credits(api, account["id"])
                    outputs = []
                    for output_index, media_id in enumerate(media_ids, 1):
                        self._check_cancel(job_id)
                        status = await api.wait_for_video(remote[0], timeout_s=900, on_poll=lambda _s, elapsed: store.patch_row("jobs", job_id, {"progress": min(90, 20 + int(elapsed / 12)), "updatedAt": time.time()}))
                        output = self._output_path(job, output_index, "mp4")
                        await api.download(status.fife_url, output)
                        outputs.append(str(output))
                        self._log("success", "output_downloaded", job_id=job_id, account_id=account["id"], details={"outputIndex": output_index, "path": str(output)})
                else:
                    baseline_text = await page.evaluate("""() => {
                        const top = document.querySelector('flow-grid-tile-container');
                        return top ? (top.innerText || '').trim().replace(/\\s+/g, ' ') : '';
                    }""")
                    await self._set_flow_count(page, count)
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    if not await client._ui.fill_prompt(page, job["prompt"]):
                        raise RuntimeError("FLOW_UI_CHANGED: prompt editor was not found")
                    await asyncio.sleep(1)
                    from ._flow._ui_interceptor import UIInterceptor
                    from ._flow._exceptions import GenerationTimeout
                    interceptor = UIInterceptor()
                    interceptor.attach(page)
                    store.patch_row("jobs", job_id, {"submissionStartedAt": time.time(), "submissionProjectId": account["projectId"], "baselineMediaIds": sorted(baseline_ids)})
                    await self._click_flow_submit(page)
                    store.patch_row("jobs", job_id, {"stage": "generating", "progress": 20, "updatedAt": time.time()})
                    self._log("success", "generation_submitted", job_id=job_id, account_id=account["id"], details={"model": model})
                    try:
                        captured = await _await_with_job_progress(
                            interceptor.wait_for("batchAsyncGenerateVideoText", timeout=30, require_success=True),
                            job_id, timeout_s=30, ceiling=30,
                        )
                    except GenerationTimeout:
                        captured = None
                    media_ids = _captured_video_ids(captured.resp or {}) if captured else []
                    if media_ids:
                        self._log("success", "api_generation_submitted", job_id=job_id, account_id=account["id"], details={"mediaIds": media_ids})
                        store.patch_row("jobs", job_id, {"mediaIds": media_ids, "stage": "generating", "progress": 20})
                        from ._flow._api import VideoJob
                        # Wait for all videos in parallel, then download in parallel
                        async def _wait_and_dl_video(media_id: str, idx: int) -> str:
                            self._check_cancel(job_id)
                            remote_job = VideoJob.__new__(VideoJob)
                            remote_job.media_name = media_id
                            remote_job.project_id = account["projectId"]
                            status = await api.wait_for_video(
                                remote_job, timeout_s=900,
                                on_poll=lambda _s, elapsed: store.patch_row("jobs", job_id, {"progress": min(90, 20 + int(elapsed / 12)), "updatedAt": time.time()}),
                            )
                            out = self._output_path(job, idx, "mp4")
                            await api.download(status.fife_url, out)
                            return str(out)
                        outputs = list(await asyncio.gather(*[
                            _wait_and_dl_video(mid, i)
                            for i, mid in enumerate(media_ids[:count], 1)
                        ]))
                        asyncio.create_task(self._sync_credits(api, account["id"]))
                    else:
                        self._log("warning", "ui_generation_fallback", job_id=job_id, account_id=account["id"], details={"kind": "video"})
                        outputs = await self._wait_and_download_flow_videos(
                            page, job, count, baseline_text, job_id,
                        )
                        asyncio.create_task(self._sync_credits(api, account["id"]))
            else:
                model = str(settings.get("model") or "Nano Banana 2")
                sources = job.get("sourceFiles") or []
                page = await browser.page()
                await client._ensure_project_page(page)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 8, "updatedAt": time.time()})
                await self._prepare_ui_model(page, "image", model, ui=client._ui)
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 12, "updatedAt": time.time()})
                await self._prepare_ui_format(
                    page,
                    str(settings.get("ratio") or "16:9"),
                    # Image resolution ("1K"/"2K"/"4K") is a plan-tier label only;
                    # Flow has no UI tab for it — do not pass to avoid FLOW_SETTING_MISMATCH.
                )
                store.patch_row("jobs", job_id, {"stage": "preparing", "progress": 15, "updatedAt": time.time()})
                for source in sources:
                    await client._ui.upload_image(page, source)
                count = max(1, min(4, int(settings.get("count", 1))))
                baseline_media = await self._project_media_elements(page)
                baseline_ids = {str(item.get("id")) for item in baseline_media if item.get("id")}
                media_items: list[dict[str, Any]] = []
                if not media_items:
                    await self._set_flow_count(page, count)
                    if not await client._ui.fill_prompt(page, job["prompt"]):
                        raise RuntimeError("FLOW_UI_CHANGED: prompt editor was not found")
                    from ._flow._ui_interceptor import UIInterceptor
                    from ._flow._exceptions import GenerationTimeout
                    interceptor = UIInterceptor()
                    interceptor.attach(page)
                    submission_patch = {"submissionStartedAt": time.time(), "submissionProjectId": account["projectId"], "baselineMediaIds": sorted(baseline_ids)}
                    store.patch_row("jobs", job_id, submission_patch)
                    job = {**job, **submission_patch}  # keep local var in sync
                    await self._click_flow_submit(page)
                    store.patch_row("jobs", job_id, {"stage": "generating", "progress": 20, "updatedAt": time.time()})
                    # Race: interceptor RPC capture vs project-API polling.
                    # batchGenerateImages may be async (returns 200 without fifeUrl),
                    # so don't wait 180s for it — poll project media in parallel.
                    intercept_task = asyncio.create_task(
                        interceptor.wait_for("batchGenerateImages", timeout=120, require_success=True)
                    )
                    poll_task = asyncio.create_task(
                        self._wait_for_project_media(
                            page, baseline_ids, "image", count, job_id,
                            api=api, job=job, timeout_s=900,
                        )
                    )
                    try:
                        done, pending = await asyncio.wait(
                            {intercept_task, poll_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # Cancel the loser
                        for t in pending:
                            t.cancel()
                            await asyncio.gather(t, return_exceptions=True)
                        finished = next(iter(done))
                        result = finished.result()
                        if finished is intercept_task:
                            captured_resp = result.resp or {}
                            media_items = _captured_image_items(captured_resp)
                            if media_items:
                                self._log("success", "api_generation_complete", job_id=job_id, account_id=account["id"], details={"mediaIds": [item["id"] for item in media_items]})
                            else:
                                # RPC returned but no fifeUrl → use poll result
                                media_items = await poll_task if not poll_task.cancelled() else await self._wait_for_project_media(
                                    page, baseline_ids, "image", count, job_id, api=api, job=job, timeout_s=900,
                                )
                        else:
                            # Poll task won
                            media_items = result
                            self._log("success", "poll_generation_complete", job_id=job_id, account_id=account["id"], details={"mediaIds": [item["id"] for item in media_items]})
                    except (GenerationTimeout, Exception):
                        for t in [intercept_task, poll_task]:
                            if not t.done():
                                t.cancel()
                                await asyncio.gather(t, return_exceptions=True)
                        raise
                media_ids = [str(item["id"]) for item in media_items]
                self._log("success", "generation_submitted", job_id=job_id, account_id=account["id"], details={"model": settings.get("model"), "mediaIds": media_ids})
                store.patch_row("jobs", job_id, {"mediaIds": media_ids, "stage": "downloading", "progress": 80})
                # Download all images in parallel for speed
                fmt = str(settings.get("format", "png")).lower()
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
            store.patch_row("jobs", job_id, {"status": "done", "stage": "done", "progress": 100, "outputs": outputs, "updatedAt": time.time()})
            if job.get("seriesContext"):
                from . import series
                series.mark_job_complete(job, outputs)
            self._log("success", "job_completed", job_id=job_id, account_id=account["id"], details={"outputCount": len(outputs)})
        except asyncio.CancelledError:
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
            needs_login = _session_needs_login(exc)
            action = "action_required" if needs_login else "failed"
            failed_stage = (store.get_row("jobs", job_id) or {}).get("stage")
            store.patch_row("jobs", job_id, {"status": action, "stage": action, "error": str(exc), "updatedAt": time.time()})
            if needs_login:
                # Do not recursively reopen the shared login profile from each
                # failed worker. Leave jobs action_required for explicit reconnect.
                store.patch_row("accounts", account["id"], {"status": "reconnect", "error": str(exc), "updatedAt": time.time()})
            if job.get("seriesContext"):
                from . import series
                series.mark_job_error(job, str(exc))
            self._log("error", "job_failed", job_id=job_id, account_id=account["id"], message=str(exc), details={"stage": failed_stage})
        finally:
            if browser:
                await browser.stop()

    def _output_folder(self, job: dict[str, Any], *, create: bool = True) -> Path:
        """Return ``flow/<kind>/<user-name>`` without hidden job folders."""
        settings = job.get("settings") or {}
        selected = Path(str(settings.get("outputDir") or "results")).expanduser()
        kind = safe_output_part(job.get("kind") or "video", "video")
        flow_tab = f"flow-{kind}"  # → ~/Downloads/ZM_AI_TOOL/flow/video/ or .../flow/image/
        series_context = job.get("seriesContext") or {}
        if series_context:
            # Series artifacts share one ``flow/series/<slug>`` namespace and
            # remain split by kind below it.  This keeps deletion/reveal paths
            # compatible with existing Series data while regular jobs use the
            # ``flow/image`` and ``flow/video`` roots above.
            root = selected_or_default("flow", "")
            series_root = root / "series" / safe_output_part(series_context.get("seriesSlug") or series_context.get("seriesTitle") or "series", "series") / kind
            folder = series_root / "anchors" if series_context.get("artifact") == "anchor" else series_root / f"tap-{int(series_context.get('episodeIndex') or 1):02d}"
        elif selected.is_absolute() and os.environ.get("ZM_AI_TOOL_DESKTOP") == "1":
            folder = _selected_flow_folder(selected, kind)

        elif selected.is_absolute():
            # Browser jobs are downloaded from the backend's public tree; do
            # not let a client-supplied absolute path escape that sandbox.
            root = selected_or_default(flow_tab, "")
            folder = root / safe_output_part(selected.name or "results", "results")

        else:
            root = selected_or_default(flow_tab, "")
            folder = root / safe_output_part(selected, "results")
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder


    def _display_output_folder(self, job: dict[str, Any]) -> Path:
        """Return the real worker destination shown in the queue UI."""
        return self._output_folder(job, create=False)

    def _output_path(self, job: dict[str, Any], output_index: int, suffix: str) -> Path:
        folder = self._output_folder(job)
        settings = job.get("settings") or {}
        prefix = safe_output_part(settings.get("filePrefix") or job["kind"], str(job["kind"]))
        safe_suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix).lower() or ("mp4" if job["kind"] == "video" else "png")
        series_context = job.get("seriesContext") or {}
        if series_context and series_context.get("sceneIndex"):
            scene_idx = int(series_context.get("sceneIndex") or 1)
            variant = f"_{output_index:02d}" if max(1, int(settings.get("count", 1))) > 1 else ""
            return folder / f"canh-{scene_idx:03d}__{job['id'][:8]}__{prefix}{variant}.{safe_suffix}"
        input_index = int(job["inputIndex"])
        variant = f"_{output_index:02d}" if max(1, int(settings.get("count", 1))) > 1 else ""
        return folder / f"{input_index:03d}__{job['id']}__{prefix}_{input_index:03d}{variant}.{safe_suffix}"

    def _check_cancel(self, job_id: str) -> None:
        if job_id in self._cancelled:
            raise asyncio.CancelledError

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._account_condition:
            self._cancelled.add(job_id)
            self._account_condition.notify_all()
        job = store.patch_row("jobs", job_id, {"status": "cancelled", "stage": "cancelled", "progress": 0, "updatedAt": time.time()})
        if job:
            self._log("warning", "job_cancel_requested", job_id=job_id, account_id=str(job.get("accountId") or ""))
        return job

    def retry(self, job_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
        existing = store.get_row("jobs", job_id)
        if not existing:
            return None
        overrides = overrides or {}
        account_id = str(overrides.get("accountId") or existing.get("accountId") or "")
        settings = dict(existing.get("settings") or {})
        settings.update({key: value for key, value in dict(overrides.get("settings") or {}).items() if value not in (None, "")})
        account = store.get_row("accounts", account_id) or {}
        settings, _ = _normalize_catalog_settings(account, str(existing.get("kind") or "video"), settings)
        if existing.get("kind") == "video" and not _catalog_section(account, "video"):
            settings["model"] = _normalize_video_model(settings.get("model"))
        if account_id and not account:
            raise ValueError("Flow account not found")
        with self._account_condition:
            last_error_signature = str(existing.get("lastFlowErrorSignature") or "")
            if last_error_signature:
                self._claimed_error_tiles.discard(last_error_signature)
            orders = [
                int(row.get("queueOrder")) for row in self.jobs()
                if row.get("accountId") == account_id and str(row.get("queueOrder", "")).isdigit()
            ]
            order = max(orders, default=-1) + 1
            self._account_next_order[account_id] = max(self._account_next_order.get(account_id, 0), order + 1)
            self._cancelled.discard(job_id)
            job = store.patch_row("jobs", job_id, {
                "resumeOnly": bool(existing.get("resumeOnly") or existing.get("submissionStartedAt") or existing.get("mediaIds")),
                "status": "queued", "stage": "queued", "progress": 0,
                "queueOrder": order, "error": None, "outputs": [], "updatedAt": time.time(),
                "accountId": account_id, "settings": settings,
                "generationRejectRetryCount": 0,
            })
            self._account_condition.notify_all()
        if job:
            self._log("info", "job_retry", job_id=job_id, account_id=str(job.get("accountId") or ""))
            threading.Thread(target=self._run_sync, args=(job_id,), daemon=True, name=f"flow-job-{job_id}").start()
        return job


service = FlowService()
