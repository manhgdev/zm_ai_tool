"""Small, cancellable network gate shared by external workers."""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .jobs import Cancelled, check_cancel, current_job_id

_probe_lock = threading.Lock()
_checked_at = float('-inf')
_available = True
_clock = threading.local()
_log = logging.getLogger(__name__)


def active_monotonic() -> float:
    """Worker timeout clock excluding time spent waiting for Internet."""
    return time.monotonic() - getattr(_clock, 'paused', 0.0)


def is_remote_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    if host in {'localhost', ''} or host.endswith('.local'):
        return False
    try:
        address = ipaddress.ip_address(host)
        return not (address.is_private or address.is_loopback or address.is_link_local)
    except ValueError:
        return True


def network_available(*, force: bool = False, timeout: float = 2.5) -> bool:
    """Cached, single-flight route probe; never sends credentials or job data."""
    global _checked_at, _available
    with _probe_lock:
        # Coalesce simultaneous failure checks; avoid one probe per job/browser.
        if time.monotonic() - _checked_at < (1.0 if force else 5.0):
            return _available
        _available = False
        # ponytail: TCP reachability detects broad outages, not captive portals
        # or provider health. Keep provider retry limits; add an HTTP health
        # probe only if captive-portal detection becomes necessary.
        for host in ('www.google.com', 'www.microsoft.com'):
            try:
                with socket.create_connection((host, 443), timeout=timeout):
                    _available = True
                    break
            except OSError:
                continue
        _checked_at = time.monotonic()
        return _available


def wait_for_network(
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_wait: Callable[[float], None] | None = None,
    force_check: bool = False,
    initial_delay: float = 5.0,
    max_delay: float = 30.0,
) -> float:
    """Return offline seconds (zero when online); cancellation always raises."""
    def check() -> None:
        check_cancel(current_job_id())
        if cancel_check and cancel_check():
            raise Cancelled()

    check()
    if network_available(force=force_check):
        check()
        return 0.0
    started = time.monotonic()
    delay = max(0.5, float(initial_delay))
    try:
        check()
        _log.info('NETWORK_WAITING')
        if on_wait:
            on_wait(delay)
        while True:
            end = time.monotonic() + delay
            while True:
                check()
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.2, remaining))
            if network_available(force=True):
                check()
                _log.info('NETWORK_RESUMED')
                return time.monotonic() - started
            delay = min(max_delay, delay * 1.7)
    finally:
        _clock.paused = getattr(_clock, 'paused', 0.0) + time.monotonic() - started
