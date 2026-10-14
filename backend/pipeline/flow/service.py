"""Persistent-account Google Flow worker built around flow-py."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import errno
import logging
import json
import os
import random
import re
import unicodedata
try:
    from ._flow._models import GenerationMode as _GenerationMode
except Exception:  # ponytail: graceful if flow lib version lacks this
    _GenerationMode = None
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

_log = logging.getLogger(__name__)

from pipeline.core.config import PUBLIC_DATA
from pipeline.core.jobs import Cancelled
from pipeline.core.network import network_available, wait_for_network
from pipeline.core.output_paths import safe_output_part, selected_or_default
from . import store

# Match both legacy labs.google/fx/tools/flow/project/<id> and new flow.google.com/project/<id>
_PROJECT_RE = re.compile(r"^(?:https://(?:flow\.google\.com|labs\.google)(?::443)?)?(?:/fx/tools/flow|/flow)?/project/([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:[/?#]|$)")
_TERMINAL = {"done", "failed", "cancelled", "action_required"}
FLOW_LOG_LIMIT = 300
_DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT = 3
_MAX_CONCURRENT_JOBS_PER_ACCOUNT = 18
_SESSION_REFRESH_INTERVAL_S = 60.0
# Transient / session / project / generation-rejected: auto-retry up to this
# many times so the queue keeps finishing. Only mandatory stops (credits,
# quota, cancel, content policy) require a manual hand.
_JOB_AUTO_RETRY_MAX = 3
# A Flow generation can legitimately take longer than ten minutes; keep a
# generous upper bound so a lost Flow page cannot leave a worker stuck forever.
# Flow acknowledges an image submission before mounting the result tile. On
# migrated accounts the project payload can lag another minute or two.
# Keep the waiter alive long enough to observe that same submission instead of
# recreating it and charging the account twice.
_IMAGE_GENERATION_TIMEOUT_S = 60
_VIDEO_GENERATION_TIMEOUT_S = 120
# While Flow's tile % keeps rising the timeout slides forward, capped at this
# multiple of the base timeout (image 5 min, video 10 min).
_GENERATION_WAIT_CAP = 5
# Flow generation reports up to 99%; downloaded = 99.1%, logo removal reports real work up to 99.9%.
POST_PROGRESS_START = 99.1
POST_PROGRESS_END = 99.9
_PLAN_SYNC_TTL_S = 300
_PROJECT_MIGRATION_RECOVERY_WINDOW_S = 600
_FLOW_SUBMIT_DELAY_MIN_S = 0.1
_FLOW_SUBMIT_DELAY_MAX_S = 1.0
_PROFILE_COPY_IGNORES = {
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "GraphiteDawnCache", "GPUPersistentCache", "ShaderCache", "GrShaderCache",
    "Crashpad", "SingletonCookie", "SingletonLock", "SingletonSocket", "LOCK",
    "BrowserMetrics", "VariationsSeedV2",
    # Chrome writes this file while starting and can remove it during shutdown;
    # it is not part of the Google session state needed by Flow.
    "RunningChromeVersion",
}


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
        if cookie.get("domain") or cookie.get("url"):
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
_IMAGE_UI_MODELS = {"Nano Banana Pro", "Nano Banana 2.1", "Nano Banana 2", "Nano Banana 2 Lite"}
_VIDEO_UI_MODELS = {
    "Omni 1.1 Flash", "Veo 3.1 - Lite", "Veo 3.1 - Fast",
    "Veo 3.1 - Quality", "Veo 3.1 - Lite [Lower Priority]",
}
# Temporarily disabled upstream; remove this entry to re-enable when Flow restores it.
_DISABLED_VIDEO_MODELS = {"Veo 3.1 - Lite [Lower Priority]"}
_DEFAULT_VIDEO_MODEL = "Veo 3.1 - Fast"


class _FlowNetworkInterrupted(Exception):
    """Close the failed browser once, then resume the same generation identity."""


def _network_ready_for_flow_job(service: "FlowService", job_id: str, *, force: bool = False) -> float:
    """Gate external Flow work while preserving a cancellable queued job."""
    def cancelled() -> bool:
        row = store.get_row("jobs", job_id)
        return job_id in service._cancelled or row is None or row.get('status') == 'cancelled'

    def report_wait(_delay: float) -> None:
        with service._account_condition:
            if not cancelled():
                store.patch_row("jobs", job_id, {
                    "status": "processing", "stage": "waiting_network",
                    "updatedAt": time.time(),
                })

    try:
        paused = wait_for_network(cancel_check=cancelled, on_wait=report_wait, force_check=force)
    except Cancelled:
        raise asyncio.CancelledError from None
    with service._account_condition:
        if cancelled():
            raise asyncio.CancelledError
        if paused:
            store.patch_row('jobs', job_id, {'stage': 'starting', 'updatedAt': time.time()})
    return paused


def _normalize_video_model(value: Any) -> str:
    """Migrate saved jobs away from models Flow has temporarily removed."""
    model = str(value or _DEFAULT_VIDEO_MODEL).strip()
    if model == "Omni Flash":
        model = "Omni 1.1 Flash"
    model = model.replace("Veo 3.1 Fast", _DEFAULT_VIDEO_MODEL).replace(
        "Veo 3.1 Quality", "Veo 3.1 - Quality",
    )
    return _DEFAULT_VIDEO_MODEL if model in _DISABLED_VIDEO_MODELS else model


def _flow_submit_button_score(text: str = "", aria_label: str = "", title: str = "") -> int:
    """Score the current Flow submit control across locales and UI builds."""
    text_value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    aria_value = re.sub(r"\s+", " ", f"{aria_label or ''} {title or ''}").strip().lower()
    if re.search(r"bắt đầu tạo|tạo ảnh|tạo video|start creating|start generation|generate now", aria_value):
        return 100
    if text_value == "arrow_forward" or "arrow_forward" in text_value:
        return 90
    if re.search(r"\b(create|generate|submit|run)\b", aria_value):
        return 80
    if re.search(r"bắt đầu tạo|tạo ảnh|tạo video|\b(create|generate|submit|run)\b", text_value):
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


_VIDEO_DONE_STATUSES = frozenset({
    "MEDIA_GENERATION_STATUS_COMPLETE",
    "MEDIA_GENERATION_STATUS_SUCCESS",
    "MEDIA_GENERATION_STATUS_SUCCESSFUL",
})
_VIDEO_FAIL_STATUSES = frozenset({
    "MEDIA_GENERATION_STATUS_FAILED",
    "MEDIA_GENERATION_STATUS_REJECTED",
})


def _video_fife_url(media: dict[str, Any] | None) -> str:
    """Extract a downloadable video URL from project-media / poll payloads."""
    if not isinstance(media, dict):
        return ""
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    candidates: list[Any] = [
        video,
        video.get("generatedVideo") if isinstance(video, dict) else None,
        video.get("video") if isinstance(video, dict) else None,
        media.get("generatedVideo"),
        media,
    ]
    for block in candidates:
        if not isinstance(block, dict):
            continue
        for key in ("fifeUrl", "fife_url", "url", "uri", "downloadUrl", "download_url"):
            value = str(block.get(key) or "").strip()
            if value.startswith("http"):
                return value
    return ""


def _image_fife_url(media: dict[str, Any] | None) -> str:
    """Extract a downloadable image URL from project-media payloads."""
    if not isinstance(media, dict):
        return ""
    image = media.get("image") if isinstance(media.get("image"), dict) else {}
    candidates: list[Any] = [
        image,
        image.get("generatedImage") if isinstance(image, dict) else None,
        media.get("generatedImage"),
        media,
    ]
    for block in candidates:
        if not isinstance(block, dict):
            continue
        for key in ("fifeUrl", "fife_url", "url", "uri", "downloadUrl", "download_url"):
            value = str(block.get(key) or "").strip()
            if value.startswith("http"):
                return value
    return ""


def _video_media_status(media: dict[str, Any] | None) -> str:
    if not isinstance(media, dict):
        return ""
    return str(
        (((media.get("mediaMetadata") or {}).get("mediaStatus") or {}).get("mediaGenerationStatus"))
        or ""
    )


def _video_download_qualities_for_plan(plan: str | None) -> list[str]:
    """Download menu tiers by Flow plan (includes 360p for faster/smaller files)."""
    normalized = str(plan or "Free").strip()
    if normalized == "Ultra":
        return ["360p", "720p", "1080p", "4K"]
    if normalized in {"Pro", "Plus"} or normalized.lower() == "plus":
        return ["360p", "720p", "1080p"]
    return ["360p", "720p"]


def _video_download_quality(settings: dict[str, Any] | None, plan: str | None = None) -> str:
    """Map job settings to Flow download menu labels, clamped to account plan.

    When ``plan`` is omitted, only map the label (callers that already clamped).
    """
    raw = str((settings or {}).get("quality") or "").strip().lower()
    if raw in {"4k", "2160", "2160p"} or "4k" in raw:
        preferred = "4K"
    elif raw in {"high", "1080", "1080p"} or "1080" in raw:
        preferred = "1080p"
    elif raw in {"360", "360p", "low", "fast"} or "360" in raw:
        preferred = "360p"
    else:
        preferred = "720p"
    if plan is None:
        return preferred
    allowed = _video_download_qualities_for_plan(plan)
    return preferred if preferred in allowed else allowed[-1]


def _pick_video_menu_item(items: list[str], preferred: str) -> int | None:
    """Index of the Flow download menu item for ``preferred`` — no silent downgrade.

    Items read like "720p Kích thước gốc" / "1080p Upscaled"; the leading token is
    the tier. 360p/720p may fall back to the original-size item, never 1080p/4K.
    """
    tier = str(preferred or "").strip().lower()
    def menu_text(value: Any) -> str:
        # Flow sometimes returns Vietnamese accents in decomposed Unicode
        # form (e.g. ``Kích thước gốc``), so normalize before matching.
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", value).strip().lower()

    texts = [menu_text(item) for item in items]
    for index, text in enumerate(texts):
        if re.match(rf"{re.escape(tier)}(?![0-9a-z])", text):
            return index
    if tier in {"360p", "720p"}:
        for index, text in enumerate(texts):
            if "goc" in text or "original" in text:
                return index
    return None


def _video_media_ready(media: dict[str, Any] | None) -> bool:
    """True when Flow has finished the clip (status and/or download URL).

    Veo Lite often marks SUCCESSFUL before ``video`` is attached, or leaves a
    fifeUrl while status stays non-terminal — either means we can proceed.
    """
    if not isinstance(media, dict):
        return False
    status = _video_media_status(media)
    if status in _VIDEO_FAIL_STATUSES:
        return False
    if status in _VIDEO_DONE_STATUSES:
        return True
    if status.endswith(("_SUCCESS", "_SUCCESSFUL", "_COMPLETE")):
        return True
    return bool(_video_fife_url(media))


def _classify_visible_flow_error(text: str) -> str:
    """Map Flow tile error copy to a stable code (credits vs quota vs generic)."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if re.search(
        r"abnormal activity|hoạt động bất thường|hoạt động đáng ngờ|suspicious activity|unusual activity",
        low,
    ):
        return f"FLOW_AUTOMATION_BLOCKED: {raw}"
    # "please wait a few seconds" is a transient server-side throttle, not bot detection.
    if re.search(r"please wait a few seconds|vui lòng chờ vài giây", low):
        return f"FLOW_GENERATION_REJECTED: {raw}"
    if re.search(
        r"credit|t[ií]n d[uụ]ng|insufficient|not enough|out of\s+credits|h[eế]t t[ií]n",
        low,
    ):
        return f"FLOW_CREDITS_EMPTY: {raw}"
    if re.search(
        r"quota|h[eế]t l[uư][oợ]t|h[eế]t h[aạ]n m[uứ]c|usage limit|generation limit|"
        r"daily limit|monthly limit|rate.?limit|too many|limit reached|h[aạ]n m[uứ]c",
        low,
    ):
        return f"FLOW_QUOTA_EXHAUSTED: {raw}"
    return f"FLOW_GENERATION_REJECTED: {raw}"


def _flow_generation_progress(pct: int) -> int:
    """Map Flow's own 0-100 tile % onto the job's generating band (15-99).

    The job is already at 15% when Flow starts counting from 0%, so a plain
    max() hides the first part of every generation.
    """
    return 15 + round(min(100, max(0, int(pct))) * 84 / 100)


def _flow_credit_blocked(text: str) -> bool:
    """Detect Flow's pre-submit credit gate, before it exposes a submit button."""
    return bool(re.search(
        r"không có đủ tín dụng để thực hiện hành động này|"
        r"not enough credits|insufficient credits|out of credits|"
        r"you (?:do not|don't) have enough credits",
        str(text or ""),
        re.I,
    ))


# Cooldown per error type (0 = no suspension):
_COOLDOWN_QUOTA_S     = 14 * 3600   # 14h — FLOW_QUOTA_EXHAUSTED (daily quota)
_COOLDOWN_RATELIMIT_S = 60          # 1min — HTTP 429 / rate limit
_COOLDOWN_CREDITS_S   = 24 * 3600   # 24h — FLOW_CREDITS_EMPTY (needs manual top-up)
# GENERATION_REJECTED — no suspension (content/prompt issue, not account issue)
# FLOW_LOGIN_REQUIRED — no suspension (_ensure_shared_reconnect handles it)


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


def _image_input_type_for_mode(mode: str) -> str:
    """Flow batchGenerateImages: edit = base image; reference = reference inputs."""
    return "IMAGE_INPUT_TYPE_BASE_IMAGE" if mode == "edit" else "IMAGE_INPUT_TYPE_REFERENCE"


def _prompt_with_reference_strength(prompt: str, mode: str, strength: Any) -> str:
    """Flow has no public strength field — steer adherence via prompt when refs are used."""
    text = str(prompt or "").strip()
    if mode not in {"edit", "reference"}:
        return text
    try:
        value = max(0, min(100, int(strength)))
    except (TypeError, ValueError):
        value = 70
    if mode == "edit":
        if value >= 75:
            hint = "Edit the source image while preserving identity, composition, and key details."
        elif value >= 40:
            hint = "Edit the source image; keep the main subject recognizable while allowing creative changes."
        else:
            hint = "Use the source image only as loose inspiration; prioritize the prompt."
    else:
        if value >= 75:
            hint = "Strictly preserve character identity, wardrobe, and style from the reference image(s)."
        elif value >= 40:
            hint = "Keep the subject and style from the reference image(s) while following the prompt."
        else:
            hint = "Loosely inspired by the reference image(s); prioritize the prompt."
    if hint.lower() in text.lower():
        return text
    return f"{hint} {text}".strip()


def _patch_batch_generate_image_inputs(body: dict[str, Any], *, mode: str, media_names: list[str]) -> dict[str, Any]:
    """Ensure imageInputs use the correct type for edit vs reference."""
    if not isinstance(body, dict):
        return body
    input_type = _image_input_type_for_mode(mode)
    names = [str(name).strip() for name in media_names if str(name).strip()]
    requests = body.get("requests")
    if not isinstance(requests, list):
        return body
    for request in requests:
        if not isinstance(request, dict):
            continue
        inputs = request.get("imageInputs")
        if not isinstance(inputs, list):
            inputs = []
        if not inputs and names:
            capped = names[:1] if mode == "edit" else names[:3]
            inputs = [
                {
                    "name": name,
                    "mediaName": name,
                    "imageInputType": input_type,
                    "role": "REFERENCE_IMAGE",
                }
                for name in capped
            ]
        else:
            for item in inputs:
                if isinstance(item, dict):
                    item["imageInputType"] = input_type
                    item.setdefault("role", "REFERENCE_IMAGE")
                    if item.get("mediaName") and not item.get("name"):
                        item["name"] = item["mediaName"]
        request["imageInputs"] = inputs
    return body


def _detect_plan(credit_info: Any) -> str | None:
    """Map Flow's Credits object to Free / Plus / Pro / Ultra.

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
    if "plus" in combined or "tier_5" in combined:
        return "Plus"
    if "pro" in combined or "tier_one" in combined or "tier_1" in combined:
        return "Pro"
    if "tier_0" in combined or "tier_3" in combined or "free" in combined or "standard" in combined:
        return "Free"
    return None


_FLOW_PLANS = frozenset({"Free", "Plus", "Pro", "Ultra"})



def _job_concurrency(settings: dict[str, Any]) -> int:
    try:
        value = int(settings.get("concurrency", _DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT))
    except (TypeError, ValueError):
        value = _DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT
    return max(1, min(_MAX_CONCURRENT_JOBS_PER_ACCOUNT, value))


def _flow_headless(settings: dict[str, Any]) -> bool:
    """Normalize visible-browser settings from current and legacy clients."""
    if "headless" in settings:
        value = settings.get("headless")
    elif "showBrowser" in settings:
        value = not settings.get("showBrowser")
    else:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value)


async def _wait_before_flow_submit() -> float:
    """Jitter only the submit edge; leave concurrency/render/download unchanged."""
    delay = random.uniform(_FLOW_SUBMIT_DELAY_MIN_S, _FLOW_SUBMIT_DELAY_MAX_S)
    await asyncio.sleep(delay)
    return delay


def _flow_job_credit_cost(account: dict[str, Any], kind: str, model: Any) -> int | None:
    """Read the selected model cost from this account's verified catalog."""
    if str(kind).lower() != "video":
        return None
    section = _catalog_section(account, "video")
    requested = str(model or "").strip()
    entries = section.get("models", []) if isinstance(section, dict) else []
    selected = next(
        (item for item in entries if isinstance(item, dict) and (
            str(item.get("name") or "") == requested
            or _match_model_choice(requested, str(item.get("name") or ""))
        )),
        None,
    )
    if not selected:
        return None
    raw = selected.get("creditCost", selected.get("cost"))
    try:
        if raw is not None:
            return max(0, int(raw))
    except (TypeError, ValueError):
        pass
    # Flow's capability UI omits creditCost on some accounts, while the
    # authoritative API model registry still exposes the exact cost.  Use it
    # only as a catalog lookup fallback; unknown models remain unbounded.
    try:
        from ._flow._api import MODEL_REGISTRY
        selected_name = str(selected.get("name") or requested)
        match = next(
            (item for item in MODEL_REGISTRY.values()
             if isinstance(item, dict) and (
                 str(item.get("display") or "") == selected_name
                 or _match_model_choice(selected_name, str(item.get("display") or ""))
             )),
            None,
        )
        cost = match.get("cost") if match else None
        return max(0, int(cost)) if cost is not None else None
    except (ImportError, TypeError, ValueError):
        return None


