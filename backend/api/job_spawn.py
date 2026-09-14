"""Background jobs. Windows: separate process so CUDA crash cannot kill uvicorn."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback

_JOB_WORKER = """# vc-job-worker
import json, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_bytes()
payload = json.loads(raw.removeprefix(b"\\xef\\xbb\\xbf").decode("utf-8"))
mod = __import__(payload["module"], fromlist=[payload["name"]])
fn = getattr(mod, payload["name"])
try:
    fn(*payload["args"])
except BaseException as exc:
    if type(exc).__name__ == "Cancelled":
        raise SystemExit(0) from exc
    raise
"""

_DEFAULT_WORKER_TIMEOUT_SECONDS = 12 * 60 * 60


def _job_python() -> str:
    """Never launch the GUI executable as a worker; launcher exits 2 on extra argv."""
    if getattr(sys, "frozen", False):
        from pipeline.core.accel import _runtime_python

        py = _runtime_python()
        if not py:
            raise RuntimeError(
                "Thiếu .venv-runtime (python.exe) — vào Thiết lập → Cài gói AI"
            )
        return py
    return sys.executable


def _ntstatus(code: int) -> str:
    hints = {
        3221226356: " STATUS_HEAP_CORRUPTION (CUDA/cuDNN)",
        3221226505: " STATUS_STACK_BUFFER_OVERRUN (CUDA/cuDNN)",
    }
    return hints.get(int(code), "")


def _mark_job_error(project_id: object, job: str, msg: str) -> None:
    if not isinstance(project_id, str) or not project_id:
        return
    try:
        from pipeline import set_status

        set_status(project_id, progress=0, message=f"Lỗi: {msg}", running=False, error=msg)
    except Exception as st_e:
        try:
            from pipeline.core.app_log import append_exception

            append_exception("[job] set_status failed", st_e)
        except Exception:
            traceback.print_exc()


def _worker_environment(backend) -> dict[str, str]:  # noqa: ANN001
    """Build a bounded environment for Windows AI workers."""
    from pathlib import Path

    from pipeline.core.runtime_site import subprocess_environment

    env = subprocess_environment()
    path_parts = [str(Path(backend))]
    meipass = getattr(sys, "_MEIPASS", None) or env.get("ZM_AI_TOOL_MEIPASS")
    if meipass:
        path_parts.insert(0, str(meipass))
        env["ZM_AI_TOOL_MEIPASS"] = str(meipass)
    env["PYTHONPATH"] = os.pathsep.join(path_parts + [env.get("PYTHONPATH", "")])
    return env


def _spawn_error_message(exc: OSError) -> str:
    from pipeline.core.runtime_site import is_windows_path_too_long_error

    if is_windows_path_too_long_error(exc):
        return (
            "Không tạo được tiến trình AI (WinError 206): PATH Windows quá dài. "
            "App đã loại đường dẫn trùng; không cần cài lại gói AI."
        )
    return str(exc).strip() or type(exc).__name__


def _worker_timeout_seconds() -> int:
    """Bound native workers while allowing long exports by default."""
    try:
        value = int(os.environ.get("ZM_AI_TOOL_WORKER_TIMEOUT") or _DEFAULT_WORKER_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        value = _DEFAULT_WORKER_TIMEOUT_SECONDS
    return max(60, min(24 * 60 * 60, value))


def _terminate_worker(proc: subprocess.Popen) -> None:
    """Kill a timed-out worker and its native children when possible."""
    try:
        from pipeline.core.jobs import kill_process_tree

        kill_process_tree(proc)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_in_thread(fn, args) -> None:
    import time

    time.sleep(0.2)
    job = getattr(fn, "__name__", "job")
    try:
        fn(*args)
    except BaseException as e:
        try:
            from pipeline.core.jobs import Cancelled

            if isinstance(e, Cancelled):
                return
        except Exception:
            if type(e).__name__ == "Cancelled":
                return
        try:
            from pipeline.core.app_log import append_exception

            append_exception(f"[job:{job}] FAILED", e)
        except Exception:
            traceback.print_exc()
        _mark_job_error(args[0] if args else None, job, str(e).strip()[:2000] or type(e).__name__)


def _run_in_subprocess(fn, args) -> None:
    import json
    import tempfile
    import time
    from pathlib import Path

    time.sleep(0.2)
    job = getattr(fn, "__name__", "job")
    project_id = args[0] if args else None
    try:
        py = _job_python()
    except RuntimeError as e:
        _mark_job_error(project_id, job, str(e))
        return
    backend = Path(__file__).resolve().parent.parent
    payload = {"module": fn.__module__, "name": fn.__name__, "args": list(args)}
    with tempfile.TemporaryDirectory(prefix="vc-job-") as td:
        tdir = Path(td)
        pin, wpy = tdir / "job.json", tdir / "worker.py"
        pin.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        wpy.write_text(_JOB_WORKER, encoding="utf-8")
        env = _worker_environment(backend)
        kw: dict = {
            "cwd": str(backend),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if sys.platform == "win32":
            kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        try:
            proc = subprocess.Popen([py, str(wpy), str(pin)], **kw)
        except OSError as exc:
            msg = _spawn_error_message(exc)
            try:
                from pipeline.core.app_log import append_exception

                append_exception(f"[job:{job}] spawn failed", exc)
            except Exception:
                pass
            _mark_job_error(project_id, job, msg)
            return
        unreg = None
        if isinstance(project_id, str):
            try:
                from pipeline.core.jobs import register_process, unregister_process

                register_process(project_id, proc)
                unreg = unregister_process
            except Exception:
                pass
        try:
            _out, err_b = proc.communicate(timeout=_worker_timeout_seconds())
        except subprocess.TimeoutExpired as exc:
            _terminate_worker(proc)
            try:
                _out, err_b = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                _out, err_b = b"", b""
            timeout = _worker_timeout_seconds()
            detail = str(exc).strip() or "worker did not exit"
            msg = f"{job} timeout after {timeout}s: {detail}"[:2000]
            try:
                from pipeline.core.app_log import append_log

                append_log(f"[job:{job}] {msg}")
            except Exception:
                pass
            _mark_job_error(project_id, job, msg)
            return
        finally:
            if unreg is not None:
                try:
                    unreg(project_id, proc)
                except Exception:
                    pass
        if proc.returncode:
            err = ((err_b or b"") + b"\n" + (_out or b"")).decode("utf-8", "replace")[-1500:]
            msg = (
                f"{job} exit {proc.returncode}{_ntstatus(proc.returncode)}\n{err}"
            ).strip()[:2000]
            try:
                from pipeline.core.app_log import append_log

                append_log(f"[job:{job}] {msg}")
            except Exception:
                pass
            _mark_job_error(project_id, job, msg)


def spawn(fn, *args) -> None:
    """Windows: job process. CUDA crash chỉ giết worker, API :8787 còn sống."""
    target = _run_in_subprocess if sys.platform == "win32" else _run_in_thread
    threading.Thread(
        target=target,
        args=(fn, args),
        daemon=True,
        name=f"job-{getattr(fn, '__name__', 'fn')}",
    ).start()
