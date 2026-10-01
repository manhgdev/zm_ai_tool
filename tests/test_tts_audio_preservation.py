import math
import wave
from array import array
from unittest.mock import patch

import pytest

from pipeline.tts import audio_utils, manager, studio


@pytest.mark.parametrize('trailing', [False, True])
def test_trim_keeps_speech_after_internal_pause(tmp_path, trailing):
    path = tmp_path / 'two_phrases.wav'
    rate = 48000
    tone = array('h', (int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)))
    silence = array('h', [0] * (rate // 2))
    samples = silence + tone + silence + tone + silence
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())
    duration = audio_utils.trim_silence(path, trailing=trailing)
    assert duration >= 2.3, 'The second phrase was cut at the internal pause'
    with wave.open(str(path), 'rb') as result:
        result.setpos(int(2.1 * rate))
        second_phrase = array('h', result.readframes(rate // 5))
    assert max(abs(sample) for sample in second_phrase) > 10000


def test_manager_passes_original_text_to_provider(tmp_path):
    text = 'Mã A 7\nCâu hai: xin chào!'
    with (
        patch.object(manager, 'resolve_voice', return_value='vn:test'),
        patch.object(manager.vieneu_engine, 'parse_voice', return_value=('preset', 'test')),
        patch.object(manager.vieneu_engine, 'synthesize', side_effect=RuntimeError('provider reached')) as synth,
    ):
        with pytest.raises(RuntimeError, match='provider reached'):
            manager.tts_segment(text, 'vn:test', tmp_path / 'out.wav', None, 'none')
    assert synth.call_args.args[0] == text


@pytest.mark.parametrize('srt', [False, True])
def test_studio_keeps_original_text_and_lines(tmp_path, srt):
    text = 'Mã A 7\nCâu hai: xin chào!'
    received = []

    def synth(chunk, voice, path, *args, **kwargs):
        received.append(chunk)
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x10\x01' * 48000)
        return 1.0

    with (
        patch.object(studio, 'TTS_OUTPUT', tmp_path),
        patch.object(studio, 'ensure_vieneu_dirs'),
        patch.object(studio, '_job_fingerprint', return_value='test'),
        patch.object(studio, '_find_cached_job', return_value=None),
        patch.object(studio, '_voice_display', return_value='test'),
        patch.object(studio, '_publish_job_progress'),
        patch.object(studio, 'prune_history'),
        patch.object(studio, 'tts_segment', side_effect=synth),
    ):
        if srt:
            studio.synth_srt_job(srt_text=f'1\n00:00:00,000 --> 00:00:01,000\n{text}\n', voice='system', keep_timeline=False)
        else:
            studio.synth_text_job(text=text, voice='system', auto_split=False)
    assert received == [text]
