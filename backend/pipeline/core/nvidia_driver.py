"""Opt-in Windows driver repair; the elevated worker only uses Windows Update.

The app stays unprivileged. A one-use loopback channel receives worker progress;
no privileged worker writes to a caller-supplied path or executes downloaded code.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache

from .runtime_active import runtime_home
from .runtime_download import RuntimeInstallError, redact

REPAIRABLE = {'DRIVER_TOO_OLD', 'NVIDIA_SMI_NOT_FOUND', 'NVIDIA_DRIVER_UNAVAILABLE', 'GPU_PROBE_FAILED'}
KINDS = {'ai_runtime', 'ocr_cuda', 'demucs_cuda'}
WAITING = {'driver_eula', 'driver_reboot'}
TERMINAL = {'driver_done', 'driver_failed'}
_lock = threading.RLock()
_requests = 0
_cpu_busy = False
_tokens: dict[str, tuple[float, str, str]] = {}
_local = threading.local()


def _path() -> Path:
    return runtime_home() / 'runtime' / 'nvidia-driver.json'


def snapshot() -> dict:
    with _lock:
        try:
            data = json.loads(_path().read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # Never start another installation on top of an unreadable journal.
            return {'phase': 'driver_unknown', 'errorCode': 'DRIVER_STATE_UNKNOWN'}


def _save(data: dict) -> None:
    with _lock:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)


def pending() -> bool:
    return sys.platform == 'win32' and (_cpu_busy or snapshot().get('phase') not in (None, *TERMINAL))


@contextlib.contextmanager
def cpu_maintenance(*, force: bool = False):
    global _cpu_busy
    with _lock:
        assert_available()
        if not force:
            assert_idle()
        _cpu_busy = True
    _local.resume = True
    try:
        yield
    finally:
        _local.resume = False
        with _lock:
            _cpu_busy = False


def assert_available() -> None:
    if pending() and not getattr(_local, 'resume', False):
        raise RuntimeInstallError('RUNTIME_BUSY', 'NVIDIA maintenance is pending', retryable=False)


@contextlib.contextmanager
def request_activity():
    """Reserve ordinary mutating API requests before their background jobs spawn."""
    global _requests
    with _lock:
        assert_available()
        _requests += 1
    try:
        yield
    finally:
        with _lock:
            _requests -= 1


@contextlib.contextmanager
def driver_mutex():
    """Same OS mutex as the elevated worker; protects runtimes across app copies."""
    if sys.platform != 'win32':
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, 'Global\\ZMAIToolNvidiaDriver')
    if not handle:
        raise RuntimeInstallError('RUNTIME_BUSY', 'Cannot open driver maintenance mutex')
    acquired = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
    try:
        if not acquired:
            raise RuntimeInstallError('RUNTIME_BUSY', 'Windows driver installation is still running')
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _active_jobs() -> bool:
    from .config import DATA, PUBLIC_DATA
    from . import jobs
    with jobs._lock:
        if any(jobs._job_pids.values()) or any(jobs._job_procs.values()):
            return True
    studio = sys.modules.get('pipeline.tts.studio')
    if studio and any(item.get('running') for item in studio.list_job_progress().values()):
        return True
    automation = sys.modules.get('pipeline.automation.service')
    if automation and any(job.get('status') in ('queued', 'running') for job in automation.service.list_jobs()):
        return True
    queue_path = DATA / 'queue' / 'jobs.json'
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text(encoding='utf-8'))
            queue = queue if isinstance(queue, list) else queue.get('jobs', [])
            if any(job.get('status') in ('running', 'queued') for job in queue):
                return True
        except (OSError, ValueError, AttributeError):
            raise RuntimeInstallError('GPU_JOBS_BUSY', 'Cannot verify queue activity')
    for path in {*DATA.glob('*/meta.json'), *PUBLIC_DATA.glob('*/meta.json')}:
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('status', {}).get('running'):
                return True
        except (OSError, ValueError):
            raise RuntimeInstallError('GPU_JOBS_BUSY', 'Cannot verify project activity')
    # Other GPU-capable exporters expose snapshots once their modules are loaded.
    for name in ('pipeline.cleaner.cleaner_jobs', 'pipeline.srt_export', 'pipeline.srt_image', 'pipeline.drawing.jobs'):
        module = sys.modules.get(name)
        if module and any(job.get('running') or job.get('status') in ('running', 'queued', 'processing') for job in module.list_jobs()):
            return True
    return False


def assert_idle() -> None:
    if _requests or _active_jobs():
        raise RuntimeInstallError('GPU_JOBS_BUSY', 'Finish current jobs before updating the GPU driver')


def confirmation(action: str, binding: str) -> str:
    with _lock:
        now = time.monotonic()
        for key, value in list(_tokens.items()):
            if value[0] < now:
                del _tokens[key]
        token = secrets.token_urlsafe(32)
        _tokens[token] = (now + 120, action, binding)
        return token


def consume(token: str, action: str, binding: str) -> None:
    with _lock:
        value = _tokens.pop(token, None)
    if not value or value[0] < time.monotonic() or value[1:] != (action, binding):
        raise RuntimeInstallError('DRIVER_CONFIRMATION_EXPIRED', 'Confirmation expired; please retry')


def _powershell() -> str:
    path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not path.is_file():
        raise RuntimeInstallError('DRIVER_UPDATE_UNAVAILABLE', 'Windows PowerShell is unavailable', retryable=False)
    return str(path)


@lru_cache(maxsize=1)
def _boot_id() -> str:
    result = subprocess.run([_powershell(), '-NoProfile', '-NonInteractive', '-Command',
                             '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")'],
                            capture_output=True, text=True, timeout=15,
                            creationflags=int(getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    if result.returncode or not result.stdout.strip():
        raise RuntimeInstallError('GPU_PROBE_FAILED', 'Cannot determine Windows boot session')
    return result.stdout.strip()


def _elevate(source: str):
    """ShellExecuteEx retains the actual elevated process handle (no kill timeout)."""
    from ctypes import wintypes
    class ExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    info = ExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # NOCLOSEPROCESS | NOASYNC
    info.lpVerb, info.lpFile, info.nShow = 'runas', _powershell(), 0
    encoded = base64.b64encode(source.encode('utf-16le')).decode('ascii')
    info.lpParameters = '-NoProfile -NonInteractive -EncodedCommand ' + encoded
    from .runtime_site import external_process_dll_search
    with external_process_dll_search(info.lpFile):
        launched = shell.ShellExecuteExW(ctypes.byref(info))
        error = ctypes.get_last_error() if not launched else 0
    if not launched:
        raise RuntimeInstallError('DRIVER_UAC_CANCELLED' if error == 1223 else 'DRIVER_UPDATE_UNAVAILABLE',
                                  f'Windows elevation failed: {error}')
    return info.hProcess


def _run_worker(state: dict, emit) -> dict:
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/progress' or not hmac.compare_digest(self.headers.get('X-Driver-Token', ''), token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 2_000_000:
                    raise ValueError('invalid length')
                data = json.loads(self.rfile.read(length))
                if data.get('operationId') != state['operationId'] or data.get('phase') not in {
                    'driver_search', 'driver_download', 'driver_install', 'driver_eula',
                    'driver_reboot', 'driver_verify', 'driver_failed',
                }:
                    raise ValueError('invalid progress')
                with _lock:
                    state.update({k: v for k, v in data.items() if k in {
                        'phase', 'eula', 'fingerprint', 'updates', 'gpus', 'results', 'rebootRequired',
                        'partialFailure', 'errorCode', 'diagnostics',
                    }})
                    _save(state)
                emit(state)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            except (ValueError, OSError):
                self.send_error(400)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'callback': f'http://127.0.0.1:{server.server_port}/progress', 'token': token,
              'operationId': state['operationId'], 'acceptedFingerprint': state.pop('acceptedFingerprint', '')}
    encoded = base64.b64encode(json.dumps(config).encode('utf-8')).decode('ascii')
    source = "$config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')) | ConvertFrom-Json\n"
    source += Path(__file__).with_name('nvidia_driver_update.ps1').read_text(encoding='utf-8')
    handle = None
    try:
        handle = _elevate(source)
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # No forced termination: Windows may still be changing the display driver.
        while True:
            waited = kernel.WaitForSingleObject(handle, 1000)
            if waited == 0:
                break
            if waited != 0x102:
                raise RuntimeInstallError('DRIVER_STATE_UNKNOWN', 'Cannot observe the elevated installer')
        if state['phase'] not in (*WAITING, 'driver_verify', 'driver_failed'):
            state.update(phase='driver_unknown', errorCode='DRIVER_STATE_UNKNOWN')
            _save(state)
        return state
    finally:
        if handle:
            kernel.CloseHandle(handle)
        server.shutdown()
        server.server_close()


def reserve(kind: str, action: str) -> dict:
    if sys.platform != 'win32' or os.environ.get('ZM_AI_TOOL_DESKTOP') != '1' or kind not in KINDS:
        raise RuntimeInstallError('DRIVER_UPDATE_UNAVAILABLE', 'Windows desktop only', retryable=False)
    with _lock, driver_mutex():
        assert_idle()
        old = snapshot()
        if action == 'accept_eula' and old.get('phase') != 'driver_eula':
            raise RuntimeInstallError('DRIVER_CONFIRMATION_EXPIRED', 'Driver terms changed')
        if action == 'update' and old.get('phase') not in (None, *TERMINAL):
            raise RuntimeInstallError('RUNTIME_BUSY', 'Driver repair already pending')
        if action == 'resume' and old.get('phase') in (None, *TERMINAL):
            raise RuntimeInstallError('DRIVER_CONFIRMATION_EXPIRED', 'No pending repair')
        state = {**(old if action == 'resume' else {}), 'operationId': str(uuid.uuid4()), 'resumeKind': kind, 'phase': 'driver_check',
                 'errorCode': '', 'diagnostics': '', 'rebootRequired': False}
        if action == 'accept_eula':
            state['acceptedFingerprint'] = old['fingerprint']
        if action != 'resume':
            state['bootId'] = _boot_id()
        elif old.get('phase') == 'driver_reboot' and old.get('bootId') == _boot_id():
            raise RuntimeInstallError('DRIVER_REBOOT_REQUIRED', 'Restart Windows before continuing')
        _save(state)
        return state


def run(state: dict, action: str, emit, resume) -> dict:
    try:
        from .runtime_install import detect_profile
        try:
            with driver_mutex():
                detect_profile()
            valid = True
        except RuntimeInstallError as exc:
            if exc.code not in REPAIRABLE:
                raise
            valid = False
            state['driverCheck'] = exc.driver_check
        if not valid and action != 'resume':
            state.update(phase='driver_uac', errorCode='')
            _save(state)
            emit(state)
            _run_worker(state, emit)
            if state['phase'] in WAITING:
                return public_status(state)
            if state['phase'] != 'driver_verify':
                raise RuntimeInstallError(state.get('errorCode') or 'DRIVER_STATE_UNKNOWN',
                                          str(state.get('diagnostics') or state.get('results') or state['phase']))
        with driver_mutex():
            detect_profile()
        state.update(phase='driver_resume', errorCode='', rebootRequired=False)
        _save(state)
        emit(state)
        _local.resume = True
        try:
            result = resume(state['resumeKind'])
        finally:
            _local.resume = False
        state.update(phase='driver_done', result=result)
        _save(state)
        return result
    except Exception as exc:
        code = exc.code if isinstance(exc, RuntimeInstallError) else 'DRIVER_UPDATE_FAILED'
        phase = 'driver_unknown' if code == 'DRIVER_STATE_UNKNOWN' else 'driver_failed'
        state.update(phase=phase, errorCode=code, diagnostics=redact(str(exc)))
        if isinstance(exc, RuntimeInstallError) and exc.driver_check:
            state['driverCheck'] = exc.driver_check
        _save(state)
        raise


def public_status(state: dict | None = None) -> dict:
    st = snapshot() if state is None else state
    phase = st.get('phase')
    out = {k: st[k] for k in ('operationId', 'resumeKind', 'rebootRequired', 'driverCheck', 'eula', 'errorCode', 'diagnostics') if k in st}
    if st.get('gpus'):
        current = {gpu.get('name'): gpu for gpu in (out.get('driverCheck') or {}).get('gpus', [])}
        out['driverCheck'] = {**(out.get('driverCheck') or {}),
                              'gpus': [{**current.get(gpu.get('name'), {}), **gpu} for gpu in st['gpus']]}
    if st.get('updates') or st.get('results'):
        out['diagnostics'] = redact('\n'.join(filter(None, [out.get('diagnostics', ''),
            json.dumps({'updates': st.get('updates', []), 'results': st.get('results', [])}, ensure_ascii=False)])))
    out.update(kind='nvidia_driver', stage=phase, running=False, progress=0)
    if phase == 'driver_failed' or phase == 'driver_unknown':
        out['error'] = st.get('errorCode') or 'DRIVER_STATE_UNKNOWN'
    return out


def binding(install_state: dict) -> str:
    st = snapshot()
    value = (st.get('operationId'), st.get('phase'), st.get('fingerprint'),
             install_state.get('kind'), install_state.get('startedAt'), install_state.get('errorCode'))
    return hashlib.sha256(repr(value).encode()).hexdigest()


def reboot() -> None:
    with _lock, driver_mutex():
        assert_idle()
        if snapshot().get('phase') != 'driver_reboot':
            raise RuntimeInstallError('DRIVER_CONFIRMATION_EXPIRED', 'No restart is pending')
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/shutdown.exe'
        # Deliberately omit /f: unsaved applications may prevent Windows restarting.
        subprocess.run([str(executable), '/r', '/t', '0'], check=True, timeout=15)
