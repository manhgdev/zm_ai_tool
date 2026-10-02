import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pipeline.flow import store
from pipeline.flow.service import FlowService


class FlowDownloadRetryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root_patch = patch.object(store, 'ROOT', Path(self.directory.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.service = FlowService()
        self.account = {'id': 'a', 'status': 'online', 'projectId': 'p', 'plan': 'Pro', 'credits': 100}
        store._write('accounts', [self.account])
        store._write('jobs', [{'id': 'j', 'accountId': 'a', 'status': 'queued', 'kind': 'video',
                              'queueOrder': 0, 'settings': {}, 'mediaIds': ['flow-thumb:original']}])

    def test_menu_timeout_retries_download_only_and_preserves_finished_parts(self):
        output = Path(self.directory.name) / 'video.mp4'
        async def download(*args):
            if downloader.await_count == 1:
                raise PlaywrightTimeoutError('menuitem timeout')
            output.write_bytes(b'video')
        with patch.object(self.service, '_download_video_via_flow_menu_once', side_effect=download) as downloader, patch(
            'pipeline.flow.service.asyncio.sleep', new_callable=AsyncMock
        ), patch.object(self.service, '_run', new_callable=AsyncMock) as run:
            asyncio.run(self.service._download_video_via_flow_menu(None, 0, output, '720p', job_id='j'))
            # A later recovery skips a part already persisted by this job.
            asyncio.run(self.service._download_video_via_flow_menu(None, 0, output, '720p', job_id='j'))
        self.assertEqual(downloader.await_count, 2)
        run.assert_not_awaited()
        self.assertEqual(store.get_row('jobs', 'j')['outputs'], [str(output)])
        self.assertEqual(store.get_row('jobs', 'j')['mediaIds'], ['flow-thumb:original'])

    def test_download_exhaustion_leaves_existing_file_and_media_intact(self):
        output = Path(self.directory.name) / 'video.mp4'
        output.write_bytes(b'previous output')
        with patch.object(self.service, '_download_video_via_flow_menu_once', new_callable=AsyncMock,
                          side_effect=PlaywrightTimeoutError('menuitem timeout')) as downloader, patch(
            'pipeline.flow.service.asyncio.sleep', new_callable=AsyncMock
        ):
            with self.assertRaisesRegex(RuntimeError, 'FLOW_DOWNLOAD_FAILED'):
                asyncio.run(self.service._download_video_via_flow_menu(None, 0, output, '720p', job_id='j'))
        self.assertEqual(downloader.await_count, 3)
        self.assertEqual(output.read_bytes(), b'previous output')
        self.assertEqual(store.get_row('jobs', 'j')['mediaIds'], ['flow-thumb:original'])

    def test_worker_does_not_restart_generation_or_switch_account_after_download_error(self):
        output = Path(self.directory.name) / 'part1.mp4'
        output.write_bytes(b'completed part')
        async def failed_run(job_id, **kwargs):
            store.patch_row('jobs', job_id, {'status': 'failed', 'stage': 'failed',
                            'error': 'Locator.wait_for: Timeout 5000ms exceeded: menuitem',
                            'outputs': [str(output)], 'allowAccountFallback': True})
        with patch.object(self.service, '_verify_account_plan_before_enqueue', return_value=self.account), patch.object(
            self.service, '_clone_runtime_profile', return_value=None
        ), patch.object(self.service, '_run', side_effect=failed_run) as run, patch.object(
            self.service, '_pick_eligible_account'
        ) as pick:
            self.service._run_sync('j')
        self.assertEqual(run.call_count, 1)
        pick.assert_not_called()
        job = store.get_row('jobs', 'j')
        self.assertEqual(job['outputs'], [str(output)])
        self.assertEqual(job['mediaIds'], ['flow-thumb:original'])
        self.assertEqual(job['accountId'], 'a')
        self.assertEqual(job.get('autoRetryCount', 0), 0)

    def test_done_job_is_not_reopened(self):
        store.patch_row('jobs', 'j', {'status': 'done'})
        with patch.object(self.service, '_log') as log:
            asyncio.run(self.service._run('j'))
        log.assert_not_called()

    def test_cancel_stops_download_retry(self):
        with patch.object(self.service, '_download_video_via_flow_menu_once', new_callable=AsyncMock,
                          side_effect=asyncio.CancelledError) as downloader:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.service._download_video_via_flow_menu(None, 0, Path(self.directory.name) / 'v.mp4', '720p', job_id='j'))
        self.assertEqual(downloader.await_count, 1)
