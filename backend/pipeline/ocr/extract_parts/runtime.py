"""Paddle/RapidOCR hardsub extract — runtime."""
from __future__ import annotations

"""RapidOCR extract — hardsub đáy + mid/vertical/labels.

Tách khỏi asr.py (Whisper) và đường dịch/phụ đề burn layout.
Không sửa logic — chỉ di chuyển.
"""

from functools import lru_cache
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any

from pipeline.core.jobs import check_cancel, run_cmd
from pipeline.core.project import cache_frames, set_status
from pipeline.core.resources import adaptive_workers

# giới hạn tổng luồng OCR phụ — tránh 100% CPU (để UI/OS ~5–10%)
_ocr_sem: threading.Semaphore | None = None
_ocr_sem_n: int = 0
_cuda_dlls_ready = False
# add_dll_directory() trả handle — phải giữ sống hoặc GC sẽ xóa dir khỏi search path!
_dll_handles: dict[str, object] = {}


def _cpu_budget(ratio: float = 0.9) -> int:
    """Số luồng CPU dùng cho OCR (auto gần full cores)."""
    n = os.cpu_count() or 4
    return max(1, min(n, int(n * ratio)))


def _ocr_pool_workers(
    requested: int | None, *, cap: int | None = None, gpu: bool = False
) -> int:
    # GPU: pack VRAM (đừng kẹp bằng CPU budget — để card đầy khi rảnh)
    from pipeline.core.resources import pack_gpu_workers

    if gpu:
        hard = (
            cap
            if cap is not None
            else pack_gpu_workers(per_job_mb=450, reserve_mb=350, hard_max=20)
        )
        return adaptive_workers(requested, kind="gpu", cap=max(1, int(hard)))
    budget = _cpu_budget(0.92)
    hard = cap if cap is not None else budget
    return adaptive_workers(requested, kind="cpu", cap=min(hard, budget))


def _ocr_semaphore() -> threading.Semaphore:
    """Semaphore toàn cục OCR — CUDA/DML pack VRAM; CPU ≤ budget cores."""
    global _ocr_sem, _ocr_sem_n
    try:
        from pipeline.core.resources import pack_gpu_workers

        # _rapidocr_gpu_kwargs định nghĩa bên dưới cùng file
        gpu_kwargs = _rapidocr_gpu_kwargs()
        if gpu_kwargs.get("det_use_cuda") or gpu_kwargs.get("det_use_dml"):
            n = pack_gpu_workers(per_job_mb=450, reserve_mb=350, hard_max=20)
        else:
            n = _cpu_budget(0.92)
    except Exception:
        n = _cpu_budget(0.92)
    if _ocr_sem is None or _ocr_sem_n != n:
        _ocr_sem = threading.Semaphore(n)
        _ocr_sem_n = n
    return _ocr_sem


def engine_providers(engine: object) -> list[str]:
    """Providers THAT cua session ONNX ben trong RapidOCR (det/cls/rec).

    _rapidocr_gpu_kwargs() chi noi «da YEU CAU cuda»; ham nay doc thiet bi
    session thuc su dung — dung de bao GPU/CPU cho nguoi dung.
    """
    out: list[str] = []
    for name in ("text_det", "text_cls", "text_rec"):
        node = getattr(engine, name, None)
        for attr in ("session", "sess", "infer", "ort_session"):
            node2 = getattr(node, attr, None) if node is not None else None
            if node2 is None:
                continue
            sess = getattr(node2, "session", node2)
            get = getattr(sess, "get_providers", None)
            if callable(get):
                try:
                    out.extend(get())
                except Exception:
                    pass
                break
    return out


def engine_device_label(engine: object) -> str:
    """Provider tang toc that cua session, nguoc lai CPU."""
    provs = engine_providers(engine)
    if any("CUDA" in p or "Tensorrt" in p or "TensorRT" in p for p in provs):
        return "CUDA"
    if any("Dml" in p or "DirectML" in p for p in provs):
        return "DirectML"
    if any("CoreML" in p for p in provs):
        return "CoreML"
    return "CPU"


