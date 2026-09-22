"""Verified, resumable wheel downloads; no package resolver or venv writes here."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class RuntimeInstallError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True, diagnostics: str = ''):
        super().__init__(message)
        self.code, self.retryable = code, retryable
        self.diagnostics = redact(diagnostics or message)


def redact(value: str) -> str:
    value = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1<redacted>@', value)
    return re.sub(r'(?i)([?&](?:token|key|signature|x-amz-signature|password)=)[^&\s]+', r'\1<redacted>', value)


def classify_error(error, default='DEPENDENCY_INSTALL_FAILED') -> tuple[str, bool]:
    status = getattr(error, 'code', None)
    text = str(error).lower()
    reason = getattr(error, 'reason', error)
    if getattr(error, 'errno', None) == 28 or any(s in text for s in ('os error 112', 'winerror 112', 'no space left', 'not enough space on the disk', 'errno 28')):
        return 'DISK_FULL', False
    if isinstance(reason, PermissionError) or any(s in text for s in ('winerror 32', 'os error 32', 'access is denied', 'permission denied')):
        return 'FILE_ACCESS_DENIED', False
    if status == 407 or re.search(r'\b407\b|proxy authentication', text):
        return 'PROXY_AUTH_FAILED', False
    if isinstance(reason, ssl.SSLError) or any(s in text for s in ('certificate_verify_failed', 'certificate verify failed', 'invalid peer certificate', 'certificate has expired')):
        return 'TLS_CERTIFICATE_FAILED', False
    for code, name, retry in [(401, 'HTTP_UNAUTHORIZED', False), (403, 'HTTP_FORBIDDEN', False), (404, 'HTTP_NOT_FOUND', False), (429, 'HTTP_RATE_LIMITED', True)]:
        if status == code or re.search(rf'(?:http\S*\s+|status(?: code)?[: ]+|error[: ]+){code}\b|\b{code} (?:client error|forbidden|not found|too many)', text):
            return name, retry
    if isinstance(reason, socket.gaierror) or any(s in text for s in ('name resolution', 'dns error', 'getaddrinfo', 'nodename nor servname')):
        return 'DNS_FAILED', True
    if isinstance(reason, (TimeoutError, socket.timeout)) or any(s in text for s in ('timed out', 'timeout', 'install_idle_timeout')):
        return 'NETWORK_TIMEOUT', True
    if isinstance(status, int) and 500 <= status < 600 or re.search(r'\b50[0-9]\b|\b52[0-9]\b', text):
        return 'INDEX_UNAVAILABLE', True
    if any(s in text for s in ('no solution found', 'resolutionimpossible', 'no matching distribution', 'could not find a version', 'dependencies are unsatisfiable')):
        return 'DEPENDENCY_RESOLUTION_FAILED', False
    if isinstance(reason, (ConnectionError, http.client.IncompleteRead)) or any(s in text for s in ('connection reset', 'connection refused', 'connection aborted', 'remote end closed', 'incomplete read', 'unexpected eof')):
        return 'DOWNLOAD_INTERRUPTED', True
    if 'os error 448' in text:
        return default, False
    return default, False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    filename: str
    url: str
    digest: str
    size: int | None = None

    def path(self, cache: Path) -> Path:
        return cache / self.digest / self.filename


def _retry_delay(error, attempt: int) -> float:
    retry_after = getattr(error, 'headers', {}).get('Retry-After') if getattr(error, 'headers', None) else None
    try:
        return min(30.0, max(1.0, float(retry_after))) if retry_after else float(2 ** attempt)
    except ValueError:
        return float(2 ** attempt)


def download_wheel(wheel: Wheel, cache: Path, report: Callable, stop: threading.Event,
                   *, opener=urllib.request.urlopen, sleep=time.sleep) -> Path:
    """At most three attempts; only complete, hash-verified wheels are visible."""
    target = wheel.path(cache)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + '.part')
    if target.is_file():
        if sha256(target) == wheel.digest:
            report(wheel, target.stat().st_size, target.stat().st_size, True, 0, 'cached')
            return target
        target.unlink()  # only this corrupt artifact, never the runtime
    total = wheel.size
    started = time.monotonic()
    for attempt in range(1, 4):
        if stop.is_set():
            raise RuntimeInstallError('INSTALL_CANCELLED', 'Download cancelled', retryable=True)
        received = part.stat().st_size if part.is_file() else 0
        try:
            # A process may have exited after the final byte but before rename.
            if received and (total is None or received == total) and sha256(part) == wheel.digest:
                os.replace(part, target)
                report(wheel, received, received, True, attempt, 'cached')
                return target
            if total is not None and received >= total:
                part.unlink(missing_ok=True)
                received = 0
            headers = {'User-Agent': 'ZM-AI-TOOL-runtime', 'Accept-Encoding': 'identity'}
            if received:
                headers['Range'] = f'bytes={received}-'
            with opener(urllib.request.Request(wheel.url, headers=headers), timeout=30) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != 'https' and urllib.parse.urlsplit(wheel.url).hostname not in ('127.0.0.1', 'localhost'):
                    raise RuntimeInstallError('TLS_CERTIFICATE_FAILED', 'Insecure download redirect', retryable=False)
                if response.status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != received:
                        raise RuntimeInstallError('DOWNLOAD_INTERRUPTED', 'Invalid resume range', retryable=False)
                    total = int(match[3])
                else:
                    received = 0  # Server ignored Range: replace, never append.
                    length = response.headers.get('Content-Length')
                    total = int(length) if length else wheel.size
                free = shutil.disk_usage(cache).free
                if total is not None and free < total - received + 64 * 1024**2:
                    raise OSError(28, f'Need {total - received} bytes; free={free}; disk={cache.anchor}')
                report(wheel, received, total, False, attempt, 'downloading')
                with part.open('ab' if received else 'wb') as output:
                    last_report = time.monotonic()
                    while True:
                        if stop.is_set():
                            raise RuntimeInstallError('INSTALL_CANCELLED', 'Download cancelled')
                        if time.monotonic() - started > 2700:
                            raise RuntimeInstallError('NETWORK_TIMEOUT', 'Download exceeded 45 minute budget', retryable=False)
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        if time.monotonic() - last_report >= .25:
                            report(wheel, received, total, False, attempt, 'downloading')
                            last_report = time.monotonic()
                if total is not None and received != total:
                    raise ConnectionError(f'Download interrupted at {received}/{total} bytes')
            report(wheel, received, total, False, attempt, 'verifying')
            if sha256(part) != wheel.digest:
                part.unlink(missing_ok=True)
                raise RuntimeInstallError('CHECKSUM_MISMATCH', f'Checksum mismatch: {wheel.name}', retryable=False)
            os.replace(part, target)
            report(wheel, received, received, False, attempt, 'complete')
            return target
        except Exception as exc:
            code, retry = (exc.code, exc.retryable) if isinstance(exc, RuntimeInstallError) else classify_error(exc, 'DOWNLOAD_INTERRUPTED')
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 416:
                part.unlink(missing_ok=True)
                code, retry = 'DOWNLOAD_INTERRUPTED', True
            delay = _retry_delay(exc, attempt)
            diagnostics = json.dumps({'package': wheel.name, 'version': wheel.version, 'url': wheel.url,
                'sha256': wheel.digest, 'partialPath': str(part), 'receivedBytes': received,
                'totalBytes': total, 'attempt': attempt, 'retryAfterSeconds': delay if retry and attempt < 3 else None,
                'httpStatus': getattr(exc, 'code', None), 'cause': str(exc)}, ensure_ascii=False)
            if not retry or attempt == 3 or stop.is_set():
                raise RuntimeInstallError(code, f'{wheel.name}: {code}', retryable=retry, diagnostics=diagnostics) from exc
            report(wheel, received, total, False, attempt, 'retrying', diagnostics)
            if sleep is time.sleep:
                stop.wait(delay)
            else:
                sleep(delay)
    raise AssertionError('unreachable')
