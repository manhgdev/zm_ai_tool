import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.service import FlowService, _flow_submit_button_score


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

    async def test_recovery_only_claims_no_prompt_media_after_submit(self):
        service = FlowService()
        api = AsyncMock()
        # Late Flow tiles often omit prompt metadata; recovery must still claim.
        api.get_project_data.return_value = {"projectContents": {"media": [
            {"name": "old", "prompt": "my prompt",
             "generatedImage": {"fifeUrl": "https://example.com/old"},
             "mediaMetadata": {"createTime": "2020-01-01T00:00:00Z"}},
            {"name": "late", "generatedImage": {"fifeUrl": "https://example.com/late"},
             "mediaMetadata": {"createTime": "2024-06-01T12:00:30Z"}},
        ]}}
        job = {
            "id": "local", "prompt": "my prompt",
            "submissionStartedAt": 1_717_243_200.0,  # 2024-06-01T12:00:00Z
            "baselineMediaIds": ["old"], "recoveryOnly": True, "resumeOnly": True,
        }
        with patch.object(service, "jobs", return_value=[]), \
             patch.object(service, "_project_media_elements", AsyncMock(return_value=[])):
            found = await service._find_existing_project_media(api, None, job, "image", 1)
        self.assertEqual([item["id"] for item in found], ["late"])

    def test_submit_button_score_prefers_localized_aria(self):
        self.assertEqual(_flow_submit_button_score("", "Bắt đầu tạo", ""), 100)
        self.assertEqual(_flow_submit_button_score("arrow_forward", "", ""), 90)
        self.assertEqual(_flow_submit_button_score("Cancel", "close", ""), 0)

    async def test_submit_click_retries_dom_resolve_then_fails(self):
        service = FlowService()
        page = MagicMock()
        page.locator.return_value.inner_text = AsyncMock(return_value="ok")
        service._flow_submit_dom = AsyncMock(return_value={"clicked": False, "score": 0, "disabled": False})
        with self.assertRaisesRegex(RuntimeError, "FLOW_SUBMIT_CLICK_FAILED"):
            await service._click_flow_submit_and_confirm(page, 0, "", set(), [], [])
        self.assertEqual(service._flow_submit_dom.call_count, 3)

    async def test_submit_no_rpc_force_clicks_once_then_not_confirmed(self):
        """DOM click without a generate request: one Playwright retry, then fail."""
        service = FlowService()
        page = MagicMock()
        loc = MagicMock()
        loc.inner_text = AsyncMock(return_value="ok")
        loc.count = AsyncMock(return_value=0)
        page.locator.return_value = loc
        page.evaluate = AsyncMock(return_value="prompt still here")
        service._flow_submit_dom = AsyncMock(return_value={
            "clicked": True, "score": 90, "disabled": False, "marked": True, "labels": ["go"],
        })
        service._force_click_marked_flow_submit = AsyncMock(return_value=True)
        service._raise_if_new_flow_error = AsyncMock()
        service._submit_failure_diagnostics = AsyncMock(return_value={"settingsOpen": True})
        service._log = MagicMock()
        clock = {"t": 0.0}

        def mono() -> float:
            cur = clock["t"]
            clock["t"] += 0.5
            return cur

        with patch("pipeline.flow.service.time.monotonic", side_effect=mono), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "FLOW_SUBMIT_NOT_CONFIRMED"):
                await service._click_flow_submit_and_confirm(
                    page, 0, "prompt still here", set(), [], [],
                )
        self.assertEqual(service._force_click_marked_flow_submit.call_count, 1)
        service._log.assert_any_call(
            "warning", "submit_not_confirmed",
            job_id="", account_id="",
            details={"settingsOpen": True},
        )

    async def test_submit_rpc_sent_slow_response_confirms_without_force_click(self):
        """Generate request left the browser: wait past 8s, never re-click."""
        service = FlowService()
        page = MagicMock()
        page.locator.return_value.inner_text = AsyncMock(return_value="ok")
        service._flow_submit_dom = AsyncMock(return_value={
            "clicked": True, "score": 90, "disabled": False, "marked": True,
        })
        service._force_click_marked_flow_submit = AsyncMock(return_value=True)
        service._raise_if_new_flow_error = AsyncMock()
        rpc_requests = [object()]
        clock = {"t": 0.0}

        def mono() -> float:
            cur = clock["t"]
            clock["t"] += 2.0
            return cur

        with patch("pipeline.flow.service.time.monotonic", side_effect=mono), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            await service._click_flow_submit_and_confirm(
                page, 0, "prompt", set(), rpc_requests, [],
            )
        service._force_click_marked_flow_submit.assert_not_called()

    def test_clear_unsent_submission_drops_markers(self):
        service = FlowService()
        with patch("pipeline.flow.service.store.patch_row") as patch_row:
            service._clear_unsent_submission("job-1")
        patch_row.assert_called_once()
        self.assertEqual(patch_row.call_args.args[0], "jobs")
        self.assertEqual(patch_row.call_args.args[1], "job-1")
        payload = patch_row.call_args.args[2]
        self.assertIsNone(payload["submissionStartedAt"])
        self.assertIsNone(payload["submissionProjectId"])
        self.assertEqual(payload["baselineMediaIds"], [])
        self.assertFalse(payload["resumeOnly"])
