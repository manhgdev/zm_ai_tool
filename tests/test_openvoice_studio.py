"""Run with backend/.venv/bin/python -m unittest discover -s tests -p test_openvoice_studio.py."""
import json
import sys
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.tts import studio


class StudioRoutingTests(unittest.TestCase):
    def test_text_srt_cache_and_options(self):
        route = {'engine': 'openvoice', 'language': 'zh', 'sourceVoice': 'cc:test:1'}
        phrase = '在这峡谷深处常在群胸某预中'
        calls = []

        def synth(text, voice, output, target, match, **options):
            calls.append((text, voice, target, match, options))
            with wave.open(str(output), 'wb') as audio:
                audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\x10\x01' * 24000)
            options['on_progress'](1)
            return 1.0

        with tempfile.TemporaryDirectory(prefix='openvoice-studio-test-') as temp, ExitStack() as stack:
            root = Path(temp)
            for name, value in [('TTS_OUTPUT', root), ('TTS_TEMP', root)]:
                stack.enter_context(patch.object(studio, name, value))
            stack.enter_context(patch.object(studio, 'ensure_vieneu_dirs'))
            stack.enter_context(patch.object(studio, 'list_voices', return_value=[]))
            stack.enter_context(patch.object(studio, 'reference_cache_token', return_value='reference'))
            token = stack.enter_context(patch.object(studio, 'clone_cache_token', return_value='checkpoint-v1'))
            stack.enter_context(patch.object(studio, 'clone_route', return_value=route))
            stack.enter_context(patch.object(studio, 'tts_segment', side_effect=synth))
            options = dict(voice='vn:clone:test', lang='zh', speed=1.1, volume=0.9, pitch=1.0)
            result = studio.synth_text_job(text=phrase, **options)
            self.assertEqual(calls[0][0], phrase)
            self.assertEqual(result['meta']['cloneRoute'], route)
            self.assertEqual(calls[0][4]['speed'], 1.1)
            self.assertTrue(studio.synth_text_job(text=phrase, **options)['cached'])
            self.assertEqual(len(calls), 1)
            token.return_value = 'checkpoint-v2'
            self.assertFalse(studio.synth_text_job(text=phrase, **options)['cached'])
            srt = f'1\n00:00:01,000 --> 00:00:02,000\n{phrase}\n'
            result = studio.synth_srt_job(srt_text=srt, keep_timeline=True, **options)
            written = (root / result['id'] / 'subs.srt').read_text(encoding='utf-8')
            self.assertIn('00:00:01,000 --> 00:00:02,000', written)
            self.assertIn(phrase, written)
            self.assertEqual(calls[-1][2:4], (1.0, 'stretch'))
            self.assertTrue((root / result['id'] / 'audio.wav').stat().st_size > 128)
            metadata = json.loads((root / result['id'] / 'meta.json').read_text())
            self.assertEqual(metadata['cloneRoute']['sourceVoice'], 'cc:test:1')


if __name__ == '__main__':
    unittest.main()
