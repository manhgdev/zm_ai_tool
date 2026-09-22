from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import runtime_install as runtime
from pipeline.core.runtime_active import active_runtime_dir
from pipeline.core.system_check import install


class HardwareTests(unittest.TestCase):
    def test_minor_link_failure_uses_verified_patch_python(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}):
            python = Path(raw) / 'runtime/python/cpython-3.12.10-windows-x86_64-none/python.exe'
            commands = []
            def run(command, *args):
                commands.append(command)
                if command[1:3] == ['python', 'install']:
                    python.parent.mkdir(parents=True)
                    python.write_bytes(b'python')
                    raise runtime.RuntimeInstallError('PYTHON_PREPARE_FAILED', 'install failed',
                        diagnostics='Failed to create Python minor version link directory (os error 448)')
            with patch.object(runtime, '_run', side_effect=run), patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                runtime._prepare('uv', Path(raw) / 'candidate')
            self.assertEqual(commands[1][:5], [str(python), '-I', '-m', 'venv', '--copies'])
            self.assertTrue(commands[-1][0].endswith('python.exe'))

    def test_minor_link_failure_does_not_hide_broken_python(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': raw}), patch.object(
            runtime, '_run', side_effect=runtime.RuntimeInstallError('PYTHON_PREPARE_FAILED', 'install failed',
                diagnostics='Failed to create Python minor version link directory (os error 448)')
        ):
            with self.assertRaises(runtime.RuntimeInstallError):
                runtime._prepare('uv', Path(raw) / 'candidate')

    def detect(self, device, output='', code=0):
        with patch('platform.machine', return_value='AMD64'), patch('pipeline.core.media.detect_device', return_value=device), patch(
            'pipeline.core.accel.nvidia_smi_executable', return_value='nvidia-smi'
        ), patch('subprocess.run', return_value=subprocess.CompletedProcess([], code, output, 'bad driver')):
            return runtime.detect_profile()

    def test_hardware_profiles(self):
        self.assertEqual(self.detect({'gpuKind': 'none'}), 'cpu')
        for kind in ('amd', 'intel'):
            self.assertEqual(self.detect({'gpuKind': kind, 'accel': 'directml'}), 'directml')
        self.assertEqual(self.detect({'gpuKind': 'nvidia'}, '551.78, 8.6'), 'nvidia-cu124')
        self.assertEqual(self.detect({'gpuKind': 'nvidia'}, '570.65, 12.0'), 'nvidia-cu128')

    def test_bad_driver_or_unknown_gpu_never_falls_back(self):
        for device, output, code in [({'gpuKind': 'nvidia'}, '', 1),
                                     ({'gpuKind': 'nvidia'}, '550.0, 8.6', 0),
                                     ({'gpuKind': 'nvidia'}, '565.0, 12.0', 0),
                                     ({'gpuKind': 'other'}, '', 0)]:
            with self.subTest(device=device, output=output), self.assertRaises(runtime.RuntimeInstallError):
                self.detect(device, output, code)

    def test_profile_commands_have_one_torch_and_one_provider(self):
        for profile in ('cpu', 'directml', 'nvidia-cu124', 'nvidia-cu128'):
            commands = []
            with patch.object(runtime, '_run', side_effect=lambda command, *args: commands.append(command)):
                runtime._install_packages('uv', Path('candidate/python.exe'), profile, True, lambda *a, **kw: None)
            torch_commands = [c for c in commands if any(s.startswith('torch==') for s in c)]
            self.assertEqual(len(torch_commands), 1)
            expected_index = profile.removeprefix('nvidia-') if profile.startswith('nvidia-') else 'cpu'
            self.assertIn(f'https://download.pytorch.org/whl/{expected_index}', torch_commands[0])
            ort = [s for c in commands for s in c if s.startswith(('onnxruntime==', 'onnxruntime-gpu==', 'onnxruntime-directml=='))]
            self.assertEqual(len(ort), 1)
            if profile == 'directml':
                self.assertTrue(ort[0].startswith('onnxruntime-directml=='))
            for command in commands:
                self.assertIn(str(Path('candidate/python.exe')), command)
                self.assertEqual(command[:4], [str(Path('candidate/python.exe')), '-I', '-m', 'pip'])
                self.assertNotIn('--python', command)
            self.assertIn('--no-deps', commands[-1])
            self.assertIn('--no-binary', commands[-1])
            self.assertTrue(all('--only-binary' in c for c in commands))

    def test_network_and_timeout_report_useful_errors(self):
        with patch.object(install, '_pip_stream', return_value=subprocess.CompletedProcess([], 1, 'Failed to download: proxy error', '')):
            with self.assertRaises(runtime.RuntimeInstallError) as caught:
                runtime._run(['uv'], 'DEPENDENCY_INSTALL_FAILED', 'package')
            self.assertEqual(caught.exception.code, 'DOWNLOAD_FAILED')
        with patch.object(install, '_pip_stream', side_effect=RuntimeError('INSTALL_IDLE_TIMEOUT')):
            with self.assertRaises(runtime.RuntimeInstallError) as caught:
                runtime._run(['uv'], 'DEPENDENCY_INSTALL_FAILED', 'package')
            self.assertIn('INSTALL_IDLE_TIMEOUT', caught.exception.diagnostics)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / 'runtime'
        self.root.mkdir()
        self.old = self.root / 'old'
        self.make_python(self.old)
        self.pointer = {'path': 'old', 'runtimePack': 'cpu'}
        (self.root / 'current.json').write_text(json.dumps(self.pointer))
        for patcher in [patch.dict(os.environ, {'ZM_AI_TOOL_HOME': str(self.home)}),
                        patch.object(runtime.sys, 'platform', 'win32'),
                        patch.object(runtime, 'detect_profile', return_value='cpu'),
                        patch.object(install, '_find_uv', return_value='uv'),
                        patch.object(runtime.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 100 * 1024**3})())]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_python(self, root):
        path = root / 'Scripts/python.exe'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'python')
        return path

    def install(self, probe, package_error=None):
        with patch.object(runtime, '_prepare', side_effect=lambda uv, root: self.make_python(root)), patch.object(
            runtime, '_install_packages', side_effect=package_error
        ), patch.object(runtime, '_probe', side_effect=probe):
            return runtime.install_runtime()

    def test_valid_runtime_skips_network_and_python_preparation(self):
        with patch.object(runtime, '_probe', return_value='ok'), patch.object(runtime, '_prepare') as prepare:
            result = runtime.install_runtime()
        self.assertFalse(result['needsRestart'])
        prepare.assert_not_called()

    def test_success_keeps_candidate_path_and_old_rollback(self):
        seen = []
        def probe(root, *args):
            if root == self.old:
                raise runtime.RuntimeInstallError('RUNTIME_PROBE_FAILED', 'old broken')
            seen.append(root)
            return 'ok'
        self.install(probe)
        self.assertEqual(active_runtime_dir(), seen[0])
        self.assertTrue(self.old.is_dir())
        self.assertEqual(json.loads((self.root / 'previous.json').read_text()), self.pointer)

    def test_failed_install_or_probe_preserves_active_runtime(self):
        for error in (None, runtime.RuntimeInstallError('DOWNLOAD_FAILED', 'offline')):
            with self.subTest(error=error), self.assertRaises(runtime.RuntimeInstallError):
                self.install(runtime.RuntimeInstallError('RUNTIME_PROBE_FAILED', 'broken'), error)
            self.assertEqual(active_runtime_dir(), self.old)
            self.assertEqual(list(self.root.glob('env-*')), [])

    def test_activation_lock_preserves_old_pointer(self):
        original = os.replace
        def replace(source, target):
            if Path(target).name == 'current.json':
                raise PermissionError('antivirus lock')
            return original(source, target)
        with patch.object(runtime.os, 'replace', side_effect=replace), self.assertRaises(runtime.RuntimeInstallError):
            self.install([runtime.RuntimeInstallError('RUNTIME_PROBE_FAILED', 'old'), 'ok'])
        self.assertEqual(active_runtime_dir(), self.old)

    def test_rollback_serialized_and_probed(self):
        previous = self.root / 'previous'
        self.make_python(previous)
        (self.root / 'previous.json').write_text(json.dumps({'path': 'previous', 'runtimePack': 'cpu'}))
        with runtime.transaction(), self.assertRaises(runtime.RuntimeInstallError) as caught:
            runtime.rollback_runtime()
        self.assertEqual(caught.exception.code, 'RUNTIME_BUSY')
        with patch.object(runtime, '_probe', side_effect=runtime.RuntimeInstallError('RUNTIME_PROBE_FAILED', 'bad')):
            with self.assertRaises(runtime.RuntimeInstallError):
                runtime.rollback_runtime()
        self.assertEqual(active_runtime_dir(), self.old)
        with patch.object(runtime, '_probe', return_value='ok'):
            runtime.rollback_runtime()
        self.assertEqual(active_runtime_dir(), previous)

    def test_rollback_to_legacy_venv_is_resolved(self):
        legacy = self.home / '.venv-runtime'
        self.make_python(legacy)
        (self.root / 'previous.json').write_text(json.dumps({'path': '../.venv-runtime', 'runtimePack': 'cpu'}))
        with patch.object(runtime, '_probe', return_value='ok'):
            runtime.rollback_runtime()
        self.assertEqual(active_runtime_dir(), legacy)


if __name__ == '__main__':
    unittest.main()
