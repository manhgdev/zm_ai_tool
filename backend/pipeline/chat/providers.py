from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from typing import Any


class ProviderError(RuntimeError):
    """Safe, machine-readable provider failure."""

    def __init__(self, code: str, message: str = ""):
        self.code = str(code or "CHAT_PROVIDER_ERROR")
        self.message = str(message or self.code)
        super().__init__(self.message)

    def safe_message(self, secret: str = "") -> str:
        return self.message.replace(secret, "[REDACTED]") if secret else self.message


API_PROVIDER_IDS = ("openai", "gemini", "deepseek", "openrouter", "grok", "groq", "nvidia")
LOCAL_PROVIDER_IDS = ("ollama",)
PROVIDER_LABELS = {
    "openai": "OpenAI API",
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
    "grok": "Grok (xAI)",
    "groq": "Groq",
    "nvidia": "NVIDIA NIM",
    "chatgpt_web": "ChatGPT Codex",
    "ollama": "Ollama (local)",
}
_STREAM_TIMEOUT_SECONDS = 45.0

# Groq exposes a Free Plan with rate limits rather than per-model pricing
# metadata. Keep this allowlist conservative so paid/unknown model ids never
# become selectable when Chat is operating in free-only mode.
_GROQ_FREE_MODELS = frozenset({
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-safeguard-20b",
    "qwen/qwen3-32b",
    "qwen/qwen3.6-27b",
    "qwen/qwen3.8-27b",
    "groq/compound",
    "groq/compound-mini",
})
_GROQ_FREE_MODELS_CASEFOLD = frozenset(item.casefold() for item in _GROQ_FREE_MODELS)

# Google does not include billing metadata in models.list. Keep this allowlist
# versioned in code; families that are explicitly free-tier (Flash/Flash-Lite
# and Gemma) are accepted while an unknown Pro/Image/TTS model is not.
_GEMINI_FREE_PATTERN = re.compile(
    r"^(?:gemini-(?:(?:2\.0|2\.5|3(?:\.[0-9]+)?)-(?:flash|flash-lite)|flash(?:-lite)?-latest)|gemma-[a-z0-9.-]+)$",
    re.IGNORECASE,
)
# Specialized Gemini families use a different API contract and should not be
# offered by this standard text-chat adapter.
_GEMINI_SPECIALIZED_PATTERN = re.compile(
    r"-(?:image|tts|native-audio|live|transcribe)(?:-|$)",
    re.IGNORECASE,
)

# xAI's public model catalogue does not expose billing metadata.  ``grok-3-
# mini`` is the developer/free model exposed by the current xAI API; keep the
# list explicit so a newly-added paid model is never silently advertised as
# free.  An explicit ``free`` flag from the API is still honoured below.
_GROK_FREE_MODELS = frozenset({
    "grok-3-mini",
    "grok-3-mini-fast",
})
_GROK_FREE_MODELS_CASEFOLD = frozenset(item.casefold() for item in _GROK_FREE_MODELS)


def _as_model_id(raw: Any) -> str:
    if isinstance(raw, str):
        return re.sub(r"^models/", "", raw.strip(), flags=re.IGNORECASE)
    if not isinstance(raw, dict):
        return ""
    value = raw.get("id") or raw.get("name") or raw.get("slug") or raw.get("model")
    return re.sub(r"^models/", "", str(value or "").strip(), flags=re.IGNORECASE)


def _zero_price(value: Any) -> bool:
    try:
        normalized = str(value or "0").strip().replace("$", "").replace(",", "")
        return float(normalized) == 0
    except (TypeError, ValueError):
        return False


