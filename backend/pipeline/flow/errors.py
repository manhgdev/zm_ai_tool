"""Flow error classification, retry delays, and network interruption handling."""
from __future__ import annotations

import asyncio
import re
import time

from pipeline.core.jobs import Cancelled
from pipeline.core.network import wait_for_network

from . import store
from .constants import (
    _FLOW_INFRA_HARD_STOP,
    _FLOW_INFRA_INTERRUPT,
    _LOGIN_LEAF_CODES,
    _SOFT_RETRY_BACKOFF_CAP_S,
)


def _soft_retry_backoff_s(attempt: int) -> float:
    """Short backoff between finite soft auto-retries."""
    return min(_SOFT_RETRY_BACKOFF_CAP_S, 3.0 * (2 ** max(0, min(int(attempt) - 1, 3))))


class _FlowNetworkInterrupted(Exception):
    """Close the failed browser once, then resume the same generation identity."""


class _FlowSessionGenerationChanged(Exception):
    """The account profile changed while this worker owned its browser."""


def _is_flow_infra_interrupt(error: Exception | str) -> bool:
    """True for network/Chrome-load failures that should wait_network, not fail."""
    text = str(error or "")
    if not text or _FLOW_INFRA_HARD_STOP.search(text):
        return False
    if _is_automation_block_error(text):
        return False
    return bool(_FLOW_INFRA_INTERRUPT.search(text))


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


def _is_non_quota_image_reject(text: str) -> bool:
    """True for Flow's generic image failure tile without actual quota/limit copy."""
    body = re.sub(r"^(?:FLOW_[A-Z0-9_]+:\s*)+", "", str(text or "").strip(), flags=re.I).lower()
    if not body:
        return False
    if re.search(
        r"quota|h[eế]t l[uư][oợ]t|h[eế]t h[aạ]n m[uứ]c|usage limit|generation limit|"
        r"daily limit|monthly limit|h[aạ]n m[uứ]c|"
        r"b[aạ]n [đd][ãa] [đd][aạ]t [đd][eế]n h[aạ]n m[uứ]c",
        body,
    ):
        return False
    return bool(
        re.search(
            r"không tạo được hình|couldn'?t create(?: this)? image|unable to (?:create|generate)(?: this)? image",
            body,
        )
    )


def _classify_visible_flow_error(text: str) -> str:
    """Map Flow tile error copy to a stable code (credits vs quota vs generic)."""
    raw = re.sub(r"^(?:FLOW_[A-Z0-9_]+:\s*)+", "", str(text or "").strip(), flags=re.I).strip()
    raw = re.sub(r"^(?:warning|error|info)\s+(?:không thành công|failed|error|success)\s+", "", raw, flags=re.I)
    raw = re.sub(
        r"(?:\s+\b(?:refresh|undo|redo|delete_forever|delete|more_vert|close|content_copy|download|warning|error_outline)\b)+\s*$",
        "",
        raw,
        flags=re.I,
    ).strip()
    if not raw:
        return ""
    low = raw.lower()
    if re.search(
        r"abnormal activity|hoạt động bất thường|hoạt động đáng ngờ|suspicious activity|unusual activity",
        low,
    ):
        return f"FLOW_AUTOMATION_BLOCKED: {raw}"
    # Credits only when the copy is clearly about Flow credits (not bare "not enough").
    if _flow_credit_blocked(raw) or re.search(
        r"(?:out of|h[eế]t)\s+(?:credits?|t[ií]n(?:\s*d[uụ]ng)?)|"
        r"(?:not enough|insufficient)\s+credits?|"
        r"không có đủ tín dụng|"
        r"\bcredits?\s+(?:empty|exhausted|depleted)",
        low,
    ):
        return f"FLOW_CREDITS_EMPTY: {raw}"
    # Daily/plan quota before "try again later" — Flow often appends
    # "Vui lòng thử lại sau" to hạn mức copy; that must not become REJECTED.
    if re.search(
        r"quota|h[eế]t l[uư][oợ]t|h[eế]t h[aạ]n m[uứ]c|usage limit|generation limit|"
        r"daily limit|monthly limit|h[aạ]n m[uứ]c|"
        r"b[aạ]n [đd][ãa] [đd][aạ]t [đd][eế]n h[aạ]n m[uứ]c",
        low,
    ):
        return f"FLOW_QUOTA_EXHAUSTED: {raw}"
    # Soft-fail image tile without "hạn mức" ("Rất tiếc, không tạo được hình ảnh này.
    # Bạn chưa bị tính phí cho lượt tạo này.") is a per-prompt generation reject, not
    # an account/model quota exhaustion — must not quarantine the model or account.
    if _is_non_quota_image_reject(raw):
        return f"FLOW_GENERATION_REJECTED: {raw}"
    # Transient throttle / rate limit — not daily quota and not bot detection.
    if re.search(
        r"please wait a few seconds|vui lòng chờ vài giây|rate.?limit|too many|"
        r"try again (?:in|later)|thử lại sau",
        low,
    ):
        return f"FLOW_GENERATION_REJECTED: {raw}"
    return f"FLOW_GENERATION_REJECTED: {raw}"