def _limit_onnx_threads() -> None:
    """ONNX/OpenMP 1 thread / process — fan-out bằng pool, không nhân core.

    LƯU Ý: các biến này chỉ ăn cho BLAS/OpenMP. ONNX Runtime KHÔNG đọc env
    (không có ORT_NUM_THREADS) — phải set qua SessionOptions, xem _ort_threads().
    """
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
    try:
        import cv2

        # OpenCV mặc định dùng MỌI core cho resize/threshold khi tiền xử lý ảnh
        # → treo máy dù OCR chạy GPU.
        cv2.setNumThreads(_cv2_thread_cap())
    except Exception:
        pass


def _cv2_thread_cap() -> int:
    """OpenCV: tối đa 2 luồng — phần nặng đã ở GPU/ffmpeg."""
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    return max(1, min(2, cores - 1))


def _ort_threads(accelerated: bool) -> dict[str, int]:
    """Số luồng CPU cho ONNX Runtime.

    Mặc định của RapidOCR là -1 = ORT tự lấy HẾT core (12 core → 12 luồng busy),
    khiến máy đơ dù model chạy CUDA. GPU: 2 luồng đủ cho op fallback + I/O.
    CPU-only: chừa lại ~1/3 số core cho UI/ffmpeg.
    """
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    if accelerated:
        n = 2
    else:
        n = max(1, int(cores * 0.6))
    return {"intra_op_num_threads": n, "inter_op_num_threads": 1}