def _is_free(provider: str, model_id: str, raw: dict[str, Any]) -> tuple[bool, str]:
    if provider == "gemini":
        if _GEMINI_SPECIALIZED_PATTERN.search(model_id):
            return False, "Gemini specialized model is not a standard text-chat model"
        methods = raw.get("supportedGenerationMethods")
        if isinstance(methods, list) and methods and not any(
            str(method).casefold() in {"generatecontent", "streamgeneratecontent"}
            for method in methods
        ):
            return False, "Gemini model does not support standard text generation"
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    # Some catalogues expose entitlement explicitly even when they omit
    # pricing.  This is the strongest signal and avoids hard-coding every
    # future free-tier model name.
    if raw.get("free") is True or raw.get("is_free") is True:
        return True, "Provider marked model as free"
    if provider == "openrouter":
        normalized_id = model_id.casefold()
        if normalized_id == "openrouter/free" or normalized_id.endswith(":free"):
            return True, "Free model variant"
        if _zero_price(pricing.get("prompt")) and _zero_price(pricing.get("completion")):
            return True, "Zero-priced model metadata"
        return False, "OpenRouter model is not marked free"
    if provider == "groq":
        # Groq's authenticated /v1/models catalogue is the entitlement
        # boundary; pricing metadata and model ids change independently of
        # this client. Every model returned by that catalogue is usable by the
        # configured Groq key.
        return True, "Groq model available for configured key"
    if provider == "nvidia":
        # NVIDIA NIM's authenticated catalogue is the entitlement boundary;
        # unlike OpenRouter it does not expose reliable per-model pricing.
        # Treat models returned by the user's NIM key as selectable here.
        return True, "NVIDIA NIM model available for configured key"
    if provider == "grok":
        if model_id.casefold() in _GROK_FREE_MODELS_CASEFOLD:
            return True, "Grok developer/free model"
        return False, "Grok model has no verified free entitlement"
    if pricing and _zero_price(pricing.get("prompt")) and _zero_price(pricing.get("completion")):
        return True, "Zero-priced model metadata"
    if provider == "gemini" and _GEMINI_FREE_PATTERN.match(model_id):
        return True, "Gemini free-tier model family"
    return False, "No verified free model entitlement"


def _capabilities(raw: dict[str, Any]) -> list[str]:
    values: list[str] = ["text"]
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    modalities = architecture.get("input_modalities") or raw.get("input_modalities") or []
    if isinstance(modalities, str):
        modalities = [modalities]
    for value in modalities if isinstance(modalities, list) else []:
        name = str(value).lower()
        if "image" in name and "vision" not in values:
            values.append("vision")
        if "audio" in name and "audio" not in values:
            values.append("audio")
        if "video" in name and "video" not in values:
            values.append("video")
    return values


def normalize_model(provider: str, raw: Any) -> dict[str, Any]:
    """Normalize an untrusted provider model record for the public API."""
    pid = str(provider or "").strip().lower()
    model_id = _as_model_id(raw)
    item = raw if isinstance(raw, dict) else {}
    free, reason = _is_free(pid, model_id, item)
    capabilities = _capabilities(item)
    # Gemini Flash families are multimodal even when models.list omits the
    # modality metadata; keep audio/image preflight useful for SRT workflows.
    if pid == "gemini" and "flash" in model_id.lower():
        capabilities = list(dict.fromkeys([*capabilities, "vision", "audio"]))
    label = str(item.get("label") or item.get("display_name") or item.get("name") or _as_model_id(item) or model_id)
    return {
        "id": model_id,
        "label": label.removeprefix("models/").strip() or model_id,
        "provider": pid,
        "free": free,
        "capabilities": capabilities,
        "available": bool(model_id),
        "reason": reason,
    }


