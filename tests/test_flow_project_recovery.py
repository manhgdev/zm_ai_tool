import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow import store
from pipeline.flow.service import FlowService


class ProjectRecoveryTest(unittest.TestCase):
    def test_bind_flow_project_updates_api_and_client(self):
        api = SimpleNamespace(project_id='old', _project_page_url='https://flow.google.com/project/old')
        client = SimpleNamespace(project_id='old', _project_url='https://flow.google.com/project/old')
        FlowService._bind_flow_project(api, client, 'new-id')
        self.assertEqual(api.project_id, 'new-id')
        self.assertEqual(api._project_page_url, 'https://flow.google.com/project/new-id')
        self.assertEqual(client.project_id, 'new-id')
        self.assertEqual(client._project_url, 'https://flow.google.com/project/new-id')

    def test_ensure_account_project_page_creates_replacement(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            store._write('accounts', [{
                'id': 'acc1',
                'status': 'online',
                'projectId': 'dead-project',
                'email': 'a@b.c',
            }])
            service = FlowService()
            page = MagicMock()
            page.url = 'https://flow.google.com/404?reason=project'
            api = SimpleNamespace(project_id='dead-project', _project_page_url='https://flow.google.com/project/dead-project')
            client = SimpleNamespace(
                project_id='dead-project',
                _project_url='https://flow.google.com/project/dead-project',
                _ensure_project_page=AsyncMock(side_effect=[
                    RuntimeError('FLOW_PROJECT_NOT_FOUND: gone'),
                    None,
                ]),
            )
            account = store.get_row('accounts', 'acc1')
            with patch.object(service, '_create_flow_project_ui', AsyncMock(return_value='fresh-project')):
                refreshed = asyncio.run(service._ensure_account_project_page(
                    page, api=api, client=client, account=account, job_id='job1',
                ))
            self.assertEqual(refreshed['projectId'], 'fresh-project')
            self.assertEqual(store.get_row('accounts', 'acc1')['projectId'], 'fresh-project')
            self.assertEqual(api.project_id, 'fresh-project')
            self.assertEqual(client.project_id, 'fresh-project')
            self.assertEqual(client._ensure_project_page.await_count, 2)


class RetryFreshGenerationTest(unittest.TestCase):
    def test_retry_clears_submission_identity_for_new_media(self):
        import threading
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            store._write('accounts', [{'id': 'a1', 'status': 'online', 'projectId': 'p1', 'plan': 'Pro'}])
            store._write('jobs', [{
                'id': 'j1', 'status': 'done', 'kind': 'image', 'accountId': 'a1',
                'queueOrder': 0, 'settings': {'model': 'Nano Banana 2', 'ratio': '1:1'},
                'mediaIds': ['m1'], 'outputs': ['/tmp/x.png'], 'submissionStartedAt': 1.0,
                'submissionProjectId': 'p1', 'resumeOnly': True, 'baselineMediaIds': ['m1'],
                'prompt': 'hi',
            }])
            service = FlowService()
            with patch.object(threading.Thread, 'start', lambda self: None):
                job = service.retry('j1')
            self.assertEqual(job['status'], 'queued')
            self.assertFalse(job['resumeOnly'])
            self.assertEqual(job.get('mediaIds'), [])
            self.assertEqual(job.get('outputs'), [])
            self.assertIsNone(job.get('submissionStartedAt'))
