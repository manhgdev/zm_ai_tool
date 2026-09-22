import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.service import _PROJECT_RE, FlowService
from pipeline.flow.browser import profile_lock


class SessionTests(unittest.TestCase):
    def test_login_redirect_never_becomes_a_flow_project(self):
        project = '6e98f844-554a-4e93-898c-4b3096681a04'
        for url in [f'https://flow.google.com/project/{project}', f'/project/{project}?x=1', f'https://labs.google/fx/tools/flow/project/{project}']:
            self.assertEqual(_PROJECT_RE.search(url).group(1), project)
        for url in [f'https://accounts.google.com/ServiceLogin?continue=https://flow.google.com/project/{project}&followup=https:', f'https://evil.test/project/{project}', f'https://flow.google.com/project/{project}&followup=https:']:
            self.assertIsNone(_PROJECT_RE.search(url))

    def test_snapshot_owns_same_lock_as_browser(self):
        with tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.service.store.profile_dir', return_value=Path(raw)):
            service = FlowService()
            def copy(account, job):
                self.assertTrue(profile_lock(Path(raw)).locked())
                return Path(raw)
            with patch.object(service, '_clone_closed_profile', side_effect=copy):
                service._clone_runtime_profile('account', 'job')
            self.assertFalse(profile_lock(Path(raw)).locked())
