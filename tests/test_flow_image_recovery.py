import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.service import FlowService


class ImageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_api_image_does_not_require_dom_thumbnail(self):
        service = FlowService()
        media = {'name': 'image-1', 'image': {'fifeUrl': 'https://example.invalid/image'}}
        api = SimpleNamespace(get_project_data=AsyncMock(return_value={'projectContents': {'media': [media]}}))
        with patch.object(service, '_project_media_elements', AsyncMock(return_value=[])), patch('pipeline.flow.service._media_prompt_matches', return_value=True), patch('pipeline.flow.service._media_created_timestamp', return_value=100), patch('pipeline.flow.service.store.patch_row'):
            result = await service._wait_for_project_media(None, set(), 'image', 1, 'job', api=api, job={'prompt': 'cat', 'createdAt': 100})
        self.assertEqual(result[0]['id'], 'image-1')
        self.assertEqual(result[0]['src'], 'https://example.invalid/image')

    async def test_unrelated_prompt_is_not_recovered(self):
        service = FlowService()
        api = SimpleNamespace(get_project_data=AsyncMock(return_value={'projectContents': {'media': [{'name': 'wrong', 'image': {'fifeUrl': 'https://example.invalid/wrong'}}]}}))
        with patch('pipeline.flow.service._media_prompt_matches', return_value=False):
            self.assertEqual(await service._find_existing_project_media(api, None, {'prompt': 'cat'}, 'image', 1), [])
