from unittest.mock import MagicMock, patch

from pipeline.mt.cloud import _chat_response_text, _http_error, _openai_compatible_chat


def test_chat_response_text_accepts_openrouter_content_parts():
    response = {
        "choices": [{
            "message": {
                "content": [
                    {"type": "text", "text": "1. Xin chào\n"},
                    {"type": "text", "text": "2. Tạm biệt"},
                ]
            }
        }]
    }

    assert _chat_response_text(response) == "1. Xin chào\n2. Tạm biệt"


def test_chat_response_text_keeps_string_response_compatible():
    response = {"choices": [{"message": {"content": "1. Xin chào"}}]}

    assert _chat_response_text(response) == "1. Xin chào"


def test_openrouter_translation_disables_reasoning_tokens():
    response = MagicMock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": "1. Xin chào"}}]}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response

    with patch("pipeline.mt.cloud.httpx.Client", return_value=client):
        result = _openai_compatible_chat(
            base_url="https://openrouter.ai/api/v1",
            api_key="test-key",
            model="openrouter/free",
            prompt="Translate hello",
            provider="openrouter",
        )

    assert result == "1. Xin chào"
    assert client.post.call_args.kwargs["json"]["reasoning_effort"] == "none"


def test_openrouter_stale_free_slug_retries_current_free_router():
    unavailable = MagicMock(status_code=404)
    unavailable.json.return_value = {
        "error": {"message": "This model is unavailable for free. Use the paid slug instead."}
    }
    recovered = MagicMock(status_code=200)
    recovered.json.return_value = {"choices": [{"message": {"content": "Xin chào"}}]}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = [unavailable, recovered]

    with patch("pipeline.mt.cloud.httpx.Client", return_value=client):
        result = _openai_compatible_chat(
            base_url="https://openrouter.ai/api/v1",
            api_key="test-key",
            model="inclusionai/ling-3.0-flash-sante:free",
            prompt="Translate hello",
            provider="openrouter",
        )

    assert result == "Xin chào"
    assert client.post.call_args_list[1].kwargs["json"]["model"] == "openrouter/free"


def test_openrouter_credits_are_classified_as_quota():
    assert "RATE_LIMITED_OR_QUOTA" in str(_http_error("openrouter", 402))
