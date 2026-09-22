from __future__ import annotations

from itertools import chain
from typing import Any

from fastapi import APIRouter, Body, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from pipeline.chat.service import ChatService
from pipeline.chat.providers import ProviderError
from pipeline.core.app_config import load_app_config
from api.i18n import current_locale

router = APIRouter(prefix="/api/chat", tags=["chat"])
service = ChatService()


def _t(vi: str, en: str) -> str:
    return vi if current_locale() == "vi" else en


def _sse(stream):
    """Start the generator before sending HTTP headers so Stop has a target."""
    try:
        first = next(stream)
    except StopIteration:
        first = ""
    return StreamingResponse(chain((first,), stream), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ConversationIn(BaseModel):
    title: str = "Cuộc trò chuyện mới"
    accountId: str = "openrouter"
    provider: str = ""
    providerId: str = ""
    model: str = ""


@router.get("/accounts")
def accounts():
    return service.list_accounts()


@router.get("/providers")
def providers(refresh: bool = False):
    return {"providers": service.providers(refresh=refresh)}


@router.post("/accounts")
def create_account(body: dict[str, Any] = Body(...)):
    provider = body.get("provider")
    if provider == "chatgpt_account":
        return service.create_account(str(body.get("label") or "ChatGPT"))
    raise HTTPException(400, _t("Nhà cung cấp tài khoản không được hỗ trợ", "Unsupported account provider"))


@router.post("/accounts/{account_id}/login")
def oauth_login(account_id: str, open_browser: bool = True):
    try:
        return service.open_browser_login(account_id, open_browser=open_browser)
    except KeyError as exc:
        raise HTTPException(404, _t("Không tìm thấy tài khoản.", "Account not found.")) from exc
    except Exception as exc:
        chrome_required = "CHAT_CHROME_REQUIRED" in str(exc)
        raise HTTPException(502, detail={"code": "chatgpt_chrome_required" if chrome_required else "chatgpt_login_failed", "message": _t("Không tìm thấy Google Chrome. Hãy cài Google Chrome rồi đăng nhập lại.", "Google Chrome was not found. Install Google Chrome, then sign in again.") if chrome_required else _t("Không bắt đầu được đăng nhập ChatGPT Codex.", "Could not start ChatGPT Codex sign-in."), "reason": str(exc)}) from exc


@router.post("/accounts/{account_id}/login-link")
def oauth_login_link(account_id: str):
    """Link-only endpoint: never launch a browser, regardless of query defaults."""
    return oauth_login(account_id, open_browser=False)


@router.post("/accounts/{account_id}/login/{login_id}/cancel")
def oauth_cancel(account_id: str, login_id: str):
    auth = service.auth_for(account_id)
    login = auth._pending.get(login_id)
    if login is not None:
        login.error = 'LOGIN_CANCELLED'
        auth.poll(login_id)
    return {'ok': True}


@router.post("/accounts/{account_id}/login/{login_id}/poll")
def oauth_poll(account_id: str, login_id: str):
    try:
        return service.poll_login(account_id, login_id)
    except KeyError as exc:
        raise HTTPException(404, _t("Phiên đăng nhập không tồn tại hoặc đã hết hạn.", "The sign-in session does not exist or has expired.")) from exc
    except Exception as exc:
        raise HTTPException(502, detail={"code": "oauth_poll_failed", "message": _t("Đăng nhập ChatGPT Codex thất bại.", "ChatGPT Codex sign-in failed."), "reason": str(exc)}) from exc


@router.post("/accounts/{account_id}/logout")
def logout(account_id: str):
    try:
        return service.browser_logout(account_id)
    except KeyError as exc:
        raise HTTPException(404, _t("Không tìm thấy tài khoản.", "Account not found.")) from exc
    except Exception as exc:
        raise HTTPException(502, detail={"code": "chatgpt_logout_failed", "message": _t("Không đăng xuất được ChatGPT.", "Could not sign out from ChatGPT."), "reason": str(exc)}) from exc


@router.post("/accounts/{account_id}/refresh")
def refresh(account_id: str):
    try:
        return service.browser_health(account_id)
    except KeyError as exc:
        raise HTTPException(404, _t("Không tìm thấy tài khoản.", "Account not found.")) from exc


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: str, deleteHistory: bool = False):
    if not service.delete_account(account_id, delete_history=deleteHistory): raise HTTPException(404, _t("Không tìm thấy tài khoản.", "Account not found."))


@router.get("/accounts/{account_id}/health")
def account_health(account_id: str):
    try:
        return service.browser_health(account_id)
    except KeyError as exc:
        raise HTTPException(404, _t("Không tìm thấy tài khoản.", "Account not found.")) from exc


