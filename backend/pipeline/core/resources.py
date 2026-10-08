"""Tự chọn mức song song theo tài nguyên còn rảnh của máy."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any


def _windows_idle_and_memory() -> tuple[float, float] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        def sample() -> tuple[int, int, int]:
            idle = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                raise OSError("GetSystemTimes")

            def ticks(value: wintypes.FILETIME) -> int:
                return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

            return ticks(idle), ticks(kernel), ticks(user)

        first = sample()
        time.sleep(0.08)
        second = sample()
        idle_delta = max(0, second[0] - first[0])
        total_delta = max(1, second[1] - first[1] + second[2] - first[2])

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        memory = MemoryStatus()
        memory.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise OSError("GlobalMemoryStatusEx")
        return min(1.0, idle_delta / total_delta), min(
            1.0, float(memory.ullAvailPhys) / max(1.0, float(memory.ullTotalPhys))
        )
    except Exception:
        return None


def _cpu_idle_and_memory() -> tuple[float, float]:
    try:
        import psutil  # type: ignore

        busy = float(psutil.cpu_percent(interval=0.08)) / 100.0
        memory_free = float(psutil.virtual_memory().available) / max(
            1.0, float(psutil.virtual_memory().total)
        )
        return max(0.0, min(1.0, 1.0 - busy)), max(0.0, min(1.0, memory_free))
    except Exception:
        windows = _windows_idle_and_memory()
        if windows is not None:
            return windows
        try:
            load = float(os.getloadavg()[0]) / max(1, os.cpu_count() or 1)
            return max(0.0, min(1.0, 1.0 - load)), 1.0
        except (AttributeError, OSError):
            return 0.75, 1.0


def _nvidia_smi_mem() -> tuple[float, float, float] | None:
    """(util_pct, free_mb, total_mb) hoặc None."""
    try:
        raw = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=1.5,
            stderr=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if sys.platform == "win32"
            else 0,
        ).splitlines()[0]
        util, free, total = (float(x.strip()) for x in raw.split(",")[:3])
        return util, free, total
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _gpu_headroom() -> tuple[float, float] | None:
    """(GPU idle 0–1, VRAM free ratio 0–1); None nếu không NVIDIA."""
    m = _nvidia_smi_mem()
    if m is None:
        return None
    util, free, total = m
    return max(0.0, min(1.0, 1.0 - util / 100.0)), max(
        0.0, min(1.0, free / max(1.0, total))
    )


def pack_gpu_workers(
    *,
    per_job_mb: int = 1200,
    reserve_mb: int = 500,
    hard_max: int = 12,
) -> int:
    """Lấp VRAM trống — mục tiêu GPU không ngồi không.

    - Ưu tiên free_mb / per_job (card đang dùng bao nhiêu cũng pack phần còn lại).
    - Util thấp: cho phép pack gần hard_max (dùng cả total*0.94 − used).
    - Util cao / VRAM sát: hạ, tối thiểu 1.
    """
    cores = max(1, os.cpu_count() or 4)
    hm = max(1, int(hard_max))
    m = _nvidia_smi_mem()
    if m is None:
        return min(hm, max(2, cores // 2))
    util, free, total = m
    per = max(350.0, float(per_job_mb))
    res = max(200.0, float(reserve_mb))
    used = max(0.0, total - free)

    # Ngân sách VRAM cho job: phần free trừ reserve; util thấp thêm headroom từ total
    if util < 25.0:
        # Card gần idle → nhắm ~94% total
        budget = max(free - res * 0.5, total * 0.94 - used - res * 0.3)
    elif util < 50.0:
        budget = max(free - res * 0.6, total * 0.90 - used - res * 0.4)
    elif util < 80.0:
        budget = free - res * 0.7
    else:
        budget = free - res

    n = int(max(0.0, budget) // per)
    # Util rất cao: chỉ giữ job vừa free
    if util >= 95.0:
        n = max(1, min(n, int((free - res) // per) or 1))
        n = max(1, round(n * 0.75))
    elif util >= 88.0:
        n = max(1, round(n * 0.88))
    # Idle + còn VRAM: không để 1 job nếu pack được nhiều hơn
    if util < 35.0 and free > per * 2:
        n = max(n, min(hm, int((free - res) // per)))

    n = max(1, n)
    # GPU fat: hard_max; host threads: không cần ≤ cores nếu job I/O-GPU bound
    return max(1, min(hm, n))


def gpu_job_cap(
    *,
    per_job_mb: int = 1200,
    reserve_mb: int = 500,
    hard_max: int = 12,
) -> int:
    """Alias pack_gpu_workers — API cũ."""
    return pack_gpu_workers(
        per_job_mb=per_job_mb, reserve_mb=reserve_mb, hard_max=hard_max
    )


def workers_label(n: int | None, *, kind: str | None = None) -> str:
    """Chuỗi gọn « · N luồng» cho progress UI (bỏ GPU/TTS/net suffix)."""
    if n is None:
        return ""
    try:
        w = int(n)
    except (TypeError, ValueError):
        return ""
    if w <= 0:
        return ""
    return f" · {w} luồng"


def progress_msg(
    label: str,
    cur: int | None = None,
    total: int | None = None,
    *,
    workers: int | None = None,
    extra: str | None = None,
) -> str:
    """Format thống nhất: «Label · 12/40 · 3 cache · 5 luồng»."""
    parts: list[str] = [label.strip()]
    if cur is not None and total is not None and total > 0:
        parts.append(f"{int(cur)}/{int(total)}")
    elif cur is not None:
        parts.append(str(int(cur)))
    if extra:
        parts.append(extra.strip())
    wbit = workers_label(workers).lstrip(" ·")
    if wbit:
        parts.append(wbit)
    return " · ".join(p for p in parts if p)


def run_with_adaptive_workers(
    items: list[Any],
    worker_fn: Callable[[Any], Any],
    *,
    kind: str = "cpu",
    requested: int | None = 0,
    cap: int = 16,
    thread_name_prefix: str = "adapt",
    on_progress: Callable[[int, int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    adapt_every_sec: float = 2.0,
) -> list[Any]:
    """Pool elastic: **duỗi khi rảnh, co khi bận** (CPU/GPU).

    - requested > 0: cố định (không co/giãn).
    - auto: mỗi ~2s đo adaptive_workers lại
      · want > w → release thêm slot (duỗi)
      · want < w → lần xong việc sau giữ slot (co), không release
    - Pool size = cap; concurrency thực = số permit semaphore.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    import time
    from typing import Any as _Any

    n = len(items)
    if n == 0:
        return []
    fixed = requested is not None and int(requested) > 0
    hard = max(1, min(int(cap), n))
    w0 = adaptive_workers(requested, kind=kind, cap=hard, tasks=n)
    w0 = max(1, min(w0, hard))

    slots = threading.Semaphore(w0)
    # held = permit đang «cầm» (target); in_use = worker đang chạy
    state = {
        "w": w0,
        "held": w0,
        "in_use": 0,
        "last": time.monotonic(),
    }
    lock = threading.Lock()
    results: list[_Any] = [None] * n
    done_n = 0
    done_lock = threading.Lock()

    def _retarget() -> int:
        """Cập nhật target w theo load. Chỉ duỗi tại đây; co ở finally worker."""
        if fixed:
            return state["w"]
        now = time.monotonic()
        with lock:
            if now - state["last"] < adapt_every_sec:
                return state["w"]
            state["last"] = now
            want = adaptive_workers(None, kind=kind, cap=hard, tasks=n)
            want = max(1, min(want, hard))
            cur = state["w"]
            if want > cur:
                # Duỗi: thêm permit
                # A previous contraction may still be waiting for in-flight
                # workers to finish. `held` is the actual permit budget; only
                # the missing permits need to be released here.
                for _ in range(max(0, want - state["held"])):
                    slots.release()
                    state["held"] += 1
                state["w"] = want
            elif want < cur:
                # Co: hạ target — worker xong sẽ không release (giữ slot)
                state["w"] = want
            return state["w"]

    def _wrapped(idx: int, item: Any) -> tuple[int, Any]:
        if cancel_check:
            cancel_check()
        _retarget()
        slots.acquire()
        with lock:
            state["in_use"] += 1
        try:
            if cancel_check:
                cancel_check()
            return idx, worker_fn(item)
        finally:
            with lock:
                state["in_use"] -= 1
                # Co: nếu held > target và không ai chờ tăng → giữ permit (không release)
                if state["held"] > state["w"]:
                    state["held"] -= 1
                    # không release → concurrency giảm
                else:
                    slots.release()

    pool_size = hard if not fixed else w0
    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix=thread_name_prefix) as pool:
        futs = {pool.submit(_wrapped, i, item): i for i, item in enumerate(items)}
        for fut in as_completed(futs):
            if cancel_check:
                cancel_check()
            idx, val = fut.result()
            results[idx] = val
            with done_lock:
                done_n += 1
                cur = done_n
            w_now = _retarget()
            if on_progress:
                on_progress(cur, n, w_now)
    return results


