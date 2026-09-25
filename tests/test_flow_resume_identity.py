import sys
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.service import FlowService


class ResumeIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_result_is_polled_until_available(self):
        service = FlowService()
        job = {'id': 'local', 'kind': 'image', 'resumeOnly': True}
        finder = AsyncMock(side_effect=[[], [{'id': 'original'}]])
        with patch.object(service, '_find_existing_project_media', finder), patch.object(service, 'jobs', return_value=[]), patch('pipeline.flow.service.store.patch_row') as save, patch('pipeline.flow.service.asyncio.sleep', AsyncMock()) as sleep:
            self.assertEqual(await service._recover_submitted_media(None, None, job), ['original'])
        self.assertEqual(finder.await_count, 2)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(save.call_args.args[2]['stage'], 'downloading')

    async def test_cancellation_stops_recovery_before_remote_lookup(self):
        service = FlowService()
        service._cancelled.add('local')
        with patch.object(service, '_find_existing_project_media', AsyncMock()) as finder:
            with self.assertRaises(asyncio.CancelledError):
                await service._recover_submitted_media(None, None, {'id': 'local', 'kind': 'video'})
        finder.assert_not_awaited()

    async def test_recovery_excludes_baseline_and_other_jobs_media(self):
        service = FlowService()
        api = AsyncMock()
        api.get_project_data.return_value = {'projectContents': {'media': [
            {'name': name, 'prompt': 'same prompt', 'video': {'fifeUrl': 'https://example.com/' + name}}
            for name in ['baseline', 'claimed', 'original']
        ]}}
        job = {'id': 'local', 'kind': 'video', 'prompt': 'same prompt', 'resumeOnly': True, 'baselineMediaIds': ['baseline']}
        with patch.object(service, 'jobs', return_value=[{'id': 'other', 'mediaIds': ['claimed']}]), patch.object(service, '_project_media_elements', AsyncMock(return_value=[])), patch('pipeline.flow.service.store.patch_row') as save:
            self.assertEqual(await service._recover_submitted_media(api, None, job), ['original'])
        self.assertEqual(save.call_args.args[2]['mediaIds'], ['original'])

    async def test_missing_id_recovers_and_persists_original_result(self):
        service = FlowService()
        job = {'id': 'local', 'kind': 'video', 'resumeOnly': True, 'settings': {'count': 1}}
        with patch.object(service, '_find_existing_project_media', AsyncMock(return_value=[{'id': 'remote', 'src': 'https://example.com/video'}])), patch.object(service, 'jobs', return_value=[]), patch('pipeline.flow.service.store.patch_row') as save:
            ids = await service._recover_submitted_media(None, None, job)
        self.assertEqual(ids, ['remote'])
        self.assertEqual(save.call_args.args[2]['mediaIds'], ['remote'])
        self.assertIn('remote', service._claimed_media_ids)

    async def test_saved_id_wins_over_prompt_and_gallery_order(self):
        api = AsyncMock()
        api.get_project_data.return_value = {'projectContents': {'media': [
            {'name': 'other', 'image': {'fifeUrl': 'https://example.com/other'}},
            {'name': 'original', 'image': {'fifeUrl': 'https://example.com/original'}},
        ]}}
        service = FlowService()
        with patch.object(service, '_project_media_elements', AsyncMock(return_value=[])):
            found = await service._find_existing_project_media(
                api, None, {'mediaIds': ['original'], 'prompt': 'same prompt'}, 'image', 1,
            )
        self.assertEqual([item['id'] for item in found], ['original'])

    async def test_recovery_uses_completed_dom_media_when_project_api_is_empty(self):
        service = FlowService()
        api = AsyncMock()
        api.get_project_data.return_value = {'projectContents': {'media': []}}
        job = {
            'id': 'local', 'kind': 'image', 'resumeOnly': True,
            'baselineMediaIds': ['old'], 'mediaIds': [],
        }
        elements = [
            {'id': 'old', 'tag': 'img', 'src': 'https://example.com/old', 'width': 100},
            {'id': 'fresh', 'tag': 'img', 'src': 'https://example.com/fresh', 'width': 100},
        ]
        with patch.object(service, '_project_media_elements', AsyncMock(return_value=elements)), \
             patch.object(service, 'jobs', return_value=[]):
            found = await service._find_existing_project_media(api, None, job, 'image', 1)
        self.assertEqual([item['id'] for item in found], ['fresh'])

    async def test_project_replacement_cohort_can_claim_unowned_baseline_media(self):
        service = FlowService()
        api = AsyncMock()
        api.get_project_data.return_value = {'projectContents': {'media': []}}
        job = {
            'id': 'local', 'kind': 'image', 'accountId': 'account',
            'createdAt': 105, 'resumeOnly': True,
            'baselineMediaIds': ['orphan'], 'mediaIds': [],
        }
        elements = [
            {'id': 'orphan', 'tag': 'img', 'src': 'https://example.com/orphan', 'width': 100},
        ]
        with patch.object(service, '_project_media_elements', AsyncMock(return_value=elements)), \
             patch.object(service, 'jobs', return_value=[]), \
             patch('pipeline.flow.service.store.get_row', return_value={'projectChangedAt': 100}):
            found = await service._find_existing_project_media(api, None, job, 'image', 1)
        self.assertEqual([item['id'] for item in found], ['orphan'])


class PlanSyncTests(unittest.TestCase):
    def test_worker_restores_session_before_requiring_manual_sync(self):
        service = FlowService()
        stale = {'id': 'account', 'status': 'reconnect', 'projectId': 'old-project'}
        restored = {'id': 'account', 'status': 'online', 'projectId': 'new-project'}
        verified = {**restored, 'planStatus': 'verified', 'plan': 'Pro'}
        results = iter([True, verified])

        def run(coro):
            coro.close()
            return next(results)

        with patch('pipeline.flow.service.store.get_row', side_effect=[stale, restored]), \
             patch('pipeline.flow.service.asyncio.run', side_effect=run):
            self.assertEqual(service._verify_account_plan_before_enqueue('account'), verified)


class FlowErrorTileTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_error_tile_is_claimed_by_only_one_concurrent_job(self):
        service = FlowService()
        page = AsyncMock()
        page.evaluate.return_value = [{
            'index': 0,
            'text': 'Không thành công Rất tiếc, không tạo được video này.',
        }]
        job = {'id': 'local', 'accountId': 'account'}
        with patch('pipeline.flow.service.store.get_row', return_value={'projectId': 'project'}):
            first = await service._claim_visible_flow_error(page, job, 1)
            second = await service._claim_visible_flow_error(page, job, 1)
        self.assertIn('Không thành công', first)
        self.assertEqual(second, '')

    async def test_same_error_tile_is_detected_again_on_the_single_newer_retry(self):
        service = FlowService()
        page = AsyncMock()
        text = 'Không thành công Rất tiếc, không tạo được video này.'
        page.evaluate.return_value = [{'index': 0, 'text': text}]
        signature = f'project:0:{text}'
        service._claimed_error_tiles.add(signature)
        job = {'id': 'local', 'accountId': 'account'}

        def row(name, _row_id):
            if name == 'accounts':
                return {'projectId': 'project'}
            return {
                **job,
                'submissionStartedAt': 20,
                'lastGenerationRejectedSubmissionAt': 10,
                'generationRejectRetryCount': 1,
            }

        with patch('pipeline.flow.service.store.get_row', side_effect=row), \
             patch('pipeline.flow.service.store.patch_row'):
            found = await service._claim_visible_flow_error(page, job, 1)
        self.assertEqual(found, text)


if __name__ == '__main__':
    unittest.main()
