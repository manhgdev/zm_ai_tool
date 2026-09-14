from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from pipeline.core.config import sanitize_httpx_no_proxy


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
SCOPE = "openid profile email offline_access"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
REDIRECT_URI = "http://localhost:1455/auth/callback"
# Legacy constants kept only for one-time Keychain migration (can be removed
# once all existing installs have migrated).
_LEGACY_KEYRING_SERVICE = "ZM AI TOOL ChatGPT"
_LEGACY_KEYRING_USER = "chatgpt-account"


def _jwt_claims(token: str | None) -> dict:
    try:
        raw = (token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, IndexError, TypeError, json.JSONDecodeError):
        return {}


def _account_id(tokens: dict) -> str:
    for token_name in ("id_token", "access_token"):
        claims = _jwt_claims(tokens.get(token_name))
        auth = claims.get("https://api.openai.com/auth") or {}
        candidate = auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id")
        if candidate:
            return str(candidate)
    return ""


class FileTokenStore:
    """Tokens stored as a plain JSON file inside the account's profile directory.

    Matches how Flow persists Chrome session cookies — no OS keychain needed,
    works cross-platform without elevation, and survives app reinstalls as long
    as the data directory is kept.
    """

    TOKEN_FILE = "chatgpt_tokens.json"

    def __init__(self, profile_dir: Path | str) -> None:
        self._path = Path(profile_dir) / self.TOKEN_FILE

    def load(self) -> dict | None:
        try:
            text = self._path.read_text(encoding="utf-8")
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def save(self, tokens: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: write to a temp file then rename.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens), encoding="utf-8")
        tmp.replace(self._path)

    def delete(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except Exception:
            pass

    def migrate_from_keyring(self, account_id: str) -> None:
        """One-time silent migration from Keychain to file store."""
        if self._path.exists():
            return  # Already migrated.
        try:
            import keyring
            raw = keyring.get_password(_LEGACY_KEYRING_SERVICE, f"{_LEGACY_KEYRING_USER}:{account_id}")
            if raw:
                value = json.loads(raw)
                if isinstance(value, dict):
                    self.save(value)
                    keyring.delete_password(_LEGACY_KEYRING_SERVICE, f"{_LEGACY_KEYRING_USER}:{account_id}")
        except Exception:
            pass


# Backwards-compatible alias so tests that still reference SystemTokenStore work.
SystemTokenStore = FileTokenStore


@dataclass
class OAuthLogin:
    id: str
    state: str
    code_verifier: str
    expires_at: float
    code: str = ""
    error: str = ""
    server: HTTPServer | None = None


class ChatGPTAuth:
    def __init__(self, token_store=None, client=None, account_id="default", browser_opener=None,
                 profile_dir: Path | str | None = None):
        if token_store is not None:
            self.store = token_store
        elif profile_dir is not None:
            store = FileTokenStore(profile_dir)
            store.migrate_from_keyring(account_id)
            self.store = store
        else:
            # Fallback: use a sibling file next to the module (should not happen
            # in production; always pass profile_dir via auth_for()).
            from pipeline.core.config import DATA
            store = FileTokenStore(DATA / "chat" / "profiles" / account_id)
            store.migrate_from_keyring(account_id)
            self.store = store
        self.account_id = account_id
        self.browser_opener = browser_opener
        sanitize_httpx_no_proxy()
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self._pending: dict[str, OAuthLogin] = {}
        self._refresh_lock = threading.Lock()
        self._login_complete = threading.Event()

    @property
    def login_complete(self) -> threading.Event:
        """Signals that the single managed OAuth window may close."""
        return self._login_complete

    @property
    def login_pending(self) -> bool:
        return bool(self._pending)

    def fail_pending(self, error: str) -> None:
        for login in self._pending.values():
            login.error = error
        self._login_complete.set()

    def status(self) -> dict:
        tokens = self.store.load()
        if not tokens:
            return {"status": "signed_out", "configured": False}
        claims = _jwt_claims(tokens.get("id_token"))
        return {"status": "connected", "configured": True, "email": claims.get("email", ""), "expiresAt": tokens.get("expires_at")}

    def start_login(self, open_browser: bool = True) -> dict:
        self._login_complete.clear()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        login = OAuthLogin(uuid.uuid4().hex, state, verifier, time.time() + 900)
        login.server = self._callback_server(login, self._login_complete)
        self._pending[login.id] = login
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        authorize_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        if open_browser:
            (self.browser_opener or webbrowser.open)(authorize_url)
        return {"loginId": login.id, "authorizationUrl": authorize_url, "expiresAt": login.expires_at}

    @staticmethod
    def _callback_server(login: OAuthLogin, login_complete: threading.Event) -> HTTPServer:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if query.get("state", [""])[0] != login.state:
                    login.error = "OAuth state mismatch"
                elif query.get("error"):
                    login.error = query["error"][0]
                else:
                    login.code = query.get("code", [""])[0]
                    if not login.code:
                        login.error = "OAuth callback did not include an authorization code"
                body = b"<html><body>Sign-in complete. You can close this window.</body></html>"
                self.send_response(200 if login.code else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                login_complete.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()

            def log_message(self, *_args):
                return

        try:
            server = HTTPServer(("127.0.0.1", 1455), Handler)
        except OSError as exc:
            raise RuntimeError("OAuth callback port 1455 is already in use") from exc
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def poll(self, login_id: str) -> dict:
        login = self._pending.get(login_id)
        if not login:
            raise KeyError(login_id)
        if time.time() >= login.expires_at:
            self._pending.pop(login_id, None)
            self._login_complete.set()
            if login.server:
                login.server.shutdown()
                login.server.server_close()
            return {"status": "expired"}
        if login.error:
            self._pending.pop(login_id, None)
            self._login_complete.set()
            if login.server:
                login.server.shutdown()
                login.server.server_close()
            return {"status": "failed", "error": login.error}
        if not login.code:
            return {"status": "pending"}
        try:
            token_response = self.client.post(f"{ISSUER}/oauth/token", data={"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": login.code, "code_verifier": login.code_verifier, "redirect_uri": REDIRECT_URI}, headers={"Accept": "application/json"})
            token_response.raise_for_status()
            tokens = self._normalize_tokens(token_response.json())
            self.store.save(tokens)
        except Exception:
            self._pending.pop(login_id, None)
            self._login_complete.set()
            if login.server:
                login.server.shutdown()
                login.server.server_close()
            raise
        self._pending.pop(login_id, None)
        if login.server:
            login.server.server_close()
        status = self.status()
        return {"status": "connected", "email": status.get("email", "")}

    @staticmethod
    def _normalize_tokens(raw: dict, previous_refresh: str = "") -> dict:
        if not raw.get("access_token"):
            raise RuntimeError("Token response is incomplete")
        expires_in = int(raw.get("expires_in") or 3600)
        return {"access_token": raw["access_token"], "refresh_token": raw.get("refresh_token") or previous_refresh, "id_token": raw.get("id_token", ""), "account_id": _account_id(raw), "expires_at": time.time() + expires_in}

    def tokens(self) -> dict:
        with self._refresh_lock:
            tokens = self.store.load()
            if not tokens:
                raise RuntimeError("ChatGPT account is signed out")
            if float(tokens.get("expires_at") or 0) > time.time() + 60:
                return tokens
            refresh = tokens.get("refresh_token")
            if not refresh:
                self.store.delete()
                raise RuntimeError("ChatGPT session expired; sign in again")
            response = self.client.post(f"{ISSUER}/oauth/token", json={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID, "scope": SCOPE}, headers={"Accept": "application/json"})
            if response.status_code in {400, 401, 403}:
                self.store.delete()
                raise RuntimeError("ChatGPT session expired; sign in again")
            response.raise_for_status()
            updated = self._normalize_tokens(response.json(), refresh)
            self.store.save(updated)
            return updated

    def logout(self) -> None:
        self.store.delete()
