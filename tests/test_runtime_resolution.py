import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core.runtime_resolution import select_wheels
from pipeline.core.runtime_download import RuntimeInstallError


class ResolutionTests(unittest.TestCase):
    def test_ctranslate_runtime_pins_setuptools_with_pkg_resources(self):
        from pipeline.core.runtime_install import CORE
        self.assertIn('ctranslate2==4.6.0', CORE)
        self.assertEqual([spec for spec in CORE if spec.startswith('setuptools')], ['setuptools==80.9.0'])

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
