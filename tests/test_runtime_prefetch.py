import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import runtime_install as runtime


class PrefetchTests(unittest.TestCase):
    def test_core_installs_while_torch_downloads_and_sdk_waits_for_both(self):
        core_installed = threading.Event()
        order = []
        groups = [
            {'name': 'torch', 'specs': ['torch==1']},
            {'name': 'core', 'specs': ['core==1']},
            {'name': 'sdk', 'specs': ['sdk==1'], 'requires': ['torch', 'core']},
        ]
        def download(command, *args):
            if 'torch==1' in command:
                self.assertTrue(core_installed.wait(3), 'Core installation waited for all downloads')
        def install(group, directory, start, end):
            if group['name'] == 'sdk':
                self.assertEqual(set(order), {'torch', 'core'})
            order.append(group['name'])
            if group['name'] == 'core':
                core_installed.set()
        updates = []
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}), patch.object(runtime, '_run', side_effect=download):
            runtime._prefetch_packages(Path('python.exe'), groups, lambda value, *a, **kw: updates.append(value), on_ready=install)
        self.assertEqual(order, ['core', 'torch', 'sdk'])
        self.assertEqual(updates, sorted(updates))

    def test_download_groups_overlap_without_installing_into_environment(self):
        barrier = threading.Barrier(3)
        commands = []
        def run(command, *args):
            commands.append(command)
            barrier.wait(timeout=3)
        groups = [{'name': str(i), 'specs': [f'package{i}==1.0']} for i in range(3)]
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}), patch.object(runtime, '_run', side_effect=run):
            result = runtime._prefetch_packages(Path('python.exe'), groups, lambda *a, **kw: None)
        self.assertEqual(len(result), 3)
        for command in commands:
            self.assertIn('download', command)
            self.assertNotIn('install', command)
        self.assertEqual(len({c[c.index('--dest') + 1] for c in commands}), 3)

    def test_failed_prefetch_prevents_all_environment_installs(self):
        with patch.object(runtime, '_prefetch_packages', side_effect=runtime.RuntimeInstallError('DOWNLOAD_FAILED', 'offline')), patch.object(runtime, '_run') as run:
            with self.assertRaises(runtime.RuntimeInstallError):
                runtime._install_packages('uv', Path('python.exe'), 'cpu', False, lambda *a, **kw: None)
        run.assert_not_called()
