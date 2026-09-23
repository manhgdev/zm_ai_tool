from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseUploadTests(unittest.TestCase):
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