@router.get("/models")
def models(accountId: str = "", provider: str = "", refresh: bool = False):
    if provider:
        try:
            return {"models": service.provider_models(provider, refresh=refresh)}
        except ProviderError as exc:
            secret = str(load_app_config().get("cloud", {}).get(provider, {}).get("apiKey") or "")
            return {"models": [], "errorCode": exc.code, "reason": exc.safe_message(secret)}
    if not accountId:
        # Without a provider filter, return the complete configured catalog so
        # callers do not mistake the default OpenRouter slice for the whole
        # ZMTool model set.  Each item still carries its provider id and free
        # capability metadata; no API key is included.
        rows = service.providers(refresh=refresh)
        all_models = [model for item in rows for model in item.get("models", []) if isinstance(model, dict)]
        return {"models": all_models, "providers": rows}
    return {"models": service.models(accountId)}


@router.get("/conversations")
def conversations():
    return service.store.list_conversations()


@router.post("/conversations")
def create_conversation(body: ConversationIn):
    provider = str(body.provider or body.providerId or service.DEFAULT_API_PROVIDER).strip().lower()
    account_id = str(body.accountId or provider).strip()
    service.remember_model(account_id if provider == "chatgpt_web" else provider, body.model)
    return service.store.create_conversation(body.title, account_id, body.model, provider_id=provider)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    conv = service.store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    attachments = service.store.list_attachments(conversation_id)
    messages = service.store.list_messages(conversation_id)
    for message in messages:
        message["attachments"] = [{**item, "url": f"/api/chat/artifacts/{item['id']}"} for item in attachments if item["message_id"] == message["id"]]
    return {**conv, "messages": messages}


@router.patch("/conversations/{conversation_id}")
def update_conversation(conversation_id: str, body: dict[str, Any] = Body(...)):
    provider_id = body.get("provider") if body.get("provider") is not None else (body.get("providerId") if body.get("providerId") is not None else body.get("provider_id"))
    if provider_id is not None:
        provider_id = str(provider_id).strip().lower()
    account_id = body.get("accountId") if body.get("accountId") is not None else body.get("account_id")
    conv = service.store.update_conversation(conversation_id, title=body.get("title"), account_id=account_id, provider_id=provider_id, model=body.get("model"))
    if not conv:
        raise HTTPException(404, "Conversation not found")
    service.remember_model(conv["account_id"] if conv.get("provider_id") == "chatgpt_web" else conv.get("provider_id", conv["account_id"]), conv["model"])
    return conv


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str):
    service.cancel(conversation_id)
    service.store.delete_conversation(conversation_id)


@router.post("/conversations/{conversation_id}/attachments")
async def upload_attachment(conversation_id: str, file: UploadFile):
    if not service.store.get_conversation(conversation_id):
        raise HTTPException(404, "Conversation not found")
    content = await file.read(20 * 1024 * 1024 + 1)
    try:
        return service.store.save_attachment(conversation_id, file.filename or "attachment", content, content_type=file.content_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages")
def send_message(conversation_id: str, body: dict[str, Any] = Body(...)):
    if not service.store.get_conversation(conversation_id):
        raise HTTPException(404, "Conversation not found")
    try:
        service.validate_prompt(body)
        service.validate_attachments(conversation_id, body.get("attachmentIds", []))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _sse(service.stream_message(conversation_id, body))


@router.get("/conversations/{conversation_id}/attachments")
def attachments(conversation_id: str):
    return [{**item, "url": f"/api/chat/artifacts/{item['id']}"} for item in service.store.list_attachments(conversation_id)]


@router.get("/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    try: path = service.store.attachment_path(artifact_id)
    except (KeyError, ValueError) as exc: raise HTTPException(404, _t("Không tìm thấy tệp.", "File not found.")) from exc
    return FileResponse(path)


@router.post("/conversations/{conversation_id}/cancel")
def cancel(conversation_id: str):
    return {"cancelled": service.cancel(conversation_id)}


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry")
def retry(conversation_id: str, message_id: str, body: dict[str, Any] | None = Body(default=None)):
    messages = service.store.list_messages(conversation_id)
    try:
        index = next(i for i, msg in enumerate(messages) if msg["id"] == message_id)
    except StopIteration as exc:
        raise HTTPException(404, "Message not found") from exc
    prior = next((m for m in reversed(messages[:index]) if m["role"] == "user"), None)
    if not prior:
        raise HTTPException(400, "No user message to retry")
    payload = {
        "content": prior["content"],
        "mode": (body or {}).get("mode"),
        "provider": (body or {}).get("provider"),
        "model": (body or {}).get("model"),
    }
    return _sse(service.stream_message(conversation_id, payload))