def _ensure_flow_credits_for_job(account: dict[str, Any], kind: str, settings: dict[str, Any]) -> None:
    if kind != "video":
        return
    credits = account.get("credits")
    if not isinstance(credits, (int, float)):
        return
    required = _flow_job_credit_cost(account, kind, settings.get("model"))
    if required is None:
        return
    if int(credits) < required:
        raise ValueError(
            f"FLOW_CREDITS_INSUFFICIENT: Cần ít nhất {required} credits, "
            f"nhưng tài khoản chỉ còn {int(credits)} (Need {required} credits; "
            f"account has {int(credits)} left)"
        )


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
    if "lite" in t:
        return False
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


def _video_ui_resolution(value: Any) -> str:
    """Flow video tabs use 360p/480p/720p/1080p; image plan tiers (1K/2K/4K) are not selectable."""
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{3,4}p", text, re.I) else ""


def _image_ui_resolution(value: Any) -> str:
    """Image plan labels are 1K/2K/4K — never video Np."""
    text = str(value or "").strip()
    match = re.fullmatch(r"([1-9]\d{0,1})[Kk]", text)
    return f"{match.group(1)}K" if match else ""


def _official_flow_video_cost(model: str, plan: str | None, duration: str = "8", resolution: str = "720p") -> int | None:
    """Official per-generation Flow price used when the live price RPC is absent."""
    name = str(model or "").lower()
    tier = str(plan or "Free").lower()
    seconds = str(duration or "8")
    if "omni" in name and "flash" in name:
        costs = {"360p": {"4": 4, "6": 5, "8": 6, "10": 7},
                 "720p": {"4": 7, "6": 10, "8": 12, "10": 15}}
        return costs.get(str(resolution or "720p").lower(), costs["720p"]).get(seconds)
    if "veo" in name and "quality" in name:
        return 100
    if "veo" in name and "fast" in name:
        return 10 if tier == "ultra" else 20
    if "veo" in name and "lite" in name:
        return 5 if tier == "ultra" else 10
    return None


def _clamp_image_resolution(value: Any, plan: str | None = None) -> str:
    plan_name = str(plan or "Free").strip()
    if plan_name == "Ultra":
        options = ["1K", "2K", "4K"]
    elif plan_name in {"Pro", "Plus"} or re.fullmatch(r"plus", plan_name, re.I):
        options = ["1K", "2K"]
    else:
        options = ["1K"]
    requested = _image_ui_resolution(value)
    return requested if requested in options else options[0]


def _video_model_entry(account: dict[str, Any], model: str) -> dict[str, Any] | None:
    """Return the verified video catalog entry for this model, if any."""
    section = _catalog_section(account, "video")
    models = [item for item in section.get("models", []) if isinstance(item, dict) and item.get("name")]
    if not models:
        return None
    selected = next((item for item in models if item["name"] == model), None)
    if selected is None:
        selected = next((item for item in models if _match_model_choice(model, str(item["name"]))), None)
    return selected


def _video_ui_duration(account: dict[str, Any], settings: dict[str, Any]) -> str | None:
    """Duration to click in Flow UI, or None when the model has no duration tabs.

    Veo (Free/Pro/Ultra) exposes a fixed length — no 4/6/8/10s radios. Only Omni
    Flash should attempt selection, even if a stale catalog lists durations.
    Passing a default ``8`` for Veo makes Playwright wait for a missing control.
    """
    model = _normalize_video_model(settings.get("model"))
    if not re.search(r"\bomni\b.*\bflash\b", model, re.I):
        return None
    selected = _video_model_entry(account, model)
    if selected is not None:
        durations = [str(value).strip() for value in selected.get("durations") or [] if str(value).strip()]
        if not durations:
            return None
        requested = str(settings.get("duration") or "").strip()
        if requested in durations:
            return requested
        fallback = str(selected.get("defaultDuration") or durations[0]).strip()
        return fallback if fallback in durations else durations[0]
    requested = str(settings.get("duration") or "8").strip()
    return requested if re.fullmatch(r"\d{1,3}", requested) else "8"


