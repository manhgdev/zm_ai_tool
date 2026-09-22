"""Install a hardware-specific Windows runtime alongside the active runtime."""
from __future__ import annotations

import contextlib
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .runtime_active import active_runtime_dir, runtime_home
from .runtime_download import Wheel, RuntimeInstallError, classify_error, download_wheel, redact
from .runtime_resolution import resolve as resolve_wheels

PYTHON_VERSION = '3.12.10'
RECIPE_VERSION = 1
Progress = Callable[[int, str, dict[str, Any]], None]
_LOCK = threading.Lock()
CORE = (
    'ctranslate2==4.6.0', 'av==16.0.1', 'pillow==12.0.0', 'opencv-python-headless==4.12.0.88',
    'numpy==2.2.6', 'huggingface-hub==0.36.0', 'pyyaml==6.0.3', 'sea-g2p==0.8.4',
    'soundfile==0.13.1', 'cffi==2.0.0', 'soxr==1.0.0', 'httpx==0.28.1',
    'tokenizers==0.22.1', 'transformers==4.57.6', 'librosa==0.11.0',
    'safetensors==0.6.2', 'pyclipper==1.3.0.post6', 'shapely==2.1.2', 'six==1.17.0',
    'tqdm==4.67.1',
)
DEMUCS = (
    'demucs==4.0.1', 'dora-search==0.1.12', 'einops==0.8.1', 'julius==0.2.7',
    'lameenc==1.8.1', 'openunmix==1.3.0', 'omegaconf==2.3.0',
    'antlr4-python3-runtime==4.9.3', 'retrying==1.4.2', 'submitit==1.5.3',
    'treetable==0.2.6', 'cloudpickle==3.1.1',
)
# Only these pure-Python source releases may use a build backend. No compiler.
SOURCE_ALLOWED = ('demucs', 'dora-search', 'julius', 'antlr4-python3-runtime')


