import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import accel, runtime_active
from pipeline.core import media


class CpuModeTests(unittest.TestCase):
    def setUp(self):
        runtime_active._hardware_cpu_only.cache_clear()
        self.addCleanup(runtime_active._hardware_cpu_only.cache_clear)

    def test_no_gpu_forces_cpu_on_every_platform_and_skips_video_gpu(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            'ZM_AI_TOOL_HOME': root, 'TORCH_DEVICE': 'cuda'
        }), patch.object(media, 'detect_device', return_value={'gpuKind': 'none', 'accel': 'cpu'}), patch.object(
            media, '_h264_encoder_available', side_effect=AssertionError('must not probe GPU')
        ), patch.object(media.subprocess, 'run', side_effect=AssertionError('must not decode on GPU')):
            for platform in ('win32', 'darwin', 'linux'):
                with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                    self.assertTrue(runtime_active.cpu_mode_enabled())
                    self.assertEqual(accel.preferred_torch_device(), 'cpu')
                    self.assertIsNone(media.h264_hardware_encoder())
                    self.assertFalse(media.nvenc_available())
                    self.assertFalse(media.nvdec_available(Path('video.mp4')))
                    self.assertIn('libx264', media.h264_encoder_args())

    def test_supported_gpu_is_not_disabled_by_unready_packages(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': root}):
            for kind, backend in [('nvidia', 'cpu'), ('apple', 'metal'), ('amd', 'directml'), ('intel', 'directml')]:
                runtime_active._hardware_cpu_only.cache_clear()
                with self.subTest(kind=kind), patch.object(media, 'detect_device', return_value={
                    'gpuKind': kind, 'accel': backend
                }):
                    self.assertFalse(runtime_active.cpu_mode_enabled())

    def test_windows_display_only_adapter_does_not_enable_directml(self):
        with patch.object(media, '_nvidia_gpus', return_value=[]), patch.object(
            media, '_windows_video_controllers', return_value=[{'Name': 'Virtual display adapter'}]
        ), patch.object(media.platform, 'system', return_value='Windows'), patch.object(
            media.platform, 'machine', return_value='AMD64'
        ):
            device = media.detect_device()
        self.assertFalse(device['hasGpu'])
        self.assertEqual(device['accel'], 'cpu')
        self.assertEqual(device['install']['ocr'], '')

    def test_saved_cpu_mode_overrides_already_cached_video_gpu(self):
        with patch.object(runtime_active, 'cpu_mode_enabled', return_value=False), patch.object(
            media.subprocess, 'run', return_value=CompletedProcess([], 0)
        ):
            media._h264_encoder_available.cache_clear()
            self.addCleanup(media._h264_encoder_available.cache_clear)
            self.assertTrue(media.nvenc_available())
        with patch.object(runtime_active, 'cpu_mode_enabled', return_value=True):
            self.assertFalse(media.nvenc_available())
            self.assertIsNone(media.h264_hardware_encoder())

    def test_cpu_only_bypasses_gpu_probes_and_overrides(self):
        from pipeline.core.system_check import probe, install
        from pipeline.export.stem import _demucs_backend_wanted, _torch_device
        from pipeline.asr.speaker import preferred_diarization_provider
        from pipeline.ocr.extract_parts import runtime as ocr
        from pipeline.ocr import locate_worker
        from pipeline.tts.engines import vieneu_frozen
        ocr._rapidocr_gpu_kwargs.cache_clear()
        self.addCleanup(ocr._rapidocr_gpu_kwargs.cache_clear)
        with patch.object(sys, 'platform', 'linux'), patch('pipeline.core.media.detect_device', return_value={
            'gpuKind': 'other', 'accel': 'cpu', 'gpuName': 'Cirrus Logic GD 5446'
        }), patch.dict(os.environ, {'TORCH_DEVICE': 'cuda', 'VIENEU_BACKEND': 'pytorch',
                                   'SPEAKER_DIARIZATION_PROVIDER': 'cuda'}), patch.dict(
            accel._cache, {'torch_device': 'cuda'}, clear=True
        ), patch.object(accel, '_nvidia_smi', side_effect=AssertionError('GPU probe')), patch.object(
            accel, '_probe_torch_device_in', side_effect=AssertionError('GPU probe')
        ):
            self.assertTrue(runtime_active.cpu_mode_enabled())
            self.assertEqual(accel.preferred_torch_device(), 'cpu')
            self.assertEqual(accel.preferred_vieneu_backend(), ('pytorch', 'cpu'))
            self.assertEqual(preferred_diarization_provider(), 'cpu')
            self.assertEqual(_demucs_backend_wanted(), 'cpu')
            self.assertEqual(_torch_device(Path('/missing/python')), 'cpu')
            self.assertEqual(install._runtime_ort_accel(), 'cpu')
            self.assertEqual(probe._runtime_torch_accel(), 'cpu')
            self.assertFalse(probe._torch_cuda_ready())
            self.assertFalse(vieneu_frozen.runtime_torch_cuda_ready())
            self.assertFalse(any(ocr._rapidocr_gpu_kwargs().values()))
            with patch.object(locate_worker.subprocess, 'run', return_value=CompletedProcess([], 0, 'CPU', '')) as run:
                self.assertEqual(locate_worker._python_can_ocr('/python'), (True, 'CPU'))
                self.assertNotIn('get_available_providers', run.call_args.args[0][-1])
            accel.apply_gpu_process_env()

    def test_linux_gpu_is_not_silently_disabled(self):
        with patch.object(sys, 'platform', 'linux'), patch('pipeline.core.media.detect_device', return_value={
            'gpuKind': 'nvidia', 'accel': 'cpu'
        }):
            self.assertFalse(runtime_active.cpu_mode_enabled())

    def test_saved_cpu_mode_is_read_on_linux(self):
        with tempfile.TemporaryDirectory() as root, patch.object(sys, 'platform', 'linux'), patch.dict(
            os.environ, {'ZM_AI_TOOL_HOME': root}
        ), patch.object(runtime_active, '_hardware_cpu_only', side_effect=AssertionError('must use metadata')):
            pointer = Path(root) / 'runtime/current.json'
            pointer.parent.mkdir()
            pointer.write_text(json.dumps({'computeMode': 'cpu'}))
            self.assertTrue(runtime_active.cpu_mode_enabled())

    def test_linux_cpu_recipe_has_no_gpu_packages_and_verifies_in_fresh_process(self):
        from pipeline.core.system_check import install
        commands = []
        def stream(command, **kwargs):
            commands.append(command)
            return CompletedProcess(command, 1 if len(commands) == 1 else 0, 'probe output', '')
        with patch.object(sys, 'platform', 'linux'), patch.object(install, '_pip_stream', side_effect=stream), patch(
            'pipeline.core.nvidia_driver.assert_idle'
        ), patch.object(runtime_active, 'clear_compute_caches'):
            result = install._install_linux_cpu_packages()
        installs = [c for c in commands if 'install' in c]
        self.assertTrue(result['ok'])
        self.assertTrue(result['needsRestart'])
        self.assertEqual(result['computeMode'], 'cpu')
        torch = next(c for c in installs if 'torch==2.6.0' in c)
        self.assertIn(install._TORCH_CPU_INDEX, torch)
        for command in installs:
            self.assertNotIn('onnxruntime-gpu', command)
            self.assertNotIn('onnxruntime-directml', command)
            self.assertFalse(any('nvidia' in part or 'cu124' in part or 'cu128' in part for part in command))
        deps = next(c for c in installs if 'librosa==0.11.0' in c)
        self.assertNotIn('--no-deps', deps)
        self.assertIn('transformers==4.57.6', deps)
        self.assertEqual(commands[0], commands[-1])

    def test_linux_cpu_probe_failure_is_not_success(self):
        from pipeline.core.system_check import install
        from pipeline.core.runtime_download import RuntimeInstallError
        def stream(command, **kwargs):
            return CompletedProcess(command, 1 if '-c' in command else 0, '', 'missing inference dependency')
        with patch.object(install, '_pip_stream', side_effect=stream), patch(
            'pipeline.core.nvidia_driver.assert_idle'
        ), patch.object(runtime_active, 'clear_compute_caches') as clear:
            with self.assertRaises(RuntimeInstallError) as caught:
                install._install_linux_cpu_packages()
        self.assertEqual(caught.exception.code, 'RUNTIME_PROBE_FAILED')
        self.assertIn('missing inference dependency', caught.exception.diagnostics)
        clear.assert_not_called()

    def test_display_only_linux_adapter_is_cpu_only(self):
        with patch.object(media, '_nvidia_gpus', return_value=[]), patch.object(
            media.subprocess, 'check_output', return_value='"Cirrus Logic" "GD 5446" "VGA compatible controller"\n'
        ), patch.object(media.platform, 'system', return_value='Linux'), patch.object(
            media.platform, 'machine', return_value='x86_64'
        ):
            device = media.detect_device()
        self.assertEqual(device['accel'], 'cpu')
        self.assertFalse(device['hasGpu'])
        self.assertEqual(device['install']['ocr'], '')
        self.assertNotIn('ocr_cuda', [item['id'] for item in device['install']['actions']])

    def test_cpu_precedes_cached_cuda_and_environment(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': root, 'ZM_AIO_TORCH_DEVICE': 'cuda'}), patch.object(sys, 'platform', 'win32'), patch.dict(accel._cache, {'torch_device': 'cuda'}):
            path = Path(root) / 'runtime/current.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'computeMode': 'cpu'}))
            self.assertEqual(accel.preferred_torch_device(), 'cpu')
            from pipeline.gpu.manager import _compute_backend
            from pipeline.asr.speaker import preferred_diarization_provider
            from pipeline.export.stem import _demucs_backend_wanted
            from pipeline.core.system_check.probe import _nvidia_present
            self.assertEqual(_compute_backend('nvidia', 'cuda'), 'cpu')
            with patch.dict(os.environ, {'SPEAKER_DIARIZATION_PROVIDER': 'cuda'}):
                self.assertEqual(preferred_diarization_provider(), 'cpu')
            self.assertEqual(_demucs_backend_wanted(), 'cpu')
            self.assertFalse(_nvidia_present())
            path.write_text('{}')
            self.assertFalse(runtime_active.cpu_mode_enabled())

    def test_gpu_environment_override_cannot_enable_cuda_without_gpu(self):
        with patch.dict(os.environ, {'ZM_AIO_TORCH_DEVICE': 'cuda'}), patch.object(
            accel, '_nvidia_smi', return_value=False
        ), patch.dict(accel._cache, {}, clear=True):
            self.assertEqual(accel.preferred_torch_device(refresh=True), 'cpu')

    def test_ocr_provider_is_cpu_when_inventory_has_no_accelerator(self):
        from pipeline.ocr.extract_parts import runtime as ocr_runtime

        ocr_runtime._rapidocr_gpu_kwargs.cache_clear()
        with patch('pipeline.core.media.detect_device', return_value={'gpuKind': 'other', 'accel': 'cpu'}):
            flags = ocr_runtime._rapidocr_gpu_kwargs()
        self.addCleanup(ocr_runtime._rapidocr_gpu_kwargs.cache_clear)
        self.assertFalse(any(flags.values()))

    def test_ocr_gpu_install_is_rejected_on_cpu_only_host(self):
        from pipeline.core.system_check import install

        with patch('pipeline.core.media.detect_device', return_value={'gpuKind': 'other', 'accel': 'cpu'}), patch.object(
            install, '_pip_stream', side_effect=AssertionError('must not install GPU package')
        ):
            with self.assertRaisesRegex(RuntimeError, 'OCR GPU'):
                install.install_ocr_cuda()


if __name__ == '__main__':
    unittest.main()