def _normalize_catalog_settings(
    account: dict[str, Any], kind: str, settings: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Select only model/ratio/duration values verified for this account."""
    section = _catalog_section(account, kind)
    original_settings = settings
    if kind == "image" and settings.get("model") in _IMAGE_UI_MODELS:
        # The live selector verifies known image models; a stale catalog must not replace them.
        if not any(item.get("name") == settings.get("model") for item in section.get("models", []) if isinstance(item, dict)):
            section = {}
    models = [item for item in section.get("models", []) if isinstance(item, dict) and item.get("name")]
    if not models:
        normalized = dict(settings)
        # Shared UI settings default to image tiers; strip them from video jobs
        # even when the account catalog has not been verified yet.
        changed = False
        if kind == "video" and not _video_ui_resolution(normalized.get("resolution")):
            if str(normalized.get("resolution") or "").strip():
                normalized["resolution"] = ""
                changed = True
        if kind == "image":
            clamped = _clamp_image_resolution(normalized.get("resolution"), account.get("plan"))
            if str(normalized.get("resolution") or "") != clamped:
                normalized["resolution"] = clamped
                changed = True
            if str(normalized.get("quality") or "").strip():
                # Download quality is video-only; drop leftover Omni 360p/720p.
                normalized["quality"] = ""
                changed = True
        if kind == "video" and _video_ui_duration(account, normalized) is None:
            if str(normalized.get("duration") or "").strip():
                # Keep the empty field for settings/UI compatibility. The
                # worker converts it to None and therefore does not click a
                # duration control for fixed-length models such as Veo.
                normalized["duration"] = ""
                changed = True
        return normalized, changed or normalized != original_settings
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
    if kind == "video":
        duration = _video_ui_duration(account, normalized)
        if duration is None:
            normalized["duration"] = ""
        elif str(normalized.get("duration") or "") != duration:
            normalized["duration"] = duration
    resolutions = [str(value) for value in selected.get("resolutions", []) if str(value)]
    if kind == "video":
        # Flow exposes generation resolution only for Omni Flash. Veo uses its
        # model-defined resolution and must not inherit a stale 360p/720p value.
        if not re.search(r"\bomni\b.*\bflash\b", str(normalized.get("model") or ""), re.I):
            normalized["resolution"] = ""
            resolutions = []
        resolutions = [value for value in resolutions if _video_ui_resolution(value)]
        requested_resolution = str(normalized.get("resolution") or "").lower()
        if not resolutions:
            # Model has no resolution UI tabs — clear any leftover value so the
            # worker does not try to click a control that does not exist.
            if requested_resolution:
                normalized["resolution"] = ""
        elif requested_resolution not in [value.lower() for value in resolutions] and not _video_ui_resolution(requested_resolution):
            fallback = str(selected.get("defaultResolution") or resolutions[0])
            normalized["resolution"] = _video_ui_resolution(fallback) or resolutions[0]
        elif requested_resolution and not _video_ui_resolution(requested_resolution):
            normalized["resolution"] = ""
    else:
        resolutions = [value for value in (_image_ui_resolution(v) for v in resolutions) if value]
        clamped = _clamp_image_resolution(
            normalized.get("resolution") if not resolutions else (
                normalized.get("resolution")
                if _image_ui_resolution(normalized.get("resolution")) in resolutions
                else (selected.get("defaultResolution") or resolutions[0])
            ),
            account.get("plan"),
        )
        if resolutions and clamped not in resolutions:
            clamped = resolutions[0]
        normalized["resolution"] = clamped
        if str(normalized.get("quality") or "").strip():
            normalized["quality"] = ""
    return normalized, normalized != original_settings


def _normalize_image_request_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the image quick settings into the worker's canonical fields."""
    normalized = dict(settings)
    if normalized.get("imageModel"):
        normalized["model"] = normalized["imageModel"]
    if normalized.get("imageRatio"):
        normalized["ratio"] = normalized["imageRatio"]
    # The image quick-settings dialog keeps its own resolution field.
    if normalized.get("imageResolution"):
        normalized["resolution"] = normalized["imageResolution"]
    if normalized.get("imageCount") is not None:
        try:
            normalized["count"] = max(1, min(4, int(normalized["imageCount"])))
        except (TypeError, ValueError):
            pass
    normalized["format"] = str(normalized.get("format") or "jpg").strip().lower() or "jpg"
    return normalized


def _mode_tab_icon(kind: str) -> str:
    """Material symbols used by Flow's language-independent mode tabs."""
    return "image" if kind == "image" else "videocam"


def _flow_project_field(kind: str) -> str:
    """Return the account field that owns the media project for a job kind."""
    return "videoProjectId" if str(kind or "image") == "video" else "imageProjectId"


def _flow_project_id(account: dict[str, Any], kind: str, *, legacy_for_video: bool = False) -> str:
    """Read a kind-specific project, with legacy account compatibility."""
    field = _flow_project_field(kind)
    specific = str(account.get(field) or "").strip()
    if specific:
        return specific
    legacy = str(account.get("projectId") or "").strip()
    if str(kind or "image") != "video" or legacy_for_video:
        return legacy
    return ""


def _flow_project_changed_at(account: dict[str, Any], kind: str) -> float:
    """Read the migration timestamp for one media project only."""
    specific = account.get(f"{_flow_project_field(kind)}ChangedAt")
    try:
        if specific:
            return float(specific)
    except (TypeError, ValueError):
        pass
    if str(kind or "image") == "image":
        try:
            return float(account.get("projectChangedAt") or 0)
        except (TypeError, ValueError):
            pass
    return 0.0


# Flow changed the generation settings controls from ``role=tab`` to
# ``role=radio``.  Keep both selectors so existing and current builds work.
_FLOW_CONTROL_SELECTOR = '[role="tab"], [role="radio"], [role="option"], button'
_DURATION_UNITS = r"(?:s|sec(?:ond)?s?|giây)"


def _duration_pattern(duration: str) -> re.Pattern[str]:
    """Match Flow duration labels in English and Vietnamese."""
    return re.compile(
        rf"(?<!\d){re.escape(str(duration).strip())}\s*{_DURATION_UNITS}(?!\w)",
        re.IGNORECASE,
    )


def _flow_control_selected_from_attrs(
    *,
    aria_selected: str | None,
    aria_checked: str | None,
    data_state: str | None,
    aria_pressed: str | None = None,
) -> bool:
    """Return whether a Flow tab/radio reports the selected state."""
    state = str(data_state or "").strip().lower()
    return (
        str(aria_selected or "").strip().lower() == "true"
        or str(aria_checked or "").strip().lower() == "true"
        or str(aria_pressed or "").strip().lower() == "true"
        or state in {"checked", "on", "active", "selected"}
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
        attrs_selected = _flow_control_selected_from_attrs(
            aria_selected=await control.get_attribute("aria-selected"),
            aria_checked=await control.get_attribute("aria-checked"),
            data_state=await control.get_attribute("data-state"),
            aria_pressed=await control.get_attribute("aria-pressed"),
        )
        if attrs_selected:
            return True
        aria_current = str(await control.get_attribute("aria-current") or "").lower()
        css_class = str(await control.get_attribute("class") or "").lower()
        if aria_current in {"true", "page", "step"} or bool(
            re.search(r"(?:^|[ _-])(selected|active|checked)(?:$|[ _-])", css_class)
        ):
            return True
        # Angular Material/Flow builds sometimes expose the state only on a
        # nested input or through the host's checked pseudo-state.
        # mat-button-toggle puts mat-button-toggle-checked on the host element
        # while visible_duration_tab resolves to the inner button child, so we
        # must also walk ancestors via closest().
        return bool(await control.evaluate("""(node) => Boolean(
          node.matches(':checked, .mat-button-toggle-checked, [aria-selected="true"], [aria-checked="true"], [data-state="on"]')
          || node.querySelector(':checked, .mat-button-toggle-checked, [aria-selected="true"], [aria-checked="true"], [data-state="on"]')
          || node.closest('.mat-button-toggle-checked, [aria-selected="true"], [aria-checked="true"], [data-state="on"]')
        )"""))
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


async def _browser_at_login_wall(browser: Any) -> bool:
    """Require a browser redirect, not an API error, as login evidence."""
    try:
        page = await browser.page()
        url = urlsplit(str(page.url or ""))
        return url.hostname == "accounts.google.com" or (
            url.hostname in {"flow.google.com", "labs.google"}
            and any(part in {"about", "login", "signin"} for part in url.path.split("/"))
        )
    except Exception:
        return False


def _session_needs_login(error: Exception) -> bool:
    """Identify failures that require the visible Google re-login flow."""
    text = str(error)
    # API 401 without a login-wall is common when Bearer is missing but cookies
    # still work for Flow UI — do not treat as reconnect.
    if "avoid reconnect storm" in text.lower():
        return False
    if re.search(r"Could not obtain Bearer token", text, re.I) and "redirected" not in text.lower():
        return False
    return bool(re.search(
        r"LOGIN_REQUIRED|SESSION_EXPIRED|session.*expired|cookies.*expired|redirected to login|recaptcha|accounts\.google\.com|flow\.google\.com/about|/about|not signed in|unauthenticated|authentication required",
        text,
        re.I,
    ))


def _is_automation_block_error(error: Exception | str) -> bool:
    """Only Flow's unusual-activity response may trigger account reconnect."""
    return bool(re.search(
        r"FLOW_AUTOMATION_BLOCKED|abnormal activity|unusual activity|"
        r"suspicious activity|hoạt động bất thường|hoạt động đáng ngờ",
        str(error or ""),
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


async def _dismiss_flow_whats_new(page) -> None:
    """Acknowledge an optional announcement, never the generation button."""
    get_by_role = getattr(page, "get_by_role", None)
    if get_by_role is None:
        return
    buttons = get_by_role("button", name=re.compile(r"^(?:Bắt đầu|Get started|Start)$", re.I))
    try:
        button_count = buttons.count()
        if not isinstance(button_count, int):
            button_count = await button_count
        button_count = int(button_count)
    except Exception:
        return
    for index in range(button_count):
        button = buttons.nth(index)
        if await button.is_visible() and await button.is_enabled():
            await button.click(timeout=5_000)
            await button.wait_for(state="hidden", timeout=10_000)
            return


async def _disable_flow_agent_mode(page) -> None:
    """Return from an Agent session to the normal generation composer."""
    try:
        # Wait for hydration before looking for the session X. The normal
        # settings pill remains mounted but hidden while a session is open.
        await page.locator(
            'button.header-action[aria-label="Đóng"]:visible, '
            'button.header-action[aria-label="Close"]:visible, '
            'button.agent-mode-chip:visible, .settings-trigger-button:visible'
        ).first.wait_for(state="visible", timeout=15_000)
    except Exception:
        return
    await _dismiss_flow_whats_new(page)
    close = page.locator('button.header-action')
    for index in range(await close.count()):
        candidate = close.nth(index)
        label = await candidate.get_attribute("aria-label") or ""
        if await candidate.is_visible() and re.fullmatch(r"Đóng|Close", label, re.I):
            await candidate.click(timeout=5_000)
            await page.locator('.settings-trigger-button:visible').first.wait_for(
                state="visible", timeout=10_000,
            )
            break
    chip = page.locator("button.agent-mode-chip[aria-pressed='true']:visible")
    if await chip.count():
        await chip.first.click()
        await asyncio.sleep(0.8)


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

    @staticmethod
    def _public_account(account: dict[str, Any] | None) -> dict[str, Any]:
        """Return account metadata safe to send to the browser/API client."""
        public = dict(account or {})
        public.pop("password", None)
        public.pop("cookie", None)
        return public

    @staticmethod
    def _session_key(account_id: str, kind: str) -> str:
        return f"{account_id}:{kind if kind in {'image', 'video'} else 'image'}"

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
                if status == 401 or "accounts.google.com" in url or "/about" in url:
                    raise RuntimeError("FLOW_LOGIN_REQUIRED: periodic session refresh reached login")
                return True
            except Exception as exc:
                if _session_needs_login(exc) and _is_automation_block_error(exc):
                    self._log("warning", "session_refresh_expired", account_id=account_id,
                              message="Flow session refresh detected expired authentication")
                    should_reconnect = _is_automation_block_error(exc)
                    with self._guard:
                        if should_reconnect and account_id in self._session_refresh_reconnects:
                            should_reconnect = False
                        elif should_reconnect:
                            self._session_refresh_reconnects.add(account_id)
                    if should_reconnect and _is_automation_block_error(exc):
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

    @staticmethod
    def _session_profile_dir(account_id: str, kind: str) -> Path:
        """Use one persistent Chrome profile per account and media kind."""
        return store.root() / "profiles" / f"{account_id}-{kind}"

    def _ensure_session_profile(self, account_id: str, kind: str) -> Path:
        target = self._session_profile_dir(account_id, kind)
        base = Path(store.profile_dir(account_id))
        if base.exists():
            if target.exists():
                self._sync_runtime_cookies(account_id, target)
            # Login/reconnect writes fresh Google cookies to the canonical
            # account profile. Refresh each media profile before opening its
            # Chrome session so image/video jobs do not reuse stale cookies.
            shutil.copytree(
                base,
                target,
                ignore=_profile_copy_ignore,
                copy_function=_profile_copy_file,
                dirs_exist_ok=True,
            )
        return target

    def _session_for(self, account_id: str, kind: str) -> Any:
        """Read the new kind slot, accepting the pre-split image slot once."""
        key = self._session_key(account_id, kind)
        value = self._manual_sessions.get(key)
        if value is None and kind == "image":
            return self._manual_sessions.get(account_id)
        return value

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

    def _release_media_ids(self, media_ids: Any) -> None:
        """Release transient claims when an attempt is discarded for retry."""
        with self._guard:
            self._claimed_media_ids.difference_update(
                str(media_id) for media_id in (media_ids or []) if str(media_id)
            )

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
        return [self._public_account(row) for row in rows]

    def jobs(self) -> list[dict[str, Any]]:
        # Queue order is FIFO: the first prompt stays at the top and is the
        # first job resumed after an app restart.  History can still sort by
        # timestamp in the UI when a newest-first view is appropriate.
        # Hot path: never run legacy folder migration here — with hundreds of
        # jobs it rewrites jobs.json / hits disk and starves DELETE / cancel.
        rows: list[dict[str, Any]] = []
        for row in sorted(store.list_rows("jobs"), key=lambda item: item.get("createdAt", 0)):
            item = dict(row)
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

    async def _remove_flow_logos(self, job_id: str, outputs: list[str]) -> str:
        """Remove the Flow logo from downloaded media; return "" on success or the failure text.

        Files already cleaned (``logoDone``) are skipped, so a retry never inpaints twice.
        """
        store.patch_row("jobs", job_id, {"stage": "removing_logo", "outputs": outputs, "updatedAt": time.time()})
        try:
            import importlib
            logo_mod = await asyncio.to_thread(importlib.import_module, "pipeline.logo_remover")
            cfg = logo_mod.LogoRemovalSettings(
                enabled=True, keep_original=False, mode="auto", lama_model_path=logo_mod.DEFAULT_LAMA_MODEL)
        except Exception as exc:  # thiếu/cài lỗi runtime
            self._log("warning", "logo_remove_failed", job_id=job_id, message=str(exc))
            return str(exc) or exc.__class__.__name__
        done = [str(p) for p in (store.get_row("jobs", job_id) or {}).get("logoDone") or []]
        todo = [p for p in outputs if str(p) not in done]
        errors: list[str] = []

        def report(index: int, fraction: float) -> None:
            store.patch_row("jobs", job_id, {
                "progress": round(POST_PROGRESS_START + (POST_PROGRESS_END - POST_PROGRESS_START) * (index + fraction) / len(todo), 1),
                "updatedAt": time.time(),
            })

        if todo:
            report(0, 0)
        for index, out_path in enumerate(todo):
            try:
                if Path(out_path).suffix.lower() == ".mp4":
                    from pipeline.cleaner.cleaner_ffmpeg import _inpaint_video
                    target = Path(out_path)
                    temporary = target.with_name(target.stem + ".logo_tmp.mp4")
                    try:
                        await asyncio.to_thread(
                            _inpaint_video, out_path, str(temporary), job_id, flow_logo=True,
                            on_progress=lambda f, i=index: report(i, f),
                        )
                        self._check_cancel(job_id)
                        error = self._output_validation_error([str(temporary)])
                        if error:
                            raise RuntimeError(error)
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    mode_used = "static_video_alpha_recovery"
                else:
                    result = await asyncio.to_thread(logo_mod.remove_logo_from_file, out_path, cfg)
                    mode_used = result.mode_used
                self._log("info", "logo_removed", job_id=job_id, message=mode_used)
                done.append(str(out_path))
                store.patch_row("jobs", job_id, {"logoDone": done})
                report(index, 1)
            except Exception as exc:
                self._log("warning", "logo_remove_failed", job_id=job_id, message=str(exc))
                errors.append(str(exc) or exc.__class__.__name__)
        return errors[0] if errors else ""

    def _fail_logo_removal(self, job_id: str, outputs: list[str], error: str, *, account_id: str = "") -> None:
        """Downloaded files stay on the job; Retry re-runs only the logo step (see retry())."""
        current = store.get_row("jobs", job_id) or {}
        if current.get("status") == "cancelled" or job_id in self._cancelled:
            return
        store.patch_row("jobs", job_id, {
            "status": "failed", "stage": "failed", "progress": 100, "outputs": outputs,
            "error": f"FLOW_LOGO_REMOVE_FAILED: {error[:300]}", "updatedAt": time.time(),
        })
        self._log("error", "job_logo_failed", job_id=job_id, account_id=account_id, message=error)

    async def _logo_only_retry(self, job_id: str) -> None:
        job = store.get_row("jobs", job_id) or {}
        outputs = [str(p) for p in job.get("outputs") or []]
        error = await self._remove_flow_logos(job_id, outputs)
        if error:
            self._fail_logo_removal(job_id, outputs, error, account_id=str(job.get("accountId") or ""))
            return
        store.patch_row("jobs", job_id, {"status": "done", "stage": "done", "progress": 100, "error": None, "updatedAt": time.time()})
        if job.get("seriesContext"):
            from . import series
            series.mark_job_complete(job, outputs)
        self._log("success", "job_completed", job_id=job_id, account_id=str(job.get("accountId") or ""), details={"outputCount": len(outputs)})

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

    def _outputs_ready_for_completion(self, job: dict[str, Any], *, account_plan: str = "") -> bool:
        """Return true when downloaded files are safe to expose as a completed job.

        A stale worker may finish after a retry has changed the row token.  The
        files and logo markers are the durable result in that case; do not put
        an already-complete job back into the queue.
        """
        outputs = [str(value) for value in (job.get("outputs") or []) if str(value).strip()]
        if not self._outputs_exist(outputs):
            return False
        settings = job.get("settings") or {}
        if not (settings.get("removeLogo") and account_plan != "Ultra"):
            return True
        done = {str(value) for value in (job.get("logoDone") or [])}
        return bool(outputs) and all(path in done for path in outputs)

    def _finalize_existing_outputs(self, job_id: str, job: dict[str, Any], *, account_id: str = "", account_plan: str = "") -> None:
        if not self._outputs_ready_for_completion(job, account_plan=account_plan):
            return
        outputs = list(job.get("outputs") or [])
        store.patch_row("jobs", job_id, {
            "status": "done", "stage": "done", "progress": 100,
            "outputs": outputs, "error": None, "updatedAt": time.time(),
        })
        if job.get("seriesContext"):
            from . import series
            series.mark_job_complete(job, outputs)
        self._log("success", "job_completed", job_id=job_id, account_id=account_id,
                  details={"outputCount": len(outputs), "recovered": True})

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

    @staticmethod
    def _image_ratio_validation_error(outputs: Any, ratio: str) -> str | None:
        """Reject a Flow image whose downloaded dimensions do not match its job."""
        expected = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4}.get(str(ratio or "").strip())
        if expected is None:
            return None
        try:
            from pipeline.core.runtime_site import ensure_cv2
            import numpy as np
            cv2 = ensure_cv2()
            for value in outputs or []:
                path = Path(str(value))
                image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                if image is None or image.shape[1] <= 0 or image.shape[0] <= 0:
                    return f"FLOW_IMAGE_RATIO_INVALID: {path}"
                actual = image.shape[1] / image.shape[0]
                if abs(actual - expected) / expected > 0.04:
                    return f"FLOW_IMAGE_RATIO_MISMATCH: expected {ratio}, got {image.shape[1]}x{image.shape[0]}"
        except Exception as exc:
            return f"FLOW_IMAGE_RATIO_CHECK_FAILED: {exc}"
        return None

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

    def suspend_account(
        self, account_id: str, reason: str, duration_seconds: float | None = None
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

    def reset_account_session(self, account_id: str) -> dict[str, Any]:
        """Reset Flow project/session identity while preserving Google cookies."""
        account = store.get_row("accounts", account_id)
        if not account:
            return {}
        if any(job.get("accountId") == account_id and job.get("status") == "processing" for job in self.jobs()):
            raise RuntimeError("Account still has active Flow jobs")
        self._close_auto_sessions(account_id)
        with self._guard:
            if any(self._session_key(account_id, kind) in self._manual_sessions for kind in ("image", "video")) or account_id in self._connecting_accounts or account_id in self._active_browsers:
                raise RuntimeError("FLOW_PROFILE_BUSY")
        if any(job.get("accountId") == account_id and job.get("status") == "processing" for job in self.jobs()):
            raise RuntimeError("Account still has active Flow jobs")
        # Reset means a clean browser profile. Preserve only the account's
        # cookie payload, exactly like adding an account by cookie; local
        # Chrome storage, Flow project data, tokens and cached session state
        # must not survive this operation.
        cookie_payload = account.get("cookie")
        profile = Path(store.profile_dir(account_id))
        shutil.rmtree(profile, ignore_errors=True)
        profile.mkdir(parents=True, exist_ok=True)
        patch = {
            "status": "connecting", "error": None, "suspendedUntil": None, "suspendReason": None,
            "accessToken": None, "refreshToken": None, "cookie": cookie_payload,
            "projectId": None, "imageProjectId": None, "videoProjectId": None,
            "projectChangedAt": None, "imageProjectIdChangedAt": None, "videoProjectIdChangedAt": None,
            "updatedAt": time.time(),
        }
        store.patch_row("accounts", account_id, patch)
        self._log("info", "account_session_reset", account_id=account_id,
                  message="Đã xoá trắng profile Flow và giữ cookie Google / Reset Flow profile wiped while preserving cookie payload")
        # Kick-start login immediately so the account does not stay stuck in
        # 'connecting' forever waiting for the user to press Connect manually.
        try:
            self.connect(account_id)
        except RuntimeError:
            pass  # ponytail: FLOW_PROFILE_BUSY race is safe to ignore here
        return self._public_account(store.get_row("accounts", account_id) or account)

    def delete_account(self, account_id: str) -> bool:
        if any(self._session_key(account_id, kind) in self._manual_sessions for kind in ("image", "video")):
            raise RuntimeError("FLOW_PROFILE_BUSY")
        if any(job.get("accountId") == account_id and job.get("status") not in _TERMINAL for job in self.jobs()):
            raise RuntimeError("Account still has active Flow jobs")
        removed = store.delete_row("accounts", account_id)
        if removed:
            with self._guard:
                self._login_passwords.pop(account_id, None)
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
        ids = {str(job['id']) for job in store.list_rows('jobs') if job.get('status') not in _TERMINAL}
        with self._account_condition:
            self._cancelled.update(ids)
            self._account_condition.notify_all()
        threading.Thread(target=self._kill_job_browsers, args=(ids,), daemon=True, name="flow-bulk-kill").start()
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
        self._stop_enqueue = True  # Prevent enqueue from creating more jobs mid-loop
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
        kind = kind if kind in {"image", "video"} else "image"
        cookie_auth = str(account.get("authMethod") or "chrome") == "cookie"
        session_key = self._session_key(account_id, kind)
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
            if session_key in self._manual_sessions:
                if auto_close:
                    self._auto_close_sessions.add(session_key)
                return {"opened": True}
            self._manual_sessions[session_key] = None
            if auto_close:
                self._auto_close_sessions.add(session_key)

        def run_session() -> None:
            browser = None
            try:
                from .browser import BrowserManager, FLOW_BASE_URL, free_local_port
                cdp_port = free_local_port()
                session_profile = self._ensure_session_profile(account_id, kind)
                browser = BrowserManager(
                    headless=headless,
                    profile_dir=session_profile,
                    google_login_compat=True,
                    fullscreen=fullscreen,
                    page_zoom=page_zoom,
                    remote_debugging_port=cdp_port,
                    restore_last_session=cookie_auth,
                )
                async def open_and_wait() -> None:
                    await browser.start()
                    with self._guard:
                        self._manual_sessions[session_key] = {
                            "browser": browser,
                            "cdp_url": browser.cdp_url,
                            "headless": bool(headless),
                            "startedAt": time.monotonic(),
                        }
                    page = await browser.page()
                    project_id = str(account.get("projectId") or "")
                    destination = FLOW_BASE_URL
                    if project_id:
                        destination += f"/project/{quote(project_id, safe='')}"
                    if cookie_auth:
                        # Cookie accounts already carry the Google session in
                        # the copied profile. Going through ServiceLogin here
                        # needlessly prompts for a password and defeats the
                        # cookie import path.
                        url = destination
                    else:
                        url = ("https://accounts.google.com/ServiceLogin?service=wise"
                               f"&continue={quote(destination, safe='')}"
                               f"&login_hint={quote(str(account.get('email') or ''), safe='')}")
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    last_cookie_sync = 0.0
                    while not page.is_closed() and browser.context.pages:
                        if cookie_auth and time.monotonic() - last_cookie_sync >= 60:
                            last_cookie_sync = time.monotonic()
                            try:
                                jar = [c for c in await browser.context.cookies() if "google" in str(c.get("domain") or "")]
                                if jar:
                                    store.patch_row("accounts", account_id, {"cookie": jar})
                            except Exception:
                                pass  # keep previously stored cookies
                        await asyncio.sleep(1)
                async def run_browser() -> None:
                    try:
                        await open_and_wait()
                    finally:
                        if browser is not None:
                            try:
                                await asyncio.wait_for(browser.stop(), timeout=8.0)
                            except Exception:
                                pass
                            # Manual image/video sessions use an account-kind
                            # profile. Persist refreshed Google cookies back to
                            # the canonical account profile before the next
                            # connect or generation reuses it.
                            try:
                                await asyncio.to_thread(
                                    self._sync_runtime_cookies,
                                    account_id,
                                    session_profile,
                                )
                            except Exception:
                                self._log(
                                    "warning",
                                    "manual_session_cookie_sync_failed",
                                    account_id=account_id,
                                    message="Could not synchronize refreshed browser cookies",
                                )
                asyncio.run(run_browser())
            except Exception as exc:
                _log.warning("manual Flow session failed for %s: %s", account_id, exc)
            finally:
                with self._guard:
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
    ) -> dict[str, Any]:
        account = store.get_row("accounts", account_id)
        if not account:
            raise KeyError(account_id)
        auto_browser = None
        with self._guard:
            busy_keys = [self._session_key(account_id, kind) for kind in ("image", "video")]
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
            store.patch_row("accounts", account_id, {"status": "connecting", "error": None, "updatedAt": time.time()})
            if cookies:
                # Keep the imported cookies so reset/reconnect can reuse them
                # exactly like the original add-by-cookie (never sent to clients).
                store.patch_row("accounts", account_id, {"cookie": cookies})
        if auto_browser is not None:
            try:
                auto_browser.kill_now()
            except Exception:
                pass
        self._log("info", "account_connecting", account_id=account_id)
        threading.Thread(
            target=lambda: asyncio.run(self._login(account_id, force_interactive=force_interactive, cookies=cookies, headless=headless)),
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
        if interactive is not True and not _is_automation_block_error(reason):
            return
        account = store.get_row("accounts", account_id) or {}
        if not account:
            return
        # A successful login followed by another worker auth failure needs
        # explicit user retry; reopening login cannot repair that same session.
        job = (store.get_row("jobs", job_id) or {}) if job_id else {}
        if job.get("authRetryCount", 0):
            return
        with self._guard:
            if account_id in self._connecting_accounts:
                return
        # Mark reconnect so admission blocks other workers on this account.
        if account.get("status") == "online":
            store.patch_row("accounts", account_id, {
                "status": "reconnect",
                "error": reason or account.get("error") or "LOGIN_REQUIRED",
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

    def _account_session_blocked(self, account_id: str) -> bool:
        """True while a shared reconnect/login is in progress for this account."""
        if account_id in self._connecting_accounts:
            return True
        account = store.get_row("accounts", account_id) or {}
        # A failed login leaves the persisted status as ``reconnect`` after
        # _connecting_accounts is cleared. Do not leave queued jobs waiting
        # forever; their admission check will start one fresh reconnect.
        return account.get("status") == "connecting"

    def _account_quota_suspended(self, account_id: str) -> bool:
        """Return true while Flow has told us this account reached its quota."""
        account = store.get_row("accounts", account_id) or {}
        suspended_until = account.get("suspendedUntil")
        try:
            return bool(suspended_until and float(suspended_until) > time.time()) and bool(
                re.search(r"FLOW_QUOTA_EXHAUSTED|quota|hạn mức|usage limit|generation limit|daily limit|monthly limit", str(account.get("suspendReason") or ""), re.I)
            )
        except (TypeError, ValueError):
            return False

    def _account_suspended(self, account_id: str) -> bool:
        """Return true while any account-level Flow cooldown is active."""
        account = store.get_row("accounts", account_id) or {}
        # Credits are a video-only constraint. Keep the account usable for
        # image generation while the video credit cooldown is active.
        reason = str(account.get("suspendReason") or "")
        if re.search(r"FLOW_CREDITS_EMPTY|CREDITS_EMPTY|out of credits|hết tín|insufficient credit", reason, re.I):
            return False
        # A visible Playwright session is the user's own verified Flow session.
        # Reuse it for automation cooldowns (the cooldown was caused by the
        # isolated browser path), while retaining quota suspensions.
        has_manual = any(self._session_key(account_id, kind) in self._manual_sessions for kind in ("image", "video"))
        has_user_session = any(
            self._session_key(account_id, kind) in self._manual_sessions
            and self._session_key(account_id, kind) not in self._auto_close_sessions
            for kind in ("image", "video")
        )
        if has_manual and has_user_session:
            reason = str(account.get("suspendReason") or "")
            if re.search(r"FLOW_AUTOMATION_BLOCKED|FLOW_GENERATION_TIMEOUT", reason, re.I):
                return False
        try:
            return bool(account.get("suspendedUntil") and float(account["suspendedUntil"]) > time.time())
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _account_suspension_error(account: dict[str, Any]) -> str:
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
            if not _session_needs_login(Exception(str(job.get("error") or ""))):
                continue
            job_id = str(job.get("id") or "")
            if not job_id:
                continue
            with self._account_condition:
                if job_id in self._running_jobs or job_id in self._cancelled or job.get("authRetryCount", 0):
                    continue
                store.patch_row("jobs", job_id, {
                    "authRetryCount": 1,
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0,
                    "error": None,
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
            if any(self._session_key(account_id, kind) in self._manual_sessions for kind in ("image", "video")):
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
        from .browser import BrowserManager
        browser = BrowserManager(
            headless=True,
            profile_dir=store.profile_dir(account_id),
            restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
        )
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
                    )
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
                if str(account.get("authMethod") or "chrome") != "cookie":
                    store.patch_row("accounts", account_id, {
                        "status": "reconnect",
                        "error": "FLOW_LOGIN_VERIFICATION_REQUIRED",
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
            acc_id = str(acc.get("id") or "")
            if not acc_id or acc_id in exclude_set:
                continue
            if acc.get("status") != "online" or not _flow_project_id(acc, kind, legacy_for_video=True):
                continue
            suspended_until = acc.get("suspendedUntil")
            if suspended_until:
                try:
                    if float(suspended_until) > time.time():
                        continue
                except (ValueError, TypeError):
                    pass
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

    def _try_fallback_account(self, job_id: str, failed_account_id: str, reason: str) -> bool:
        """Attempt to fallback a failed/blocked job to another eligible online account."""
        # Completed media belongs to its original account/project. Switching
        # accounts here would regenerate it rather than retry the download.
        existing = store.get_row("jobs", job_id) or {}
        if existing.get("mediaIds") or existing.get("outputs"):
            return False
        # Only suspend for errors that are genuinely account-level faults.
        # GENERATION_REJECTED / unknown reasons already return early in suspend_account.
        self.suspend_account(failed_account_id, reason)

        job = store.get_row("jobs", job_id)
        if not job or job.get("status") in {"done", "cancelled"} or job_id in self._cancelled:
            return False
        if not job.get("allowAccountFallback", bool(job.get("randomAccount"))):
            return False

        kind = str(job.get("kind") or "image")
        settings = dict(job.get("settings") or {})
        model = settings.get("model")
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
            raise ValueError("FLOW_PLAN_SYNC_REQUIRED: Flow account was not found")
        if account.get("status") != "online" or not _flow_project_id(account, kind, legacy_for_video=True):
            # Only an explicit unusual-activity response owns the automatic
            # reconnect lifecycle. Other account/session errors stay scoped to
            # this job and must not take an otherwise-online account offline.
            reason = str(account.get("error") or "FLOW_LOGIN_REQUIRED")
            if _is_automation_block_error(reason):
                self._ensure_shared_reconnect(account_id, reason=reason)
            raise ValueError(
                f"FLOW_LOGIN_REQUIRED: {reason}"
            )
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



    async def _login(self, account_id: str, *, force_interactive: bool = False, cookies: list[dict[str, Any]] | None = None, headless: bool = False) -> None:
        """Open a visible Chrome window to authenticate and sync the Flow account.

        Authenticate at Flow home without selecting or creating a project.
        Preserve saved project IDs for the generation workers.
        """
        if headless:
            force_interactive = False  # automatic reconnect with 'open Chrome' off: never show a window
        account = store.get_row("accounts", account_id) or {}
        existing_project_id = str(account.get("projectId") or "")
        cookie_auth = bool(cookies) or str(account.get("authMethod") or "chrome") == "cookie"
        fresh_cookies = bool(cookies)
        if cookies:
            cookies = _account_cookie_records(cookies)  # persistent expiry, see _account_cookie_records
        if cookie_auth and not cookies:
            cookies = _account_cookie_records(account.get("cookie"))

        browser = None
        login_ok = False
        switch_to_login = False
        with self._guard:
            self._login_passwords.pop(account_id, None)
        password = ""
        headless_login = cookie_auth or headless
        try:
            from .browser import BrowserManager, FLOW_BASE_URL
            browser = BrowserManager(
                headless=headless_login,
                profile_dir=store.profile_dir(account_id),
                google_login_compat=True,
                fullscreen=True,
                page_zoom=1.0,
            )
            # Linux without a display normalizes visible requests to headless.
            headless_login = browser.headless
            await browser.start()
            if cookies:
                try:
                    await browser.context.add_cookies(cookies)
                except Exception:
                    # Playwright validation can include cookie values in its error.
                    raise RuntimeError("FLOW_COOKIE_IMPORT_FAILED") from None
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
                    if urlsplit(url).hostname not in {"flow.google.com", "labs.google"} or "/about" in url:
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

            # Điền email vào ô input và click Next tự động
            try:
                if not cookie_auth and urlsplit(str(page.url or "")).hostname == "accounts.google.com":
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
                if headless_login and not is_auth and (
                    urlsplit(str(page.url or "")).hostname == "accounts.google.com"
                    or "/about" in str(page.url or "")
                ):
                    raise RuntimeError("FLOW_LOGIN_VERIFICATION_REQUIRED")
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

            # Verify session, capture Bearer (jobs need ya29; credits alone are not enough),
            # then close Chrome before cloning the profile for queued jobs.
            from ._flow._api import FlowAPI
            _log.info("_login: fetching credits for project=%s", project_id)
            login_api = FlowAPI(browser, project_id=existing_project_id)
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
            if email and not str(stored_row.get("email") or "").strip():
                # Cookie accounts may be added without an email; adopt the one Flow reports.
                patch["label"] = str(email).split("@", 1)[0].strip() or stored_row.get("label")
            if access_token and str(access_token).startswith("ya29."):
                patch["accessToken"] = access_token
                patch["accessTokenAt"] = now
            if detected_plan:
                patch["plan"] = detected_plan
            if cookie_auth:
                try:
                    jar = [c for c in await browser.context.cookies() if "google" in str(c.get("domain") or "")]
                    if jar:
                        patch["cookie"] = jar
                except Exception:
                    pass  # keep the previously stored cookies
            store.patch_row("accounts", account_id, patch)
            self._log(
                "success",
                "account_connected",
                account_id=account_id,
                details={
                    "projectId": project_id,
                    "credits": credits,
                    "plan": detected_plan,
                    "bearer": bool(access_token),
                },
            )
            login_ok = True
        except Exception as exc:
            login_ok = False
            detail = str(exc).replace(password, "[redacted]") if password else str(exc)
            login_wall = await _browser_at_login_wall(browser)
            cookie_expired = bool(cookie_auth and login_wall)
            # Expired cookies cannot sign in again and only the user can fix it: switch to Chrome
            # login and open the visible window below (even for an automatic reconnect).
            switch_to_login = cookie_expired
            store.patch_row("accounts", account_id, {
                "status": "reconnect",
                "error": "FLOW_COOKIE_EXPIRED" if cookie_expired else detail,
                **({"authMethod": "chrome"} if switch_to_login else {}),
                "updatedAt": time.time(),
            })
            self._log("error", "account_connect_failed", account_id=account_id, message=detail)
        finally:
            password = ""
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
            self._wake_account_waiters(account_id)
            # Clone only after Chrome released the profile — otherwise cookies are incomplete
            # and every job raises "Could not obtain Bearer token" in a loop.
            if login_ok:
                self._requeue_auth_failed_jobs(account_id)
        if switch_to_login:
            self._log("warning", "cookie_expired_switch_to_login", account_id=account_id)
            with self._guard:
                self._connecting_accounts.add(account_id)
            store.patch_row("accounts", account_id, {"status": "connecting", "error": None, "updatedAt": time.time()})
            await self._login(account_id, force_interactive=True, headless=False)


    async def _create_flow_project_ui(self, page, *, avoid_project_id: str = "") -> str:
        """Create a brand-new Flow project via the lobby UI. Returns the new project id or \"\"."""
        from .browser import FLOW_BASE_URL
        await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
        await asyncio.sleep(2)
        match = _PROJECT_RE.search(str(page.url or ""))
        if match and match.group(1) != avoid_project_id:
            return match.group(1)

        candidates = [
            page.get_by_role("button", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I)),
            page.get_by_role("link", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I)),
            page.get_by_text(re.compile(r"^\s*(new project|create project|tạo dự án|dự án mới)\s*$", re.I)),
            page.locator(
                '[aria-label*="new project" i], [aria-label*="create project" i], '
                '[aria-label*="dự án mới" i], [aria-label*="tạo dự án" i]'
            ),
            page.locator(
                'button:has-text("New project"), a:has-text("New project"), '
                'button:has-text("Create project"), a:has-text("Create project"), '
                'button:has-text("New Project"), button:has-text("Dự án mới"), '
                'button:has-text("Tạo dự án")'
            ),
            page.locator('[data-testid*="create-project" i], [data-testid*="new-project" i]'),
        ]
        clicked = False
        for locator in candidates:
            try:
                target = locator.first
                if await target.count() == 0:
                    continue
                if not await target.is_visible():
                    continue
                await target.click(force=True)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            return ""

        def _is_replacement(url: str) -> bool:
            found = _PROJECT_RE.search(url or "")
            return bool(found and found.group(1) != avoid_project_id)

        try:
            await page.wait_for_url(_is_replacement, timeout=15_000)
        except Exception:
            create = page.get_by_role(
                "button", name=re.compile(r"^create$|^tạo$|^tạo dự án$|^done$|^xong$|^continue$|^tiếp$", re.I),
            ).first
            if await create.count():
                await create.click(force=True)
                try:
                    await page.wait_for_url(_is_replacement, timeout=15_000)
                except Exception:
                    return ""
            else:
                return ""
        match = _PROJECT_RE.search(str(page.url or ""))
        if match and match.group(1) != avoid_project_id:
            return match.group(1)
        return ""

    @staticmethod
    def _bind_flow_project(api, client, project_id: str) -> None:
        """Point API + client at a replacement project without rebuilding the browser."""
        api.project_id = project_id
        api._project_page_url = f"https://flow.google.com/project/{project_id}" if project_id else ""
        client.project_id = project_id
        client._project_url = f"https://flow.google.com/project/{project_id}"

    async def _ensure_account_project_page(
        self,
        page,
        *,
        api,
        client,
        account: dict[str, Any],
        job_id: str = "",
        kind: str = "image",
    ) -> dict[str, Any]:
        """Open the kind-specific account project; repair it if deleted."""
        current_job = store.get_row("jobs", job_id) if job_id else None
        scoped = bool(current_job)
        settings = (current_job or {}).get("settings") or {}
        project_field = (
            "laneProject_" + safe_output_part(kind) + "_"
            + safe_output_part(settings.get("ratio") or "16:9") + "_"
            + safe_output_part(settings.get("resolution") or "default")
            if scoped else _flow_project_field(kind)
        )
        target_id = str((current_job or {}).get("submissionProjectId") or account.get(project_field) or "") if scoped else _flow_project_id(account, kind)
        if scoped and not target_id:
            account_id = str(account.get("id") or "")
            with self._guard:
                lane_lock = self._project_recovery_locks.setdefault(account_id + ":" + project_field, threading.Lock())
            while not lane_lock.acquire(blocking=False):
                await asyncio.sleep(0.1)
            try:
                persisted = store.get_row("accounts", account_id) or {}
                target_id = str(persisted.get(project_field) or "")
                if not target_id:
                    target_id = await self._create_flow_project_ui(page)
                    if not target_id:
                        raise RuntimeError("FLOW_PROJECT_NOT_FOUND: Could not create scoped project")
                    store.patch_row("accounts", account_id, {project_field: target_id})
                account = {**account, project_field: target_id}
            finally:
                lane_lock.release()
        legacy_id = str(account.get("projectId") or "").strip()
        if not scoped and kind == "image" and not account.get(project_field) and legacy_id:
            store.patch_row("accounts", str(account.get("id") or ""), {project_field: legacy_id})
            account = {**account, project_field: legacy_id}
            target_id = legacy_id
        elif not scoped and kind == "video" and not target_id:
            # Existing accounts have one legacy project. Preserve it for images
            # and create a dedicated gallery for video on first use.
            new_id = await self._create_flow_project_ui(page, avoid_project_id=legacy_id)
            if not new_id:
                raise RuntimeError(
                    f"FLOW_PROJECT_NOT_FOUND: Could not create the {kind} project "
                    f"(legacy id: {legacy_id or 'none'}; current url: {page.url})"
                )
            changed_at = time.time()
            patch = {
                project_field: new_id,
                f"{project_field}ChangedAt": changed_at,
                "status": "online",
                "error": None,
                "updatedAt": time.time(),
            }
            if kind == "image":
                patch["projectChangedAt"] = changed_at
            store.patch_row("accounts", str(account.get("id") or ""), patch)
            account = {**account, **patch}
            target_id = new_id
        if target_id and str(client.project_id or "") != target_id:
            self._bind_flow_project(api, client, target_id)
        try:
            await client._ensure_project_page(page)
            # A deleted project first loads its own URL, then Angular redirects to
            # /404?reason=project a few seconds later — wait for either outcome.
            for _ in range(16):
                if "/404" in str(page.url or ""):
                    break
                if await page.locator("flow-grid-tile-container, [contenteditable='true'], textarea").count():
                    break
                await asyncio.sleep(0.5)
            if "/404" not in str(page.url or ""):
                await _dismiss_flow_whats_new(page)
                # Flow is a SPA: the project URL can be ready before the
                # composer finishes mounting. Give Angular one short frame
                # before filling prompt/settings; the UI helpers still poll
                # afterwards when a cold profile needs longer.
                await asyncio.sleep(0.3)
                return account
        except RuntimeError as exc:
            if "FLOW_PROJECT_NOT_FOUND" not in str(exc):
                raise
        old_id = str(target_id or client.project_id or "")
        account_id = str(account.get("id") or "")
        with self._guard:
            recovery_lock = self._project_recovery_locks.setdefault(account_id + ":" + project_field, threading.Lock())
        while not recovery_lock.acquire(blocking=False):
            await asyncio.sleep(0.1)
        try:
            # Another worker may have repaired this account while this worker
            # was waiting. Reuse its project instead of creating a second one.
            persisted = store.get_row("accounts", account_id) or {}
            persisted_id = str(persisted.get(project_field) or "") if scoped else _flow_project_id(persisted, kind)
            if persisted_id and persisted_id != old_id:
                self._bind_flow_project(api, client, persisted_id)
                await client._ensure_project_page(page)
                await _dismiss_flow_whats_new(page)
                await asyncio.sleep(0.3)
                return {**account, **persisted}
            self._log(
            "warning",
            "project_missing_creating_replacement",
            job_id=job_id,
            account_id=str(account.get("id") or ""),
            details={"oldProjectId": old_id, "url": str(page.url or "")},
            )
            new_id = await self._create_flow_project_ui(page, avoid_project_id=old_id)
            if not new_id:
                raise RuntimeError(
                    f"FLOW_PROJECT_NOT_FOUND: Could not create a replacement project "
                    f"(old id: {old_id or 'none'}; current url: {page.url})"
                )
            changed_at = time.time()
            patch: dict[str, Any] = {
                project_field: new_id,
                "previousProjectId": old_id,
                f"{project_field}ChangedAt": changed_at,
                "status": "online",
                "error": None,
                "updatedAt": time.time(),
            }
            if kind == "image" and not scoped:
                patch["projectId"] = new_id
                patch["projectChangedAt"] = changed_at
            store.patch_row("accounts", str(account["id"]), patch)
            self._bind_flow_project(api, client, new_id)
            self._log(
                "success",
                "project_replaced",
                job_id=job_id,
                account_id=str(account.get("id") or ""),
                details={"oldProjectId": old_id, "projectId": new_id},
            )
            await client._ensure_project_page(page)
            await _dismiss_flow_whats_new(page)
            await asyncio.sleep(0.3)
            return {**account, **patch}
        finally:
            recovery_lock.release()

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
        from .browser import BrowserManager, FLOW_BASE_URL
        browser = None
        try:
            browser = BrowserManager(
                headless=True,
                profile_dir=store.profile_dir(account_id),
                restore_last_session=str((store.get_row("accounts", account_id) or {}).get("authMethod") or "chrome") == "cookie",
            )
            await browser.start()
            account = store.get_row("accounts", account_id) or {}
            if str(account.get("authMethod") or "chrome") == "cookie":
                cookie_records = _account_cookie_records(account.get("cookie"))
                if cookie_records:
                    await browser.context.add_cookies(cookie_records)
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
                if "accounts.google.com" not in page.url and ("flow.google.com" in page.url or "labs.google" in page.url):
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



    def enqueue(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
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
            if self._account_suspended(account_id):
                if allow_fallback:
                    alt = self._pick_eligible_account(kind=kind, model=model, exclude_ids=[account_id])
                    if alt:
                        account_id = str(alt.get("id"))
                        account = alt
                    else:
                        raise ValueError(self._account_suspension_error(account))
                else:
                    raise ValueError(self._account_suspension_error(account))
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
        store.put_rows("jobs", pending)

        for job in pending:
            if series_context:
                from . import series
                series.register_job(job)
            self._log("info", "job_queued", job_id=job["id"], account_id=job["accountId"], details={"kind": job["kind"], "inputIndex": job["inputIndex"]})
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
        generation_token = str(job.get("generationToken") or "")
        order = int(job.get("queueOrder", job.get("inputIndex", 0)))
        with self._account_condition:
            def account_concurrency_limit() -> int:
                # Image and video share one account budget. Use the highest
                # requested Flow lane count across this account's queued or
                # active jobs instead of blocking one media kind at another
                # kind's smaller per-job setting.
                values = [
                    _job_concurrency(row.get("settings") or {})
                    for row in store.list_rows("jobs")
                    if str(row.get("accountId") or "") == account_id
                    and row.get("status") in {"queued", "processing"}
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
                or self._account_active.get(account_id, 0) >= account_concurrency_limit()
                or self._account_session_blocked(account_id)
                or self._account_suspended(account_id)
            ):
                if self._account_suspended(account_id):
                    account_snapshot = store.get_row("accounts", account_id) or {}
                    store.patch_row("jobs", job_id, {
                        "status": "failed",
                        "stage": "failed",
                        "error": self._account_suspension_error(account_snapshot),
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
        _HARD_ERROR = re.compile(
            r"LOGIN_REQUIRED|GENERATION_REJECTED|HTTP\s+401|HTTP\s+403|API auth rejected|FLOW_CREDITS_EMPTY|FLOW_CREDITS_INSUFFICIENT|FLOW_QUOTA_EXHAUSTED|FLOW_DOWNLOAD_QUALITY_UNAVAILABLE",
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
                    if _session_needs_login(verify_exc) and _is_automation_block_error(verify_exc):
                        store.patch_row("jobs", job_id, {
                            "status": "action_required",
                            "stage": "action_required",
                            "error": str(verify_exc),
                            "updatedAt": time.time(),
                        })
                        account_row = store.get_row("accounts", account_id) or {}
                        if account_row.get("status") not in {"connecting", "online"}:
                            store.patch_row("accounts", account_id, {
                                "status": "reconnect",
                                "error": str(verify_exc),
                                "updatedAt": time.time(),
                            })
                        self._ensure_shared_reconnect(account_id, job_id=job_id, reason=str(verify_exc))
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

            # Transient / reject → auto-retry up to _JOB_AUTO_RETRY_MAX.
            # Mandatory stops only: credits, quota, cancel, content policy.
            for _auto_i in range(_JOB_AUTO_RETRY_MAX):
                finished = store.get_row("jobs", job_id) or finished
                finished_error = str(finished.get("error") or "")
                rejected = "FLOW_GENERATION_REJECTED" in finished_error
                # Each job owns an isolated Chrome profile, so unusual activity
                # is retried in a fresh Chrome for this job only; siblings keep running.
                blocked = _is_automation_block_error(finished_error)
                raw_retry = finished.get("autoRetryCount")
                if raw_retry is None:
                    raw_retry = finished.get("generationRejectRetryCount")
                try:
                    retry_count = int(raw_retry or 0)
                except (TypeError, ValueError):
                    retry_count = 0
                should_auto_retry = (
                    finished.get("status") == "failed"
                    and (
                        not finished.get("submissionStartedAt")
                        # ponytail: lost results retry fresh (small duplicate-charge risk, user chose completion).
                        or blocked
                        or re.search(r"RESULT_NOT_FOUND|EMPTY_OUTPUT|GENERATION_TIMEOUT|FLOW_AUTOMATION_FAILED|ERR_ABORTED|has been closed|Target closed|Connection closed", finished_error, re.I)
                    )
                    and not finished.get("mediaIds")
                    and not finished.get("outputs")
                    and retry_count < _JOB_AUTO_RETRY_MAX
                    and (rejected or blocked or not _HARD_ERROR.search(finished_error))
                )
                if not should_auto_retry:
                    break
                next_count = retry_count + 1
                self._log(
                    "warning", "job_auto_retry", job_id=job_id, account_id=account_id,
                    message=finished_error,
                    details={"attempt": next_count, "max": _JOB_AUTO_RETRY_MAX, "freshChrome": True},
                )
                if job_id in self._cancelled or not store.get_row("jobs", job_id):
                    return
                patch: dict[str, Any] = {
                    "status": "processing",
                    "stage": "retrying",
                    "progress": 0,
                    # Keep the last Flow failure visible while the automatic
                    # retry is running; otherwise the card looks healthy even
                    # though Flow just rejected/timed out the attempt.
                    "error": finished_error,
                    "autoRetryCount": next_count,
                    "updatedAt": time.time(),
                }
                if rejected:
                    patch["generationRejectRetryCount"] = next_count
                if blocked or re.search(r"RESULT_NOT_FOUND|EMPTY_OUTPUT|GENERATION_TIMEOUT", finished_error, re.I):
                    self._release_media_ids(finished.get("mediaIds"))
                    patch.update({
                        "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "mediaIds": [], "resumeOnly": False,
                    })
                store.patch_row("jobs", job_id, patch)
                # Flow's unusual-activity gate needs a short cool-down before a new Chrome submits again.
                time.sleep(10 * next_count if blocked else 3)
                if job_id in self._cancelled or not store.get_row("jobs", job_id):
                    return
                store.patch_row("jobs", job_id, {
                    "status": "queued", "stage": "queued", "progress": 0,
                    "error": finished_error,
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
                    store.patch_row(
                        "jobs",
                        job_id,
                        {
                            "status": "action_required",
                            "stage": "action_required",
                            "error": str(exc),
                            "updatedAt": time.time(),
                        },
                    )
                    account_row = store.get_row("accounts", account_id) or {}
                    if account_row.get("status") not in {"connecting", "online"}:
                        store.patch_row("accounts", account_id, {
                            "status": "reconnect",
                            "error": str(exc),
                            "updatedAt": time.time(),
                        })
                    # A dead cookie needs an explicit user Connect. Only an
                    # actual unusual-activity response is eligible for the
                    # automatic shared reconnect cycle.
                    if _is_automation_block_error(exc):
                        self._ensure_shared_reconnect(account_id, job_id=job_id, reason=str(exc))
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
                if current_job.get("quotaModelFallbackApplied") and "FLOW_QUOTA_EXHAUSTED" in err_msg:
                    self.suspend_account(account_id, err_msg)
                elif current_job.get("allowAccountFallback", bool(current_job.get("randomAccount"))):
                    self._try_fallback_account(job_id, account_id, err_msg)
                elif re.search(
                    r"GENERATION_REJECTED|FLOW_CREDITS_EMPTY|FLOW_QUOTA_EXHAUSTED",
                    err_msg,
                    re.I,
                ):
                    self.suspend_account(account_id, err_msg)
            self._close_auto_session_if_idle(
                account_id,
                str(current_job.get("kind") or job.get("kind") or "video"),
            )

    def _clone_runtime_profile(self, account_id: str, job_id: str, kind: str = "image") -> Path | None:
        from .browser import profile_lock
        # Cookie databases can only be copied once Chrome releases this profile.
        with profile_lock(store.profile_dir(account_id)):
            self._check_cancel(job_id)
            return self._clone_closed_profile(account_id, job_id)

    def _sync_runtime_cookies(self, account_id: str, runtime_profile: Path) -> None:
        """Merge rotated cookies after Chrome closes, without exporting secrets."""
        from .browser import profile_lock
        # Chromium encrypts values using the cloned profile's existing key.
        # Wait for the account profile owner to close before writing its database.
        with profile_lock(store.profile_dir(account_id)):
            if not store.get_row("accounts", account_id):
                return
            profile = store.profile_dir(account_id)
            for relative in ("Default/Network/Cookies", "Default/Cookies"):
                source, target = runtime_profile / relative, profile / relative
                if not source.is_file() or not target.is_file():
                    continue
                try:
                    with sqlite3.connect(target, timeout=10) as db:
                        db.execute("ATTACH DATABASE ? AS refreshed", (str(source),))
                        columns = [row[1] for row in db.execute("PRAGMA main.table_info(cookies)")]
                        incoming = [row[1] for row in db.execute("PRAGMA refreshed.table_info(cookies)")]
                        if columns != incoming or "last_update_utc" not in columns:
                            raise ValueError("incompatible cookie database")
                        names = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
                        # Native unique keys include partition/source fields on
                        # newer Chrome. Let SQLite use that exact native key.
                        db.execute(f"INSERT INTO main.cookies ({names}) SELECT {names} FROM refreshed.cookies WHERE 1 "
                                   f"ON CONFLICT DO UPDATE SET " + ", ".join(
                                       f'"{name}" = excluded."{name}"' for name in columns
                                   ) + " WHERE excluded.last_update_utc > cookies.last_update_utc")
                except (sqlite3.Error, ValueError, OSError):
                    # Never include database rows or exception text (secrets).
                    self._log("warning", "cookie_sync_failed", account_id=account_id,
                              message="Could not synchronize refreshed browser cookies")

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
        await _disable_flow_agent_mode(page)

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
            # Current Flow exposes the mode controls as radios whose accessible
            # name is localized ("Hình ảnh"), while their inner text may only
            # contain an icon. Prefer the accessibility name before text.
            for role in ("radio", "tab"):
                named = page.get_by_role(role, name=mode_pattern).first
                if await named.count() > 0 and await named.is_visible():
                    return named

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

        async def _model_controls():
            candidates = page.locator('button, [role="button"]')
            found = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                text = (await candidate.inner_text()).strip()
                aria_label = await candidate.get_attribute("aria-label") or ""
                combined = f"{text} {aria_label}"
                if family.search(combined) or re.search(
                    r"chọn nhóm mô hình|select model|model group|model selector",
                    combined,
                    re.I,
                ):
                    found.append(candidate)
            return found

        async def _model_pill_already_visible() -> bool:
            return bool(await _model_controls())

        async def _model_pill_matches(target_model: str) -> bool:
            """True when the visible model pill already shows the desired model."""
            for candidate in await _model_controls():
                text = (await candidate.inner_text()).strip()
                aria_label = await candidate.get_attribute("aria-label") or ""
                if _match_model_choice(target_model, f"{text} {aria_label}"):
                    return True
            return False

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

        if not switched:
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
                raise RuntimeError(f"FLOW_UI_CHANGED: {kind} mode tab was not found")
            elif not await _flow_control_is_selected(mode_tab):
                await mode_tab.click(force=True)
                await asyncio.sleep(0.6)



        # Verify the requested mode is really active (switch_mode may report
        # success while the page is still on the other mode).
        for _mode_check in range(3):
            try:
                verify_tab = await _visible_mode_tab()
                if verify_tab is None or await _flow_control_is_selected(verify_tab):
                    break
                await verify_tab.click(force=True)
                await asyncio.sleep(0.8)
            except Exception:
                break

        # ------------------------------------------------------------------
        # Step 3: find and click model family selector, pick the right option
        # ------------------------------------------------------------------
        selector = None
        model_controls = await _model_controls()
        if model_controls:
            selector = model_controls[-1]
        if selector is None:
            raise RuntimeError(f"FLOW_UI_CHANGED: {kind} model selector was not found")
        # Always reopen and choose the requested option. Flow can keep a stale
        # settings summary after a previous prompt, so a matching label alone
        # is not proof that this job's settings were applied to its composer.
        choices = page.locator('[role="menuitem"], [role="option"]')
        selected = False
        visible_texts: list[str] = []
        for menu_attempt in range(3):
            await selector.click()
            await asyncio.sleep(0.5 + menu_attempt * 0.7)
            for index in range(await choices.count()):
                choice = choices.nth(index)
                text = (await choice.inner_text()).strip()
                if await choice.is_visible() and _match_model_choice(model, text):
                    # Flow's nested CDK overlays can intercept a coordinate click.
                    # Dispatch on the visible option, then wait for its menu to close.
                    await choice.dispatch_event("click", timeout=5_000)
                    await choice.wait_for(state="hidden", timeout=10_000)
                    selected = True
                    break
            if selected:
                break
            visible_texts = []
            for index in range(await choices.count()):
                choice = choices.nth(index)
                if await choice.is_visible():
                    visible_texts.append((await choice.inner_text()).strip())
            if visible_texts:
                break  # menu is open and the model really is absent
            await page.keyboard.press("Escape")  # menu never opened: close and retry
        if not selected:
            raise RuntimeError(f"FLOW_MODEL_UNAVAILABLE: {model} — available: {visible_texts}")
        if not await _model_pill_matches(model):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: model {model} was not selected")


    async def _prepare_ui_format(
        self,
        page,
        ratio: str,
        duration: str | None = None,
        resolution: str | None = None,
        duration_model: str = "",
    ) -> None:
        """Select current numeric Flow tabs and verify the requested format.

        Recent Flow builds label aspect tabs ``16:9``/``9:16``/``1:1`` rather
        than the older Landscape/Portrait/Square labels used by flow-py.
        """
        await _dismiss_flow_whats_new(page)
        async def visible_tab(label: str | re.Pattern[str]):
            if getattr(page, "is_closed", lambda: False)():
                raise RuntimeError("Target page, context or browser has been closed")
            pattern = label if hasattr(label, "search") else re.compile(re.escape(label))
            matches = page.locator(_FLOW_CONTROL_SELECTOR).filter(
                has_text=pattern
            )
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                try:
                    try:
                        visible = await candidate.is_visible(timeout=0)
                    except TypeError:
                        # Keep lightweight test doubles and older Playwright
                        # wrappers compatible with the zero-wait probe.
                        visible = await candidate.is_visible()
                    if visible:
                        return candidate
                except Exception:
                    pass
            return None

        async def visible_ratio_tab(label: str):
            """Find a ratio control across Flow's text and icon-based UIs.

            Some Flow builds render the square/portrait controls with only a
            Material icon (for example ``crop_1_1``) and expose the numeric
            ratio through ``aria-label`` or ``data-value``.  A text-only
            locator then waits for 30 seconds even though the control exists.
            """
            candidate = await visible_tab(label)
            if candidate is not None:
                return candidate
            icon = f"crop_{label.replace(':', '_')}"
            controls = page.locator(_FLOW_CONTROL_SELECTOR)
            for index in range(await controls.count()):
                control = controls.nth(index)
                try:
                    try:
                        visible = await control.is_visible(timeout=0)
                    except TypeError:
                        visible = await control.is_visible()
                    if not visible:
                        continue
                    values = [
                        await control.inner_text(),
                        await control.get_attribute("aria-label"),
                        await control.get_attribute("data-value"),
                        await control.get_attribute("title"),
                    ]
                    haystack = " ".join(str(value or "") for value in values)
                    if re.search(rf"(?<!\\d){re.escape(label)}(?!\\d)", haystack) or icon in haystack:
                        return control
                except Exception:
                    continue
            return None

        async def visible_duration_tab(label: re.Pattern[str]):
            # Flow has shipped duration controls as tabs, radios, and plain
            # Material buttons. Keep the broad fallback scoped to the exact
            # duration text so model/ratio controls cannot be selected by it.
            selectors = (
                _FLOW_CONTROL_SELECTOR,
                'button, [role="option"], [role="menuitem"], [data-value]',
            )
            candidates = []
            for selector in selectors:
                matches = page.locator(selector).filter(has_text=label)
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        try:
                            text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                            css_class = str(await candidate.get_attribute("class") or "").lower()
                            aria_label = str(await candidate.get_attribute("aria-label") or "").lower()
                            trigger_ancestor = bool(await candidate.evaluate("""node => Boolean(
                                node.closest('.settings-trigger-button, .settings-summary')
                            )"""))
                        except Exception:
                            text = ""
                            css_class = ""
                            aria_label = ""
                            trigger_ancestor = False
                        # The bottom-bar summary also contains the requested
                        # duration (e.g. ``Video · 720p · 6 giây``), but it is
                        # only the settings-panel trigger, not the 6s option.
                        # Never treat that summary as a duration control.
                        if (
                            "settings-trigger" in css_class
                            or "settings-summary" in css_class
                            or trigger_ancestor
                            or "điều kiện kích hoạt" in aria_label
                            or "settings" in aria_label and "duration" not in aria_label
                        ):
                            continue
                        candidates.append((len(text) or 10_000, candidate))
            # has_text also matches a parent group containing all four pills.
            # Prefer the smallest matching node so the click lands on the
            # actual 6s/8s/10s control rather than the group wrapper.
            if candidates:
                candidates.sort(key=lambda item: item[0])
                return candidates[0][1]
            return None

        async def duration_debug(label: re.Pattern[str]) -> list[dict[str, str]]:
            result: list[dict[str, str]] = []
            matches = page.locator(
                'button, [role="tab"], [role="radio"], [role="option"], [role="menuitem"], [data-value]'
            ).filter(has_text=label)
            for index in range(min(12, await matches.count())):
                candidate = matches.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    parent_info = await candidate.evaluate(
                        "node => { const p = node.parentElement; return p ? p.tagName + '|' + (p.className||'') : ''; }"
                    )
                    result.append({
                        "text": re.sub(r"\s+", " ", (await candidate.inner_text()).strip()),
                        "ariaSelected": str(await candidate.get_attribute("aria-selected") or ""),
                        "ariaChecked": str(await candidate.get_attribute("aria-checked") or ""),
                        "ariaPressed": str(await candidate.get_attribute("aria-pressed") or ""),
                        "class": str(await candidate.get_attribute("class") or ""),
                        "parentTag": str(parent_info or ""),
                    })
                except Exception:
                    continue
            return result

        async def summary_confirms_duration(value: str) -> bool:
            """Accept Flow's bottom-bar summary as the selected state.

            Some builds render duration controls as a closed overlay and expose
            only the committed value on the settings trigger. In that state
            there is no aria-checked node to inspect, but the summary is the
            value that will be submitted and must not be replaced by 8s.
            """
            pattern = _duration_pattern(value)
            summaries = page.locator(".settings-trigger-button, .settings-summary")
            for index in range(await summaries.count()):
                summary = summaries.nth(index)
                try:
                    if await summary.is_visible() and pattern.search(
                        re.sub(r"\s+", " ", (await summary.inner_text()).strip())
                    ):
                        return True
                except Exception:
                    continue
            return False

        async def summary_confirms_resolution(value: str) -> bool:
            """Accept Flow's committed resolution summary when controls close after selection."""
            summaries = page.locator(".settings-trigger-button, .settings-summary")
            for index in range(await summaries.count()):
                summary = summaries.nth(index)
                try:
                    if not await summary.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await summary.inner_text()).strip())
                    if re.search(rf"(?<!\d){re.escape(value)}(?!\d)", text, re.I):
                        return True
                    if value == "720p" and re.search(r"original size|kích thước gốc", text, re.I):
                        return True
                except Exception:
                    continue
            return False

        ratio_label = str(ratio or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{1,2}", ratio_label):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid aspect ratio {ratio_label!r}")
        ratio_tab = None
        for attempt in range(4):
            if getattr(page, "is_closed", lambda: False)():
                raise RuntimeError("Target page, context or browser has been closed")
            ratio_tab = await visible_ratio_tab(ratio_label)
            if ratio_tab is not None:
                break
            # Model selection can leave a transient menu open or close the
            # settings popover. Focus textarea (so pill opens settings not media picker),
            # then reopen via pill click.
            if attempt:
                keyboard = getattr(page, "keyboard", None)
                if keyboard is not None:
                    await keyboard.press("Escape")
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
            # 2. Fallback: aria-haspopup (menu / dialog / listbox)
            if trigger is None:
                for haspopup in ("menu", "dialog", "listbox", "true"):
                    pills = page.locator(f'button[aria-haspopup="{haspopup}"]')
                    for index in range(await pills.count() - 1, -1, -1):
                        candidate = pills.nth(index)
                        try:
                            if await candidate.is_visible(timeout=0):
                                trigger = candidate
                                break
                        except Exception:
                            pass
                    if trigger is not None:
                        break
            # 3. Fallback: settings-trigger-button class
            if trigger is None:
                stb = page.locator(".settings-trigger-button, .settings-summary")
                for index in range(await stb.count() - 1, -1, -1):
                    candidate = stb.nth(index)
                    try:
                        if await candidate.is_visible(timeout=0):
                            trigger = candidate
                            break
                    except Exception:
                        pass
            if trigger is not None:
                await trigger.click()
            await asyncio.sleep(1.2)
        if ratio_tab is None:
            raise RuntimeError(f"FLOW_UI_CHANGED: aspect ratio {ratio_label} was not found")
        scroll_into_view = getattr(ratio_tab, "scroll_into_view_if_needed", None)
        if scroll_into_view is not None:
            await scroll_into_view()
        click_ratio = getattr(ratio_tab, "click", None)
        if click_ratio is not None:
            await click_ratio(timeout=5_000)
            await asyncio.sleep(0.5)
        if not await _flow_control_is_selected(ratio_tab):
            # Second retry — UI may lag on slow machines
            await ratio_tab.click(timeout=5_000)
            await asyncio.sleep(0.8)
        if not await _flow_control_is_selected(ratio_tab):
            # mat-button-toggle ancestor fallback
            try:
                host = await ratio_tab.evaluate_handle(
                    "node => node.closest('mat-button-toggle, [role=\"radio\"], [role=\"tab\"]') || node.parentElement"
                )
                await host.as_element().click(force=True)  # type: ignore[union-attr]
                await asyncio.sleep(0.8)
            except Exception as _exc:
                _log.debug("ratio ancestor click failed: %s", _exc)
        if not await _flow_control_is_selected(ratio_tab):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: aspect ratio {ratio_label} was not selected")


        if resolution:
            resolution_value = _video_ui_resolution(resolution).lower()
            if not resolution_value:
                # Shared settings often keep image plan labels (1K/2K/4K). Veo has
                # no resolution tabs — ignore instead of FLOW_SETTING_MISMATCH.
                _log.info("_prepare_ui_format: ignoring non-video resolution %r", resolution)
            else:
                # Tab text may be "360pinfo" (trailing info icon) — only forbid digits.
                resolution_pattern = re.compile(rf"(?<!\d){re.escape(resolution_value)}(?!\d)", re.I)
                # Prefer the smallest visible control. A broad has_text match
                # can otherwise return the settings container containing both
                # 360p and 720p, leaving Flow on its 720p default.
                resolution_tab = None
                resolution_candidates = []
                # gflow-cli drives the committed Flow control: exact radios inside
                # the settings radiogroup, then verifies aria-checked. Prefer this
                # anchor over broad text matches that can hit the 720p summary.
                exact_radios = page.locator("[role='radiogroup'] [role='radio']").filter(has_text=resolution_pattern)
                for index in range(await exact_radios.count()):
                    candidate = exact_radios.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                        if re.fullmatch(rf"{re.escape(resolution_value)}(?:\s*info)?", text, re.I):
                            resolution_candidates.append((-1, len(text), candidate))
                    except Exception:
                        continue
                if not resolution_candidates:
                    for selector in ('mat-button-toggle', _FLOW_CONTROL_SELECTOR, 'button, [role="radio"], [role="tab"], [data-value]'):
                        matches = page.locator(selector).filter(has_text=resolution_pattern)
                        for index in range(await matches.count()):
                            candidate = matches.nth(index)
                            try:
                                if not await candidate.is_visible():
                                    continue
                                text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                                if selector == 'mat-button-toggle' and not re.fullmatch(rf"{re.escape(resolution_value)}(?:\s*info)?", text, re.I):
                                    continue
                                values = " ".join(filter(None, [
                                    text,
                                    await candidate.get_attribute("aria-label"),
                                    await candidate.get_attribute("data-value"),
                                    await candidate.get_attribute("title"),
                                ]))
                                if resolution_pattern.search(values):
                                    resolution_candidates.append((0 if selector == 'mat-button-toggle' else 1, len(text), candidate))
                            except Exception:
                                continue
                if resolution_candidates:
                    chosen = min(resolution_candidates, key=lambda item: item[:2])
                    resolution_tab = chosen[2]
                    if chosen[0] == 0:
                        # Material handles input on the button, not the host's
                        # ripple overlay. Use a real actionable button click.
                        resolution_tab = resolution_tab.locator('button[role="radio"]').first
                if resolution_tab is None:
                    if re.search(r"\bomni\b.*\bflash\b", str(duration_model or ""), re.I):
                        raise RuntimeError(
                            f"FLOW_SETTING_MISMATCH: Omni Flash resolution {resolution_value} was not found"
                        )
                    _log.info("_prepare_ui_format: resolution %s control is hidden; using Flow model default", resolution_value)
                    return
                await resolution_tab.scroll_into_view_if_needed()
                await resolution_tab.click()
                await asyncio.sleep(0.5)
                if not await _flow_control_is_selected(resolution_tab):
                    await resolution_tab.click(force=True)
                    await asyncio.sleep(0.8)
                if not await _flow_control_is_selected(resolution_tab):
                    # mat-button-toggle: visible_tab resolves to inner <button>,
                    # but the clickable host is the parent. Try closest ancestor.
                    try:
                        host = await resolution_tab.evaluate_handle(
                            "node => node.closest('mat-button-toggle, [role=\"radio\"], [role=\"tab\"]') || node.parentElement"
                        )
                        await host.as_element().click(force=True)  # type: ignore[union-attr]
                        await asyncio.sleep(0.8)
                    except Exception as _exc:
                        _log.debug("resolution ancestor click failed: %s", _exc)
                if not await _flow_control_is_selected(resolution_tab):
                    if await summary_confirms_resolution(resolution_value):
                        _log.info(
                            "_prepare_ui_format: resolution %s confirmed by settings summary",
                            resolution_value,
                        )
                    else:
                        raise RuntimeError(
                            f"FLOW_SETTING_MISMATCH: resolution {resolution_value} was not confirmed selected"
                        )

        if duration is not None:
            duration_value = str(duration).strip()
            if not re.fullmatch(r"\d{1,3}", duration_value):
                raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid duration {duration_value!r}")
            duration_label = f"{duration_value}s"
            duration_tab = await visible_duration_tab(_duration_pattern(duration_value))
            if duration_tab is None:
                # Selecting the aspect ratio can close the settings popover.
                # Reopen it before concluding that the duration control is
                # unavailable; the bottom-bar summary is not a control.
                trigger_candidates = page.locator(".settings-trigger-button, .settings-summary").filter(
                    has_text=re.compile(r"\b(?:Video|Omni|Veo)\b", re.I)
                )
                trigger = trigger_candidates.nth(await trigger_candidates.count() - 1)
                if await trigger.count() and await trigger.is_visible():
                    await trigger.click()
                    await asyncio.sleep(0.5)
                    duration_tab = await visible_duration_tab(_duration_pattern(duration_value))
            if duration_tab is None:
                if await summary_confirms_duration(duration_value):
                    _log.info("_prepare_ui_format: duration %ss confirmed by settings summary", duration_value)
                    return
                # An explicit duration is a contract. Silently accepting the
                # model default turns a requested 6s Omni job into 8s output.
                # Callers that omit the model are legacy/internal probes; they
                # cannot tell Veo's fixed default from a missing Omni control.
                if not duration_model:
                    _log.info("_prepare_ui_format: duration %s control is hidden; using Flow model default", duration_label)
                    return
                raise RuntimeError(
                    f"FLOW_SETTING_MISMATCH: duration {duration_label} was not confirmed selected (model={duration_model or 'unknown'})"
                )
            else:
                # The MAT-BUTTON-TOGGLE host has role="presentation" and Angular
                # sets aria-checked on the inner mat-button-toggle-button, not on
                # the host. force=True clicks bypass Angular's zone; use a standard
                # click on the inner button so Angular processes the event normally.
                # _flow_control_is_selected already checks closest() ancestors so
                # mat-button-toggle-checked on the host is also caught as a fallback.
                _host_tag = ""
                _inner_btn = duration_tab  # keep reference to inner button for click
                try:
                    host = await duration_tab.evaluate_handle(
                        "node => node.closest('mat-button-toggle') || node"
                    )
                    if host:
                        resolved = host.as_element()
                        if resolved:
                            _host_tag = await resolved.evaluate(
                                "node => node.tagName + '.' + (node.className||'').replace(/\\s+/g,'.')"
                            )
                            # keep duration_tab as the inner button for aria-checked checks
                except Exception as _e:
                    _host_tag = f"err:{_e}"

                try:
                    await _inner_btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                # Always click, even when aria-checked already says true. The
                # current Flow page can retain that state from the previous
                # prompt while the new composer has not committed it yet.
                # Standard click first lets Angular's NgZone process the event.
                _clicked = False
                try:
                    await _inner_btn.click(timeout=3_000)
                    await asyncio.sleep(0.4)
                    _clicked = True
                except Exception:
                    pass
                # Fall back to force click if the standard click was intercepted.
                if not _clicked or not await _flow_control_is_selected(_inner_btn):
                    try:
                        await _inner_btn.click(force=True, timeout=5_000)
                        await asyncio.sleep(0.4)
                    except Exception as exc:
                        raise RuntimeError(
                            f"FLOW_SETTING_MISMATCH: duration {duration_label} was not confirmed selected (model={duration_model or 'unknown'})"
                        ) from exc


                for _ in range(8):
                    if await _flow_control_is_selected(duration_tab):
                        break
                    await asyncio.sleep(0.2)
                if duration_tab is not None and not await _flow_control_is_selected(duration_tab):
                    if await summary_confirms_duration(duration_value):
                        _log.info("_prepare_ui_format: duration %ss confirmed by settings summary", duration_value)
                        return
                    if not duration_model:
                        _log.info("_prepare_ui_format: duration %s not confirmed; using Flow model default", duration_label)
                        return
                    try:
                        _host_attrs = await duration_tab.evaluate("""node => {
                            const attrs = {};
                            for (const a of node.attributes) attrs[a.name] = a.value;
                            attrs['_class'] = node.className;
                            // If this is the inner button, also report parent host state
                            const host = node.closest('mat-button-toggle');
                            if (host) {
                                attrs['_host_class'] = host.className;
                                attrs['_host_aria_checked'] = host.getAttribute('aria-checked');
                            }
                            return attrs;
                        }""")
                    except Exception:
                        _host_attrs = {}
                    raise RuntimeError(
                        f"FLOW_SETTING_MISMATCH: duration {duration_label} was not confirmed selected"
                        f" (model={duration_model or 'unknown'}; host={_host_tag}; hostAttrs={_host_attrs};"
                        f" controls={await duration_debug(_duration_pattern(duration_value))})"
                    )

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
        # Submit stays disabled while an attached frame is still processing.
        deadline = time.monotonic() + 60
        escaped = False
        while not candidates and time.monotonic() < deadline:
            try:
                body_text = await page.locator("body").inner_text()
                if _flow_credit_blocked(body_text):
                    raise RuntimeError(
                        "FLOW_CREDITS_INSUFFICIENT: Không có đủ tín dụng để thực hiện hành động này; "
                        "hãy chọn model/cấu hình ít tốn credits hơn."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
            for index in range(await buttons.count()):
                candidate = buttons.nth(index)
                try:
                    if not await candidate.is_visible() or await candidate.is_disabled():
                        continue
                    score = _flow_submit_button_score(
                        await candidate.inner_text(),
                        await candidate.get_attribute("aria-label") or "",
                        await candidate.get_attribute("title") or "",
                    )
                    if score:
                        candidates.append((score, index, candidate))
                except Exception:
                    continue
            if not candidates:
                if not escaped and time.monotonic() > deadline - 40:
                    escaped = True
                    try:
                        await page.keyboard.press("Escape")  # close a settings popover left over the composer
                    except Exception:
                        pass
                await asyncio.sleep(1)
        if not candidates:
            labels: list[str] = []
            try:
                for index in range(min(40, await buttons.count())):
                    node = buttons.nth(index)
                    if await node.is_visible():
                        labels.append(((await node.get_attribute("aria-label")) or (await node.inner_text()) or "").strip()[:30])
            except Exception:
                pass
            raise RuntimeError(
                "FLOW_UI_CHANGED: submit button was not found or is disabled; "
                f"refresh the Flow project and retry (visible buttons: {[l for l in labels if l][:15]})"
            )
        _, _, button = max(candidates, key=lambda item: (item[0], item[1]))
        try:
            baseline_tile_count = await page.locator("flow-grid-tile-container").count()
        except Exception:
            baseline_tile_count = 0
        try:
            baseline_composer_text = await page.evaluate("""() => [...document.querySelectorAll(
                '[role="textbox"], textarea, [contenteditable="true"]'
            )].map(node => node.value || node.innerText || node.textContent || '').join(' ').trim()""")
        except Exception:
            baseline_composer_text = ""
        baseline_errors = {str(item.get("text") or "") for item in await self._scan_visible_errors(page, 24)}
        # The generate RPC is the ground truth: DOM signals can lag or be
        # absent (virtualized grid, composer not cleared) while Flow already
        # accepted and billed the generation.
        rpc_requests: list[Any] = []
        rpc_responses: list[Any] = []

        def _is_generate_rpc(url: str) -> bool:
            return "batchAsyncGenerateVideo" in url or "batchGenerateImages" in url

        def _on_request(request) -> None:
            if _is_generate_rpc(request.url):
                rpc_requests.append(request)

        def _on_response(response) -> None:
            if _is_generate_rpc(response.url):
                rpc_responses.append(response)

        page.on("request", _on_request)
        page.on("response", _on_response)
        try:
            await self._click_flow_submit_and_confirm(
                page, button, baseline_tile_count, baseline_composer_text,
                baseline_errors, rpc_requests, rpc_responses,
            )
        finally:
            page.remove_listener("request", _on_request)
            page.remove_listener("response", _on_response)

    async def _click_flow_submit_and_confirm(
        self, page, button, baseline_tile_count: int, baseline_composer_text: str,
        baseline_errors: set[str], rpc_requests: list[Any], rpc_responses: list[Any],
    ) -> None:
        try:
            try:
                await button.hover(timeout=2_000)
                await asyncio.sleep(0.4)
            except Exception:
                pass
            await button.click(force=True, timeout=8_000)
        except Exception as exc:
            raise RuntimeError(f"FLOW_SUBMIT_CLICK_FAILED: {exc}") from exc
        # A successful click must produce an observable Flow acknowledgement.
        # Do not label the job as ``generating`` merely because Playwright's
        # click returned; otherwise a rejected/no-op submit sits at 15% until
        # the generic generation timeout hides the real cause.
        ack_deadline = time.monotonic() + 8
        rpc_deadline = time.monotonic() + 30
        while time.monotonic() < (rpc_deadline if rpc_requests else ack_deadline):
            for response in rpc_responses:
                if response.ok:
                    return
                try:
                    detail = re.sub(r"\s+", " ", await response.text())[:300]
                except Exception:
                    detail = ""
                text = f"HTTP {response.status} {detail}".strip()
                raise RuntimeError(_classify_visible_flow_error(text))
            try:
                body = await page.locator("body").inner_text()
                if _flow_credit_blocked(body):
                    raise RuntimeError("FLOW_CREDITS_INSUFFICIENT: Flow rejected the submit because credits are insufficient")
                if rpc_requests:
                    # Sent but not answered yet: never fall back to a resubmit.
                    await asyncio.sleep(0.5)
                    continue
                visible_tiles = await page.locator("flow-grid-tile-container").count()
                progressbars = await page.locator("flow-grid-tile-container [role='progressbar'], flow-grid-tile-container [aria-valuenow], flow-pending-tile").count()
                if visible_tiles > baseline_tile_count or progressbars:
                    return
                if await button.is_disabled():
                    return
                # Flow clears the composer as soon as the request is accepted;
                # this signal often arrives before the tile/progress DOM.
                composer_text = await page.evaluate("""() => [...document.querySelectorAll(
                    \"[role='textbox'], textarea, [contenteditable='true']\"
                )].map(node => node.value || node.innerText || node.textContent || '').join(' ').trim()""")
                if baseline_composer_text and not composer_text:
                    return
            except RuntimeError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if rpc_requests:
            # The RPC left the browser; Flow's grid polling owns the result now.
            return
        # Flow rejects some submits (e.g. abnormal-activity) with a toast or
        # error tile instead of a new generation tile; surface that cause.
        for item in await self._scan_visible_errors(page, 24):
            text = str(item.get("text") or "")
            if text and text not in baseline_errors:
                raise RuntimeError(_classify_visible_flow_error(text))
        raise RuntimeError(
            "FLOW_SUBMIT_NOT_CONFIRMED: Flow did not acknowledge the submit "
            "(no generate request was sent and no new generation tile appeared within 8 seconds)"
        )

    async def _ensure_flow_prompt_ready(self, page, ui, prompt: str) -> None:
        """Re-focus and verify the composer immediately before Submit."""
        wanted = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if not wanted:
            raise RuntimeError("FLOW_PROMPT_NOT_CONFIRMED: prompt is empty before submit")
        for _ in range(3):
            if await ui.fill_prompt(page, wanted):
                try:
                    actual = await page.evaluate("""() => {
                        const nodes = [
                            ...document.querySelectorAll('[role="textbox"], textarea, [contenteditable="true"]'),
                        ];
                        return nodes.map((node) => node.value || node.innerText || node.textContent || '').join(' ');
                    }""")
                    normalized = re.sub(r"\s+", " ", str(actual or "")).strip()
                    if wanted in normalized or normalized in wanted and len(normalized) >= min(24, len(wanted)):
                        return
                except Exception:
                    pass
            await asyncio.sleep(0.7)
        raise RuntimeError(
            "FLOW_PROMPT_NOT_CONFIRMED: prompt text was not present in the Flow composer before submit"
        )

    async def _set_flow_count(self, page, count: int) -> None:
        """Select the generation count in either the old tab or new radio UI."""
        desired = max(1, min(4, int(count or 1)))
        target = page.locator(_FLOW_CONTROL_SELECTOR).filter(
            has_text=re.compile(rf"^\s*x{desired}\s*$", re.IGNORECASE),
        )
        for index in range(await target.count()):
            candidate = target.nth(index)
            if not await candidate.is_visible():
                continue
            # Re-apply the count for every new prompt. A stale xN summary can
            # survive a tab reuse even when the active composer has not taken
            # the setting for this job.
            await candidate.click(force=True)
            await asyncio.sleep(0.25)
            if await _flow_control_is_selected(candidate):
                return
        if desired == 1 and await target.count() == 0:
            # Some Flow layouts omit the count control because x1 is the only
            # supported choice. Only then is proceeding without a click safe.
            _log.info("_set_flow_count: x1 is the only available generation count")
            return
        raise RuntimeError(f"FLOW_UI_CHANGED: generation count x{desired} was not found")

    async def _project_media_elements(self, page) -> list[dict[str, Any]]:
        """Read generated media tiles from Flow's current Angular DOM."""
        try:
            items = await page.evaluate(
                """() => {
                    const nodes = [
                        ...document.querySelectorAll(
                            'img[data-media-id], video[data-media-id], [data-media-id] img, [data-media-id] video, video[src], video source'
                        ),
                    ];
                    return nodes.map(element => {
                        const host = element.closest('[data-media-id]');
                        const id = element.getAttribute('data-media-id')
                            || (host && host.getAttribute('data-media-id'))
                            || '';
                        const media = element.tagName.toLowerCase() === 'source'
                            ? (element.parentElement || element)
                            : element;
                        const src = media.currentSrc || media.src || element.src || '';
                        return {
                            id,
                            tag: media.tagName.toLowerCase(),
                            src,
                            width: media.naturalWidth || media.videoWidth || 0,
                            height: media.naturalHeight || media.videoHeight || 0,
                            readyState: media.readyState || 0,
                        };
                    }).filter(item => item.src && (item.id || item.tag === 'video' || item.tag === 'img'))
                    .map(item => ({...item, id: item.id || `flow-src:${item.src}`}));
                }"""
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
            # Flow has returned completed images both under `image` and
            # under `generatedImage`; accept either shape during recovery.
            if kind == "image" and not (media.get("image") or media.get("generatedImage") or _image_fife_url(media)):
                continue
            if kind == "video" and not _video_media_ready(media):
                continue
            media_ids = set(job.get("mediaIds") or [])
            if not media_ids and str(media['name']) in set(job.get("baselineMediaIds") or []):
                continue
            if media_ids and str(media['name']) not in media_ids:
                continue
            # A parallel first attempt must have prompt metadata before it can
            # claim a tile, because the DOM/API may contain a sibling job's
            # fresh result. Recovery is different: the submission identity,
            # baseline ids, timestamp and global claims are already persisted,
            # so accept a completed API record even when Flow omitted its
            # prompt metadata. This is the common shape for late media that
            # appeared after the initial waiter timed out.
            # User rule: a result must never be assigned to the wrong job, so
            # prompt metadata is required even in recovery. No match = not found
            # = the job times out and is recreated by auto-retry.
            # A persisted media id is not sufficient ownership proof: an older
            # recovery may have claimed a sibling tile. Keep the prompt check
            # active for both fresh and resumed jobs.
            created_at = _media_created_timestamp(media)
            if not _media_prompt_matches(media, str(job.get("prompt") or "")):
                continue
            if not media_ids and created_at and created_at < minimum_time:
                continue
            records.append(media)
        if not records:
            if not job.get("mediaIds"):
                # DOM tiles carry no prompt, so they can never be attributed
                # safely to one job. Wait for API metadata or recreate.
                return []
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
            project_changed_at = _flow_project_changed_at(account, kind)
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
            if kind == "video" and len(found) < max(1, int(count or 1)):
                baseline_thumbs = {str(value) for value in job.get("baselineThumbs") or []}
                clean_baseline_thumbs = {u.split("?")[0].strip() for u in baseline_thumbs if u}
                try:
                    tiles = await page.evaluate("""() =>
                        [...document.querySelectorAll('flow-grid-tile-container')].slice(0, 24).map((tile, index) => {
                            const thumb = tile.querySelector('.thumbnail');
                            const text = (tile.innerText || '').trim().replace(/\\s+/g, ' ');
                            const pct = text.match(/(\\d+)%/);
                            return {
                                index,
                                thumb: thumb ? (thumb.currentSrc || thumb.src || '') : '',
                                pct: pct ? Number(pct[1]) : -1,
                                busy: !!tile.querySelector('[role="progressbar"], mat-progress-spinner, mat-spinner, .loading, .generating'),
                            };
                        })
                    """) or []
                except Exception:
                    tiles = []
                for tile in tiles:
                    thumb = str(tile.get("thumb") or "")
                    clean_thumb = thumb.split("?")[0].strip()
                    if (not thumb.startswith("http") or thumb in baseline_thumbs or (clean_thumb and clean_thumb in clean_baseline_thumbs)
                            or tile.get("busy") or int(tile.get("pct") or -1) >= 0):
                        continue
                    media_id = f"flow-thumb:{thumb}"
                    if media_id in used_ids or media_id in seen:
                        continue
                    seen.add(media_id)
                    found.append({"id": media_id, "tag": "video", "src": thumb,
                                  "tileIndex": int(tile.get("index") or 0)})
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
                url = _image_fife_url(media) if kind == "image" else _video_fife_url(media)
                if url.startswith('https://'):
                    item = {'id': str(media['name']), 'tag': 'img' if kind == 'image' else 'video', 'src': url}
            if item:
                found.append(item)
            if len(found) >= max(1, int(count or 1)):
                break
        return found

    async def _recover_submitted_media(self, api, page, job, timeout_s=_IMAGE_GENERATION_TIMEOUT_S):
        """Poll the original project and persist ownership before downloading."""
        count = len(job.get("mediaIds") or []) or max(1, min(4, int((job.get("settings") or {}).get("count", 1))))
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        empty_checks = 0
        # Flow may acknowledge the request before it mounts a generating tile.
        # Do not recreate an image on the first empty DOM probe.
        submission_grace_deadline = time.monotonic() + 8 if job.get("kind") == "image" else 0
        while deadline is None or time.monotonic() < deadline:
            self._check_cancel(job["id"])
            visible_error = await self._claim_visible_flow_error(page, job, count)
            if visible_error:
                classified = _classify_visible_flow_error(visible_error)
                if classified.startswith("FLOW_GENERATION_REJECTED:"):
                    store.patch_row("jobs", job["id"], {
                        "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "mediaIds": [], "resumeOnly": False,
                        "updatedAt": time.time(),
                    })
                raise RuntimeError(classified)
            # Recovery may need to claim media records whose request metadata
            # is absent in Flow's project payload. Keep this flag local to the
            # recovery probe so the initial parallel poll remains strict.
            recovery_job = {**job, "recoveryOnly": True}
            items = await self._find_existing_project_media(api, page, recovery_job, job["kind"], count)
            if len(items) >= count:
                ids = list(job.get("mediaIds") or [])
                if not ids:
                    ids = self._claim_media_ids([str(item['id']) for item in items], count)
                if ids:
                    store.patch_row("jobs", job["id"], {"mediaIds": ids, "stage": "downloading", "updatedAt": time.time()})
                    return ids
            # Recovery runs against an existing submission. The first visible
            # tile can belong to an older job (including an unusual-activity
            # banner), so classifying it here would attach the wrong error to
            # this submission. Initial submit polling still checks visible
            # errors in _wait_for_project_media().
            if job.get("kind") == "image" and page is not None and deadline is not None:
                try:
                    tile_state = await page.evaluate("""() => {
                        const tiles = [...document.querySelectorAll('flow-grid-tile-container')];
                        let percent = -1;
                        let generating = false;
                        for (const tile of tiles) {
                            if (tile.querySelector('[data-media-id], img, video')) continue;
                            const text = tile.innerText || '';
                            const match = text.match(/(\\d+)\\s*%/);
                            const bar = tile.querySelector('[role="progressbar"][aria-valuenow]');
                            const aria = Number(bar?.getAttribute('aria-valuenow'));
                            const value = Math.max(bar && Number.isFinite(aria) ? aria : -1, match ? Number(match[1]) : -1);
                            if (Number.isFinite(value) && value >= 0) percent = Math.max(percent, Math.min(99, value));
                            if (bar || tile.querySelector('mat-progress-spinner, mat-spinner, .generating') || match) generating = true;
                        }
                        return { generating, percent };
                    }""") or {}
                    generating = bool(tile_state.get("generating"))
                    percent = int(tile_state.get("percent") or -1)
                    if generating and percent >= 0:
                        store.patch_row("jobs", job["id"], {
                            "stage": "generating", "progress": _flow_generation_progress(percent),
                            "updatedAt": time.time(),
                        })
                except Exception:
                    generating = False
                # Keep polling only while Flow visibly reports an active tile.
                # A completed tile was handled above; everything else is a
                # failed or lost submission and should surface immediately.
                if generating:
                    deadline = max(deadline, time.monotonic() + 5)
            empty_checks += 1
            if empty_checks >= 1 and page is not None and time.monotonic() >= submission_grace_deadline:
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
                # An empty grid during the first recovery probes is not proof
                # that submit failed; Flow can mount the tile after navigation
                # settles. Keep the browser alive until the real recovery
                # timeout instead of closing Chrome and recreating too early.
            store.patch_row("jobs", job["id"], {"stage": "recovering", "updatedAt": time.time()})
            await asyncio.sleep(5)
        if job.get("kind") == "image":
            baseline = {str(x) for x in job.get("baselineMediaIds") or []}
            reliable = False
            try:
                data = await api.get_project_data()
                names = {str(m.get("name")) for m in data.get("projectContents", {}).get("media", []) if isinstance(m, dict)}
                reliable = bool(names) and (not baseline or bool(names & baseline))
            except Exception:
                reliable = False
            if reliable:
                raise RuntimeError("FLOW_RESULT_NOT_FOUND: submitted image result not found and nothing is generating; recreating")
        raise RuntimeError("FLOW_GENERATION_TIMEOUT: original result is not ready; retry continues recovery")

    @staticmethod
    async def _scan_visible_errors(page, limit: int) -> list[dict[str, Any]]:
        """Visible failed-tile / rejection texts on the Flow grid (newest first)."""
        if page is None:
            return []
        try:
            return await page.evaluate("""(limit) => {
                const visible = (node) => !!node && node.getClientRects().length > 0;
                const textOf = (node) => (node?.innerText || '').trim().replace(/\\s+/g, ' ');
                const tiles = [...document.querySelectorAll('flow-grid-tile-container')]
                    .slice(0, Math.max(1, limit || 1));
                const tileErrors = tiles.map((tile, index) => {
                    const candidate = tile.querySelector(
                        '.error-message, [role="alert"], [data-testid*="error" i], [class*="error" i]'
                    );
                    if (visible(candidate)) {
                        const text = textOf(candidate);
                        // Prompt tiles can carry role=alert/error classes for
                        // accessibility and include icon names such as
                        // play_circle/keyboard_return. Require actual error
                        // language before claiming a generation rejection.
                        if (text && /(?:error|failed|failure|unsuccessful|rejected|blocked|sensitive|safety|try again|not available|abnormal activity|suspicious activity|unusual activity|lỗi|thất bại|không thành công|hoạt động bất thường|hoạt động đáng ngờ|nhạy cảm|an toàn|thử lại)/i.test(text)) {
                            return { index, text };
                        }
                    }
                    // Prompt/preview tiles contain icon names and prompt text.
                    // Only an explicit error element above proves rejection.
                    return null;
                }).filter(Boolean);
                if (tileErrors.length) return tileErrors;
                // Some Flow builds render the rejection in a visible dialog
                // attached to the generation surface instead of the tile.
                const dialog = [...document.querySelectorAll(
                    '[role="dialog"], [role="alert"], mat-snack-bar-container, .mat-mdc-snack-bar-container'
                )].find(node => visible(node) && /(?:abnormal activity|hoạt động bất thường|suspicious activity|unusual activity|please wait a few seconds|vui lòng chờ vài giây|credit|tín dụng|quota|hạn mức)/i.test(textOf(node)));
                const text = textOf(dialog);
                return text ? [{ index: 0, text }] : [];
            }""", max(1, int(limit or 1))) or []
        except Exception:
            return []

    async def _remember_visible_errors(self, page, job_id: str) -> None:
        """Errors already on screen at submit time belong to older jobs, not this one."""
        texts = sorted({str(item.get("text") or "") for item in await self._scan_visible_errors(page, 24)})
        store.patch_row("jobs", job_id, {"baselineErrorTexts": texts})

    async def _claim_visible_flow_error(
        self,
        page,
        job: dict[str, Any],
        tile_limit: int,
    ) -> str:
        """Claim one visible failed Flow tile so concurrent jobs do not share it."""
        errors = await self._scan_visible_errors(page, tile_limit)
        if not errors:
            return ""
        account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
        project_id = _flow_project_id(
            account, str(job.get("kind") or "image"), legacy_for_video=True,
        )
        current_job = store.get_row("jobs", str(job.get("id") or "")) or job
        current_submission_at = float(current_job.get("submissionStartedAt") or 0)
        last_rejected_submission_at = float(current_job.get("lastGenerationRejectedSubmissionAt") or 0)
        rejection_retry_count = int(current_job.get("generationRejectRetryCount") or 0)
        baseline_errors = {str(text) for text in current_job.get("baselineErrorTexts") or []}
        with self._guard:
            for item in errors or []:
                text = str(item.get("text") or "").strip()
                if not text or text in baseline_errors:
                    continue
                signature = f"{project_id}:{int(item.get('index') or 0)}:{text}"
                if signature in self._claimed_error_tiles:
                    # The same gallery position/text can fail again on later
                    # auto-retries. Accept it only for that job's newer
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

    @staticmethod
    async def _flow_tile_percent(page, expected_count: int) -> int:
        """Real % shown on the newest Flow tiles, or -1 when Flow shows none."""
        try:
            values = await page.evaluate("""(n) =>
                [...document.querySelectorAll('flow-grid-tile-container')].slice(0, n).map(t => {
                    const bar = t.querySelector('[role="progressbar"][aria-valuenow]');
                    const aria = parseFloat(bar?.getAttribute('aria-valuenow'));
                    const m = ((t.innerText || '') + ' ' + (t.textContent || '')).match(/(\\d+)\\s*%/);
                    return Math.round(Math.max(isNaN(aria) ? -1 : aria, m ? parseInt(m[1], 10) : -1));
                })
            """, max(1, expected_count)) or []
        except Exception:
            return -1
        values = [v for v in values if v >= 0]
        return max(values) if values else -1

    async def _wait_for_project_media(
        self,
        page,
        baseline_ids: set[str],
        kind: str,
        expected_count: int,
        job_id: str,
        timeout_s: int = _IMAGE_GENERATION_TIMEOUT_S,
        api=None,
        job: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Wait for media tiles after a current Flow Angular submit."""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        hard_deadline = time.monotonic() + timeout_s * _GENERATION_WAIT_CAP
        last_pct = -1
        next_project_check = 0.0
        store.patch_row("jobs", job_id, {
            "stage": "generating",
            "progress": int((store.get_row("jobs", job_id) or {}).get("progress") or 0),
            "updatedAt": time.time(),
        })
        while deadline is None or time.monotonic() < deadline:
            self._check_cancel(job_id)
            items = await self._project_media_elements(page)
            if api is not None and job is not None and time.monotonic() >= next_project_check:
                next_project_check = time.monotonic() + 4
                fresh_job = store.get_row("jobs", job_id) or job
                recovered = await self._find_existing_project_media(api, page, fresh_job, kind, expected_count)
                recovered = [item for item in recovered if str(item['id']) not in baseline_ids]
                if len(recovered) >= max(1, expected_count):
                    return recovered[:max(1, expected_count)]
            # Parallel tabs must never claim a DOM tile without API ownership
            # metadata: the tile can belong to a sibling job in the same Flow
            # project. Waiting/retrying is safer than downloading the wrong image.
            if job and job.get("parallelSession"):
                items = []
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
                return fresh  # caller claims the first unclaimed N (siblings may own earlier ones)
            # Do not scan document.body.innerText here. Flow keeps hidden
            # accessibility copy and banners in the page shell; a body-wide
            # match can turn unrelated text into FLOW_AUTOMATION_BLOCKED.
            # _claim_visible_flow_error() below inspects only visible tile errors.
            flow_error = await self._claim_visible_flow_error(
                page,
                job or (store.get_row("jobs", job_id) or {"id": job_id}),
                expected_count,
            )
            if flow_error:
                classified = _classify_visible_flow_error(flow_error)
                if classified.startswith("FLOW_GENERATION_REJECTED:"):
                    store.patch_row("jobs", job_id, {
                        "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "mediaIds": [], "resumeOnly": False,
                        "updatedAt": time.time(),
                    })
                store.patch_row("jobs", job_id, {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "progress": 0,
                    "updatedAt": time.time(),
                })
                raise RuntimeError(classified)
            current_progress = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            pct = await self._flow_tile_percent(page, expected_count)
            # Only expose a real Flow tile percentage. Missing progress means
            # the submission is not healthy and must be handled by timeout or
            # recovery instead of being presented as advancing work.
            patch = {"stage": "generating", "updatedAt": time.time()}
            if pct >= 0:
                patch["progress"] = max(current_progress, _flow_generation_progress(pct))
                if pct > last_pct and deadline is not None:
                    last_pct = pct
                    deadline = max(deadline, min(hard_deadline, time.monotonic() + timeout_s))
                if pct >= 99:
                    # Flow reports 99% before the image URL/naturalWidth is
                    # mounted. Keep collecting this submitted result instead
                    # of treating the near-complete job as lost and charging
                    # a duplicate retry.
                    deadline = max(deadline or 0, time.monotonic() + 20)
            store.patch_row("jobs", job_id, patch)
            await asyncio.sleep(1)
        raise RuntimeError(
            f"FLOW_GENERATION_TIMEOUT: no completed {kind} media appeared after submit"
        )


    async def _download_video_via_flow_menu(
        self, page, tile_index: int, output: Path, quality: str, *, job_id: str,
    ) -> None:
        """Retry only this media download; never re-enter generation."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        current = store.get_row("jobs", job_id) or {}
        if str(output) in (current.get("outputs") or []) and self._outputs_exist([output]):
            return
        for attempt in range(1, 4):
            self._check_cancel(job_id)
            store.patch_row("jobs", job_id, {"stage": "downloading", "updatedAt": time.time()})
            try:
                await self._download_video_via_flow_menu_once(page, tile_index, output, quality)
                error = self._output_validation_error([output])
                if error:
                    raise RuntimeError(error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, PlaywrightTimeoutError) and attempt < 3:
                    self._log("warning", "download_retry", job_id=job_id,
                              account_id=str(current.get("accountId") or ""), message=str(exc),
                              details={"stage": "downloading", "attempt": attempt,
                                       "path": str(output), "quality": quality})
                    await asyncio.sleep(1)
                    continue
                raise RuntimeError(f"FLOW_DOWNLOAD_FAILED: {exc}") from exc
            # Persist each finished part before starting the next one.
            saved = list((store.get_row("jobs", job_id) or {}).get("outputs") or [])
            if str(output) not in saved:
                saved.append(str(output))
            store.patch_row("jobs", job_id, {"outputs": saved, "updatedAt": time.time()})
            return

    async def _download_video_via_flow_menu_once(
        self,
        page,
        tile_index: int,
        output: Path,
        quality: str,
    ) -> None:
        """Open Flow's download menu and pick the exact requested tier (real file)."""
        preferred = _video_download_quality({"quality": quality})
        output.parent.mkdir(parents=True, exist_ok=True)
        grid_url = str(page.url or "")
        # Close any lingering modals/drawers/backdrops before interacting with the tile
        try:
            for _ in range(3):
                backdrop = page.locator(".cdk-overlay-backdrop")
                if await backdrop.count() > 0 and await backdrop.first.is_visible():
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                else:
                    break
        except Exception:
            pass
        tile = page.locator("flow-grid-tile-container").nth(tile_index)
        try:
            await tile.locator(".thumbnail").first.click(timeout=8_000)
        except Exception:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass
            await tile.locator(".thumbnail").first.click(force=True, timeout=10_000)
        dl_btn = page.locator(
            'button[aria-label*="Tải xuống"], button[aria-label*="Download"], '
            'button[aria-label*="download"], button[title*="Download"], '
            'button[data-testid*="download" i], [role="button"][aria-label*="Download"], '
            'button:has(mat-icon:text-is("download")), '
            'button:has(mat-icon:text-is("file_download"))'
        ).first
        try:
            # Legacy Flow exposes the download button directly in the editor.
            # Do not open the three-dot menu: its "Upload content" action is
            # adjacent and can be selected by automation on this page.
            await dl_btn.wait_for(state="visible", timeout=10_000)
            await dl_btn.click()
            menu_items = page.get_by_role("menuitem")
            await menu_items.first.wait_for(state="visible", timeout=5_000)
            texts = [await menu_items.nth(i).inner_text() for i in range(await menu_items.count())]
            index = _pick_video_menu_item(texts, preferred)
            if index is None or await menu_items.nth(index).get_attribute("aria-disabled") == "true":
                raise RuntimeError(
                    f"FLOW_DOWNLOAD_QUALITY_UNAVAILABLE: menu has no {preferred} (menu: {texts})"
                )
            # Upscaled tiers render server-side first (~1–2 min).
            async with page.expect_download(timeout=300_000) as dl_info:
                await menu_items.nth(index).click()
            dl = await dl_info.value
            partial = output.with_name(output.name + ".part")
            try:
                await dl.save_as(str(partial))
                if self._output_validation_error([partial]):
                    raise RuntimeError("FLOW_OUTPUT_EMPTY: downloaded video is empty")
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
        finally:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            # Clicking a thumbnail opens /edit/<id>; return to the grid so the next
            # tile index still points at the same list.
            if "/edit/" in str(page.url or "") and grid_url and "/edit/" not in grid_url:
                try:
                    await page.goto(grid_url, wait_until="domcontentloaded", timeout=20_000)
                    await page.locator("flow-grid-tile-container").first.wait_for(timeout=10_000)
                except Exception:
                    pass

    async def _download_video_from_project_media(
        self,
        api,
        job: dict[str, Any],
        output: Path,
        output_index: int,
    ) -> str:
        """Download a completed video directly when Flow's menu is broken.

        The project payload contains an authenticated FIFE URL after Flow has
        finished rendering. This path preserves the generated media and avoids
        submitting a duplicate job just because the edit-page menu changed.
        """
        data = await api.get_project_data()
        submission_at = float(job.get("submissionStartedAt") or 0)
        baseline_ids = {str(value) for value in job.get("baselineMediaIds") or []}
        candidates: list[dict[str, Any]] = []
        for media in data.get("projectContents", {}).get("media", []):
            if not isinstance(media, dict) or not media.get("name"):
                continue
            media_id = str(media.get("name"))
            if media_id in baseline_ids or not _video_media_ready(media):
                continue
            url = _video_fife_url(media)
            if not url.startswith("http"):
                continue
            created_at = _media_created_timestamp(media)
            if submission_at and created_at and created_at < submission_at - 30:
                continue
            candidates.append({"id": media_id, "url": url, "createdAt": created_at})
        candidates.sort(key=lambda item: float(item.get("createdAt") or 0), reverse=True)
        if not candidates:
            raise RuntimeError("FLOW_DOWNLOAD_URL_MISSING: completed video URL was not exposed by Flow")
        selected = candidates[min(max(0, int(output_index) - 1), len(candidates) - 1)]
        await api.download(str(selected["url"]), output)
        error = self._output_validation_error([output])
        if error:
            raise RuntimeError(error)
        return str(selected["id"])

    async def _download_upscaled_video_api(
        self,
        api,
        media_id: str,
        output: Path,
        *,
        settings: dict[str, Any] | None,
        preferred: str,
        job_id: str,
    ) -> None:
        """Real Flow upsample API (1080p free). Used when the download menu fails."""
        from ._flow._api import (
            VIDEO_AR_LANDSCAPE,
            VIDEO_AR_PORTRAIT,
            VIDEO_RES_1080P,
        )

        if preferred != "1080p":
            raise RuntimeError(
                f"FLOW_DOWNLOAD_QUALITY_UNAVAILABLE: API upsample only covers 1080p, not {preferred}"
            )
        if not media_id or str(media_id).startswith("dom-video:"):
            raise RuntimeError("FLOW_DOWNLOAD_QUALITY_UNAVAILABLE: no media id for upsample")
        try:
            data = await api.get_project_data()
        except Exception as exc:
            raise RuntimeError(f"FLOW_UPSCALE_PROJECT_MISSING: {exc}") from exc
        workflow_id = ""
        for media in data.get("projectContents", {}).get("media", []):
            if str((media or {}).get("name") or "") == str(media_id):
                workflow_id = str(media.get("workflowId") or "")
                break
        if not workflow_id:
            raise RuntimeError("FLOW_UPSCALE_WORKFLOW_MISSING: video has no workflowId")
        ratio = str((settings or {}).get("ratio") or "16:9")
        aspect = VIDEO_AR_PORTRAIT if ratio in {"9:16", "3:4", "2:3"} else VIDEO_AR_LANDSCAPE
        _up_job, status = await api.upscale_and_wait(
            media_id,
            workflow_id,
            timeout_s=_VIDEO_GENERATION_TIMEOUT_S,
            resolution=VIDEO_RES_1080P,
            aspect_ratio=aspect,
            on_poll=lambda _s, elapsed: store.patch_row(
                "jobs", job_id, {"progress": min(95, 90 + int(elapsed / 60)), "updatedAt": time.time()}
            ),
        )
        url = status.fife_url or await self._resolve_video_fife_url(api, _up_job.media_name)
        if not url:
            raise RuntimeError(f"FLOW_DOWNLOAD_URL_MISSING: upsampled {_up_job.media_name}")
        await api.download(url, output)

    async def _flow_tile_thumbs(self, page) -> list[str]:
        """Thumbnail URLs already on the grid, so old clips are never re-claimed."""
        try:
            return list(await page.evaluate("""() =>
                [...document.querySelectorAll('flow-grid-tile-container .thumbnail')]
                    .map(img => img.currentSrc || img.src || '')
                    .filter(Boolean)
            """) or [])
        except Exception:
            return []

    async def _wait_and_download_flow_videos(
        self,
        page,
        job: dict[str, Any],
        count: int,
        baseline_thumbs: list[str],
        job_id: str,
        timeout_s: int = _VIDEO_GENERATION_TIMEOUT_S,
        api=None,
    ) -> list[str]:
        """Wait for this job's new Flow video tiles, then download via the quality menu."""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        hard_deadline = time.monotonic() + timeout_s * _GENERATION_WAIT_CAP
        last_pct = -1
        expected_count = max(1, min(4, int(count or 1)))
        started_at = time.monotonic()
        known = set(baseline_thumbs or [])
        tile_indexes: list[int] = []
        while deadline is None or time.monotonic() < deadline:
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
                raise RuntimeError(_classify_visible_flow_error(flow_error))
            tiles = await page.evaluate("""() =>
                [...document.querySelectorAll('flow-grid-tile-container')].slice(0, 24).map((t, index) => {
                    const text = (t.innerText || '').trim().replace(/\\s+/g, ' ');
                    // Only video tiles: uploaded start frames are image tiles.
                    const thumb = t.querySelector('flow-video-tile .thumbnail');
                    // A finished tile mounts a playback progressbar on hover;
                    // only a pending tile's progress means "still generating".
                    const pending = !!t.querySelector('flow-pending-tile') || (!thumb && !!t.querySelector(
                        '[role="progressbar"], mat-progress-spinner, mat-spinner, .loading, .generating'));
                    // Flow exposes % via aria-valuenow or text; an indeterminate
                    // spinner can pin aria-valuenow at 0 while the text advances.
                    const bar = t.querySelector('[role="progressbar"][aria-valuenow]');
                    const ariaPct = Number(bar?.getAttribute('aria-valuenow'));
                    const pctMatch = (text + ' ' + (t.textContent || '')).match(/(\\d+)\\s*%/);
                    const pct = pending ? Math.round(Math.max(
                        bar && Number.isFinite(ariaPct) ? ariaPct : -1,
                        pctMatch ? parseInt(pctMatch[1], 10) : -1,
                    )) : -1;
                    return {
                        index,
                        label: t.getAttribute('aria-label') || '',
                        thumb: thumb ? (thumb.currentSrc || thumb.src || '') : '',
                        pct,
                        busy: pending,
                    };
                })
            """) or []
            elapsed = time.monotonic() - started_at
            current_progress = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            pcts = [tile["pct"] for tile in tiles if tile.get("pct", -1) >= 0]
            patch = {"stage": "generating", "updatedAt": time.time()}
            if pcts:
                # Real Flow tile % only; no time-based guessing.
                patch["progress"] = max(current_progress, _flow_generation_progress(max(pcts)))
                if max(pcts) > last_pct and deadline is not None:
                    last_pct = max(pcts)
                    deadline = max(deadline, min(hard_deadline, time.monotonic() + timeout_s))
            store.patch_row("jobs", job_id, patch)
            # aria-label is a Flow-generated short title, not the prompt. The grid is
            # newest-first, so this submission owns the first video/pending tiles;
            # an older run finishing further down must not be claimed.
            ours = [
                tile for tile in tiles
                if tile.get("thumb") or tile.get("pct", -1) >= 0 or tile.get("busy")
            ][:expected_count]
            finished = [
                tile for tile in ours
                if tile.get("thumb") and tile["thumb"] not in known
                and tile.get("pct", -1) < 0 and not tile.get("busy")
            ]
            if len(finished) >= expected_count:
                claimed = self._claim_media_ids(
                    [f"flow-thumb:{tile['thumb']}" for tile in finished], expected_count,
                )
                if claimed:
                    tile_indexes = [
                        tile["index"] for tile in finished
                        if f"flow-thumb:{tile['thumb']}" in claimed
                    ]
                    store.patch_row("jobs", job_id, {"mediaIds": claimed, "updatedAt": time.time()})
                    break
            await asyncio.sleep(1.5)
        else:
            raise RuntimeError(
                f"FLOW_AUTOMATION_FAILED: video result was not observed after submit within {timeout_s}s"
            )

        store.patch_row("jobs", job_id, {"stage": "downloading", "updatedAt": time.time()})
        account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
        quality = _video_download_quality(
            job.get("settings") if isinstance(job.get("settings"), dict) else {},
            str(account.get("plan") or ""),
        )
        outputs: list[str] = []
        for output_index, tile_index in enumerate(tile_indexes, 1):
            self._check_cancel(job_id)
            output = self._output_path(job, output_index, "mp4")
            try:
                await self._download_video_via_flow_menu(page, tile_index, output, quality, job_id=job_id)
            except Exception as menu_exc:
                if api is None:
                    raise
                try:
                    await self._download_video_from_project_media(api, job, output, output_index)
                except Exception as api_exc:
                    raise menu_exc from api_exc
            outputs.append(str(output))
            self._log(
                "success", "output_downloaded",
                job_id=job_id, account_id=str(job.get("accountId") or ""),
                details={"outputIndex": output_index, "path": str(output), "quality": quality},
            )
        return outputs

    async def _resolve_video_fife_url(self, api, media_id: str) -> str:
        """Prefer project payload URL when status polling lags behind the UI."""
        if not media_id or str(media_id).startswith("dom-video:"):
            return ""
        try:
            data = await api.get_project_data()
        except Exception:
            return ""
        for media in data.get("projectContents", {}).get("media", []):
            if str((media or {}).get("name") or "") != str(media_id):
                continue
            return _video_fife_url(media)
        return ""

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
            from .browser import BrowserManager
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
            # Visible and headless jobs deliberately use the same browser path;
            # the only behavioral switch is ``headless``.  Each job gets one
            # isolated profile, so Flow never has to coordinate multiple tabs
            # or a separately bootstrapped CDP session.
            browser = BrowserManager(
                headless=headless,
                profile_dir=profile_dir or (
                    self._clone_runtime_profile(str(account["id"]), job_id, job_kind)
                    if job_kind in {"image", "video"}
                    else store.profile_dir(account["id"])
                ),
                restore_last_session=str(account.get("authMethod") or "chrome") == "cookie",
            )
            self._check_cancel(job_id)
            with self._guard:
                self._active_browsers[job_id] = browser
            await browser.start()
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
            # A lost-result failure means the previous submission was not
            # observable in Flow. Retrying it through the recovery poll only
            # opens the gallery and leaves the composer untouched, which looks
            # like a frozen 15% job. Start a real UI submission instead.
            recovery_error = str(job.get("error") or "")
            lost_result_retry = bool(
                re.search(r"FLOW_RESULT_NOT_FOUND|FLOW_EMPTY_OUTPUT|GENERATION_TIMEOUT|FLOW_AUTOMATION_FAILED", recovery_error, re.I)
                and not job.get("mediaIds")
                and not job.get("outputs")
            )
            recovery_state = (
                str(job.get("stage") or "") in {"recovering", "downloading"}
                or bool(job.get("mediaIds"))
                or bool(job.get("resumeOnly"))
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
            if not recovery_state:
                page = await browser.page()
                account = await self._ensure_account_project_page(
                    page, api=api, client=client, account=account, job_id=job_id, kind=job_kind,
                )
                project_id = str(client.project_id)
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
                    await self._ensure_flow_prompt_ready(page, client._ui, job["prompt"])
                    await _wait_before_flow_submit()
                    await self._click_flow_submit(page)
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
                # Settings/mode steps above can blur or clear the composer; make
                # sure the prompt is present or Submit stays disabled.
                await self._ensure_flow_prompt_ready(page, client._ui, prompt_text)
                await _wait_before_flow_submit()
                media_items = []
                try:
                    await self._click_flow_submit(page)
                except RuntimeError as exc:
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
                        store.patch_row("jobs", job_id, {
                            "submissionStartedAt": None, "submissionProjectId": None,
                            "baselineMediaIds": [], "updatedAt": time.time(),
                        })
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
                submitted_job = {
                    **job,
                    "submissionStartedAt": time.time(),
                    "submissionProjectId": project_id,
                    "baselineMediaIds": sorted(baseline_ids),
                }
                if not media_items:
                    media_items = await self._wait_for_project_media(
                        page, baseline_ids, "image", count, job_id,
                        api=api, job=submitted_job,
                    )
                if not media_items:
                    raise RuntimeError("FLOW_EMPTY_OUTPUT: Flow returned no completed images")
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
            if not await asyncio.to_thread(network_available, force=True):
                store.patch_row('jobs', job_id, {
                    'status': 'processing', 'stage': 'waiting_network',
                    'networkError': str(exc), 'updatedAt': time.time(),
                })
                raise _FlowNetworkInterrupted from exc
            needs_login = _session_needs_login(exc)
            automation_block = _is_automation_block_error(exc)
            error = str(exc)
            if not needs_login and not error.startswith("FLOW_QUOTA_EXHAUSTED:"):
                classified_error = _classify_visible_flow_error(error)
                if classified_error.startswith("FLOW_QUOTA_EXHAUSTED:"):
                    error = classified_error
            quota_model_fallback = (
                error.startswith("FLOW_QUOTA_EXHAUSTED:")
                and str((current.get("settings") or {}).get("model") or "") == "Nano Banana Pro"
                and str(current.get("kind") or "") == "image"
                and not bool(current.get("quotaModelFallbackApplied"))
            )
            if quota_model_fallback:
                self._apply_image_quota_fallback(job_id, error)
                return
            # A normal submit/timeout/auth error belongs to this job only. Do
            # not take a healthy account offline unless Flow explicitly shows
            # unusual activity and the shared recovery path is required.
            action = "action_required" if needs_login and automation_block else "failed"
            failed_stage = (store.get_row("jobs", job_id) or {}).get("stage")
            store.patch_row("jobs", job_id, {"status": action, "stage": action, "error": error, "updatedAt": time.time()})
            if needs_login and automation_block:
                # Kick the single shared account login immediately — do not
                # wait for the outer worker loop (Bearer missing = re-login).
                # Drop stale seeded token so cooldown does not block Chrome.
                store.patch_row("accounts", account["id"], {
                    "status": "reconnect",
                    "error": str(exc),
                    "accessToken": None,
                    "accessTokenAt": None,
                    "updatedAt": time.time(),
                })
                self._ensure_shared_reconnect(
                    account["id"],
                    job_id=job_id,
                    reason=str(exc),
                )
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

    def _apply_image_quota_fallback(self, job_id: str, error: str) -> None:
        with self._account_condition:
            current = store.get_row("jobs", job_id) or {}
            if not current or current.get("status") == "cancelled" or job_id in self._cancelled:
                return
            account_id = current["accountId"]
            event = {
                "id": uuid.uuid4().hex, "jobId": job_id, "accountId": account_id,
                "randomAccount": bool(current.get("randomAccount")),
                "fromModel": "Nano Banana Pro", "toModel": "Nano Banana 2",
                "rawError": error, "createdAt": time.time(),
            }
            for row in store.list_rows("jobs"):
                is_current = row["id"] == job_id
                if not is_current and not (
                    row.get("accountId") == account_id and row.get("status") == "queued"
                    and row.get("kind") == "image"
                    and (row.get("settings") or {}).get("model") == "Nano Banana Pro"
                ):
                    continue
                patch = {"settings": {**dict(row.get("settings") or {}), "model": "Nano Banana 2"},
                         "updatedAt": time.time()}
                if is_current:
                    patch.update({"quotaModelFallbackApplied": True, "modelFallback": event,
                                  "status": "processing", "stage": "model_fallback", "error": None})
                store.patch_row("jobs", row["id"], patch)
            store.patch_row("accounts", account_id, {"lastModelFallback": event, "updatedAt": time.time()})
            self._log("warning", "image_model_fallback", job_id=job_id, account_id=account_id,
                      message=error, details=event)

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

        A CDP manager is attached to the user's manually opened Chrome and may
        only disconnect/close its owned tab. A normal job manager owns its
        profile, so a stuck Playwright shutdown can be force-killed safely.
        """
        try:
            await asyncio.wait_for(browser.stop(), timeout=8.0)
            if not getattr(browser, "cdp_url", None):
                # Persistent Chromium can outlive Playwright's context close
                # for a short time. Reap the job-owned profile process too.
                await asyncio.to_thread(browser.kill_now)
                if account_id and runtime_profile and runtime_profile.is_dir():
                    await asyncio.to_thread(self._sync_runtime_cookies, account_id, runtime_profile)
            return
        except Exception as exc:
            self._log(
                "warning",
                "browser_cleanup_failed",
                job_id=job_id,
                message=str(exc),
                details={"cdpAttached": bool(getattr(browser, "cdp_url", None))},
            )
        # Never kill a browser reached through CDP: it belongs to the user's
        # manual account session. Automatic jobs have no cdp_url and own the
        # temporary profile, so kill_now closes only that Chrome process.
        if not getattr(browser, "cdp_url", None):
            try:
                await asyncio.to_thread(browser.kill_now)
                if account_id and runtime_profile and runtime_profile.is_dir():
                    await asyncio.to_thread(self._sync_runtime_cookies, account_id, runtime_profile)
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

    def retry(self, job_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
        existing = store.get_row("jobs", job_id)
        if not existing:
            return None
        overrides = overrides or {}
        if str(existing.get("error") or "").startswith("FLOW_LOGO_REMOVE_FAILED") and self._outputs_exist(existing.get("outputs")):
            # Images are already downloaded: redo only the logo step, no Flow account needed.
            self._cancelled.discard(job_id)
            job = store.patch_row("jobs", job_id, {
                "status": "processing", "stage": "removing_logo", "progress": POST_PROGRESS_START, "error": None, "updatedAt": time.time(),
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
        settings.update({key: value for key, value in dict(overrides.get("settings") or {}).items() if value not in (None, "")})
        mode = str(existing.get("mode") or "text")
        requested_mode = str(overrides.get("mode") or "").strip()
        if requested_mode in {"text", "edit", "reference", "frame"}:
            mode = requested_mode
        # headless is sent at the top level by the UI (not inside settings{})
        if "headless" in overrides:
            settings["headless"] = _flow_headless({"headless": overrides["headless"]})
        kind = str(existing.get("kind") or "video")
        if kind == "image":
            # Popup values are canonical for this retry. Mirror them into the
            # image-prefixed fields before normalization so stale snapshot
            # fields cannot overwrite a newly selected model/ratio/resolution.
            requested_settings = dict(overrides.get("settings") or {})
            for key, image_key in (("model", "imageModel"), ("ratio", "imageRatio"), ("resolution", "imageResolution"), ("count", "imageCount")):
                if requested_settings.get(key) not in (None, ""):
                    settings[image_key] = requested_settings[key]
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
            and self._account_suspended(account_id)
        ):
            # Only the explicit UI retry may override a cooldown. Automatic
            # retries and internal model fallbacks must keep the quarantine.
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
            job = store.patch_row("jobs", job_id, {
                **recovery_fields,
                "status": "queued", "stage": "queued", "progress": 0,
                "queueOrder": order, "error": None, "outputs": [], "updatedAt": time.time(),
                "accountId": account_id, "settings": settings,
                "generationToken": uuid.uuid4().hex,
                "mode": mode,
                "generationRejectRetryCount": 0,
                "autoRetryCount": 0,
                "authRetryCount": 0,
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


service = FlowService()
