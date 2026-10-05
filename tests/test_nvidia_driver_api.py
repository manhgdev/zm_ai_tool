"""Run with the backend environment: python -m unittest discover -s tests -p test_nvidia_driver_api.py."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
try:
    from fastapi import HTTPException
    from starlette.requests import Request
    from pydantic import ValidationError
    from api.routes import system
except ImportError:
    system = None
from pipeline.core import nvidia_driver as driver


@unittest.skipIf(system is None, 'Requires backend environment with FastAPI')
class DriverApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for item in [patch.dict(os.environ, {'ZM_AI_TOOL_HOME': temp.name, 'ZM_AI_TOOL_DESKTOP': '1', 'ZM_AI_TOOL_PORT': '8787'}),
                     patch.dict(system._install_state, {'running': False, 'kind': 'ai_runtime', 'startedAt': 1, 'errorCode': 'DRIVER_TOO_OLD'}, clear=True)]:
            item.start()
            self.addCleanup(item.stop)
        driver._tokens.clear()

    def request(self, origin='http://127.0.0.1:8787', client='127.0.0.1', host='127.0.0.1:8787', marker='1', referer=''):
        headers = [(b'host', host.encode()), (b'x-zm-driver-request', marker.encode())]
        if origin:
            headers.append((b'origin', origin.encode()))
        if referer:
            headers.append((b'referer', referer.encode()))
        return Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/', 'headers': headers,
                        'client': (client, 1234), 'server': ('127.0.0.1', 8787), 'query_string': b''})

    def test_same_origin_with_marker_is_allowed(self):
        with patch.object(sys, 'platform', 'win32'):
            system._require_driver_origin(self.request())
            system._require_driver_origin(self.request(origin='', referer='http://127.0.0.1:8787/'))

    def test_cross_origin_null_remote_host_and_missing_marker_blocked(self):
        cases = [{'origin': 'https://evil.test'}, {'origin': 'null'}, {'origin': ''},
                 {'client': '192.168.1.5'}, {'host': 'evil.test'}, {'marker': ''},
                 {'origin': 'http://127.0.0.1:5173'}]
        for case in cases:
            with self.subTest(case=case), patch.object(sys, 'platform', 'win32'), self.assertRaises(HTTPException) as caught:
                system._require_driver_origin(self.request(**case))
            self.assertEqual(caught.exception.status_code, 403)

    def test_arbitrary_commands_urls_and_kind_are_not_accepted(self):
        for key in ('command', 'url', 'path', 'resumeKind', 'updateId'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                system.DriverActionIn(action='update', token='token', **{key: 'malicious'})

    def test_kind_is_derived_from_failed_backend_job(self):
        self.assertEqual(system._driver_kind('update'), 'ai_runtime')
        system._install_state['errorCode'] = 'DOWNLOAD_FAILED'
        with self.assertRaises(HTTPException):
            system._driver_kind('update')

    def test_cpu_action_bound_to_original_failure(self):
        with patch.object(sys, 'platform', 'win32'):
            self.assertEqual(system._cpu_resume_kind(), 'ai_runtime')
            driver._save({'phase': 'driver_install', 'resumeKind': 'ai_runtime'})
            with self.assertRaises(HTTPException):
                system._cpu_resume_kind()
            driver._save({'phase': 'driver_reboot', 'resumeKind': 'ai_runtime'})
            with self.assertRaises(HTTPException):
                system._cpu_resume_kind()

    def test_gpu_package_errors_offer_cpu_without_driver_update(self):
        for code in ('DOWNLOAD_FAILED', 'DEPENDENCY_INSTALL_FAILED', 'RUNTIME_PROBE_FAILED', 'DISK_FULL'):
            with self.subTest(code=code), patch.object(sys, 'platform', 'win32'), patch(
                'pipeline.core.runtime_active.cpu_mode_enabled', return_value=False
            ):
                system._install_state.update(errorCode=code, error=code, runtimeProfile='nvidia-cu124')
                status = system.api_install_status()
                self.assertTrue(status['cpuRuntimeAvailable'])
                self.assertFalse(status['driverRepairAvailable'])
                self.assertEqual(system._cpu_resume_kind(), 'ai_runtime')
                with self.assertRaises(HTTPException):
                    system._driver_kind('update')
                token = system.api_driver_confirmation(self.request(), 'cpu')['token']
                with patch.object(system, '_start_install_job', return_value={'running': True}) as start:
                    system.api_install_cpu_runtime(self.request(), system.CpuRuntimeIn(token=token))
                    self.assertEqual(start.call_args.args[0], 'cpu_runtime')

    def test_failure_before_profile_selection_requires_physical_gpu(self):
        system._install_state.update(errorCode='HARDWARE_UNSUPPORTED', runtimeProfile='')
        for has_gpu in (False, True):
            with self.subTest(has_gpu=has_gpu), patch.object(sys, 'platform', 'win32'), patch(
                'pipeline.core.runtime_active.cpu_mode_enabled', return_value=not has_gpu
            ), patch('pipeline.core.media.detect_device', return_value={'hasGpu': has_gpu}):
                status = system.api_install_status()
                self.assertEqual(status['cpuRuntimeAvailable'], has_gpu)
                self.assertFalse(status['driverRepairAvailable'])

    def test_cpu_failure_success_and_running_job_do_not_offer_gpu_recovery(self):
        for changes in ({'runtimeProfile': 'cpu', 'errorCode': 'DOWNLOAD_FAILED'},
                        {'runtimeProfile': 'nvidia-cu124', 'errorCode': ''},
                        {'runtimeProfile': 'nvidia-cu124', 'errorCode': 'DOWNLOAD_FAILED', 'running': True}):
            with self.subTest(changes=changes), patch.dict(system._install_state, changes), patch.object(sys, 'platform', 'win32'):
                status = system.api_install_status()
                self.assertFalse(status['cpuRuntimeAvailable'])
                self.assertFalse(status['driverRepairAvailable'])

    def test_cpu_endpoint_consumes_token_and_rejects_extra_fields(self):
        request = self.request()
        with patch.object(sys, 'platform', 'win32'), patch.object(system, '_start_install_job', return_value={'running': True}) as start:
            token = system.api_driver_confirmation(request, 'cpu')['token']
            body = system.CpuRuntimeIn(token=token)
            system.api_install_cpu_runtime(request, body)
            with self.assertRaises(HTTPException):
                system.api_install_cpu_runtime(request, body)
        self.assertEqual(start.call_args.args[0], 'cpu_runtime')
        start.assert_called_once()
        with self.assertRaises(ValidationError):
            system.CpuRuntimeIn(token='token', compute_mode='cuda')

    def test_busy_before_reservation_can_retry_original_kind(self):
        system._install_state.update(kind='nvidia_driver', resumeKind='ocr_cuda', errorCode='GPU_JOBS_BUSY')
        self.assertEqual(system._driver_kind('update'), 'ocr_cuda')
        with patch.object(sys, 'platform', 'win32'):
            self.assertTrue(system.api_install_status()['driverRepairAvailable'])

    def test_runtime_progress_is_mapped_after_driver_progress(self):
        system._install_state.update(running=True, kind='nvidia_driver', progress=80)
        system._set_install_progress(25, '', {'stage': 'download'})
        self.assertEqual(system._install_state['stage'], 'download')
        self.assertEqual(system._install_state['progress'], 85)

    def test_previous_failure_is_restored_after_app_restart(self):
        system._install_state.update(startedAt=0)
        driver._save({'phase': 'driver_failed', 'operationId': 'op', 'resumeKind': 'ai_runtime', 'errorCode': 'DRIVER_DOWNLOAD_FAILED'})
        with patch.object(sys, 'platform', 'win32'):
            status = system.api_install_status()
        self.assertTrue(status['driverRepairAvailable'])
        self.assertEqual(status['errorCode'], 'DRIVER_DOWNLOAD_FAILED')

    def test_one_confirmation_starts_only_one_job(self):
        request = self.request()
        with patch.object(sys, 'platform', 'win32'), patch.object(system, '_start_driver_job', return_value={'running': True}) as start:
            token = system.api_driver_confirmation(request, 'update')['token']
            payload = system.DriverActionIn(action='update', token=token)
            system.api_install_nvidia_driver(request, payload)
            with self.assertRaises(HTTPException):
                system.api_install_nvidia_driver(request, payload)
        start.assert_called_once_with('ai_runtime', 'update')

    def test_confirmation_cannot_be_used_after_state_changes(self):
        request = self.request()
        with patch.object(sys, 'platform', 'win32'), patch.object(system, '_start_driver_job') as start:
            token = system.api_driver_confirmation(request, 'update')['token']
            system._install_state['startedAt'] = 2
            with self.assertRaises(HTTPException):
                system.api_install_nvidia_driver(request, system.DriverActionIn(action='update', token=token))
        start.assert_not_called()

    def test_polling_restores_eula_without_relaunch(self):
        driver._save({'phase': 'driver_eula', 'operationId': 'op', 'resumeKind': 'ai_runtime', 'eula': ['terms']})
        with patch.object(sys, 'platform', 'win32'), patch.object(system, '_start_driver_job') as start:
            result = system.api_install_status()
        self.assertEqual(result['eula'], ['terms'])
        self.assertFalse(result['running'])
        start.assert_not_called()

    def test_polling_after_new_boot_only_resumes(self):
        driver._save({'phase': 'driver_reboot', 'operationId': 'op', 'resumeKind': 'ocr_cuda', 'bootId': 'old'})
        with patch.object(sys, 'platform', 'win32'), patch.object(driver, 'driver_mutex', side_effect=contextlib.nullcontext), patch.object(driver, '_boot_id', return_value='new'), patch.object(driver, 'assert_idle'), patch.object(system, '_start_driver_job') as start:
            system.api_install_status()
        start.assert_called_once_with('ocr_cuda', 'resume')

    def test_active_native_installer_never_gets_second_worker(self):
        driver._save({'phase': 'driver_install', 'operationId': 'op', 'resumeKind': 'ocr_cuda'})
        with patch.object(sys, 'platform', 'win32'), patch.object(driver, 'driver_mutex', side_effect=driver.RuntimeInstallError('RUNTIME_BUSY', 'busy')), patch.object(system, '_start_driver_job') as start:
            result = system.api_install_status()
        self.assertEqual(result['stage'], 'driver_reconnect')
        self.assertTrue(result['running'])
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
