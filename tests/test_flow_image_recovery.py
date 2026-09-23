import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.service import FlowService, _captured_image_items, _captured_video_ids


class ImageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_done_requires_non_empty_files(self):
        with self.subTest("missing"):
            self.assertFalse(FlowService._outputs_exist(['/tmp/flow-missing-output.png']))
        with self.subTest("empty"):
            path = Path(__file__).with_name('.flow-empty-output.test')
            try:
                path.write_bytes(b'')
                self.assertEqual(FlowService._output_validation_error([path]), f'FLOW_OUTPUT_EMPTY: {path}')
            finally:
                path.unlink(missing_ok=True)

    def test_done_accepts_downloaded_non_empty_file(self):
        path = Path(__file__).with_name('.flow-output.test')
        try:
            path.write_bytes(b'valid-media')
            self.assertTrue(FlowService._outputs_exist([path]))
            self.assertIsNone(FlowService._output_validation_error([path]))
        finally:
            path.unlink(missing_ok=True)

    def test_captured_image_response_supports_current_and_legacy_shapes(self):
        current = {'media': [{'name': 'image-1', 'image': {'generatedImage': {'fifeUrl': 'https://example.invalid/1'}}}]}
        legacy = {'generatedImages': [{'mediaName': 'image-2', 'fifeUrl': 'https://example.invalid/2'}]}
        self.assertEqual(_captured_image_items(current), [{'id': 'image-1', 'src': 'https://example.invalid/1'}])
        self.assertEqual(_captured_image_items(legacy), [{'id': 'image-2', 'src': 'https://example.invalid/2'}])

    def test_captured_video_response_supports_jobs_and_media_shapes(self):
        response = {
            'jobs': [{'mediaId': {'mediaName': 'video-1'}}],
            'media': [{'name': 'video-2'}],
        }
        self.assertEqual(_captured_video_ids(response), ['video-1', 'video-2'])

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

    def test_prompt_match_accepts_flow_normalized_text(self):
        from pipeline.flow.service import _media_prompt_matches
        self.assertTrue(_media_prompt_matches(
            {'mediaMetadata': {'requestData': {'promptInputs': [{'textInput': 'A cat,  in sunlight'}]}}},
            'a cat, in sunlight',
        ))
