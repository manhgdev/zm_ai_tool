"""Windows: ẩn cửa sổ console + giới hạn CPU cho subprocess nặng."""
from __future__ import annotations

import os
import subprocess
import sys

_patched = False

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_INFORMATION = 0x0400


def cpu_budget_cores(fraction: float = 0.6, *, minimum: int = 2) -> int:
    """Số core tối đa cho job nền — chừa phần còn lại cho UI/hệ điều hành.

    Chỉnh bằng ZM_AI_TOOL_JOB_CPU_FRACTION (0.1–1.0) nếu máy vẫn nặng/thừa.
    """
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    raw = (os.environ.get("ZM_AI_TOOL_JOB_CPU_FRACTION") or "").strip()
    if raw:
        try:
            fraction = max(0.1, min(1.0, float(raw)))
        except ValueError:
            pass
    return max(minimum, min(cores, int(cores * fraction)))


def limit_process_cpu(
    proc: subprocess.Popen,
    *,
    fraction: float = 0.6,
    below_normal: bool = True,
) -> bool:
    """Kẹp process (và con của nó) vào N core đầu + hạ ưu tiên.

    Decode video của OpenCV/FFmpeg tự spawn thread theo số core (đo được
    8.4/12 core) và KHÔNG nghe biến môi trường nào — chặn ở tầng OS là cách
    duy nhất giữ máy không đơ. Trả False nếu không áp được (bỏ qua êm).
    """
    if sys.platform != "win32":
        return _limit_posix(proc, fraction=fraction, below_normal=below_normal)
    try:
        import ctypes
        from ctypes import wintypes

        pid = int(proc.pid)
        if pid <= 0:
            return False
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(
            _PROCESS_SET_INFORMATION | _PROCESS_QUERY_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            mask = (1 << cpu_budget_cores(fraction)) - 1
            k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
            ok = bool(k32.SetProcessAffinityMask(handle, ctypes.c_size_t(mask)))
            if below_normal:
                k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                k32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
            return ok
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _limit_posix(proc: subprocess.Popen, *, fraction: float, below_normal: bool) -> bool:
    try:
        if below_normal:
            os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
        sched = getattr(os, "sched_setaffinity", None)
        if sched:
            sched(proc.pid, set(range(cpu_budget_cores(fraction))))
        return True
    except Exception:
        return False


def hide_console_kwargs() -> dict:
    """Kwargs cho Popen/run/check_output — không hiện CMD đen."""
    if sys.platform != "win32":
        return {}
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return {"creationflags": flags}


def apply_subprocess_no_window() -> None:
    """Patch subprocess.Popen một lần — mọi call sau không flash CMD."""
    global _patched
    if _patched or sys.platform != "win32":
        return
    _patched = True
    _orig = subprocess.Popen
    no_win = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    class Popen(_orig):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            # Không ghi đè nếu caller đã set flags / shell interactive
            if kwargs.get("creationflags") is None and not kwargs.get("shell"):
                kwargs["creationflags"] = no_win
            super().__init__(*args, **kwargs)

    subprocess.Popen = Popen  # type: ignore[misc, assignment]
