"""ffmpeg/ffprobe helpers and hardware probe."""
from __future__ import annotations

import os
import platform
import hashlib
import json
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from .jobs import run_cmd


# Chocolatey ShimGen ~400KB. Copy vào _internal thì relative `..\lib\ffmpeg\...`
# gãy (exit 4294967295). Shim ở đúng chỗ gốc (chocolatey\bin) vẫn chạy được.
# ponytail: size heuristic chỉ cho bản bundle. Ceiling = binary UPX < 2MB. Upgrade: parse ShimGen PE.
_FF_MIN_BYTES = 2_000_000


def _is_real_ff_bin(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        # Homebrew ffmpeg trên macOS nhỏ (codec ở dylib) — chỉ chặn ShimGen trên Windows.
        if sys.platform != "win32":
            return True
        return path.stat().st_size >= _FF_MIN_BYTES
    except OSError:
        return False


@lru_cache(maxsize=16)
def _has_ffmpeg_filter(filter_name: str) -> bool:
    try:
        res = subprocess.run([_ff_bin("ffmpeg"), "-filters"], capture_output=True, text=True, timeout=5)
        for line in res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == filter_name:
                return True
        return False
    except Exception:
        return False


def _ff_bin(name: str) -> str:
    """Trả binary name (hoặc full path) cho ffmpeg/ffprobe.

    Frozen: ưu tiên file thật trong _MEIPASS; bỏ shim đã copy (gãy).
    PATH: nhận cả shim gốc — chỉ skip thư mục bundle.
    """
    ext = ".exe" if sys.platform == "win32" else ""
    meipass = getattr(sys, "_MEIPASS", None)
    meipass_res: Path | None = None
    if meipass:
        try:
            meipass_res = Path(meipass).resolve()
        except OSError:
            meipass_res = Path(meipass)
        bundled = meipass_res / f"{name}{ext}"
        if _is_real_ff_bin(bundled):
            return str(bundled)

    # Ưu tiên ffmpeg-full (keg-only brew có libass/freetype) nếu có trên macOS
    if sys.platform == "darwin":
        for custom_dir in (
            Path("/opt/homebrew/opt/ffmpeg-full/bin"),
            Path("/usr/local/opt/ffmpeg-full/bin"),
        ):
            custom_bin = custom_dir / f"{name}{ext}"
            if custom_bin.is_file():
                return str(custom_bin)

    seen: set[str] = set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder:
            continue
        try:
            folder_res = Path(folder).resolve()
        except OSError:
            continue
        if meipass_res is not None and folder_res == meipass_res:
            continue
        candidate = folder_res / f"{name}{ext}"
        try:
            key = str(candidate.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return str(candidate)

    found = shutil.which(name)
    if found:
        found = found.strip()
        if meipass_res is not None:
            try:
                if Path(found).resolve().is_relative_to(meipass_res):
                    found = ""
            except (OSError, ValueError):
                pass
        if found:
            return found
    return name





def _gpu_kind_from_name(name: str) -> str:
    value = name.casefold()
    if any(token in value for token in ("nvidia", "geforce", "quadro", "tesla", "titan")):
        return "nvidia"
    if any(token in value for token in ("amd", "radeon", "firepro")):
        return "amd"
    if any(token in value for token in ("intel", "iris", "arc", "uhd graphics")):
        return "intel"
    return "other"


def _windows_video_controllers() -> list[dict[str, Any]]:
    """Use CIM on current Windows; WMIC disappeared from many clean Windows 11 installs."""
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion,PNPDeviceID,ConfigManagerErrorCode | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True, stderr=subprocess.DEVNULL, timeout=8,
        ).strip()
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else [data]
    except (FileNotFoundError, subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return []


def _nvidia_gpus() -> list[dict[str, Any]]:
    """All NVIDIA adapters visible to this process, including vGPU/passthrough."""
    from .accel import nvidia_smi_executable, _safe_subprocess_env
    try:
        out = subprocess.check_output(
            [
                nvidia_smi_executable(),
                "--query-gpu=index,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
            env=_safe_subprocess_env(),
        ).strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    result: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            index = len(result)
        try:
            memory = int(float(parts[2])) if len(parts) > 2 else None
        except ValueError:
            memory = None
        result.append({
            "index": index,
            "name": parts[1],
            "kind": "nvidia",
            "vramMb": memory,
            "driver": parts[3] if len(parts) > 3 else "",
            "accel": "cuda",
            "source": "nvidia-smi",
        })
    return result


def _windows_gpu_inventory(nvidia: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge CUDA-visible NVIDIA GPUs with every Windows display adapter."""
    result = list(nvidia)
    known = {str(gpu.get("name") or "").casefold() for gpu in result}
    for controller in _windows_video_controllers():
        name = str(controller.get("Name") or "").strip()
        instance = str(controller.get("PNPDeviceID") or "")
        if not name:
            continue
        kind = "nvidia" if "VEN_10DE" in instance.upper() else _gpu_kind_from_name(name)
        if "microsoft basic" in name.casefold() and kind != "nvidia":
            continue
        # Device Manager may repeat an adapter already reported by nvidia-smi.
        if kind == "nvidia" and any(
            name.casefold() in item or item in name.casefold() for item in known if item
        ):
            continue
        try:
            ram = int(controller.get("AdapterRAM") or 0)
        except (TypeError, ValueError):
            ram = 0
        result.append({
            "index": len(result),
            "name": name,
            "kind": kind,
            "vramMb": ram // (1024 * 1024) if ram > 0 else None,
            "driver": str(controller.get("DriverVersion") or ""),
            "accel": "directml",
            "source": "windows-cim",
            "deviceInstanceId": instance,
            "deviceProblemCode": controller.get("ConfigManagerErrorCode"),
        })
        known.add(name.casefold())
    return result


def _atomic_replace(src: Path, dst: Path, *, attempts: int = 30) -> None:
    """Replace dst with src; retry when Windows locks final.mp4 (preview/player/AV)."""
    src_p, dst_p = Path(src), Path(dst)
    if not src_p.is_file():
        raise FileNotFoundError(str(src_p))
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    last: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(str(src_p), str(dst_p))
            return
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            win = getattr(exc, "winerror", None)
            if win not in (5, 32) and getattr(exc, "errno", None) not in (13, 11):
                raise
            last = exc
        if attempt >= 1 and dst_p.exists():
            try:
                dst_p.unlink()
                os.replace(str(src_p), str(dst_p))
                return
            except OSError as exc:
                last = exc
        time.sleep(min(1.25, 0.04 * (attempt + 1)))
    # Some readers hold a share that blocks rename but allows overwrite-copy.
    try:
        shutil.copyfile(str(src_p), str(dst_p))
        try:
            src_p.unlink(missing_ok=True)
        except OSError:
            pass
        return
    except OSError as exc:
        last = exc
    assert last is not None
    raise last


def atempo_chain(ratio: float) -> str:
    """ffmpeg atempo chỉ [0.5, 100] — chain nhiều bước khi ngoài khoảng."""
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return "anull"
    if r <= 0 or abs(r - 1.0) < 0.01:
        return "anull"
    parts: list[str] = []
    while r > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        r *= 2.0
    r = min(100.0, max(0.5, r))
    if abs(r - 1.0) >= 0.01:
        parts.append(f"atempo={r:.4f}")
    return ",".join(parts) if parts else "anull"


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """Probe the encoder, not just ffmpeg's compiled encoder list."""
    try:
        return subprocess.run(
            [
                _ff_bin("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=size=256x256:rate=1",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False


@lru_cache(maxsize=None)
def _h264_encoder_available(codec: str) -> bool:
    """Probe a hardware encoder by actually encoding one frame."""
    try:
        return subprocess.run(
            [
                _ff_bin("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=size=256x256:rate=1",
                "-frames:v", "1", "-c:v", codec, "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False


def h264_hardware_encoder() -> str | None:
    """Best available native H.264 encoder for the active machine."""
    if nvenc_available():
        return "h264_nvenc"
    # Apple Silicon / Intel Macs use the platform VideoToolbox encoder.
    # Probe an actual frame first: FFmpeg may list the codec even when the
    # runtime is missing the required hardware service.
    if sys.platform == "darwin" and _h264_encoder_available("h264_videotoolbox"):
        return "h264_videotoolbox"
    if sys.platform != "win32":
        return None
    try:
        device = detect_device()
        kinds = {str(gpu.get("kind") or "") for gpu in device.get("gpus", [])}
        kinds.add(str(device.get("gpuKind") or ""))
    except Exception:
        kinds = set()
    # Prefer a discrete AMD encoder; QSV remains available on hybrid laptops.
    candidates = tuple(
        codec for codec, kind in (("h264_amf", "amd"), ("h264_qsv", "intel"))
        if kind in kinds
    ) or ("h264_qsv", "h264_amf")
    return next((codec for codec in candidates if _h264_encoder_available(codec)), None)


def nvdec_available(path: Path) -> bool:
    """Probe CUDA/NVDEC against the actual input codec."""
    try:
        return subprocess.run(
            [
                _ff_bin("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-i", str(path), "-frames:v", "1",
                "-vf", "hwdownload,format=nv12",
                "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False


def h264_encoder_args(
    *, fast: bool = False, throughput: bool = False, quality: int | None = None
) -> list[str]:
    # The editor's <video> displays the source as limited-range BT.709.  Every
    # H.264 render must carry the same signalling; otherwise players may assume
    # a different range/transfer and the published mask changes colour even when
    # the compositor pixels were correct.
    color_tags = [
        "-color_range", "tv",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
    ]
    q = max(0, min(51, int(quality))) if quality is not None else (28 if throughput else 18)
    encoder = h264_hardware_encoder()
    if encoder == "h264_nvenc":
        if throughput:
            # Bản trung gian (burn raw→mp4): ưu tiên fps nuôi NVENC, chất lượng để encode 1080 cuối.
            return [
                "-c:v", "h264_nvenc", "-preset", "p1",
                "-tune", "ll", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                "-pix_fmt", "yuv420p", *color_tags,
            ]
        return [
            "-c:v", "h264_nvenc", "-preset", "p3" if fast else "p5",
            "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
            "-pix_fmt", "yuv420p", *color_tags,
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v", "h264_qsv", "-preset", "veryfast" if throughput else "faster",
            "-global_quality", str(q), "-pix_fmt", "nv12", *color_tags,
        ]
    if encoder == "h264_amf":
        return [
            "-c:v", "h264_amf", "-quality", "speed" if throughput or fast else "balanced",
            "-rc", "cqp", "-qp_i", str(q),
            "-qp_p", str(q), "-pix_fmt", "nv12", *color_tags,
        ]
    if encoder == "h264_videotoolbox":
        # VideoToolbox quality is 1–100 (higher is better), unlike x264 CRF.
        vt_quality = 58 if throughput else (72 if fast else 78)
        return [
            "-c:v", "h264_videotoolbox", "-realtime", "1" if throughput else "0",
            "-q:v", str(vt_quality), "-pix_fmt", "yuv420p", *color_tags,
        ]
    return [
        "-c:v", "libx264", "-preset", "ultrafast" if throughput else ("veryfast" if fast else "fast"),
        "-crf", str(q), "-pix_fmt", "yuv420p", *color_tags,
    ]

def detect_device() -> dict[str, Any]:
    """Probe OS + GPU cho Thiết lập / cài đặt đúng backend.

    Trả về đủ để UI quyết định: Windows/macOS/Linux, có GPU không, GPU gì,
    và gói nên cài (OCR CUDA / Demucs CUDA / demucs-mlx).
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        os_id, os_label = "macos", "macOS"
    elif system == "Windows":
        os_id, os_label = "windows", "Windows"
    elif system == "Linux":
        os_id, os_label = "linux", "Linux"
    else:
        os_id, os_label = "unknown", system or "Unknown"

    arch = machine or "?"
    apple_silicon = system == "Darwin" and machine.lower() in ("arm64", "aarch64")

    gpu_kind = "none"
    gpu_name = ""
    vram_mb: int | None = None
    driver = ""
    accel = "cpu"
    gpus: list[dict[str, Any]] = []

    if apple_silicon:
        gpu_kind = "apple"
        accel = "metal"
        chip = ""
        try:
            chip = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass
        gpu_name = chip or f"Apple Silicon ({machine})"
        gpus = [{
            "index": 0, "name": gpu_name, "kind": "apple", "vramMb": None,
            "driver": "", "accel": "metal", "source": "apple-silicon",
        }]
    else:
        nvidia_gpus = _nvidia_gpus()
        if system == "Windows":
            gpus = _windows_gpu_inventory(nvidia_gpus)
        else:
            gpus = nvidia_gpus
        if nvidia_gpus:
            selected = nvidia_gpus[0]
            gpu_kind = "nvidia"
            accel = "cuda"
            gpu_name = str(selected["name"])
            vram_mb = selected.get("vramMb")
            driver = str(selected.get("driver") or "")

        # Fallback: NVIDIA without nvidia-smi, AMD and Intel via native OS inventory.
        if gpu_kind == "none":
            if system == "Windows":
                # Prefer a discrete GPU over the integrated adapter on hybrid laptops.
                candidates = sorted(
                    gpus,
                    key=lambda item: (
                        item.get("kind") in ("nvidia", "amd"),
                        int(item.get("vramMb") or 0),
                    ),
                    reverse=True,
                )
                if candidates:
                    selected = candidates[0]
                    gpu_name = str(selected["name"])
                    gpu_kind = str(selected.get("kind") or "other")
                    accel = str(selected.get("accel") or "directml")
                    driver = str(selected.get("driver") or "")
                    vram_mb = selected.get("vramMb")
            elif system == "Linux":
                try:
                    out = subprocess.check_output(
                        ["lspci", "-mm"], text=True, stderr=subprocess.DEVNULL, timeout=3,
                    )
                    for line in out.splitlines():
                        if any(k in line for k in ("VGA", "Display", "3D controller")):
                            parts = line.split('"')
                            gpu_name = (f"{parts[3]} {parts[5]}".strip()
                                        if len(parts) >= 6 else line.split()[-1])
                            gpu_kind = _gpu_kind_from_name(gpu_name)
                            accel = "rocm" if gpu_kind == "amd" else "cpu"
                            gpus = [{
                                "index": 0, "name": gpu_name, "kind": gpu_kind,
                                "vramMb": None, "driver": "", "accel": accel,
                                "source": "lspci",
                            }]
                            break
                except (FileNotFoundError, subprocess.SubprocessError, OSError):
                    pass

    if gpu_kind == "nvidia":
        demucs_label = "Cài Demucs CUDA"
        demucs_backend = "cuda"
        ocr_action = "ocr_cuda"
        ocr_label = "Cài OCR CUDA"
        install_summary = f"{os_label} + {gpu_name} → OCR CUDA + Demucs CUDA"
        install_hint = "Máy có NVIDIA — nên cài GPU tăng tốc OCR và Demucs CUDA."
    elif gpu_kind == "apple":
        demucs_label = "Cài Demucs (Apple Metal)"
        demucs_backend = "mlx"
        ocr_action = ""
        ocr_label = ""
        install_summary = f"{os_label} Apple Silicon ({gpu_name}) → Demucs-MLX (Metal)"
        install_hint = "Mac Apple Silicon — OCR chạy CPU/ANE; tách lời dùng demucs-mlx (Metal)."
    elif gpu_kind in ("amd", "intel") and accel == "directml":
        demucs_label = "Cài Demucs (CPU)"
        demucs_backend = "cpu"
        ocr_action = "ai_runtime"
        ocr_label = "Cài OCR DirectML"
        install_summary = f"{os_label} + {gpu_name} → {accel.upper() if accel != 'cpu' else 'CPU'}"
        install_hint = f"GPU: {gpu_name}. OCR dùng DirectML; các tác vụ không hỗ trợ sẽ tự dùng CPU."
    elif gpu_kind == "amd" and accel == "rocm":
        demucs_label = "Cài Demucs (CPU)"
        demucs_backend = "cpu"
        ocr_action = ""
        ocr_label = ""
        install_summary = f"{os_label} + {gpu_name} → ROCm"
        install_hint = f"GPU: {gpu_name}. Tác vụ PyTorch dùng ROCm khi tương thích, còn lại tự dùng CPU."
    else:
        demucs_label = "Cài Demucs (CPU)"
        demucs_backend = "cpu"
        ocr_action = ""
        ocr_label = ""
        install_summary = f"{os_label} · CPU ({arch}) — không phát hiện GPU tăng tốc"
        install_hint = "Không thấy NVIDIA/Apple GPU — Demucs chạy CPU (chậm hơn)."

    # ── Kế hoạch cài TẤT CẢ mục Thiết lập theo OS/GPU ──
    if os_id == "macos":
        py_link = "https://www.python.org/downloads/macos/"
        ff_cmd = "brew install ffmpeg"
        ff_link = ""
        ff_label = "Cài ffmpeg (brew)"
        ollama_link = "https://ollama.com/download/mac"
        node_link = "https://github.com/nvm-sh/nvm#installing-and-updating"
        node_label = "Cài Node.js qua NVM"
        tts_id, tts_name, tts_hint = "say", "macOS say", "TTS hệ thống macOS (có sẵn)."
        tts_install, tts_label = "", ""
    elif os_id == "windows":
        py_link = "https://www.python.org/downloads/windows/"
        ff_cmd = ""
        ff_link = "https://www.gyan.dev/ffmpeg/builds/"
        ff_label = "Tải ffmpeg (Windows)"
        ollama_link = "https://ollama.com/download/windows"
        node_link = "https://github.com/coreybutler/nvm-windows/releases"
        node_label = "Cài Node.js qua NVM for Windows"
        tts_id, tts_name = "espeak", "espeak-ng"
        tts_hint = "TTS hệ thống Windows/Linux (tuỳ chọn)."
        tts_install = "https://github.com/espeak-ng/espeak-ng/releases"
        tts_label = "Tải espeak-ng"
    else:  # linux / unknown
        py_link = "https://www.python.org/downloads/"
        ff_cmd = "sudo apt install ffmpeg"
        ff_link = ""
        ff_label = "Cài ffmpeg (apt)"
        ollama_link = "https://ollama.com/download/linux"
        node_link = "https://github.com/nvm-sh/nvm#installing-and-updating"
        node_label = "Cài Node.js qua NVM"
        tts_id, tts_name = "espeak", "espeak-ng"
        tts_hint = "TTS hệ thống Linux: sudo apt install espeak-ng"
        tts_install = "sudo apt install espeak-ng"
        tts_label = "Cài espeak-ng"

    pip = f'"{sys.executable}" -m pip install' if os_id == "windows" else f"{sys.executable} -m pip install"

    items_plan: dict[str, dict[str, Any]] = {
        "python": {
            "kind": "url",
            "value": py_link,
            "label": f"Tải Python ({os_label})",
            "hint": f"Cần Python ≥ 3.10 trên {os_label}.",
        },
        "ffmpeg": {
            "kind": "cmd" if ff_cmd else "url",
            "value": ff_cmd or ff_link,
            "label": ff_label,
            "hint": "Bắt buộc cắt audio / burn / mux.",
        },
        "ffprobe": {
            "kind": "cmd" if ff_cmd else "url",
            "value": ff_cmd or ff_link,
            "label": ff_label,
            "hint": "Thường đi kèm ffmpeg.",
        },
        "faster_whisper": {
            "kind": "cmd",
            "value": f"{pip} faster-whisper",
            "label": "Cài faster-whisper",
            "hint": "ASR giọng nói (Whisper).",
        },
        "rapidocr_onnxruntime": {
            "kind": "cmd",
            "value": f"{pip} rapidocr-onnxruntime",
            "label": "Cài RapidOCR",
            "hint": "OCR hardsub / nhãn trên khung.",
        },
        "httpx": {
            "kind": "cmd",
            "value": f"{pip} httpx",
            "label": "Cài httpx",
            "hint": "Gọi API dịch / TTS cloud.",
        },
        "PIL": {
            "kind": "cmd",
            "value": f"{pip} pillow",
            "label": "Cài Pillow",
            "hint": "Vẽ caption khi burn.",
        },
        "cv2": {
            "kind": "cmd",
            "value": f"{pip} opencv-python-headless",
            "label": "Cài OpenCV",
            "hint": "Xử lý khung OCR.",
        },
        "ocr_cuda": {
            "kind": "action",
            "value": ocr_action,
            "label": ocr_label or "OCR CUDA (không cần)",
            "hint": (
                "ONNX Runtime CUDA — NVIDIA."
                if gpu_kind == "nvidia"
                else "ONNX Runtime DirectML — AMD/Intel Windows."
                if accel == "directml"
                else "Máy này không có ONNX GPU provider được hỗ trợ."
            ),
            "relevant": gpu_kind == "nvidia" or accel == "directml",
        },
        "demucs": {
            "kind": "action",
            "value": "demucs_cuda",
            "label": demucs_label,
            "hint": install_hint,
            "relevant": True,
            "backend": demucs_backend,
        },
        tts_id: {
            "kind": "url" if (tts_install or "").startswith("http") else ("cmd" if tts_install else "none"),
            "value": tts_install,
            "label": tts_label or tts_name,
            "hint": tts_hint,
            "relevant": True,
            "name": tts_name,
        },
        "ollama": {
            "kind": "url",
            "value": ollama_link,
            "label": f"Tải Ollama ({os_label})",
            "hint": "Dịch local (tuỳ chọn).",
        },
        "node": {
            "kind": "action",
            "value": "nvm",
            "label": node_label,
            "hint": f"Nhấn Cài để tự cài NVM + Node.js LTS. Tài liệu: {node_link}",
        },
        "data": {
            "kind": "none",
            "value": "",
            "label": "",
            "hint": "Thư mục lưu project / cache / xuất.",
        },
    }

    actions = []
    if ocr_action:
        actions.append({"id": ocr_action, "label": ocr_label})
    actions.append({"id": "demucs_cuda", "label": demucs_label})

    vram_txt = f"{vram_mb} MB" if vram_mb else ""
    label_bits = [os_label, arch]
    if gpu_name:
        label_bits.append(gpu_name)
    if vram_txt:
        label_bits.append(vram_txt)
    label = " · ".join(label_bits)

    return {
        "os": os_id,
        "osLabel": os_label,
        "arch": arch,
        "appleSilicon": apple_silicon,
        "gpuKind": gpu_kind,
        "gpuName": gpu_name,
        "vramMb": vram_mb,
        "driver": driver,
        "accel": accel,
        "label": label,
        # A display adapter is not automatically a usable local-AI GPU.  For
        # example, Cirrus Logic on a headless Linux VPS is visible in lspci but
        # has no CUDA/ROCm/DirectML backend. Keep it in `gpus` for diagnostics,
        # while all local AI and setup decisions remain CPU-only.
        "hasGpu": gpu_kind in ("nvidia", "apple") or accel in ("cuda", "metal", "rocm", "directml"),
        "gpuCount": len(gpus),
        "hybridGpu": len({str(g.get("kind")) for g in gpus}) > 1,
        "gpus": gpus,
        "install": {
            "ocr": ocr_action,
            "ocrLabel": ocr_label,
            "demucs": "demucs_cuda",
            "demucsLabel": demucs_label,
            "demucsBackend": demucs_backend,
            "summary": install_summary,
            "hint": install_hint,
            "actions": actions,
            "items": items_plan,
        },
    }


def hardware() -> dict[str, str]:
    d = detect_device()
    return {
        "label": d["label"],
        "accel": str(d["accel"]),
        "os": str(d["os"]),
        "gpuKind": str(d["gpuKind"]),
        "gpuName": str(d.get("gpuName") or ""),
    }


def ffprobe_duration(path: Path) -> float:
    """Độ dài giây; file hỏng / ffprobe fail → 0.0 (không raise)."""
    try:
        out = subprocess.check_output(
            [
                _ff_bin("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float(out)
    except ValueError:
        return 0.0


def _has_audio_stream(path: Path) -> bool:
    try:
        out = subprocess.check_output(
            [
                _ff_bin("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            text=True,
            timeout=15,
        )
        return bool(out.strip())
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def filter_complex_args(graph: str) -> list[str]:
    """Inline filter graph. Homebrew ffmpeg has no -filter_complex_script (exit 8)."""
    return ["-filter_complex", graph]


def _retime_spans(
    duration: float,
    ordered: list[dict[str, Any]],
    base: float,
) -> list[tuple[float, float, float, float, float]]:
    """Build the same piecewise clock the Editor uses while playing.

    Segments are allowed to overlap (for example a long vertical OCR lane
    underneath a short Mid caption).  The old cursor walk consumed the first
    segment and silently ignored the later one, while the Editor deliberately
    prefers Mid, then label, then horizontal, then vertical.  Split at every
    boundary and choose one active lane for each interval so both paths use
    one clock.
    """
    if duration <= 0:
        return []

    def _num(value: Any, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _lane(segment: dict[str, Any]) -> int:
        # Keep this ordering in lock-step with frontend pickTimelineSeg().
        layout = str(segment.get("layout") or "horizontal").lower()
        return {"mid": 0, "label": 1, "horizontal": 2, "vertical": 3}.get(layout, 4)

    def _active_at(value: float) -> dict[str, Any] | None:
        active = [
            segment
            for segment in ordered
            if value >= max(0.0, _num(segment.get("start")))
            and value < min(duration, _num(segment.get("end"), _num(segment.get("start"))))
        ]
        if not active:
            return None
        best_lane = min(_lane(segment) for segment in active)
        candidates = [segment for segment in active if _lane(segment) == best_lane]
        if best_lane == 0:
            # solidMidAt() chooses the nearest start (the later Mid when
            # intervals overlap); stable order breaks exact ties.
            return max(
                enumerate(candidates),
                key=lambda item: (_num(item[1].get("start")), item[0]),
            )[1]
        # labels/horizontal/vertical use the first hit in the Editor's
        # segment order, matching Array.find() in pickTimelineSeg().
        return candidates[0]

    boundaries = {0.0, float(duration)}
    for segment in ordered:
        start = max(0.0, min(float(duration), _num(segment.get("start"))))
        end = max(start, min(float(duration), _num(segment.get("end"), start)))
        boundaries.add(start)
        boundaries.add(end)
    points = sorted(boundaries)
    raw: list[tuple[float, float, float]] = []
    for start, end in zip(points, points[1:]):
        if end <= start + 0.001:
            continue
        active = _active_at((start + end) / 2)
        segment_speed = _num(active.get("videoSpeed"), 1.0) if active else 1.0
        speed = max(0.25, min(2.0, base * segment_speed))
        if raw and abs(raw[-1][2] - speed) < 1e-6 and abs(raw[-1][1] - start) < 1e-6:
            raw[-1] = (raw[-1][0], end, speed)
        else:
            raw.append((start, end, speed))

    spans: list[tuple[float, float, float, float, float]] = []
    out_cursor = 0.0
    for start, end, speed in raw:
        out_end = out_cursor + (end - start) / speed
        spans.append((start, end, speed, out_cursor, out_end))
        out_cursor = out_end
    return spans


def retime_timeline_time(
    value: float,
    duration: float,
    segments: list[dict[str, Any]],
    *,
    base_speed: float = 1.0,
) -> float:
    """Map one source timeline timestamp through the preview playback rates."""
    base = max(0.25, min(2.0, float(base_speed or 1.0)))
    ordered = sorted((dict(s) for s in segments), key=lambda s: float(s.get("start") or 0))
    spans = _retime_spans(max(0.0, duration), ordered, base)
    source = max(0.0, min(max(0.0, duration), float(value or 0)))
    for source_start, source_end, speed, output_start, _output_end in spans:
        if source <= source_end + 1e-6:
            return output_start + max(0.0, min(source_end - source_start, source - source_start)) / speed
    return spans[-1][4] if spans else source


def _video_keyframes(video: Path) -> list[float]:
    """PTS keyframe — đọc packet header, không decode (nhanh cả video dài)."""
    try:
        outp = subprocess.run(
            [_ff_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=600,
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                if sys.platform == "win32" else 0
            ),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kfs: list[float] = []
    for line in outp.splitlines():
        bits = line.strip().split(",")
        if len(bits) >= 2 and "K" in bits[1]:
            try:
                kfs.append(float(bits[0]))
            except ValueError:
                pass
    return sorted(set(kfs))


def _strip_pix_fmt(args: list[str]) -> list[str]:
    """Frame CUDA không nhận -pix_fmt yuv420p (nvenc tự xử lý hw frame)."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "-pix_fmt":
            skip = True
            continue
        out.append(a)
    return out


def _hw_decode_args() -> list[str]:
    """NVDEC decode, frame ở lại GPU — chỉ cho graph KHÔNG đụng pixel
    (trim/setpts/concat). Filter vẽ (gblur/overlay…) là CPU, đừng dùng."""
    return (
        ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        if nvenc_available()
        else []
    )


def _retime_status(project_id: str | None, message: str, progress: int) -> None:
    if not project_id:
        return
    from .project import set_status

    set_status(project_id, step="export", progress=progress, message=message, running=True)


def _retime_segmented(
    video: Path,
    tmp_out: Path,
    spans: list[tuple[float, float, float, float, float]],
    merged: list[tuple[float, float, float, float, float]],
    duration: float,
    cache_dir: Path,
    key: str,
    project_id: str | None,
    has_audio: bool,
) -> bool:
    """Chỉ re-encode cửa sổ quanh span videoSpeed≠1 (nới về keyframe, NVDEC→NVENC),
    copy packet phần còn lại, audio atempo một lần. False = dùng full graph."""
    import bisect

    speedy = [sp for sp in merged if abs(sp[2] - 1.0) > 0.001]
    if not speedy:
        return False
    kfs = _video_keyframes(video)
    if len(kfs) < 3:
        return False
    wins: list[list[float]] = []
    for s, e, _sp, _o1, _o2 in speedy:
        ia = bisect.bisect_right(kfs, s + 1e-6) - 1
        ka = kfs[ia] if ia >= 0 else 0.0
        ib = bisect.bisect_left(kfs, e - 1e-6)
        kb = kfs[ib] if ib < len(kfs) else duration
        if wins and ka <= wins[-1][1] + 0.5:
            wins[-1][1] = max(wins[-1][1], kb)
        else:
            wins.append([ka, min(kb, duration)])
    enc_total = sum(b - a for a, b in wins)
    if enc_total > 0.85 * duration:
        return False  # gần như cả video phải encode — full graph một lệnh gọn hơn

    # fps → nửa khung: cắt segment muxer NGAY TRƯỚC keyframe = packet-chính-xác
    try:
        rate = subprocess.run(
            [_ff_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=60,
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                if sys.platform == "win32" else 0
            ),
        ).stdout.strip()
        num, _s, den = rate.partition("/")
        fps = float(num) / float(den or 1)
    except (OSError, ValueError, ZeroDivisionError, subprocess.SubprocessError):
        fps = 25.0
    eps = max(0.008, 0.5 / max(1.0, fps))

    pieces: list[tuple[float, float, bool]] = []
    cur = 0.0
    for a, b in wins:
        if a > cur + 1e-3:
            pieces.append((cur, a, False))
        pieces.append((a, min(b, duration), True))
        cur = min(b, duration)
    if cur < duration - 1e-3:
        pieces.append((cur, duration, False))

    tdir = cache_dir / f"retime_seg_{key}"
    shutil.rmtree(tdir, ignore_errors=True)
    tdir.mkdir(parents=True, exist_ok=True)
    try:
        cuts = ",".join(f"{p[0] - eps:.6f}" for p in pieces[1:])
        run_cmd(
            project_id,
            [_ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
             "-map", "0:v", "-an", "-c", "copy", "-f", "segment",
             "-segment_times", cuts, "-reset_timestamps", "1",
             str(tdir / "p_%d.mp4")],
        )
        files = [tdir / f"p_{i}.mp4" for i in range(len(pieces))]
        if not all(f.is_file() for f in files):
            return False

        hw = _hw_decode_args()
        n_win = sum(1 for p in pieces if p[2])
        wi = 0
        parts: list[Path] = []
        for i, (a, b, is_win) in enumerate(pieces):
            if not is_win:
                parts.append(files[i])
                continue
            wi += 1
            _retime_status(
                project_id,
                f"Giãn thời lượng theo TTS · đoạn {wi}/{n_win}"
                + (" · GPU" if hw else ""),
                3 + int(11 * wi / max(1, n_win)),
            )
            subs = [sp for sp in spans if sp[1] > a + 1e-6 and sp[0] < b - 1e-6]
            filt: list[str] = []
            labels: list[str] = []
            for j, (s, e, spd, _o1, _o2) in enumerate(subs):
                s2, e2 = max(s, a) - a, min(e, b) - a
                filt.append(
                    f"[0:v]trim=start={s2:.6f}:end={e2:.6f},"
                    f"setpts=(PTS-STARTPTS)/{max(0.25, min(4.0, spd)):.6f}[v{j}]"
                )
                labels.append(f"[v{j}]")
            filt.append("".join(labels) + f"concat=n={len(subs)}:v=1:a=0[vout]")
            fc = tdir / f"fc_{i}.txt"
            fc.write_text(";\n".join(filt) + "\n", encoding="utf-8")
            enc = tdir / f"e_{i}.mp4"

            def _enc_cmd(hw_args: list[str]) -> list[str]:
                enc_args = h264_encoder_args(fast=True)
                if hw_args:
                    enc_args = _strip_pix_fmt(enc_args)
                return [_ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                        *hw_args, "-i", str(files[i]),
                        *filter_complex_args(fc.read_text(encoding="utf-8")), "-map", "[vout]",
                        *enc_args, "-an", str(enc)]

            try:
                run_cmd(project_id, _enc_cmd(hw))
            except (RuntimeError, subprocess.CalledProcessError):
                if not hw:
                    raise
                hw = []  # NVDEC lỗi (codec lạ…) → CPU decode, vẫn NVENC encode
                run_cmd(project_id, _enc_cmd([]))
            parts.append(enc)

        lst = tdir / "concat.txt"
        lst.write_text(
            "\n".join(f"file '{str(f).replace(chr(92), '/')}'" for f in parts) + "\n",
            encoding="utf-8",
        )
        if has_audio:
            afilt: list[str] = []
            albl: list[str] = []
            for j, (s, e, spd, _o1, _o2) in enumerate(spans):
                afilt.append(
                    f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS,"
                    f"{atempo_chain(max(0.25, min(4.0, spd)))}[a{j}]"
                )
                albl.append(f"[a{j}]")
            afilt.append("".join(albl) + f"concat=n={len(albl)}:v=0:a=1[aout]")
            afc = tdir / "afc.txt"
            afc.write_text(";\n".join(afilt) + "\n", encoding="utf-8")
            aud = tdir / "aud.m4a"
            run_cmd(
                project_id,
                [_ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(video), *filter_complex_args(afc.read_text(encoding="utf-8")),
                 "-map", "[aout]", "-c:a", "aac", "-b:a", "128k", str(aud)],
            )
            # Nối + ghép audio một lệnh — video chỉ ghi đĩa một lượt
            run_cmd(
                project_id,
                [_ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(aud),
                 "-map", "0:v", "-map", "1:a", "-c", "copy",
                 "-map_metadata", "-1", "-map_chapters", "-1", str(tmp_out)],
            )
        else:
            run_cmd(
                project_id,
                [_ff_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(lst),
                 "-c", "copy", "-an", str(tmp_out)],
            )
        expect = spans[-1][4] if spans else duration
        got = ffprobe_duration(tmp_out)
        if abs(got - expect) > 0.25:
            tmp_out.unlink(missing_ok=True)
            return False
        return True
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


def retime_video_segments(
    video: Path,
    segments: list[dict[str, Any]],
    cache_dir: Path,
    project_id: str | None = None,
    *,
    base_speed: float = 1.0,
) -> tuple[Path, list[dict[str, Any]]]:
    """Retime speech spans and return segments mapped onto output timeline.

    ``base_speed`` mirrors the Editor's soft prefer-video playback rate; the
    per-segment ``videoSpeed`` is multiplied on top of it.
    """
    duration = ffprobe_duration(video)
    base = max(0.25, min(2.0, float(base_speed or 1.0)))
    ordered = sorted((dict(s) for s in segments), key=lambda s: float(s.get("start") or 0))
    if duration <= 0 or (
        abs(base - 1.0) <= 0.001
        and not any(abs(float(s.get("videoSpeed") or 1) - 1.0) > 0.001 for s in ordered)
    ):
        return video, ordered

    stat = video.stat()
    signature = [
         (s.get("id"), round(float(s.get("start") or 0), 4), round(float(s.get("end") or 0), 4),
         round(base * float(s.get("videoSpeed") or 1), 3))
        for s in ordered
    ]
    key = hashlib.sha1(
        json.dumps([str(video.resolve()), stat.st_size, stat.st_mtime_ns, signature]).encode()
    ).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"retimed_{key}.mp4"

    spans = _retime_spans(duration, ordered, base)

    def map_time(value: float) -> float:
        for source_start, source_end, speed, output_start, output_end in spans:
            if value <= source_end + 1e-6:
                return output_start + max(0.0, min(source_end - source_start, value - source_start)) / speed
        return spans[-1][4] if spans else value

    remapped = []
    for segment in ordered:
        mapped = dict(segment)
        mapped["start"] = map_time(float(segment.get("start") or 0))
        mapped["end"] = map_time(float(segment.get("end") or 0))
        if segment.get("coverStart") is not None:
            try:
                mapped["coverStart"] = map_time(float(segment["coverStart"]))
            except (TypeError, ValueError):
                pass
        if segment.get("coverEnd") is not None:
            try:
                mapped["coverEnd"] = map_time(float(segment["coverEnd"]))
            except (TypeError, ValueError):
                pass
        # speed đã “nướng” vào timeline — đừng retime lần 2
        mapped.pop("videoSpeed", None)
        remapped.append(mapped)

    if not out.exists():
        # Gộp span liền kề cùng speed → ít node filter (tránh WinError 206)
        merged: list[tuple[float, float, float, float, float]] = []
        for sp in spans:
            if (
                merged
                and abs(merged[-1][2] - sp[2]) < 1e-6
                and abs(merged[-1][1] - sp[0]) < 1e-4
            ):
                prev = merged[-1]
                merged[-1] = (prev[0], sp[1], prev[2], prev[3], sp[4])
            else:
                merged.append(sp)
        has_audio = _has_audio_stream(video)

        # Đường nhanh: chỉ encode cửa sổ quanh span đổi tốc độ (NVDEC→NVENC),
        # copy packet phần còn lại — video dài chỉnh vài câu gần như miễn phí.
        seg_tmp = out.with_name(f"{out.stem}.seg_tmp{out.suffix}")
        seg_tmp.unlink(missing_ok=True)
        try:
            if _retime_segmented(
                video, seg_tmp, spans, merged, duration,
                cache_dir, key, project_id, has_audio,
            ):
                _atomic_replace(seg_tmp, out)
                return out, remapped
        except (RuntimeError, subprocess.CalledProcessError):
            pass  # lỗi ffmpeg thật → full graph bên dưới (Cancelled vẫn ném qua)
        finally:
            seg_tmp.unlink(missing_ok=True)
        _retime_status(
            project_id,
            "Giãn thời lượng theo TTS (toàn bộ video"
            + (" · GPU" if _hw_decode_args() else "") + ")…",
            4,
        )
        filters: list[str] = []
        labels: list[str] = []
        for i, (start, end, speed, _out_start, _out_end) in enumerate(merged):
            sp = max(0.25, min(4.0, float(speed) or 1.0))
            filters.append(
                f"[0:v]trim=start={start:.6f}:end={end:.6f},"
                f"setpts=(PTS-STARTPTS)/{sp:.6f}[v{i}]"
            )
            if has_audio:
                a_chain = atempo_chain(sp)
                filters.append(
                    f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
                    f"{a_chain}[a{i}]"
                )
                labels.append(f"[v{i}][a{i}]")
            else:
                labels.append(f"[v{i}]")
        n = len(merged)
        if has_audio:
            filters.append("".join(labels) + f"concat=n={n}:v=1:a=1[vout][aout]")
        else:
            filters.append("".join(labels) + f"concat=n={n}:v=1:a=0[vout]")
        # Script file — không nhét filter_complex vào argv (Windows MAX_PATH / 206)
        fc_path = cache_dir / f"retimed_{key}_fc.txt"
        fc_path.write_text(";\n".join(filters) + "\n", encoding="utf-8")
        try:
            hw_args = _hw_decode_args()  # trim/setpts không đụng pixel → NVDEC được
            cmd = [
                _ff_bin("ffmpeg"), 
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                *hw_args,
                "-i",
                str(video),
                *filter_complex_args(fc_path.read_text(encoding="utf-8")),
                "-map",
                "[vout]",
            ]
            enc_args = (
                _strip_pix_fmt(h264_encoder_args(fast=True))
                if hw_args
                else h264_encoder_args(fast=True)
            )
            if has_audio:
                cmd += ["-map", "[aout]", *enc_args, "-c:a", "aac", "-b:a", "128k"]
            else:
                cmd += [*enc_args, "-an"]
            # Ghi tmp rồi rename: huỷ/crash giữa chừng để lại file cụt mà
            # lần chạy sau vẫn coi là cache hợp lệ (gate chỉ check exists()).
            tmp_out = out.with_name(f"{out.stem}.tmp{out.suffix}")
            tmp_out.unlink(missing_ok=True)
            cmd += ["-map_metadata", "-1", "-map_chapters", "-1", str(tmp_out)]
            try:
                try:
                    run_cmd(project_id, cmd)
                except RuntimeError:
                    if not hw_args:
                        raise
                    # NVDEC không ăn codec này → CPU decode, NVENC vẫn encode
                    cmd = [c for c in cmd if c not in ("-hwaccel", "cuda", "-hwaccel_output_format")]
                    run_cmd(project_id, cmd)
                _atomic_replace(tmp_out, out)
            except BaseException:
                tmp_out.unlink(missing_ok=True)
                raise
        finally:
            try:
                fc_path.unlink(missing_ok=True)
            except OSError:
                pass
    return out, remapped


def retime_audio_track(
    audio: Path,
    segments: list[dict[str, Any]],
    cache_dir: Path,
    project_id: str | None = None,
    *,
    base_speed: float = 1.0,
    source_start: float = 0.0,
    source_duration: float | None = None,
) -> Path:
    """Apply the video preview clock to a cached external stem track."""
    full_duration = ffprobe_duration(audio)
    offset = max(0.0, float(source_start or 0))
    duration = max(
        0.0,
        min(
            full_duration - offset,
            float(source_duration) if source_duration is not None else full_duration - offset,
        ),
    )
    base = max(0.25, min(2.0, float(base_speed or 1.0)))
    ordered = sorted((dict(s) for s in segments), key=lambda s: float(s.get("start") or 0))
    has_speed = any(abs(float(s.get("videoSpeed") or 1) - 1.0) > 0.001 for s in ordered)
    if duration <= 0 or (
        offset <= 0.001
        and abs(duration - full_duration) <= 0.02
        and abs(base - 1.0) <= 0.001
        and not has_speed
    ):
        return audio

    stat = audio.stat()
    signature = [
        (
            s.get("id"),
            round(float(s.get("start") or 0), 4),
            round(float(s.get("end") or 0), 4),
            round(base * float(s.get("videoSpeed") or 1), 3),
        )
        for s in ordered
    ]
    key = hashlib.sha1(
        json.dumps(
            [str(audio.resolve()), stat.st_size, stat.st_mtime_ns, offset, duration, signature]
        ).encode()
    ).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"retimed_audio_{key}.wav"
    if out.exists():
        return out

    spans = _retime_spans(duration, ordered, base)
    filters: list[str] = []
    labels: list[str] = []
    for i, (start, end, speed, _out_start, _out_end) in enumerate(spans):
        filters.append(
            f"[0:a]atrim=start={offset + start:.6f}:end={offset + end:.6f},"
            f"asetpts=PTS-STARTPTS,{atempo_chain(speed)}[a{i}]"
        )
        labels.append(f"[a{i}]")
    filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    fc_path = cache_dir / f"retimed_audio_{key}_fc.txt"
    fc_path.write_text(";\n".join(filters) + "\n", encoding="utf-8")
    tmp_out = out.with_name(f"{out.stem}.tmp{out.suffix}")
    tmp_out.unlink(missing_ok=True)
    try:
        run_cmd(
            project_id,
            [
                _ff_bin("ffmpeg"), 
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(audio),
                *filter_complex_args(fc_path.read_text(encoding="utf-8")),
                "-map",
                "[aout]",
                "-c:a",
                "pcm_s16le",
                str(tmp_out),
            ],
        )
        _atomic_replace(tmp_out, out)
    except BaseException:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        fc_path.unlink(missing_ok=True)
    return out


def ensure_preview_clip(
    source: Path, dest: Path, sec: float, project_id: str | None = None, *, start: float = 0
) -> Path:
    """Cắt N giây đầu để thử nhanh; cache theo dest path.

    Ghi *.tmp.mp4 rồi rename — tránh Range vào file đang ghi (416).
    Không dùng .mp4.tmp: ffmpeg/nvenc không nhận extension → exit -22.
    """
    if not source.is_file() or source.stat().st_size < 64:
        raise RuntimeError(f"SOURCE_ERROR: không đọc được video {source}")
    if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.tmp{dest.suffix}")  # preview_10.tmp.mp4
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    has_a = _has_audio_stream(source)
    maps = ["-map", "0:v:0"] + (["-map", "0:a:0"] if has_a else [])
    audio_args = ["-c:a", "aac", "-ac", "2", "-ar", "44100"] if has_a else ["-an"]
    copy_cmd = [
        _ff_bin("ffmpeg"), "-y",
        "-ss", str(start), "-t", str(sec), "-i", str(source),
        *maps,
        *(["-an"] if not has_a else []),
        "-c", "copy", "-avoid_negative_ts", "make_zero", str(tmp),
    ]
    try:
        run_cmd(project_id, copy_cmd)
    except Exception:
        try:
            run_cmd(
                project_id,
                [
                    _ff_bin("ffmpeg"), "-y",
                    "-ss", str(start), "-t", str(sec), "-i", str(source),
                    *maps,
                    *h264_encoder_args(fast=True),
                    *audio_args,
                    str(tmp),
                ],
            )
        except Exception:
            try:
                # videotoolbox can exit 254 on silent/odd review compiles
                run_cmd(
                    project_id,
                    [
                        _ff_bin("ffmpeg"), "-y",
                        "-ss", str(start), "-t", str(sec), "-i", str(source),
                        *maps,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-pix_fmt", "yuv420p",
                        *audio_args,
                        str(tmp),
                    ],
                )
            except Exception:
                run_cmd(
                    project_id,
                    [
                        _ff_bin("ffmpeg"), "-y",
                        "-ss", str(start), "-t", str(sec), "-i", str(source),
                        "-map", "0:v:0", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-pix_fmt", "yuv420p",
                        str(tmp),
                    ],
                )
    _atomic_replace(tmp, dest)
    return dest


def ensure_playback_speed(
    source: Path,
    dest: Path,
    speed: float = 1.0,
    project_id: str | None = None,
    *,
    force: bool = False,
) -> Path:
    """Bake tốc độ người dùng chọn vào file.

    speed < 1 = chậm hơn (dài hơn): setpts *= 1/speed, atempo = speed.

    Encode trung gian: ultrafast/p1 + CRF cao — chỉ phục vụ ASR/timeline,
    không phải file xuất cuối (xuất encode lại sau).
    """
    speed = max(0.5, min(2.0, float(speed)))
    if abs(speed - 1.0) < 0.001:
        return source
    if (
        not force
        and dest.exists()
        and dest.stat().st_mtime >= source.stat().st_mtime
        and dest.stat().st_size > 1024
    ):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.tmp{dest.suffix}")
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    pts = 1.0 / speed
    has_a = _has_audio_stream(source)
    # Bake pipeline: ưu tiên tốc độ, chất lượng vừa đủ cho Whisper/OCR
    if nvenc_available():
        vcodec = [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ll",
            "-rc",
            "vbr",
            "-cq",
            "28",
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        vcodec = [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "0",
        ]
    cmd = [
        _ff_bin("ffmpeg"), 
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-filter:v",
        f"setpts={pts:.6f}*PTS",
        *vcodec,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]
    if has_a:
        # atempo chỉ 0.5–2.0 — speed 0.80 ok 1 bước; AAC 128k đủ cho ASR
        cmd += ["-filter:a", f"atempo={speed:.6f}", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd.append(str(tmp))
    try:
        run_cmd(project_id, cmd)
        _atomic_replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return dest


# Facade: speed/timeline-baseline chuyển sang core/speed_timeline.py —
# giữ import cũ `from pipeline.core.media import ...` hoạt động.
from .speed_timeline import (  # noqa: F401
    _SEG_TIME_KEYS,
    apply_timeline_from_baseline,
    clamp_playback_speed,
    ensure_project_initial_playback_rate,
    ensure_timeline_baseline,
    initial_rate_from_match_duration,
    invalidate_timeline_baseline,
    meta_baked_speed,
    meta_has_user_bake,
    preview_1x_path,
    preview_clip_matches,
    remap_timeline_for_speed_change,
    scale_time_fields,
    speed_cache_tag,
    _snapshot_timeline_1x,
)


def extract_audio(video: Path, wav: Path, project_id: str | None = None) -> None:
    run_cmd(
        project_id,
        [
            _ff_bin("ffmpeg"), 
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
    )

def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            _ff_bin("ffprobe"), 
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)


def video_codec(path: Path) -> str:
    return subprocess.check_output(
        [
            _ff_bin("ffprobe"), 
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip().lower()


def encode_export_1080(
    src: Path,
    dst: Path,
    project_id: str | None = None,
    target_height: int | None = 1080,
    *,
    crop: tuple[int, int, int, int] | None = None,
    video_scale_x: float = 100.0,
    video_scale_y: float = 100.0,
) -> Path:
    """Encode H.264 cuối: crop (tuỳ chọn) + scale trong **một** pass ffmpeg.

    crop = (x, y, w, h) pixel nguồn — gộp với scale để khỏi encode 2 lần.
    """
    w, h = video_size(src)
    # Sau crop: kích thước frame đầu vào scale
    if crop is not None:
        _cx, _cy, cw, ch = crop
        in_w, in_h = int(cw), int(ch)
    else:
        in_w, in_h = w, h

    vf_parts = export_transform_filters(
        w, h, crop, target_height, video_scale_x, video_scale_y
    )
    already_target = not vf_parts

    if already_target and crop is None and video_codec(src) == "h264":
        video_args = ["-c:v", "copy"]
    elif vf_parts:
        video_args = ["-vf", ",".join(vf_parts), *h264_encoder_args()]
    else:
        video_args = h264_encoder_args()

    tmp = dst.with_suffix(".tmp_export.mp4")
    tmp.unlink(missing_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        project_id,
        [
            _ff_bin("ffmpeg"), 
            "-y",
            "-i",
            str(src),
            *video_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            str(tmp),
        ],
    )
    _atomic_replace(tmp, dst)
    return dst


def export_transform_filters(
    source_w: int,
    source_h: int,
    crop: tuple[int, int, int, int] | None,
    target_height: int | None,
    video_scale_x: float = 100.0,
    video_scale_y: float = 100.0,
) -> list[str]:
    """FFmpeg transform khớp preview: scale lớp video rồi crop/pad vào canvas."""
    zx = max(0.01, min(5.0, float(video_scale_x) / 100.0))
    zy = max(0.01, min(5.0, float(video_scale_y) / 100.0))
    cx, cy, cw, ch = crop or (0, 0, source_w, source_h)
    parts: list[str] = []
    if abs(zx - 1.0) < 1e-6 and abs(zy - 1.0) < 1e-6:
        if crop is not None:
            parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
    else:
        sw = max(2, int(round(source_w * zx)) // 2 * 2)
        sh = max(2, int(round(source_h * zy)) // 2 * 2)
        cut_w, cut_h = min(cw, sw), min(ch, sh)
        center_x = (cx + cw / 2) * zx
        center_y = (cy + ch / 2) * zy
        x = max(0, min(sw - cut_w, int(round(center_x - cut_w / 2))))
        y = max(0, min(sh - cut_h, int(round(center_y - cut_h / 2))))
        parts.extend([
            f"scale={sw}:{sh}",
            f"crop={cut_w}:{cut_h}:{x}:{y}",
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:black",
        ])
    if target_height:
        if ch >= cw and cw != int(target_height):
            parts.append(f"scale={int(target_height)}:-2")
        elif ch < cw and ch != int(target_height):
            parts.append(f"scale=-2:{int(target_height)}")
    return parts


# Khớp LivePreviewEditor.ASPECT_PRESETS (w/h ratio)
_ASPECT_PRESETS: dict[str, tuple[float, float]] = {
    "16:9": (16, 9),
    "4:3": (4, 3),
    "2.35:1": (235, 100),
    "2:1": (2, 1),
    "1.85:1": (185, 100),
    "9:16": (9, 16),
    "3:4": (3, 4),
    "58inch": (108, 234),
    "1:1": (1, 1),
}


def resolve_export_crop(
    source_w: int,
    source_h: int,
    preset_id: str,
    custom: dict[str, float] | None = None,
) -> tuple[int, int, int, int] | None:
    """Crop giống resolveCropRect FE — preset + pan (previewCrop x/y) hoặc custom."""
    if source_w <= 0 or source_h <= 0:
        return None
    key = (preset_id or "original").strip()
    if key == "custom" and custom:
        x = max(0.0, min(0.95, float(custom.get("x", 0)))) * source_w
        y = max(0.0, min(0.95, float(custom.get("y", 0)))) * source_h
        w = max(0.05, min(1.0 - x / source_w, float(custom.get("w", 1)))) * source_w
        h = max(0.05, min(1.0 - y / source_h, float(custom.get("h", 1)))) * source_h
    elif key in ("", "original", "custom"):
        return None
    else:
        dims = _ASPECT_PRESETS.get(key)
        if not dims:
            return None
        tw, th = dims
        target = tw / th
        source = source_w / source_h
        if source >= target:
            h = float(source_h)
            w = h * target
        else:
            w = float(source_w)
            h = w / target
        # Pan từ previewCrop (x,y normalized) — mặc định giữa
        if custom is not None:
            try:
                nx = float(custom.get("x", (source_w - w) / 2.0 / source_w))
                ny = float(custom.get("y", (source_h - h) / 2.0 / source_h))
            except (TypeError, ValueError):
                nx = (source_w - w) / 2.0 / max(1.0, float(source_w))
                ny = (source_h - h) / 2.0 / max(1.0, float(source_h))
            max_nx = max(0.0, 1.0 - w / source_w)
            max_ny = max(0.0, 1.0 - h / source_h)
            x = max(0.0, min(max_nx, nx)) * source_w
            y = max(0.0, min(max_ny, ny)) * source_h
        else:
            x = (source_w - w) / 2.0
            y = (source_h - h) / 2.0
    xi = max(0, int(round(x)))
    yi = max(0, int(round(y)))
    wi = int(round(w))
    hi = int(round(h))
    # H.264 cần chẵn
    wi -= wi % 2
    hi -= hi % 2
    xi -= xi % 2
    yi -= yi % 2
    xi = max(0, min(source_w - wi, xi))
    yi = max(0, min(source_h - hi, yi))
    if wi < 2 or hi < 2:
        return None
    if wi >= source_w - 1 and hi >= source_h - 1:
        return None
    return xi, yi, wi, hi


def crop_export_aspect(
    src: Path,
    dst: Path,
    preset_id: str,
    *,
    custom: dict[str, float] | None = None,
    project_id: str | None = None,
) -> Path:
    """Cắt khung theo previewAspectRatio (sau burn, trước encode 1080)."""
    sw, sh = video_size(src)
    crop = resolve_export_crop(sw, sh, preset_id, custom)
    if crop is None:
        if src.resolve() != dst.resolve():
            import shutil

            shutil.copy2(src, dst)
        return dst
    x, y, w, h = crop
    tmp = dst.with_suffix(".tmpcrop.mp4")
    tmp.unlink(missing_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        project_id,
        [
            _ff_bin("ffmpeg"), 
            "-y",
            "-i",
            str(src),
            "-vf",
            f"crop={w}:{h}:{x}:{y}",
            *h264_encoder_args(),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            str(tmp),
        ],
    )
    _atomic_replace(tmp, dst)
    return dst
