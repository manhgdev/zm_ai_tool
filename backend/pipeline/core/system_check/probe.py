"""Probe phần cứng / môi trường: GPU, venv, module import, CUDA, Demucs."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def _safe_subprocess_env() -> dict[str, str]:
    """Use the bounded desktop environment for every runtime probe."""
    try:
        from pipeline.core.runtime_site import subprocess_environment

        return subprocess_environment()
    except Exception:
        return os.environ.copy()


def _which(name: str) -> str | None:
    result = shutil.which(name)
    # Windows PATH entries đôi khi có trailing space/CR → strip để tránh
    # lỗi 'ffprobe ' (có space) → exit 4294967295 (ERROR_INVALID_FUNCTION).
    return result.strip() if result else None



def _run_ver(cmd: list[str], *, timeout: float = 4.0) -> str:
    try:
        out = subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=_safe_subprocess_env(),
        ).strip()
        return out.splitlines()[0][:120] if out else "ok"
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        return f"error: {e}"


def _mod_ok(name: str, *, dist_map: dict[str, list[str]] | None = None) -> tuple[bool, str]:
    try:
        if getattr(sys, "frozen", False) and name in _AI_RUNTIME_MODULES:
            return _runtime_mod_ok(name)
        if importlib.util.find_spec(name) is None:
            return False, "chưa cài"
        module = __import__(name)
        if name == "cv2" and not getattr(module, "VideoCapture", None):
            return False, "cv2 thiếu VideoCapture"
        # Import OK = package usable. Metadata có thể hỏng (~orch dist-info) — đừng coi là thiếu.
        try:
            dists = (dist_map or _pkg_distributions()).get(name) or []
            if dists:
                return True, importlib.metadata.version(dists[0])
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, str(e)[:80]


def _runtime_mod_ok(name: str) -> tuple[bool, str]:
    """Probe import in .venv-runtime — PyInstaller parent cannot import AI wheels."""
    return _runtime_modules_batch_ok([name]).get(name, (False, "unknown"))


def _runtime_modules_batch_ok(names: list[str]) -> dict[str, tuple[bool, str]]:
    """One subprocess for all runtime imports — avoids 8× cold-start on system checks."""
    py = _runtime_python()
    if not py.is_file():
        return {n: (False, "thiếu .venv-runtime") for n in names}
    payload = json.dumps(names)
    script = (
        "import json, sys\n"
        "names = json.loads(sys.argv[1])\n"
        "out = {}\n"
        "for n in names:\n"
        "  try:\n"
        "    m = __import__(n)\n"
        "    if n == 'cv2' and not getattr(m, 'VideoCapture', None):\n"
        "      raise ImportError('cv2 thiếu VideoCapture')\n"
        "    if n == 'transformers':\n"
        "      from transformers import PretrainedConfig, PreTrainedModel, Qwen3Model\n"
        "    if n == 'vieneu':\n"
        "      from vieneu import Vieneu\n"
        "      from vieneu._v3_turbo_engine.inference_v3_turbo import VieNeuTTSv3Turbo\n"
        "    out[n] = [True, 'ok']\n"
        "  except Exception as e:\n"
        "    out[n] = [False, type(e).__name__ + ': ' + str(e)[-1500:]]\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [str(py), "-c", script, payload],
            capture_output=True,
            text=True,
            timeout=120,
            env=_safe_subprocess_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return {n: (False, "timeout") for n in names}
    except OSError as exc:
        return {n: (False, str(exc)) for n in names}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "import fail").strip()[-1500:]
        return {n: (False, err or "import fail") for n in names}
    try:
        lines = (proc.stdout or "").strip().splitlines()
        raw = json.loads(lines[-1] if lines else "{}")
    except json.JSONDecodeError:
        return {n: (False, "probe parse fail") for n in names}
    out: dict[str, tuple[bool, str]] = {}
    for n in names:
        pair = raw.get(n) if isinstance(raw, dict) else None
        if isinstance(pair, list) and len(pair) >= 2:
            out[n] = bool(pair[0]), str(pair[1])
        else:
            out[n] = False, "missing"
    return out


_PKG_DIST: dict[str, list[str]] | None = None


def _pkg_distributions() -> dict[str, list[str]]:
    global _PKG_DIST
    if _PKG_DIST is None:
        _PKG_DIST = importlib.metadata.packages_distributions()
    return _PKG_DIST


_RUNTIME_FAST_DIST = (
    "faster-whisper",
    "rapidocr-onnxruntime",
    "sherpa-onnx",
    "transformers",
    "vieneu",
    "torch",
)
_TORCH_CUDA_CACHE: tuple[float, bool] | None = None
_TORCH_CUDA_TTL = 120.0
_OCR_CUDA_CACHE: tuple[float, tuple[bool, str]] | None = None
_OCR_CUDA_TTL = 60.0
_DEMUCS_PY_CACHE: tuple[float, Path | None] | None = None
_DEMUCS_PY_TTL = 300.0

_AI_RUNTIME_MODULES = (
    "faster_whisper",
    "rapidocr_onnxruntime",
    "PIL",
    "cv2",
    "torch",
    "torchaudio",
    "transformers",
    "vieneu",
    "soundfile",
    "sherpa_onnx",
    "cffi",
)


def _invalidate_probe_caches() -> None:
    global _TORCH_CUDA_CACHE, _OCR_CUDA_CACHE, _demucs_cache, _DEMUCS_PY_CACHE
    _TORCH_CUDA_CACHE = None
    _OCR_CUDA_CACHE = None
    _demucs_cache = None
    _DEMUCS_PY_CACHE = None
    from pipeline.tts.engines import vieneu_frozen
    from pipeline.core import accel

    vieneu_frozen._CUDA_READY = None
    with accel._lock:
        accel._cache.clear()
    accel.accel_label.cache_clear()


def _torch_cuda_ready_cached() -> bool:
    global _TORCH_CUDA_CACHE
    now = time.monotonic()
    if _TORCH_CUDA_CACHE and now - _TORCH_CUDA_CACHE[0] < _TORCH_CUDA_TTL:
        return _TORCH_CUDA_CACHE[1]
    ok = _torch_cuda_ready()
    _TORCH_CUDA_CACHE = (now, ok)
    return ok


def _ocr_cuda_check_cached(*, refresh: bool = False) -> tuple[bool, str]:
    global _OCR_CUDA_CACHE
    now = time.monotonic()
    if not refresh and _OCR_CUDA_CACHE and now - _OCR_CUDA_CACHE[0] < _OCR_CUDA_TTL:
        return _OCR_CUDA_CACHE[1]
    result = _ocr_cuda_check()
    _OCR_CUDA_CACHE = (now, result)
    return result


def _nvidia_present() -> bool:
    from ..accel import _nvidia_smi

    return _nvidia_smi()


def _apple_silicon_runtime() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def _runtime_torch_accel() -> str:
    if _nvidia_present():
        return "cuda"
    if _apple_silicon_runtime():
        return "mac"
    if sys.platform.startswith("linux"):
        try:
            from ..media import detect_device

            if detect_device().get("accel") == "rocm":
                return "rocm"
        except Exception:
            pass
    return "cpu"


def _torch_cuda_ready() -> bool:
    if getattr(sys, "frozen", False):
        try:
            from pipeline.tts.engines.vieneu_frozen import runtime_torch_cuda_ready

            return runtime_torch_cuda_ready()
        except Exception:
            return False
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _runtime_python() -> Path:
    if getattr(sys, "frozen", False):
        home = _zm_ai_tool_home()
        venv = home / ".venv-runtime"
        return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return Path(sys.executable)


def _ocr_python() -> Path:
    if getattr(sys, "frozen", False):
        home = Path(os.environ.get("ZM_AI_TOOL_HOME") or "")
        for venv_name in (".venv-runtime", ".venv-ocr"):
            venv = home / venv_name
            py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            if py.is_file():
                return py
        return home / ".venv-runtime" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return Path(sys.executable)


def _zm_ai_tool_home() -> Path:
    home = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    if home:
        return Path(home)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ZM_AI_TOOL"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ZM_AI_TOOL"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ZM_AI_TOOL"


def _venv_site_packages(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Lib" / "site-packages"
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return venv / "lib" / ver / "site-packages"


def _site_has_dist(sp: Path, prefix: str) -> bool:
    if not sp.is_dir():
        return False
    pre = prefix.lower().replace("_", "-")
    for entry in sp.iterdir():
        name = entry.name.lower().replace("_", "-")
        if name.startswith(pre):
            return True
    return False


def _runtime_venv_fast() -> tuple[bool, str]:
    """Filesystem-only — no torch/whisper import subprocess."""
    venv = _zm_ai_tool_home() / ".venv-runtime"
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not py.is_file():
        return False, "chưa cài"
    sp = _venv_site_packages(venv)
    missing = [n for n in _RUNTIME_FAST_DIST if not _site_has_dist(sp, n)]
    if missing:
        return False, f"thiếu: {', '.join(missing)}"
    return True, "đã cài · .venv-runtime"


def _ocr_venv_fast(accel: str = "cuda") -> tuple[bool, str]:
    py = _ocr_python()
    if not py.is_file():
        return False, "chưa cài"
    sp = _venv_site_packages(py.parent.parent)
    package = "onnxruntime-directml" if accel == "directml" else "onnxruntime-gpu"
    if not _site_has_dist(sp, package):
        return False, f"chưa cài {package}"
    provider = "DirectML" if accel == "directml" else "CUDA"
    return True, f"đã cài · bấm Kiểm tra lại để xác minh {provider}"


def _demucs_venv_fast() -> tuple[bool, str]:
    from pipeline.export.stem import _demucs_py_in, _demucs_root_candidates

    apple = _apple_silicon()
    for root in _demucs_root_candidates():
        py = _demucs_py_in(root)
        if not py.is_file():
            continue
        sp = _venv_site_packages(py.parent.parent)
        if apple:
            if _site_has_dist(sp, "demucs-mlx") or _site_has_dist(sp, "demucs_mlx"):
                return True, "đã cài · Apple Metal"
        elif _site_has_dist(sp, "demucs"):
            return True, "đã cài"
    if apple:
        return False, "thiếu demucs-mlx"
    return False, "thiếu demucs"


def _mod_ok_fast(name: str) -> tuple[bool, str]:
    if importlib.util.find_spec(name) is None:
        return False, "chưa cài"
    return True, "ok"


def _clear_torch_modules() -> None:
    for name in list(sys.modules):
        if name == "torch" or name.startswith("torch."):
            del sys.modules[name]


def _torch_dll_locked() -> bool:
    """True nếu torch đã load trong process này — pip đụng _C.pyd → WinError 5."""
    return "torch" in sys.modules or any(k.startswith("torch.") for k in sys.modules)


def _torch_broken() -> bool:
    """True nếu torch cài xong nhưng import bị lỗi — dấu hiệu file bị mix version."""
    if not importlib.util.find_spec("torch"):
        return False  # chưa cài, không phải broken
    try:
        import torch  # noqa: F401  # pylint: disable=import-outside-toplevel
        return False
    except (AttributeError, ImportError):
        return True


def _ocr_cuda_check() -> tuple[bool, str]:
    """Probe ORT providers — frozen app probes .venv-ocr subprocess (in-process ORT is CPU stub)."""
    if getattr(sys, "frozen", False):
        py = _ocr_python()
        if py.is_file():
            return _ocr_cuda_check_fresh(py)
        return False, "thiếu .venv-ocr"
    try:
        from pipeline.ocr.extract import prepare_cuda_dlls

        prepare_cuda_dlls()
        import onnxruntime as ort

        providers = list(ort.get_available_providers())
        detail = ",".join(providers) if providers else "no providers"
        return "CUDAExecutionProvider" in providers, detail
    except Exception as e:
        return False, str(e)[:160]


def _ocr_directml_check() -> tuple[bool, str]:
    """Verify the DirectML provider in the runtime that will execute OCR."""
    py = _ocr_python() if getattr(sys, "frozen", False) else Path(sys.executable)
    if getattr(sys, "frozen", False) and not py.is_file():
        return False, "thiếu runtime OCR"
    try:
        proc = subprocess.run(
            [
                str(py), "-c",
                "import onnxruntime as ort; print(','.join(ort.get_available_providers()))",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=_safe_subprocess_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:160]
    detail = (proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr.strip())[:500]
    return proc.returncode == 0 and "DmlExecutionProvider" in detail, detail or "no providers"


def _ocr_cuda_check_fresh(python: str | Path = sys.executable) -> tuple[bool, str]:
    """Probe ORT in a new process; this API may still hold the old CPU DLL."""
    try:
        proc = subprocess.run(
            [
                str(python),
                "-c",
                "import onnxruntime as ort; print(','.join(ort.get_available_providers()))",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=_safe_subprocess_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    detail = (proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr.strip())[:500]
    return proc.returncode == 0 and "CUDAExecutionProvider" in detail, detail or "no providers"


def _demucs_venv_python() -> Path | None:
    """Tìm python .venv-demucs — ưu tiên venv đã import được demucs."""
    global _DEMUCS_PY_CACHE
    now = time.monotonic()
    if _DEMUCS_PY_CACHE and now - _DEMUCS_PY_CACHE[0] < _DEMUCS_PY_TTL:
        return _DEMUCS_PY_CACHE[1]

    from pipeline.export.stem import _demucs_py_in, _demucs_root_candidates

    candidates: list[Path] = []
    for root in _demucs_root_candidates():
        py = _demucs_py_in(root)
        if py.is_file():
            candidates.append(py)
    if not candidates:
        _DEMUCS_PY_CACHE = (now, None)
        return None

    def _import_ok(exe: Path) -> bool:
        try:
            r = subprocess.run(
                [str(exe), "-c", "import demucs, soundfile"],
                capture_output=True,
                timeout=25,
                env=_safe_subprocess_env(),
            )
            if r.returncode == 0:
                return True
            r2 = subprocess.run(
                [str(exe), "-c", "import demucs_mlx, soundfile"],
                capture_output=True,
                timeout=25,
                env=_safe_subprocess_env(),
            )
            return r2.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    found: Path | None = None
    for py in candidates:
        if _import_ok(py):
            found = py
            break
    if found is None:
        found = candidates[0]
    _DEMUCS_PY_CACHE = (now, found)
    return found


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def _demucs_check_uncached() -> tuple[bool, str]:
    """Demucs sẵn sàng.

    - Apple Silicon → demucs-mlx (Metal)
    - NVIDIA → torch CUDA
    - Không GPU → torch CPU cũng ok
    """
    want_cuda = bool(_which("nvidia-smi"))
    want_mlx = _apple_silicon()
    py = _demucs_venv_python()
    if not py:
        return False, "chưa có backend/.venv-demucs (bấm Cài đặt)"

    if want_mlx:
        try:
            r = subprocess.run(
                [
                    str(py),
                    "-c",
                    (
                        "import demucs_mlx, soundfile; "
                        "import importlib.metadata as m; "
                        "print(m.version('demucs-mlx'))"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=45,
                env=_safe_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)[:160]
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "import fail").strip()[-160:]
            return False, err or "chưa có demucs-mlx (Apple GPU)"
        ver = (r.stdout or "").strip() or "?"
        return True, f"mlx · Apple Silicon · demucs-mlx {ver}"

    try:
        r = subprocess.run(
            [
                str(py),
                "-c",
                (
                    "import demucs, soundfile, torch; "
                    "d=('cuda' if torch.cuda.is_available() else "
                    "('mps' if getattr(torch.backends,'mps',None) and torch.backends.mps.is_available() else 'cpu')); "
                    "n=(torch.cuda.get_device_name(0) if d=='cuda' else ('Apple GPU' if d=='mps' else 'CPU')); "
                    "print(f'{d}|{n}|{torch.__version__}')"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            env=_safe_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:160]
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "No module named 'demucs'" in err or "No module named \"demucs\"" in err:
            return False, "chưa cài demucs trong .venv-demucs (bấm Cài đặt)"
        if "No module named 'torch'" in err:
            return False, "chưa cài torch trong .venv-demucs (bấm Cài đặt)"
        short = err.splitlines()[-1][:160] if err else "import demucs/torch thất bại"
        return False, short
    parts = (r.stdout or "").strip().split("|")
    device = parts[0] if parts else "?"
    name = parts[1] if len(parts) > 1 else "?"
    ver = parts[2] if len(parts) > 2 else "?"
    detail = f"{device} · {name} · torch {ver}"
    if want_cuda and device != "cuda":
        return False, f"đang CPU (chậm) — {detail}"
    return True, detail


_DEMUCS_CACHE_TTL = 300.0
_demucs_cache: tuple[float, tuple[bool, str]] | None = None
_demucs_cache_lock = threading.Lock()


def _demucs_check(*, refresh: bool = False) -> tuple[bool, str]:
    global _demucs_cache, _DEMUCS_PY_CACHE
    with _demucs_cache_lock:
        if refresh:
            # The venv may have been created by the installer after a cached miss.
            _DEMUCS_PY_CACHE = None
        now = time.monotonic()
        if not refresh and _demucs_cache and now - _demucs_cache[0] < _DEMUCS_CACHE_TTL:
            return _demucs_cache[1]
        result = _demucs_check_uncached()
        _demucs_cache = (now, result)
        return result
