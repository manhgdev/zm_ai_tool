"""Shared Flow limits, selectors, and provider identifiers."""
from __future__ import annotations

import re

# Match flow.google.com/project/<id> URLs and project paths.
_PROJECT_RE = re.compile(r"^(?:https://flow\.google\.com(?::443)?)?(?:/flow)?/project/([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:[/?#]|$)")

_TERMINAL = {"done", "failed", "cancelled", "action_required"}

FLOW_LOG_LIMIT = 300

_DEFAULT_CONCURRENT_JOBS_PER_ACCOUNT = 3

_MAX_CONCURRENT_JOBS_PER_ACCOUNT = 18

_SESSION_REFRESH_INTERVAL_S = 60.0

# Soft errors (timeout, reject, download blip, Chrome closed) auto-retry up to
# this many times — finite on purpose; infinite retry is a bug. Hard stops
# (credits, daily quota, unrecoverable login, cancel) never enter this budget.
# Unusual activity gets one session-reset retry on its own counter.
_JOB_AUTO_RETRY_MAX = 3

_UNUSUAL_ACTIVITY_RESET_MAX = 1

_ACCOUNT_RECOVERY_MAX = 3

_SOFT_RETRY_BACKOFF_CAP_S = 30.0

# A Flow generation can legitimately take longer than ten minutes; keep a
# generous upper bound so a lost Flow page cannot leave a worker stuck forever.
# Flow acknowledges an image submission before mounting the result tile. On
# migrated accounts the project payload can lag another minute or two.
# Keep the waiter alive long enough to observe that same submission instead of
# recreating it and charging the account twice.
_IMAGE_GENERATION_TIMEOUT_S = 90

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

_FLOW_COOKIE_DOMAINS = (
    "google.com", "google.com.vn", "googleusercontent.com", "gstatic.com", "flow.google.com",
)

# Google ties the login to these profile files, not to the cookie DB alone:
# deleting any of them made Google demand verification. Verified by wiping
# everything else from a signed-in profile and still landing in Flow.
_PROFILE_IDENTITY_ROOT = {"Default", "Local State", "Last Version"}

_PROFILE_IDENTITY_DEFAULT = {
    "Cookies", "Cookies-journal", "Network",
    "Device Bound Sessions", "Device Bound Sessions-journal",
    "Preferences", "Secure Preferences",
    "Login Data", "Login Data-journal", "Login Data For Account", "Login Data For Account-journal",
    "Web Data", "Web Data-journal", "Account Web Data", "Account Web Data-journal",
    "Sync Data", "Trust Tokens", "Trust Tokens-journal",
    "TransportSecurity", "Network Persistent State", "ServerCertificate", "ServerCertificate-journal",
}

_IMAGE_UI_MODELS = {"Nano Banana Pro", "Nano Banana 2.1", "Nano Banana 2", "Nano Banana 2 Lite"}

_VIDEO_UI_MODELS = {
    "Omni 1.1 Flash", "Veo 3.1 - Lite", "Veo 3.1 - Fast",
    "Veo 3.1 - Quality", "Veo 3.1 - Lite [Lower Priority]",
}

# Temporarily disabled upstream; remove this entry to re-enable when Flow restores it.
_DISABLED_VIDEO_MODELS = {"Veo 3.1 - Lite [Lower Priority]"}

_DEFAULT_VIDEO_MODEL = "Veo 3.1 - Fast"

# Chrome/page load blips when the TCP probe still thinks the host is online.
_FLOW_INFRA_INTERRUPT = re.compile(
    r"FLOW_NETWORK_UNAVAILABLE|ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|"
    r"ERR_CONNECTION_(?:RESET|TIMED_OUT|CLOSED|REFUSED)|ERR_NAME_NOT_RESOLVED|"
    r"net::ERR_|Page\.goto|Navigation timeout|Timeout \d+ms exceeded|"
    r"NS_ERROR_NET|Could not connect|ECONNRESET|ENETUNREACH|EHOSTUNREACH",
    re.I,
)

_FLOW_INFRA_HARD_STOP = re.compile(
    r"FLOW_CREDITS_EMPTY|FLOW_CREDITS_INSUFFICIENT|FLOW_QUOTA_EXHAUSTED|"
    r"FLOW_DOWNLOAD_QUALITY_UNAVAILABLE|FLOW_AUTOMATION_BLOCKED",
    re.I,
)

_VIDEO_DONE_STATUSES = frozenset({
    "MEDIA_GENERATION_STATUS_COMPLETE",
    "MEDIA_GENERATION_STATUS_SUCCESS",
    "MEDIA_GENERATION_STATUS_SUCCESSFUL",
})

_VIDEO_FAIL_STATUSES = frozenset({
    "MEDIA_GENERATION_STATUS_FAILED",
    "MEDIA_GENERATION_STATUS_REJECTED",
})

# Cooldown per error type (0 = no suspension):
_COOLDOWN_QUOTA_S     = 14 * 3600   # 14h — FLOW_QUOTA_EXHAUSTED (daily quota)

_COOLDOWN_RATELIMIT_S = 60          # 1min — HTTP 429 / rate limit

_COOLDOWN_CREDITS_S   = 24 * 3600   # 24h — FLOW_CREDITS_EMPTY (needs manual top-up)
# GENERATION_REJECTED — no suspension (content/prompt issue, not account issue)
# FLOW_LOGIN_REQUIRED — no suspension (_ensure_shared_reconnect handles it)

_FLOW_PLANS = frozenset({"Free", "Plus", "Pro", "Ultra"})

# Flow changed the generation settings controls from ``role=tab`` to
# ``role=radio``.  Keep both selectors so existing and current builds work.
_FLOW_CONTROL_SELECTOR = '[role="tab"], [role="radio"], [role="option"], button'

_DURATION_UNITS = r"(?:s|sec(?:ond)?s?|giây)"

_LOGIN_LEAF_CODES = frozenset({
    "FLOW_LOGIN_VERIFICATION_REQUIRED",
    "FLOW_LOGIN_PASSWORD_REQUIRED",
    "FLOW_LOGIN_REQUIRED",
    "FLOW_SESSION_EXPIRED",
    "FLOW_COOKIE_EXPIRED",
    "FLOW_COOKIE_SESSION_UNVERIFIED",
})
