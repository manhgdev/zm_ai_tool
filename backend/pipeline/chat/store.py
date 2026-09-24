from __future__ import annotations

import sqlite3
import threading
import uuid
import mimetypes
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatStore:
    def __init__(self, db_path: Path, attachments_root: Path):
        self.db_path, self.attachments_root = Path(db_path), Path(attachments_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachments_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, account_id TEXT NOT NULL,
            model TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '',
            provider_thread_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
          );
          CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, message_id TEXT,
            kind TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
            size INTEGER NOT NULL, created_at TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'application/octet-stream'
          );
          CREATE TABLE IF NOT EXISTS chat_accounts (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
            browser_family TEXT NOT NULL, profile_path TEXT NOT NULL,
            status TEXT NOT NULL, last_model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT ''
          );
        """)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(attachments)")}
        if "content_type" not in columns:
            self._db.execute("ALTER TABLE attachments ADD COLUMN content_type TEXT NOT NULL DEFAULT 'application/octet-stream'")
        conversation_columns = {row[1] for row in self._db.execute("PRAGMA table_info(conversations)")}
        if "provider_id" not in conversation_columns:
            self._db.execute("ALTER TABLE conversations ADD COLUMN provider_id TEXT NOT NULL DEFAULT ''")
            self._db.execute("UPDATE conversations SET provider_id=account_id WHERE provider_id='' OR provider_id IS NULL")
        if "provider_thread_url" not in conversation_columns:
            self._db.execute("ALTER TABLE conversations ADD COLUMN provider_thread_url TEXT NOT NULL DEFAULT ''")
        account_columns = {row[1] for row in self._db.execute("PRAGMA table_info(chat_accounts)")}
        if "error" not in account_columns:
            self._db.execute("ALTER TABLE chat_accounts ADD COLUMN error TEXT NOT NULL DEFAULT ''")
        if "error_code" not in account_columns:
            self._db.execute("ALTER TABLE chat_accounts ADD COLUMN error_code TEXT NOT NULL DEFAULT ''")
        # Legacy Edge/Brave rows keep their profile and history, but all new
        # browser work follows Flow's single Google Chrome runtime.
        self._db.execute("UPDATE chat_accounts SET browser_family='chrome' WHERE browser_family!='chrome'")
        self._db.execute("UPDATE messages SET status='interrupted', updated_at=? WHERE status='streaming'", (_now(),))
        self._db.commit()

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def close(self):
        self._db.close()

    def create_account(self, label, profile_path, account_id=None):
        item = {"id": account_id or uuid.uuid4().hex, "label": str(label).strip()[:80] or "ChatGPT",
                "email": "", "browser_family": "chrome", "profile_path": str(Path(profile_path)),
                "status": "signed_out", "last_model": "", "created_at": _now(), "updated_at": _now(),
                "error": "", "error_code": ""}
        with self._lock:
            self._db.execute(
                "INSERT INTO chat_accounts (id,label,email,browser_family,profile_path,status,last_model,created_at,updated_at,error,error_code) "
                "VALUES (:id,:label,:email,:browser_family,:profile_path,:status,:last_model,:created_at,:updated_at,:error,:error_code)",
                item,
            )
            self._db.commit()
        return item

    def list_accounts(self, public=False):
        with self._lock:
            rows = [dict(x) for x in self._db.execute("SELECT * FROM chat_accounts ORDER BY created_at")]
        if public:
            for row in rows:
                row.pop("profile_path", None)
                row.pop("browser_family", None)
                row["errorCode"] = row.pop("error_code", "")
        return rows

    def get_account(self, account_id):
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM chat_accounts WHERE id=?", (account_id,)).fetchone())

    def update_account(self, account_id, **values):
        allowed = {k: v for k, v in values.items() if k in {"label", "email", "status", "last_model", "error", "error_code"} and v is not None}
        if not allowed or not self.get_account(account_id): return self.get_account(account_id)
        allowed["updated_at"] = _now()
        with self._lock:
            self._db.execute("UPDATE chat_accounts SET " + ",".join(f"{k}=?" for k in allowed) + " WHERE id=?", (*allowed.values(), account_id))
            self._db.commit()
        return self.get_account(account_id)

    def delete_account(self, account_id, delete_history=False):
        import shutil
        account = self.get_account(account_id)
        if not account: return False
        with self._lock:
            if delete_history:
                conversation_ids = [row[0] for row in self._db.execute("SELECT id FROM conversations WHERE account_id=?", (account_id,))]
                for conversation_id in conversation_ids:
                    self.delete_conversation(conversation_id)
            self._db.execute("DELETE FROM chat_accounts WHERE id=?", (account_id,))
            self._db.commit()
        shutil.rmtree(account["profile_path"], ignore_errors=True)
        return True

    def create_conversation(self, title="Cuộc trò chuyện mới", account_id="openrouter", model="", provider_id=None):
        provider_id = str(provider_id or account_id or "openrouter").strip()
        item = {"id": uuid.uuid4().hex, "title": title[:160], "account_id": account_id,
                "model": model, "provider_id": provider_id, "provider_thread_url": "",
                "created_at": _now(), "updated_at": _now()}
        with self._lock:
            self._db.execute(
                "INSERT INTO conversations (id,title,account_id,model,provider_id,provider_thread_url,created_at,updated_at) "
                "VALUES (:id,:title,:account_id,:model,:provider_id,:provider_thread_url,:created_at,:updated_at)",
                item,
            )
            self._db.commit()
        return item

    def list_conversations(self):
        with self._lock:
            return [dict(x) for x in self._db.execute("SELECT * FROM conversations ORDER BY updated_at DESC")]

    def get_conversation(self, conversation_id):
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone())

    def update_conversation(self, conversation_id, **values):
        allowed = {k: v for k, v in values.items() if k in {"title", "account_id", "model", "provider_id", "provider_thread_url"} and v is not None}
        # Older callers only know account_id. Keep the new provider field in
        # sync for API conversations while allowing Codex account IDs to remain
        # stored separately when both values are supplied.
        if "account_id" in allowed and "provider_id" not in allowed:
            allowed["provider_id"] = allowed["account_id"]
        if "title" in allowed:
            allowed["title"] = str(allowed["title"]).strip()[:160]
            if not allowed["title"]:
                allowed.pop("title")
        if not allowed or not self.get_conversation(conversation_id):
            return self.get_conversation(conversation_id)
        allowed["updated_at"] = _now()
        with self._lock:
            self._db.execute("UPDATE conversations SET " + ",".join(f"{k}=?" for k in allowed) + " WHERE id=?", (*allowed.values(), conversation_id))
            self._db.commit()
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id):
        import shutil
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            self._db.execute("DELETE FROM attachments WHERE conversation_id=?", (conversation_id,))
            self._db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            self._db.commit()
        shutil.rmtree(self.attachments_root / conversation_id, ignore_errors=True)

    def create_message(self, conversation_id, role, content, status="completed", error=None):
        if not self.get_conversation(conversation_id):
            raise KeyError(conversation_id)
        item = {"id": uuid.uuid4().hex, "conversation_id": conversation_id, "role": role,
                "content": content, "status": status, "error": error, "created_at": _now(), "updated_at": _now()}
        with self._lock:
            self._db.execute("INSERT INTO messages VALUES (:id,:conversation_id,:role,:content,:status,:error,:created_at,:updated_at)", item)
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), conversation_id))
            self._db.commit()
        return item

    def update_message(self, message_id, **values):
        allowed = {k: v for k, v in values.items() if k in {"content", "status", "error"}}
        allowed["updated_at"] = _now()
        with self._lock:
            self._db.execute("UPDATE messages SET " + ",".join(f"{k}=?" for k in allowed) + " WHERE id=?", (*allowed.values(), message_id))
            self._db.commit()
        return self._row(self._db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def delete_messages_after(self, conversation_id: str, after_created_at: str) -> None:
        """Delete all messages in a conversation created after the given timestamp."""
        with self._lock:
            self._db.execute(
                "DELETE FROM messages WHERE conversation_id=? AND created_at>?",
                (conversation_id, after_created_at),
            )
            self._db.commit()

    def list_messages(self, conversation_id):
        with self._lock:
            return [dict(x) for x in self._db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (conversation_id,))]

    def save_attachment(self, conversation_id, filename, content, max_size=20 * 1024 * 1024, message_id=None, content_type=None):
        if len(content) > max_size:
            raise ValueError("Attachment is too large")
        if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename or "\0" in filename:
            raise ValueError("Unsafe attachment filename")
        # Keep the shared Chat/Automation attachment contract explicit here.
        allowed = {
            "image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf",
            "text/plain", "text/markdown", "text/vtt", "application/x-subrip",
            "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/aac",
            "audio/flac", "audio/ogg",
        }
        declared = str(content_type or "").split(";", 1)[0].strip().lower()
        guessed = (mimetypes.guess_type(filename)[0] if declared in {"", "application/octet-stream", "binary/octet-stream"} else declared) or "application/octet-stream"
        if guessed not in allowed:
            raise ValueError("Unsupported attachment type")
        signatures = {
            "image/png": (b"\x89PNG\r\n\x1a\n",),
            "image/jpeg": (b"\xff\xd8\xff",),
            "image/gif": (b"GIF87a", b"GIF89a"),
            "image/webp": (b"RIFF",),
            "application/pdf": (b"%PDF-",),
        }
        if guessed in signatures and not any(content.startswith(prefix) for prefix in signatures[guessed]):
            raise ValueError("Attachment content does not match its declared type")
        if guessed == "image/webp" and content[8:12] != b"WEBP":
            raise ValueError("Attachment content does not match its declared type")
        with self._lock:
            total = self._db.execute("SELECT COALESCE(SUM(size),0) FROM attachments WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            count = self._db.execute("SELECT COUNT(*) FROM attachments WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        if total + len(content) > 100 * 1024 * 1024 or count >= 100:
            raise ValueError("Conversation attachment limit exceeded")
        aid = uuid.uuid4().hex
        folder = self.attachments_root / conversation_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{aid}-{filename}"
        path.write_bytes(content)
        row = (aid, conversation_id, message_id, "file", filename, str(path), len(content), _now(), guessed)
        with self._lock:
            self._db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?)", row)
            self._db.commit()
        return {"id": aid, "message_id": message_id, "kind": "file", "name": filename, "size": len(content), "content_type": guessed}

    def save_artifact(self, conversation_id, message_id, filename, content, content_type):
        item = self.save_attachment(conversation_id, filename, content, max_size=25 * 1024 * 1024, message_id=message_id, content_type=content_type)
        with self._lock:
            self._db.execute("UPDATE attachments SET kind='artifact' WHERE id=?", (item["id"],))
            self._db.commit()
        item["kind"] = "artifact"
        return item

    def list_attachments(self, conversation_id, message_id=None):
        sql, args = "SELECT id,conversation_id,message_id,kind,name,size,created_at,content_type FROM attachments WHERE conversation_id=?", [conversation_id]
        if message_id is not None:
            sql += " AND message_id=?"; args.append(message_id)
        with self._lock:
            return [dict(x) for x in self._db.execute(sql + " ORDER BY created_at", args)]

    def attachment_path(self, attachment_id):
        with self._lock:
            row = self._db.execute("SELECT value FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not row: raise KeyError(attachment_id)
        path = Path(row[0]).resolve()
        root = self.attachments_root.resolve()
        if root not in path.parents: raise ValueError("Unsafe attachment path")
        return path

    def attach_to_message(self, conversation_id, message_id, attachment_ids):
        ids = list(dict.fromkeys(str(x) for x in attachment_ids))
        if len(ids) > 10: raise ValueError("A message can contain at most 10 attachments")
        with self._lock:
            for attachment_id in ids:
                row = self._db.execute("SELECT conversation_id FROM attachments WHERE id=?", (attachment_id,)).fetchone()
                if not row or row[0] != conversation_id: raise ValueError("Attachment does not belong to this conversation")
                self._db.execute("UPDATE attachments SET message_id=? WHERE id=?", (message_id, attachment_id))
            self._db.commit()
        return self.list_attachments(conversation_id, message_id)