def _model_records(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload if isinstance(payload, list) else []


class OpenAICompatibleProvider:
    """Streaming adapter for OpenAI-compatible text APIs."""

    def __init__(self, provider: str, api_key: str, base_url: str, api_keys: list[str] | None = None):
        self.provider = str(provider or "openai").lower()
        self.api_key = str(api_key or "")
        self.api_keys = [str(key).strip() for key in (api_keys or [self.api_key]) if str(key).strip()]
        self.base_url = str(base_url or "").rstrip("/")
        # Set for each request so the service can include provider-reported
        # usage in the terminal SSE event without exposing credentials.
        self.last_usage: dict[str, int] | None = None

    def _headers(self, key: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {key or self.api_key}", "Content-Type": "application/json"}

    def model_records(self, timeout: float = 20.0) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ProviderError("CHAT_PROVIDER_KEY_MISSING", f"{PROVIDER_LABELS.get(self.provider, self.provider)} API key is not configured")
        try:
            import httpx

            with httpx.Client(timeout=timeout, trust_env=False) as client:
                last_auth_error: ProviderError | None = None
                for key in self.api_keys:
                    response = client.get(f"{self.base_url}/models", headers=self._headers(key))
                    if response.status_code >= 400:
                        error = ProviderError(f"CHAT_PROVIDER_HTTP_{response.status_code}")
                        # A stale key must not hide a later valid key saved for
                        # the same provider. Other failures (quota, outage,
                        # malformed request) remain terminal and visible.
                        if response.status_code in {401, 403} and key != self.api_keys[-1]:
                            last_auth_error = error
                            continue
                        raise error
                    self.api_key = key
                    return [normalize_model(self.provider, item) for item in _model_records(response.json()) if _as_model_id(item)]
                if last_auth_error:
                    raise last_auth_error
                raise ProviderError("CHAT_PROVIDER_KEY_MISSING")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("CHAT_PROVIDER_MODELS_UNAVAILABLE", str(exc)) from None

    def models(self) -> list[str]:
        return [str(item["id"]) for item in self.model_records() if item.get("id")]

    @staticmethod
    def _message_content(value: Any) -> Any:
        if isinstance(value, (str, list, dict)):
            return value
        return str(value or "")

    def stream_events(self, model: str, messages: list[dict[str, Any]], cancel, attachments: list[dict[str, Any]] | None = None) -> Iterator[tuple[str, str]]:
        import httpx

        payload_messages = [
            {"role": str(message.get("role") or "user"), "content": self._message_content(message.get("content"))}
            for message in messages
            if str(message.get("status") or "completed") == "completed"
        ]
        if attachments and payload_messages:
            payload_messages[-1]["content"] = self._append_openai_attachments(payload_messages[-1]["content"], attachments)
        self.last_usage = None
        payload = {
            "model": model,
            "messages": payload_messages,
            "stream": True,
            # OpenAI-compatible providers that implement the standard option
            # return usage in the final streamed chunk. Providers that ignore
            # this field continue to stream normally.
            "stream_options": {"include_usage": True},
        }
        try:
            for index, key in enumerate(self.api_keys):
                try:
                    with httpx.stream(
                        "POST", f"{self.base_url}/chat/completions", headers=self._headers(key),
                        json=payload, timeout=httpx.Timeout(_STREAM_TIMEOUT_SECONDS, connect=10.0), trust_env=False,
                    ) as response:
                        if response.status_code >= 400:
                            error = ProviderError(f"CHAT_PROVIDER_HTTP_{response.status_code}")
                            if response.status_code in {401, 403} and index < len(self.api_keys) - 1:
                                continue
                            raise error
                        self.api_key = key
                        for line in response.iter_lines():
                            if cancel.is_set():
                                return
                            if not line or not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                return
                            try:
                                chunk = json.loads(raw)
                                usage = chunk.get("usage") if isinstance(chunk, dict) else None
                                if isinstance(usage, dict):
                                    self.last_usage = {
                                        str(key): int(value)
                                        for key, value in usage.items()
                                        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                        and isinstance(value, (int, float))
                                    } or self.last_usage
                                choices = chunk.get("choices") or []
                                delta = (choices[0].get("delta") or {}) if choices else {}
                                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                                if isinstance(reasoning, str) and reasoning:
                                    yield "reasoning.delta", reasoning
                                content = delta.get("content")
                                if isinstance(content, str) and content:
                                    yield "content.delta", content
                            except (ValueError, TypeError, KeyError, IndexError):
                                continue
                        return
                except ProviderError:
                    raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("CHAT_PROVIDER_STREAM_UNAVAILABLE", str(exc)) from None

    def stream(self, model: str, messages: list[dict[str, Any]], cancel, attachments: list[dict[str, Any]] | None = None) -> Iterator[str]:
        for kind, delta in self.stream_events(model, messages, cancel, attachments=attachments):
            if kind == "content.delta":
                yield delta

    @staticmethod
    def _append_openai_attachments(content: Any, attachments: list[dict[str, Any]]) -> Any:
        """Add supported multimodal parts without silently dropping files."""
        if not any(
            str(item.get("content_type") or "").startswith(("image/", "audio/"))
            for item in attachments
        ):
            return content
        parts: list[dict[str, Any]] = [{"type": "text", "text": str(content or "")}]
        for item in attachments:
            content_type = str(item.get("content_type") or "")
            if content_type.startswith("image/"):
                encoded = item.get("data")
                if encoded:
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{encoded}"}})
                continue
            if content_type.startswith("audio/"):
                encoded = item.get("data")
                if encoded:
                    subtype = content_type.split("/", 1)[1].lower()
                    audio_format = "wav" if subtype in {"wav", "x-wav"} else "mp3" if subtype in {"mpeg", "mp3"} else subtype
                    parts.append({"type": "input_audio", "input_audio": {"data": encoded, "format": audio_format}})
        return parts


class OpenAIProvider(OpenAICompatibleProvider):
    """Backward-compatible OpenAI adapter used by existing callers/tests."""

    def __init__(self, api_key: str, base_url: str):
        super().__init__("openai", api_key, base_url)


class OllamaProvider(OpenAICompatibleProvider):
    """Local Ollama's OpenAI-compatible API (no cloud key required)."""

    def __init__(self, base_url: str = "http://127.0.0.1:11434/v1"):
        super().__init__("ollama", "ollama-local", base_url)

    def _headers(self, key: str | None = None) -> dict[str, str]:
        return {"Content-Type": "application/json"}


class GeminiProvider:
    def __init__(self, provider: str, api_key: str, base_url: str):
        self.provider = str(provider or "gemini").lower()
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.last_usage: dict[str, int] | None = None

    def model_records(self, timeout: float = 20.0) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ProviderError("CHAT_PROVIDER_KEY_MISSING", "Gemini API key is not configured")
        try:
            import httpx

            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.get(f"{self.base_url}/models", params={"key": self.api_key})
            if response.status_code >= 400:
                raise ProviderError(f"CHAT_PROVIDER_HTTP_{response.status_code}")
            return [normalize_model("gemini", item) for item in _model_records(response.json()) if _as_model_id(item)]
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("CHAT_PROVIDER_MODELS_UNAVAILABLE", str(exc)) from None

    def models(self) -> list[str]:
        return [str(item["id"]) for item in self.model_records() if item.get("id")]

    @staticmethod
    def _parts(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            parts: list[dict[str, Any]] = []
            for item in value:
                if not isinstance(item, dict):
                    parts.append({"text": str(item)})
                elif item.get("type") == "inline_data":
                    parts.append({"inlineData": {"mimeType": item.get("mime_type"), "data": item.get("data")}})
                else:
                    text = item.get("text") or item.get("content")
                    if text is not None:
                        parts.append({"text": str(text)})
            return parts or [{"text": ""}]
        return [{"text": str(value or "")}]

    def stream(self, model: str, messages: list[dict[str, Any]], cancel, attachments: list[dict[str, Any]] | None = None) -> Iterator[str]:
        import httpx

        self.last_usage = None

        contents: list[dict[str, Any]] = []
        system: list[dict[str, Any]] = []
        for message in messages:
            if str(message.get("status") or "completed") != "completed":
                continue
            role = str(message.get("role") or "user")
            parts = self._parts(message.get("content"))
            if role == "system":
                system.extend(parts)
            else:
                contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        if attachments and contents:
            for item in attachments:
                if item.get("text"):
                    contents[-1]["parts"].append({"text": f"[{item.get('name') or 'attachment'}]\n{item['text']}"})
                elif item.get("data") and item.get("content_type"):
                    contents[-1]["parts"].append({"inlineData": {"mimeType": item["content_type"], "data": item["data"]}})
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16_000},
        }
        if system:
            payload["systemInstruction"] = {"parts": system}
        url = f"{self.base_url}/models/{model}:streamGenerateContent"
        try:
            with httpx.Client(timeout=180, trust_env=False) as client:
                with client.stream("POST", url, params={"alt": "sse", "key": self.api_key}, json=payload, headers={"Content-Type": "application/json"}) as response:
                    if response.status_code >= 400:
                        raise ProviderError(f"CHAT_PROVIDER_HTTP_{response.status_code}")
                    for line in response.iter_lines():
                        if cancel.is_set():
                            return
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                            usage = event.get("usageMetadata") if isinstance(event, dict) else None
                            if isinstance(usage, dict):
                                token_map = {
                                    "prompt_tokens": usage.get("promptTokenCount"),
                                    "completion_tokens": usage.get("candidatesTokenCount"),
                                    "total_tokens": usage.get("totalTokenCount"),
                                }
                                if all(isinstance(value, (int, float)) for value in token_map.values()):
                                    self.last_usage = {key: int(value) for key, value in token_map.items()}
                            candidates = event.get("candidates") or []
                            parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
                            for part in parts:
                                text = part.get("text") if isinstance(part, dict) else None
                                if isinstance(text, str) and text:
                                    yield text
                        except (ValueError, TypeError, KeyError, IndexError):
                            continue
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("CHAT_PROVIDER_STREAM_UNAVAILABLE", str(exc)) from None


def encode_attachment(path: str, content_type: str) -> dict[str, str]:
    """Read a validated local attachment for native multimodal providers."""
    with open(path, "rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")
    return {"content_type": content_type, "data": data}


class ChatGPTAccountProvider:
    BASE_URL = "https://chatgpt.com/backend-api/codex"
    CLIENT_VERSION = "0.142.5"

    def __init__(self, auth):
        self.auth = auth

    def _headers(self):
        tokens = self.auth.tokens()
        return {"Authorization": f"Bearer {tokens['access_token']}", "chatgpt-account-id": tokens["account_id"], "OpenAI-Beta": "responses=experimental", "originator": "codex_cli_rs", "Content-Type": "application/json"}

    @staticmethod
    def _models(raw) -> list[str]:
        lists = raw if isinstance(raw, list) else next((raw.get(key) for key in ("models", "data", "items", "available_models") if isinstance(raw.get(key), list)), []) if isinstance(raw, dict) else []
        found = []
        for item in lists:
            value = item if isinstance(item, str) else next((item.get(key) for key in ("slug", "id", "model", "name") if item.get(key)), None) if isinstance(item, dict) else None
            if isinstance(value, str) and value not in found:
                found.append(value)
        return found

    def models(self):
        import httpx

        response = httpx.get(f"{self.BASE_URL}/models?client_version={self.CLIENT_VERSION}", headers=self._headers(), timeout=30)
        response.raise_for_status()
        return self._models(response.json())

    def stream(self, model: str, messages: list[dict], cancel, attachments=None) -> Iterator[str]:
        import httpx

        inputs = [{"role": message["role"], "content": message["content"]} for message in messages if message.get("status") == "completed"]
        payload = {"model": model, "instructions": "You are a helpful assistant. Answer directly and helpfully.", "input": inputs, "stream": True, "store": False, "reasoning": {"effort": "medium", "summary": "auto"}, "text": {"verbosity": "medium"}, "include": ["reasoning.encrypted_content"]}
        with httpx.stream("POST", f"{self.BASE_URL}/responses?client_version={self.CLIENT_VERSION}", headers=self._headers(), json=payload, timeout=180) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if cancel.is_set():
                    return
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    return
                try:
                    event = json.loads(raw)
                    if event.get("type") in {"response.output_text.delta", "content.delta"} and isinstance(event.get("delta"), str):
                        yield event["delta"]
                except (ValueError, TypeError):
                    continue
