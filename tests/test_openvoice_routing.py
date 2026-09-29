"""Routing must not alter text, cloud voices, or VI/EN inference."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.tts import manager


@pytest.mark.parametrize('lang', ['vi', 'en'])
def test_local_clone_stays_on_vieneu(tmp_path, lang):
    with patch.object(manager.vieneu_engine, 'synthesize') as old:
        manager.synthesize_raw('Original text', 'vn:clone:test', tmp_path / 'out.wav', lang)
    old.assert_called_once()
    assert old.call_args.args[0] == 'Original text'


def test_cloud_never_enters_clone_route(tmp_path):
    with patch.object(manager, 'clone_route', side_effect=AssertionError('cloud routed as clone')), patch.object(manager, '_capcut_tts') as cloud:
        manager.synthesize_raw('在这峡谷深处常在群胸某预中', 'cc:voice:resource', tmp_path / 'out.wav', 'zh')
    assert cloud.call_args.args[0] == '在这峡谷深处常在群胸某预中'


def test_auto_clone_requires_confirmation():
    with pytest.raises(ValueError, match='TTS_LANGUAGE_REQUIRED'):
        manager.clone_route('vn:clone:test', 'auto')


def test_non_local_clone_selects_stable_capcut():
    with patch.object(manager, '_cc_voice_options', return_value=[{'id': 'cc:z:2'}, {'id': 'cc:a:1'}]):
        assert manager.clone_route('vn:clone:test', 'zh')['sourceVoice'] == 'cc:a:1'


def test_no_matching_source_fails_without_paid_fallback():
    with patch.object(manager, '_cc_voice_options', return_value=[]):
        with pytest.raises(ValueError, match='TTS_SOURCE_UNAVAILABLE'):
            manager.clone_route('vn:clone:test', 'zh')


def test_non_local_text_preserved(tmp_path):
    route = {'engine': 'openvoice', 'language': 'zh', 'sourceVoice': 'cc:a:1'}
    with patch.object(manager, 'clone_route', return_value=route), patch('pipeline.tts.engines.openvoice.synthesize') as convert:
        manager.synthesize_raw('在这峡谷深处常在群胸某预中', 'vn:clone:test', tmp_path / 'out.wav', 'zh')
    assert convert.call_args.args[0] == '在这峡谷深处常在群胸某预中'


def test_clone_visible_across_languages():
    clone = {'id': 'vn:clone:test', 'type': 'clone', 'language': 'vi'}
    with patch.object(manager.vieneu_engine, 'list_voices', return_value=[clone]), patch.object(manager, '_cc_voice_options', return_value=[]), patch.object(manager, '_el_voice_options', return_value=[]), patch.object(manager.system_engine, 'list_voices', return_value=[]):
        assert manager.list_voices('zh') == [clone]


def test_zmai_reference_language_filter():
    voices = [
        {'id': 'vi-reference', 'type': 'zmAI', 'mode': 'reference', 'language': 'vi'},
        {'id': 'zh-reference', 'type': 'zmAI', 'mode': 'reference', 'language': 'zh'},
    ]
    with patch.object(manager.vieneu_engine, 'list_voices', return_value=voices), patch.object(manager, '_cc_voice_options', return_value=[]), patch.object(manager, '_el_voice_options', return_value=[]), patch.object(manager.system_engine, 'list_voices', return_value=[]):
        assert manager.list_voices('zh') == [voices[1]]
        assert manager.list_voices('vi') == [voices[0]]
        assert manager.list_voices('auto') == voices


def test_missing_runtime_never_calls_cloud(tmp_path):
    from pipeline.tts.engines import openvoice
    with patch.object(openvoice, 'status', return_value={'ready': False}), patch.object(manager, '_capcut_tts') as cloud:
        with pytest.raises(RuntimeError, match='OPENVOICE_NOT_INSTALLED'):
            openvoice.synthesize('你好', 'vn:clone:test', tmp_path / 'out.wav', {'sourceVoice': 'cc:a:1'})
    cloud.assert_not_called()


def test_zmtts_reference_is_materialized_only_on_use(tmp_path):
    from pipeline.tts.engines import openvoice
    from pipeline.tts.engines import vieneu
    voice = 'zmtss:demo'
    ref = tmp_path / 'demo.wav'
    with patch.object(vieneu, 'parse_voice', return_value=('remote-reference', voice)), \
         patch.object(vieneu, '_ensure_remote_reference') as materialize, \
         patch.object(vieneu, 'preview_path', return_value=ref):
        assert openvoice.reference(voice) == ref
    materialize.assert_called_once_with(voice)


def test_capcut_failure_never_substitutes_audio(tmp_path):
    from pipeline.tts.engines import openvoice
    with patch.object(openvoice, 'status', return_value={'ready': True}), patch.object(openvoice, 'reference', return_value=tmp_path / 'ref.wav'), patch.object(manager, '_capcut_tts', side_effect=RuntimeError('capcut failed')), patch.object(openvoice, '_request') as worker:
        with pytest.raises(RuntimeError, match='capcut failed'):
            openvoice.synthesize('你好', 'vn:clone:test', tmp_path / 'out.wav', {'sourceVoice': 'cc:a:1'})
    worker.assert_not_called()
    assert not (tmp_path / 'out.wav').exists()


def test_worker_failure_never_publishes_partial(tmp_path):
    from pipeline.tts.engines import openvoice
    def fail(payload, *args):
        Path(payload['output']).write_bytes(b'partial')
        raise RuntimeError('OPENVOICE_WORKER_FAILED')
    with patch.object(openvoice, 'status', return_value={'ready': True}), patch.object(openvoice, 'reference', return_value=tmp_path / 'ref.wav'), patch.object(manager, '_capcut_tts'), patch.object(openvoice, '_request', side_effect=fail):
        with pytest.raises(RuntimeError, match='OPENVOICE_WORKER_FAILED'):
            openvoice.synthesize('你好', 'vn:clone:test', tmp_path / 'out.wav', {'sourceVoice': 'cc:a:1'})
    assert not (tmp_path / 'out.wav').exists()


def test_cancel_before_cloud(tmp_path):
    from pipeline.tts.engines import openvoice
    with patch.object(openvoice, 'status', return_value={'ready': True}), patch.object(openvoice, 'reference', return_value=tmp_path / 'ref.wav'), patch.object(manager, '_capcut_tts') as cloud:
        with pytest.raises(RuntimeError, match='OPENVOICE_CANCELLED'):
            openvoice.synthesize('你好', 'vn:clone:test', tmp_path / 'out.wav', {}, cancel_check=lambda: True)
    cloud.assert_not_called()


def test_reference_change_invalidates_cache(tmp_path):
    from pipeline.tts.engines import openvoice
    ref = tmp_path / 'ref.wav'
    ref.write_bytes(b'first')
    with patch.object(openvoice, 'reference', return_value=ref):
        first = openvoice.cache_token('vn:clone:test', {'sourceVoice': 'cc:a:1'})
        ref.write_bytes(b'second')
        assert first != openvoice.cache_token('vn:clone:test', {'sourceVoice': 'cc:a:1'})


def test_add_clone_does_not_load_vieneu(tmp_path):
    from pipeline.tts import voice_store
    ref = tmp_path / 'input.wav'
    ref.write_bytes(b'RIFF')
    with patch('pipeline.core.media.ffprobe_duration', return_value=1), patch.object(voice_store, 'ensure_vieneu_dirs'), patch.object(voice_store, 'load_cloned', return_value=[]), patch.object(voice_store, 'CLONED_DIR', tmp_path), patch.object(voice_store, 'add_cloned') as save, patch.object(manager.vieneu_engine, 'get_client', side_effect=AssertionError('VieNeu loaded')):
        manager.vieneu_engine.clone_voice('Test', ref)
    save.assert_called_once()
