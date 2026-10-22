"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_render_access.py"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app


class RenderAccessTest(unittest.TestCase):
    def test_existing_results_are_readable_but_writes_still_require_activation(self):
        app = create_app()
        # Exercise the real middleware without reading or changing user exports.
        app.router.routes.clear()

        async def existing_result():
            return {"ok": True}

        readable = ["/api/drawing/hands", "/api/drawing/hands/default",
                    "/api/drawing/hands/custom:sample.png",
                    "/api/voices", "/api/tts/status", "/api/tts/voices/zmt:sample/preview",
                    "/api/renders", "/api/renders/result/video",
                    "/api/renders/result/text", "/api/renders/result/thumbnail"]
        blocked = [("POST", "/api/tts/studio/synthesize"),
                   ("POST", "/api/drawing/jobs"),
                   ("POST", "/api/drawing/hands"),
                   ("POST", "/api/projects/example/run"),
                   ("PUT", "/api/renders/result/text"),
                   ("DELETE", "/api/renders/result"),
                   ("POST", "/api/tts/voices/sample/preview"),
                   ("GET", "/api/renders/result/unknown")]
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
        for method, path in chat:
            app.add_api_route(path, existing_result, methods=[method])
        client = TestClient(app)
        with patch("pipeline.core.license.license_cached_valid", return_value=False):
            for method, path in chat:
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
