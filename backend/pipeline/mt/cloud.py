"""Machine translation backends — cloud."""
from __future__ import annotations

"""MT: Ollama + Google free fallback."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import re
import threading
import time
from typing import Any

import httpx

from pipeline.core.jobs import check_cancel
from pipeline.core.project import set_status
from pipeline.core.resources import progress_msg


from .text import *  # noqa: F403

_NVIDIA_RPM = 40
_nvidia_rate_lock = threading.Lock()
_nvidia_next_request_at = 0.0
_gemini_rate_lock = threading.Lock()
_gemini_next_request_at = 0.0
_gemini_rate_limited_until = 0.0
_KEY_FAILOVER = {401, 403, 429, 500, 502, 503, 504}


def _cloud_error(provider: str, reason: str) -> RuntimeError:
    return RuntimeError(f"CLOUD_TRANSLATION_{provider.upper().replace('-', '_')}_{reason}")


def _http_error(provider: str, status: int) -> RuntimeError:
    reason = {
        400: "MODEL_OR_REQUEST_INVALID",
        401: "AUTH_FAILED",
        403: "ACCESS_DENIED",
        404: "MODEL_OR_REQUEST_INVALID",
        422: "MODEL_OR_REQUEST_INVALID",
        429: "RATE_LIMITED_OR_QUOTA",
        500: "SERVICE_UNAVAILABLE",
        502: "SERVICE_UNAVAILABLE",
        503: "SERVICE_UNAVAILABLE",
        504: "SERVICE_UNAVAILABLE",
    }.get(status, "REQUEST_FAILED")
    return _cloud_error(provider, reason)


def _usable_keys(*, api_key: str = "", api_keys: list[str] | None = None) -> list[str]:
    keys = list(dict.fromkeys(key.strip() for key in (api_keys or [api_key]) if str(key).strip()))
    if not keys:
        raise RuntimeError("API key is required")
    random.SystemRandom().shuffle(keys)
    return keys


def _wait_for_gemini_request() -> None:
    """Temporarily pace all Gemini callers after the API reports 429."""
    global _gemini_next_request_at
    with _gemini_rate_lock:
        now = time.monotonic()
        interval = 6.0 if now < _gemini_rate_limited_until else 0.0
        wait_s = max(0.0, _gemini_next_request_at - now) if interval else 0.0
        _gemini_next_request_at = max(now, _gemini_next_request_at) + interval
    if wait_s:
        time.sleep(wait_s)


def _gemini_retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        header = float(response.headers.get("retry-after") or 0)
    except ValueError:
        header = 0.0
    if header > 0:
        return header
    try:
        details = ((response.json().get("error") or {}).get("details")) or []
        for detail in details:
            match = re.fullmatch(r"([\d.]+)s", str(detail.get("retryDelay") or ""))
            if match:
                return float(match.group(1))
    except (AttributeError, TypeError, ValueError):
        pass
    return min(60.0, 10.0 * (2 ** attempt))


def _cool_down_gemini(retry_s: float) -> None:
    global _gemini_next_request_at, _gemini_rate_limited_until
    with _gemini_rate_lock:
        now = time.monotonic()
        _gemini_next_request_at = max(_gemini_next_request_at, now + retry_s)
        _gemini_rate_limited_until = max(_gemini_rate_limited_until, now + 300.0)


def _wait_for_nvidia_request() -> None:
    """Reserve one shared NVIDIA request slot (40 RPM across all projects)."""
    global _nvidia_next_request_at
    interval = 60.0 / _NVIDIA_RPM
    with _nvidia_rate_lock:
        now = time.monotonic()
        wait_s = max(0.0, _nvidia_next_request_at - now)
        _nvidia_next_request_at = max(now, _nvidia_next_request_at) + interval
    if wait_s:
        time.sleep(wait_s)


def _cool_down_nvidia(retry_s: float) -> None:
    global _nvidia_next_request_at
    with _nvidia_rate_lock:
        _nvidia_next_request_at = max(_nvidia_next_request_at, time.monotonic() + retry_s)

def _cloud_batch_prompt(
    chunk: list[str], *, target_lang: str, source_lang: str
) -> str:
    n = len(chunk)
    numbered = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(chunk))
    src = source_lang if source_lang not in ("", "auto") else "auto"
    src_name = _lang_name(src) if src != "auto" else "the source language"
    return (
        f"Translate these {n} video subtitles from {src_name} into {_lang_name(target_lang)}. "
        "Be faithful. Do not invent content. "
        f"Return exactly {n} numbered lines only, format: 1. translation\\n2. ...\n\n"
        f"{numbered}"
    )


def _estimate_tokens(text: str) -> int:
    """Token estimate: CJK=1 char/token, mixed≈1.5, ASCII≈4."""
    cjk = sum(1 for c in text if "一" <= c <= "鿿" or "぀" <= c <= "ヿ")
    latin = len(text) - cjk
    return cjk + latin // 3 + 1


def _short_429_pause(response: httpx.Response, attempt: int) -> float:
    """Local wait (seconds) after every key returns 429 — no shared cooldown."""
    return max(3.0, min(8.0, _gemini_retry_delay(response, attempt)))


def _openai_compatible_chat(
    *,
    base_url: str,
    api_key: str = "",
    api_keys: list[str] | None = None,
    model: str,
    prompt: str,
    timeout: float = 120.0,
    max_output_tokens: int = 512,
    max_input_tokens: int = 200_000,
    system_msg: str = "You translate video subtitles. Output numbered lines only.",
    provider: str = "cloud",
) -> str:
    """Cap input+output ≤ max_input_tokens; raise ValueError if prompt too big."""
    in_est = _estimate_tokens(prompt) + _estimate_tokens(system_msg)
    total_est = in_est + max_output_tokens
    if total_est > max_input_tokens:
        raise ValueError(
            f"Prompt ~{in_est} tokens + output {max_output_tokens} > "
            f"limit {max_input_tokens}. Retry with smaller batch."
        )
    keys = _usable_keys(api_key=api_key, api_keys=api_keys)
    multi = len(keys) > 1
    url = base_url.rstrip("/") + "/chat/completions"
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        is_nvidia = "integrate.api.nvidia.com" in url.lower()
        key_index = 0
        failed_keys = 0
        nvidia_attempt = 0
        rate_attempt = 0
        first_429: int | None = None
        while True:
            # ponytail: multi-key skips shared NVIDIA pacing so a 429 on one key
            # does not stall the others; single-key keeps the 40 RPM slot.
            if is_nvidia and not multi:
                _wait_for_nvidia_request()
            try:
                r = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {keys[key_index]}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://localhost/zm_ai_tool",
                        "X-Title": "ZM AI TOOL",
                    },
                    json={
                        "model": model,
                        "temperature": 0,
                        "max_tokens": max_output_tokens,
                        "messages": [
                            {
                                "role": "system",
                                "content": system_msg,
                            },
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
            except httpx.HTTPError:
                raise _cloud_error(provider, "NETWORK_UNAVAILABLE") from None
            if r.status_code == 429:
                if first_429 is None:
                    first_429 = key_index
                if multi and failed_keys < len(keys) - 1:
                    key_index = (key_index + 1) % len(keys)
                    failed_keys += 1
                    continue
                if multi and rate_attempt < 3:
                    # All keys 429 → pause briefly, resume on the oldest (first) key.
                    time.sleep(_short_429_pause(r, rate_attempt))
                    key_index = first_429
                    failed_keys = 0
                    first_429 = None
                    rate_attempt += 1
                    continue
                if not multi and is_nvidia and nvidia_attempt < 2:
                    try:
                        retry_s = float(r.headers.get("retry-after") or 0)
                    except ValueError:
                        retry_s = 0.0
                    _cool_down_nvidia(max(5.0, retry_s))
                    nvidia_attempt += 1
                    continue
                break
            if r.status_code in _KEY_FAILOVER and failed_keys < len(keys) - 1:
                first_429 = None
                key_index = (key_index + 1) % len(keys)
                failed_keys += 1
                continue
            break
        if r.status_code >= 400:
            # Never propagate httpx's URL here: Gemini auth is carried in the
            # query string and its repr can expose an API key in job logs.
            raise _http_error(provider, r.status_code)
        data = r.json()
        return (
            (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
            or ""
        ).strip()


def _nvidia_riva_language_codes(source_lang: str, target_lang: str, text: str) -> tuple[str, str]:
    """Resolve Riva's source/target codes without losing zh-cn's hyphen."""
    aliases = {"zh": "zh-cn", "vi": "vi", "en": "en", "ja": "ja", "ko": "ko"}
    source = (source_lang or "auto").lower().replace("_", "-")
    if source == "auto":
        source = "zh-cn" if re.search(r"[\u3400-\u9fff\uf900-\ufaff]", text) else "en"
    else:
        source = aliases.get(source.split("-")[0], source)
    target = aliases.get((target_lang or "vi").lower().split("-")[0], target_lang)
    return source, target


def _nvidia_riva_language_pair(source_lang: str, target_lang: str, text: str) -> str:
    """Riva Translate expects the language pair in the system message."""
    source, target = _nvidia_riva_language_codes(source_lang, target_lang, text)
    return f"{source}-{target}"


def _gemini_generate(
    *,
    base_url: str,
    api_key: str = "",
    api_keys: list[str] | None = None,
    model: str,
    prompt: str,
    timeout: float = 120.0,
    max_output_tokens: int = 2048,
) -> str:
    # base: .../v1beta  →  .../v1beta/models/{model}:generateContent
    root = (base_url or "").rstrip("/")
    if not root:
        root = "https://generativelanguage.googleapis.com/v1beta"
    elif not root.endswith("v1beta") and "/models" not in root:
        root = root + "/v1beta"
    url = f"{root}/models/{model}:generateContent"
    keys = _usable_keys(api_key=api_key, api_keys=api_keys)
    multi = len(keys) > 1
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        rate_attempt = 0
        transient_attempt = 0
        transport_attempt = 0
        key_index = 0
        failed_keys = 0
        first_429: int | None = None
        while True:
            # Multi-key: never share the Gemini cooldown — a 429 on one key must
            # not stall the others. Single-key keeps the previous wait behavior.
            if not multi:
                _wait_for_gemini_request()
            try:
                r = client.post(
                    url,
                    params={"key": keys[key_index]},
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0, "maxOutputTokens": max_output_tokens},
                    },
                )
            except httpx.TransportError:
                # DNS/connect/reset errors have no HTTP status, so they used
                # to skip the retry policy and fail the whole Review at once.
                if transport_attempt >= 2:
                    raise _cloud_error("gemini", "NETWORK_UNAVAILABLE") from None
                time.sleep(2 ** transport_attempt)
                transport_attempt += 1
                continue
            if r.status_code == 429:
                if first_429 is None:
                    first_429 = key_index
                if multi and failed_keys < len(keys) - 1:
                    key_index = (key_index + 1) % len(keys)
                    failed_keys += 1
                    continue
                if multi and rate_attempt < 3:
                    # All keys 429 → wait a few seconds, then retry the oldest key.
                    time.sleep(_short_429_pause(r, rate_attempt))
                    key_index = first_429
                    failed_keys = 0
                    first_429 = None
                    rate_attempt += 1
                    continue
                if not multi and rate_attempt < 7:
                    _cool_down_gemini(_gemini_retry_delay(r, rate_attempt))
                    rate_attempt += 1
                    continue
                break
            if r.status_code in _KEY_FAILOVER and failed_keys < len(keys) - 1:
                first_429 = None
                key_index = (key_index + 1) % len(keys)
                failed_keys += 1
                continue
            if r.status_code in {500, 502, 503, 504} and transient_attempt < 3:
                time.sleep(2 ** (transient_attempt + 1))
                transient_attempt += 1
                continue
            break
        if r.status_code >= 400:
            # Keep the Review error safe to show in queue logs: an httpx
            # exception includes Gemini's query-string API key.
            raise _http_error("gemini", r.status_code)
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        return "".join(str(p.get("text") or "") for p in parts).strip()


def translate_cloud(
    texts: list[str],
    target_lang: str,
    provider: str,
    project_id: str | None = None,
    *,
    source_lang: str = "auto",
    batch_size: int = 8,
    workers: int = 2,
) -> list[str]:
    """OpenAI / DeepSeek / OpenRouter (chat) hoặc Gemini generateContent."""
    from pipeline.core.app_config import provider_credentials, provider_api_keys

    pid = (provider or "").lower().strip()
    if pid == "9router":
        pid = "openrouter"
    if pid == "xai":
        pid = "grok"
    try:
        cred = provider_credentials(pid)
        api_keys = provider_api_keys(pid)
    except RuntimeError as exc:
        if "API key" in str(exc):
            raise _cloud_error(pid, "API_KEY_MISSING") from None
        raise
    base_url, model = cred["baseUrl"], cred["model"]
    out: list[str] = [""] * len(texts)
    if not texts:
        return out
    total = max(1, len(texts))
    riva_translate = pid == "nvidia" and "riva-translate" in model.lower()
    bs = 1 if riva_translate else max(1, min(16, int(batch_size or 8)))
    starts = list(range(0, len(texts), bs))
    w = max(1, min(6, int(workers or 2), len(starts)))
    label = {
        "openai": "OpenAI",
        "gemini": "Gemini",
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "grok": "Grok",
        "groq": "Groq",
        "nvidia": "NVIDIA NIM",
    }.get(pid, pid)
    if project_id:
        set_status(
            project_id,
            step="translate",
            progress=55,
            message=progress_msg(f"Dịch {label}", workers=w, extra=str(model)),
            running=True,
        )
    done = 0
    done_lock = __import__("threading").Lock()

    def _batch(start: int) -> tuple[int, list[str]]:
        check_cancel(project_id)
        chunk = texts[start : start + bs]
        if riva_translate:
            source_code, target_code = _nvidia_riva_language_codes(
                source_lang, target_lang, chunk[0]
            )
            riva_input = chunk[0]
            if source_code == target_code:
                return start, [_clean_burn_text(riva_input, target_lang=target_lang)]
            # Riva supports English-centric pairs. Pivot through English for
            # e.g. Chinese -> Vietnamese, preserving each segment's position.
            pairs = [(source_code, target_code)] if 'en' in (source_code, target_code) else [
                (source_code, 'en'), ('en', target_code),
            ]
            raw = riva_input
            for source, target in pairs:
                check_cancel(project_id)
                raw = _openai_compatible_chat(
                    base_url=base_url,
                    api_keys=api_keys,
                    model=model,
                    prompt=raw,
                    system_msg=f'{source}-{target}',
                    max_output_tokens=512,
                    max_input_tokens=8_192,
                    provider=pid,
                )
                if not raw or not raw.strip():
                    raise _cloud_error(pid, 'INVALID_RESPONSE')
            cleaned = _clean_burn_text(raw, target_lang=target_lang)
            if not cleaned:
                raise _cloud_error(pid, "INVALID_RESPONSE")
            return start, [cleaned]
        prompt = _cloud_batch_prompt(chunk, target_lang=target_lang, source_lang=source_lang)
        try:
            if pid == "gemini":
                raw = _gemini_generate(
                    base_url=base_url, api_keys=api_keys, model=model, prompt=prompt
                )
            else:
                raw = _openai_compatible_chat(
                    base_url=base_url, api_keys=api_keys, model=model, prompt=prompt, provider=pid
                )
        except ValueError:
            # Input too large for context window — send 1-by-1 instead
            raw = None
        if raw is None:
            # 1 request / câu (batch quá lớn hoặc parse fail)
            parsed = []
            for text in chunk:
                check_cancel(project_id)
                one = (
                    f"Translate into {_lang_name(target_lang)}. "
                    "Return ONLY the translation.\n\n"
                    f"{text}"
                )
                if pid == "gemini":
                    line = _gemini_generate(
                        base_url=base_url, api_keys=api_keys, model=model, prompt=one
                    )
                else:
                    line = _openai_compatible_chat(
                        base_url=base_url,
                        api_keys=api_keys,
                        model=model,
                        prompt=one,
                        max_output_tokens=256,
                        provider=pid,
                    )
                line = (line or "").strip().splitlines()[0].strip()
                if not line:
                    raise _cloud_error(pid, "INVALID_RESPONSE")
                parsed.append(line)
        else:
            parsed = _parse_numbered_batch(raw, len(chunk), chunk)
            if parsed is None:
                # parse fail — retry 1-by-1
                parsed = []
                for text in chunk:
                    check_cancel(project_id)
                    one = (
                        f"Translate into {_lang_name(target_lang)}. "
                        "Return ONLY the translation.\n\n"
                        f"{text}"
                    )
                    if pid == "gemini":
                        line = _gemini_generate(
                            base_url=base_url,
                            api_keys=api_keys,
                            model=model,
                            prompt=one,
                        )
                    else:
                        line = _openai_compatible_chat(
                            base_url=base_url,
                            api_keys=api_keys,
                            model=model,
                            prompt=one,
                            max_output_tokens=256,
                            provider=pid,
                        )
                    line = (line or "").strip().splitlines()[0].strip()
                    if not line:
                        raise _cloud_error(pid, "INVALID_RESPONSE")
                    parsed.append(line)
        cleaned = []
        for text, line in zip(chunk, parsed):
            line = _clean_burn_text(line, target_lang=target_lang) or line
            if not (line or "").strip():
                raise _cloud_error(pid, "INVALID_RESPONSE")
            cleaned.append(line.strip())
        return start, cleaned

    with ThreadPoolExecutor(max_workers=w, thread_name_prefix=f"cloud-{pid}") as pool:
        futs = {pool.submit(_batch, s): s for s in starts}
        for fut in as_completed(futs):
            check_cancel(project_id)
            start, lines = fut.result()
            for j, line in enumerate(lines):
                out[start + j] = line
            with done_lock:
                done += len(lines)
                cur = done
            if project_id:
                set_status(
                    project_id,
                    step="translate",
                    progress=55 + int(35 * cur / total),
                    message=progress_msg(f"Dịch {label}", cur, total, workers=w),
                    running=True,
                )
    return out
