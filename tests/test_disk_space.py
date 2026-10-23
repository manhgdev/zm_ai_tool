"""PYTHONPATH=backend backend/.venv/bin/python -m pytest -q tests/test_disk_space.py"""
import errno
import json
import threading
from types import SimpleNamespace

from pipeline.core.runtime_download import RuntimeInstallError, classify_error, disk_full_details


def test_worker_traceback_identifies_the_write_drive_not_source_drive():
    error = RuntimeError('File "C:\\app\\transformers.py", line 116\n'
                         "OSError: [WinError 112] There is not enough space on the disk: 'D:\\models\\hf\\modules'")
    details = disk_full_details(error)
    assert details['errorCode'] == 'DISK_FULL'
    assert details['diskDrive'] == 'D:'
    assert details['diskPath'] == r'D:\models\hf\modules'
    assert details['retryable'] is False


def test_localized_windows_error_uses_native_error_number():
    error = OSError('localized message')
    error.winerror = 112
    error.filename = r'E:\ZM AI TOOL\cache'
    assert classify_error(error) == ('DISK_FULL', False)
    assert disk_full_details(error)['diskDrive'] == 'E:'


def test_nested_disk_error_uses_nearest_existing_volume(tmp_path, monkeypatch):
    from pipeline.core import runtime_download
    path = tmp_path / 'not-created' / 'output.wav'
    cause = OSError(errno.ENOSPC, 'localized message', str(path))
    error = RuntimeError('TTS initialization failed')
    error.__cause__ = cause
    visited = []
    def disk_usage(path):
        visited.append(path)
        return SimpleNamespace(free=123)
    monkeypatch.setattr(runtime_download.shutil, 'disk_usage', disk_usage)
    details = disk_full_details(error)
    assert details['diskPath'] == str(path)
    assert details['freeDiskBytes'] == 123
    assert visited == [tmp_path]


def test_installer_retains_required_space_and_download_location(tmp_path):
    path = tmp_path / 'wheels' / 'torch.whl.part'
    error = RuntimeInstallError('DISK_FULL', 'torch: DISK_FULL', diagnostics=json.dumps({
        'partialPath': str(path), 'requiredBytes': 10 * 1024 ** 3,
    }))
    details = disk_full_details(error)
    assert details['diskPath'] == str(path)
    assert details['requiredDiskBytes'] == 10 * 1024 ** 3
    assert details['freeDiskBytes'] >= 0


def test_unavailable_disk_probe_preserves_the_original_error(tmp_path, monkeypatch):
    from pipeline.core import runtime_download
    def unavailable(path):
        raise PermissionError('unavailable')
    monkeypatch.setattr(runtime_download.shutil, 'disk_usage', unavailable)
    details = disk_full_details(OSError(errno.ENOSPC, 'No space left', str(tmp_path)))
    assert details['errorCode'] == 'DISK_FULL'
    assert 'freeDiskBytes' not in details
    assert disk_full_details(RuntimeError('DISK_FULL')) == {'errorCode': 'DISK_FULL', 'retryable': False}
    assert disk_full_details(PermissionError('Access denied')) == {}


def test_vieneu_status_reports_disk_failure_without_loading_the_model(monkeypatch):
    from pipeline.tts.engines import vieneu
    monkeypatch.setattr(vieneu, 'available', lambda: False)
    monkeypatch.setattr(vieneu, '_load_state', 'error')
    monkeypatch.setattr(vieneu, '_client_err', "[WinError 112] Not enough space on the disk: 'D:\\cache'")
    status = vieneu.status()
    assert status['errorCode'] == 'DISK_FULL'
    assert status['diskDrive'] == 'D:'
    monkeypatch.setattr(vieneu, '_load_state', 'cold')
    assert 'errorCode' not in vieneu.status()


def test_install_failure_status_keeps_disk_details_for_the_popup(tmp_path, monkeypatch):
    from api.routes import system
    from pipeline.core import nvidia_driver, app_log, runtime_active
    done = threading.Event()
    monkeypatch.setattr(system, '_install_state', {**system._install_state, 'running': False})
    monkeypatch.setattr(system, '_install_cancel', threading.Event())
    monkeypatch.setattr(system, '_reset_install_log', lambda kind: None)
    monkeypatch.setattr(system, '_append_install_log', lambda text: None)
    monkeypatch.setattr(app_log, 'append_log', lambda *args, **kwargs: None)
    monkeypatch.setattr(system, '_start_checks_warm', done.set)
    monkeypatch.setattr(nvidia_driver, 'assert_available', lambda: None)
    monkeypatch.setattr(runtime_active, 'cpu_mode_enabled', lambda: True)
    path = tmp_path / 'runtime'
    def install():
        raise RuntimeInstallError('DISK_FULL', 'Not enough free space', diagnostics=json.dumps({
            'disk': str(path), 'requiredBytes': 4 * 1024 ** 3,
        }))
    system._start_install_job('vieneu_runtime', install)
    assert done.wait(5)
    status = system.api_install_status()
    assert not status['running']
    assert status['errorCode'] == 'DISK_FULL'
    assert status['diskPath'] == str(path)
    assert status['requiredDiskBytes'] == 4 * 1024 ** 3
    assert status['retryable'] is False
