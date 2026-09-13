"""Bootstrap AI packages from .venv-runtime under PyInstaller frozen builds.

PyInstaller --exclude-module leaves hollow entries in sys.modules; find_spec still
passes so Thiết lập báo OK while VieNeu fails on ``from transformers import …``.
Install a meta-path finder so runtime-root imports resolve from site-packages.
"""
from __future__ import annotations

import ntpath
import os
import sys
import threading
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
from pathlib import Path

# add_dll_directory() trả handle — phải giữ sống hoặc GC sẽ xóa dir khỏi DLL search path!
_dll_handles: dict[str, object] = {}
_dll_search_lock = threading.RLock()


@contextmanager
def external_process_dll_search(executable: str):
    """Do not let an external Python inherit PyInstaller's DLL directory.

    The bootloader calls SetDllDirectory(_MEIPASS). That setting is inherited
    by child processes and can bind Torch to the bundle's incompatible DLLs.
    Serialize the temporary process-global reset around Popen, then restore.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        yield
        return
    with _dll_search_lock:
        if _windows_path_key(executable) == _windows_path_key(sys.executable):
            yield
            return
        import ctypes

        kernel = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetDllDirectoryW(len(buffer), buffer)
        if length >= len(buffer):
            raise OSError("GetDllDirectoryW buffer too small")
        previous = buffer.value
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError()
        try:
            yield
        finally:
            kernel.SetDllDirectoryW(previous or None)

# Windows environment blocks are limited to 32,767 characters. Keep margin
# for process-spawn wrappers and preserve both app/runtime and system entries
# when an inherited PATH is oversized.
_WINDOWS_PATH_LIMIT = 32_700

_RUNTIME_ROOTS = frozenset(
    {
        "accelerate",
        "ctranslate2",
        # ponytail: không hook cv2 — OpenCV bootstrap tự import lại "cv2" lần 2;
        # meta-path sẽ chạy lại __init__ → "recursion is detected".
        "datasets",
        "faster_whisper",
        "huggingface_hub",
        "numpy",
        "onnxruntime",
        "PIL",
        "rapidocr_onnxruntime",
        "safetensors",
        "sea_g2p",
        "soundfile",
        "soxr",
        "tokenizers",
        "torch",
        "torchaudio",
        "transformers",
        "vieneu",
        "yaml",
    }
)

# Stub/hollow modules to drop from sys.modules (may include packages not in meta-path).
_PURGE_ROOTS = _RUNTIME_ROOTS | frozenset({"cv2"})

_AI_PRELOAD = (
    "safetensors",
    "huggingface_hub",
    "tokenizers",
    "torch",
    "transformers",
)


class _RuntimeSiteFinder:
    """Redirect excluded AI imports to runtime venv before PyInstaller frozen importer."""

    def __init__(self, site: str) -> None:
        self._site = site

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        root = fullname.split(".", 1)[0]
        if root not in _RUNTIME_ROOTS:
            return None
        # ponytail: only top-level — PathFinder on dotted names can resolve
        # huggingface_hub.utils.tqdm → site-packages/tqdm (wrong module).
        if "." in fullname:
            return None
        return PathFinder.find_spec(fullname, [self._site])


def runtime_site_packages() -> Path | None:
    """Site-packages dir for the desktop runtime venv (frozen) or None in dev."""
    if not getattr(sys, "frozen", False):
        return None
    home = (os.environ.get("VIDEO_CLONE_HOME") or "").strip()
    if not home:
        return None
    venv = Path(home) / ".venv-runtime"
    if sys.platform == "win32":
        site = venv / "Lib" / "site-packages"
    else:
        site = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    return site if site.is_dir() else None


def _windows_path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value.strip().strip('"')))


def sanitize_windows_path(value: str) -> str:
    """Remove duplicates and bound a Windows PATH before DLL/process calls."""
    if sys.platform != "win32":
        return value
    seen: set[str] = set()
    parts: list[str] = []
    for part in value.split(";"):
        if not part.strip():
            continue
        key = _windows_path_key(part)
        if key in seen:
            continue
        seen.add(key)
        parts.append(part)
    joined = ";".join(parts)
    if len(joined) <= _WINDOWS_PATH_LIMIT:
        return joined

    # Keep a prefix (bundle/runtime DLLs) and suffix (system tools). Do not
    # truncate individual entries: a shortened directory is never usable.
    head: list[str] = []
    head_len = 0
    last = parts[-1] if parts else ""
    for part in parts:
        next_len = len(part) + (1 if head else 0)
        if head_len + next_len + (1 if part != last else 0) + len(last) > _WINDOWS_PATH_LIMIT:
            break
        head.append(part)
        head_len += next_len
    tail: list[str] = []
    for part in reversed(parts[len(head):]):
        candidate = [part, *tail]
        size = head_len + (1 if head else 0) + len(";".join(candidate))
        if size > _WINDOWS_PATH_LIMIT:
            break
        tail.insert(0, part)
    return ";".join([*head, *tail])


def sanitize_process_environment(
    env: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Normalize the current process environment before Windows spawns.

    Frozen desktop launches inherit PATH from Explorer, shell integrations and
    package managers.  Keep the process itself bounded as well as child
    environments; otherwise native imports (notably torch) can fail before a
    child has a chance to receive ``subprocess_environment``.
    """
    target = env if env is not None else os.environ
    if sys.platform == "win32":
        target["PATH"] = sanitize_windows_path(target.get("PATH", ""))
    return target