def detect_profile() -> str:
    from .media import detect_device
    from .accel import nvidia_smi_executable

    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeInstallError('HARDWARE_UNSUPPORTED', 'Windows x64 required', retryable=False)
    device = detect_device()
    kind = device.get('gpuKind')
    if kind == 'nvidia':
        try:
            result = subprocess.run(
                [nvidia_smi_executable(), '--query-gpu=driver_version,compute_cap', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=15,
                creationflags=int(getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
            )
            if result.returncode:
                raise ValueError(result.stderr or result.stdout)
            rows = [line.split(',') for line in result.stdout.splitlines() if line.strip()]
            drivers = [tuple(int(n) for n in row[0].strip().split('.')) for row in rows]
            caps = [float(row[1]) for row in rows]
            if not caps:
                raise ValueError('nvidia-smi returned no GPU')
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
            raise RuntimeInstallError('DRIVER_TOO_OLD', 'Cannot validate NVIDIA driver', retryable=False, diagnostics=str(exc)) from exc
        modern = any(cap >= 10 for cap in caps)
        # CUDA 12.x minor compatibility on Windows starts at 528.33.
        # 551.78 is the driver bundled with CUDA 12.4 Update 1, not its
        # compatibility floor. Actual CUDA/ORT probes still gate activation.
        # https://docs.nvidia.com/cuda/archive/12.4.1/cuda-toolkit-release-notes/
        minimum = (570, 65) if modern else (528, 33)
        if min(caps) < 5 or (modern and min(caps) < 7):
            raise RuntimeInstallError('HARDWARE_UNSUPPORTED', 'No common CUDA profile for these GPUs', retryable=False)
        if any(driver < minimum for driver in drivers):
            detected = ', '.join('.'.join(map(str, driver)) for driver in drivers)
            raise RuntimeInstallError('DRIVER_TOO_OLD', f'NVIDIA requires driver >= {minimum[0]}.{minimum[1]}',
                                      retryable=False, diagnostics=f'Detected NVIDIA driver: {detected}; minimum: {minimum[0]}.{minimum[1]}')
        return 'nvidia-cu128' if modern else 'nvidia-cu124'
    if kind in ('amd', 'intel') and device.get('accel') == 'directml':
        return 'directml'
    if kind == 'none' and not device.get('gpuName'):
        return 'cpu'
    raise RuntimeInstallError('HARDWARE_UNSUPPORTED', 'GPU cannot be identified safely', retryable=False, diagnostics=json.dumps(device))


@contextlib.contextmanager
def transaction():
    # Reject concurrent install/rollback instead of queuing stale selections.
    if not _LOCK.acquire(blocking=False):
        raise RuntimeInstallError('RUNTIME_BUSY', 'Runtime installation/rollback already running')
    handle = None
    locked = False
    try:
        # OS lock also protects two independently launched desktop processes.
        if os.name == 'nt':
            import msvcrt
            root = runtime_home() / 'runtime'
            root.mkdir(parents=True, exist_ok=True)
            handle = (root / 'install.lock').open('a+b')
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                raise RuntimeInstallError('RUNTIME_BUSY', 'Another application is changing the runtime') from exc
        yield
    finally:
        try:
            if handle:
                try:
                    if locked:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    handle.close()
        finally:
            _LOCK.release()


def _metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _python(root: Path) -> Path:
    return root / 'Scripts/python.exe'


def _probe(root: Path, profile: str, demucs: bool = False) -> str:
    from . import runtime_probe
    from .system_check.install import _runtime_subprocess_env
    command = [str(_python(root)), '-I', '-c', runtime_probe.PROBE_SOURCE, profile]
    if demucs:
        command.append('--demucs')
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180,
                                env=_runtime_subprocess_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeInstallError('RUNTIME_PROBE_FAILED', 'Cannot execute runtime Python', diagnostics=str(exc)) from exc
    output = (result.stdout + '\n' + result.stderr)[-12000:]
    if result.returncode:
        raise RuntimeInstallError('RUNTIME_PROBE_FAILED', 'Runtime probe failed', diagnostics=output)
    return output


def _run(command: list[str], error: str, label: str, progress=None) -> None:
    from .system_check.install import _pip_stream, _install_log_fn
    if _install_log_fn:
        _install_log_fn(subprocess.list2cmdline(command) + '\n')
    temporary = runtime_home() / 'runtime' / 'tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    try:
        result = _pip_stream(command, timeout=2700, idle_timeout=300, progress=progress, environment={
            'UV_PYTHON_INSTALL_DIR': str(runtime_home() / 'runtime' / 'python'),
            'UV_CACHE_DIR': str(runtime_home() / 'runtime' / 'cache'),
            'PIP_CACHE_DIR': str(runtime_home() / 'runtime' / 'pip-cache'),
            'TEMP': str(temporary),
            'TMP': str(temporary),
            'TMPDIR': str(temporary),
            # Bound disk concurrency independently from downloads. Do not run
            # multiple resolvers/writers against the same environment.
            'UV_CONCURRENT_DOWNLOADS': '16',
            'UV_CONCURRENT_INSTALLS': str(min(8, max(2, os.cpu_count() or 2))),
        })
    except (OSError, RuntimeError) as exc:
        code, retryable = classify_error(exc, error)
        raise RuntimeInstallError(code, f'{label} failed', retryable=retryable, diagnostics=str(exc)) from exc
    if result.returncode:
        output = (result.stdout or '') + '\n' + (result.stderr or '')
        if any(term in output.lower() for term in ('os error 112', 'winerror 112', 'no space left', 'not enough space on the disk', 'errno 28')):
            raise RuntimeInstallError('DISK_FULL', f'Insufficient disk space at {runtime_home()}',
                                      retryable=False, diagnostics=output[-12000:])
        code, retryable = classify_error(RuntimeError(output), error)
        raise RuntimeInstallError(code, f'{label} failed', retryable=retryable, diagnostics=output[-12000:])


def _prepare(uv: str, root: Path) -> Path:
    from .system_check.install import _runtime_subprocess_env, _install_log_fn
    # Use the concrete patch installation, never uv's minor-version junction.
    python = runtime_home() / 'runtime' / 'python' / f'cpython-{PYTHON_VERSION}-windows-x86_64-none' / 'python.exe'

    def usable() -> bool:
        if not python.is_file():
            return False
        try:
            result = subprocess.run(
                [str(python), '-I', '-c',
                 "import sys,ssl,venv,struct; assert sys.version_info[:3] == (3,12,10); assert struct.calcsize('P') == 8"],
                capture_output=True, text=True, timeout=30, env=_runtime_subprocess_env(),
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if not usable():
        try:
            _run([uv, 'python', 'install', PYTHON_VERSION, '--no-bin', '--no-registry'],
                 'PYTHON_PREPARE_FAILED', 'Python download')
        except RuntimeInstallError as exc:
            # uv creates a minor-version link even with --no-bin. Windows can
            # reject that junction after the concrete interpreter is installed.
            detail = exc.diagnostics.lower()
            if not ('minor version link' in detail and 'os error 448' in detail and usable()):
                raise
            if _install_log_fn:
                _install_log_fn('Windows blocked the optional Python minor-version link; verified patch interpreter will be used directly.\n')
        if not usable():
            raise RuntimeInstallError('PYTHON_PREPARE_FAILED', 'Downloaded Python failed its version/stdlib probe')
    # CPython copies its Windows launchers and records the real patch home;
    # uv's managed-minor junction is not involved in creating/querying this venv.
    _run([str(python), '-I', '-m', 'venv', '--copies', str(root)],
         'PYTHON_PREPARE_FAILED', 'Python environment')
    _run([str(_python(root)), '-I', '-c',
          "import sys,ssl,pip; assert sys.version_info[:3] == (3,12,10)"],
         'PYTHON_PREPARE_FAILED', 'Python environment verification')
    return _python(root)


def _download_workers() -> int:
    # Bound sockets and temporary buffers on low-RAM clients; no extra dependency.
    workers = min(16, max(4, (os.cpu_count() or 2) * 2))
    if os.name == 'nt':
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page', 'availablePage', 'virtual', 'availableVirtual', 'extended')
            ]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            if status.available < 2 * 1024**3:
                workers = min(workers, 4)
            elif status.available < 4 * 1024**3:
                workers = min(workers, 8)
    return workers


def _prefetch_packages(wheels: list[Wheel], groups: list[dict[str, Any]], emit, on_ready) -> None:
    """A single hash cache and one writer, with concurrent resumable downloads."""
    from .system_check.install import _install_log_fn
    root = runtime_home() / 'runtime'
    cache = root / 'wheels'
    cache.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    guard = threading.Lock()
    state = {w.name: {'received': 0, 'total': w.size, 'cache': False, 'attempt': 0, 'activity': 'queued'} for w in wheels}
    activity = {'phase': 'download', 'group': ''}
    installed: set[str] = set()
    downloads: dict[str, Path] = {}
    received_network = 0
    def report(wheel, received, total, cached, attempt, status, diagnostics=''):
        nonlocal received_network
        with guard:
            old = state[wheel.name]
            if not cached and old['activity'] not in ('queued', 'retrying') and received >= old['received']:
                received_network += received - old['received']
            state[wheel.name] = {'received': received, 'total': total, 'cache': cached, 'attempt': attempt, 'activity': status}
        if diagnostics and _install_log_fn:
            _install_log_fn(redact(diagnostics) + '\n')

    def monitor():
        last_bytes, last_time = 0, time.monotonic()
        while not stop.wait(.5):
            with guard:
                snapshot = {name: dict(item) for name, item in state.items()}
                phase, group = activity['phase'], activity['group']
                network_bytes = received_network
                progress = 30 + int(44 * len(installed) / max(1, len(groups)))
            now = time.monotonic()
            speed = max(0, network_bytes - last_bytes) / max(.01, now - last_time)
            last_bytes, last_time = network_bytes, now
            total = sum(item['total'] for item in snapshot.values()) if all(item['total'] is not None for item in snapshot.values()) else None
            received = sum(item['received'] for item in snapshot.values())
            active = [name for name, item in snapshot.items() if item['activity'] not in ('complete', 'cached')]
            emit(progress, phase, currentPackage=group or ', '.join(active[:3]), downloadedBytes=received,
                 totalBytes=total, speedBytesPerSecond=round(speed), etaSeconds=round(max(0, total - received) / speed) if total is not None and speed > 0 else None,
                 cacheHit=sum(item['cache'] for item in snapshot.values()),
                 downloadItems=[{'package': name, **item} for name, item in snapshot.items()])

    monitor_thread = threading.Thread(target=monitor, daemon=True, name='runtime-progress')
    monitor_thread.start()
    try:
        workers = _download_workers()
        if shutil.disk_usage(root).free < 8 * 1024**3:
            workers = min(workers, 4)
        # Start the largest known artifacts first; unknown-sized Torch wheels
        # also start immediately so small-package metadata never delays them.
        ordered = sorted(wheels, key=lambda w: (w.name == 'torch', w.size or 0), reverse=True)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ai-download') as pool:
            futures = {pool.submit(download_wheel, wheel, cache, report, stop): wheel for wheel in ordered}
            try:
                while futures or len(installed) < len(groups):
                    done, _ = wait(futures, timeout=.5, return_when=FIRST_COMPLETED) if futures else (set(), set())
                    for future in done:
                        wheel = futures.pop(future)
                        downloads[wheel.name] = future.result()
                    for group in groups:
                        name = group['name']
                        if name in installed or not set(group.get('requires', ())) <= installed:
                            continue
                        if not set(group['packages']) <= downloads.keys():
                            continue
                        paths = [downloads[package] for package in group['packages']]
                        # Exact extracted size is available from verified wheel ZIPs.
                        unpacked = 0
                        for path in paths:
                            with zipfile.ZipFile(path) as bundle:
                                unpacked += sum(entry.file_size for entry in bundle.infolist())
                        remaining = sum(max(0, (state[w.name]['total'] or 0) - state[w.name]['received']) for w in futures.values())
                        required = 2 * unpacked + remaining + 1024**3  # installer extraction + copy + reserve
                        free = shutil.disk_usage(root).free
                        emit(30 + int(44 * len(installed) / len(groups)), 'install',
                             currentPackage=name, requiredDiskBytes=required, freeDiskBytes=free, diskDrive=root.anchor)
                        if free < required:
                            raise RuntimeInstallError('DISK_FULL', f'Not enough disk space at {root}', retryable=False,
                                diagnostics=json.dumps({'disk': str(root), 'requiredBytes': required, 'freeBytes': free}))
                        with guard:
                            activity.update(phase='install', group=name)
                        on_ready(group, paths)
                        with guard:
                            installed.add(name)
                            activity.update(phase='download', group='')
                    if not futures and len(installed) != len(groups):
                        raise RuntimeInstallError('DEPENDENCY_RESOLUTION_FAILED', 'Unresolved group prerequisites', retryable=False)
            except Exception:
                stop.set()
                for future in futures:
                    future.cancel()
                raise
    finally:
        stop.set()
        monitor_thread.join(timeout=2)
    received = sum(item['received'] for item in state.values())
    emit(74, 'install', downloadedBytes=received, totalBytes=received,
         speedBytesPerSecond=0, etaSeconds=0, currentPackage='')


def _install_packages(uv: str, python: Path, profile: str, demucs: bool, emit) -> None:
    root = runtime_home() / 'runtime'
    # uv compile is the only resolver. The installer consumes exact verified
    # wheel paths with --no-deps; CPU ORT can never overwrite CUDA/DirectML.
    wheels = None
    for attempt in range(1, 4):
        try:
            wheels = resolve_wheels(uv, profile, CORE, root, _run, emit)
            break
        except RuntimeInstallError as exc:
            if not exc.retryable or attempt == 3:
                raise
            emit(20, 'resolve', currentPackage=profile, retryAttempt=attempt, retryAfterSeconds=2 ** attempt)
            time.sleep(2 ** attempt)
    assert wheels is not None
    by_name = {wheel.name: wheel for wheel in wheels}
    sdk = {'faster-whisper', 'rapidocr-onnxruntime', 'vieneu'}
    torch = {'torch', 'torchaudio'}
    sherpa = {'sherpa-onnx'} if profile.startswith('nvidia-') else set()
    groups = [
        {'name': 'Core / ONNX', 'packages': sorted(by_name.keys() - sdk - torch - sherpa)},
        {'name': 'Torch', 'packages': sorted(torch), 'requires': ['Core / ONNX']},
        {'name': 'Whisper / OCR / VieNeu', 'packages': sorted(sdk), 'requires': ['Core / ONNX', 'Torch']},
    ]
    if sherpa:
        groups.append({'name': 'Sherpa CUDA', 'packages': sorted(sherpa), 'requires': ['Core / ONNX', 'Torch']})
    pip_base = [str(python), '-I', '-m', 'pip', '--isolated', 'install',
                '--cache-dir', str(root / 'cache'), '--disable-pip-version-check', '--no-input', '--no-compile']
    uv_base = [uv, 'pip', 'install', '--python', str(python), '--link-mode', 'copy']
    use_pip = False
    requirements_dir = root / 'requirements'
    requirements_dir.mkdir(parents=True, exist_ok=True)
    def install(paths, label):
        nonlocal use_pip
        requirement_file = requirements_dir / (re.sub(r'[^A-Za-z0-9_.-]+', '_', label) + '.txt')
        requirement_file.write_text('\n'.join(str(path) for path in paths) + '\n', encoding='utf-8')
        command = ['--no-index', '--no-deps', '--requirement', str(requirement_file)]
        try:
            _run([*(pip_base if use_pip else uv_base), *command], 'DEPENDENCY_INSTALL_FAILED', label)
        except RuntimeInstallError as exc:
            if use_pip or 'os error 448' not in exc.diagnostics.lower() or 'interpreter' not in exc.diagnostics.lower():
                raise
            use_pip = True
            _run([*pip_base, *command], 'DEPENDENCY_INSTALL_FAILED', label)
    def install_ready(group, paths):
        install(paths, group['name'])
    _prefetch_packages(wheels, groups, emit, install_ready)
    if demucs:
        emit(75, 'install', currentPackage='Demucs')
        demucs_command = ['--only-binary', ':all:', '--no-binary', ','.join(SOURCE_ALLOWED), '--no-deps', *DEMUCS]
        try:
            _run([*(pip_base if use_pip else uv_base), *demucs_command], 'DEPENDENCY_INSTALL_FAILED', 'Demucs')
        except RuntimeInstallError as exc:
            if use_pip or 'os error 448' not in exc.diagnostics.lower() or 'interpreter' not in exc.diagnostics.lower():
                raise
            _run([*pip_base, *demucs_command], 'DEPENDENCY_INSTALL_FAILED', 'Demucs')


def _write_pointer(root: Path, name: str, data: dict[str, Any]) -> None:
    temporary = root / (name + '.tmp')
    try:
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        os.replace(temporary, root / name)
    except OSError as exc:
        raise RuntimeInstallError('ACTIVATION_FAILED', 'Cannot switch runtime pointer', diagnostics=str(exc)) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def install_runtime(progress: Progress | None = None, *, demucs: bool = False) -> dict[str, Any]:
    from .system_check.install import _find_uv
    if sys.platform != 'win32':
        raise RuntimeInstallError('HARDWARE_UNSUPPORTED', 'Windows installer only', retryable=False)
    with transaction():
        def emit(value, stage, **fields):
            if progress:
                progress(value, '', {'stage': stage, 'runtimeProfile': profile,
                                    'runtimePack': profile, 'currentPackage': '', **fields})
        profile = ''
        emit(2, 'detect_hardware')
        profile = detect_profile()
        root = runtime_home() / 'runtime'
        current = active_runtime_dir()
        metadata = _metadata(root / 'current.json')
        # Keep existing optional features on AI/OCR reinstall.
        demucs = demucs or 'demucs' in metadata.get('features', []) or bool(list((current / 'Lib/site-packages').glob('demucs-*.dist-info')))
        emit(5, 'probe')
        if _python(current).is_file():
            try:
                diagnostics = _probe(current, profile, demucs)
                return {'ok': True, 'message': 'Runtime ready', 'runtimeProfile': profile,
                        'runtimePack': profile, 'diagnostics': diagnostics, 'needsRestart': False}
            except RuntimeInstallError:
                pass
        uv = _find_uv()
        if not uv:
            raise RuntimeInstallError('PYTHON_PREPARE_FAILED', 'Application is missing bundled uv', retryable=False)
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < (10 if profile.startswith('nvidia-') else 4) * 1024**3:
            raise RuntimeInstallError('DISK_FULL', 'Not enough free space for a parallel runtime', retryable=False)
        candidate = Path(tempfile.mkdtemp(prefix='env-', dir=root))
        try:
            emit(15, 'prepare_python', currentPackage=f'Python {PYTHON_VERSION}')
            python = _prepare(uv, candidate)
            _install_packages(uv, python, profile, demucs, emit)
            emit(85, 'probe')
            diagnostics = _probe(candidate, profile, demucs)
            (candidate / '.app-runtime.json').write_text(json.dumps({'recipe': RECIPE_VERSION}), encoding='utf-8')
            # Legacy venv remains usable as a rollback target too.
            previous = metadata or {'path': '../.venv-runtime', 'runtimePack': profile}
            if current.is_dir() and _python(current).is_file():
                _write_pointer(root, 'previous.json', previous)
            emit(95, 'activate')
            _write_pointer(root, 'current.json', {
                'schemaVersion': 1, 'recipeVersion': RECIPE_VERSION, 'path': candidate.name,
                'runtimeProfile': profile, 'runtimePack': profile,
                'features': ['demucs'] if demucs else [], 'activatedAt': time.time(),
            })
        except Exception:
            # No active runtime is modified, including on antivirus/file lock.
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        emit(100, 'activate')
        _prune(root)
        return {'ok': True, 'message': 'Runtime installed', 'needsRestart': True,
                'runtimeProfile': profile, 'runtimePack': profile, 'diagnostics': diagnostics}


def _prune(root: Path) -> None:
    """Remove only marked environments created by this installer, after activation."""
    keep = {(root / str(_metadata(root / name).get('path', ''))).resolve()
            for name in ('current.json', 'previous.json')}
    for candidate in root.glob('env-*'):
        if (candidate.is_symlink() or candidate.resolve() in keep or
                not (candidate / '.app-runtime.json').is_file()):
            continue
        # Locked workers may still own old DLLs; leave them for a later install.
        shutil.rmtree(candidate, ignore_errors=True)


def rollback_runtime(progress: Progress | None = None) -> dict[str, Any]:
    with transaction():
        root = runtime_home() / 'runtime'
        previous = _metadata(root / 'previous.json')
        relative = previous.get('path')
        if not isinstance(relative, str) or not relative:
            raise RuntimeInstallError('ACTIVATION_FAILED', 'No valid previous runtime', retryable=False)
        target = (root / relative).resolve()
        legacy = (runtime_home() / '.venv-runtime').resolve()
        if not ((root.resolve() in target.parents) or target == legacy):
            raise RuntimeInstallError('ACTIVATION_FAILED', 'Unsafe rollback path', retryable=False)
        profile = str(previous.get('runtimeProfile') or previous.get('runtimePack') or 'cpu').removeprefix('gpu-')
        if progress:
            progress(10, '', {'stage': 'rollback', 'runtimeProfile': profile})
        _probe(target, profile, 'demucs' in previous.get('features', []))
        current = _metadata(root / 'current.json')
        _write_pointer(root, 'current.json', previous)
        if current:
            _write_pointer(root, 'previous.json', current)
        return {'ok': True, 'message': 'Runtime restored', 'needsRestart': True}
