"""Worker failure boundaries; optional real CPU/cache test uses installed runtime."""
import json
import math
import queue
import struct
import sys
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.tts.engines import openvoice


def test_worker_failure_is_controlled():
    responses = queue.Queue()
    responses.put({'ok': False, 'error': 'OPENVOICE_WORKER_FAILED'})
    process = Mock()
    process.poll.return_value = None
    with patch.object(openvoice, '_process', process), patch.object(openvoice, '_responses', responses), patch.object(openvoice, 'status', return_value={'ready': True}), patch.object(openvoice, '_stop') as stop:
        with pytest.raises(RuntimeError, match='OPENVOICE_WORKER_FAILED'):
            openvoice._request({})
        stop.assert_called_once()


def test_worker_timeout_kills_worker():
    process = Mock()
    process.poll.return_value = None
    with patch.object(openvoice, '_process', process), patch.object(openvoice, '_responses', queue.Queue()), patch.object(openvoice, 'status', return_value={'ready': True}), patch.object(openvoice, '_stop') as stop, patch.object(openvoice.time, 'monotonic', side_effect=[0, 181]):
        with pytest.raises(RuntimeError, match='OPENVOICE_TIMEOUT'):
            openvoice._request({})
        stop.assert_called_once()


def test_cancel_stops_only_converter_worker():
    process = Mock()
    process.poll.return_value = None
    with patch.object(openvoice, '_process', process), patch.object(openvoice, '_responses', queue.Queue()), patch.object(openvoice, 'status', return_value={'ready': True}), patch.object(openvoice, '_stop') as stop:
        with pytest.raises(RuntimeError, match='OPENVOICE_CANCELLED'):
            openvoice._request({}, cancel_check=lambda: True)
        stop.assert_called_once()


@pytest.mark.skipif(not openvoice.status()['ready'] or sys.platform == 'win32', reason='Optional isolated OpenVoice runtime required; Windows needs native verification')
def test_real_worker_cache_and_recovery(tmp_path, monkeypatch):
    installed = openvoice.ROOT
    interpreter = openvoice.python_path()
    for filename in ['checkpoint.pth', 'config.json', f'OpenVoice-{openvoice.REVISION}']:
        (tmp_path / filename).symlink_to(installed / filename)
    monkeypatch.setattr(openvoice, 'ROOT', tmp_path)
    monkeypatch.setattr(openvoice, 'python_path', lambda: interpreter)
    (tmp_path / 'ready.json').write_text(json.dumps({'version': openvoice.VERSION}))
    source = tmp_path / 'tone.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(i * 2 * math.pi * 220 / 24000))) for i in range(48000)))
    payload = {'source': str(source), 'reference': str(source), 'output': str(tmp_path / 'out.wav')}
    try:
        with openvoice._lock:
            assert openvoice._request(payload)['ok']
            cache = list((tmp_path / 'embeddings').glob('*.pt'))
            assert len(cache) == 1
            before = cache[0].stat().st_mtime_ns
            assert openvoice._request(payload)['ok']
            assert cache[0].stat().st_mtime_ns == before
            cache[0].write_bytes(b'corrupt cache')
            assert openvoice._request(payload)['ok']
            assert cache[0].stat().st_size > 100
            empty = tmp_path / 'empty.wav'
            empty.write_bytes(b'')
            with pytest.raises(RuntimeError):
                openvoice._request({**payload, 'source': str(empty)})
            assert openvoice._request(payload)['ok']
    finally:
        openvoice._stop()
