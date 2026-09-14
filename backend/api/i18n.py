"""Request locale shared with the desktop frontend (vi or en)."""
from __future__ import annotations

from contextvars import ContextVar
from fastapi import Request

_locale: ContextVar[str] = ContextVar("locale", default="vi")


def locale_from_request(request: Request) -> str:
    saved = (request.cookies.get("zm_ai_tool_locale") or "").lower()
    if saved in {"vi", "en"}:
        return saved
    accepted = (request.headers.get("accept-language") or "").lower()
    return "vi" if accepted.startswith("vi") else "en"


def current_locale() -> str:
    return _locale.get()


def set_request_locale(request: Request):
    return _locale.set(locale_from_request(request))


def reset_request_locale(token) -> None:
    _locale.reset(token)
