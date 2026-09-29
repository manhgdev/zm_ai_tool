import sys
from pathlib import Path
from unittest.mock import patch
import pytest

pytest.importorskip('fastapi')

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from api.routes import tts_voices


def test_zmtss_preview_redirects_without_materializing():
    item = {'id': 'demo', 'audio': 'audio/demo.wav'}
    with patch('pipeline.tts.zmtss_catalog.get', return_value=item), \
         patch('pipeline.tts.zmtss_catalog.remote_url', return_value='https://example.test/demo.wav'), \
         patch.object(tts_voices.vieneu_engine, '_ensure_remote_reference') as materialize:
        response = tts_voices.api_tts_voice_preview('zmt:demo')
    assert response.status_code == 307
    assert response.headers['location'] == 'https://example.test/demo.wav'
    materialize.assert_not_called()
