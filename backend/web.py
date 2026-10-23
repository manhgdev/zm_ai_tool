"""Google-authenticated web gateway with one private worker per account."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlencode, urlsplit

import anyio
import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.staticfiles import StaticFiles
from accounts import Accounts, access, license_status, management_routes

BACKEND = Path(__file__).resolve().parent
HOP_HEADERS = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "cookie", "x-zm-worker", "x-zm-account"}


SESSION_COOKIE = "zm_web_session"
STATE_COOKIE = "zm_web_login"
SESSION_SECONDS = 7 * 24 * 3600


class WebAuth(Accounts):
    @staticmethod
    def origin() -> str:
        raw = os.environ.get("ZM_AI_TOOL_WEB_ORIGIN", "").rstrip("/")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError:
            return ""
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or (port is not None and not 1 <= port <= 65535)
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            return ""
        return raw

    def configured(self) -> bool:
        return bool(self.origin() and os.environ.get("ZM_AI_TOOL_GOOGLE_CLIENT_ID")
                    and os.environ.get("ZM_AI_TOOL_GOOGLE_CLIENT_SECRET"))

    def cookie(self, response, name: str, value: str, seconds: int):
        response.set_cookie(name, value, max_age=seconds, httponly=True,
                            secure=self.origin().startswith("https://"), samesite="lax", path="/")

    def user(self, request: Request) -> dict | None:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return None
        with self.connect() as db:
            row = db.execute("""SELECT users.* FROM sessions JOIN users
                ON users.id=sessions.user_id WHERE sessions.hash=? AND sessions.expires>? AND users.deleted=0""",
                (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def create_session(self, identity: dict) -> str:
        user_id = self.login_identity(identity)
        token = secrets.token_urlsafe(32)
        with self.connect(write=True) as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (self.digest(token), user_id, time.time() + SESSION_SECONDS))
        return token

    def routes(self) -> APIRouter:
        router = APIRouter(prefix="/api/auth")

        @router.get("/session")
        def session(request: Request):
            user = self.user(request)
            return JSONResponse({"mode": "web", "configured": self.configured(), "user": self.public(user) if user else None},
                                headers={"Cache-Control": "no-store"})

        @router.get("/google")
        def login(return_to: str = "/video-clone"):
            if not self.configured():
                raise HTTPException(503, detail="GOOGLE_AUTH_NOT_CONFIGURED")
            # Only an app-relative path is accepted, never a protocol-relative URL.
            if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to or any(ord(c) < 32 for c in return_to):
                return_to = "/video-clone"
            browser_token, state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
            with self.connect() as db:
                db.execute("DELETE FROM logins WHERE expires<=?", (time.time(),))
                db.execute("INSERT INTO logins VALUES (?, ?, ?, ?, ?, ?)",
                           (self.digest(browser_token), state, nonce, verifier, return_to, time.time() + 600))
            import base64
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
                "client_id": os.environ["ZM_AI_TOOL_GOOGLE_CLIENT_ID"],
                "redirect_uri": self.origin() + "/api/auth/google/callback",
                "response_type": "code", "scope": "openid email profile", "state": state,
                "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
                "prompt": "select_account",
            }), status_code=303)
            self.cookie(response, STATE_COOKIE, browser_token, 600)
            return response

        @router.get("/google/callback")
        async def callback(request: Request, state: str = "", code: str = ""):
            browser_token = request.cookies.get(STATE_COOKIE, "")
            with self.connect() as db:
                # Consume once, even if the provider returns an error.
                row = db.execute("DELETE FROM logins WHERE hash=? RETURNING *",
                                 (self.digest(browser_token),)).fetchone()
            if not self.configured() or not row or row["expires"] <= time.time() or not secrets.compare_digest(state, row["state"]) or not code:
                response = RedirectResponse("/login?error=google_login_failed", status_code=303)
            else:
                try:
                    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                        result = await client.post("https://oauth2.googleapis.com/token", data={
                            "client_id": os.environ["ZM_AI_TOOL_GOOGLE_CLIENT_ID"],
                            "client_secret": os.environ["ZM_AI_TOOL_GOOGLE_CLIENT_SECRET"],
                            "code": code, "grant_type": "authorization_code", "code_verifier": row["verifier"],
                            "redirect_uri": self.origin() + "/api/auth/google/callback",
                        })
                        result.raise_for_status()
                        encoded_token = result.json()["id_token"]
                    identity = await anyio.to_thread.run_sync(self.verify_identity, encoded_token)
                    if not secrets.compare_digest(str(identity.get("nonce") or ""), row["nonce"]):
                        raise ValueError("Invalid nonce")
                    token = self.create_session(identity)
                    response = RedirectResponse(row["return_to"], status_code=303)
                    self.cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
                except Exception:
                    response = RedirectResponse("/login?error=google_login_failed", status_code=303)
            response.delete_cookie(STATE_COOKIE, path="/")
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @router.post("/logout")
        def logout(request: Request):
            with self.connect() as db:
                db.execute("DELETE FROM sessions WHERE hash=?",
                           (self.digest(request.cookies.get(SESSION_COOKIE, "")),))
            response = JSONResponse({"ok": True})
            response.delete_cookie(SESSION_COOKIE, path="/")
            response.headers["Cache-Control"] = "no-store"
            return response

        return router

    @staticmethod
    def verify_identity(encoded_token: str) -> dict:
        # Use Google's verifier for signature, issuer, audience and expiry checks.
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.id_token import verify_oauth2_token
        from functools import partial
        return verify_oauth2_token(encoded_token, partial(GoogleRequest(), timeout=15), os.environ["ZM_AI_TOOL_GOOGLE_CLIENT_ID"])


_TEXT_FIELDS = {
    "text", "prompt", "negativePrompt", "systemPrompt", "instruction", "content",
    "translation", "sourceSubtitle", "dubSubtitle", "srtText", "srt_text", "script",
    "timelineText", "manual_text", "title", "description", "name", "label", "email", "cookie", "cookies",
    "apiKey", "apiKeys", "keys", "password", "model", "message",
}


def validate_paths(value, root: Path, field: str = "") -> None:
    """Local paths supplied by a browser must stay in its own workspace."""
    if field in _TEXT_FIELDS:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source" and field in {"segments", "cues"}:
                continue  # Subtitle source is text; Review/queue source is a file.
            validate_paths(item, root, str(key))
    elif isinstance(value, (tuple, list)):
        for item in value:
            validate_paths(item, root, field)
    elif isinstance(value, str):
        raw = value.strip()
        if field.lower().endswith("url") and raw.startswith(("/api/", "/data/")):
            virtual_path = unquote(urlsplit(raw).path)
            if "\\" in virtual_path or any(part in {".", ".."} for part in virtual_path.split("/")):
                raise HTTPException(403, "WEB_PATH_OUTSIDE_ACCOUNT")
            return
        if raw.startswith(("{", "[")):
            try:
                validate_paths(json.loads(raw), root, field)
            except json.JSONDecodeError:
                pass
        if raw.lower().startswith("file:"):
            raise HTTPException(403, "WEB_PATH_OUTSIDE_ACCOUNT")
        if raw.startswith(("/", "~", ".", "\\")) or PureWindowsPath(raw).is_absolute():
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = root / path
            if PureWindowsPath(raw).is_absolute() and os.name != "nt":
                raise HTTPException(403, "WEB_PATH_OUTSIDE_ACCOUNT")
            try:
                path.resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                raise HTTPException(403, "WEB_PATH_OUTSIDE_ACCOUNT") from None
        elif any(part == ".." for part in raw.replace("\\", "/").split("/")):
            raise HTTPException(403, "WEB_PATH_OUTSIDE_ACCOUNT")


def browser_route_allowed(path: str, method: str) -> bool:
    # Cleanup is scoped to this worker's account directories.
    if path == "/api/system/cleanup":
        return method in {"GET", "POST"}
    # Resource installs already run in this account's worker. Let the shared
    # installer UI observe/cancel them without exposing machine maintenance.
    if path == "/api/system/install/status":
        return method == "GET"
    if path == "/api/system/install/cancel":
        return method == "POST"
    if path in {
        "/api/system/install/ai_runtime", "/api/system/install/ocr_cuda",
        "/api/system/install/demucs_cuda", "/api/system/install/nvm",
    }:
        return method == "POST"
    # Native dialogs and machine-wide maintenance belong to the desktop/admin.
    if path.startswith("/api/system/"):
        return (method == "GET" and path in {
            "/api/system/checks", "/api/system/info", "/api/system/setup-gate", "/api/system/downloads",
        }) or (method == "POST" and path == "/api/system/resolve-output-folder")
    return not (any(segment in {"reveal", "open", "open-folder", "open-output", "reveal-output", "open-output-folder", "open-session"} for segment in path.split("/"))
                or path == "/api/hardware/mode")


async def check_web_paths(request: Request):
    # A global FastAPI dependency covers included domain routers too, and uses
    # the same parsed JSON/form cache as the endpoint (including file uploads).
    root = request.app.state.web_workspace
    validate_paths(dict(request.query_params), root)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json") or (not content_type and await request.body()):
        try:
            field = "segments" if "/segments" in request.url.path else ""
            validate_paths(await request.json(), root, field)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "INVALID_JSON") from None
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        for key, item in form.multi_items():
            if isinstance(item, str):
                validate_paths(item, root, key)


def protect_worker(app, root: Path, secret: str):
    app.state.web_workspace = root

    @app.middleware("http")
    async def private_api(request: Request, call_next):
        supplied = request.headers.get("x-zm-worker", "")
        if not secret or not secrets.compare_digest(supplied, secret):
            return JSONResponse({"detail": "WEB_WORKER_PRIVATE"}, status_code=403)
        if not browser_route_allowed(request.url.path, request.method):
            return JSONResponse({"detail": "DESKTOP_ONLY"}, status_code=403)
        # Reject path separators/traversal before any project/store helper runs.
        if "\\" in request.url.path or any(p in {".", ".."} for p in request.url.path.split("/")):
            return JSONResponse({"detail": "WEB_PATH_OUTSIDE_ACCOUNT"}, status_code=403)
        if request.url.path == "/api/system/setup-gate" and request.method == "GET":
            # The administrator provisions the server; browsers do not install
            # drivers/runtimes on their account's first visit.
            return JSONResponse({"passed": True})
        return await call_next(request)
    return app


def create_worker():
    root = os.environ.get("ZM_AI_TOOL_WEB_WORKSPACE")
    secret = os.environ.get("ZM_AI_TOOL_WEB_WORKER_SECRET")
    if not root or not secret:
        raise RuntimeError("Web workers must be started by the web gateway")
    from api.app import create_app
    return protect_worker(create_app(dependencies=[Depends(check_web_paths)]), Path(root).resolve(), secret)


@dataclass
class Worker:
    process: asyncio.subprocess.Process
    client: httpx.AsyncClient
    secret: str


class Workers:
    def __init__(self, root: Path):
        from pipeline.core.config import DATA

        self.root = root
        self.resource_data = Path(os.environ.get("ZM_AI_TOOL_RESOURCE_DATA") or DATA).resolve()
        # Capture server cache paths before each worker gets a private XDG cache.
        # Share model blobs, not HF_HOME (which can contain login credentials).
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        hf_home = Path(os.environ.get("HF_HOME") or cache / "huggingface")
        self.model_cache_env = {key: str(Path(value).expanduser().resolve()) for key, value in {
            "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or str(hf_home / "hub"),
            "HF_ASSETS_CACHE": os.environ.get("HF_ASSETS_CACHE") or str(hf_home / "assets"),
            "TORCH_HOME": os.environ.get("TORCH_HOME") or str(cache / "torch"),
        }.items()}
        # Browser binaries are server resources; profiles/cookies remain private.
        self.browser_cache_env = {key: str(Path(value).expanduser().resolve()) for key, value in {
            "PUPPETEER_CACHE_DIR": os.environ.get("PUPPETEER_CACHE_DIR") or str(cache / "puppeteer"),
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(cache / "ms-playwright"),
        }.items()}
        self.items: dict[str, Worker] = {}
        self.lock = asyncio.Lock()
        self.limit = int(os.environ.get("ZM_AI_TOOL_WEB_MAX_WORKERS", "8"))
        if self.limit < 1:
            raise ValueError("ZM_AI_TOOL_WEB_MAX_WORKERS must be >= 1")

    async def get(self, user_id: str, grant: dict | None = None) -> Worker:
        async with self.lock:
            worker = self.items.get(user_id)
            if worker and worker.process.returncode is None:
                return worker
            if worker:
                await worker.client.aclose()
                self.items.pop(user_id)
            if len(self.items) >= self.limit:
                raise HTTPException(503, "WEB_WORKER_CAPACITY")
            root = self.root / "users" / user_id
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            (root / "tmp").mkdir(exist_ok=True, mode=0o700)
            secret = secrets.token_urlsafe(32)
            env = dict(os.environ)
            # OAuth credentials stay in the gateway. Account provider keys are
            # entered in that account's configuration, never inherited globally.
            for key in list(env):
                if key.startswith("ZM_AI_TOOL_GOOGLE_") or key in {
                    "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
                    "OPENROUTER_API_KEY", "OPENCODE_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
                    "MISTRAL_API_KEY", "ELEVENLABS_API_KEYS", "ELEVENLABS_API_KEY",
                }:
                    env.pop(key)
            env.update({
                **self.model_cache_env,
                **self.browser_cache_env,
                "PYTHONPATH": str(BACKEND) + os.pathsep + env.get("PYTHONPATH", ""),
                "ZM_AI_TOOL_DESKTOP": "0", "ZM_AI_TOOL_WEB_WORKER": "1",
                "ZM_AI_TOOL_WEB_WORKSPACE": str(root), "ZM_AI_TOOL_WEB_WORKER_SECRET": secret,
                "ZM_AI_TOOL_DATA": str(root / "data"), "ZM_AI_TOOL_PUBLIC_DATA": str(root / "public"),
                "ZM_AI_TOOL_RESOURCE_DATA": str(self.resource_data),
                "ZM_AI_TOOL_OUTPUT_ROOT": str(root / "outputs"),
                "ZM_AI_TOOL_ALLOW_ORIGINS": WebAuth.origin(),
                "ZM_AI_TOOL_WEB_AUTH_DB": str(self.root / "accounts.sqlite3"),
                "ZM_AI_TOOL_WEB_USER_ID": user_id,
                "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
                "TMPDIR": str(root / "tmp"),
            })
            # Bind the socket here and inherit it: no free-port race and no
            # publicly listening worker. Every request also requires a secret.
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            log = (root / "worker.log").open("ab")
            try:
                if os.name == "nt":
                    raise HTTPException(503, "WEB_SERVER_REQUIRES_POSIX")
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "uvicorn", "web:create_worker", "--factory",
                    "--fd", str(listener.fileno()), "--no-access-log",
                    cwd=str(root), env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=log,
                )
            finally:
                listener.close()
                log.close()
            client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                       timeout=httpx.Timeout(60, connect=5),
                                       headers={"x-zm-worker": secret}, limits=httpx.Limits(max_connections=64))
            worker = Worker(process, client, secret)
            self.items[user_id] = worker
            # ponytail: one process per account reuses all existing singleton
            # stores/event buses. Ceiling: one host + MAX_WORKERS resident users;
            # upgrade path: supervised containers/worker scheduling across hosts.
            for _ in range(120):
                if process.returncode is not None:
                    break
                try:
                    response = await client.get("/api/health", timeout=1)
                    if response.is_success:
                        return worker
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            await self.stop(worker)
            self.items.pop(user_id, None)
            raise HTTPException(503, "WEB_WORKER_START_FAILED")

    @staticmethod
    async def stop(worker: Worker):
        if worker.process.returncode is None:
            worker.process.terminate()
            try:
                await asyncio.wait_for(worker.process.wait(), timeout=15)
            except asyncio.TimeoutError:
                worker.process.kill()
                await worker.process.wait()
        await worker.client.aclose()

    async def close(self):
        await asyncio.gather(*(self.stop(w) for w in self.items.values()))
        self.items.clear()


def create_web_app(root: Path | None = None) -> FastAPI:
    os.environ.setdefault("ZM_AI_TOOL_WEB_ORIGIN", "http://localhost:5173")
    if not WebAuth.origin():
        raise ValueError("ZM_AI_TOOL_WEB_ORIGIN requires HTTPS, or HTTP localhost, without a path")
    client_id = os.environ.get("ZM_AI_TOOL_GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ZM_AI_TOOL_GOOGLE_CLIENT_SECRET", "").strip()
    if bool(client_id) != bool(client_secret):
        raise ValueError("Set both Google Client ID and Secret, or leave both empty")
    os.environ.update(ZM_AI_TOOL_GOOGLE_CLIENT_ID=client_id, ZM_AI_TOOL_GOOGLE_CLIENT_SECRET=client_secret)
    root = (root or BACKEND.parent / Path(os.environ.get("ZM_AI_TOOL_WEB_DATA") or "backend/web-data").expanduser()).resolve()
    if root.is_relative_to((BACKEND.parent / "frontend/dist").resolve()):
        raise ValueError("ZM_AI_TOOL_WEB_DATA must be outside frontend/dist")
    auth = WebAuth(root)
    workers = Workers(root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await workers.close()

    app = FastAPI(title="ZM AI TOOL Web", lifespan=lifespan)
    app.state.web_auth, app.state.web_workers = auth, workers

    @app.middleware("http")
    async def web_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path not in {
            '/api/desktop/activate', '/api/desktop/exchange', '/api/desktop/status', '/api/desktop/logout',
        }:
            if not auth.origin() or request.headers.get("origin") != auth.origin():
                return JSONResponse({"detail": "WEB_ORIGIN_REJECTED"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith(("/api/", "/data/")):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.include_router(auth.routes())

    app.include_router(management_routes(auth))

    @app.get('/api/license/status')
    def account_status(request: Request):
        user = auth.user(request)
        if not user:
            raise HTTPException(401, 'WEB_LOGIN_REQUIRED')
        return license_status(user)


    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "zm_ai_tool", "mode": "web", "port": int(os.environ.get("ZM_AI_TOOL_PORT", "8787"))}

    @app.get("/api/system/downloads")
    def downloads():
        from api.routes.system import api_desktop_downloads

        return api_desktop_downloads()

    async def proxy(request: Request):
        user = auth.user(request)
        if not user:
            raise HTTPException(401, "WEB_LOGIN_REQUIRED")
        bound_account = request.headers.get("x-zm-account") or request.query_params.get("web_account")
        if bound_account and bound_account != user["id"]:
            raise HTTPException(401, "WEB_ACCOUNT_CHANGED")
        if request.url.path == "/api/events" and bound_account != user["id"]:
            raise HTTPException(401, "WEB_ACCOUNT_CHANGED")
        if request.method not in {"GET", "HEAD"} and bound_account != user["id"]:
            raise HTTPException(401, "WEB_ACCOUNT_CHANGED")
        if request.url.path.startswith(('/api/internal/', '/api/license/', '/api/desktop/', '/api/admin/', '/api/auth/desktop/')):
            raise HTTPException(403, 'WEB_ACCOUNT_AUTH_ONLY')
        grant = access(user)
        if not grant['valid']:
            raise HTTPException(403, 'ACCESS_' + grant['state'].upper())
        worker = await workers.get(user["id"], grant)
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_HEADERS}
        locale = request.cookies.get("zm_ai_tool_locale")
        if locale in {"vi", "en"}:
            headers["cookie"] = f"zm_ai_tool_locale={locale}"
        # Keep SSE and video/download responses streaming; never buffer uploads.
        upstream = worker.client.build_request(request.method, path, headers=headers, content=request.stream())
        try:
            response = await worker.client.send(upstream, stream=True)
        except ClientDisconnect:
            # The caller aborted while sending its body; never replay a partial request.
            logging.getLogger("uvicorn.error").info(
                "event=web_client_disconnected method=%s path=%r", request.method, request.url.path,
            )
            return Response(status_code=499)
        except httpx.HTTPError:
            raise HTTPException(502, "WEB_WORKER_UNAVAILABLE") from None
        response_headers = {key: value for key, value in response.headers.items()
                            if key.lower() not in HOP_HEADERS | {"set-cookie"}}

        async def stream():
            next_check = 0.0
            try:
                async for chunk in response.aiter_raw():
                    # Revoke long-lived event streams after sign-out/expiry too.
                    if time.monotonic() >= next_check:
                        current = await anyio.to_thread.run_sync(auth.user, request)
                        if not current or not access(current)['valid']:
                            return
                        next_check = time.monotonic() + 15
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(stream(), status_code=response.status_code,
                                 headers=response_headers, background=BackgroundTask(response.aclose))

    app.add_api_route("/api/{path:path}", proxy, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    app.add_api_route("/data/{path:path}", proxy, methods=["GET", "HEAD"])
    dist = BACKEND.parent / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="web-assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate, headers={"Cache-Control": "no-store"} if candidate.suffix == ".html" else None)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})
        raise HTTPException(503, "WEB_FRONTEND_NOT_BUILT")

    return app
