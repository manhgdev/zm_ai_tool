"""Job cancel flags + killable subprocess runner."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

# cancel thật: Event + kill subprocess đang chạy
_cancel_flags: dict[str, threading.Event] = {}
_job_procs: dict[str, list[subprocess.Popen]] = {}
_job_pids: dict[str, set[int]] = {}  # bare PIDs (frozen TTS worker, etc.)
_job_gen: dict[str, int] = {}
_cancel_aliases: dict[str, set[str]] = {}
_lock = threading.Lock()
# thread-local: project_id của worker TTS/export — subprocess tự gắn
_tls = threading.local()


class Cancelled(Exception):
    """Job bị user huỷ."""


def set_job_context(project_id: str | None) -> None:
    """Gắn project_id cho thread hiện tại (TTS worker) → register_process tự biết."""
    _tls.project_id = project_id


def current_job_id() -> str | None:
    pid = getattr(_tls, "project_id", None)
    return pid if isinstance(pid, str) and pid else None


def kill_pid_tree(pid: int) -> None:
    """Kill process + children by OS pid."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)),
            check=False,
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Dừng cả process con; p.kill() một mình để sót ffmpeg/Demucs trên Windows."""
    if proc.poll() is not None:
        return
    try:
        kill_pid_tree(int(proc.pid))
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def register_process(project_id: str | None, proc: subprocess.Popen) -> None:
    pid = project_id or current_job_id()
    if not pid:
        return
    with _lock:
        _job_procs.setdefault(pid, []).append(proc)
        try:
            _job_pids.setdefault(pid, set()).add(int(proc.pid))
        except Exception:
            pass
    # đã cancel trước khi register → kill ngay
    if is_cancelled(pid):
        kill_process_tree(proc)


def register_pid(project_id: str | None, pid: int) -> None:
    jid = project_id or current_job_id()
    if not jid or pid <= 0:
        return
    with _lock:
        _job_pids.setdefault(jid, set()).add(int(pid))
    if is_cancelled(jid):
        kill_pid_tree(int(pid))


def unregister_process(project_id: str | None, proc: subprocess.Popen) -> None:
    pid = project_id or current_job_id()
    if not pid:
        return
    with _lock:
        current = _job_procs.get(pid)
        if current is not None:
            _job_procs[pid] = [item for item in current if item is not proc]
        try:
            pset = _job_pids.get(pid)
            if pset is not None:
                pset.discard(int(proc.pid))
        except Exception:
            pass


def unregister_pid(project_id: str | None, os_pid: int) -> None:
    jid = project_id or current_job_id()
    if not jid:
        return
    with _lock:
        pset = _job_pids.get(jid)
        if pset is not None:
            pset.discard(int(os_pid))


def begin_job(project_id: str) -> int:
    """Bắt đầu job mới.

    Kế thừa cancel chỉ khi user Huỷ lúc Queued (arm_job đã tạo event,
    rồi request_cancel set). arm_job luôn reset event sạch trước mỗi job mới
    nên cancel của lần trước không dính.
    """
    with _lock:
        gen = int(_job_gen.get(project_id, 0)) + 1
        _job_gen[project_id] = gen
        prev = _cancel_flags.get(project_id)
        inherit_cancel = bool(prev is not None and prev.is_set())
        ev = threading.Event()
        if inherit_cancel:
            ev.set()
        _cancel_flags[project_id] = ev
        old = list(_job_procs.get(project_id, []))
        old_pids = list(_job_pids.get(project_id, set()))
        _job_procs[project_id] = []
        _job_pids[project_id] = set()
    for p in old:
        kill_process_tree(p)
    for op in old_pids:
        kill_pid_tree(op)
    return gen


def arm_job(project_id: str) -> int:
    """Gắn flag cancel sớm (Queued) — Huỷ trước begin_job vẫn ăn.

    Luôn tạo event sạch (bỏ cancelled của job trước).
    """
    with _lock:
        _job_gen.setdefault(project_id, 0)
        _cancel_flags[project_id] = threading.Event()
        return int(_job_gen.get(project_id, 0))


def kill_job_processes(project_id: str) -> None:
    """Kill mọi subprocess đã register cho job (không đụng cancel flag)."""
    with _lock:
        procs = list(_job_procs.get(project_id, []) or [])
        pids = list(_job_pids.get(project_id, set()) or set())
    for p in procs:
        kill_process_tree(p)
    for op in pids:
        kill_pid_tree(op)


def share_cancel(src: str, dest: str) -> None:
    """Queue job id and project id cancel together even if begin_job replaces Events."""
    if not src or not dest or src == dest:
        return
    with _lock:
        _cancel_aliases.setdefault(src, set()).add(dest)
        _cancel_aliases.setdefault(dest, set()).add(src)


def request_cancel(project_id: str) -> bool:
    """Đánh dấu huỷ + kill ngay mọi subprocess (ffmpeg/TTS/OCR/Demucs)."""
    with _lock:
        ids = {project_id, *(_cancel_aliases.get(project_id) or set())}
        for pid in ids:
            ev = _cancel_flags.get(pid)
            if not ev:
                ev = threading.Event()
                _cancel_flags[pid] = ev
                _job_gen.setdefault(pid, 0)
            ev.set()
    for pid in ids:
        kill_job_processes(pid)
    # TTS Studio (job_id riêng) — chỉ set flag, không đệ quy request_cancel
    try:
        from pipeline.tts.studio import mark_cancel as _studio_mark

        _studio_mark(project_id)
    except Exception:
        try:
            from pipeline.tts import studio as _studio

            with _studio._jobs_lock:
                if project_id in _studio._running or project_id in _studio._cancel_flags:
                    _studio._cancel_flags[project_id] = True
        except Exception:
            pass
    # Do NOT shutdown_all_workers() here — that wiped the warm VieNeu pool on
    # every cancel (Flow/dub/queue), forcing a full model reload on the next
    # /text-to-speech visit. Active workers are already killed via register_process.
    return True


def clear_job(project_id: str, gen: int | None = None) -> None:
    """Xóa flag. gen → chỉ clear đúng generation. Kill sót process trước khi xóa."""
    with _lock:
        if gen is not None and _job_gen.get(project_id) != gen:
            return
        procs = list(_job_procs.pop(project_id, []) or [])
        pids = list(_job_pids.pop(project_id, set()) or set())
        _cancel_flags.pop(project_id, None)
    for p in procs:
        kill_process_tree(p)
    for op in pids:
        kill_pid_tree(op)


def check_cancel(project_id: str | None, gen: int | None = None) -> None:
    if not project_id:
        return
    with _lock:
        if gen is not None and _job_gen.get(project_id) != gen:
            return
        ev = _cancel_flags.get(project_id)
        if ev and ev.is_set():
            raise Cancelled()


def job_generation(project_id: str) -> int | None:
    with _lock:
        return _job_gen.get(project_id)


def is_cancelled(project_id: str | None) -> bool:
    if not project_id:
        return False
    with _lock:
        ev = _cancel_flags.get(project_id)
        return bool(ev and ev.is_set())


def short_cmd_error(exc: BaseException, *, limit: int = 280) -> str:
    """Rút gọn CalledProcessError — không dump cả argv ffmpeg vào UI."""
    if isinstance(exc, subprocess.CalledProcessError):
        code = exc.returncode
        cmd = exc.cmd
        head = ""
        if isinstance(cmd, (list, tuple)) and cmd:
            # Chỉ binary + vài flag đầu
            parts = [str(x) for x in cmd[:4]]
            head = " ".join(parts)
            if len(cmd) > 4:
                head += " …"
        elif cmd:
            head = str(cmd)[:120]
        msg = f"Lệnh thất bại (exit {code})"
        if head:
            msg += f": {head}"
        # WinError 206 / path dài
        err = getattr(exc, "strerror", None) or ""
        if "206" in str(code) or "too long" in str(exc).lower():
            msg = "PATH Windows quá dài (WinError 206) — app đã loại đường dẫn trùng; không cần cài lại gói AI."
        return msg[:limit]
    text = str(exc).strip() or type(exc).__name__
    # Cắt khối Command '[ffmpeg'… khổng lồ
    if "Command '" in text or "Command \"" in text:
        if "206" in text or "too long" in text.lower():
            return "PATH Windows quá dài (WinError 206) — app đã loại đường dẫn trùng; không cần cài lại gói AI."
        if "ffmpeg" in text.lower():
            # Lấy exit status nếu có
            import re

            m = re.search(r"exit status (-?\d+)", text, re.I)
            code = m.group(1) if m else "?"
            return f"ffmpeg thất bại (exit {code}). Xem log backend."
        return text[:limit]
    return text[:limit]


def run_cmd(project_id: str | None, cmd: list[str], **kwargs: Any) -> None:
    """subprocess có thể kill khi huỷ."""
    jid = project_id or current_job_id()
    check_cancel(jid)
    kw = dict(kwargs)
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    try:
        from .winproc import hide_console_kwargs

        for k, v in hide_console_kwargs().items():
            kw.setdefault(k, v)
    except Exception:
        pass
    p = subprocess.Popen(cmd, **kw)
    register_process(jid, p)
    try:
        while p.poll() is None:
            try:
                check_cancel(jid)
            except Cancelled:
                kill_process_tree(p)
                raise
            time.sleep(0.08)
        if p.returncode not in (0, None):
            check_cancel(jid)
            raise subprocess.CalledProcessError(p.returncode or 1, cmd)
    finally:
        unregister_process(jid, p)
