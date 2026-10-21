"""Run: backend/.venv/bin/python -m unittest discover -s tests -p test_windows_release_diagnostic.py"""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WindowsDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.load((ROOT / '.github/workflows/release-windows.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        self.script = (ROOT / 'scripts/diagnose-windows-release.ps1').read_text(encoding='utf-8')

    def test_diagnostic_cannot_publish_or_build(self):
        jobs = self.workflow['jobs']
        self.assertEqual(jobs['build']['if'], "inputs.audit_release == ''")
        diagnostic = jobs['diagnose']
        self.assertEqual(diagnostic['runs-on'], 'windows-2022')
        self.assertEqual(diagnostic['permissions'], {'contents': 'read'})
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        preparation = (ROOT / 'scripts/prepare-windows-defender.ps1').read_text(encoding='utf-8')
        steps = '\n'.join(step.get('run', '') for step in diagnostic['steps']) + self.script + preparation
        for forbidden in ('gh release upload', 'gh release create', 'npm run build', 'git push', 'Add-MpPreference', '-DisableRemediation', '-Restore', '-DisableArchiveScanning $true', '-DisableRealtimeMonitoring $true'):
            self.assertNotIn(forbidden, steps)
        self.assertIn("Remove-MpPreference -ExclusionPath 'C:\\', 'D:\\'", steps)
        self.assertIn('Set-MpPreference -DisableArchiveScanning $false', steps)
        self.assertIn("$env:RUNNER_ENVIRONMENT -ne 'github-hosted'", steps)
        self.assertTrue(any(step.get('if') == 'always()' and 'upload-artifact' in step.get('uses', '') for step in diagnostic['steps']))
        for job in (diagnostic, jobs['build']):
            self.assertTrue(any('prepare-windows-defender.ps1' in step.get('run', '') for step in job['steps']))
        for step in jobs['build']['steps']:
            if any(script in step.get('run', '') for script in ('prepare-windows-defender.ps1', 'audit-windows-release.ps1', 'check-windows-desktop.ps1')):
                self.assertEqual(step['if'], 'inputs.security_audit == true')

    def test_installer_is_verified_and_scanned_before_execution(self):
        positions = [self.script.index(text) for text in (
            'gh release download', '$file = Record-File $installer',
            '$file.sha256 -ne $report.expectedSha256', 'Scan-File $installer',
            'Run-Checked $installer', "scripts/windows_release_audit.py", "Run-Checked $exe")]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(self.script.index('Remove-Item Env:GH_TOKEN'), self.script.index('Run-Checked $installer'))

    def test_failures_always_save_forensic_report(self):
        finally_block = self.script[self.script.index('} finally {'):]
        for required in ('defender-detections.json', 'defender-events.json', 'missingFiles', 'diagnosis.json'):
            self.assertIn(required, finally_block)
        self.assertIn("$report.status = 'scanner_unavailable'", self.script)
        self.assertIn("$report.status = 'hash_mismatch'", self.script)
        self.assertIn("$report.status = 'detected'", finally_block)
        self.assertIn('InitialDetectionTime -ge $started', self.script)
        self.assertIn('sha256 = $before.sha256', self.script)

    def test_diagnostic_launches_normal_desktop_after_scanning(self):
        self.assertLess(self.script.index("'scan-payload'"), self.script.index("'desktop-startup'"))
        self.assertIn("'check-windows-desktop.ps1'", self.script)
        build_steps = self.workflow['jobs']['build']['steps']
        self.assertTrue(any('-CheckSystemTts' in step.get('run', '') for step in build_steps))


if __name__ == '__main__':
    unittest.main()
