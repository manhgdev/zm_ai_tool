import unittest
from unittest.mock import AsyncMock, patch

from pipeline.flow.service import FlowService


class SubmitCompletedImagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_submit_probe_only_accepts_matching_unclaimed_new_images(self):
        service = FlowService()
        api = AsyncMock()
        api.get_project_data.return_value = {"projectContents": {"media": [
            {"name": name, "prompt": prompt,
             "generatedImage": {"fifeUrl": "https://example.com/" + name}}
            for name, prompt in [("old", "my prompt"), ("claimed", "my prompt"),
                                 ("sibling", "other prompt"), ("unknown", ""),
                                 ("mine", "my prompt")]
        ]}}
        job = {"id": "local", "prompt": "my prompt", "submissionStartedAt": 100,
               "baselineMediaIds": ["old"], "parallelSession": True,
               "recoveryOnly": False}
        with patch.object(service, "jobs", return_value=[{"id": "other", "mediaIds": ["claimed"]}]), patch.object(service, "_project_media_elements", AsyncMock(return_value=[])):
            found = await service._find_existing_project_media(api, None, job, "image", 1)
            self.assertEqual([item["id"] for item in found], ["mine"])
            api.get_project_data.return_value["projectContents"]["media"].pop()
            self.assertEqual(await service._find_existing_project_media(api, None, job, "image", 1), [])
