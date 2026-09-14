"""Cài đặt gói AI runtime: pip/uv install torch, Whisper, OCR, VieNeu, Demucs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from .checks import _ai_runtime_detail, _invalidate_checks_cache
from .probe import (
    _AI_RUNTIME_MODULES,
    _apple_silicon,
    _clear_torch_modules,
    _demucs_check,
    _mod_ok,
    _nvidia_present,
    _ocr_cuda_check,
    _ocr_cuda_check_fresh,
    _ocr_directml_check,
    _ocr_venv_fast,
    _runtime_torch_accel,
    _runtime_mod_ok,
    _runtime_modules_batch_ok,
    _runtime_venv_fast,
    _torch_broken,
    _torch_cuda_ready,
    _torch_cuda_ready_cached,
    _torch_dll_locked,
    _zm_ai_tool_home,
    _venv_site_packages,
    _which,
)

# Callback do routes/system.py gán khi start install job — nhận 1 dòng log pip.
# ponytail: Callable thay vì import tránh circular dep.
_install_log_fn: Any = None  # Callable[[str], None] | None
_install_progress_fn: Any = None  # Callable[[int, str], None] | None


def _report_install(progress: int | float, message: str) -> None:
    if _install_progress_fn is None:
        return
    try:
        _install_progress_fn(progress, message)
    except Exception:
        pass


def _runtime_subprocess_env() -> dict[str, str]:
    """Environment for pip/uv probes, including Windows PATH hardening."""
    from ..runtime_site import subprocess_environment

    return subprocess_environment({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        # Tăng tốc uv: 10 kết nối song song, timeout dài hơn, không compile .pyc lúc cài
        "UV_CONCURRENT_DOWNLOADS": "10",
        "UV_HTTP_TIMEOUT": "300",
        "UV_COMPILE_BYTECODE": "0",
        "UV_NO_PROGRESS": "false",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    })


def _clean_corrupted_dists(site: Path | None = None) -> None:
    """Xóa các thư mục ~* do pip để lại khi uninstall bị ngắt giữa chừng (WinError 32).
    Ví dụ: ~orch (từ torch), ~okenizers (từ tokenizers).
    """
    if site is None:
        try:
            import site as _site
            dirs = _site.getsitepackages() or []
            site = Path(dirs[-1]) if dirs else None
        except Exception:
            return
    if not site or not site.is_dir():
        return
    for p in site.iterdir():
        if p.name.startswith("~") and (p.is_dir() or p.suffix in (".dist-info", ".data")):
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    # Dọn dummy package 'perth' (threading helper bị cài nhầm thay vì resemble-perth, gây lỗi warning watermarker ở VieNeu)
    for name in ("perth", "perth-1.0.0.dist-info"):
        target = site / name
        if target.exists():
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except Exception:
                pass


def _pip_stream(
    cmd: list[str],
    *,
    timeout: float = 1800,
    idle_timeout: float | None = 900,
    progress: tuple[int, int, str] | None = None,
) -> subprocess.CompletedProcess:
    """Stream installer output without waiting forever on a silent Windows pipe."""
    import queue as _queue
    output = ""
    q: _queue.Queue[str | None] = _queue.Queue()
    env = _runtime_subprocess_env()

    def _reader(stdout) -> None:  # chạy trong thread riêng
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while raw := stdout.read(4096):
                chunk = decoder.decode(raw)
                if chunk:
                    q.put(chunk)
            tail = decoder.decode(b"", final=True)
            if tail:
                q.put(tail)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)  # sentinel

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )
    assert proc.stdout
    t = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
    t.start()
    last_activity = time.monotonic()
    deadline = last_activity + timeout
    exit_seen: float | None = None
    log_pending = ""
    progress_scan_tail = ""
    progress_value = progress[0] if progress else 0
    if progress:
        _report_install(progress_value, f"Đang cài {progress[2]}… / Installing {progress[2]}…")

    timed_out = ""
    while True:
        now = time.monotonic()
        # Check the hard deadline even while a spinner keeps stdout busy.
        # Otherwise a continuously non-empty queue can bypass q.get()'s
        # timeout forever despite the child making no meaningful progress.
        if now >= deadline:
            timed_out = (
                f"INSTALL_TIMEOUT: Cài đặt vượt quá {round(timeout / 60)} phút. / "
                f"Installation exceeded {round(timeout / 60)} minutes."
            )
            break
        try:
            chunk = q.get(timeout=min(0.5, max(0.01, deadline - now)))
        except _queue.Empty:
            now = time.monotonic()
            if now >= deadline:
                timed_out = (
                    f"INSTALL_TIMEOUT: Cài đặt vượt quá {round(timeout / 60)} phút. / "
                    f"Installation exceeded {round(timeout / 60)} minutes."
                )
                break
            if idle_timeout is not None and now - last_activity >= idle_timeout:
                timed_out = (
                    f"INSTALL_IDLE_TIMEOUT: Không nhận được dữ liệu trong "
                    f"{round(idle_timeout / 60)} phút; đã dừng tiến trình bị treo. / "
                    f"No installer activity for {round(idle_timeout / 60)} minutes; "
                    "the stalled process was stopped."
                )
                break
            if proc.poll() is not None:
                exit_seen = exit_seen or now
                # A grandchild can accidentally retain stdout after uv exits.
                if now - exit_seen >= 3:
                    break
            continue
        if chunk is None:
            break

        last_activity = time.monotonic()
        output = (output + chunk)[-1_000_000:]
        if _install_log_fn is not None:
            try:
                log_parts = (log_pending + chunk).replace("\r", "\n").split("\n")
                log_pending = log_parts.pop()
                if log_parts:
                    _install_log_fn("\n".join(log_parts) + "\n")
            except Exception:
                pass
        if progress:
            clean = re.sub(
                r"\x1b\[[0-9;?]*[ -/]*[@-~]",
                "",
                progress_scan_tail + chunk,
            ).lower()
            progress_scan_tail = clean[-512:]
            markers = (
                ("successfully installed", 0.98, "Đã cài xong / Installed"),
                ("uninstalled ", 0.95, "Đã gỡ gói cũ / Old package removed"),
                ("installed ", 0.95, "Đã cài xong / Installed"),
                ("prepared ", 0.78, "Đã chuẩn bị gói / Packages prepared"),
                ("installing collected packages", 0.82, "Đang ghi gói / Installing files"),
                ("downloaded ", 0.68, "Đã tải gói / Packages downloaded"),
                ("downloading ", 0.22, "Đang tải gói / Downloading packages"),
                ("collecting ", 0.12, "Đang phân tích gói / Resolving packages"),
                ("resolved ", 0.10, "Đã phân tích gói / Packages resolved"),
            )
            for marker, fraction, status in markers:
                if marker in clean:
                    start, end, label = progress
                    progress_value = max(progress_value, round(start + (end - start) * fraction))
                    _report_install(progress_value, f"{status}: {label}")
                    break

            # pip/uv progress bars use either ``42%`` or ``120/950 MiB``.
            # Map the real byte/download ratio to the download portion of this
            # stage; package extraction and installation still need headroom.
            ratio: float | None = None
            percent_matches = re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%", clean)
            if percent_matches:
                pct = float(percent_matches[-1])
                if 0 <= pct <= 100:
                    ratio = pct / 100
            byte_matches = re.findall(
                r"(\d+(?:\.\d+)?)\s*([kmgt]?i?b)?\s*/\s*"
                r"(\d+(?:\.\d+)?)\s*([kmgt]?i?b)",
                clean,
            )
            if byte_matches:
                current, current_unit, total, total_unit = byte_matches[-1]
                powers = {"b": 0, "kb": 1, "kib": 1, "mb": 2, "mib": 2,
                          "gb": 3, "gib": 3, "tb": 4, "tib": 4}
                current_bytes = float(current) * (1024 ** powers[current_unit or total_unit])
                total_bytes = float(total) * (1024 ** powers[total_unit])
                if total_bytes > 0:
                    ratio = max(0.0, min(1.0, current_bytes / total_bytes))
            if ratio is not None:
                start, end, label = progress
                value = round(start + (end - start) * (0.22 + 0.46 * ratio))
                if value > progress_value:
                    progress_value = value
                    _report_install(
                        progress_value,
                        f"Đang tải / Downloading {label}: {round(ratio * 100)}%",
                    )

    if timed_out:
        from ..jobs import kill_process_tree

        kill_process_tree(proc)
    termination_error = ""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        from ..jobs import kill_process_tree

        kill_process_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            termination_error = (
                "INSTALL_TERMINATION_FAILED: Không thể dừng tiến trình cài đặt. / "
                "The installer process could not be stopped."
            )
    finally:
        if log_pending and _install_log_fn is not None:
            try:
                _install_log_fn(log_pending + "\n")
            except Exception:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        t.join(timeout=3)

    if timed_out:
        tail = re.sub(r"\r+", "\n", output).strip()[-3000:]
        raise RuntimeError(f"{timed_out}\n{tail}" if tail else timed_out)
    if termination_error:
        raise RuntimeError(termination_error)
    if progress and proc.returncode == 0:
        _report_install(progress[1], f"Đã hoàn tất {progress[2]} / Finished {progress[2]}")
    return subprocess.CompletedProcess(cmd, proc.returncode, output, "")


_AI_RUNTIME_PACKAGES = (
    "faster-whisper>=1.1.0",
    "rapidocr-onnxruntime>=1.3.20",
    "pillow",
    "opencv-python-headless",
    "huggingface-hub>=0.34",   # bỏ <1.0 — hub 1.x đang có, không cần downgrade
    "pyyaml",
    "sea-g2p",
    "soundfile",
    "sherpa-onnx>=1.12.0",
    "sherpa-onnx-bin>=1.12.0",
    "cffi",
    "soxr",
    "httpx",
    "tokenizers",
    "transformers>=4.46.0",
)

# 3 nhóm riêng — mỗi nhóm cài 1 pip call với --no-deps, có header log riêng.
_PKG_WHISPER = ("faster-whisper>=1.1.0", "soundfile", "cffi", "pycparser", "soxr", "tokenizers")
_PKG_DIARIZATION = ("sherpa-onnx>=1.12.0", "sherpa-onnx-bin>=1.12.0", "soundfile")
_PKG_OCR     = ("rapidocr-onnxruntime>=1.3.20", "pillow", "opencv-python-headless<5.0")
_PKG_VIENEU  = (
    "huggingface-hub>=0.34", "httpx", "pyyaml",
    "sea-g2p",
    # transformers cài riêng --no-deps (conflict tokenizers)
)
_VIENEU_PACKAGE = "vieneu>=3.2.0"
_TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu124"
_TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_TORCH_ROCM_INDEX = "https://download.pytorch.org/whl/rocm6.2"
# onnxruntime-gpu cho CUDA 12.x (torch cu124) — PyPI chính thức có sẵn wheel CUDA 12 cho 1.20.x
_ORT_GPU_PKG = "onnxruntime-gpu>=1.20.0,<1.21.0"  # CUDA 12/cuDNN 9; meets VieNeu's >=1.20 floor (1.20.0, 1.20.2)
_ORT_DIRECTML_PKG = "onnxruntime-directml"
_SHERPA_CUDA_SPEC = "sherpa-onnx==1.13.5+cuda12.cudnn9"
_SHERPA_CUDA_INDEX = "https://k2-fsa.github.io/sherpa/onnx/cuda.html"

# Frozen APP packages live in a user-owned runtime venv.  Only repair modules
# that fail to import; upgrading every package on each click makes uv reject
# locally-installed wheels that have no newer index release.
_FROZEN_PACKAGE_MODULES: dict[str, tuple[str, ...]] = {
    "faster-whisper>=1.1.0": ("faster_whisper",),
    "rapidocr-onnxruntime>=1.3.20": ("rapidocr_onnxruntime",),
    "pillow": ("PIL",),
    "opencv-python-headless": ("cv2",),
    "huggingface-hub>=0.34": ("transformers", "vieneu"),
    "transformers>=4.46.0": ("transformers",),
    "tokenizers": ("transformers",),
    "soundfile": ("soundfile",),
    "cffi": ("cffi",),
    "sherpa-onnx>=1.12.0": ("sherpa_onnx",),
    "sherpa-onnx-bin>=1.12.0": ("sherpa_onnx",),
    "httpx": ("vieneu",),
    "pyyaml": ("vieneu",),
    "sea-g2p": ("vieneu",),
    "soxr": ("vieneu",),
}


def _frozen_runtime_missing_modules() -> list[str]:
    """Return runtime modules that fail a real import probe, not dist metadata."""
    status = _runtime_modules_batch_ok(list(_AI_RUNTIME_MODULES))
    return [name for name in _AI_RUNTIME_MODULES if not status.get(name, (False, "missing"))[0]]


def _frozen_runtime_package_specs(missing: list[str]) -> list[str]:
    """Map failed modules to a stable, de-duplicated package install list."""
    wanted = {
        package
        for package, modules in _FROZEN_PACKAGE_MODULES.items()
        if any(module in missing for module in modules)
    }
    packages = [package for package in _AI_RUNTIME_PACKAGES if package in wanted]
    if "transformers" in missing or "vieneu" in missing:
        # VieNeu SDK recommends 4.57.6. Resolve its declared hub/tokenizers
        # dependencies together; --no-deps was hiding incompatible installs.
        packages = [p for p in packages if not p.startswith(("transformers", "huggingface-hub", "tokenizers"))]
        packages += ["transformers==4.57.6"]
    if "vieneu" in missing:
        # Inference dependencies omitted by our intentional --no-deps SDK
        # install (which avoids pulling the upstream Gradio/legacy UI stack).
        packages += ["librosa>=0.11.0", "safetensors>=0.4.3"]
    return packages


def _verify_frozen_runtime_install() -> None:
    """Fail installation on broken imports or unusable CUDA, never a green badge."""
    _invalidate_checks_cache()
    statuses = _runtime_modules_batch_ok(list(_AI_RUNTIME_MODULES))
    errors = [f"{name}: {detail}" for name, (ok, detail) in statuses.items() if not ok]
    if errors:
        detail = "\n".join(errors)
        if "WinError 126" in detail or "WinError 127" in detail:
            detail += (
                "\nKiểm tra Microsoft Visual C++ Redistributable x64 và driver NVIDIA. / "
                "Check Microsoft Visual C++ Redistributable x64 and the NVIDIA driver."
            )
        raise RuntimeError("AI_RUNTIME_IMPORT_FAILED: Kiểm tra import thất bại / Import check failed:\n" + detail)
    if _nvidia_present() and not _torch_cuda_ready():
        raise RuntimeError(
            "AI_RUNTIME_CUDA_UNAVAILABLE: Đã cài Torch nhưng CUDA chưa chạy được. "
            "Kiểm tra driver NVIDIA và GPU được cấp cho máy ảo; không tự hạ xuống CPU. / "
            "Torch is installed but CUDA cannot run. Check the NVIDIA driver and VM GPU passthrough; no silent CPU fallback."
        )
    ort_accel = _runtime_ort_accel()
    if ort_accel == "cuda":
        ok, detail = _ocr_cuda_check()
        if not ok:
            raise RuntimeError(
                "AI_RUNTIME_ORT_CUDA_UNAVAILABLE: ONNX Runtime đã cài nhưng CUDA provider "
                f"chưa chạy được: {detail}. Kiểm tra driver NVIDIA/CUDA rồi thử lại. / "
                "ONNX Runtime is installed but its CUDA provider is unavailable. "
                "Check the NVIDIA driver/CUDA runtime and retry."
            )
    elif ort_accel == "directml":
        ok, detail = _ocr_directml_check()
        if not ok:
            raise RuntimeError(
                "AI_RUNTIME_ORT_DIRECTML_UNAVAILABLE: ONNX Runtime DirectML chưa chạy được: "
                f"{detail}. / ONNX Runtime DirectML is unavailable after installation."
            )


def _sherpa_cuda_ready(python: Path | str = sys.executable) -> bool:
    if _runtime_ort_accel() != "cuda":
        return True
    try:
        proc = subprocess.run(
            [str(python), "-c", "import sherpa_onnx; print(sherpa_onnx.__version__)"],
            capture_output=True, text=True, timeout=30, env=_runtime_subprocess_env(),
        )
        return proc.returncode == 0 and ("+cuda" in proc.stdout.lower())
    except Exception:
        return False


def _install_sherpa_cuda(python: Path | str, uv: str | None = None) -> None:
    if _runtime_ort_accel() != "cuda" or _sherpa_cuda_ready(python):
        return
    if _install_log_fn:
        _install_log_fn("\n=== Speaker diarization GPU (Sherpa CUDA 12) ===\n")
    cmd = (
        [
            uv, "pip", "install", "--python", str(python),
            "--force-reinstall", "--no-deps", _SHERPA_CUDA_SPEC,
            "-f", _SHERPA_CUDA_INDEX,
        ]
        if uv else
        [
            str(python), "-m", "pip", "install", "--force-reinstall",
            "--no-deps", _SHERPA_CUDA_SPEC, "-f", _SHERPA_CUDA_INDEX,
        ]
    )
    proc = _pip_stream(
        cmd,
        timeout=1800,
        progress=(68, 76, "Sherpa CUDA"),
    )
    if proc.returncode:
        raise RuntimeError("[Sherpa CUDA] " + (proc.stderr or proc.stdout)[-3000:])


def _runtime_ort_accel() -> str:
    try:
        from ..media import detect_device

        return str(detect_device().get("accel") or "cpu")
    except Exception:
        return "cuda" if _nvidia_present() else "cpu"
# ponytail: chỉ dùng trong dev mode — map module → package spec để chỉ
# cài đúng gói thiếu (không --upgrade cả lô). Frozen dùng base_cmd đủ bộ.
_MODULE_TO_PACKAGE: dict[str, str] = {
    "faster_whisper": "faster-whisper>=1.1.0",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime>=1.3.20",
    "PIL": "pillow",
    "cv2": "opencv-python-headless",
    "torch": "torch",
    "torchaudio": "torchaudio",
    "transformers": "transformers>=4.46.0",
    "vieneu": "vieneu>=3.2.0",
    "soundfile": "soundfile",
    "sherpa_onnx": "sherpa-onnx>=1.12.0",
    "cffi": "cffi",
}


def _find_uv() -> str | None:
    """Tìm 'uv' trên PATH và các vị trí cài đặt phổ biến (Windows / macOS / Linux)."""
    bundle_candidates: list[Path] = []
    for raw in (
        os.environ.get("ZM_AI_TOOL_BUNDLE"),
        str(getattr(sys, "_MEIPASS", "") or ""),
        str(Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ""),
    ):
        if raw:
            bundle_candidates.append(Path(raw) / ("uv.exe" if sys.platform == "win32" else "uv"))
    found = shutil.which("uv")
    if found:
        return found
    for candidate in bundle_candidates:
        if candidate.is_file():
            return str(candidate)
    home = Path.home()
    if sys.platform == "win32":
        localappdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates = [
            localappdata / "uv" / "bin" / "uv.exe",       # uv installer mặc định Windows
            localappdata / "Programs" / "uv" / "uv.exe",   # winget/scoop variant
            home / ".cargo" / "bin" / "uv.exe",            # cargo install uv
        ]
    else:
        candidates = [
            home / ".cargo" / "bin" / "uv",
            home / ".local" / "bin" / "uv",
            Path("/usr/local/bin/uv"),
            Path("/opt/homebrew/bin/uv"),
        ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _ensure_frozen_runtime_venv(uv: str, venv: Path) -> Path:
    """Provision APP-owned Python; the destination machine needs no system Python."""
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if py.is_file():
        try:
            probe = subprocess.run(
                [str(py), "-I", "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=30,
                env=_runtime_subprocess_env(),
            )
            if probe.returncode == 0:
                return py
        except (OSError, subprocess.SubprocessError):
            pass

    backup: Path | None = None
    if venv.exists():
        backup = venv.with_name(f"{venv.name}.repair-{time.time_ns()}")
        venv.replace(backup)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        managed = _pip_stream(
            [uv, "python", "install", version],
            timeout=1800,
            progress=(24, 26, f"Python {version}"),
        )
        if managed.returncode:
            raise RuntimeError(
                "Không tải được Python runtime. Kiểm tra Internet rồi thử lại.\n"
                + (managed.stdout or managed.stderr)[-2000:]
            )
        help_result = subprocess.run(
            [uv, "venv", "--help"], capture_output=True, text=True, timeout=30,
            env=_runtime_subprocess_env(),
        )
        command = [uv, "venv", "--python", version, "--seed"]
        if "--relocatable" in (help_result.stdout or ""):
            command.append("--relocatable")
        created = _pip_stream(
            [*command, str(venv)],
            timeout=900,
            progress=(26, 28, "Python virtual environment"),
        )
        if created.returncode or not py.is_file():
            raise RuntimeError(
                "Không tạo được Python runtime riêng cho APP.\n"
                + (created.stdout or created.stderr)[-2000:]
            )
    except Exception:
        if venv.exists():
            shutil.rmtree(venv, ignore_errors=True)
        if backup is not None and backup.exists():
            backup.replace(venv)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
        if _install_log_fn:
            _install_log_fn(
                "Đã tự sửa Python runtime bị hỏng sau khi di chuyển thư mục. / "
                "Repaired the Python runtime after the Portable folder moved.\n"
            )
    return py


def _runtime_pip_cmd(*extra: str) -> list[str]:
    if getattr(sys, "frozen", False):
        home = _zm_ai_tool_home()
        venv = home / ".venv-runtime"
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        uv = _find_uv()
        if not uv:
            hint = (
                "Mở PowerShell và chạy: winget install --id astral-sh.uv -e"
                if sys.platform == "win32" else
                "Chạy: curl -LsSf https://astral.sh/uv/install.sh | sh"
            )
            raise RuntimeError(f"Không tìm thấy uv để cài gói AI. {hint}")
        py = _ensure_frozen_runtime_venv(uv, venv)
        return [uv, "pip", "install", "--python", str(py), *extra]
    return [sys.executable, "-m", "pip", "install", *extra]


def _runtime_pip_uninstall_cmd(*packages: str) -> list[str]:
    if getattr(sys, "frozen", False):
        home = _zm_ai_tool_home()
        venv = home / ".venv-runtime"
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        uv = _find_uv()
        if not uv:
            raise RuntimeError(
                "Không tìm thấy uv để cài gói AI. "
                + ("Mở PowerShell và chạy: winget install --id astral-sh.uv -e" if sys.platform == "win32"
                   else "Chạy: curl -LsSf https://astral.sh/uv/install.sh | sh")
            )
        py = _ensure_frozen_runtime_venv(uv, venv)
        return [uv, "pip", "uninstall", "--python", str(py), *packages]
    return [sys.executable, "-m", "pip", "uninstall", "-y", *packages]


def _runtime_pip_install(
    *packages: str,
    index_url: str | None = None,
    timeout: float = 600,
    progress: tuple[int, int, str] | None = None,
) -> None:
    if not packages:
        return
    cmd = _runtime_pip_cmd("--upgrade", *packages)
    if index_url:
        cmd.extend(["--index-url", index_url])
    proc = _pip_stream(cmd, timeout=timeout, progress=progress)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout)[-2000:])


def _install_runtime_torch(*, accel: str | None = None) -> None:
    """PyTorch khớp GPU — VieNeu auto chỉ dùng CUDA khi torch.cuda sẵn sàng."""
    wanted = accel or _runtime_torch_accel()
    if wanted == "cuda":
        index_url = _runtime_torch_cuda_index()
        removed = _pip_stream(
            _runtime_pip_uninstall_cmd("torch", "torchaudio", "torchvision"),
            timeout=300,
            idle_timeout=120,
            progress=(80, 82, "Torch cũ / old Torch"),
        )
        if removed.returncode:
            raise RuntimeError((removed.stderr or removed.stdout)[-2000:])
        _runtime_pip_install(
            "torch",
            "torchaudio",
            index_url=index_url,
            timeout=2700,
            progress=(82, 96, "PyTorch CUDA + Torchaudio"),
        )
        return
    if wanted == "mac":
        _runtime_pip_install(
            "torch", "torchaudio", timeout=1200,
            progress=(82, 96, "PyTorch Metal + Torchaudio"),
        )
        return
    if wanted == "rocm":
        _runtime_pip_install(
            "torch", "torchaudio", index_url=_TORCH_ROCM_INDEX, timeout=2400,
            progress=(82, 96, "PyTorch ROCm + Torchaudio"),
        )
        return
    idx = None if sys.platform == "darwin" else _TORCH_CPU_INDEX
    _runtime_pip_install(
        "torch", "torchaudio", index_url=idx, timeout=1200,
        progress=(82, 96, "PyTorch CPU + Torchaudio"),
    )


def _runtime_torch_cuda_index() -> str:
    """Blackwell needs CUDA 12.8 wheels; cu124 has no sm_100/sm_120 kernels."""
    from ..accel import nvidia_smi_executable

    try:
        proc = subprocess.run(
            [nvidia_smi_executable(), "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8, env=_runtime_subprocess_env(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0,
        )
        if proc.returncode == 0 and any(float(cap.strip()) >= 10 for cap in proc.stdout.splitlines()):
            return "https://download.pytorch.org/whl/cu128"
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return _TORCH_CUDA_INDEX


_torch_warm_done = False  # once per process — không spam pip / log


def _runtime_torch_needs_install() -> bool:
    if getattr(sys, "frozen", False):
        if not _runtime_mod_ok("torch")[0]:
            return True
        if not _runtime_mod_ok("torchaudio")[0]:
            return True
        return _nvidia_present() and not _torch_cuda_ready_cached()
    # Chỉ import được mới tin — metadata ~orch hỏng không bắt reinstall.
    if not _mod_ok("torch")[0]:
        return True
    if not _mod_ok("torchaudio")[0]:
        return True
    return _nvidia_present() and not _torch_cuda_ready_cached()


def ensure_runtime_torch() -> None:
    """VieNeu zmAI/clone cần torch(+audio); NVIDIA cần bản CUDA (không phải PyPI CPU)."""
    global _torch_warm_done
    if _torch_warm_done:
        return
    if getattr(sys, "frozen", False):
        if _runtime_torch_needs_install():
            _install_runtime_torch()
            _invalidate_checks_cache()
            if _runtime_torch_needs_install():
                raise RuntimeError("AI_RUNTIME_TORCH_FAILED: Torch/CUDA chưa sẵn sàng / Torch/CUDA is not ready")
        _torch_warm_done = True
        return
    if not _runtime_torch_needs_install():
        _torch_warm_done = True
        return

    # Dev: torch đã load (uvicorn/worker) → tuyệt đối không pip (Access denied _C.pyd).
    # Thiếu torchaudio: cài tay khi tắt backend, không auto-pip trong warm.
    if not getattr(sys, "frozen", False) and (
        _torch_dll_locked() or _mod_ok("torch")[0]
    ):
        if not _mod_ok("torchaudio")[0]:
            print(
                "[ensure_runtime_torch] torchaudio missing — skip pip while server running. "
                "Stop backend then: pip install torchaudio --index-url "
                f"{_TORCH_CUDA_INDEX if _nvidia_present() else _TORCH_CPU_INDEX}",
                flush=True,
            )
        _torch_warm_done = True
        return

    before_cuda = _torch_cuda_ready()
    try:
        _install_runtime_torch()
    except Exception as exc:
        # WinError 5 / pip fail — log 1 lần, không kill warm-models
        print(f"[ensure_runtime_torch] install skipped: {exc}", flush=True)
        _torch_warm_done = True
        return
    if not before_cuda:
        _clear_torch_modules()
    _torch_warm_done = True


def ensure_runtime_transformers() -> None:
    """VieNeu PyTorch backend cần transformers (đăng ký model_type vieneu_v3)."""
    from ..runtime_site import (
        bootstrap_ai_runtime,
        install_runtime_meta_path,
        runtime_site_packages,
        verify_transformers_ok,
        is_windows_path_too_long_error,
        _purge_external_modules,
    )

    if not getattr(sys, "frozen", False):
        bootstrap_ai_runtime()
    ok, _detail = verify_transformers_ok()
    if ok:
        return
    if is_windows_path_too_long_error(_detail):
        raise RuntimeError(
            "PATH Windows quá dài nên không thể nạp transformers. "
            "App đã loại đường dẫn trùng; không cần cài lại gói AI."
        )
    if "No module named" in _detail and any(f"'{name}'" in _detail for name in sys.stdlib_module_names):
        raise RuntimeError(
            f"AI_RUNTIME_STDLIB_MISSING: {_detail}. "
            "Thiếu thư viện Python chuẩn; cập nhật APP/runtime, cài lại transformers không sửa được. / "
            "Python standard library is missing; update the app/runtime instead of reinstalling transformers."
        )
    packages = ("transformers==4.57.6", "safetensors") if getattr(sys, "frozen", False) else (
        "transformers>=4.46.0", "huggingface-hub>=0.34", "safetensors",
    )
    _runtime_pip_install(*packages, timeout=1200)
    if not getattr(sys, "frozen", False):
        root = runtime_site_packages()
        if root:
            _purge_external_modules(root)
        install_runtime_meta_path()
        bootstrap_ai_runtime()
    ok, detail = verify_transformers_ok()
    if not ok:
        if is_windows_path_too_long_error(detail):
            raise RuntimeError(
                "PATH Windows quá dài nên không thể nạp transformers. "
                "App đã loại đường dẫn trùng; không cần cài lại gói AI."
            )
        raise RuntimeError(
            f"transformers chưa import được sau cài đặt: {detail}. "
            "Thử Thiết lập → Cài gói AI rồi khởi động lại app."
        )


def ensure_torchaudio() -> None:
    ensure_runtime_torch()


def install_ai_runtime() -> dict[str, Any]:
    """Cài nhóm ASR/OCR nặng vào venv riêng của bản desktop."""
    from pipeline.asr.speaker import ensure_diarization_models
    from pipeline.core.config import DATA

    _report_install(2, "Đang kiểm tra model tách người nói… / Checking speaker models…")

    def model_progress(stage: str, current: int, total: int) -> None:
        start, end, name = (
            (2, 10, "model phân đoạn / segmentation model")
            if stage == "segmentation"
            else (10, 18, "model nhận dạng giọng / speaker embedding model")
        )
        ratio = current / total if total > 0 else 0
        value = start + round((end - start) * max(0.0, min(1.0, ratio)))
        size = (
            f" ({current // (1 << 20)}/{total // (1 << 20)} MB)"
            if total > 0 else ""
        )
        if total <= 0 and current > 0:
            size = f" ({current // (1 << 20)} MB)"
        _report_install(value, f"Đang tải {name}{size}")

    ensure_diarization_models(
        DATA / "models" / "pyannote",
        log=_install_log_fn,
        progress=model_progress,
    )
    _report_install(18, "Đã kiểm tra model / Speaker models ready")
    if getattr(sys, "frozen", False):
        _report_install(20, "Đang kiểm tra các gói đã cài… / Checking installed packages…")
        _invalidate_checks_cache()
        ok, detail = _runtime_venv_fast()
        # Filesystem metadata is only a fast hint; import every runtime module
        # before declaring success so a broken wheel is repaired on demand.
        missing = _frozen_runtime_missing_modules()
        needs_torch = (
            "torch" in missing
            or "torchaudio" in missing
            or (_nvidia_present() and not _torch_cuda_ready_cached())
        )
        if ok and not missing and not needs_torch and (
            _runtime_ort_accel() != "cuda"
            or _sherpa_cuda_ready(
                _zm_ai_tool_home()
                / ".venv-runtime"
                / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            )
        ):
            return {
                "ok": True,
                "message": "Gói AI đã sẵn sàng",
                "detail": detail,
            }
        # Probe imports in the runtime venv.  A package that is installed and
        # usable must not be upgraded just because its dist-info is unusual.
        # ``missing`` and ``needs_torch`` were calculated above for this branch.
        cv2_ok = "cv2" not in missing
    else:
        # Phát hiện torch bị corrupt (file mix version) — phải reinstall khi dừng backend.
        if _torch_broken():
            idx = _TORCH_CUDA_INDEX if _nvidia_present() else _TORCH_CPU_INDEX
            raise RuntimeError(
                "torch bị hỏng (AttributeError khi import). "
                "Dừng backend rồi chạy:\n"
                f"pip install --force-reinstall --no-deps torch torchaudio "
                f"--index-url {idx}"
            )
        missing = [name for name in _AI_RUNTIME_MODULES if not _mod_ok(name)[0]]
        needs_torch = _runtime_torch_needs_install()
    if not missing and not needs_torch and _sherpa_cuda_ready():
        return {
            "ok": True,
            "message": "Gói AI đã sẵn sàng",
            "detail": _ai_runtime_detail(),
        }

    if getattr(sys, "frozen", False):
        home = _zm_ai_tool_home()
        venv = home / ".venv-runtime"
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        uv = _find_uv()
        if not uv:
            raise RuntimeError("Bản ứng dụng thiếu uv để cài gói AI")
        _report_install(24, "Đang chuẩn bị Python runtime… / Preparing Python runtime…")
        py = _ensure_frozen_runtime_venv(uv, venv)
        _report_install(28, "Python runtime đã sẵn sàng / Python runtime ready")

        from pipeline.tts.engines import vieneu_frozen

        # Do not replace DLLs underneath an active synthesis or cached model.
        with vieneu_frozen._pool_lock:
            idle = [worker for workers in vieneu_frozen._idle.values() for worker in workers]
            if any(worker.alive() and worker not in idle for worker in vieneu_frozen._all_workers):
                raise RuntimeError("AI_RUNTIME_BUSY: Dừng job TTS trước khi cài / Stop TTS jobs before installing")
        vieneu_frozen.shutdown_all_workers()

        # Windows: cv2.pyd bị lock khi đã preload → không thể xoá/thay thế.
        # Bỏ opencv khỏi danh sách cài nếu cv2 đã load trong process hiện tại.
        _cv2_locked = sys.platform == "win32" and "cv2" in sys.modules
        if _cv2_locked:
            _install_log_fn and _install_log_fn(
                "cv2 đã load — bỏ qua opencv (sẽ cập nhật khi khởi động lại)\n"
            )

        # opencv-python + headless cùng lúc → đụng cv2; chỉ giữ headless.
        if not _cv2_locked:
            opencv_remove = ["opencv-python"] + ([] if cv2_ok else ["opencv-python-headless"])
            try:
                subprocess.run(
                    [uv, "pip", "uninstall", "--python", str(py), *opencv_remove],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=_runtime_subprocess_env(),
                )
            except (OSError, subprocess.SubprocessError):
                pass  # bỏ qua nếu file bị lock

        # Lọc bỏ OpenCV nếu cv2.pyd đang bị lock.
        packages = _frozen_runtime_package_specs(missing)
        if _cv2_locked:
            packages = [
                p for p in packages
                if not p.startswith("opencv-python")
            ]
        ort_accel = _runtime_ort_accel()
        if ort_accel == "cuda":
            packages.append(_ORT_GPU_PKG)
        elif ort_accel == "directml":
            packages.append(_ORT_DIRECTML_PKG)
        base_cmd = [
            uv, "pip", "install", "--python", str(py), "--upgrade",
            "--index-strategy", "unsafe-best-match",
            *packages,
        ] if packages else []
        vieneu_cmd = [
            uv, "pip", "install", "--python", str(py), "--upgrade", "--no-deps", _VIENEU_PACKAGE
        ]
    else:
        # ponytail: LUÔN --no-deps — pip scan dist-info khi resolve thấy ~orch corrupt
        # → reinstall torch → WinError 32. --no-deps bỏ qua resolve hoàn toàn.
        _clean_corrupted_dists()

        # pkg spec → module name để kiểm tra trước khi pip (tránh lock .pyd đang dùng)
        _PKG_MOD: dict[str, str] = {
            "faster-whisper": "faster_whisper",
            "soundfile": "soundfile",
            "sherpa-onnx": "sherpa_onnx",
            "sherpa-onnx-bin": "sherpa_onnx_bin",
            "cffi": "cffi",
            "pycparser": "pycparser",
            "soxr": "soxr",
            "tokenizers": "tokenizers",
            "rapidocr-onnxruntime": "rapidocr_onnxruntime",
            "pillow": "PIL",
            "opencv-python-headless": "cv2",
            "huggingface-hub": "huggingface_hub",
            "httpx": "httpx",
            "pyyaml": "yaml",
            "sea-g2p": "sea_g2p",
            "transformers": "transformers",
            "vieneu": "vieneu",
        }

        def _pkg_base(spec: str) -> str:
            """'faster-whisper>=1.1.0' → 'faster-whisper'"""
            return spec.split(">=")[0].split("==")[0].split("<")[0].strip()

        def _need_install(spec: str) -> bool:
            mod = _PKG_MOD.get(_pkg_base(spec))
            if mod is None:
                return True  # không biết → cài cho chắc
            return not _mod_ok(mod)[0]

        def _pip_group(label: str, *pkgs: str) -> None:
            """Cài các pkg chưa import được, --no-deps, in header vào log."""
            needed = [p for p in pkgs if _need_install(p)]
            if not needed:
                if _install_log_fn:
                    _install_log_fn(f"\n=== {label} — đã có, bỏ qua ===\n")
                return
            if _install_log_fn:
                _install_log_fn(f"\n=== {label} ===\n")
            proc = _pip_stream([sys.executable, "-m", "pip", "install", "--no-deps", *needed])
            if proc.returncode:
                raise RuntimeError(f"[{label}] " + (proc.stderr or proc.stdout)[-2000:])

        # Nhóm 1 — Whisper
        _pip_group("Whisper (ASR)", *_PKG_WHISPER)

        # Nhóm 1b — speaker diarization, dùng chung audio 16 kHz của Whisper.
        _pip_group("Speaker diarization", *_PKG_DIARIZATION)
        _install_sherpa_cuda(sys.executable)

        # Nhóm 2 — OCR
        _pip_group("OCR", *_PKG_OCR)
        ort_accel = _runtime_ort_accel()
        if ort_accel == "cuda":
            if _install_log_fn:
                _install_log_fn("\n=== OCR GPU (onnxruntime-gpu) ===\n")
            _pip_stream([sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime"])
            proc_gpu = _pip_stream([sys.executable, "-m", "pip", "install", _ORT_GPU_PKG])
            if proc_gpu.returncode:
                raise RuntimeError("[OCR GPU] " + (proc_gpu.stderr or proc_gpu.stdout)[-2000:])
        elif ort_accel == "directml":
            if _install_log_fn:
                _install_log_fn("\n=== OCR GPU (DirectML) ===\n")
            # Both wheels own the same `onnxruntime` module; keep exactly one provider wheel.
            _pip_stream([sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime"])
            proc_dml = _pip_stream([
                sys.executable, "-m", "pip", "install", "--force-reinstall", _ORT_DIRECTML_PKG
            ])
            if proc_dml.returncode:
                raise RuntimeError("[OCR DirectML] " + (proc_dml.stderr or proc_dml.stdout)[-2000:])

        # Nhóm 3 — zmAI + VieNeu
        _pip_group("zmAI + VieNeu", *_PKG_VIENEU)

        # transformers riêng --no-deps (conflict tokenizers với mọi version)
        if _need_install("transformers"):
            if _install_log_fn:
                _install_log_fn("\n=== Transformers ===\n")
            proc_t = _pip_stream([
                sys.executable, "-m", "pip", "install", "--no-deps", "transformers>=4.46.0"
            ])
            if proc_t.returncode:
                raise RuntimeError("[Transformers] " + (proc_t.stderr or proc_t.stdout)[-2000:])

        # VieNeu package
        if _need_install("vieneu"):
            if _install_log_fn:
                _install_log_fn("\n=== VieNeu Local ===\n")
            proc_v = _pip_stream([sys.executable, "-m", "pip", "install", "--no-deps", _VIENEU_PACKAGE])
            if proc_v.returncode:
                raise RuntimeError("[VieNeu] " + (proc_v.stderr or proc_v.stdout)[-2000:])

        return {
            "ok": True,
            "message": "Đã cài thành công",
            "detail": _ai_runtime_detail(),
        }

    if missing:
        if not base_cmd:
            pass  # tất cả gói cần thiết đều đã có, bỏ qua
        else:
            _clean_corrupted_dists(_venv_site_packages(venv))
            if _install_log_fn:
                _install_log_fn("\n=== AI dependencies ===\n")
            proc = _pip_stream(
                base_cmd,
                timeout=2400,
                progress=(30, 62, "các thư viện AI / AI dependencies"),
            )
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout)[-3000:])
            if getattr(sys, "frozen", False) and ort_accel in ("cuda", "directml"):
                removed = _pip_stream(
                    [uv, "pip", "uninstall", "--python", str(py), "onnxruntime"],
                    timeout=300,
                    idle_timeout=120,
                    progress=(62, 64, "ONNX Runtime cũ / old ONNX Runtime"),
                )
                if removed.returncode:
                    raise RuntimeError((removed.stderr or removed.stdout)[-2000:])
                provider_pkg = _ORT_GPU_PKG if ort_accel == "cuda" else _ORT_DIRECTML_PKG
                provider_cmd = [
                    uv, "pip", "install", "--python", str(py), "--force-reinstall",
                    "--index-strategy", "unsafe-best-match",
                    provider_pkg,
                ]
                proc_provider = _pip_stream(
                    provider_cmd,
                    timeout=1200,
                    progress=(64, 68, "ONNX Runtime GPU"),
                )
                if proc_provider.returncode:
                    raise RuntimeError((proc_provider.stderr or proc_provider.stdout)[-3000:])
    if getattr(sys, "frozen", False):
        _install_sherpa_cuda(py, uv)
    if "vieneu" in missing or not _mod_ok("vieneu")[0]:
        proc = _pip_stream(
            vieneu_cmd,
            timeout=1200,
            progress=(76, 80, "VieNeu"),
        )
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout)[-3000:])
    if needs_torch:
        if not getattr(sys, "frozen", False) and _torch_dll_locked():
            # torch đã load → chỉ có thể cài torchaudio nếu đó là thứ duy nhất thiếu
            torch_ok = _mod_ok("torch")[0]
            torchaudio_missing = not _mod_ok("torchaudio")[0]
            need_cuda_swap = _nvidia_present() and torch_ok and not _torch_cuda_ready_cached()
            if torchaudio_missing and torch_ok and not need_cuda_swap:
                # ponytail: torchaudio chưa load → .pyd không bị lock → cài an toàn.
                # --no-deps: tránh pip kéo torch theo → đụng torch._C.pyd đang locked.
                idx = _TORCH_CUDA_INDEX if _nvidia_present() else _TORCH_CPU_INDEX
                proc = _pip_stream(
                    [sys.executable, "-m", "pip", "install", "--no-deps", "torchaudio", "--index-url", idx],
                )
                if proc.returncode:
                    raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
            else:
                raise RuntimeError(
                    "torch đang được load bởi backend đang chạy — không thể cài/nâng cấp.\n"
                    "Dừng backend, rồi chạy tay:\n"
                    f"  pip install torch torchaudio --index-url "
                    f"{_TORCH_CUDA_INDEX if _nvidia_present() else _TORCH_CPU_INDEX}"
                )
        else:
            _install_runtime_torch()
            _clear_torch_modules()
    else:
        _report_install(96, "PyTorch đã sẵn sàng / PyTorch ready")
    _report_install(97, "Đang xác minh runtime… / Verifying AI runtime…")
    _invalidate_checks_cache()
    if getattr(sys, "frozen", False):
        _verify_frozen_runtime_install()
    return {
        "ok": True,
        "message": "Đã cài gói AI",
        "detail": _ai_runtime_detail(),
    }


def install_ocr_cuda() -> dict[str, Any]:
    """Install the OCR GPU runtime into the Python running this API."""
    _report_install(5, "Đang kiểm tra OCR GPU… / Checking OCR GPU…")
    ok, detail = _ocr_cuda_check()
    if ok:
        return {"ok": True, "message": "GPU tăng tốc đã được cài", "detail": detail}
    if getattr(sys, "frozen", False):
        home = Path(os.environ.get("ZM_AI_TOOL_HOME") or "")
        venv = home / ".venv-runtime"
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        uv = _find_uv()
        if not uv:
            raise RuntimeError("Bản ứng dụng thiếu uv để cài OCR GPU")
        _report_install(15, "Đang chuẩn bị Python runtime… / Preparing Python runtime…")
        py = _ensure_frozen_runtime_venv(uv, venv)
        removed = _pip_stream(
            [uv, "pip", "uninstall", "--python", str(py), "onnxruntime"],
            timeout=300,
            idle_timeout=120,
            progress=(30, 38, "ONNX Runtime CPU"),
        )
        if removed.returncode:
            raise RuntimeError((removed.stderr or removed.stdout)[-2000:])
        try:
            proc = _pip_stream(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(py),
                    "--force-reinstall",
                    "--index-strategy",
                    "unsafe-best-match",
                    _ORT_GPU_PKG,
                ],
                timeout=1200,
                progress=(38, 92, "ONNX Runtime GPU"),
            )
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
        except Exception:
            # GPU is optional. Restore the CPU provider so a failed upgrade
            # never leaves OCR unusable on Windows.
            _report_install(92, "Đang khôi phục OCR CPU… / Restoring OCR CPU…")
            try:
                _pip_stream(
                    [uv, "pip", "install", "--python", str(py), "onnxruntime"],
                    timeout=600,
                    idle_timeout=300,
                )
            except Exception:
                pass
            raise
        _report_install(96, "Đang xác minh CUDA provider… / Verifying CUDA provider…")
        ok, detail = _ocr_cuda_check_fresh(py)
        if not ok:
            raise RuntimeError(f"CUDA provider unavailable after install: {detail}")
        _invalidate_checks_cache()
        return {
            "ok": True,
            "message": "Đã cài OCR GPU",
            "detail": detail,
        }
    pip = [sys.executable, "-m", "pip"]
    _pip_stream(
        pip + ["uninstall", "-y", "onnxruntime", "onnxruntime-gpu"],
        timeout=180,
        idle_timeout=120,
        progress=(10, 25, "ONNX Runtime cũ / old ONNX Runtime"),
    )
    try:
        proc = _pip_stream(
            pip
            + [
                "install",
                "--progress-bar",
                "off",
                _ORT_GPU_PKG,
            ],
            timeout=900,
            progress=(25, 95, "ONNX Runtime GPU"),
        )
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
    except Exception:
        # ponytail: keep OCR usable if the optional 2 GB GPU install fails.
        _report_install(95, "Đang khôi phục OCR CPU… / Restoring OCR CPU…")
        try:
            _pip_stream(
                pip + ["install", "onnxruntime"],
                timeout=600,
                idle_timeout=300,
            )
        except Exception:
            pass
        raise
    # ponytail: Windows keeps the old ORT DLL mapped until this API exits; verify after restart.
    _invalidate_checks_cache()
    return {"ok": True, "message": "Đã cài GPU tăng tốc", "detail": "CUDAExecutionProvider"}


def install_demucs_cuda() -> dict[str, Any]:
    """Cài Demucs tối ưu: NVIDIA CUDA / Apple demucs-mlx / CPU."""
    ok, detail = _demucs_check()
    if ok:
        return {"ok": True, "message": "Demucs đã sẵn sàng", "detail": detail}
    from pipeline.export.stem import _demucs_python

    py = Path(_demucs_python(None, report=False))
    ok, detail = _demucs_check(refresh=True)
    if not ok:
        raise RuntimeError(f"Demucs chưa sẵn sàng sau khi cài: {detail} · python={py}")
    label = "Apple GPU" if _apple_silicon() else ("NVIDIA GPU" if _which("nvidia-smi") else "CPU")
    _invalidate_checks_cache()
    return {"ok": True, "message": f"Đã cài Demucs ({label})", "detail": detail}


def install_nvm() -> dict[str, Any]:
    """Install NVM and the current Node.js LTS without opening a browser."""
    if shutil.which("node"):
        return {"ok": True, "message": "Node.js đã sẵn sàng", "detail": "node trên PATH"}
    if sys.platform == "win32":
        winget = shutil.which("winget")
        if not winget:
            raise RuntimeError("Windows thiếu winget để tự cài NVM for Windows")
        proc = _pip_stream([
            winget, "install", "--id", "CoreyButler.NVMforWindows", "-e", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
        ], timeout=900)
        if proc.returncode:
            raise RuntimeError((proc.stdout or proc.stderr)[-2000:])
        candidates = [Path(value) / suffix for value, suffix in (
            (os.environ.get("NVM_HOME"), "nvm.exe"),
            (os.environ.get("APPDATA"), "nvm/nvm.exe"),
            (os.environ.get("ProgramFiles", r"C:\Program Files"), "nvm/nvm.exe"),
        ) if value]
        nvm = next((str(path) for path in candidates if path.is_file()), None) or shutil.which("nvm")
        if not nvm:
            raise RuntimeError("NVM đã cài nhưng chưa tìm thấy nvm.exe; khởi động lại app")
        for args in (("install", "lts"), ("use", "lts")):
            result = _pip_stream([nvm, *args], timeout=1200)
            if result.returncode:
                raise RuntimeError((result.stdout or result.stderr)[-2000:])
        return {"ok": True, "message": "Đã cài NVM + Node.js LTS", "detail": "NVM for Windows"}

    # Official nvm installer also wires the user's shell profile.
    url = "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh"
    try:
        script = urllib.request.urlopen(url, timeout=30).read()
    except Exception as exc:
        raise RuntimeError(f"Không tải được NVM installer: {exc}") from exc
    with tempfile.NamedTemporaryFile(suffix=".sh") as tmp:
        tmp.write(script)
        tmp.flush()
        installed = _pip_stream(["bash", tmp.name], timeout=900)
    if installed.returncode:
        raise RuntimeError((installed.stdout or installed.stderr)[-2000:])
    nvm_dir = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    nvm_sh = nvm_dir / "nvm.sh"
    if not nvm_sh.is_file():
        raise RuntimeError(f"NVM installer hoàn tất nhưng thiếu {nvm_sh}")
    command = f'. "{nvm_sh}" && nvm install --lts && nvm alias default "lts/*"'
    node = _pip_stream(["bash", "-lc", command], timeout=1800)
    if node.returncode:
        raise RuntimeError((node.stdout or node.stderr)[-2000:])
    return {"ok": True, "message": "Đã cài NVM + Node.js LTS", "detail": str(nvm_dir)}
