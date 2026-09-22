import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import runtime_install as runtime


class PrefetchTests(unittest.TestCase):
    def test_core_installs_while_torch_downloads_and_sdk_waits_for_both(self):
        core_installed = threading.Event()
        order = []
        groups = [
            {'name': 'core', 'packages': ['core']},
            {'name': 'torch', 'packages': ['torch'], 'requires': ['core']},
            {'name': 'sdk', 'packages': ['sdk'], 'requires': ['torch', 'core']},
        ]
        wheels = [runtime.Wheel(name, '1', f'{name}.whl', 'https://example.invalid', 'a' * 64, 1) for name in ('core', 'torch', 'sdk')]
        def download(wheel, cache, report, stop):
            if wheel.name == 'torch':
                self.assertTrue(core_installed.wait(3), 'Core installation waited for all downloads')
            path = cache / wheel.filename
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('test', b'1')
            report(wheel, 1, 1, False, 1, 'complete')
            return path
        def install(group, paths):
            if group['name'] == 'sdk':
                self.assertEqual(set(order), {'torch', 'core'})
            order.append(group['name'])
            if group['name'] == 'core':
                core_installed.set()
        updates = []
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}), patch.object(runtime, 'download_wheel', side_effect=download):
            runtime._prefetch_packages(wheels, groups, lambda value, *a, **kw: updates.append(value), on_ready=install)
        self.assertEqual(order, ['core', 'torch', 'sdk'])
        self.assertEqual(updates, sorted(updates))

    def test_download_groups_overlap_without_installing_into_environment(self):
        barrier = threading.Barrier(3)
        commands = []
        def run(wheel, cache, report, stop):
            commands.append(wheel.name)
            barrier.wait(timeout=3)
            path = cache / wheel.filename
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('test', b'1')
            report(wheel, 1, 1, False, 1, 'complete')
            return path
        wheels = [runtime.Wheel(str(i), '1', f'{i}.whl', 'https://example.invalid', 'a' * 64, 1) for i in range(3)]
        groups = [{'name': str(i), 'packages': [str(i)]} for i in range(3)]
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}), patch.object(runtime, 'download_wheel', side_effect=run):
            runtime._prefetch_packages(wheels, groups, lambda *a, **kw: None, lambda *a: None)
        self.assertEqual(len(set(commands)), 3)

    def test_failed_prefetch_prevents_all_environment_installs(self):
        wheel = runtime.Wheel('bad', '1', 'bad.whl', 'https://example.invalid', 'a' * 64, 1)
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}), patch.object(runtime, 'download_wheel', side_effect=runtime.RuntimeInstallError('DNS_FAILED', 'offline')), patch.object(runtime, '_run') as run:
            with self.assertRaises(runtime.RuntimeInstallError):
                runtime._prefetch_packages([wheel], [{'name': 'bad', 'packages': ['bad']}], lambda *a, **kw: None, run)
        run.assert_not_called()
