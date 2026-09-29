"""Optional OpenVoice V2 converter, isolated from the VieNeu environment."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from ...core.config import DATA
from .openvoice_worker import SCRIPT

REVISION = '74a1d147b17a8c3092dd5430504bd83ef6c7eb23'
MODEL_REVISION = 'f36e7edfe1684461a8343844af60babc2efbb727'
CHECKPOINT_SHA = '9652c27e92b6b2a91632590ac9962ef7ae2b712e5c5b7f4c34ec55ee2b37ab9e'
VERSION = f'openvoice-v2:{REVISION}:{CHECKPOINT_SHA}:pcm-v1'
ROOT = DATA / 'openvoice-v2'
_lock = threading.Lock()
_install_lock = threading.Lock()
_process = None
_responses = None
_install_state = 'missing'


def _environment():
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONIOENCODING='utf-8', VIRTUAL_ENV=str(ROOT / 'venv'))
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    return env


def python_path():
    return ROOT / 'venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def status():
    try:
        marker = json.loads((ROOT / 'ready.json').read_text(encoding='utf-8'))
        ready = python_path().is_file() and marker.get('version') == VERSION and all((ROOT / file).is_file() for file in ('checkpoint.pth', 'config.json'))
    except (OSError, ValueError):
        ready = False
    return {'id': 'openvoice', 'name': 'OpenVoice V2', 'local': True,
            'installed': ready, 'ready': ready, 'loadState': _install_state if not ready else 'ready',
            'loaded': _process is not None and _process.poll() is None}


def _download(url, path, digest):
    pending = path.with_suffix(path.suffix + '.tmp')
    try:
        with urllib.request.urlopen(url, timeout=120) as response, pending.open('wb') as out:
            shutil.copyfileobj(response, out)
        if hashlib.sha256(pending.read_bytes()).hexdigest() != digest:
            raise RuntimeError('OPENVOICE_CHECKSUM_FAILED')
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _install(progress):
    global _install_state
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        progress(2)
        with (ROOT / 'install.log').open('a', encoding='utf-8') as log:
            interpreter = sys.executable
            if getattr(sys, 'frozen', False):
                from ...core.runtime_active import runtime_python
                interpreter = str(runtime_python())
            def run(args):
                subprocess.run(args, check=True, stdout=log, stderr=log, timeout=1800,
                               env=_environment(),
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if not python_path().is_file():
                run([interpreter, '-m', 'venv', str(ROOT / 'venv')])
            progress(10)
            archive = ROOT / 'source.zip'
            _download(f'https://codeload.github.com/myshell-ai/OpenVoice/zip/{REVISION}', archive,
                      'd08cbc84f4ec7abc76f9dddb5bbb221e906e49cfe5febd37133152bdeacb8be4')
            with zipfile.ZipFile(archive) as bundle:
                for item in bundle.infolist():
                    destination = (ROOT / item.filename).resolve()
                    if ROOT.resolve() not in destination.parents:
                        raise RuntimeError('OPENVOICE_INVALID_ARCHIVE')
                bundle.extractall(ROOT)
            for name, digest in [('checkpoint.pth', CHECKPOINT_SHA), ('config.json', '9dfff60350b8c63f2c664efd92a61b2516efb22671466960f0e5dfebd881fa47')]:
                _download(f'https://huggingface.co/myshell-ai/OpenVoiceV2/resolve/{MODEL_REVISION}/converter/{name}', ROOT / name, digest)
            progress(35)
            # Only imports used by the converter; upstream setup.py pins obsolete
            # NumPy and brings ASR/UI dependencies that must not touch VieNeu.
            run([str(python_path()), '-m', 'pip', 'install', 'torch==2.6.0', 'numpy==1.26.4',
                 'librosa==0.11.0', 'soundfile==0.13.1', 'eng-to-ipa==0.0.2', 'inflect==7.0.0',
                 'Unidecode==1.3.8', 'pypinyin==0.50.0', 'cn2an==0.5.22', 'jieba==0.42.1', 'langid==1.1.6'])
            progress(85)
            with _lock:
                _request({'op': 'probe'}, require_ready=False)
            pending = ROOT / 'ready.tmp'
            pending.write_text(json.dumps({'version': VERSION}), encoding='utf-8')
            pending.replace(ROOT / 'ready.json')
        _install_state = 'ready'
        progress(100)
    except Exception:
        _install_state = 'failed'
        progress(0, 'OPENVOICE_INSTALL_FAILED')
        import logging
        logging.getLogger(__name__).exception('OpenVoice installation failed')
    finally:
        _install_lock.release()


def install(progress=None):
    global _install_state
    progress = progress or (lambda pct, error=None: None)
    if status()['ready']:
        progress(100)
        return status()
    if not _install_lock.acquire(blocking=False):
        raise RuntimeError('OPENVOICE_INSTALL_BUSY')
    _install_state = 'installing'
    threading.Thread(target=_install, args=(progress,), daemon=True, name='openvoice-install').start()
    return status()


def _stop():
    global _process, _responses
    process, _process = _process, None
    _responses = None
    if process is not None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()


atexit.register(_stop)


def _request(payload, cancel_check=None, on_progress=None, require_ready=True):
    global _process, _responses
    if require_ready and not status()['ready']:
        raise RuntimeError('OPENVOICE_NOT_INSTALLED')
    if _process is None or _process.poll() is not None:
        _stop()
        env = dict(_environment(), PYTHONPATH=str(ROOT / f'OpenVoice-{REVISION}'))
        with (ROOT / 'worker.log').open('a', encoding='utf-8') as log:
            _process = subprocess.Popen([str(python_path()), '-u', '-c', SCRIPT, str(ROOT), VERSION],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                        text=True, encoding='utf-8', env=env,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _responses = queue.Queue()
        def read(process, responses):
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except Exception:
                pass
            finally:
                responses.put({'ok': False, 'error': 'OPENVOICE_WORKER_FAILED'})
        threading.Thread(target=read, args=(_process, _responses), daemon=True).start()
    try:
        _process.stdin.write(json.dumps(payload) + '\n')
        _process.stdin.flush()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if cancel_check and cancel_check():
                raise RuntimeError('OPENVOICE_CANCELLED')
            try:
                result = _responses.get(timeout=0.2)
            except queue.Empty:
                continue
            if 'progress' in result:
                if on_progress:
                    on_progress(result['progress'])
                continue
            if not result.get('ok'):
                raise RuntimeError(result.get('error', 'OPENVOICE_WORKER_FAILED'))
            return result
        raise RuntimeError('OPENVOICE_TIMEOUT')
    except Exception:
        _stop()
        raise


def reference(voice):
    from . import vieneu
    parsed = vieneu.parse_voice(voice)
    if parsed and parsed[0] == 'remote-reference':
        # ZMTTS voices are listed lazily. Materialize the demo only when a
        # synthesis/cache fingerprint actually needs reference audio.
        vieneu._ensure_remote_reference(parsed[1])
    path = vieneu.preview_path(voice)
    if path is None:
        raise ValueError('OPENVOICE_REFERENCE_MISSING')
    return path


def cache_token(voice, route):
    path = reference(voice)
    return VERSION + ':' + route['sourceVoice'] + ':' + hashlib.sha256(path.read_bytes()).hexdigest()


def synthesize(text, voice, output, route, cancel_check=None, on_progress=None):
    from ..manager import _capcut_tts, _cc_parse
    from ..eleven import _el_tts, _el_voice_id
    if not status()['ready']:
        raise RuntimeError('OPENVOICE_NOT_INSTALLED')
    ref = reference(voice)
    deadline = time.monotonic() + 600
    while not _lock.acquire(timeout=0.2):
        if cancel_check and cancel_check():
            raise RuntimeError('OPENVOICE_CANCELLED')
        if time.monotonic() > deadline:
            raise RuntimeError('OPENVOICE_TIMEOUT')
    try:
        if cancel_check and cancel_check():
            raise RuntimeError('OPENVOICE_CANCELLED')
        with tempfile.TemporaryDirectory(prefix='openvoice-', dir=output.parent) as temp:
            source, converted = Path(temp) / 'source.wav', Path(temp) / 'converted.wav'
            if on_progress:
                on_progress(0.05)
            source_voice = route['sourceVoice']
            parsed_capcut = _cc_parse(source_voice)
            if parsed_capcut:
                _capcut_tts(text, *parsed_capcut, source, cancel_check=cancel_check)
            else:
                eleven_id = _el_voice_id(source_voice)
                if not eleven_id:
                    raise RuntimeError('TTS_SOURCE_UNAVAILABLE')
                if cancel_check and cancel_check():
                    raise RuntimeError('OPENVOICE_CANCELLED')
                _el_tts(text, eleven_id, source, lang=route.get('language'))
            _request({'source': str(source.resolve()), 'reference': str(ref.resolve()),
                      'output': str(converted.resolve())}, cancel_check, on_progress)
            if cancel_check and cancel_check():
                raise RuntimeError('OPENVOICE_CANCELLED')
            converted.replace(output)
            metadata = Path(temp) / 'metadata.json'
            metadata.write_text(json.dumps({**route, 'text': text, 'cacheToken': cache_token(voice, route)}), encoding='utf-8')
            metadata.replace(output.with_suffix('.openvoice.json'))
            if on_progress:
                on_progress(1.0)
    finally:
        _lock.release()


def detect_language(text):
    if not status()['ready']:
        raise RuntimeError('OPENVOICE_NOT_INSTALLED')
    # Separate short-lived CPU process keeps optional detection dependencies out
    # of the API environment. A suggestion never authorizes synthesis.
    result = subprocess.run([str(python_path()), '-c',
        'import json,sys,langid; print(json.dumps({"language":langid.classify(sys.stdin.read())[0]}))'],
        input=text[:10000], text=True, encoding='utf-8', capture_output=True, timeout=30, env=_environment(),
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
    return {**json.loads(result.stdout), 'requiresConfirmation': True}
