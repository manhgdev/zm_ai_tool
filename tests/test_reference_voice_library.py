"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_reference_voice_library.py"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api.routes.tts_voices import api_voices
from pipeline.tts import manager, voice_store
from pipeline.tts.engines import moss_tts_nano, vieneu


class ReferenceVoiceLibraryTest(unittest.TestCase):
    def account(self, root, name):
        data = root / name
        voices = data / 'voices' / 'vieneu'
        return patch.multiple(
            voice_store, DATA=data, SHARED_REFERENCE_ROOT=root / 'shared',
            REFERENCE_ROOT=data / 'voice-ref', REFERENCE_VOICES_JSON=data / 'voice-ref' / 'voices.json',
            VIENEU_ROOT=voices, VOICES_JSON=voices / 'voices.json',
            CLONED_DIR=voices / 'cloned', PRESETS_DIR=voices / 'presets', CACHE_DIR=voices / 'cache',
            TTS_OUTPUT=data / 'output', TTS_TEMP=data / 'temp',
        )

    def test_api_shares_live_library_and_keeps_new_clones_private(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(vieneu.zmtss_catalog, 'voices', return_value=[]), \
             patch.object(vieneu, 'list_preset_from_assets', return_value=[]), \
             patch.object(manager, '_cc_voice_options', return_value=[]), \
             patch.object(manager, '_el_voice_options', return_value=[]), \
             patch.object(manager.system_engine, 'list_voices', return_value=[]):
            root = Path(directory)
            shared = root / 'shared'
            shared.mkdir()
            entries = [{'id': 'common', 'name': 'Common', 'engine': 'vieneu', 'language': 'zh',
                        'type': 'zmAI', 'ref_file': 'common.wav', 'hidden': False}]
            catalog = shared / 'voices.json'
            catalog.write_text(json.dumps(entries))
            (shared / 'common.wav').write_bytes(b'shared audio')
            for account in ('first', 'second'):
                # Existing account registries must not hide the shared library.
                private = root / account / 'voice-ref'
                private.mkdir(parents=True)
                (private / 'voices.json').write_text('[]')
                with self.account(root, account):
                    result = api_voices('all')
                    self.assertEqual([v['id'] for v in result], ['common'])
                    self.assertEqual(vieneu.preview_path('common'), shared / 'common.wav')
                    self.assertEqual(moss_tts_nano.reference_path('common'), (shared / 'common.wav').resolve())
                    voice_store.ensure_vieneu_dirs()
                    (voice_store.CLONED_DIR / f'{account}.wav').write_bytes(b'private clone')
                    voice_store.add_cloned(account, account, f'cloned/{account}.wav')
                    ids = {v['id'] for v in api_voices('all')}
                    self.assertEqual(ids, {'common', f'vn:clone:{account}'})
                    self.assertEqual((private / 'voices.json').read_text(), '[]')
            # A library update reaches both existing accounts without copying it.
            entries.append({**entries[0], 'id': 'new', 'name': 'New'})
            catalog.write_text(json.dumps(entries))
            for account in ('first', 'second'):
                with self.account(root, account):
                    ids = {v['id'] for v in api_voices('all')}
                    self.assertEqual(ids, {'common', 'new', f'vn:clone:{account}'})
                    self.assertTrue(voice_store.update_reference('common', favorite=True))
                    self.assertTrue(voice_store.get_reference_voice('common')['favorite'])
                    saved = json.loads(voice_store.REFERENCE_VOICES_JSON.read_text())
                    self.assertEqual([v['id'] for v in saved], ['common'])
            self.assertEqual(json.loads(catalog.read_text()), entries)


if __name__ == '__main__':
    unittest.main()
