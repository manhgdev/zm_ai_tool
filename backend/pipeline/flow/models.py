"""Flow model catalogs, entitlement costs, and request setting normalization."""
from __future__ import annotations

import re
from typing import Any

from .constants import (
    _DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT,
    _DEFAULT_VIDEO_MODEL,
    _DISABLED_VIDEO_MODELS,
    _DURATION_UNITS,
    _IMAGE_UI_MODELS,
    _MAX_CONCURRENT_JOBS_PER_ACCOUNT,
)


def _normalize_video_model(value: Any) -> str:
    """Migrate saved jobs away from models Flow has temporarily removed."""
    model = str(value or _DEFAULT_VIDEO_MODEL).strip()
    if model == "Omni Flash":
        model = "Omni 1.1 Flash"
    model = model.replace("Veo 3.1 Fast", _DEFAULT_VIDEO_MODEL).replace(
        "Veo 3.1 Quality", "Veo 3.1 - Quality",
    )
    return _DEFAULT_VIDEO_MODEL if model in _DISABLED_VIDEO_MODELS else model


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
