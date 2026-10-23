"""Run: PYTHONPATH=backend backend/.venv/bin/python -m pytest -q tests/test_web_accounts.py"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.testclient import TestClient

from web import (SESSION_COOKIE, STATE_COOKIE, WebAuth, check_web_paths,
                 create_web_app, protect_worker, validate_paths)

ORIGIN = "http://localhost:5173"


@pytest.mark.parametrize("custom", [False, True])
def test_workers_share_browser_binaries_with_private_caches(tmp_path, monkeypatch, custom):
    from web import Workers
    from pipeline.flow.browser import BrowserManager, chrome_executable

    monkeypatch.setattr(sys, "platform", "linux")
    cache = tmp_path / "server-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    for key, folder in (("PUPPETEER_CACHE_DIR", "puppeteer"), ("PLAYWRIGHT_BROWSERS_PATH", "ms-playwright")):
        monkeypatch.setenv(key, str(tmp_path / folder) if custom else "")
    workers = Workers(tmp_path / "web")
    binary = Path(workers.browser_cache_env["PUPPETEER_CACHE_DIR"]) / "chrome/linux-147/chrome-linux64/chrome"
    binary.parent.mkdir(parents=True)
    binary.touch()
    binary.chmod(0o755)
    monkeypatch.setattr("pipeline.flow.browser.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda path: path == binary)
    profiles = []
    for user in ("a", "b"):
        root = workers.root / "users" / user
        monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
        for key, value in workers.browser_cache_env.items():
            monkeypatch.setenv(key, value)
        assert chrome_executable() == binary
        profiles.append(BrowserManager(profile_dir=root / "flow").profile_dir)
    assert profiles[0] != profiles[1]


@pytest.fixture
def web_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ZM_AI_TOOL_WEB_ORIGIN", ORIGIN)
    monkeypatch.setenv("ZM_AI_TOOL_OWNER_EMAILS", "")
    monkeypatch.setenv("ZM_AI_TOOL_RESOURCE_DATA", str(tmp_path / "shared-resources"))
    monkeypatch.delenv("ZM_AI_TOOL_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("ZM_AI_TOOL_GOOGLE_CLIENT_SECRET", raising=False)
    return create_web_app(tmp_path / "web")


def signed_in(app, sub="user-a"):
    auth = app.state.web_auth
    token = auth.create_session({"sub": sub, "email": sub + "@example.com", "name": sub, "email_verified": True})
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(SESSION_COOKIE, token)
    user = client.get("/api/auth/session").json()["user"]
    with auth.connect(write=True) as db:
        db.execute("UPDATE users SET starts_at=1,lifetime=1 WHERE id=?", (user["id"],))
    user = client.get("/api/auth/session").json()["user"]
    client.headers.update({"Origin": ORIGIN, "X-ZM-Account": user["id"]})
    return client, user, token


@pytest.mark.parametrize("cutoff", [0, 12])
@pytest.mark.parametrize("kind", ["image", "video"])
def test_proxy_handles_client_disconnect_without_replaying_request(web_app, monkeypatch, caplog, cutoff, kind):
    client, user, token = signed_in(web_app)
    client.close()
    body = json.dumps({"prompts": ["synthetic-private-prompt"], "kind": kind,
                       "mode": "text", "accountId": "flow-account"}).encode()
    delivered = []

    async def worker_request(request):
        delivered.append(request.content)
        result = json.dumps({"jobs": [{"id": "job-" + kind, "kind": kind}]}).encode()
        return httpx.Response(200, stream=httpx.ByteStream(result))

    async def exercise():
        async with httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(worker_request)) as upstream:
            monkeypatch.setattr(web_app.state.web_workers, "get", AsyncMock(return_value=SimpleNamespace(client=upstream)))
            headers = {"host": "localhost:5173", "origin": ORIGIN, "x-zm-account": user["id"],
                       "cookie": f"{SESSION_COOKIE}={token}", "content-type": "application/json",
                       "content-length": str(len(body))}
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                     "http_version": "1.1", "method": "POST", "scheme": "http",
                     "path": "/api/flow/jobs", "raw_path": b"/api/flow/jobs",
                     "root_path": "", "query_string": b"",
                     "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
                     "client": ("127.0.0.1", 1234), "server": ("localhost", 5173)}
            messages = [{"type": "http.request", "body": body[:cutoff], "more_body": True}] if cutoff else []
            messages.append({"type": "http.disconnect"})
            sent = []

            async def receive():
                return messages.pop(0) if messages else {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            await web_app(scope, receive, send)
            assert next(message["status"] for message in sent if message["type"] == "http.response.start") == 499
            assert delivered == []
            # The shared client remains usable after an aborted request body.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web_app), base_url=ORIGIN) as browser:
                response = await browser.post("/api/flow/jobs", content=body, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"jobs": [{"id": "job-" + kind, "kind": kind}]}
            assert delivered == [body]

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(exercise())
    assert "event=web_client_disconnected method=POST path='/api/flow/jobs'" in caplog.text
    assert "synthetic-private-prompt" not in caplog.text
    assert token not in caplog.text


def test_frontend_html_is_not_cached_across_deployments(tmp_path, monkeypatch):
    import web

    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Test</title>")
    monkeypatch.setattr(web, "BACKEND", tmp_path / "backend")
    app = create_web_app(tmp_path / "web")
    with TestClient(app) as client:
        for path in ("/", "/index.html", "/text-to-speech", "/?landing=1"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"


def test_no_google_config_fails_closed_and_private_routes_require_session(web_app):
    with TestClient(web_app) as client:
        assert client.get("/api/health").json()["mode"] == "web"
        assert client.get("/api/auth/session").json() == {"mode": "web", "configured": False, "user": None}
        assert client.get("/api/auth/google").status_code == 503
        for route in ("/api/config", "/api/queue/jobs", "/data/example.txt", "/api/events"):
            assert client.get(route).status_code == 401


def test_public_downloads_reuse_release_assets_without_starting_a_worker(web_app, monkeypatch):
    from api.routes import system

    repository = system._UPDATE_REPOSITORY
    names = {
        "windows": "ZM_AI_TOOL_v1.2.3-windows-x64-Setup.exe",
        "macos-arm64": "ZM_AI_TOOL_v1.2.3-macos-arm64.pkg",
    }
    published = {
        key: {"name": name, "url": f"https://github.com/{repository}/releases/download/v1.2.3/{name}"}
        for key, name in names.items()
    }
    release = {"tag_name": "v1.2.3", "_repository": repository, "assets": [
        {"name": item["name"], "browser_download_url": item["url"]} for item in published.values()
    ]}
    # An asset outside the release repository must never become a download link.
    release["assets"].append({"name": "ZM_AI_TOOL_v1.2.3-macos-x86_64.pkg", "browser_download_url": "https://other.example/file.pkg"})
    monkeypatch.setattr(system, "_DOWNLOAD_RELEASE_CACHE", {"release": None, "expires": 0.0})
    with patch.object(system, "_latest_release", return_value=release) as lookup, TestClient(web_app) as client:
        for _ in range(2):
            result = client.get("/api/system/downloads")
            assert result.status_code == 200
            assert result.json() == {"version": "1.2.3", "prerelease": False, "downloads": published}
        lookup.assert_called_once()
        for route in ("/api/config", "/api/queue/jobs", "/api/system/update/check", "/data/example.txt"):
            assert client.get(route).status_code == 401
        with patch.object(system, "_download_release", side_effect=OSError("upstream unavailable")):
            failed = client.get("/api/system/downloads")
            assert failed.status_code == 502
            assert failed.json() == {"detail": "DOWNLOAD_RELEASE_UNAVAILABLE"}
        assert web_app.state.web_workers.items == {}
        assert not (web_app.state.web_auth.root / "users").exists()


def test_web_configuration_validates_origin_credentials_and_private_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_SECRET", "")
    monkeypatch.delenv("ZM_AI_TOOL_WEB_ORIGIN", raising=False)
    monkeypatch.setenv("ZM_AI_TOOL_WEB_MAX_WORKERS", "2")
    app = create_web_app(tmp_path / "valid")
    assert WebAuth.origin() == "http://localhost:5173"
    assert app.state.web_workers.limit == 2
    for origin in ("http://public.example", "https://example.com/path", "http://[", "https://example.com:bad"):
        with monkeypatch.context() as m:
            m.setenv("ZM_AI_TOOL_WEB_ORIGIN", origin)
            with pytest.raises(ValueError):
                create_web_app(tmp_path / "invalid")
    with monkeypatch.context() as m:
        m.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_ID", "only-one-value")
        with pytest.raises(ValueError, match="both"):
            create_web_app(tmp_path / "invalid")
    with monkeypatch.context() as m:
        m.setenv("ZM_AI_TOOL_WEB_MAX_WORKERS", "0")
        with pytest.raises(ValueError, match="MAX_WORKERS"):
            create_web_app(tmp_path / "invalid")
    import web
    monkeypatch.setattr(web, "BACKEND", tmp_path / "backend")
    monkeypatch.setenv("ZM_AI_TOOL_WEB_DATA", "private-storage")
    monkeypatch.chdir(tmp_path / "valid")
    assert create_web_app().state.web_auth.root == tmp_path / "private-storage"
    with pytest.raises(ValueError, match="outside"):
        create_web_app(tmp_path / "frontend/dist/private")


def test_stable_google_subject_and_revocable_sessions(web_app):
    client, user, token = signed_in(web_app)
    auth = web_app.state.web_auth
    second = auth.create_session({"sub": "user-a", "email": "renamed@example.com", "name": "New name", "email_verified": True})
    assert second != token
    assert client.get("/api/auth/session").json()["user"] == {**user, "email": "renamed@example.com"}
    assert client.post("/api/auth/logout", headers={"Origin": "https://other.example"}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/session").json()["user"] is None
    assert client.get("/api/config").status_code == 401
    with auth.connect() as db:
        assert db.execute("SELECT hash FROM sessions WHERE hash=?", (token,)).fetchone() is None
        db.execute("UPDATE sessions SET expires=0")
    client.cookies.set(SESSION_COOKIE, second)
    assert client.get("/api/auth/session").json()["user"] is None
    with pytest.raises(ValueError):
        auth.create_session({"sub": "user-b", "email_verified": False})


def test_google_state_pkce_nonce_callback_and_replay(web_app, monkeypatch):
    monkeypatch.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_SECRET", "test-secret")
    auth = web_app.state.web_auth
    with TestClient(web_app, base_url=ORIGIN, follow_redirects=False) as client:
        response = client.get("/api/auth/google?return_to=//outside.example")
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["scope"] == ["openid email profile"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [ORIGIN + "/api/auth/google/callback"]
        assert "httponly" in response.headers["set-cookie"].lower()
        state_cookie = client.cookies.get(STATE_COOKIE)
        with auth.connect() as db:
            pending = dict(db.execute("SELECT * FROM logins").fetchone())
        assert pending["return_to"] == "/video-clone"
        assert pending["hash"] != state_cookie

        original_client = httpx.AsyncClient
        requests = []

        def token_endpoint(request):
            requests.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"id_token": "provider-token"})

        def token_client(**kwargs):
            return original_client(**kwargs, transport=httpx.MockTransport(token_endpoint))

        identity = {"sub": "google-a", "email": "a@example.com", "name": "A", "email_verified": True, "nonce": pending["nonce"]}
        with patch("web.httpx.AsyncClient", token_client), patch.object(auth, "verify_identity", return_value=identity):
            result = client.get("/api/auth/google/callback", params={"state": query["state"][0], "code": "code-a"})
        assert result.headers["location"] == "/video-clone"
        assert requests[0]["code_verifier"] == [pending["verifier"]]
        assert client.get("/api/auth/session").json()["user"]["email"] == "a@example.com"
        client.cookies.set(STATE_COOKIE, state_cookie)
        replay = client.get("/api/auth/google/callback", params={"state": query["state"][0], "code": "code-a"})
        assert replay.headers["location"] == "/login?error=google_login_failed"
        # Provider tokens/secrets are never persisted in the account DB.
        assert b"provider-token" not in auth.path.read_bytes()
        assert b"test-secret" not in auth.path.read_bytes()


def test_bad_google_state_cannot_create_a_session(web_app, monkeypatch):
    monkeypatch.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("ZM_AI_TOOL_GOOGLE_CLIENT_SECRET", "test-secret")
    with TestClient(web_app, base_url=ORIGIN, follow_redirects=False) as client:
        client.get("/api/auth/google")
        with patch.object(web_app.state.web_auth, "verify_identity") as verifier:
            result = client.get("/api/auth/google/callback?state=wrong&code=code-a")
            assert result.headers["location"] == "/login?error=google_login_failed"
            verifier.assert_not_called()
        assert SESSION_COOKIE not in client.cookies


def test_worker_boundary_checks_json_query_and_multipart_without_losing_uploads(tmp_path):
    raw = FastAPI(dependencies=[Depends(check_web_paths)])

    @raw.post("/api/example")
    async def example(request: Request):
        return await request.json()

    @raw.post("/api/upload")
    async def upload(source: str = Form(...), file: UploadFile = File(...)):
        return {"source": source, "content": (await file.read()).decode()}

    @raw.get("/api/example")
    async def query(source: str):
        return {"source": source}

    app = protect_worker(raw, tmp_path, "private-secret")
    with TestClient(app) as client:
        assert client.get("/api/example?source=ok").status_code == 403
        client.headers["x-zm-worker"] = "private-secret"
        assert client.post("/api/example", json={"nested": {"path": "/etc/passwd"}}).status_code == 403
        assert client.post("/api/example", json={"prompt": "/imagine a forest", "segments": [{"source": "/Hello"}]}).status_code == 200
        assert client.post("/api/example", json={"assetUrl": "/data/project-a/assets/logo.png"}).status_code == 200
        assert client.post("/api/example", json={"assetUrl": "/data/%2e%2e/other/file"}).status_code == 403
        assert client.post("/api/example", content='{"path":"/etc/passwd"}', headers={"Content-Type": "application/merge-patch+json"}).status_code == 403
        assert client.post("/api/example", content='{"path":"/etc/passwd"}').status_code == 403
        assert client.get("/api/example", params={"source": str(tmp_path.parent / "other" / "video.mp4")}).status_code == 403
        files = {"file": ("video.txt", b"upload retained", "text/plain")}
        assert client.post("/api/upload", data={"source": json.dumps({"path": "/etc/passwd"})}, files=files).status_code == 403
        result = client.post("/api/upload", data={"source": str(tmp_path / "video.mp4")}, files=files)
        assert result.json()["content"] == "upload retained"
        assert client.post("/api/system/restart").status_code == 403

    link = tmp_path / "escape"
    link.symlink_to(tmp_path.parent, target_is_directory=True)
    for value in ("../other/file.mp4", "file:///etc/passwd", str(link / "other"), "C:\\Users\\Other\\private.mp4"):
        with pytest.raises(Exception) as exc:
            validate_paths(value, tmp_path)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("failure", [False, True])
def test_web_moss_install_can_be_polled_without_allowing_machine_maintenance(tmp_path, monkeypatch, failure):
    from api.routes import system
    from pipeline.core import config, nvidia_driver
    from pipeline.tts.engines import moss_tts_nano

    started, finish, done = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "RESOURCE_DATA", tmp_path / "shared-resources")
    monkeypatch.setenv("ZM_AI_TOOL_WEB_WORKER", "1")
    for key in ("MOSS_TTS_NANO_RUNTIME_DIR", "MOSS_TTS_NANO_MODEL_DIR", "MOSS_TTS_NANO_VOICES_DIR", "MOSS_TTS_NANO_PYTHON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(system, "_install_state", {**system._install_state, "running": False})
    monkeypatch.setattr(system, "_install_cancel", threading.Event())
    monkeypatch.setattr(system, "_reset_install_log", lambda kind: None)
    monkeypatch.setattr(system, "_append_install_log", lambda message: None)
    monkeypatch.setattr(system, "_start_checks_warm", done.set)
    monkeypatch.setattr(nvidia_driver, "assert_available", lambda: None)

    def install(*, progress):
        source, model, refs = moss_tts_nano._paths()
        assert source.is_relative_to(config.RESOURCE_DATA)
        assert model.is_relative_to(config.RESOURCE_DATA)
        assert moss_tts_nano._python().is_relative_to(config.RESOURCE_DATA)
        assert refs.is_relative_to(config.DATA)
        progress(50, "Downloading MOSS models")
        started.set()
        assert finish.wait(5)
        if failure:
            raise RuntimeError("MOSS download failed")
        return {"message": "MOSS installed", "needsRestart": False}

    monkeypatch.setattr(moss_tts_nano, "install", install)
    app = FastAPI(dependencies=[Depends(check_web_paths)])
    app.include_router(system.router)
    protect_worker(app, tmp_path, "private-secret")
    with TestClient(app) as client:
        assert client.get("/api/system/install/status").status_code == 403
        assert client.post("/api/resources/moss_tts_nano/install").status_code == 403
        client.headers["x-zm-worker"] = "private-secret"
        try:
            response = client.post("/api/resources/moss_tts_nano/install")
            assert response.status_code == 200
            assert response.json()["running"] is True
            assert started.wait(5)
            status = client.get("/api/system/install/status")
            assert status.status_code == 200
            assert status.json()["kind"] == "moss_tts_nano_runtime"
            assert status.json()["running"] is True
            assert status.json()["progress"] == 50
        finally:
            finish.set()
            assert done.wait(5)
        status = client.get("/api/system/install/status").json()
        assert status["running"] is False
        assert bool(status.get("error")) is failure
        if failure:
            assert status["error"] == "MOSS download failed"
        else:
            assert status["progress"] == 100
            assert status["needsRestart"] is False
        assert client.post("/api/system/install/cancel").status_code == 200
        for route in ("/api/system/restart", "/api/system/install/nvidia_driver", "/api/system/install/cpu_runtime"):
            assert client.post(route).status_code == 403
        assert client.post("/api/system/install/status").status_code == 403
        assert client.get("/api/system/install/cancel").status_code == 403


def test_two_real_workers_keep_files_preferences_and_job_stores_separate(web_app):
    # A real private API process per user exercises imports, persistent stores,
    # proxy streaming and per-user config without contacting Google or running AI.
    with TestClient(web_app) as supervisor:
        auth = web_app.state.web_auth
        accounts = []
        for sub in ("user-a", "user-b"):
            token = auth.create_session({"sub": sub, "email": sub + "@example.com", "email_verified": True})
            uid = auth.digest("google:" + sub)
            with auth.connect(write=True) as db:
                db.execute("UPDATE users SET starts_at=1,lifetime=1 WHERE id=?", (uid,))
            accounts.append(({"id": uid}, token))
        (a, token_a), (b, token_b) = accounts

        def select(user, token):
            supervisor.cookies.set(SESSION_COOKIE, token)
            supervisor.headers.update({"Origin": ORIGIN, "X-ZM-Account": user["id"]})

        uploaded_sources = {}
        for user, token, marker in ((a, token_a, "A"), (b, token_b, "B")):
            select(user, token)
            root = web_app.state.web_auth.root / "users" / user["id"]
            (root / "public").mkdir(parents=True)
            (root / "public" / "result.txt").write_text(marker)
            exports = root / "public" / "exports"
            exports.mkdir()
            (exports / "result.txt").write_text(marker)
            (root / "data" / "queue").mkdir(parents=True)
            (root / "data" / "queue" / "jobs.json").write_text(json.dumps([{"id": "same-job", "marker": marker}]))
            response = supervisor.get("/data/result.txt")
            assert response.status_code == 200, response.text
            assert response.text == marker
            partial = supervisor.get("/data/result.txt", headers={"Range": "bytes=0-0"})
            assert partial.status_code == 206
            assert partial.text == marker
            config = supervisor.get("/api/config")
            assert config.status_code == 200, config.text
            assert config.json()["desktop"] is False
            assert str(root / "outputs") == config.json()["desktopOutputRoot"]
            resources = supervisor.get("/api/resources")
            assert resources.status_code == 200, resources.text
            moss = next(item for item in resources.json()["items"] if item["id"] == "moss_tts_nano")
            shared = web_app.state.web_workers.resource_data
            assert moss["detail"] == str(shared / "moss_tts_nano" / "models")
            assert supervisor.put("/api/ui-preferences", json={"outputRoot": str(shared)}).status_code == 403
            assert supervisor.put("/api/ui-preferences", json={"locale": "vi" if marker == "A" else "en"}).status_code == 200
            assert supervisor.get("/api/ui-preferences").json()["locale"] == ("vi" if marker == "A" else "en")
            inventory = supervisor.get("/api/system/cleanup")
            assert inventory.status_code == 200, inventory.text
            assert inventory.json()["targets"]["public_exports"] == {
                "bytes": 1, "items": 1, "path": str(exports), "selectable": True,
            }
            outside = supervisor.put("/api/ui-preferences", json={"outputRoot": str(root.parent / "other-account")})
            assert outside.status_code == 403, outside.text
            assert outside.json()["detail"] == "WEB_PATH_OUTSIDE_ACCOUNT"
            video = marker.encode() * (2 * 1024 * 1024)
            uploaded = supervisor.post("/api/queue/upload", files={"file": ("../Phim thử.MP4", video, "video/mp4")})
            assert uploaded.status_code == 200, uploaded.text
            source = Path(uploaded.json()["path"])
            assert source.is_relative_to(root / "data" / "queue_inputs")
            assert source.name == "Phim thử.mp4"
            assert source.read_bytes() == video
            uploaded_sources[user["id"]] = str(source)
            for kind in ("clone", "review"):
                staged = supervisor.post("/api/queue", json={"type": kind, "sources": [str(source)], "start_now": False})
                assert staged.status_code == 200, staged.text
                job = staged.json()["jobs"][0]
                assert job["source"] == str(source)
                assert job["type"] == kind and job["status"] == "paused"
        select(a, token_a)
        assert supervisor.post("/api/queue", json={"sources": [uploaded_sources[b["id"]]], "start_now": False}).status_code == 403
        duplicate = supervisor.post("/api/queue/upload", files={"file": ("Phim thử.MP4", b"new source", "video/mp4")})
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["path"] != uploaded_sources[a["id"]]
        assert Path(uploaded_sources[a["id"]]).stat().st_size == 2 * 1024 * 1024
        input_root = Path(uploaded_sources[a["id"]]).parent.parent
        before = set(input_root.iterdir())
        for name, content, code in (("unsafe.txt", b"x", "VIDEO_UPLOAD_UNSUPPORTED"), ("empty.mp4", b"", "VIDEO_UPLOAD_EMPTY")):
            rejected = supervisor.post("/api/queue/upload", files={"file": (name, content)})
            assert rejected.status_code in {400, 422}
            assert rejected.json()["detail"] == code
        assert set(input_root.iterdir()) == before
        assert supervisor.post("/api/system/pick-videos").status_code == 403
        assert supervisor.post("/api/system/pick-folder").status_code == 403
        cleaned = supervisor.post("/api/system/cleanup", json={"targets": ["public_exports"]})
        assert cleaned.status_code == 200, cleaned.text
        assert cleaned.json()["ok"] is True
        assert cleaned.json()["deleted"] == ["public_exports"]
        assert cleaned.json()["targets"]["public_exports"]["bytes"] == 0
        roots = web_app.state.web_auth.root / "users"
        assert list((roots / a["id"] / "public" / "exports").iterdir()) == []
        assert (roots / b["id"] / "public" / "exports" / "result.txt").read_text() == "B"
        assert supervisor.post("/api/system/cleanup", json={"targets": ["data_protected:ui_preferences.json"]}).status_code == 400
        assert supervisor.delete("/api/system/cleanup").status_code == 403
        assert supervisor.get("/data/result.txt").text == "A"
        assert supervisor.get("/api/ui-preferences").json()["locale"] == "vi"
        assert supervisor.put("/api/ui-preferences", headers={"X-ZM-Account": b["id"]}, json={"locale": "en"}).status_code == 401
        assert supervisor.get("/api/events", params={"web_account": b["id"]}, headers={"X-ZM-Account": ""}).status_code == 401
        assert len(web_app.state.web_workers.items) == 2
    assert not web_app.state.web_workers.items


def test_queue_flow_jobs_and_events_use_the_account_workspace(tmp_path):
    backend = Path(__file__).resolve().parents[1] / "backend"
    code = """