def _nvidia_bin_dirs() -> list[Path]:
    """Mọi thư mục nvidia/*/bin (pip CUDA wheels) — purelib + .venv-runtime + sys.path."""
    roots: list[Path] = []
    try:
        import sysconfig

        roots.append(Path(sysconfig.get_paths()["purelib"]))
    except Exception:
        pass
    try:
        exe_parent = Path(sys.executable).parent
        site1 = exe_parent.parent / "Lib" / "site-packages"
        site2 = exe_parent / "Lib" / "site-packages"
        for s in (site1, site2):
            if s.is_dir():
                roots.append(s)
    except Exception:
        pass
    home = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    if home:
        for venv_name in (".venv-runtime", ".venv-ocr"):
            ocr_site = (
                Path(home) / venv_name / "Lib" / "site-packages"
                if os.name == "nt"
                else Path(home)
                / venv_name
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            roots.append(ocr_site)
    # Thêm tất cả site-packages từ sys.path — frozen app có .venv-runtime trong sys.path[0]
    for p in sys.path:
        if p:
            roots.append(Path(p))
    # Cố gắng lấy path từ onnxruntime đã import (nếu sẵn có).
    # KHOONG import onnxruntime ở đây — sẽ poison sys.modules nếu DLL chưa sẵn.
    existing_ort = sys.modules.get("onnxruntime")
    if existing_ort is not None and getattr(existing_ort, "__file__", None):
        try:
            roots.append(Path(existing_ort.__file__).resolve().parent.parent)
        except Exception:
            pass

    seen: set[str] = set()
    bins: list[Path] = []
    for root in roots:
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            for b in nvidia.glob("*/bin"):
                key = str(b.resolve()) if b.exists() else str(b)
                if key not in seen and b.is_dir():
                    seen.add(key)
                    bins.append(b)
        tlib = root / "torch" / "lib"
        if tlib.is_dir():
            key = str(tlib.resolve()) if tlib.exists() else str(tlib)
            if key not in seen:
                seen.add(key)
                bins.append(tlib)
    return bins


_cuda_lock = threading.Lock()


def prepare_cuda_dlls() -> None:
    """PATH + add_dll_directory cho CUDA pip wheels (Whisper + RapidOCR + onnxruntime-gpu)."""
    global _cuda_dlls_ready
    if os.name != "nt" or _cuda_dlls_ready:
        return
    with _cuda_lock:
        if _cuda_dlls_ready:
            return
        bins = _nvidia_bin_dirs()
        # Thêm onnxruntime/capi/ từ mọi site-packages — onnxruntime-gpu đặt DLL ở đây,
        # không phải nvidia/*/bin. Thiếu thư mục này → DLL load failed importing _core.
        seen_bins = {str(b) for b in bins}
        for p in sys.path:
            if not p:
                continue
            sp = Path(p)
            capi = sp / "onnxruntime" / "capi"
            if capi.is_dir() and str(capi) not in seen_bins:
                seen_bins.add(str(capi))
                bins.append(capi)
            sherpa_lib = sp / "sherpa_onnx" / "lib"
            if sherpa_lib.is_dir() and str(sherpa_lib) not in seen_bins:
                seen_bins.add(str(sherpa_lib))
                bins.append(sherpa_lib)
            # cv2.pyd phụ thuộc vào opencv_world4xx.dll nằm trong cv2/ package dir.
            # Windows 10+ không tự tìm DLL trong thư mục .pyd mà không có add_dll_directory.
            cv2_dir = sp / "cv2"
            if cv2_dir.is_dir() and str(cv2_dir) not in seen_bins:
                seen_bins.add(str(cv2_dir))
                bins.append(cv2_dir)
            # delvewheel pattern: DLLs đặt trong <package>.libs/ (e.g. av.libs/, numpy.libs/)
            # av._core.pyd cần avcodec/avformat DLLs trong av.libs/
            if sp.is_dir():
                for libs_dir in sp.glob("*.libs"):
                    if libs_dir.is_dir() and str(libs_dir) not in seen_bins:
                        seen_bins.add(str(libs_dir))
                        bins.append(libs_dir)
        # Frozen APP (PyInstaller): thêm _MEIPASS/onnxruntime/capi/ và _MEIPASS/torch/lib/
        meipass = getattr(sys, "_MEIPASS", os.environ.get("ZM_AI_TOOL_MEIPASS"))
        if meipass:
            for sub in ("onnxruntime/capi", "torch/lib"):
                d = Path(meipass) / sub
                if d.is_dir() and str(d) not in seen_bins:
                    seen_bins.add(str(d))
                    bins.insert(0, d)  # ưu tiên _MEIPASS trước
        if not bins:
            return
        from pipeline.core.runtime_site import _windows_path_key, prepend_windows_path

        for b in reversed(bins):
            prepend_windows_path(b)
        # Windows: LoadLibrary tìm DLL qua add_dll_directory (PATH đôi khi không đủ).
        # QUAN TRỌNG: lưu handle vào _dll_handles — nếu handle bị GC thì dir bị xóa khỏi search path!
        add_dir = getattr(os, "add_dll_directory", None)
        if add_dir:
            for b in bins:
                try:
                    key = _windows_path_key(str(b))
                    if key not in _dll_handles:
                        handle = add_dir(str(b))
                        if handle is not None:
                            _dll_handles[key] = handle
                except (OSError, FileNotFoundError):
                    pass
        _cuda_dlls_ready = True


def _reset_cuda_dlls() -> None:
    """Force re-scan DLL dirs (gọi sau khi sys.path thay đổi, e.g. .venv-runtime added)."""
    global _cuda_dlls_ready
    with _cuda_lock:
        _cuda_dlls_ready = False


def _patch_rapidocr_onnxruntime_ep() -> None:
    """Stable CUDA options in RapidOCR's limited adapter (CoreML is excluded because
    PaddleOCR text_cls and text_rec fail under CoreMLExecutionProvider in onnxruntime)."""
    try:
        from rapidocr_onnxruntime.utils.infer_engine import OrtInferSession
        if hasattr(OrtInferSession, "_patched_ep_list"):
            return
        orig_get_ep_list = OrtInferSession._get_ep_list

        def patched_get_ep_list(self) -> list:
            ep_list = list(orig_get_ep_list(self))
            for idx, (ep_name, ep_opts) in enumerate(ep_list):
                if ep_name == "CUDAExecutionProvider" and isinstance(ep_opts, dict):
                    ep_opts["cudnn_conv_algo_search"] = "HEURISTIC"
            return ep_list
        OrtInferSession._get_ep_list = patched_get_ep_list
        OrtInferSession._patched_ep_list = True
    except Exception:
        pass


def _rapidocr_labels(*, use_cuda: bool | None = None) -> Any:
    """OCR lỏng hơn cho nhãn 1 chữ / graphic nhỏ (default min_height=30 bỏ sót 行)."""
    # QUAN TRỌNG: thứ tự call bắt buộc:
    # 1. prepare_cuda_dlls() — đăng ký CUDA DLL paths trước mọi onnxruntime import
    # 2. ensure_cv2()       — preload cv2 trước rapidocr (rapidocr import cv2 ngay khi load)
    # 3. import rapidocr   — khi đó cv2 đã trong sys.modules, không trigger bootstrap lại
    prepare_cuda_dlls()
    try:
        from pipeline.core.runtime_site import ensure_cv2
        ensure_cv2()
    except Exception:
        pass
    from rapidocr_onnxruntime import RapidOCR  # type: ignore
    _patch_rapidocr_onnxruntime_ep()

    _limit_onnx_threads()
    gpu_kwargs: dict[str, bool] = (
        _rapidocr_gpu_kwargs()
        if use_cuda is None
        else {
            "det_use_cuda": use_cuda,
            "cls_use_cuda": use_cuda,
            "rec_use_cuda": use_cuda,
            "det_use_dml": False,
            "cls_use_dml": False,
            "rec_use_dml": False,
        }
    )
    return RapidOCR(
        **gpu_kwargs,
        **_ort_threads(
            bool(gpu_kwargs.get("det_use_cuda") or gpu_kwargs.get("det_use_dml"))
        ),
        box_thresh=0.3,
        thresh=0.2,
        text_score=0.3,
        unclip_ratio=2.0,
        min_height=8,
    )


@lru_cache(maxsize=1)
def _rapidocr_gpu_kwargs() -> dict[str, bool]:
    """Chọn ONNX GPU provider thực sự khả dụng: CUDA, rồi DirectML."""
    use_cuda = False
    use_dml = False
    try:
        # prepare_cuda_dlls đã đăng ký cả onnxruntime/capi/ — gọi trước khi import ort
        prepare_cuda_dlls()
        import onnxruntime as ort
        _patch_rapidocr_onnxruntime_ep()

        providers = list(ort.get_available_providers())
        use_cuda = "CUDAExecutionProvider" in providers
        use_dml = not use_cuda and "DmlExecutionProvider" in providers
        try:
            from pipeline.core.app_log import append_log

            append_log(
                f"[ocr-gpu] providers={providers} -> cuda={use_cuda} dml={use_dml} "
                f"coreml={'CoreMLExecutionProvider' in providers}"
            )
        except Exception:
            pass
    except (ImportError, OSError) as e:
        use_cuda = False
        try:
            from pipeline.core.app_log import append_log

            append_log(f"[ocr-gpu] check err={e} -> use_cuda=False")
        except Exception:
            pass
    return {
        "det_use_cuda": use_cuda,
        "cls_use_cuda": use_cuda,
        "rec_use_cuda": use_cuda,
        "det_use_dml": use_dml,
        "cls_use_dml": use_dml,
        "rec_use_dml": use_dml,
    }


def _onnx_provider_available(provider: str) -> bool:
    """Whether this runtime advertises an ONNX execution provider."""
    try:
        import onnxruntime as ort

        return provider in ort.get_available_providers()
    except (ImportError, OSError):
        return False


__all__ = [
    '_ocr_sem',
    '_ocr_sem_n',
    '_cuda_dlls_ready',
    '_cpu_budget',
    '_ocr_pool_workers',
    '_ocr_semaphore',
    '_limit_onnx_threads',
    '_nvidia_bin_dirs',
    'prepare_cuda_dlls',
    '_rapidocr_labels',
    '_rapidocr_gpu_kwargs',
    '_onnx_provider_available',
    'engine_providers',
    'engine_device_label',
]
