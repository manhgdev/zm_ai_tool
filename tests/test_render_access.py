"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_render_access.py"""
import unittest
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app


class RenderAccessTest(unittest.TestCase):
    def test_all_flow_management_is_open_but_generation_and_retries_stay_gated(self):
        from api.routes.flow import router

        app = create_app()
        app.router.routes.clear()

        async def flow_action():
            return {"ok": True}

        blocked_routes = {
            ("POST", "/api/flow/jobs"), ("POST", "/api/flow/jobs/{job_id}/retry"),
            ("POST", "/api/flow/jobs/batch-retry"), ("POST", "/api/flow/series/draft"),
            ("POST", "/api/flow/series/{series_id}/draft-more"),
            ("POST", "/api/flow/series/{series_id}/run"),
            ("POST", "/api/flow/series/{series_id}/anchors/generate"),
            ("POST", "/api/flow/series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/generate"),
        }
        routes = {(method, route.path) for route in router.routes for method in route.methods}
        self.assertTrue(blocked_routes <= routes)
        # Every real Flow route is covered without executing destructive/provider operations.
        allowed = [(method, re.sub(r"\{[^}]+\}", "example", path)) for method, path in routes - blocked_routes]
        blocked = [(method, re.sub(r"\{[^}]+\}", "example", path)) for method, path in blocked_routes]
        allowed += [("GET", "/api/flow-other/jobs"), ("POST", "/api/flow-other/jobs")]
        for method, path in allowed + blocked:
            app.add_api_route(path, flow_action, methods=[method])
        client = TestClient(app)
        with patch("pipeline.core.license.license_cached_valid", return_value=False) as check:
            for method, path in allowed:
                with self.subTest(method=method, path=path):
                    self.assertEqual(client.request(method, path).status_code, 200)
            check.assert_not_called()
            for method, path in blocked:
                for suffix in ("", "/", "?test=1"):
                    with self.subTest(method=method, path=path, suffix=suffix):
                        response = client.request(method, path + suffix, follow_redirects=False)
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(response.json(), {"detail": "ACCESS_REQUIRED"})
        with patch("pipeline.core.license.license_cached_valid", return_value=True):
            for method, path in blocked:
                self.assertEqual(client.request(method, path).status_code, 200)

    def test_unactivated_flow_open_folder_uses_existing_service(self):
        with TemporaryDirectory() as directory, \
             patch("api.routes.flow.service") as service, \
             patch("api.routes.flow.subprocess.Popen") as popen, \
             patch("pipeline.core.license.license_cached_valid", return_value=False):
            service._output_folder.return_value = Path(directory)
            response = TestClient(create_app()).post("/api/flow/open-folder?output_dir=test&kind=image")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True, "path": directory})
            service._output_folder.assert_called_once_with({"kind": "image", "settings": {"outputDir": "test"}}, create=True)
            popen.assert_called_once()

    def test_flow_logo_only_is_open_but_mixed_or_invalid_retries_stay_gated(self):
        from fastapi import Request

        app = create_app()
        app.router.routes.clear()

        async def action(request: Request):
            return await request.json()

        single = "/api/flow/jobs/example/retry"
        batch = "/api/flow/jobs/batch-retry"
        for path in [single, batch]:
            app.add_api_route(path, action, methods=["POST"])
        client = TestClient(app)
        with patch("pipeline.core.license.license_cached_valid", return_value=False) as check:
            for path, body in [(single, {"logoOnly": True}), (batch, {"items": [{"jobId": "a", "logoOnly": True}]})]:
                self.assertEqual(client.post(path, json=body).status_code, 200)
            check.assert_not_called()
            for path, body in [(single, {}), (single, {"logoOnly": "true"}), (single, {"logoOnly": False}),
                               (batch, {"items": []}), (batch, {"logoOnly": True}),
                               (batch, {"items": [{"logoOnly": True}, {"jobId": "b"}]}),
                               (batch, {"items": [None]}), (single, None)]:
                with self.subTest(path=path, body=body):
                    self.assertEqual(client.post(path, json=body).status_code, 403)
            self.assertEqual(client.post(single, content="bad-json").status_code, 403)

    def test_unactivated_flow_series_logs_and_existing_outputs_are_usable(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService

        with TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)), \
             patch("api.routes.flow.service", FlowService()), \
             patch("pipeline.flow.queue.threading.Thread") as thread, \
             patch("pipeline.core.license.license_cached_valid", return_value=False):
            store._CACHE.clear()
            self.addCleanup(store._CACHE.clear)
            client = TestClient(create_app())
            created = client.post("/api/flow/series", json={"title": "Test series"})
            self.assertEqual(created.status_code, 200)
            path = f'/api/flow/series/{created.json()["id"]}'
            self.assertEqual(client.get(path).status_code, 200)
            edited = client.put(path, json={"title": "Edited series"})
            self.assertEqual(edited.status_code, 200)
            self.assertEqual(edited.json()["title"], "Edited series")
            self.assertEqual(len(client.get("/api/flow/series").json()["items"]), 1)
            self.assertEqual(client.get("/api/flow/logs").status_code, 200)
            output = Path(directory) / "existing.txt"
            output.write_text("existing output", encoding="utf-8")
            job = {"id": "existing", "status": "done", "kind": "image", "outputs": [str(output)]}
            store.put_row("jobs", job)
            download = client.get("/api/flow/jobs/existing/outputs/0?download=true")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.text, "existing output")
            self.assertEqual(client.post("/api/flow/jobs/existing/retry").status_code, 403)
            self.assertEqual(store.get_row("jobs", "existing"), job)
            logo = client.post("/api/flow/jobs/existing/retry", json={"logoOnly": True})
            self.assertEqual(logo.status_code, 200)
            self.assertEqual(logo.json()["stage"], "removing_logo")
            thread.assert_called_once()
            self.assertEqual(thread.call_args.kwargs["name"], "flow-logo-existing")
            store.patch_row("jobs", "existing", {"outputs": []})
            missing = client.post("/api/flow/jobs/existing/retry", json={"logoOnly": True})
            self.assertEqual(missing.status_code, 400)
            self.assertIn("FLOW_LOGO_NO_OUTPUTS", missing.json()["detail"])
            self.assertEqual(thread.call_count, 1)

    def test_unactivated_flow_account_crud_preserves_validation(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService

        # Real routes/store, isolated from the user's accounts and Chrome profiles.
        with TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)), \
             patch("api.routes.flow.service", FlowService()), \
             patch("pipeline.core.license.license_cached_valid", return_value=False):
            store._CACHE.clear()
            self.addCleanup(store._CACHE.clear)
            client = TestClient(create_app())
            response = client.post("/api/flow/accounts", json={"email": "flow-test", "authMethod": "chrome"})
            self.assertEqual(response.status_code, 200)
            account = response.json()
            self.assertEqual(account["email"], "flow-test@gmail.com")
            path = f'/api/flow/accounts/{account["id"]}'
            self.assertEqual(len(client.get("/api/flow/accounts").json()["accounts"]), 1)
            self.assertEqual(client.get("/api/flow/jobs").status_code, 200)
            response = client.put(path, json={"email": "flow-edited", "isDefault": True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["email"], "flow-edited@gmail.com")
            self.assertTrue(response.json()["isDefault"])
            invalid = client.put(path, json={"email": "invalid", "cookie": "not-a-cookie"})
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.json(), {"detail": "FLOW_COOKIE_INVALID"})
            self.assertEqual(store.get_row("accounts", account["id"])["email"], "flow-edited@gmail.com")
            self.assertEqual(client.delete(path).status_code, 200)
            self.assertEqual(client.get("/api/flow/accounts").json(), {"accounts": []})

    def test_queue_existing_actions_are_open(self):
        from fastapi import Request

        app = create_app()
        app.router.routes.clear()

        async def action(request: Request):
            return await request.json()

        app.add_api_route('/api/queue/job/action', action, methods=['POST'])
        app.add_api_route('/api/queue/action', action, methods=['POST'])
        client = TestClient(app)
        with patch('pipeline.core.license.license_cached_valid', return_value=False):
            for op in ['remove', 'pause', 'cancel', 'pause_all', 'clear_completed', 'clear_logs']:
                response = client.post('/api/queue/job/action', json={'op': op})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'op': op})
            for op in ['resume', 'retry', 'resume_all', 'retry_failed']:
                self.assertEqual(client.post('/api/queue/action', json={'op': op}).status_code, 200)

    def test_results_management_is_open_but_generation_requires_activation(self):
        app = create_app()
        # Exercise the real middleware without reading or changing user exports.
        app.router.routes.clear()

        async def existing_result():
            return {"ok": True}

        readable = ["/api/resources", "/api/system/install/status", "/api/ui-preferences",
                    "/api/drawing/hands", "/api/drawing/hands/default",
                    "/api/drawing/hands/custom:sample.png",
                    "/api/voices", "/api/tts/status", "/api/tts/voices/zmt:sample/preview",
                    "/api/renders", "/api/renders/result/video",
                    "/api/renders/result/text", "/api/renders/result/thumbnail"]
        blocked = [("POST", "/api/tts/studio/synthesize"),
                   ("POST", "/api/drawing/jobs"),
                   ("POST", "/api/projects/example/run")]
        installs = [("POST", f"/api/resources/{resource}/install") for resource in
                    ["whisper", "ocr", "diarization", "demucs", "vieneu", "moss_tts_nano"]]
        installs += [("POST", "/api/system/install/ai_runtime")]
        installs += [("PUT", "/api/ui-preferences"), ("POST", "/api/drawing/hands"),
                     ("POST", "/api/tts/voices/sample/preview"), ("GET", "/api/renders-other")]
        render_writes = [("PUT", "/api/renders/result"),
                         ("PUT", "/api/renders/result/text"),
                         ("DELETE", "/api/renders/result"),
                         ("POST", "/api/renders/delete-batch"),
                         ("POST", "/api/renders/result/reveal")]
        chat = [("GET", "/api/chat/providers"), ("GET", "/api/chat/conversations"),
                ("POST", "/api/chat/accounts"), ("POST", "/api/chat/conversations"),
                ("POST", "/api/chat/conversations/c/attachments"),
                ("POST", "/api/chat/conversations/c/messages"),
                ("POST", "/api/chat/conversations/c/messages/m/retry"),
                ("PATCH", "/api/chat/conversations/c"),
                ("DELETE", "/api/chat/conversations/c")]
        for path in readable:
            app.add_api_route(path, existing_result, methods=["GET"])
        for method, path in blocked:
            app.add_api_route(path, existing_result, methods=[method])
        for method, path in chat + render_writes + installs:
            app.add_api_route(path, existing_result, methods=[method])
        client = TestClient(app)
        with patch("pipeline.core.license.license_cached_valid", return_value=False):
            for method, path in chat + render_writes + installs:
                with self.subTest(method=method, path=path):
                    self.assertEqual(client.request(method, path).status_code, 200)
            for path in readable:
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.get("/api/renders/result/video?download=true").status_code, 200)
            for method, path in blocked:
                with self.subTest(method=method, path=path):
                    response = client.request(method, path)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json(), {"detail": "ACCESS_REQUIRED"})


if __name__ == "__main__":
    unittest.main()
