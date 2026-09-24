"""FastAPI application factory."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes_all import router
from pipeline.core.cleanup import run_public_cleanup_periodically
from pipeline.core.config import PUBLIC_DATA


def _allowed_origins() -> list[str]:
    """Origin của chính app (dev Vite + webview) + cấu hình thêm qua env."""
    import os

    port = str(os.environ.get("ZM_AI_TOOL_PORT") or 8787)
    origins: list[str] = []
    for host in ("localhost", "127.0.0.1"):
        for p in (port, "5173", "4173"):
            origins.append(f"http://{host}:{p}")
    # webview desktop load từ file:// hoặc app scheme → Origin "null"
    origins.append("null")
    extra = (
        os.environ.get("ZM_AI_TOOL_ALLOW_ORIGINS")

        or ""
    ).strip()
    if extra:
        origins.extend(o.strip() for o in extra.split(",") if o.strip())
    seen: set[str] = set()
    return [o for o in origins if not (o in seen or seen.add(o))]


def create_app() -> FastAPI:
    # ponytail: do NOT import torch/GPU stuff here — blocks the main thread 2–10s.
    # Model loading remains lazy; the first feature that needs it owns warm-up.
    try:
        from pipeline.core.config import sanitize_httpx_no_proxy

        sanitize_httpx_no_proxy()
    except Exception:
        pass
    # Windows: hide subprocess console windows (cheap, 1ms)
    try:
        from pipeline.core.winproc import apply_subprocess_no_window
        apply_subprocess_no_window()
    except Exception:
        pass

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            from pipeline.core.config import ensure_data_dirs

            ensure_data_dirs()
        except Exception:
            pass
        try:
            from pipeline.download import ensure_download_dirs

            ensure_download_dirs()
        except Exception:
            pass
        try:
            from pipeline.flow import service as flow_service

            flow_service.start()
        except Exception:
            pass
        try:
            from pipeline.automation import service as automation_service

            automation_service.start()
        except Exception:
            pass


        threading.Thread(
            target=run_public_cleanup_periodically,
            name="cleanup-public",
            daemon=True,
        ).start()

        try:
            from pipeline.core.app_log import append_log, install_process_hooks

            install_process_hooks()
            append_log("[api] lifespan start", also_print=False)
        except Exception:
            pass

        def _run() -> None:
            # Set process environment only. VieNeu/Whisper/Torch are loaded by
            # their first real task instead of consuming CPU and writing logs
            # while the UI is idle.
            try:
                from pipeline.core.accel import apply_gpu_process_env
                apply_gpu_process_env()
            except Exception:
                pass

        def _warm_checks() -> None:
            """Pre-warm _CHECKS_CACHE ngay sau server start — request đầu tiên trả cache ~<1s."""
            try:
                from pipeline.core.system_check import system_checks
                system_checks(fast=True)
            except Exception:
                pass

        threading.Thread(target=_warm_checks, name="warm-checks", daemon=True).start()
        threading.Thread(target=_run, name="prepare-accel", daemon=True).start()
        yield

    app = FastAPI(title="ZM AI TOOL Local", lifespan=lifespan)
    # API local không có auth → chỉ nhận origin của chính app (Vite dev / webview).
    # allow_origins=["*"] cho phép mọi trang web user đang mở gọi API (xóa
    # project, đọc file). Thêm origin khác qua ZM_AI_TOOL_ALLOW_ORIGINS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def bind_locale(request, call_next):
        from api.i18n import reset_request_locale, set_request_locale

        token = set_request_locale(request)
        try:
            return await call_next(request)
        finally:
            reset_request_locale(token)

    @app.middleware("http")
    async def require_license(request, call_next):
        path = request.url.path
        # ponytail: chỉ gate /api/* — static files (/, .js, .css) phải qua để FE tải
        # và tự hiện LicensePage. Không gate → webview chỉ thấy 403 JSON.
        if not path.startswith("/api/"):
            return await call_next(request)
        setup_paths = ("/api/license", "/api/system", "/api/config", "/api/hardware")
        if path == "/api/health" or path.startswith(setup_paths):
            return await call_next(request)
        from pipeline.core.license import license_cached_valid

        if not await anyio.to_thread.run_sync(license_cached_valid):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cần kích hoạt key ZM Tool để sử dụng ứng dụng"},
            )
        return await call_next(request)

    @app.get("/api/health")
    def api_health() -> dict[str, object]:
        """Cheap readiness — dev.mjs / desktop launcher; no torch / model load."""
        import os

        from pipeline.core.config import DATA

        port = int(os.environ.get("ZM_AI_TOOL_PORT") or 8787)
        return {"ok": True, "app": "zm_ai_tool", "port": port, "data": str(DATA)}

    app.include_router(router)
    # StaticFiles kiểm thư mục tồn tại lúc mount → máy mới (chưa có public/)
    # sẽ sập ngay khi khởi động. ensure_data_dirs chạy ở lifespan là quá muộn.
    try:
        from pipeline.core.config import ensure_data_dirs

        ensure_data_dirs()
    except OSError:
        pass
    app.mount("/data", StaticFiles(directory=str(PUBLIC_DATA)), name="public-data")

    return app
