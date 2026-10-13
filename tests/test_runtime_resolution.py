import sys
import unittest
import tempfile
import tomllib
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core.runtime_resolution import select_wheels, resolve
from pipeline.core.runtime_download import RuntimeInstallError


class ResolutionTests(unittest.TestCase):
    def test_vieneu_resolution_has_no_other_ai_sdks(self):
        for profile in ('cpu', 'directml', 'nvidia-cu124'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as raw, patch(
                'pipeline.core.runtime_resolution.select_wheels', return_value=['resolved'],
            ):
                def run(command, *args):
                    Path(command[command.index('--output-file') + 1]).write_text('packages = []')
                result = resolve('uv', profile, ('soundfile==0.13.1',), Path(raw), run, lambda *a, **k: None, features=('vieneu',))
                self.assertEqual(result, ['resolved'])
                project = next(Path(raw).rglob('pyproject.toml'))
                dependencies = tomllib.loads(project.read_text())['project']['dependencies']
                self.assertIn('vieneu==3.2.0', dependencies)
                self.assertFalse(any(spec.startswith(('faster-whisper', 'rapidocr', 'sherpa')) for spec in dependencies))
                self.assertEqual(sum(spec.startswith('onnxruntime') for spec in dependencies), 1)

    def test_ctranslate_runtime_pins_setuptools_with_pkg_resources(self):
        from pipeline.core.runtime_install import CORE
        self.assertIn('ctranslate2==4.6.0', CORE)
        self.assertEqual([spec for spec in CORE if spec.startswith('setuptools')], ['setuptools==80.9.0'])

    def test_bundled_exe_stack_matches_runtime_pins(self):
        from pipeline.core.runtime_install import CORE
        text = (Path(__file__).resolve().parents[1] / 'backend' / 'requirements-app.txt').read_text()
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
        for name in ('numpy', 'pillow', 'opencv-python-headless'):
            spec = next(s for s in CORE if s.startswith(name + '=='))
            self.assertIn(spec, lines)
        self.assertIn('onnxruntime==1.20.1', lines)  # == CPU provider in runtime_resolution.resolve
        self.assertNotIn('rapidocr-onnxruntime', ' '.join(lines))

    def test_selects_only_windows_cp312_and_keeps_hash(self):
        packages = [{'name': 'test', 'version': '1.0', 'wheels': [
            {'url': f'https://example.invalid/test-1.0-{tag}.whl', 'hashes': {'sha256': 'a' * 64}, 'size': 100}
            for tag in ('cp312-cp312-manylinux2014_x86_64', 'cp311-cp311-win_amd64', 'cp312-cp312-win_amd64')
        ]}]
        result = select_wheels({'packages': packages})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].filename.endswith('cp312-cp312-win_amd64.whl'))
        self.assertEqual(result[0].digest, 'a' * 64)

    def test_rejects_missing_hash_and_unusable_or_unsafe_wheel(self):
        for url in ('http://example.invalid/test-1.0-py3-none-any.whl', 'https://example.invalid/test-1.0-cp311-cp311-win_amd64.whl', 'https://example.invalid/test-1.0-py3-none-any.whl'):
            with self.assertRaises(RuntimeInstallError):
                select_wheels({'packages': [{'name': 'test', 'version': '1.0', 'wheels': [{'url': url}]}]})
