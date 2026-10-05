"""Multi-vendor GPU manager. Does not hard-code a single NVIDIA/CUDA path."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Any

from pipeline.core.accel import preferred_torch_device
from pipeline.core.media import detect_device, h264_hardware_encoder, nvenc_available
from pipeline.core.resources import gpu_job_cap

_lock = threading.Lock()
_resident: dict[str, str] = {}  # model_role -> device_id


def list_gpus() -> list[dict[str, Any]]:
    info = detect_device()
    gpus = list(info.get("gpus") or [])
    if not gpus:
        gpus = [{
            "index": 0,
            "name": info.get("gpuName") or "CPU",
            "kind": info.get("gpuKind") or "none",
            "vramMb": info.get("vramMb"),
            "driver": info.get("driver") or "",
            "accel": info.get("accel") or "cpu",
            "source": "detect_device",
        }]
    torch_dev = preferred_torch_device()
    for gpu in gpus:
        gpu["compute"] = _compute_backend(str(gpu.get("kind") or ""), torch_dev)
        gpu["encode"] = _encode_hint(str(gpu.get("kind") or ""))
        gpu["decode"] = gpu["encode"]
    return gpus


def _compute_backend(kind: str, torch_dev: str) -> str:
    from pipeline.core.runtime_active import cpu_mode_enabled
    if cpu_mode_enabled():
        return 'cpu'
    if kind == "nvidia" or torch_dev == "cuda":
        return "cuda"
    if kind == "apple" or torch_dev == "mps":
        return "mps"
    if kind in ("amd", "intel") and sys.platform == "win32":
        return "directml"
    return "cpu"


def _encode_hint(kind: str) -> str:
    enc = h264_hardware_encoder() or "libx264"
    if kind == "nvidia" and nvenc_available():
        return "nvenc"
    if kind == "apple":
        return "videotoolbox" if enc == "h264_videotoolbox" else enc
    if kind == "intel":
        return "qsv" if enc == "h264_qsv" else enc
    if kind == "amd":
        return "amf" if enc == "h264_amf" else enc
    return enc


def vram_free_mb(index: int = 0) -> int | None:
    if not shutil.which("nvidia-smi") and sys.platform == "win32":
        return None
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                f"--id={index}",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=4,
            stderr=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0,
        ).strip()
        return int(float(out.splitlines()[0]))
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, OSError, IndexError):
        return None


def assign_device(task: str, *, need_mb: int = 0) -> dict[str, Any]:
    """Pick a device for a task. Falls back to CPU when VRAM is insufficient."""
    gpus = list_gpus()
    prefer_compute = task in {"vision", "asr", "tts", "llm", "embedding", "clone_ai"}
    ranked = sorted(
        enumerate(gpus),
        key=lambda item: (
            0 if prefer_compute and str(item[1].get("compute")) in ("cuda", "mps") else 1,
            -(int(item[1].get("vramMb") or 0)),
        ),
    )
    with _lock:
        sticky = _resident.get(task)
    for idx, gpu in ranked:
        device_id = f"{gpu.get('compute')}:{idx}"
        if sticky and sticky != device_id and task == "vision":
            continue
        free = vram_free_mb(int(gpu.get("index") or idx))
        if need_mb and free is not None and free < need_mb:
            continue
        chosen = {
            "deviceId": device_id,
            "index": int(gpu.get("index") or idx),
            "name": gpu.get("name"),
            "compute": gpu.get("compute"),
            "encode": gpu.get("encode"),
            "kind": gpu.get("kind"),
        }
        if task == "vision":
            with _lock:
                _resident[task] = device_id
        if chosen["compute"] == "cuda":
            os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
        return chosen
    return {
        "deviceId": "cpu:0",
        "index": 0,
        "name": "CPU",
        "compute": "cpu",
        "encode": h264_hardware_encoder() or "libx264",
        "kind": "none",
    }


def model_residency(role: str | None = None) -> dict[str, str]:
    with _lock:
        if role:
            return {role: _resident[role]} if role in _resident else {}
        return dict(_resident)


def diagnostics() -> dict[str, Any]:
    info = detect_device()
    gpus = list_gpus()
    return {
        "os": info.get("os"),
        "osLabel": info.get("osLabel"),
        "arch": info.get("arch"),
        "accel": info.get("accel"),
        "torchDevice": preferred_torch_device(),
        "encoder": h264_hardware_encoder() or "libx264",
        "gpuJobCap": gpu_job_cap(),
        "gpus": gpus,
        "residency": model_residency(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }
