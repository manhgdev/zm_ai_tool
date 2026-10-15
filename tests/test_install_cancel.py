"""Cooperative package-install cancel via progress callback."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from pipeline.core.runtime_download import RuntimeInstallError


class InstallCancelTests(unittest.TestCase):
    def test_progress_raises_after_cancel(self):
        from api.routes import system

        system._install_cancel.clear()
        with system._install_lock:
            system._install_state.update(running=True, kind='whisper_runtime', progress=1, error='')
        system._install_cancel.set()
        with self.assertRaises(RuntimeInstallError) as raised:
            system._set_install_progress(10, 'downloading')
        self.assertEqual(raised.exception.code, 'INSTALL_CANCELLED')

    def test_cancel_endpoint_sets_flag_while_running(self):
        from api.routes import system

        system._install_cancel.clear()
        with system._install_lock:
            system._install_state.update(running=True, kind='whisper_runtime', progress=5, cancelRequested=False)
        out = system.api_install_cancel()
        self.assertTrue(out.get('cancelling'))
        self.assertTrue(system._install_cancel.is_set())
        with system._install_lock:
            self.assertTrue(system._install_state.get('cancelRequested'))
            system._install_state['running'] = False
        system._install_cancel.clear()

    def test_start_job_honours_cancel_during_work(self):
        from api.routes import system

        started = threading.Event()
        finished = threading.Event()

        def slow():
            started.set()
            for _ in range(40):
                system._raise_if_install_cancelled()
                time.sleep(0.05)
            return {'ok': True, 'message': 'done'}

        with (
            patch.object(system, '_start_checks_warm'),
            patch('pipeline.core.nvidia_driver.assert_available'),
        ):
            with system._install_lock:
                system._install_state.update(running=False, kind='', errorCode='', result=None)
            system._start_install_job('whisper_runtime', slow, needs_restart=False)
            self.assertTrue(started.wait(2))
            system.api_install_cancel()
            for _ in range(80):
                with system._install_lock:
                    if not system._install_state.get('running'):
                        finished.set()
                        break
                time.sleep(0.05)
            self.assertTrue(finished.is_set())
            with system._install_lock:
                self.assertEqual(system._install_state.get('errorCode'), 'INSTALL_CANCELLED')
                system._install_state.update(running=False, errorCode='', result=None, cancelRequested=False)
            system._install_cancel.clear()


if __name__ == '__main__':
    unittest.main()
