"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_opencode.py"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from api.deps import ReviewScriptIn, ReviewTranslationIn, Settings
from api.routes.review import ReviewIn
from pipeline.chat.providers import API_PROVIDER_IDS, ProviderError
from pipeline.chat.service import ChatService
from pipeline.core import app_config
from pipeline.mt.api import translate_segments
from pipeline.mt.review import _request_review


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(app_config, "_CONFIG_PATH", Path(directory.name) / "app_config.json"))
        self.enterContext(patch.dict(os.environ, {
            **{meta["env"]: "" for meta in app_config.PROVIDERS.values()},
            "OPENCODE_API_KEY": "test-opencode-key",
        }))
        self.service = ChatService(store=object())

    def mock_http(self, handler):
        client = httpx.Client
        self.enterContext(patch("httpx.Client", side_effect=lambda **kwargs: client(
            transport=httpx.MockTransport(handler), **kwargs,
        )))

    def test_configuration_persists_keys_and_matches_api_schemas(self):
        self.assertEqual(API_PROVIDER_IDS[API_PROVIDER_IDS.index("openrouter") + 1], "opencode")
        self.assertEqual(app_config.provider_credentials("opencode")["model"], "big-pickle")
        app_config.save_app_config({"cloud": {"opencode": {
            "keys": ["saved-first", "saved-second"], "model": "glm-5.3-flash-free",
        }}})
        app_config.save_app_config({"cloud": {"opencode": {"keys": ["__keep:1__", "__keep:0__"]}}})
        self.assertEqual(app_config.provider_api_keys("opencode"), ["saved-second", "saved-first"])
        config = app_config.public_app_config()["cloud"]["opencode"]
        self.assertTrue(config["apiKeySet"])
        self.assertEqual(config["keyCount"], 2)
        self.assertEqual(config["baseUrl"], "https://opencode.ai/zen/v1")
        self.assertEqual(Settings(translationReviewTranslator="opencode").translationReviewTranslator, "opencode")
        self.assertEqual(ReviewTranslationIn(translator="opencode").translator, "opencode")
        self.assertEqual(ReviewScriptIn(translator="opencode", cues=[{
            "id": "cue-1", "source": "Hello", "start": 0, "end": 1,
        }]).translator, "opencode")
        self.assertEqual(ReviewIn(source="test.mp4", reviewProvider="opencode").reviewProvider, "opencode")
        app_config.save_app_config({"cloud": {"opencode": {"keys": []}}})
        with self.assertRaisesRegex(RuntimeError, "API key OpenCode"):
            app_config.provider_credentials("opencode")

    def test_catalogue_filters_free_and_incompatible_models(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://opencode.ai/zen/v1/models")
            self.assertEqual(request.headers["authorization"], "Bearer test-opencode-key")
            return httpx.Response(200, json={"data": [{"id": model} for model in (
                "big-pickle", "glm-5.3-flash-free", "deepseek-v4.1-flash-free",
                "minimax-m3", "jev-1.13-free", "claude-opus-5", "gpt-6-sol",
                "gemini-3-flash", "qwen3.8-flash", "qwen3.8-max", "test",
            )]})

        self.mock_http(handler)
        free = self.service.provider_models("opencode")
        self.assertEqual([row["id"] for row in free], ["big-pickle", "glm-5.3-flash-free", "deepseek-v4.1-flash-free"])
        self.assertTrue(all(row["free"] and row["available"] for row in free))
        all_models = self.service.provider_models("opencode", include_paid=True)
        self.assertEqual([row["id"] for row in all_models], [*[row["id"] for row in free], "minimax-m3", "qwen3.8-max"])
        self.assertFalse(all_models[-1]["free"])
        self.assertEqual(self.service.resolve_provider("opencode", "big-pickle")[:2], ("opencode", "big-pickle"))
        with self.assertRaisesRegex(ProviderError, "No verified free model"):
            self.service.resolve_provider("opencode", "minimax-m3")

    def test_chat_stream_uses_configured_endpoint_key_and_model(self):
        provider = self.service._api_provider("opencode")
        response = httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n')
        with patch("httpx.stream", return_value=MagicMock(__enter__=lambda _: response)) as stream:
            chunks = list(provider.stream("big-pickle", [{"role": "user", "content": "Hi"}], threading.Event()))
        self.assertEqual(chunks, ["Hello"])
        self.assertEqual(stream.call_args.args, ("POST", "https://opencode.ai/zen/v1/chat/completions"))
        self.assertEqual(stream.call_args.kwargs["json"]["model"], "big-pickle")
        self.assertEqual(stream.call_args.kwargs["headers"]["Authorization"], "Bearer test-opencode-key")

    def test_translation_and_ai_review_use_opencode_without_provider_fallback(self):
        requests = []

        def handler(request):
            self.assertEqual(str(request.url), "https://opencode.ai/zen/v1/chat/completions")
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(payload["model"], "big-pickle")
            content = '1. Xin chào.' if len(requests) == 1 else '{"translation":"Xin chào."}'
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        self.mock_http(handler)
        self.enterContext(patch("pipeline.mt.cloud.wait_for_network", return_value=True))
        result = translate_segments(["Hello."], "vi", translator="opencode", source_lang="en", workers=1)
        self.assertEqual(result, ["Xin chào"])
        self.assertEqual(_request_review("test", provider="opencode", ollama_mode="local",
                                         ollama_model="", ollama_local_tier="balanced"), '{"translation":"Xin chào."}')
        self.assertEqual(len(requests), 2)

    def test_provider_errors_remain_visible(self):
        self.mock_http(lambda _: httpx.Response(403, json={"error": {"message": "Account access required"}}))
        with self.assertRaises(ProviderError) as error:
            self.service.provider_models("opencode")
        self.assertEqual(error.exception.code, "CHAT_PROVIDER_HTTP_403")
        app_config.save_app_config({"cloud": {"opencode": {"keys": []}}})
        with self.assertRaisesRegex(ProviderError, "API key OpenCode"):
            self.service.provider_models("opencode", refresh=True)


if __name__ == "__main__":
    unittest.main()
