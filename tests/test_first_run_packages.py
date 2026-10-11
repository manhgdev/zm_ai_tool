from unittest.mock import patch

from pipeline.core.system_check import checks


def test_only_media_tools_are_required_packages(tmp_path):
    device = {'gpuKind': 'none', 'accel': 'cpu', 'install': {}, 'osLabel': 'Windows', 'arch': 'x64'}
    with (
        patch('pipeline.core.config.DATA', tmp_path),
        patch('pipeline.core.media.detect_device', return_value=device),
        patch('pipeline.core.media._ff_bin', return_value=str(tmp_path / 'missing.exe')),
        patch.object(checks, '_ollama_executable', return_value=None),
        patch.object(checks, '_node_executable', return_value=None),
        patch.object(checks, '_mod_ok_fast', return_value=(False, 'missing')),
        patch.object(checks, '_mod_ok', return_value=(False, 'missing')),
        patch.object(checks, '_runtime_mod_ok', return_value=(False, 'missing')),
        patch.object(checks, '_demucs_venv_fast', return_value=(False, 'missing')),
        patch.object(checks, '_which', return_value=None),
        patch('httpx.get', side_effect=OSError('offline')),
    ):
        result = checks._system_checks_uncached(fast=True)
        executable = tmp_path / 'media.exe'
        executable.touch()
        with patch('pipeline.core.media._ff_bin', return_value=str(executable)), patch.object(checks, '_run_ver', return_value='ok'):
            ready = checks._system_checks_uncached(fast=True)
    assert result['requiredMissing'] == ['ffmpeg', 'ffprobe']
    assert ready['ok'] is True
    assert ready['requiredMissing'] == []
    packages = [item for item in result['items'] if item['id'] not in ('data', 'device')]
    assert {item['id'] for item in packages if item['required']} == {'ffmpeg', 'ffprobe'}
    assert {'ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu'} <= set(result['optionalMissing'])
