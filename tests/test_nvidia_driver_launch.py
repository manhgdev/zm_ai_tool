"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_nvidia_driver_launch.py"""
import ctypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline.core import nvidia_driver as driver
from pipeline.core.runtime_download import RuntimeInstallError


class DriverLaunchTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            'callback': 'http://127.0.0.1:12345/progress', 'token': 'a' * 43,
            'operationId': '00000000-0000-0000-0000-000000000001', 'acceptedFingerprint': '',
        }

    def test_elevation_runs_fixed_file_with_quoted_path_and_data_arguments(self):
        with tempfile.TemporaryDirectory(prefix='ZM tiếng Việt & ') as directory:
            worker = Path(directory) / 'nvidia_driver_update.ps1'
            worker.touch()
            captured = {}

            def launch(pointer):
                info = pointer._obj
                captured.update(file=info.lpFile, verb=info.lpVerb, arguments=info.lpParameters)
                info.hProcess = 123
                return True

            shell = Mock()
            shell.ShellExecuteExW.side_effect = launch
            with patch.object(driver, '__file__', str(Path(directory) / 'nvidia_driver.py')), \
                 patch.object(driver, '_powershell', return_value=r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'), \
                 patch.object(ctypes, 'WinDLL', return_value=shell, create=True):
                self.assertEqual(driver._elevate(self.config), 123)
                self.config['acceptedFingerprint'] = 'b' * 64
                driver._elevate(self.config)

            self.assertEqual(captured['verb'], 'runas')
            self.assertIn(f'-File "{worker.resolve()}"', captured['arguments'])
            for key, flag in [('callback', 'Callback'), ('token', 'Token'), ('operationId', 'OperationId'), ('acceptedFingerprint', 'AcceptedFingerprint')]:
                self.assertIn(f'-{flag} {self.config[key]}', captured['arguments'])
            self.assertNotIn('-EncodedCommand', captured['arguments'])
            self.assertIn('-ExecutionPolicy RemoteSigned', captured['arguments'])
            self.assertNotIn('-ExecutionPolicy Bypass', captured['arguments'])

    def test_missing_bundled_worker_stops_before_uac(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(driver, '__file__', str(Path(directory) / 'nvidia_driver.py')), \
             patch.object(ctypes, 'WinDLL', create=True) as load_library:
            with self.assertRaises(FileNotFoundError):
                driver._elevate(self.config)
            load_library.assert_not_called()

    def test_uac_cancellation_remains_an_explicit_error(self):
        shell = Mock()
        shell.ShellExecuteExW.return_value = False
        with patch.object(driver, '_powershell', return_value='powershell.exe'), \
             patch.object(ctypes, 'WinDLL', return_value=shell, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=1223, create=True):
            with self.assertRaises(RuntimeInstallError) as error:
                driver._elevate(self.config)
            self.assertEqual(error.exception.code, 'DRIVER_UAC_CANCELLED')


if __name__ == '__main__':
    unittest.main()
