import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from api.routes import system
from pipeline.core import runtime_active, nvidia_driver
from pipeline.asr import whisper


class HardwareModeTests(unittest.TestCase):
    def test_linux_mode_switch_ignores_stale_windows_maintenance_metadata(self):
        with patch.object(sys, 'platform', 'linux'), \
             patch.object(system, 'hardware', return_value={'label': 'CPU', 'accel': 'cpu', 'gpuKind': 'none', 'hasGpu': False}), \
             patch.object(nvidia_driver, 'cpu_maintenance', side_effect=AssertionError('must not run on Linux')):
            # The route's platform guard is exercised by the implementation;
            # the unsupported GPU branch remains a controlled 400 response.
            with self.assertRaises(system.HTTPException) as raised:
                system.api_hardware_mode(system.HardwareModeIn(mode='gpu'))
            self.assertEqual(raised.exception.status_code, 400)

    def test_tts_idle_progress_does_not_block_compute_switch(self):
        from types import SimpleNamespace
        from pipeline.core import config, jobs

        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as root, \
                 patch.object(config, 'DATA', Path(root)), \
                 patch.object(config, 'PUBLIC_DATA', Path(root)), \
                 patch.object(jobs, '_job_pids', {}), \
                 patch.object(jobs, '_job_procs', {}), \
                 patch.dict(sys.modules, {
                     'pipeline.tts.studio': SimpleNamespace(list_job_progress=lambda: {
                         'old-job': {'done': False, 'running': running, 'pct': 45}}),
                     'pipeline.automation.service': None,
                     'pipeline.cleaner.cleaner_jobs': None,
                     'pipeline.srt_export': None,
                     'pipeline.srt_image': None,
                     'pipeline.drawing.jobs': None,
                 }):
                self.assertEqual(nvidia_driver._active_jobs(), running)

    def test_windows_mode_request_does_not_count_itself_as_busy(self):
        import ast
        import asyncio
        from types import SimpleNamespace

        source = Path(__file__).resolve().parents[1] / 'backend/api/app.py'
        middleware = next(node for node in ast.walk(ast.parse(source.read_text()))
                          if isinstance(node, ast.AsyncFunctionDef) and node.name == 'driver_maintenance')
        middleware.decorator_list = []
        namespace = {}
        exec(compile(ast.Module(body=[middleware], type_ignores=[]), str(source), 'exec'), namespace)

        async def change_mode(_request):
            with nvidia_driver.cpu_maintenance():
                return 'changed'

        with patch.object(sys, 'platform', 'win32'), \
             patch.object(nvidia_driver, 'pending', return_value=False), \
             patch.object(nvidia_driver, '_active_jobs', return_value=False), \
             patch.object(nvidia_driver, '_requests', 0):
            request = SimpleNamespace(url=SimpleNamespace(path='/api/hardware/mode'), method='POST')
            result = asyncio.run(namespace['driver_maintenance'](request, change_mode))
            self.assertEqual(result, 'changed')

    def test_switch_preserves_runtime_pointer_and_reports_selected_device(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': root}), \
             patch.object(system, 'hardware', return_value={'label': 'GPU', 'accel': 'metal', 'gpuKind': 'apple'}), \
             patch.object(runtime_active, '_hardware_cpu_only', return_value=False), \
             patch.object(runtime_active, 'clear_compute_caches', side_effect=runtime_active.clear_compute_caches) as clear, \
             patch.object(nvidia_driver, 'cpu_maintenance', contextlib.nullcontext), \
             patch.object(system, '_install_state', {}), \
             patch('pipeline.tts.engines.vieneu._load_state', 'cold'), \
             patch('pipeline.tts.engines.vieneu.reset_client') as reset, \
             patch.object(whisper, 'reset_whisper'):
            pointer = Path(root) / 'runtime/current.json'
            pointer.parent.mkdir()
            pointer.write_text(json.dumps({'path': 'versions/original', 'computeMode': 'auto'}))
            self.assertEqual(system.api_hardware_mode(system.HardwareModeIn(mode='cpu'))['accel'], 'cpu')
            self.assertTrue(runtime_active.cpu_mode_enabled())
            self.assertEqual(json.loads(pointer.read_text())['path'], 'versions/original')
            self.assertEqual(reset.call_count, 1)
            self.assertEqual(clear.call_count, 1)

    def test_busy_jobs_reject_switch_before_reset(self):
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(system, '_install_state', {}), \
             patch.object(nvidia_driver, 'cpu_maintenance', side_effect=nvidia_driver.RuntimeInstallError('GPU_JOBS_BUSY', 'busy')), \
             patch('pipeline.tts.engines.vieneu.reset_client') as reset:
            with self.assertRaises(system.HTTPException) as raised:
                system.api_hardware_mode(system.HardwareModeIn(mode='cpu'))
            self.assertEqual(raised.exception.status_code, 409)
            reset.assert_not_called()

    def test_cpu_disables_apple_mlx(self):
        with patch.object(runtime_active, 'cpu_mode_enabled', return_value=True):
            self.assertFalse(whisper._mlx_whisper_available())


if __name__ == '__main__':
    unittest.main()
