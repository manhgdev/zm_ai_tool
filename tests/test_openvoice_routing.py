"""Stable TTS routing never enters the removed automatic OpenVoice route."""
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pipeline.tts import manager
from pipeline.tts.engines import vieneu


def test_local_clone_stays_on_vieneu(tmp_path):
    with patch.object(manager.vieneu_engine, "synthesize") as synth:
        manager.synthesize_raw("Original text", "vn:clone:test", tmp_path / "out.wav", "vi")
    synth.assert_called_once()
    assert synth.call_args.args[0] == "Original text"


def test_vieneu_rejects_unsupported_language_before_writing_empty_wav(tmp_path):
    with patch.object(manager.vieneu_engine, "synthesize") as synth:
        with pytest.raises(RuntimeError, match="chỉ hỗ trợ tiếng Việt và tiếng Anh"):
            manager.synthesize_raw("你好", "vn:clone:test", tmp_path / "out.wav", "zh")
    synth.assert_not_called()


def test_cloud_voice_stays_on_direct_capcut_path(tmp_path):
    with patch.object(manager, "_capcut_tts") as synth:
        manager.synthesize_raw("原文", "cc:voice:resource", tmp_path / "out.wav", "zh")
    synth.assert_called_once()
    assert synth.call_args.args[0] == "原文"


def test_no_openvoice_route_or_cache_helpers():
    assert not hasattr(manager, "clone_route")
    assert not hasattr(manager, "clone_cache_token")


def test_clone_is_filtered_by_selected_language():
    clone = {"id": "vn:clone:test", "type": "clone", "language": "vi"}
    with patch.object(manager.vieneu_engine, "list_voices", return_value=[clone]), \
        patch.object(manager, "_cc_voice_options", return_value=[]), \
        patch.object(manager, "_el_voice_options", return_value=[]), \
        patch.object(manager.system_engine, "list_voices", return_value=[]):
        assert manager.list_voices("zh") == []


def test_zmt_chinese_reference_is_not_listed_as_vieneu(tmp_path):
    references = [
        {
            "id": "zmt:tieng-trung--zf-007",
            "name": "zf_007",
            "source": "ZMTTS",
            "language": "zh",
            "ref_file": "zmt-zf-007.wav",
        },
        {
            "id": "zmt:tieng-viet--af-001",
            "name": "af_001",
            "source": "ZMTTS",
            "language": "vi",
            "ref_file": "zmt-af-001.wav",
        },
    ]
    with patch.object(vieneu.voice_store, "load_reference_voices", return_value=references), \
        patch.object(vieneu.voice_store, "reference_path", return_value=tmp_path / "ref.wav"), \
        patch.object(vieneu.voice_store, "load_cloned", return_value=[]), \
        patch.object(vieneu, "list_preset_from_assets", return_value=[]), \
        patch.object(vieneu.zmtss_catalog, "voices", return_value=[]):
        ids = {voice["id"] for voice in vieneu.list_voices()}
    assert "zmt:tieng-trung--zf-007" not in ids
    assert "zmt:tieng-viet--af-001" in ids