import json, sys
from pipeline.queue import store as queue
from pipeline.flow import store as flow
from pipeline.core.realtime import realtime
marker, action = sys.argv[1:]
if action == 'write':
    queue.insert({'id': 'same-job', 'marker': marker, 'status': 'queued'})
    flow.put_row('jobs', {'id': 'same-job', 'marker': marker, 'status': 'queued'})
else:
    queue.mutate('same-job', {'status': 'cancelled'})
event = realtime.publish('test', 'job.updated', 'same-job', {'marker': marker})
print(json.dumps({'queue': queue.load_all(), 'flow': flow.list_rows('jobs'), 'event': event['payload']}))
"""

    def run(marker, action):
        root = tmp_path / marker
        root.mkdir(exist_ok=True)
        env = {**os.environ, "PYTHONPATH": str(backend), "ZM_AI_TOOL_DATA": str(root / "data"),
               "ZM_AI_TOOL_PUBLIC_DATA": str(root / "public"), "ZM_AI_TOOL_OUTPUT_ROOT": str(root / "outputs")}
        result = subprocess.run([sys.executable, "-c", code, marker, action], env=env, cwd=root,
                                capture_output=True, text=True, check=True, timeout=15)
        return json.loads(result.stdout)

    for marker in ("a", "b"):
        snapshot = run(marker, "write")
        assert [row["marker"] for row in snapshot["queue"]] == [marker]
        assert [row["marker"] for row in snapshot["flow"]] == [marker]
        assert snapshot["event"] == {"marker": marker}
    assert run("a", "cancel")["queue"][0]["status"] == "cancelled"
    assert json.loads((tmp_path / "b/data/queue/jobs.json").read_text())[0]["status"] == "queued"


def test_entry_selects_google_web_and_local_desktop(tmp_path):
    backend = Path(__file__).resolve().parents[1] / "backend"
    code = """
from fastapi.testclient import TestClient
from main import app
client = TestClient(app)
print(client.get('/api/auth/session').json()['mode'])
"""
    for desktop in ("0", "1"):
        env = {**os.environ, "PYTHONPATH": str(backend), "ZM_AI_TOOL_DESKTOP": desktop,
               "ZM_AI_TOOL_WEB_DATA": str(tmp_path / "web"), "ZM_AI_TOOL_DATA": str(tmp_path / "data"),
               "ZM_AI_TOOL_PUBLIC_DATA": str(tmp_path / "public"), "ZM_AI_TOOL_OUTPUT_ROOT": str(tmp_path / "outputs")}
        result = subprocess.run([sys.executable, "-c", code], env=env, cwd=backend,
                                capture_output=True, text=True, check=True, timeout=15)
        assert result.stdout.strip() == ("desktop" if desktop == "1" else "web")
