"""Transient CDN/network download error detection (stdlib only)."""
from __future__ import annotations

import re


def is_transient_download_error(exc: BaseException | str) -> bool:
    """True for CDN/network blips that should retry download without regenerating."""
    text = str(exc or "")
    return bool(re.search(
        r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EPIPE|ENETUNREACH|"
        r"ERR_CONNECTION_(?:RESET|TIMED_OUT|CLOSED|REFUSED)|"
        r"ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|ERR_SSL|"
        r"net::ERR_|socket disconnected|TLS|secure TLS|"
        r"TimeoutError|Timeout \d+ms exceeded",
        text,
        re.I,
    ))
