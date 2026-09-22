"""Inventory exactly the release payload; run with the build Python on Windows."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def inventory(bundle: Path, installer: Path, portable: Path) -> dict:
    files = []
    for path in sorted(bundle.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Unexpected payload symlink: {path}')
        if path.is_file():
            files.append({'path': path.relative_to(bundle).as_posix(), 'size': path.stat().st_size,
                          'sha256': digest(path), 'binary': path.suffix.lower() in ('.exe', '.dll', '.pyd')})
    if not any(item['path'] == 'ZM AI TOOL.exe' for item in files):
        raise ValueError('Main application EXE is missing')
    return {'schemaVersion': 1, 'signaturePolicy': 'Unsigned unless Authenticode report says Valid; not a safety verdict',
            'python': platform.python_version(), 'files': files,
            'assets': [{'name': path.name, 'size': path.stat().st_size, 'sha256': digest(path)} for path in (installer, portable)]}


def main():
    parser = argparse.ArgumentParser()
    for key in ('bundle', 'installer', 'portable', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = inventory(args.bundle, args.installer, args.portable)
    (args.output / 'windows-artifact-manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (args.output / 'windows-SHA256SUMS.txt').write_text(''.join(f"{a['sha256']}  {a['name']}\n" for a in report['assets']), encoding='utf-8')
    # Build environment inventory is explicitly separate from packaged files;
    # a package installed during build need not be present in the frozen app.
    packages = sorted([{'name': d.metadata['Name'], 'version': d.version} for d in importlib.metadata.distributions()
                       if d.metadata['Name']], key=lambda p: p['name'].lower())
    (args.output / 'windows-build-environment.json').write_text(json.dumps({'python': platform.python_version(), 'packages': packages}, indent=2), encoding='utf-8')
    components = [{'type': 'file', 'name': f['path'], 'hashes': [{'alg': 'SHA-256', 'content': f['sha256']}]} for f in report['files']]
    sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'version': 1,
            'metadata': {'component': {'type': 'application', 'name': 'ZM AI TOOL'},
                         'properties': [{'name': 'scope', 'value': 'Exact bundled files. Python build packages listed separately; dependency analysis is incomplete.'}]},
            'components': components, 'compositions': [{'aggregate': 'incomplete'}]}
    (args.output / 'windows-sbom.cdx.json').write_text(json.dumps(sbom, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
