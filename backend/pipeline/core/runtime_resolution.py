"""One uv resolution for a Windows profile, exported as verified wheel URLs."""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

from .runtime_download import RuntimeInstallError, Wheel

UV_VERSION = '0.12.15'


def select_wheels(lock: dict) -> list[Wheel]:
    tags = list(cpython_tags((3, 12), ['cp312'], ['win_amd64']))
    tags += list(compatible_tags((3, 12), 'cp312', ['win_amd64']))
    ranks = {tag: rank for rank, tag in enumerate(tags)}
    result = []
    for package in lock.get('packages', []):
        candidates = []
        for wheel in package.get('wheels', []):
            url = wheel.get('url', '')
            filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
            if Path(filename).name != filename or '\\' in filename or urlsplit(url).scheme != 'https':
                continue
            try:
                name, version, _, wheel_tags = parse_wheel_filename(filename)
            except ValueError:
                continue
            if name != package['name'] or str(version) != package['version']:
                continue
            matching = [ranks[tag] for tag in wheel_tags if tag in ranks]
            digest = wheel.get('hashes', {}).get('sha256', '')
            if matching and re.fullmatch('[0-9a-f]{64}', digest):
                candidates.append((min(matching), Wheel(name, str(version), filename, url, digest, wheel.get('size'))))
        if not candidates:
            raise RuntimeInstallError('DEPENDENCY_RESOLUTION_FAILED', f'No hashed Windows cp312 wheel: {package.get("name")}', retryable=False)
        result.append(min(candidates, key=lambda item: item[0])[1])
    if not result:
        raise RuntimeInstallError('DEPENDENCY_RESOLUTION_FAILED', 'Empty runtime lock', retryable=False)
    if len({w.name for w in result}) != len(result):
        raise RuntimeInstallError('DEPENDENCY_RESOLUTION_FAILED', 'Multiple versions in platform lock', retryable=False)
    return result


def resolve(uv: str, profile: str, core: tuple[str, ...], root: Path, run, emit) -> list[Wheel]:
    cuda = profile.startswith('nvidia-')
    torch_backend = profile.removeprefix('nvidia-') if cuda else 'cpu'
    torch_version = '2.7.1' if torch_backend == 'cu128' else '2.6.0'
    provider = {'cpu': 'onnxruntime==1.20.1', 'directml': 'onnxruntime-directml==1.23.0'}.get(profile, 'onnxruntime-gpu==1.20.2')
    sdk = ('faster-whisper==1.2.1', 'rapidocr-onnxruntime==1.4.4', 'vieneu==3.2.0')
    specs = (*core, f'torch=={torch_version}', f'torchaudio=={torch_version}', provider,
             'sherpa-onnx==1.13.5+cuda12.cudnn9' if cuda else 'sherpa-onnx==1.13.8', *sdk)
    # These SDK distributions declare CPU ORT / duplicate OpenCV / upstream UI
    # extras. The app supplies their used dependencies explicitly in CORE.
    config = '[project]\nname = "zm-runtime"\nversion = "1.0.0"\nrequires-python = "==3.12.*"\n'
    config += 'dependencies = ' + json.dumps(specs) + '\n'
    for spec in (*sdk, *(('sherpa-onnx==1.13.5+cuda12.cudnn9',) if cuda else ())):
        name, version = spec.split('==')
        config += f'\n[[tool.uv.dependency-metadata]]\nname = "{name}"\nversion = "{version}"\nrequires-dist = []\n'
    identity = hashlib.sha256((config + profile + UV_VERSION).encode()).hexdigest()[:24]
    directory = root / 'resolution' / identity
    directory.mkdir(parents=True, exist_ok=True)
    project = directory / 'pyproject.toml'
    project.write_text(config, encoding='utf-8')
    lock = directory / 'pylock.toml'
    emit(20, 'resolve', currentPackage=profile)
    if lock.is_file():
        try:
            return select_wheels(tomllib.loads(lock.read_text(encoding='utf-8')))
        except (ValueError, KeyError, RuntimeInstallError):
            lock.unlink()  # invalid metadata only; cached wheels remain
    command = [uv, 'pip', 'compile', str(project), '--directory', str(directory),
               '--python-version', '3.12.10', '--python-platform', 'x86_64-pc-windows-msvc',
               '--no-python-downloads', '--only-binary', ':all:', '--torch-backend', torch_backend,
               '--format', 'pylock.toml', '--output-file', str(lock)]
    if cuda:
        command += ['--find-links', 'https://k2-fsa.github.io/sherpa/onnx/cuda.html']
    run(command, 'DEPENDENCY_RESOLUTION_FAILED', f'Resolve {profile}')
    return select_wheels(tomllib.loads(lock.read_text(encoding='utf-8')))
