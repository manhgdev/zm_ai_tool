"""Stable, hashed APP machine identity. Raw platform identifiers stay on the machine."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

def _command(args):
    return subprocess.check_output(args, text=True, encoding='utf-8', errors='replace',
                                   stderr=subprocess.DEVNULL, timeout=8).strip()


@lru_cache(maxsize=1)
def machine_id():
    system = platform.system()
    try:
        if system == 'Darwin':
            output = _command(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'])
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
            value = str(uuid.UUID(match[1])) if match else ''
        elif system == 'Windows':
            value = str(uuid.UUID(_command([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                '(Get-CimInstance Win32_ComputerSystemProduct).UUID',
            ])))
        elif system == 'Linux':
            value = ''
            for path in ('/sys/class/dmi/id/product_uuid', '/etc/machine-id'):
                try:
                    value = str(uuid.UUID(Path(path).read_text().strip()))
                    if uuid.UUID(value).int not in (0, 2**128 - 1):
                        break
                except (OSError, ValueError):
                    continue
        else:
            value = ''
        if not value or uuid.UUID(value).int in (0, 2**128 - 1):
            raise ValueError()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('DEVICE_ID_UNAVAILABLE') from None
    # ponytail: platform UUIDs identify honest clients, not hardware attestation.
    # A patched client/VM can spoof them; stronger protection needs platform attestation.
    return hashlib.sha256(f'zm-ai-tool:machine:v1:{system}:{value}'.encode()).hexdigest()


@lru_cache(maxsize=1)
def desktop_device_type():
    """Use native chassis/model information before falling back to the battery."""
    try:
        system = platform.system()
        if system == 'Darwin':
            hardware = json.loads(_command(['/usr/sbin/system_profiler', 'SPHardwareDataType', '-json']))
            model = hardware['SPHardwareDataType'][0].get('machine_name', '').replace(' ', '').lower()
            if 'macbook' in model:
                return 'laptop'
            if model in ('imac', 'imacpro', 'macmini', 'macstudio', 'macpro'):
                return 'pc'
            chassis = set()
        elif system == 'Windows':
            chassis = {int(value) for value in _command([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                'Get-CimInstance Win32_SystemEnclosure | ForEach-Object { $_.ChassisTypes }',
            ]).split()}
        elif system == 'Linux':
            chassis = {int(Path('/sys/class/dmi/id/chassis_type').read_text().strip())}
        else:
            chassis = set()
        if chassis & {8, 9, 10, 11, 14, 30, 31, 32}:
            return 'laptop'
        if chassis & {3, 4, 5, 6, 7, 13, 15, 16, 17, 23, 24, 35, 36}:
            return 'pc'
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    # ponytail: unknown chassis uses a battery heuristic (UPS can mislead it);
    # extend native model/chassis mappings if a platform needs more precise detection.
    try:
        import psutil
        return 'laptop' if psutil.sensors_battery() is not None else 'pc'
    except (OSError, AttributeError, NotImplementedError):
        return 'pc'