def adaptive_workers(
    requested: int | None,
    *,
    kind: str = "cpu",
    cap: int = 16,
    tasks: int | None = None,
) -> int:
    """Auto workers: lấp CPU/GPU khi rảnh; bận mới hạ. requested>0 = cố định.

    kind:
      cpu     — pool CPU
      gpu     — OCR/CUDA frame — pack VRAM (per_job nhẹ)
      network — HTTP cloud TTS
      tts     — VieNeu local — pack VRAM (per_job nặng)
    CPU trần ~92% cores. GPU trần = min(cap, pack VRAM), không kẹp giả 4.
    """
    cores = max(1, os.cpu_count() or 4)
    hard_cpu_cap = max(1, int(cores * 0.92))
    if kind == "network":
        limit = max(1, min(int(cap), 32))
    elif kind in ("gpu", "tts"):
        # GPU: cap từ caller (VRAM hard); không cắt bằng 92% cores
        limit = max(1, int(cap))
    else:
        limit = max(1, min(int(cap), hard_cpu_cap))

    if requested is not None and int(requested) > 0:
        value = min(limit, int(requested))
    else:
        cpu_idle, memory_free = _cpu_idle_and_memory()
        # CPU rảnh → gần hard_cpu_cap; bận → ~50%
        cpu_target = max(1, round(cores * (0.50 + 0.42 * cpu_idle)))
        cpu_target = min(cpu_target, hard_cpu_cap)
        # Soft host-RAM pressure: keep jobs alive, but progressively reduce
        # concurrency before Linux/macOS starts thrashing near 100% usage.
        if memory_free < 0.05:
            cpu_target = max(1, round(cpu_target * 0.30))
        elif memory_free < 0.10:
            cpu_target = max(1, round(cpu_target * 0.50))
        elif memory_free < 0.15:
            cpu_target = max(1, round(cpu_target * 0.72))

        if kind == "network":
            network_target = round(6 + 14 * cpu_idle)
            value = max(1, min(limit, network_target))
        elif kind in ("gpu", "tts"):
            # OCR frame nhẹ hơn TTS neural
            per_mb = 1500 if kind == "tts" else 450
            reserve = 600 if kind == "tts" else 350
            smi = _nvidia_smi_mem()
            if smi is None:
                frac = 0.6 if kind == "tts" else 0.95
                value = min(limit, max(1, round(cpu_target * frac)))
            else:
                util, free, total = smi
                packed = pack_gpu_workers(
                    per_job_mb=per_mb, reserve_mb=reserve, hard_max=limit
                )
                # GPU rảnh (util thấp): lấy full pack — không nhân c_scale làm hụt
                if util < 40.0:
                    value = packed
                elif util < 70.0:
                    # Hơi bận: vẫn gần pack, chỉ hạ nhẹ nếu CPU cũng nóng
                    value = max(1, round(packed * (0.90 + 0.10 * cpu_idle)))
                else:
                    # GPU bận: theo free VRAM đã nằm trong pack; hạ thêm nếu CPU full
                    value = max(1, round(packed * (0.75 + 0.25 * cpu_idle)))
                value = min(limit, value)
                # Còn trống rõ (free > 2 job) mà value=1 → nâng tối thiểu 2
                if free >= per_mb * 2 + reserve and value < 2 and limit >= 2:
                    value = 2
                # Rất rảnh: free đủ N job → không để dưới N-1
                fit = int(max(0.0, free - reserve) // per_mb)
                if util < 45.0 and fit >= 3:
                    value = max(value, min(limit, fit))
        else:
            value = min(limit, cpu_target)

        # Apply the same host-RAM throttle to network/GPU/TTS branches after
        # their device-specific packing logic. Explicit requested counts stay
        # unchanged; automatic mode is the safe adaptive path.
        if kind != "cpu":
            if memory_free < 0.05:
                value = max(1, round(value * 0.50))
            elif memory_free < 0.10:
                value = max(1, round(value * 0.72))

    if tasks is not None:
        value = min(value, max(1, int(tasks)))
    return max(1, value)
