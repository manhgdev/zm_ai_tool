"""PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_action_access.py"""
import threading
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient

from api.app import create_app


class ActionAccessTest(unittest.TestCase):
    def test_twelve_page_action_policy_and_request_bodies(self):
        app = create_app()
        app.router.routes.clear()

        async def action(request: Request):
            return {"ok": True, "body": await request.json() if await request.body() else None}

        blocked = [
            ("/api/tts/studio/synthesize", {"text": "Voice", "publishOutput": True}),
            ("/api/media-compose/jobs", {}),
            ("/api/cleaner/jobs", {}),
            ("/api/drawing/jobs", {}),
            ("/api/drawing/jobs/start", {"ids": ["drawing"]}),
            ("/api/queue/start", {"ids": ["batch"]}),
            ("/api/queue", {"start_now": True}),
            ("/api/projects/p/run", {"previewSec": 20, "runPreviewSec": 0}),
            ("/api/review/generate", {}),
            ("/api/projects/p/export", {}),
        ]
        open_posts = [
            ("/api/download/jobs", {}), ("/api/srt-export/jobs", {}),
            ("/api/drawing/timeline-render", {}),
            ("/api/tts/studio/transcribe", {}), ("/api/tts/studio/clone", {}),
            ("/api/tts/studio/synthesize", {"text": "Preview", "publishOutput": False}),
            ("/api/queue", {"start_now": False}),
            ("/api/queue/j/action", {"op": "retry"}),
            ("/api/queue/j/action", {"op": "resume"}),
            ("/api/queue/action", {"op": "resume_all"}),
            ("/api/projects/p/run", {"previewSec": 20, "runPreviewSec": 5}),
            ("/api/projects/p/run", {"previewSec": 20}),
            ("/api/projects/p/editor-run", {"runPreviewSec": 0}),
            ("/api/projects/p/dub", {}), ("/api/projects/p/ocr-translate", {}),
            ("/api/projects/p/segments/s/preview-tts", {}),
            ("/api/projects/p/segments/retranscribe-range", {}),
            ("/api/projects/p/audio/no-vocals", {}), ("/api/projects/p/rebake-speed", {}),
            ("/api/projects/p/review-script", {}), ("/api/projects/p/settings", {}),
            ("/api/config", {}), ("/api/config/output-root", {}),
            ("/api/system/pick-folder", {}), ("/api/system/cleanup", {}),
            ("/api/media-compose/cache/clear", {}), ("/api/media-compose/jobs/j/pause", {}),
            ("/api/cleaner/jobs/j/retry", {}), ("/api/cleaner/jobs/j/reveal", {}),
            ("/api/automation/suggest-topics", {}), ("/api/automation/youtube/rewrite", {}),
            ("/api/automation/jobs/j/resume", {}), ("/api/automation/jobs/j/retry", {}),
            ("/api/automation/jobs/j/select-topic", {}),
        ]
        for path in {path for path, _ in blocked + open_posts}:
            app.add_api_route(path, action, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        client = TestClient(app)
        with patch("pipeline.core.license.license_cached_valid", return_value=False) as check:
            for path, body in open_posts:
                with self.subTest(open=path, body=body):
                    response = client.post(path, json=body)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["body"], body)
            check.assert_not_called()
            for path, body in blocked:
                for suffix in ("", "/", "?test=1"):
                    with self.subTest(blocked=path + suffix):
                        response = client.post(path + suffix, json=body, follow_redirects=False)
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(response.json(), {"detail": "ACCESS_REQUIRED"})
                for method in ["GET", "PUT", "PATCH", "DELETE"]:
                    self.assertEqual(client.request(method, path).status_code, 200)
            # Explicit previews cannot be used to publish SRT/full-length outputs.
            for body in [{}, None, {"text": "x" * 201, "publishOutput": False},
                         {"text": "x", "publishOutput": "false"},
                         {"text": "x", "srtText": "1", "publishOutput": False},
                         {"text": "x", "outputDir": "/tmp/output", "publishOutput": False}]:
                self.assertEqual(client.post("/api/tts/studio/synthesize", json=body).status_code, 403)
            for raw in [0, -1, 0.5, None, "bad", {}, []]:
                self.assertEqual(client.post("/api/projects/p/run", json={"runPreviewSec": raw, "previewSec": 0}).status_code, 403)
        with patch("pipeline.core.license.license_cached_valid", return_value=True):
            for path, body in blocked:
                self.assertEqual(client.post(path, json=body).status_code, 200)

    def test_real_multipart_staging_and_run_flags_before_mutation(self):
        with patch("pipeline.core.license.license_cached_valid", return_value=False), \
             patch("api.routes.drawing.create_job", return_value={"id": "drawing"}) as create, \
             patch("api.routes.drawing.start_batch") as start, \
             patch("api.routes.automation.service") as automation:
            client = TestClient(create_app())
            for value in [None, "true", "1"]:
                data = {} if value is None else {"start_now": value}
                response = client.post("/api/drawing/jobs/batch", data=data, files={"images": ("test.png", b"test", "image/png")})
                self.assertEqual(response.status_code, 403)
            create.assert_not_called()
            start.assert_not_called()
            staged = client.post("/api/drawing/jobs/batch", data={"start_now": "false"}, files={"images": ("test.png", b"test", "image/png")})
            self.assertEqual(staged.status_code, 200)
            self.assertFalse(staged.json()["started"])
            create.assert_called_once()
            start.assert_not_called()
            for value in [None, "true", "1"]:
                data = {"topic": "Test", **({} if value is None else {"startNow": value})}
                self.assertEqual(client.post("/api/automation/jobs", data=data).status_code, 403)
            automation.create_job.assert_not_called()
            automation.start_job.assert_not_called()
            automation.create_job.return_value = {"id": "automation"}
            response = client.post("/api/automation/jobs", data={"topic": "Test", "startNow": "false"})
            self.assertEqual(response.status_code, 200)
            automation.create_job.assert_called_once()
            automation.start_job.assert_not_called()

    def test_real_batch_start_is_atomic_at_access_boundary_and_resume_remains_open(self):
        client = TestClient(create_app())
        with patch("api.routes.queue.get_job", return_value={"status": "paused"}), \
             patch("api.routes.queue.job_action", return_value={"ok": True}) as action:
            with patch("pipeline.core.license.license_cached_valid", return_value=False):
                self.assertEqual(client.post("/api/queue/start", json={"ids": ["a", "b"]}).status_code, 403)
                action.assert_not_called()
                self.assertEqual(client.post("/api/queue/a/action", json={"op": "resume"}).status_code, 200)
            action.reset_mock()
            with patch("pipeline.core.license.license_cached_valid", return_value=True):
                response = client.post("/api/queue/start", json={"ids": ["a", "b", "a"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["ids"], ["a", "b"])
                self.assertEqual(action.call_count, 2)

    def test_desktop_workers_allow_preview_and_existing_jobs_but_web_keeps_auth_checks(self):
        from pipeline.core import license as licensing
        from api.job_spawn import _run_in_thread
        from pipeline.queue.engine import QueueEngine
        from pipeline.automation.service import AutomationService
        from pipeline.automation.store import AutomationStore
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with patch.dict("os.environ", {"ZM_AI_TOOL_DESKTOP": "1", "ZM_AI_TOOL_WEB_WORKER": "0"}), \
             patch.object(licensing, "license_cached_valid", return_value=False):
            self.assertFalse(licensing.can_start_job())  # Flow generation still checks access.
            self.assertTrue(licensing.can_start_job(activation_required=False))
            calls = []
            _run_in_thread(lambda: calls.append(licensing.is_admitted()), ())
            self.assertEqual(calls, [True])
            queue = QueueEngine.__new__(QueueEngine)
            with patch.object(queue, "_run_admitted_job", side_effect=lambda _: calls.append(licensing.is_admitted())):
                queue._run_job("preview")
            self.assertEqual(calls, [True, True])
            with TemporaryDirectory() as directory:
                store = AutomationStore(Path(directory) / "automation.sqlite3", Path(directory) / "jobs")
                started, release = threading.Event(), threading.Event()
                def runner(_):
                    calls.append(licensing.is_admitted())
                    started.set()
                    self.assertTrue(release.wait(3))
                service = AutomationService(store=store, runner=runner, max_workers=1)
                try:
                    job = store.create_job("topic", "Existing", {})
                    service.resume_job(job["id"])
                    self.assertTrue(started.wait(3))
                    pending = store.create_job("topic", "Pending", {})
                    service.resume_job(pending["id"])
                    licensing.pause_pending_jobs()
                    self.assertFalse(service._futures[pending["id"]].cancelled())
                    release.set()
                    service._futures[job["id"]].result(timeout=3)
                    service._futures[pending["id"]].result(timeout=3)
                    self.assertEqual(store.get_job(job["id"])["status"], "completed")
                    self.assertEqual(store.get_job(pending["id"])["status"], "completed")
                    self.assertEqual(calls, [True, True, True, True])
                    with patch("pipeline.flow.service") as flow:
                        flow.jobs.return_value = [{"id": "child", "status": "action_required"}]
                        flow.retry.side_effect = lambda _: calls.append(licensing.is_admitted())
                        store.update_job(job["id"], status="paused", stage="compose", child_job_ids=["child"])
                        service.retry_job(job["id"], from_stage="compose")
                        service._futures[job["id"]].result(timeout=3)
                        flow.retry.assert_called_once_with("child")
                        self.assertEqual(calls[-2:], [True, True])
                finally:
                    release.set()
                    service.close()
            with patch.dict("os.environ", {"ZM_AI_TOOL_WEB_WORKER": "1"}):
                self.assertFalse(licensing.can_start_job(activation_required=False))

    def test_tts_preview_runs_without_publishing_or_async_access_failure(self):
        from api.routes.tts_studio import api_tts_studio_synth
        from api.deps import StudioSynthIn

        with patch.dict("os.environ", {"ZM_AI_TOOL_DESKTOP": "1", "ZM_AI_TOOL_WEB_WORKER": "0"}), \
             patch("pipeline.core.license.license_cached_valid", return_value=False), \
             patch("threading.Thread") as thread, \
             patch("pipeline.tts.studio.synth_text_job", return_value={"id": "preview-job"}) as synth, \
             patch("pipeline.tts.studio.set_job_progress"), \
             patch("pipeline.tts.studio.set_job_complete") as complete, \
             patch("pipeline.tts.studio.set_job_error") as error, \
             patch("pipeline.tts.studio.publish_job_outputs") as publish:
            api_tts_studio_synth(StudioSynthIn(text="Preview", publishOutput=False))
            thread.call_args.kwargs["target"]()
            synth.assert_called_once()
            complete.assert_called_once()
            error.assert_not_called()
            publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
