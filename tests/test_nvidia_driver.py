from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import nvidia_driver as driver, runtime_install as runtime, media


class DetectionTests(unittest.TestCase):
    def detect(self, responses):
        with patch('platform.machine', return_value='AMD64'), patch.object(media, 'detect_device', return_value={
            'gpuKind': 'nvidia', 'gpuName': 'Test NVIDIA',
        }), patch('subprocess.run', side_effect=responses):
            return runtime.detect_profile()

    def response(self, text, code=0):
        return subprocess.CompletedProcess([], code, text, 'query failed' if code else '')

    def test_missing_tool_is_not_old_driver(self):
        with self.assertRaises(runtime.RuntimeInstallError) as caught:
            self.detect([FileNotFoundError()])
        self.assertEqual(caught.exception.code, 'NVIDIA_SMI_NOT_FOUND')
        self.assertEqual(caught.exception.driver_check['gpus'][0]['name'], 'Test NVIDIA')

    def test_failed_query_is_not_old_driver(self):
        for response, expected in [(self.response('', 1), 'NVIDIA_DRIVER_UNAVAILABLE'),
                                   (subprocess.TimeoutExpired('nvidia-smi', 15), 'GPU_PROBE_FAILED'),
                                   (self.response(''), 'GPU_PROBE_FAILED'),
                                   (self.response('GPU-a, Test, invalid'), 'GPU_PROBE_FAILED')]:
            with self.subTest(expected=expected), self.assertRaises(runtime.RuntimeInstallError) as caught:
                self.detect([response])
            self.assertEqual(caught.exception.code, expected)

    def test_capability_failure_preserves_driver(self):
        with self.assertRaises(runtime.RuntimeInstallError) as caught:
            self.detect([self.response('GPU-a, Test, 580.01'), self.response('unsupported query', 1)])
        self.assertEqual(caught.exception.code, 'GPU_PROBE_FAILED')
        self.assertEqual(caught.exception.driver_check['gpus'][0]['driver'], '580.01')

    def test_uuid_matches_reordered_gpus(self):
        self.assertEqual(self.detect([self.response('GPU-a, A, 580.01\nGPU-b, B, 580.01'),
                                      self.response('GPU-b, 12.0\nGPU-a, 8.6')]), 'nvidia-cu128')

    def test_only_proven_old_version_gets_old_error(self):
        with self.assertRaises(runtime.RuntimeInstallError) as caught:
            self.detect([self.response('GPU-a, Test, 528.32'), self.response('GPU-a, 8.6')])
        self.assertEqual(caught.exception.code, 'DRIVER_TOO_OLD')
        self.assertEqual(caught.exception.driver_check['minimumVersion'], '528.33')

    def test_unknown_capability_and_mixed_unsupported_gpus(self):
        for caps, code in [('GPU-a, N/A', 'GPU_PROBE_FAILED'), ('GPU-a, nan', 'GPU_PROBE_FAILED'),
                           ('GPU-other, 8.6', 'GPU_PROBE_FAILED'), ('GPU-a, 3.5', 'HARDWARE_UNSUPPORTED')]:
            with self.subTest(caps=caps), self.assertRaises(runtime.RuntimeInstallError) as caught:
                self.detect([self.response('GPU-a, Test, 580.01'), self.response(caps)])
            self.assertEqual(caught.exception.code, code)

    def test_inventory_reuses_executable_resolver(self):
        with patch('pipeline.core.accel.nvidia_smi_executable', return_value='C:/Windows/System32/nvidia-smi.exe'), patch(
            'subprocess.check_output', return_value='0, RTX Test, 8192, 580.01'
        ) as run:
            self.assertEqual(media._nvidia_gpus()[0]['driver'], '580.01')
        self.assertEqual(run.call_args.args[0][0], 'C:/Windows/System32/nvidia-smi.exe')

    def test_basic_display_adapter_is_nvidia_by_pci_id(self):
        with patch.object(media, '_windows_video_controllers', return_value=[{
            'Name': 'Microsoft Basic Display Adapter', 'PNPDeviceID': r'PCI\VEN_10DE&DEV_1234', 'ConfigManagerErrorCode': 28,
        }]):
            gpu = media._windows_gpu_inventory([])[0]
        self.assertEqual(gpu['kind'], 'nvidia')
        self.assertEqual(gpu['deviceProblemCode'], 28)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'ZM_AI_TOOL_HOME': self.temp.name, 'ZM_AI_TOOL_DESKTOP': '1'})
        env.start()
        self.addCleanup(env.stop)
        driver._tokens.clear()
        self.emit, self.resume = Mock(), Mock(return_value={'ok': True})

    def state(self, phase='driver_check'):
        return {'operationId': 'test-op', 'phase': phase, 'resumeKind': 'ocr_cuda', 'bootId': 'old-boot'}

    def old(self):
        return runtime.RuntimeInstallError('DRIVER_TOO_OLD', 'old', driver_check={'minimumVersion': '528.33'})

    def test_valid_driver_never_updates_and_resumes_original_kind(self):
        with patch.object(runtime, 'detect_profile', return_value='nvidia-cu124'), patch.object(driver, '_run_worker') as worker:
            self.assertTrue(driver.run(self.state(), 'update', self.emit, self.resume)['ok'])
        worker.assert_not_called()
        self.resume.assert_called_once_with('ocr_cuda')
        self.assertEqual(driver.snapshot()['phase'], 'driver_done')

    def test_update_must_pass_driver_check_before_resuming(self):
        def worker(state, emit):
            state['phase'] = 'driver_verify'
        with patch.object(runtime, 'detect_profile', side_effect=[self.old(), self.old()]), patch.object(driver, '_run_worker', side_effect=worker):
            with self.assertRaises(runtime.RuntimeInstallError):
                driver.run(self.state(), 'update', self.emit, self.resume)
        self.resume.assert_not_called()
        self.assertEqual(driver.snapshot()['phase'], 'driver_failed')

    def test_successful_update_resumes_once(self):
        def worker(state, emit):
            state['phase'] = 'driver_verify'
        with patch.object(runtime, 'detect_profile', side_effect=[self.old(), 'nvidia-cu124']), patch.object(driver, '_run_worker', side_effect=worker):
            driver.run(self.state(), 'update', self.emit, self.resume)
        self.resume.assert_called_once_with('ocr_cuda')

    def test_reboot_and_eula_do_not_resume(self):
        for phase in ('driver_eula', 'driver_reboot'):
            def worker(state, emit):
                state.update(phase=phase, rebootRequired=phase == 'driver_reboot')
            with self.subTest(phase=phase), patch.object(runtime, 'detect_profile', side_effect=self.old()), patch.object(driver, '_run_worker', side_effect=worker):
                result = driver.run(self.state(), 'update', self.emit, self.resume)
            self.assertEqual(result['stage'], phase)
        self.resume.assert_not_called()

    def test_uac_cancel_network_failure_and_partial_install(self):
        for code in ('DRIVER_UAC_CANCELLED', 'DRIVER_DOWNLOAD_FAILED', 'DRIVER_INSTALL_FAILED', 'DRIVER_UPDATE_NOT_FOUND'):
            with self.subTest(code=code), patch.object(runtime, 'detect_profile', side_effect=self.old()), patch.object(
                driver, '_run_worker', side_effect=runtime.RuntimeInstallError(code, code)
            ):
                with self.assertRaises(runtime.RuntimeInstallError):
                    driver.run(self.state(), 'update', self.emit, self.resume)
            self.assertEqual(driver.snapshot()['errorCode'], code)
        self.resume.assert_not_called()

    def test_recovery_never_launches_another_installer(self):
        with patch.object(runtime, 'detect_profile', side_effect=self.old()), patch.object(driver, '_run_worker') as worker:
            with self.assertRaises(runtime.RuntimeInstallError):
                driver.run(self.state(), 'resume', self.emit, self.resume)
        worker.assert_not_called()

    def test_unknown_result_remains_blocking(self):
        with patch.object(runtime, 'detect_profile', side_effect=self.old()), patch.object(
            driver, '_run_worker', side_effect=runtime.RuntimeInstallError('DRIVER_STATE_UNKNOWN', 'unknown')
        ):
            with self.assertRaises(runtime.RuntimeInstallError):
                driver.run(self.state(), 'update', self.emit, self.resume)
        self.assertEqual(driver.snapshot()['phase'], 'driver_unknown')
        with patch.object(sys, 'platform', 'win32'), self.assertRaises(runtime.RuntimeInstallError):
            driver.assert_available()

    def test_confirmation_is_single_use_bound_and_expires(self):
        for action, binding in [('update', 'changed'), ('reboot', 'state')]:
            token = driver.confirmation('update', 'state')
            with self.assertRaises(runtime.RuntimeInstallError):
                driver.consume(token, action, binding)
        token = driver.confirmation('update', 'state')
        driver.consume(token, 'update', 'state')
        with self.assertRaises(runtime.RuntimeInstallError):
            driver.consume(token, 'update', 'state')
        with patch.object(driver.time, 'monotonic', return_value=0):
            token = driver.confirmation('update', 'state')
        with patch.object(driver.time, 'monotonic', return_value=121), self.assertRaises(runtime.RuntimeInstallError):
            driver.consume(token, 'update', 'state')

    def test_busy_jobs_block_before_reservation(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(driver, 'driver_mutex', side_effect=contextlib.nullcontext), patch.object(driver, '_active_jobs', return_value=True):
            with self.assertRaises(runtime.RuntimeInstallError) as caught:
                driver.reserve('ocr_cuda', 'update')
        self.assertEqual(caught.exception.code, 'GPU_JOBS_BUSY')
        self.assertEqual(driver.snapshot(), {})

    def test_same_boot_cannot_resume_reboot(self):
        driver._save(self.state('driver_reboot'))
        with patch.object(sys, 'platform', 'win32'), patch.object(driver, 'driver_mutex', side_effect=contextlib.nullcontext), patch.object(driver, '_active_jobs', return_value=False), patch.object(driver, '_boot_id', return_value='old-boot'):
            with self.assertRaises(runtime.RuntimeInstallError) as caught:
                driver.reserve('ocr_cuda', 'resume')
        self.assertEqual(caught.exception.code, 'DRIVER_REBOOT_REQUIRED')
        self.assertEqual(driver.snapshot()['phase'], 'driver_reboot')

    def test_new_boot_resumes_without_accepting_terms_again(self):
        driver._save(self.state('driver_reboot'))
        with patch.object(sys, 'platform', 'win32'), patch.object(driver, 'driver_mutex', side_effect=contextlib.nullcontext), patch.object(driver, '_active_jobs', return_value=False), patch.object(driver, '_boot_id', return_value='new-boot'):
            state = driver.reserve('ocr_cuda', 'resume')
        self.assertEqual(state['resumeKind'], 'ocr_cuda')
        self.assertEqual(state['phase'], 'driver_check')

    def test_corrupt_journal_fails_closed(self):
        driver._path().parent.mkdir(parents=True)
        driver._path().write_text('{')
        with patch.object(sys, 'platform', 'win32'), self.assertRaises(runtime.RuntimeInstallError):
            driver.assert_available()

    def test_maintenance_blocks_new_job_registration(self):
        from pipeline.core.jobs import arm_job, begin_job
        driver._save(self.state('driver_install'))
        with patch.object(sys, 'platform', 'win32'):
            for fn in (arm_job, begin_job):
                with self.assertRaises(runtime.RuntimeInstallError):
                    fn('must-not-start')

    def test_public_and_legacy_project_activity_is_checked(self):
        from pipeline.core import config
        private, public = self.root / 'private', self.root / 'public'
        public.mkdir()
        project = public / 'test-project'
        project.mkdir()
        (project / 'meta.json').write_text(json.dumps({'status': {'running': True}}))
        with patch.object(config, 'DATA', private), patch.object(config, 'PUBLIC_DATA', public):
            self.assertTrue(driver._active_jobs())

    def test_encoded_worker_fits_windows_command_line(self):
        source = Path(driver.__file__).with_name('nvidia_driver_update.ps1').read_text()
        self.assertLess(len(source.encode('utf-16le')) * 4 // 3 + 2500, 32767)


if __name__ == '__main__':
    unittest.main()
