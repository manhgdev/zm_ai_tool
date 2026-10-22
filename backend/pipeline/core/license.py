"""APP account/key credentials and bounded access cache; Web worker admission state."""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import math
import os
import platform
import secrets
import sqlite3
import threading
import time
import uuid
import webbrowser
import weakref
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from pipeline.core.config import DATA, ensure_data_dirs

LICENSE_FILE = DATA / 'license.json'
_CACHE_SECONDS = 300
_OFFLINE_MAX_SECONDS = 900
_lock = threading.RLock()
_request_lock = threading.Lock()
_cache = None
_checked_at = 0.0
_pending = {}
_continuations = set()
_admitted = contextvars.ContextVar('admitted_job', default=False)
_monitor_started = False
_pending_watchers = []
_SIG_KEY = f'zm-tool-license-{uuid.getnode()}-2025'.encode()


def _sign(payload):
    return hmac.new(_SIG_KEY, json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def _read():
    try:
        data = json.loads(LICENSE_FILE.read_text(encoding='utf-8'))
        signature = data.pop('_sig', '')
        if not hmac.compare_digest(str(signature), _sign(data)):
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def _save(data):
    ensure_data_dirs()
    payload = {**data, '_sig': _sign(data)}
    temporary = LICENSE_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(LICENSE_FILE)


def server_url():
    raw = os.environ.get('ZM_AI_TOOL_AUTH_SERVER_URL', 'https://zmaitool.com').rstrip('/')
    parsed = urlsplit(raw)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1')):
        raise ValueError('INVALID_AUTH_SERVER_URL')
    return raw


def _request(action, body):
    with httpx.Client(timeout=8, trust_env=False) as client:
        response = client.post(server_url() + '/api/desktop/' + action, json=body)
    if response.status_code in (400, 401, 403, 404, 409, 422):
        detail = response.json().get('detail', 'DEVICE_REVOKED')
        raise ValueError(detail if isinstance(detail, str) else 'INVALID_FIELDS')
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError('INVALID_LICENSE_RESPONSE')
    return result


def _empty(code='ACCESS_PENDING', configured=False):
    return {'valid': False, 'configured': configured, 'mode': 'account', 'keyMasked': '', 'remainingDay': 0,
            'expiresAt': None, 'activationLimit': 0, 'message': '', 'code': code, 'state': code.removeprefix('ACCESS_').lower()}


def _current(status, verified_at):
    result = dict(status)
    now = time.time()
    begins = result.get('startsAt')
    if begins:
        try:
            if datetime.fromisoformat(begins.replace('Z', '+00:00')).timestamp() > now:
                result.update(valid=False, code='ACCESS_SCHEDULED', state='scheduled')
        except (ValueError, TypeError):
            result.update(valid=False, code='INVALID_LICENSE_RESPONSE')
    expiry = result.get('expiresAt')
    if expiry:
        try:
            remaining = datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp() - now
            result['remainingDay'] = max(0, math.ceil(remaining / 86400))
            if remaining <= 0:
                result.update(valid=False, code='ACCESS_EXPIRED', state='expired')
        except (ValueError, TypeError):
            result.update(valid=False, code='INVALID_LICENSE_RESPONSE')
    if result.get('lifetime'):
        result['remainingDay'] = -1
    if result.get('valid') and (not verified_at or now - verified_at >= _OFFLINE_MAX_SECONDS or verified_at > now + 60):
        result.update(valid=False, code='LICENSE_CHECK_REQUIRED', transient=True)
    return result


def _install(result, previous=None, *, mode=None):
    global _cache, _checked_at
    if not isinstance(result.get('deviceToken'), str) or not result['deviceToken'] or not isinstance(result.get('valid'), bool):
        raise RuntimeError('INVALID_LICENSE_RESPONSE')
    if mode and result.get('mode') != mode:
        raise RuntimeError('INVALID_LICENSE_RESPONSE')
    if result.get('mode') not in ('account', 'key') or result.get('state') not in ('active', 'pending', 'blocked', 'scheduled', 'expired', 'deleted', 'unactivated') or result['valid'] != (result['state'] == 'active') or type(result.get('lifetime')) is not bool:
        raise RuntimeError('INVALID_LICENSE_RESPONSE')
    if result['valid']:
        if not result.get('startsAt') or not result['lifetime'] and not result.get('expiresAt'):
            raise RuntimeError('INVALID_LICENSE_RESPONSE')
        for field in ('startsAt', 'expiresAt'):
            if result.get(field):
                try:
                    parsed = datetime.fromisoformat(result[field].replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    raise RuntimeError('INVALID_LICENSE_RESPONSE') from None
    data = previous or _read()
    token = result['deviceToken']
    status = {k: v for k, v in result.items() if k != 'deviceToken'}
    status.update(configured=True, message='')
    data.update(device_token=token, status=status, verified_at=time.time(), authority=server_url())
    _save(data)
    _cache, _checked_at = data, time.monotonic()
    return _current(status, data['verified_at'])


def installation():
    data = _read()
    if not data.get('installation_id'):
        data['installation_id'] = secrets.token_hex(16)
        _save(data)
    return data


def license_status(*, force=False):
    global _cache, _checked_at
    if os.environ.get('ZM_AI_TOOL_WEB_WORKER') == '1':
        # SQLite is the same authority used by the gateway; admission sees revocation immediately.
        from accounts import license_status as account_status
        try:
            with sqlite3.connect('file:' + os.environ['ZM_AI_TOOL_WEB_AUTH_DB'] + '?mode=ro', uri=True, timeout=5) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('SELECT * FROM users WHERE id=?', (os.environ['ZM_AI_TOOL_WEB_USER_ID'],)).fetchone()
                return account_status(dict(row)) if row else _empty('ACCOUNT_DELETED', True)
        except (KeyError, sqlite3.Error):
            return _empty('LICENSE_CHECK_REQUIRED', True)
    with _request_lock:
        with _lock:
            data = _cache or _read()
            token = data.get('device_token')
            if not token or data.get('authority') != server_url():
                return _empty()
            if not force and _checked_at and time.monotonic() - _checked_at < _CACHE_SECONDS:
                return _current(data.get('status') or _empty(), data.get('verified_at', 0))
        try:
            result = _request('status', {'deviceToken': token})
            if not isinstance(result.get('valid'), bool):
                raise RuntimeError('INVALID_LICENSE_RESPONSE')
            return _install({**result, 'deviceToken': token}, data, mode=data.get('status', {}).get('mode'))
        except ValueError as exc:
            status = {**(data.get('status') or _empty()), 'valid': False, 'configured': True, 'code': str(exc), 'state': 'blocked'}
            data.update(status=status, verified_at=0)
            with _lock:
                _save(data)
                _cache, _checked_at = data, time.monotonic()
            return data['status']
        except Exception:
            with _lock:
                # Do not move verified_at on a network failure, including RAM cache hits.
                _cache, _checked_at = data, time.monotonic()
            result = _current(data.get('status') or _empty('LICENSE_CHECK_REQUIRED', True), data.get('verified_at', 0))
            result['transient'] = True
            return result


def activate_license(key):
    if not isinstance(key, str) or not key.strip().startswith('ZMAI-'):
        raise ValueError('KEY_INVALID')
    with _request_lock, _lock:
        data = installation()
        if data.get('device_token') and data.get('authority') == server_url():
            raise ValueError('DEVICE_ALREADY_CONFIGURED')
        result = _request('activate', {'key': key.strip(), 'installationId': data['installation_id'], 'name': platform.node()[:200]})
        status = _install(result, data, mode='key')
        _pending.clear()  # A late browser callback must not replace the chosen key.
        return status


def deactivate_license():
    global _cache, _checked_at
    with _request_lock, _lock:
        data = installation()
        if data.get('device_token'):
            try:
                _request('logout', {'deviceToken': data['device_token']})
            except ValueError as exc:
                if str(exc) != 'DEVICE_REVOKED':
                    raise ValueError('LOGOUT_NETWORK_REQUIRED') from None
            except Exception:
                # Keep the credential so an offline sign-out can be retried without losing the device slot.
                raise ValueError('LOGOUT_NETWORK_REQUIRED') from None
        _save({'installation_id': data['installation_id']})
        _pending.clear()
        _cache, _checked_at = None, 0
    return _empty()


def start_desktop_login(port):
    with _lock:
        data = _read()
        if data.get('device_token') and data.get('authority') == server_url():
            raise ValueError('DEVICE_ALREADY_CONFIGURED')
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        redirect = f'http://127.0.0.1:{port}/api/auth/desktop/callback'
        _pending.clear()
        _pending[state] = {'verifier': verifier, 'redirectUri': redirect, 'expires': time.time() + 600}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        url = server_url() + '/api/desktop/authorize?' + urlencode({'state': state, 'challenge': challenge, 'redirect_uri': redirect})
    webbrowser.open(url)
    return {'authorizationUrl': url}


def finish_desktop_login(state, code):
    with _request_lock, _lock:
        pending = _pending.pop(state, None)
        if not pending or pending['expires'] <= time.time():
            raise ValueError('DESKTOP_CODE_INVALID')
        data = installation()
        if data.get('device_token') and data.get('authority') == server_url():
            raise ValueError('DEVICE_ALREADY_CONFIGURED')
        result = _request('exchange', {**{k: v for k, v in pending.items() if k != 'expires'}, 'code': code,
                                      'installationId': data['installation_id'], 'name': platform.node()[:200]})
        _install(result, data, mode='account')


def license_cached_valid():
    return bool(license_status().get('valid'))


def is_admitted():
    return _admitted.get() or os.environ.get('ZM_AI_TOOL_ADMITTED_JOB') == '1'


def continue_job(kind, uid):
    with _lock:
        _continuations.add((kind, uid))


def continuation(kind, uid):
    with _lock:
        return (kind, uid) in _continuations


def end_continuation(kind, uid):
    with _lock:
        _continuations.discard((kind, uid))


def can_start_job():
    if is_admitted():
        return True
    # Standalone scripts/tests without a hosted runtime retain their normal behavior.
    if os.environ.get('ZM_AI_TOOL_WEB_WORKER') != '1' and os.environ.get('ZM_AI_TOOL_DESKTOP') != '1':
        return True
    return license_cached_valid()


@contextmanager
def admitted_job():
    token = _admitted.set(True)
    try:
        yield
    finally:
        _admitted.reset(token)


def run_admitted(fn, *args, **kwargs):
    with admitted_job():
        return fn(*args, **kwargs)


def watch_pending_jobs(callback):
    _pending_watchers.append(weakref.WeakMethod(callback))


def pause_pending_jobs():
    for reference in list(_pending_watchers):
        callback = reference()
        if callback is None:
            _pending_watchers.remove(reference)
        else:
            callback()


def start_license_monitor():
    global _monitor_started
    if _monitor_started or not (os.environ.get('ZM_AI_TOOL_DESKTOP') == '1' or os.environ.get('ZM_AI_TOOL_WEB_WORKER') == '1'):
        return
    _monitor_started = True
    def loop():
        while True:
            try:
                if not license_status().get("valid"):
                    pause_pending_jobs()
            except Exception:
                pass
            time.sleep(1)
    threading.Thread(target=loop, name='account-access', daemon=True).start()