def _is_flow_quota_exhausted(text: str) -> bool:
    """True for coded or raw Flow hạn mức / daily quota tile copy."""
    raw = str(text or "")
    if _is_non_quota_image_reject(raw):
        return False
    if "FLOW_QUOTA_EXHAUSTED" in raw:
        return True
    return _classify_visible_flow_error(raw).startswith("FLOW_QUOTA_EXHAUSTED:")


def _flow_credit_blocked(text: str) -> bool:
    """Detect Flow's pre-submit credit gate, before it exposes a submit button."""
    return bool(re.search(
        r"không có đủ tín dụng để thực hiện hành động này|"
        r"not enough credits|insufficient credits|out of credits|"
        r"you (?:do not|don't) have enough credits",
        str(text or ""),
        re.I,
    ))


def _collapse_flow_login_error(reason: str) -> str:
    """Single login/session code — never nest FLOW_LOGIN_REQUIRED: prefixes."""
    text = str(reason or "").strip() or "FLOW_LOGIN_REQUIRED"
    if re.search(r"flow account was not found|account not found", text, re.I) and not text.upper().startswith("FLOW_ACCOUNT_NOT_FOUND"):
        return f"FLOW_SESSION_EXPIRED: {text}"
    parts = [p.strip() for p in re.split(r"\s*:\s*", text) if p.strip()]
    leaf_i = None
    for i, part in enumerate(parts):
        if part.upper() in _LOGIN_LEAF_CODES:
            leaf_i = i
    if leaf_i is None:
        return text if text.upper().startswith("FLOW_LOGIN_REQUIRED") else f"FLOW_LOGIN_REQUIRED: {text}"
    code = parts[leaf_i].upper()
    detail = ": ".join(
        p for p in parts[leaf_i + 1 :] if p.upper() not in _LOGIN_LEAF_CODES
    ).strip()
    return f"{code}: {detail}" if detail else code


def _confirmed_login_wall_error(error: Exception | str) -> bool:
    """True only for a confirmed Google login wall / expired cookie — not verification prompts."""
    text = str(error or "")
    if re.search(r"FLOW_LOGIN_VERIFICATION_REQUIRED|FLOW_LOGIN_PASSWORD_REQUIRED", text, re.I):
        return False
    return bool(re.search(
        r"FLOW_COOKIE_EXPIRED|Google session is not signed in|"
        r"session cookies expired|cookies.*expired|redirected to login|reached login|login wall|"
        r"flow account was not found|account not found",
        text,
        re.I,
    ))


def _session_needs_login(error: Exception | str) -> bool:
    """Identify failures that require the visible Google re-login flow."""
    text = str(error)
    # API 401 without a login-wall is common when Bearer is missing but cookies
    # still work for Flow UI — do not treat as reconnect.
    if "avoid reconnect storm" in text.lower():
        return False
    if re.search(r"Could not obtain Bearer token", text, re.I) and "redirected" not in text.lower():
        return False
    # Soft generation failures / quota must never look like a login wall.
    if _is_flow_quota_exhausted(text) or "FLOW_GENERATION_REJECTED" in text:
        return False
    return bool(re.search(
        r"LOGIN_REQUIRED|SESSION_EXPIRED|COOKIE_EXPIRED|session.*expired|cookies.*expired|redirected to login|recaptcha|accounts\.google\.com|flow\.google\.com/about|/about|not signed in|unauthenticated|authentication required|flow account was not found|account not found",
        text,
        re.I,
    ))


def _is_automation_block_error(error: Exception | str) -> bool:
    """Only Flow's unusual-activity response may trigger account reconnect."""
    return bool(re.search(
        r"FLOW_AUTOMATION_BLOCKED|abnormal activity|unusual activity|"
        r"suspicious activity|hoạt động bất thường|hoạt động đáng ngờ|"
        r"PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        str(error or ""),
        re.I,
    ))


def _rpc_public_error(detail: str) -> str:
    """Map a batchexecute PUBLIC_ERROR_* body onto a stable Flow error code."""
    raw = str(detail or "")
    match = re.search(r"PUBLIC_ERROR_(\w+)", raw)
    if not match:
        return ""
    reason = match.group(1).replace("_", " ").lower()
    return _classify_visible_flow_error(f"{reason} {raw}")


def _redact_flow_diag_url(url: str) -> str:
    """Strip cookie/token query params from diagnostic URLs."""
    text = str(url or "")
    return re.sub(
        r"(?i)(token|access_token|auth|authorization|bearer|cookie|key|sapisid|sid)=[^&?#]*",
        r"\1=[redacted]",
        text,
    )
