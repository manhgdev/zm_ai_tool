import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.windows_release_audit import inventory


class ArtifactAuditTests(unittest.TestCase):
    def test_inventory_hashes_exe_binaries_and_both_release_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / 'bundle'
            bundle.mkdir()
            (bundle / 'ZM AI TOOL.exe').write_bytes(b'exe')
            (bundle / 'python312.dll').write_bytes(b'dll')
            setup, portable = root / 'Setup.exe', root / 'Portable.zip'
            setup.write_bytes(b'setup')
            portable.write_bytes(b'zip')
            data = inventory(bundle, setup, portable)
            self.assertEqual(len(data['files']), 2)
            self.assertTrue(all(row['binary'] for row in data['files']))
            self.assertEqual(data['assets'][0]['sha256'], hashlib.sha256(b'setup').hexdigest())

    def test_audit_is_required_before_publish_and_does_not_weaken_defender(self):
        workflow = (ROOT / '.github/workflows/release-windows.yml').read_text()
        script = (ROOT / 'scripts/audit-windows-release.ps1').read_text()
        launcher = (ROOT / 'build_app/launcher.py').read_text()
        self.assertLess(workflow.index('Inventory and scan'), workflow.index('Publish GitHub Release'))
        self.assertIn('Get-MpComputerStatus', script)
        self.assertIn('Get-AuthenticodeSignature', script)
        self.assertIn('Get-FileHash', script)
        self.assertNotIn('Set-MpPreference', script)
        self.assertNotIn('Add-MpPreference', script)
        self.assertNotIn('Zone.Identifier', launcher.split('def prepare_pythonnet')[0])
        self.assertNotIn('unblock_windows_motw', launcher)

    def test_updater_refuses_wrong_hash(self):
        sys.path.insert(0, str(ROOT / 'backend'))
        from api.routes.system import _verify_update
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / 'Setup.exe'
            target.write_bytes(b'payload')
            _verify_update(target, hashlib.sha256(b'payload').hexdigest())
            target.write_bytes(b'tampered')
            with self.assertRaisesRegex(RuntimeError, 'CHECKSUM_MISMATCH'):
                _verify_update(target, hashlib.sha256(b'payload').hexdigest())
