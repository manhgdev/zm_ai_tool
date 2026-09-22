"""Transactional installer for prebuilt Windows AI runtime packs.

Desktop releases never resolve packages on the user's machine.  CI publishes a
manifest plus immutable ZIP layers; this module resumes downloads, verifies
SHA-256, extracts safely, probes the staged interpreter, then atomically changes
the active-runtime pointer.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime_active import active_runtime_dir, runtime_home

DEFAULT_MANIFEST_URL = (
    "https://github.com/manhgdev/zm_ai_tool/releases/latest/download/"
    "runtime-manifest.json"
)
STAGES = (
    "detect_hardware", "fetch_manifest", "download", "verify_checksum",
    "extract", "probe", "activate", "rollback",
)
Progress = Callable[[int, str, dict[str, Any]], None]
_POINTER_LOCK = threading.Lock()


def _is_windows_desktop() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _manifest_url() -> str:
    configured = (os.environ.get("ZM_AI_TOOL_RUNTIME_MANIFEST_URL") or "").strip()
    if configured:
        return configured
    version = (os.environ.get("ZM_AI_TOOL_VERSION") or "").strip().removeprefix("v")
    if version:
        return f"https://github.com/manhgdev/zm_ai_tool/releases/download/v{version}/runtime-manifest.json"
    return DEFAULT_MANIFEST_URL


class RuntimePackError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool, diagnostics: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.diagnostics = diagnostics or message


@dataclass(frozen=True)
class Pack:
    pack_id: str
    url: str
    sha256: str
    size: int
    unpacked_size: int


def _emit(progress: Progress | None, value: int, message: str, **fields: Any) -> None:
    if progress:
        progress(value, message, fields)


def _load_manifest(url: str) -> tuple[dict[str, Any], str]:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read()
            final_url = response.geturl()
    except Exception as exc:
        raise RuntimePackError(
            "DOWNLOAD_FAILED",
            f"Không tải được runtime manifest / Could not download runtime manifest: {exc}",
            retryable=True,
        ) from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimePackError("CHECKSUM_MISMATCH", "Runtime manifest không hợp lệ / Invalid runtime manifest", retryable=True) from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise RuntimePackError("RUNTIME_PROBE_FAILED", "Runtime manifest không tương thích / Incompatible runtime manifest", retryable=False)
    return manifest, final_url


def _hardware_pack() -> str:
    from .media import detect_device

    device = detect_device()
    kind = str(device.get("gpuKind") or "none")
    accel = str(device.get("accel") or "cpu")
    if kind == "nvidia":
        from .accel import nvidia_smi_executable

        try:
            proc = subprocess.run(
                [nvidia_smi_executable(), "--query-gpu=driver_version,compute_cap", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            rows = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if proc.returncode != 0 or not rows:
                raise OSError((proc.stderr or proc.stdout or "nvidia-smi returned no GPU").strip())
            parsed = [(float(part[0].strip()), float(part[1].strip())) for line in rows if len(part := line.split(",")) >= 2]
            if not parsed:
                raise ValueError("nvidia-smi did not return driver/compute capability")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RuntimePackError(
                "DRIVER_TOO_OLD",
                "Không xác minh được NVIDIA driver bằng nvidia-smi / Could not validate the NVIDIA driver",
                retryable=False,
                diagnostics=str(exc),
            ) from exc
        needs_cu128 = any(compute >= 10.0 for _, compute in parsed)
        minimum_driver = 570.65 if needs_cu128 else 551.78
        if any(driver < minimum_driver for driver, _ in parsed):
            raise RuntimePackError(
                "DRIVER_TOO_OLD",
                f"NVIDIA driver quá cũ: cần >= {minimum_driver:.0f} / NVIDIA driver is too old",
                retryable=False,
                diagnostics=json.dumps({"gpus": parsed, "minimumDriver": minimum_driver}),
            )
        return "gpu-nvidia-cu128" if needs_cu128 else "gpu-nvidia-cu124"
    if sys.platform == "win32" and kind in ("amd", "intel", "other") and accel == "directml":
        return "gpu-directml"
    if kind in ("none", "apple") or accel == "cpu":
        return "cpu"
    raise RuntimePackError(
        "HARDWARE_UNSUPPORTED",
        f"GPU chưa được hỗ trợ / Unsupported GPU: {device.get('gpuName') or kind}",
        retryable=False,
        diagnostics=json.dumps(device, ensure_ascii=False),
    )


def _pack_from_manifest(manifest: dict[str, Any], pack_id: str, manifest_url: str) -> Pack:
    raw = (manifest.get("packs") or {}).get(pack_id)
    if not isinstance(raw, dict):
        raise RuntimePackError("HARDWARE_UNSUPPORTED", f"Release thiếu runtime pack: {pack_id}", retryable=False)
    sha = str(raw.get("sha256") or "").lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimePackError("CHECKSUM_MISMATCH", f"Manifest thiếu SHA-256 hợp lệ: {pack_id}", retryable=False)
    return Pack(
        pack_id=pack_id,
        url=urllib.parse.urljoin(manifest_url, str(raw.get("url") or "")),
        sha256=sha,
        size=max(0, int(raw.get("size") or 0)),
        unpacked_size=max(0, int(raw.get("unpackedSize") or 0)),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(pack: Pack, cache: Path, progress: Progress | None, base: int, span: int, total_all: int) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / f"{pack.pack_id}-{pack.sha256[:12]}.zip"
    if final.is_file() and _sha256(final) == pack.sha256:
        return final
    final.unlink(missing_ok=True)
    part = final.with_suffix(".zip.part")
    current = part.stat().st_size if part.is_file() else 0
    if current and pack.size and current >= pack.size:
        if current == pack.size and _sha256(part) == pack.sha256:
            os.replace(part, final)
            return final
        part.unlink(missing_ok=True)
        current = 0
    headers = {"User-Agent": "ZM-AI-TOOL-runtime/1"}
    if current:
        headers["Range"] = f"bytes={current}-"
    try:
        request = urllib.request.Request(pack.url, headers=headers)
        with urllib.request.urlopen(request, timeout=300) as response:
            if current and response.status != 206:
                current = 0
            mode = "ab" if current else "wb"
            expected = pack.size or int(response.headers.get("Content-Length") or 0) + current
            with part.open(mode) as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    current += len(chunk)
                    ratio = current / expected if expected else 0
                    _emit(
                        progress, base + round(span * min(1, ratio)),
                        f"Đang tải {pack.pack_id} / Downloading {pack.pack_id}",
                        stage="download", runtimePack=pack.pack_id,
                        downloadedBytes=current, totalBytes=expected or total_all,
                    )
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and current:
            part.unlink(missing_ok=True)
            return _download(pack, cache, progress, base, span, total_all)
        raise RuntimePackError("DOWNLOAD_FAILED", f"Tải {pack.pack_id} thất bại / Download failed: {exc}", retryable=True) from exc
    except Exception as exc:
        raise RuntimePackError("DOWNLOAD_FAILED", f"Tải {pack.pack_id} thất bại / Download failed: {exc}", retryable=True) from exc
    if _sha256(part) != pack.sha256:
        part.unlink(missing_ok=True)
        raise RuntimePackError("CHECKSUM_MISMATCH", f"SHA-256 không khớp: {pack.pack_id}", retryable=True)
    os.replace(part, final)
    return final


def _safe_extract(archive: Path, target: Path) -> None:
    root = target.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                destination = (target / info.filename).resolve()
                if destination != root and root not in destination.parents:
                    raise RuntimePackError("EXTRACT_FAILED", f"Đường dẫn ZIP không an toàn: {info.filename}", retryable=False)
            bundle.extractall(target)
    except RuntimePackError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimePackError("EXTRACT_FAILED", f"Không giải nén được runtime / Could not extract runtime: {exc}", retryable=True) from exc


def _python_in(root: Path) -> Path:
    return root / "Scripts/python.exe"


def _probe(root: Path, accelerator: str, *, require_demucs: bool = False) -> str:
    python = _python_in(root)
    if not python.is_file():
        raise RuntimePackError("RUNTIME_PROBE_FAILED", "Runtime pack thiếu Scripts/python.exe", retryable=False)
    modules = ["faster_whisper", "rapidocr_onnxruntime", "PIL", "cv2", "transformers", "vieneu", "soundfile", "sherpa_onnx", "cffi"]
    if require_demucs:
        modules.append("demucs")
    script = (
        "import importlib,json,sys\n"
        f"mods={modules!r}\n"
        "out={}\n"
        "for n in mods:\n"
        " try: importlib.import_module(n); out[n]='ok'\n"
        " except Exception as e: out[n]=type(e).__name__+': '+str(e)\n"
        f"acc={accelerator!r}\n"
        "if acc.startswith('gpu-nvidia'):\n"
        " import torch,onnxruntime as ort\n"
        " ready=torch.cuda.is_available() and 'CUDAExecutionProvider' in ort.get_available_providers()\n"
        " if ready:\n"
        "  value=(torch.ones(1,device='cuda')+1).cpu().item(); torch.cuda.synchronize(); ready=value==2\n"
        " out['gpu']='cuda' if ready else 'unavailable'\n"
        "elif acc=='gpu-directml':\n"
        " import onnxruntime as ort\n"
        " out['gpu']='directml' if 'DmlExecutionProvider' in ort.get_available_providers() else 'unavailable'\n"
        "bad={k:v for k,v in out.items() if v not in ('ok','cuda','directml')}\n"
        "print(json.dumps(out)); sys.exit(1 if bad else 0)\n"
    )
    try:
        proc = subprocess.run([str(python), "-I", "-c", script], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimePackError("RUNTIME_PROBE_FAILED", f"Không chạy được Python runtime: {exc}", retryable=True) from exc
    diagnostics = (proc.stdout + "\n" + proc.stderr).strip()[-8000:]
    if proc.returncode:
        code = "DRIVER_TOO_OLD" if accelerator.startswith("gpu-") and "unavailable" in diagnostics else "RUNTIME_PROBE_FAILED"
        raise RuntimePackError(code, "Runtime mới không vượt qua kiểm tra / Runtime probe failed", retryable=True, diagnostics=diagnostics)
    return diagnostics


def install_runtime_packs(
    progress: Progress | None = None,
    *,
    features: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not _is_windows_desktop():
        raise RuntimePackError("HARDWARE_UNSUPPORTED", "Runtime pack chỉ dành cho Windows desktop", retryable=False)
    home = runtime_home()
    runtime_root = home / "runtime"
    cache = runtime_root / "downloads"
    runtime_root.mkdir(parents=True, exist_ok=True)
    _emit(progress, 2, "Đang nhận diện phần cứng / Detecting hardware", stage="detect_hardware")
    accelerator = _hardware_pack()
    manifest_url = _manifest_url()
    _emit(progress, 5, "Đang tải runtime manifest / Fetching runtime manifest", stage="fetch_manifest", runtimePack=accelerator)
    manifest, final_manifest_url = _load_manifest(manifest_url)
    app_version = (os.environ.get("ZM_AI_TOOL_VERSION") or "").strip().removeprefix("v")
    compatible = str(manifest.get("appVersion") or "").strip().removeprefix("v")
    if app_version and compatible and compatible != app_version:
        raise RuntimePackError(
            "RUNTIME_PROBE_FAILED",
            f"Runtime dành cho APP {compatible}, bản hiện tại là {app_version}",
            retryable=False,
        )
    version = str(manifest.get("runtimeVersion") or "").strip()
    if not version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in version):
        raise RuntimePackError("RUNTIME_PROBE_FAILED", "runtimeVersion không hợp lệ", retryable=False)
    pack_ids = ["core-ai-win-x64"] + ([] if accelerator == "cpu" else [accelerator])
    if "demucs" in features:
        if not accelerator.startswith("gpu-nvidia"):
            pack_ids.append("gpu-torch-cpu")
        pack_ids.append("feature-demucs")
    packs = [_pack_from_manifest(manifest, item, final_manifest_url) for item in pack_ids]
    # Installation temporarily owns both downloaded archives and the extracted
    # tree. Existing runtimes are already reflected in free space.
    cached_bytes = 0
    for pack in packs:
        cached = cache / f"{pack.pack_id}-{pack.sha256[:12]}.zip"
        if cached.is_file() and _sha256(cached) == pack.sha256:
            cached_bytes += pack.size
    required = sum(pack.unpacked_size + pack.size for pack in packs) - cached_bytes
    free = shutil.disk_usage(runtime_root).free
    if required and free < required + 512 * 1024 * 1024:
        raise RuntimePackError("DISK_FULL", f"Không đủ dung lượng: cần {required} bytes, còn {free} bytes", retryable=False)
    total = sum(pack.size for pack in packs)
    archives: list[Path] = []
    download_span = 48 / max(1, len(packs))
    for index, pack in enumerate(packs):
        base = 10 + round(index * download_span)
        archives.append(_download(pack, cache, progress, base, max(1, round(download_span - 1)), total))
        _emit(progress, min(58, base + round(download_span - 1)), f"Đã xác minh {pack.pack_id}", stage="verify_checksum", runtimePack=pack.pack_id, requiredDiskBytes=required)
    identity = hashlib.sha256("".join(pack.sha256 for pack in packs).encode()).hexdigest()[:12]
    final_dir = runtime_root / f"{version}-{accelerator}-{identity}"
    staging = runtime_root / f".{version}-{accelerator}.{os.getpid()}.tmp"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        if final_dir.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            candidate = final_dir
        else:
            for archive in archives:
                _emit(progress, 62, "Đang giải nén runtime / Extracting runtime", stage="extract", runtimePack=accelerator)
                _safe_extract(archive, staging)
            candidate = staging
        _emit(progress, 84, "Đang kiểm tra runtime / Probing runtime", stage="probe", runtimePack=accelerator)
        diagnostics = _probe(candidate, accelerator, require_demucs="demucs" in features)
        if candidate == staging:
            os.replace(staging, final_dir)
        pointer = runtime_root / "current.json"
        previous = runtime_root / "previous.json"
        payload = {"schemaVersion": 1, "runtimeVersion": version, "runtimePack": accelerator, "path": final_dir.name, "activatedAt": time.time()}
        pointer_tmp = pointer.with_suffix(".json.tmp")
        try:
            with _POINTER_LOCK:
                if pointer.is_file():
                    shutil.copy2(pointer, previous)
                pointer_tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(pointer_tmp, pointer)
        except OSError as exc:
            pointer_tmp.unlink(missing_ok=True)
            raise RuntimePackError(
                "ACTIVATION_FAILED",
                f"Không kích hoạt được runtime / Could not activate runtime: {exc}",
                retryable=True,
            ) from exc
        _emit(progress, 100, "Đã kích hoạt AI runtime / AI runtime activated", stage="activate", runtimePack=accelerator)
        return {"ok": True, "message": "Đã cài AI runtime", "detail": f"{version} · {accelerator}", "runtimePack": accelerator, "diagnostics": diagnostics, "needsRestart": True}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install_demucs_pack(progress: Progress | None = None) -> dict[str, Any]:
    return install_runtime_packs(progress, features=("demucs",))


def rollback_runtime() -> dict[str, Any]:
    root = runtime_home() / "runtime"
    current, previous = root / "current.json", root / "previous.json"
    if not previous.is_file():
        raise RuntimePackError("ACTIVATION_FAILED", "Không có runtime để rollback", retryable=False)
    try:
        payload = json.loads(previous.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimePackError("ACTIVATION_FAILED", "Runtime rollback metadata không hợp lệ", retryable=False, diagnostics=str(exc)) from exc
    target = (root / str(payload.get("path") or "")).resolve()
    if root.resolve() not in target.parents or not _python_in(target).is_file():
        raise RuntimePackError("ACTIVATION_FAILED", "Runtime rollback không hợp lệ", retryable=False)
    _probe(target, str(payload.get("runtimePack") or "cpu"), require_demucs=False)
    tmp = current.with_suffix(".json.tmp")
    with _POINTER_LOCK:
        old = current.read_bytes() if current.is_file() else b""
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, current)
        if old:
            previous_tmp = previous.with_suffix(".json.tmp")
            previous_tmp.write_bytes(old)
            os.replace(previous_tmp, previous)
    return {"ok": True, "message": "Đã rollback AI runtime", "needsRestart": True}
