from pathlib import Path
import unittest
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ReleaseUploadTests(unittest.TestCase):
    def test_update_selection_does_not_trust_github_latest_label(self):
        sys.path.insert(0, str(ROOT / 'backend'))
        from api.routes import system
        payload = [
            {'tag_name': 'v8.3.2', 'draft': False, 'prerelease': False},
            {'tag_name': 'v8.3.3', 'draft': False, 'prerelease': False},
            {'tag_name': 'v9.0.0-beta', 'draft': False, 'prerelease': True},
        ]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return __import__('json').dumps(payload).encode()
        with patch('urllib.request.urlopen', return_value=Response()):
            self.assertEqual(system._latest_release()['tag_name'], 'v8.3.3')

    def test_node24_and_only_three_installation_assets(self):
        mac = (ROOT / '.github/workflows/release-macos.yml').read_text()
        win = (ROOT / '.github/workflows/release-windows.yml').read_text()
        for workflow in (mac, win):
            self.assertIn('node-version: 24', workflow)
        for line in mac.splitlines():
            if 'gh release upload' in line or 'gh release create' in line:
                self.assertIn('"$PKG"', line)
                self.assertNotIn('$ZIP', line)
        for line in win.splitlines():
            if 'gh release upload' in line or 'gh release create' in line:
                self.assertIn('"$INSTALLER" "$ZIP"', line)
                self.assertNotIn('windows-audit', line)
                self.assertNotIn('*', line)
        self.assertIn('name: windows-security-audit', win)
        self.assertIn('! -s "$PKG"', mac)


if __name__ == '__main__':
    unittest.main()
