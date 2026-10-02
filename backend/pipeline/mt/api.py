"""Machine translation backends — api."""
from __future__ import annotations

"""MT: Ollama + Google free fallback."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from pipeline.core.jobs import check_cancel
from pipeline.core.project import set_status


from .text import *  # noqa: F403
from .free import translate_google_free, translate_mymemory, translate_tiktok
from .ollama import translate_ollama
from .cloud import translate_cloud

def translate_segments(
    texts: list[str],
    target_lang: str,
    project_id: str | None = None,
    *,
    source_lang: str = "auto",
    translator: str = "google",
    workers: int = 2,
    ollama_mode: str = "cloud",
    ollama_model: str = "minimax-m3:cloud",
    ollama_local_tier: str = "balanced",
    durations: list[float] | None = None,
    translation_model: str | None = None,
) -> list[str]:
    """google | mymemory | tiktok | ollama | openai | gemini | deepseek | openrouter | grok | groq | nvidia.

    Free MT fallback cứng: Google → TikTok → MyMemory (bỏ engine đã thử).
    """
    if not texts:
        return []
    eng = (translator or "google").lower().strip()
    if eng in ("9router", "open-router"):
        eng = "openrouter"
    if eng in ("xai", "x-ai"):
        eng = "grok"
    if eng in ("tt", "tiktok_trans", "tiktok-translate"):
        eng = "tiktok"
    if eng in ("mm", "my-memory", "my_memory"):
        eng = "mymemory"
    if eng == "capcut":
        # CapCut only exposes translation through its media STT task.  The
        # video pipeline intercepts this choice before reaching text MT;
        # reject unsupported text-only callers instead of silently using
        # Google and misreporting the selected provider.
        raise RuntimeError("CapCut cloud chỉ dịch khi nhận dạng trực tiếp từ video.")
    w = max(1, min(16, int(workers or 2)))

    def _clean_all(raw: list[str]) -> list[str]:
        out: list[str] = []
        for src, tr in zip(texts, raw):
            cleaned = _clean_burn_text(tr, target_lang=target_lang) or tr
            out.append((cleaned or "").strip() or src)
        return out

    def _run_free(name: str) -> list[str]:
        if name == "google":
            return translate_google_free(
                texts,
                target_lang,
                source_lang,
                workers=w,
                project_id=project_id,
            )
        if name == "tiktok":
            return translate_tiktok(
                texts,
                target_lang,
                source_lang,
                workers=min(w, 6),
                project_id=project_id,
            )
        if name == "mymemory":
            return translate_mymemory(
                texts,
                target_lang,
                source_lang,
                workers=min(w, 8),
                project_id=project_id,
            )
        raise RuntimeError(f"unknown free mt: {name}")

    def _free_chain(primary: str) -> list[str]:
        # Thứ tự fallback: Google → TikTok → MyMemory (primary lên đầu)
        base = ["google", "tiktok", "mymemory"]
        order = [primary] + [x for x in base if x != primary]
        last_err: Exception | None = None
        for name in order:
            try:
                if project_id and name != primary:
                    set_status(
                        project_id,
                        step="translate",
                        progress=58,
                        message=f"Fallback {name}…",
                        running=True,
                    )
                raw = _run_free(name)
                out = _clean_all(raw)
                # vá chỗ rỗng/hỏng bằng engine kế trong chain
                need = [
                    i
                    for i, (s, t) in enumerate(zip(texts, out))
                    if _needs_google_fallback(s, t, target_lang=target_lang)
                ]
                if not need:
                    return out
                for fb in order:
                    if fb == name:
                        continue
                    try:
                        fixed = _run_free(fb)
                        # chỉ lấy các index need
                        for i in need:
                            tr = fixed[i] if i < len(fixed) else ""
                            cleaned = _clean_burn_text(tr, target_lang=target_lang) or tr
                            if cleaned and not _needs_google_fallback(
                                texts[i], cleaned, target_lang=target_lang
                            ):
                                out[i] = cleaned.strip() or texts[i]
                        need = [
                            i
                            for i, (s, t) in enumerate(zip(texts, out))
                            if _needs_google_fallback(s, t, target_lang=target_lang)
                        ]
                        if not need:
                            return out
                    except (
                        httpx.HTTPError,
                        RuntimeError,
                        ValueError,
                        TypeError,
                        IndexError,
                    ):
                        continue
                return out
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError, IndexError) as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return list(texts)

    # Cloud provider is an explicit user choice: retry/rotate only inside it.
    if eng in ("openai", "gemini", "deepseek", "openrouter", "grok", "groq", "nvidia", "mistral"):
        raw = translate_cloud(
            texts,
            target_lang,
            eng,
            project_id=project_id,
            source_lang=source_lang,
            workers=w,
            model_override=translation_model,
        )
        out = _clean_all(raw)
        if any(_needs_google_fallback(s, t, target_lang=target_lang) for s, t in zip(texts, out)):
            raise RuntimeError(f"CLOUD_TRANSLATION_{eng.upper()}_INVALID_RESPONSE")
        return out

    if eng in ("ollama", "local", "llm"):
        # Ollama là lựa chọn chủ động: lỗi phải nổi lên UI, tuyệt đối không âm thầm
        # thay toàn bộ bản dịch bằng Google.
        raw = translate_ollama(
            texts,
            target_lang,
            project_id=project_id,
            source_lang=source_lang,
            workers=w,
            mode=ollama_mode,
            model=ollama_model,
            local_tier=ollama_local_tier,
            durations=durations,
        )
        return _clean_all(raw)

    # Free: google | tiktok | mymemory (+ fallback chain)
    if eng not in ("google", "tiktok", "mymemory"):
        eng = "google"
    return _free_chain(eng)


def _with_google_fallback(
    texts: list[str],
    translations: list[str],
    *,
    target_lang: str,
    source_lang: str,
    project_id: str | None = None,
    workers: int = 8,
) -> list[str]:
    """Chỗ nào LLM hỏng → Google free (song song)."""
    out = list(translations)
    need = [
        i
        for i, (src, tr) in enumerate(zip(texts, out))
        if _needs_google_fallback(src, tr, target_lang=target_lang)
    ]
    if not need:
        return out
    if project_id:
        set_status(
            project_id,
            step="translate",
            progress=78,
            message=f"Google fallback {len(need)} đoạn…",
            running=True,
        )
    try:
        fixed = translate_google_free(
            [texts[i] for i in need],
            target_lang,
            source_lang,
            workers=workers,
            project_id=None,
        )
        for i, tr in zip(need, fixed):
            cleaned = _clean_burn_text(tr, target_lang=target_lang) or tr
            out[i] = cleaned.strip() or texts[i]
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        for i in need:
            if not (out[i] or "").strip() or _needs_google_fallback(
                texts[i], out[i], target_lang=target_lang
            ):
                out[i] = texts[i]
    return out
