import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from api.routes import system


class CleanupTests(unittest.TestCase):
    def test_all_preserves_activation_keys_and_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, public = root / 'data', root / 'public'
            targets = {key: (public if key.startswith('public_') else data) / path.relative_to(system.PUBLIC_DATA if key.startswith('public_') else system.DATA) for key, path in system.CLEANUP_TARGETS.items()}
            for path in targets.values():
                path.mkdir(parents=True, exist_ok=True)
                (path / 'junk').write_text('temporary')
            protected = [data / name for name in ('license.json', 'app_config.json', 'ui_preferences.json', 'flow/accounts.json', 'chat/profiles/token.json', 'voices/vieneu/voices.json')]
            for path in protected:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('keep')
            with patch.object(system, 'DATA', data), patch.object(system, 'PUBLIC_DATA', public), patch.object(system, 'CLEANUP_TARGETS', targets):
                self.assertEqual(system.api_cleanup(system.CleanupBody(targets=[]))['deleted'], [])
                self.assertTrue(system.api_cleanup(system.CleanupBody())['ok'])
                self.assertTrue(all(path.read_text() == 'keep' for path in protected))
                self.assertTrue(all(not list(path.iterdir()) for path in targets.values()))
                targets['data_cache'].rmdir()
                targets['data_cache'].symlink_to(data / 'flow', target_is_directory=True)
                self.assertFalse(system.api_cleanup(system.CleanupBody(targets=['data_cache']))['ok'])
                self.assertEqual((data / 'flow/accounts.json').read_text(), 'keep')
                self.assertEqual((data / 'app_config.json').read_text(), 'keep')

    def test_inventory_discovers_projects_and_reports_protected_areas(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, public = Path(tmp) / 'data', Path(tmp) / 'public'
            for path in (data / 'chat', data / 'drawing/hands', data / 'drawing/job-one', public / 'project-one'):
                path.mkdir(parents=True)
                (path / 'payload').write_text('abc')
            (public / 'project-one/meta.json').write_text('{}')
            (data / 'license.json').write_text('keep-license')
            with patch.object(system, 'DATA', data), patch.object(system, 'PUBLIC_DATA', public), patch.object(system, 'CLEANUP_TARGETS', {}), patch.object(system, 'CLEANUP_PROTECTED', {data / 'license.json', data / 'chat'}), patch('pipeline.core.nvidia_driver._active_jobs', return_value=False), patch('pipeline.flow.service.service.jobs', return_value=[]):
                rows = system._cleanup_inventory()
                self.assertTrue(rows['public_project:project-one']['selectable'])
                self.assertTrue(rows['data_drawing:job-one']['selectable'])
                self.assertFalse(rows['data_protected:chat']['selectable'])
                self.assertFalse(rows['data_protected:drawing/hands']['selectable'])
                self.assertEqual(rows['public_project:project-one']['items'], 2)
                with self.assertRaises(system.HTTPException):
                    system.api_cleanup(system.CleanupBody(targets=['data_protected:license.json']))
                self.assertTrue(system.api_cleanup(system.CleanupBody(targets=['public_project:project-one']))['ok'])
                self.assertEqual((data / 'license.json').read_text(), 'keep-license')
                with patch.object(system, 'CLEANUP_TARGETS', {'data_root': data}):
                    self.assertFalse(system.api_cleanup(system.CleanupBody(targets=['data_root']))['ok'])
                with patch('pipeline.core.nvidia_driver._active_jobs', return_value=True):
                    with self.assertRaises(system.HTTPException) as raised:
                        system.api_cleanup(system.CleanupBody(targets=['data_drawing:job-one']))
                    self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual((data / 'drawing/job-one/payload').read_text(), 'abc')
