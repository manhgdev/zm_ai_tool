"""License endpoints available before the main app is unlocked."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from pipeline.core.license import activate_license, deactivate_license, license_status, start_desktop_login, finish_desktop_login

router = APIRouter(tags=["license"])


class ActivateIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@router.get("/api/license/status")
def get_license_status(force: bool = False) -> dict:
    return license_status(force=force)


@router.post("/api/license/activate")
def activate(body: ActivateIn) -> dict:
    try:
        return activate_license(body.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "LICENSE_SERVER_UNAVAILABLE") from exc


@router.post("/api/license/deactivate")
def deactivate() -> dict:
    try:
        return deactivate_license()
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post('/api/auth/desktop/start')
def desktop_start(request: Request):
    try:
        return start_desktop_login(request.url.port or 8787)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/api/auth/desktop/callback')
def desktop_callback(state: str = '', code: str = ''):
    try:
        finish_desktop_login(state, code)
        return HTMLResponse('<html lang="vi"><meta charset="utf-8"><title>ZM AI TOOL</title><p>Đã đăng nhập. Bạn có thể quay lại ứng dụng. / Signed in. You can return to the app.</p></html>', headers={'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store'})
    except Exception:
        return HTMLResponse('<html lang="vi"><meta charset="utf-8"><title>ZM AI TOOL</title><p>Đăng nhập chưa hoàn tất. Hãy thử lại trong ứng dụng. / Sign-in failed. Please try again in the app.</p></html>', status_code=400, headers={'Cache-Control': 'no-store'})