def subprocess_environment(
    overrides: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """Copy the process environment and bound Windows PATH before spawning."""
    env = dict(sanitize_process_environment(os.environ.copy()))
    if getattr(sys, "frozen", False) or env.get("VIDEO_CLONE_MEIPASS"):
        # External Python owns its stdlib/site-packages, not the launcher's
        # Python installation. Preserve app paths explicitly added by workers.
        for name in ("PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"):
            env.pop(name, None)
        env["PYTHONNOUSERSITE"] = "1"
    for name, value in (overrides or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = str(value)
    sanitize_process_environment(env)
    return env


def prepend_windows_path(path: str | Path, env: MutableMapping[str, str] = os.environ) -> None:
    """Prepend one normalized PATH entry without growing PATH on repeat calls."""
    value = str(path)
    if sys.platform != "win32":
        env["PATH"] = value + os.pathsep + env.get("PATH", "")
        return
    target = _windows_path_key(value)
    rest = [
        part
        for part in sanitize_windows_path(env.get("PATH", "")).split(";")
        if part and _windows_path_key(part) != target
    ]
    env["PATH"] = sanitize_windows_path(";".join([value, *rest]))


def prepare_runtime_torch_dlls(site: Path) -> None:
    """Add torch/lib to DLL search path (frozen + external torch wheels)."""
    torch_lib = site / "torch" / "lib"
    if not torch_lib.is_dir():
        return
    # Keep the venv path as supplied.  Resolving junctions can expand a short
    # user path into a long network/store path on Windows (and triggers
    # WinError 206 inside torch's own DLL bootstrap).
    lib_s = str(torch_lib)
    prepend_windows_path(lib_s)
    add_dir = getattr(os, "add_dll_directory", None)
    if add_dir and sys.platform == "win32":
        try:
            key = _windows_path_key(lib_s)
            if key not in _dll_handles:
                handle = add_dir(lib_s)
                if handle is not None:
                    _dll_handles[key] = handle
        except OSError:
            pass


def is_windows_path_too_long_error(error: object) -> bool:
    text = str(error).lower()
    return (
        getattr(error, "winerror", None) == 206
        or getattr(error, "errno", None) == 206
        or "winerror 206" in text
        or "filename or extension is too long" in text
    )


def _purge_external_modules(site: Path) -> None:
    site_s = str(site).replace("\\", "/")
    for key in list(sys.modules):
        root = key.split(".", 1)[0]
        if root not in _PURGE_ROOTS:
            continue
        mod = sys.modules.get(key)
        if mod is None:
            sys.modules.pop(key, None)
            continue
        mod_file = (getattr(mod, "__file__", "") or "").replace("\\", "/")
        if mod_file and site_s in mod_file:
            continue
        sys.modules.pop(key, None)
    # OpenCV leftover after failed/partial import
    if hasattr(sys, "OpenCV_LOADER"):
        try:
            delattr(sys, "OpenCV_LOADER")
        except Exception:
            pass


def _sanitize_cv2_sys_path() -> None:
    """Drop path entries that break OpenCV bootstrap (recursion).

    Known killers from app.log:
    - .../site-packages/cv2  (package dir, not parent site-packages)
    - .../.venv-ocr/...      (OCR CUDA venv before runtime)
    """
    cleaned: list[str] = []
    for p in sys.path:
        if not p:
            continue
        norm = p.replace("\\", "/").rstrip("/").lower()
        if norm.endswith("/cv2"):
            continue
        if "/.venv-ocr/" in f"/{norm}/" or norm.endswith("/.venv-ocr/lib/site-packages"):
            continue
        # Windows short / mixed case site-packages\\cv2
        if norm.replace("/", "\\").endswith("\\cv2"):
            continue
        cleaned.append(p)
    sys.path[:] = cleaned


def prepare_cv2_import_path(site: Path | None = None) -> str | None:
    """Force runtime site-packages to sys.path[0]; strip ocr/cv2 traps. Returns site str."""
    root = site or runtime_site_packages()
    _sanitize_cv2_sys_path()
    if not root or not root.is_dir():
        return None
    site_s = str(root)
    while site_s in sys.path:
        sys.path.remove(site_s)
    sys.path.insert(0, site_s)
    _sanitize_cv2_sys_path()
    return site_s


def ensure_cv2():
    """Import OpenCV an toàn (frozen + dev). Trả module hoặc raise ImportError.

    OpenCV bootstrap re-import ``cv2`` khi path[0] là site khác (.venv-ocr)
    hoặc khi ``.../site-packages/cv2`` nằm trên sys.path → recursion → APP tắt.
    """
    existing = sys.modules.get("cv2")
    if existing is not None and getattr(existing, "VideoCapture", None):
        return existing

    root = runtime_site_packages()
    # Dev: path sạch rồi import
    if not getattr(sys, "frozen", False) or not root or not root.is_dir():
        _sanitize_cv2_sys_path()
        import cv2 as _cv2

        return _cv2

    prepare_cv2_import_path(root)

    if hasattr(sys, "OpenCV_LOADER"):
        try:
            delattr(sys, "OpenCV_LOADER")
        except Exception:
            pass
    for key in list(sys.modules):
        if key == "cv2" or key.startswith("cv2."):
            sys.modules.pop(key, None)

    # Re-assert path immediately before import (other code may mutate sys.path)
    prepare_cv2_import_path(root)
    import cv2 as _cv2  # noqa: F401

    # OpenCV bootstrap có thể thêm site-packages/cv2 vào path; dọn lại.
    _sanitize_cv2_sys_path()

    if not getattr(_cv2, "VideoCapture", None):
        raise ImportError("cv2 loaded without VideoCapture")
    return _cv2


def preload_cv2() -> None:
    """Import OpenCV once while runtime site-packages is sys.path[0]."""
    if not getattr(sys, "frozen", False):
        return
    try:
        ensure_cv2()
        try:
            from pipeline.core.app_log import append_log

            append_log("[cv2] preload ok", also_print=False)
        except Exception:
            pass
    except (ModuleNotFoundError, ImportError) as e:
        # Bình thường ở lần mở đầu tiên trước khi APP cài gói AI; không ghi traceback giả như crash.
        try:
            from pipeline.core.app_log import append_log

            append_log(f"[cv2] chưa sẵn sàng, chờ cài gói AI: {e}", also_print=False)
        except Exception:
            pass
    except Exception as e:
        try:
            from pipeline.core.app_log import append_exception

            append_exception("[cv2] preload failed", e)
        except Exception:
            pass


def install_runtime_meta_path(site: Path | None = None, *, gpu_in_process: bool = True) -> None:
    """Hook meta-path so runtime venv wins over PyInstaller exclude stubs (frozen only).

    gpu_in_process=False: process UI/API — không nạp CUDA/cv2. GPU ở worker .venv-runtime.
    """
    if not getattr(sys, "frozen", False):
        return
    sanitize_process_environment()
    root = site or runtime_site_packages()
    if not root or not root.is_dir():
        return
    prepare_cv2_import_path(root)
    if gpu_in_process:
        prepare_runtime_torch_dlls(root)
        try:
            from pipeline.ocr.extract_parts.runtime import _reset_cuda_dlls, prepare_cuda_dlls
            _reset_cuda_dlls()
            prepare_cuda_dlls()
        except Exception:
            pass
    _purge_external_modules(root)
    site_s = str(root)
    if not any(isinstance(f, _RuntimeSiteFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _RuntimeSiteFinder(site_s))
    if gpu_in_process:
        preload_cv2()


def _load_from_site(name: str, site: Path) -> None:
    spec = PathFinder.find_spec(name, [str(site)])
    if not spec or not spec.loader:
        raise ModuleNotFoundError(name)
    mod = module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)


def ensure_runtime_import(name: str, *, site: Path | None = None) -> None:
    """Import module from runtime venv; frozen builds install meta-path + purge stubs."""
    install_runtime_meta_path(site)
    if not getattr(sys, "frozen", False):
        __import__(name)
        return
    root = site or runtime_site_packages()
    if not root or not root.is_dir():
        raise ModuleNotFoundError(name)
    existing = sys.modules.get(name)
    if existing is not None:
        mod_file = (getattr(existing, "__file__", "") or "").replace("\\", "/")
        if mod_file and str(root).replace("\\", "/") in mod_file:
            return
    _purge_external_modules(root)
    if name == "torch":
        __import__("torch")
        return
    _load_from_site(name, root)


def bootstrap_ai_runtime(site: Path | None = None) -> None:
    """Preload HF/transformers stack before VieNeu model code runs (frozen only)."""
    if not getattr(sys, "frozen", False):
        return
    root = site or runtime_site_packages()
    if not root or not root.is_dir():
        return
    install_runtime_meta_path(root)
    for mod in _AI_PRELOAD:
        try:
            ensure_runtime_import(mod, site=root)
        except Exception:
            pass


def verify_transformers_ok() -> tuple[bool, str]:
    """Exercise lazy model imports in the interpreter that actually runs AI."""
    if getattr(sys, "frozen", False):
        from .system_check.probe import _runtime_mod_ok

        return _runtime_mod_ok("transformers")
    try:
        install_runtime_meta_path()
        ensure_runtime_import("torch")
        ensure_runtime_import("transformers")
        import importlib

        import transformers
        from transformers import PreTrainedModel, Qwen3Model  # noqa: F401

        cfg = importlib.import_module("transformers.configuration_utils")
        if not (getattr(cfg, "PretrainedConfig", None) or getattr(cfg, "PreTrainedConfig", None)):
            raise ImportError("missing PretrainedConfig")
        return True, str(getattr(transformers, "__version__", "") or "ok")
    except Exception as e:
        return False, str(e)[:160]
