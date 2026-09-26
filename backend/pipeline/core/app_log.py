"""App log ring — desktop + dev. UI tab Log đọc từ đây."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

_lock = threading.Lock()
APP_LOG_LIMIT = 50
_ring: deque[str] = deque(maxlen=APP_LOG_LIMIT)
_hooks_installed = False


def log_path() -> Path:
    """Desktop: ZM_AI_TOOL_HOME/app.log; dev: backend/data/app.log."""
    home = os.environ.get("ZM_AI_TOOL_HOME")
    if home:
        return Path(home) / "app.log"
    # backend/pipeline/core → parents[2] = backend
    return Path(__file__).resolve().parents[2] / "data" / "app.log"


def _persist_ring() -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(("\n".join(_ring) + "\n") if _ring else "", encoding="utf-8", errors="replace")
    except OSError:
        pass


def append_log(message: str, *, also_print: bool = True) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = message.rstrip("\n")
    if not line:
        return
    stamped = "\n".join(f"[{ts}] {ln}" if i == 0 else ln for i, ln in enumerate(line.splitlines()))
    with _lock:
        _ring.append(stamped)
        _persist_ring()
    if also_print:
        try:
            print(stamped, flush=True)
        except Exception:
            pass


def append_exception(prefix: str, exc: BaseException | None = None) -> None:
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        tb = traceback.format_exc()
    append_log(f"{prefix}\n{tb}".rstrip())


def read_log(*, tail: int = APP_LOG_LIMIT, max_chars: int = 400_000) -> dict[str, Any]:
    """Ưu tiên ring memory (đã xoay vòng); file disk là bản ghi lại của ring."""
    tail = max(1, min(APP_LOG_LIMIT, int(tail)))
    path = log_path()
    with _lock:
        mem = list(_ring)[-tail:]
    if mem:
        body = "\n".join(mem)
    else:
        chunks: list[str] = []
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                # Cold start: file có thể còn log cũ dài — chỉ lấy đuôi theo số dòng.
                chunks = text.splitlines()[-tail:]
        except OSError as e:
            chunks = [f"[log-read] {e}"]
        body = "\n".join(chunks)
    if len(body) > max_chars:
        body = body[-max_chars:]
        body = "…\n" + body
    return {
        "path": str(path),
        "text": body,
        "lines": body.count("\n") + (1 if body else 0),
        "desktop": (os.environ.get("ZM_AI_TOOL_DESKTOP")) == "1",
    }


def clear_log() -> dict[str, Any]:
    path = log_path()
    with _lock:
        _ring.clear()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": str(e), "path": str(path)}
    append_log("[log] cleared by user", also_print=False)
    return {"ok": True, "path": str(path)}


def install_process_hooks() -> None:
    """sys/threading excepthook → app.log. Gọi 1 lần từ app lifespan / launcher."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    # Seed ring from disk so the first append does not erase prior rotated tail.
    with _lock:
        if not _ring:
            path = log_path()
            try:
                if path.is_file():
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-APP_LOG_LIMIT:]:
                        if line:
                            _ring.append(line)
                    _persist_ring()
            except OSError:
                pass

    def _thread_hook(args: Any) -> None:
        try:
            if args.exc_type and issubclass(args.exc_type, SystemExit):
                return
            name = args.thread.name if args.thread else "?"
            append_exception(
                f"[thread:{name}] {getattr(args.exc_type, '__name__', '?')}",
                args.exc_value if isinstance(args.exc_value, BaseException) else None,
            )
            if args.exc_value is None and args.exc_traceback is not None:
                append_log(
                    "".join(
                        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                    )
                )
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            from asyncio.proactor_events import _ProactorBasePipeTransport

            _orig_call_conn_lost = _ProactorBasePipeTransport._call_connection_lost

            def _patched_call_connection_lost(self, exc=None):
                try:
                    _orig_call_conn_lost(self, exc)
                except ConnectionResetError:
                    pass
                except OSError as e:
                    if getattr(e, "winerror", None) == 10054:
                        pass
                    else:
                        raise

            _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost
        except Exception:
            pass

    try:
        threading.excepthook = _thread_hook  # type: ignore[attr-defined]
    except Exception:
        pass

    prev = sys.excepthook

    def _sys_hook(exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            if issubclass(exc_type, SystemExit):
                return
            append_log(
                f"[sys] {exc_type.__name__}: {exc}\n"
                + "".join(traceback.format_exception(exc_type, exc, tb))
            )
        except Exception:
            pass
        try:
            prev(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _sys_hook
    append_log("[app] log hooks installed", also_print=False)
