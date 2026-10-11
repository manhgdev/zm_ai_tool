import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from pipeline.core import runtime_install as runtime
from pipeline.core.system_check import install


class VieneuInstallTests(unittest.TestCase):
    def test_resource_uses_vieneu_installer(self):
        from api.routes import system
        with patch.object(system, '_start_install_job', return_value={'running': True}) as start:
            self.assertTrue(system.api_install_resource('vieneu')['running'])
        self.assertEqual(start.call_args.args[0], 'vieneu_runtime')
        self.assertIs(start.call_args.args[1], install.install_vieneu_runtime)

    def test_windows_installs_only_vieneu_feature(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), patch.object(runtime, 'install_runtime', return_value={'ok': True}) as run:
            self.assertTrue(install.install_vieneu_runtime()['ok'])
        self.assertEqual(run.call_args.kwargs['features'], ('vieneu',))

    def test_native_installer_does_not_pull_whisper_ocr_or_speaker_models(self):
        with patch.object(install, '_runtime_torch_needs_install', return_value=False), patch.object(
            install, '_runtime_ort_accel', return_value='cpu'
        ), patch.object(install, '_runtime_pip_install') as pip, patch.object(install, '_mod_ok', return_value=(True, 'ok')), patch(
            'pipeline.core.runtime_site.bootstrap_ai_runtime'
        ), patch('pipeline.core.runtime_site.install_runtime_meta_path'), patch.object(install, '_invalidate_checks_cache'):
            self.assertTrue(install.install_vieneu_runtime()['ok'])
        specs = [spec for call in pip.call_args_list for spec in call.args]
        self.assertIn('vieneu==3.2.0', specs)
        self.assertIn('onnxruntime==1.20.1', specs)
        self.assertFalse(any(spec.startswith(('faster-whisper', 'rapidocr', 'sherpa', 'opencv')) for spec in specs))

    def test_windows_package_recipe_and_probe_use_selected_features(self):
        captured = {}
        def resolve(uv, profile, core, *args, **kwargs):
            captured['core'] = core
            captured['features'] = kwargs['features']
            return [runtime.Wheel(name, '1', name + '.whl', 'https://example.invalid/a', 'a' * 64, 1)
                    for name in ('numpy', 'torch', 'torchaudio', 'vieneu', 'onnxruntime')]
        with tempfile.TemporaryDirectory() as raw, patch.dict('os.environ', {'ZM_AI_TOOL_HOME': raw}), patch.object(
            runtime, 'resolve_wheels', side_effect=resolve
        ), patch.object(runtime, '_prefetch_packages') as prefetch:
            runtime._install_packages('uv', Path('python'), 'cpu', False, lambda *a, **kw: None, features=('vieneu',))
            groups = prefetch.call_args.args[1]
            self.assertEqual({name for group in groups for name in group['packages']}, {'numpy', 'torch', 'torchaudio', 'vieneu', 'onnxruntime'})
        self.assertEqual(captured['features'], ('vieneu',))
        self.assertFalse(any(spec.startswith(('ctranslate2', 'av==', 'opencv', 'pyclipper')) for spec in captured['core']))
        with patch.object(runtime.subprocess, 'run', return_value=CompletedProcess([], 0, 'ok', '')) as run:
            runtime._probe(Path('runtime'), 'cpu', features=('vieneu',))
        self.assertIn('--features=vieneu', run.call_args.args[0])

    def test_windows_feature_recipes_include_audio_io_without_other_sdks(self):
        for feature in ('whisper', 'ocr', 'diarization', 'demucs'):
            selected = () if feature == 'demucs' else (feature,)
            with self.subTest(feature=feature), tempfile.TemporaryDirectory() as raw, patch.dict('os.environ', {'ZM_AI_TOOL_HOME': raw}), patch.object(
                runtime, 'resolve_wheels', return_value=[runtime.Wheel(name, '1', name + '.whl', 'https://example.invalid/a', 'a' * 64, 1)
                    for name in ('numpy', 'torch', 'torchaudio', 'onnxruntime')]
            ) as resolve, patch.object(runtime, '_prefetch_packages'), patch.object(runtime, '_run'):
                runtime._install_packages('uv', Path('python'), 'cpu', feature == 'demucs', lambda *a, **kw: None, features=selected)
                core = resolve.call_args.args[2]
                names = {spec.split('==')[0] for spec in core}
                self.assertIn('soundfile', names)
                self.assertEqual(resolve.call_args.kwargs['features'], selected)
                if feature != 'whisper':
                    self.assertFalse(names & {'ctranslate2', 'av'})
                if feature != 'ocr':
                    self.assertFalse(names & {'opencv-python-headless', 'pillow'})
                self.assertNotIn('transformers', names)
                if feature == 'demucs':
                    self.assertTrue({'pyyaml', 'tqdm'} <= names)

    def test_each_optional_resource_routes_to_its_own_installer(self):
        from api.routes import system
        from pipeline.core import system_check
        with patch.object(system, '_start_install_job', return_value={'running': True}) as start:
            for resource_id, installer in (
                ('whisper', system_check.install_whisper_runtime),
                ('ocr', system_check.install_ocr_runtime),
                ('diarization', system_check.install_diarization_runtime),
                ('demucs', system_check.install_demucs_runtime),
            ):
                self.assertTrue(system.api_install_resource(resource_id)['running'])
                self.assertEqual(start.call_args.args[0], f'{resource_id}_runtime')
                self.assertIs(start.call_args.args[1], installer)

    def test_native_feature_install_lists_only_requested_sdk(self):
        with patch.object(install, '_runtime_ort_accel', return_value='cpu'), patch.object(
            install, '_runtime_pip_install'
        ) as pip, patch.object(install, '_mod_ok', return_value=(True, 'ok')), patch(
            'pipeline.core.runtime_site.bootstrap_ai_runtime'
        ), patch('pipeline.core.runtime_site.install_runtime_meta_path'), patch.object(install, '_invalidate_checks_cache'):
            install._install_native_optional_feature('ocr')
        specs = [spec for call in pip.call_args_list for spec in call.args]
        self.assertIn('rapidocr-onnxruntime==1.4.4', specs)
        self.assertFalse(any(spec.startswith(('faster-whisper', 'sherpa-onnx', 'vieneu')) for spec in specs))

    def test_native_diarization_install_includes_soundfile(self):
        with patch.object(install, '_runtime_ort_accel', return_value='cpu'), patch.object(
            install, '_runtime_pip_install'
        ) as pip, patch.object(install, '_mod_ok', return_value=(True, 'ok')), patch(
            'pipeline.core.runtime_site.bootstrap_ai_runtime'
        ), patch('pipeline.core.runtime_site.install_runtime_meta_path'), patch.object(install, '_invalidate_checks_cache'), patch(
            'pipeline.asr.speaker.ensure_diarization_models'
        ):
            install._install_native_optional_feature('diarization')
        specs = [spec for call in pip.call_args_list for spec in call.args]
        self.assertIn('soundfile==0.13.1', specs)

    def test_add_vieneu_retains_existing_ocr_and_only_commits_after_probe(self):
        from tests.test_runtime_install import TransactionTests
        fixture = TransactionTests('test_valid_runtime_skips_network_and_python_preparation')
        fixture.setUp()
        try:
            (fixture.old / 'Lib/site-packages/rapidocr_onnxruntime').mkdir(parents=True)
            calls = []
            def probe(root, profile, demucs, **kwargs):
                calls.append(kwargs['features'])
                if root == fixture.old:
                    raise runtime.RuntimeInstallError('RUNTIME_PROBE_FAILED', 'missing VieNeu')
                return 'ok'
            with patch.object(runtime, '_prepare', side_effect=lambda uv, root: fixture.make_python(root)), patch.object(
                runtime, '_install_packages'
            ) as packages, patch.object(runtime, '_probe', side_effect=probe):
                runtime.install_runtime(features=('vieneu',))
            self.assertEqual(calls, [('ocr', 'vieneu'), ('ocr', 'vieneu')])
            self.assertEqual(packages.call_args.kwargs['features'], ('ocr', 'vieneu'))
            pointer = json.loads((fixture.root / 'current.json').read_text())
            self.assertEqual(pointer['features'], ['ocr', 'vieneu'])
        finally:
            fixture.doCleanups()

    def test_demucs_only_pointer_and_rollback_probe_keep_optional_features_empty(self):
        from tests.test_runtime_install import TransactionTests
        fixture = TransactionTests('test_valid_runtime_skips_network_and_python_preparation')
        fixture.setUp()
        try:
            with patch.object(runtime, '_prepare', side_effect=lambda uv, root: fixture.make_python(root)), patch.object(
                runtime, '_install_packages'
            ), patch.object(runtime, '_probe', side_effect=[runtime.RuntimeInstallError('RUNTIME_PROBE_FAILED', 'missing Demucs'), 'ok']):
                runtime.install_runtime(features=(), demucs=True)
            pointer = json.loads((fixture.root / 'current.json').read_text())
            self.assertEqual(pointer['features'], ['demucs'])
        finally:
            fixture.doCleanups()
        with patch.object(runtime, '_probe', return_value='ok') as probe:
            with tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                root = home / 'runtime'
                previous = root / 'previous'
                root.mkdir(parents=True)
                (previous / 'Scripts').mkdir(parents=True)
                (previous / 'Scripts' / 'python.exe').touch()
                (root / 'previous.json').write_text(json.dumps({
                    'path': 'previous', 'runtimePack': 'cpu', 'features': ['demucs'],
                }))
                with patch.object(runtime, 'runtime_home', return_value=home), patch.object(runtime, 'active_runtime_dir', return_value=previous), patch.object(runtime, 'transaction', return_value=contextlib.nullcontext()):
                    runtime.rollback_runtime()
        self.assertEqual(probe.call_args.kwargs['features'], ())


if __name__ == '__main__':
    unittest.main()
