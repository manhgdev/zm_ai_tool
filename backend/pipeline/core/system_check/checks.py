"""Danh sách dependency + ready/missing cho first-run UI (system_checks)."""
from __future__ import annotations

import platform
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .probe import (
    _AI_RUNTIME_MODULES,
    _demucs_check,
    _demucs_venv_fast,
    _invalidate_probe_caches,
    _mod_ok,
    _mod_ok_fast,
    _nvidia_present,
    _ocr_cuda_check_cached,
    _ocr_directml_check,
    _ocr_venv_fast,
    _pkg_distributions,
    _run_ver,
    _runtime_modules_batch_ok,
    _runtime_mod_ok,
    _runtime_venv_fast,
    _required_ai_runtime_modules,
    _torch_cuda_ready_cached,
    _which,
)

_CHECKS_CACHE: tuple[float, bool, dict[str, Any]] | None = None
_CHECKS_TTL = 90.0  # 90s — checks không đổi thường xuyên
_checks_lock = threading.Lock()  # tránh thundering herd khi cache miss đồng thời


def _invalidate_checks_cache() -> None:
    global _CHECKS_CACHE
    _CHECKS_CACHE = None
    _invalidate_probe_caches()


def _ai_runtime_detail(*, torch_cuda: bool | None = None) -> str:
    base = "Whisper · OCR · Tách người nói · zmAI · VieNeu Local"
    if _nvidia_present():
        cuda = torch_cuda if torch_cuda is not None else _torch_cuda_ready_cached()
        if cuda:
            if getattr(sys, "frozen", False):
                return f"{base} · VieNeu CUDA (runtime)"
            try:
                import torch

                return f"{base} · VieNeu CUDA · {torch.cuda.get_device_name(0)}"
            except Exception:
                return f"{base} · VieNeu CUDA"
        return f"{base} · VieNeu ONNX/CPU (cần PyTorch CUDA)"
    return base


def _item(
    *,
    id: str,
    name: str,
    ok: bool,
    required: bool,
    detail: str,
    hint: str,
    install: str = "",
    installLabel: str = "",
) -> dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "ok": ok,
        "required": required,
        "detail": detail,
        "hint": hint,
        "install": install,
        "installLabel": installLabel,
    }


def _plan_item(plan: dict[str, Any], item_id: str) -> dict[str, Any]:
    items = plan.get("items") if isinstance(plan.get("items"), dict) else {}
    raw = items.get(item_id) if isinstance(items, dict) else None
    return raw if isinstance(raw, dict) else {}


def _install_from_plan(plan: dict[str, Any], item_id: str) -> tuple[str, str, str]:
    """Trả (install_value, install_label, hint) theo thiết bị."""
    p = _plan_item(plan, item_id)
    kind = str(p.get("kind") or "")
    value = str(p.get("value") or "")
    label = str(p.get("label") or "")
    hint = str(p.get("hint") or "")
    if kind == "none" or not value:
        return "", label, hint
    if kind == "action" and not p.get("relevant", True):
        return "", label, hint
    return value, label, hint


def _ollama_candidates() -> tuple[Path, ...]:
    """Known install locations when a GUI-launched APP has a minimal PATH."""
    if sys.platform == "win32":
        return (
            Path.home() / "AppData/Local/Programs/Ollama/ollama.exe",
            Path.home() / "AppData/Local/Ollama/ollama.exe",
            Path("C:/Program Files/Ollama/ollama.exe"),
        )
    if sys.platform == "darwin":
        return (
            Path("/Applications/Ollama.app/Contents/Resources/ollama"),
            Path.home() / "Applications/Ollama.app/Contents/Resources/ollama",
            Path("/opt/homebrew/bin/ollama"),
            Path("/usr/local/bin/ollama"),
        )
    return (
        Path("/usr/local/bin/ollama"),
        Path("/usr/bin/ollama"),
        Path("/snap/bin/ollama"),
    )


def _ollama_executable() -> str | None:
    """Find Ollama even when Finder/Explorer did not inherit the shell PATH."""
    found = _which("ollama")
    if found:
        return found
    for path in _ollama_candidates():
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _node_executable() -> str | None:
    """Tìm Node do NVM cài dù process hiện tại chưa nhận PATH mới."""
    found = _which("node")
    if found:
        return found
    if sys.platform == "win32":
        candidates = [
            os.environ.get("NVM_SYMLINK"),
            str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"),
        ]
        for directory in filter(None, candidates):
            path = Path(directory) / "node.exe"
            if path.is_file():
                return str(path)
        return None
    nvm_dir = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    nodes = list(nvm_dir.glob("versions/node/*/bin/node"))
    return str(max(nodes, key=lambda path: path.stat().st_mtime)) if nodes else None


def system_checks(*, refresh: bool = False, fast: bool = True) -> dict[str, Any]:
    """Danh sách dependency + ready/missing cho first-run UI."""
    global _CHECKS_CACHE
    if refresh:
        _invalidate_checks_cache()
    else:
        with _checks_lock:
            if (
                _CHECKS_CACHE
                and _CHECKS_CACHE[1] == fast
                and time.monotonic() - _CHECKS_CACHE[0] < _CHECKS_TTL
            ):
                return _CHECKS_CACHE[2]
    # ponytail: _checks_lock giữ trong uncached gây serialize requests → chỉ guard write.
    # Thundering herd tệ nhất = N requests chạy uncached cùng lúc, đều write cache→ok.
    result = _system_checks_uncached(fast=fast)
    with _checks_lock:
        _CHECKS_CACHE = (time.monotonic(), fast, result)
    return result


def _system_checks_uncached(*, fast: bool = True) -> dict[str, Any]:
    """fast=True: chỉ PATH/venv/dist-info (~vài giây). fast=False: import probe nặng."""
    from ..media import detect_device, _ff_bin

    items: list[dict[str, Any]] = []
    system = platform.system()
    machine = platform.machine()

    # ponytail: detect_device (nvidia-smi) + _run_ver subprocesses chạy song song để tránh
    # tổng ~15-20s tuần tự → timeout 15s của frontend. detect_device đã @lru_cache nên
    # submit thừa cũng nhanh từ lần 2 trở đi.
    ff_cmd = _ff_bin("ffmpeg")
    fp_cmd = _ff_bin("ffprobe")
    ff = ff_cmd if Path(ff_cmd).is_file() else None
    fp = fp_cmd if Path(fp_cmd).is_file() else None
    ol = _ollama_executable()
    node = _node_executable()
    with ThreadPoolExecutor(max_workers=6) as _pool:
        _fut_device = _pool.submit(detect_device)
        _fut_ff = _pool.submit(_run_ver, [ff, "-version"]) if ff else None
        _fut_fp = _pool.submit(_run_ver, [fp, "-version"]) if fp else None
        _fut_ol = _pool.submit(_run_ver, [ol, "--version"]) if ol else None
        _fut_nd = _pool.submit(_run_ver, [node, "-v"]) if node else None
        device = _fut_device.result()
        ff_ver = _fut_ff.result() if _fut_ff else "không có trên PATH"
        fp_ver = _fut_fp.result() if _fut_fp else "không có trên PATH"
        ol_ver = _fut_ol.result() if _fut_ol else "chưa cài"
        node_ver = _fut_nd.result() if _fut_nd else "không có (chỉ cần khi dev UI)"

    plan = device.get("install") or {}

    # ── Thiết bị (luôn hiện đầu) — quyết định path cài ──
    gpu_line = device.get("gpuName") or "không có GPU tăng tốc"
    if device.get("vramMb"):
        gpu_line = f"{gpu_line} · {device['vramMb']} MB"
    if device.get("driver"):
        gpu_line = f"{gpu_line} · driver {device['driver']}"
    items.append(
        _item(
            id="device",
            name=f"Thiết bị · {device.get('osLabel')}",
            ok=True,
            required=True,
            detail=(
                f"{device.get('osLabel')} · {device.get('arch')} · "
                f"GPU: {gpu_line} · accel={device.get('accel')}"
            ),
            hint=str(plan.get("hint") or ""),
        )
    )

    # ffmpeg / ffprobe
    ff_inst, ff_lab, ff_hint = _install_from_plan(plan, "ffmpeg")
    items.append(
        _item(
            id="ffmpeg",
            name="ffmpeg",
            ok=bool(ff) and not str(ff_ver).startswith("error"),
            required=True,
            detail=ff_ver,
            hint=ff_hint or "Bắt buộc để cắt audio, cover/burn, mux xuất video.",
            install=ff_inst,
            installLabel=ff_lab,
        )
    )
    fp_inst, fp_lab, fp_hint = _install_from_plan(plan, "ffprobe")
    items.append(
        _item(
            id="ffprobe",
            name="ffprobe",
            ok=bool(fp) and not str(fp_ver).startswith("error"),
            required=True,
            detail=fp_ver,
            hint=fp_hint or "Thường đi kèm ffmpeg (cùng package).",
            install=fp_inst,
            installLabel=fp_lab,
        )
    )

    dist = None if fast else _pkg_distributions()
    nvidia = device.get("gpuKind") == "nvidia"
    directml = device.get("accel") == "directml"
    if fast:
        demucs_ok, demucs_detail = _demucs_venv_fast()
        cuda_ok, cuda_detail = (
            _ocr_venv_fast("directml" if directml else "cuda")
            if (nvidia or directml) else (True, "")
        )
        torch_cuda_ok = True
        if getattr(sys, "frozen", False):
            runtime_ok, runtime_detail = _runtime_venv_fast()
            runtime_missing = [] if runtime_ok else list(_required_ai_runtime_modules())
        else:
            runtime_missing = [
                mid for mid in _required_ai_runtime_modules() if not _mod_ok_fast(mid)[0]
            ]
            runtime_detail = (
                "đã cài"
                if not runtime_missing
                else f"thiếu: {', '.join(runtime_missing)}"
            )
        runtime_torch_cuda = False
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_demucs = pool.submit(_demucs_check)
            fut_ocr = pool.submit(_ocr_cuda_check_cached) if nvidia else None
            fut_dml = pool.submit(_ocr_directml_check) if directml else None
            fut_cuda = pool.submit(_torch_cuda_ready_cached) if _nvidia_present() else None
            demucs_ok, demucs_detail = fut_demucs.result()
            cuda_ok, cuda_detail = (
                fut_ocr.result() if fut_ocr else
                fut_dml.result() if fut_dml else (True, "")
            )
            torch_cuda_ok = fut_cuda.result() if fut_cuda else True

        if getattr(sys, "frozen", False):
            required_runtime = _required_ai_runtime_modules()
            runtime_status = _runtime_modules_batch_ok(list(required_runtime))
            runtime_missing = [mid for mid in required_runtime if not runtime_status.get(mid, (False, ""))[0]]
        else:
            runtime_missing = [mid for mid in _required_ai_runtime_modules() if not _mod_ok(mid, dist_map=dist)[0]]
        runtime_torch_cuda = _nvidia_present() and not torch_cuda_ok
        runtime_detail = (
            _ai_runtime_detail(torch_cuda=torch_cuda_ok)
            if not runtime_missing
            else f"thiếu: {', '.join(runtime_missing)}"
        )
    # === 3 nhóm riêng thay vì 1 item gộp ===

    # Nhóm 1 — Whisper ASR
    _whisper_mods = ("faster_whisper",)
    _whisper_missing = [m for m in _whisper_mods if m in runtime_missing]
    items.append(
        _item(
            id="ai_runtime",   # cùng install id → bấm Cài chạy cả 3 nhóm
            name="Whisper (ASR)",
            ok=not _whisper_missing,
            required=True,
            detail="đã cài" if not _whisper_missing else f"thiếu: {', '.join(_whisper_missing)}",
            hint="Bộ nhận dạng giọng nói Faster-Whisper · soundfile · soxr.",
            install="ai_runtime",
            installLabel="Cài gói AI",
        )
    )


    # Nhóm 1b — Sherpa-ONNX + hai model offline. Hiển thị riêng để người dùng
    # biết rõ vì sao công tắc «Tách người nói» có/không sẵn sàng.
    from ..config import DATA
    from pipeline.asr.speaker import diarization_provider_for_device

    diarization_dir = Path(DATA) / "models" / "pyannote"
    segmentation_model = diarization_dir / "model.int8.onnx"
    embedding_model = diarization_dir / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
    # find_spec() vẫn trả true khi native dylib của Sherpa bị thiếu. Riêng mục
    # này phải import thật để không hiện «Đã cài» giả.
    diarization_package_ok, diarization_package_detail = (
        _runtime_mod_ok("sherpa_onnx") if getattr(sys, "frozen", False)
        else _mod_ok("sherpa_onnx", dist_map=dist)
    )
    diarization_models_ok = segmentation_model.is_file() and embedding_model.is_file()
    diarization_ok = diarization_package_ok and diarization_models_ok
    diarization_provider = diarization_provider_for_device(device)
    provider_label = {
        "cuda": "NVIDIA CUDA",
        "coreml": "Apple CoreML/Metal",
        "cpu": "CPU đa luồng",
    }.get(diarization_provider, diarization_provider)
    diarization_missing: list[str] = []
    if not diarization_package_ok:
        diarization_missing.append("sherpa-onnx runtime")
    if not diarization_models_ok:
        diarization_missing.append("2 model speaker")
    items.append(
        _item(
            id="ai_runtime_diarization",
            name="Sherpa-ONNX (Tách người nói)",
            ok=diarization_ok,
            required=False,
            detail=(
                f"đã cài · tự động: {provider_label}" if diarization_ok
                else f"thiếu: {', '.join(diarization_missing)} · {diarization_package_detail[:160]}"
            ),
            hint="Dùng kết quả nhận diện phần cứng chung của ZM AI TOOL; nếu backend tăng tốc không tương thích sẽ fallback CPU.",
            install="",
            installLabel="Tải khi bật Tách người nói",
        )
    )

    # Nhóm 2 — OCR
    _ocr_mods = ("rapidocr_onnxruntime", "PIL", "cv2")
    _ocr_missing = [m for m in _ocr_mods if m in runtime_missing]
    items.append(
        _item(
            id="ai_runtime_ocr",
            name="OCR (nhận dạng chữ)",
            ok=not _ocr_missing,
            required=True,
            detail="đã cài" if not _ocr_missing else f"thiếu: {', '.join(_ocr_missing)}",
            hint="RapidOCR · Pillow · OpenCV — đọc phụ đề từ video.",
            install="ai_runtime",
            installLabel="Cài gói AI",
        )
    )

    # Nhóm 3 — zmAI + VieNeu
    _vieneu_mods = (
        ("torch", "torchaudio", "transformers", "vieneu", "soundfile")
        if nvidia else ("transformers", "vieneu", "soundfile")
    )
    _vieneu_missing = [m for m in _vieneu_mods if m in runtime_missing]
    _vieneu_torch_bad = runtime_torch_cuda
    items.append(
        _item(
            id="ai_runtime_vieneu",
            name="zmAI + VieNeu Local",
            ok=not _vieneu_missing and not _vieneu_torch_bad,
            required=True,
            detail=(
                f"đã cài · VieNeu ONNX/CPU (cần PyTorch CUDA)" if (_vieneu_torch_bad and not _vieneu_missing)
                else "đã cài" if not _vieneu_missing
                else f"thiếu: {', '.join(_vieneu_missing)}"
            ),
            hint=(
                "Transformers · VieNeu TTS · huggingface-hub. "
                "Có NVIDIA: dùng GPU khi PyTorch CUDA đã cài."
            ),
            install="ai_runtime",
            installLabel="Cài gói AI",
        )
    )


    for mid, title, req in (("httpx", "httpx", True),):
        ok, detail = _mod_ok_fast(mid) if fast else _mod_ok(mid, dist_map=dist)
        inst, lab, hint = _install_from_plan(plan, mid)
        items.append(
            _item(
                id=mid,
                name=title,
                ok=ok,
                required=req,
                detail=detail,
                hint=hint,
                install=inst,
                installLabel=lab,
            )
        )

    # OCR GPU — CUDA trên NVIDIA, DirectML trên AMD/Intel Windows.
    ocr_inst, ocr_lab, ocr_hint = _install_from_plan(plan, "ocr_cuda")
    if nvidia or directml:
        items.append(
            _item(
                id="ocr_cuda",
                name="GPU tăng tốc OCR",
                ok=cuda_ok,
                required=False,
                detail=cuda_detail or f"DirectML · {device.get('gpuName')}",
                hint=ocr_hint,
                install=ocr_inst,
                installLabel=ocr_lab,
            )
        )

    # Demucs — đã probe ở trên
    dem_inst, dem_lab, dem_hint = _install_from_plan(plan, "demucs")
    items.append(
        _item(
            id="demucs",
            name="Demucs (xóa lời)",
            ok=demucs_ok,
            required=False,
            detail=demucs_detail,
            hint=dem_hint,
            install=dem_inst,
            installLabel=dem_lab,
        )
    )

    # TTS hệ thống — theo OS trong plan
    if system == "Darwin":
        say = _which("say")
        t_inst, t_lab, t_hint = _install_from_plan(plan, "say")
        items.append(
            _item(
                id="say",
                name=str(_plan_item(plan, "say").get("name") or "macOS say"),
                ok=bool(say),
                required=False,
                detail=say or "không có",
                hint=t_hint,
                install=t_inst,
                installLabel=t_lab,
            )
        )
    else:
        esp = _which("espeak-ng") or _which("espeak")
        t_inst, t_lab, t_hint = _install_from_plan(plan, "espeak")
        items.append(
            _item(
                id="espeak",
                name=str(_plan_item(plan, "espeak").get("name") or "espeak-ng"),
                ok=bool(esp),
                required=False,
                detail=esp or "không có",
                hint=t_hint,
                install=t_inst,
                installLabel=t_lab,
            )
        )

    # Ollama
    ol_ok = False
    ol_detail = ol_ver
    try:
        import httpx

        r = httpx.get("http://127.0.0.1:11434/api/tags", timeout=0.8)
        ol_ok = r.status_code < 500
        if ol_ok:
            n = len((r.json() or {}).get("models") or [])
            ol_detail = f"{ol_ver} · server OK · {n} model local · Cloud kiểm tra khi dịch"
    except Exception:
        if ol:
            ol_ok = True
            ol_detail = f"{ol_ver} · đã cài (server chưa chạy)"
    ol_inst, ol_lab, ol_hint = _install_from_plan(plan, "ollama")
    items.append(
        _item(
            id="ollama",
            name="Ollama",
            ok=ol_ok,
            required=False,
            detail=ol_detail,
            hint=ol_hint or "Dịch local (translator = ollama).",
            install=ol_inst,
            installLabel=ol_lab,
        )
    )

    # Node
    nd_inst, nd_lab, nd_hint = _install_from_plan(plan, "node")
    items.append(
        _item(
            id="node",
            name="Node.js (NVM)",
            ok=bool(node),
            required=False,
            detail=node_ver,
            hint=nd_hint,
            install=nd_inst,
            installLabel=nd_lab,
        )
    )

    # Data dir
    try:
        from ..config import DATA

        data_path = Path(DATA)
        data_path.mkdir(parents=True, exist_ok=True)
        data_ok = data_path.is_dir() and data_path.exists()
        data_detail = str(data_path.resolve())
    except Exception as e:
        data_ok = False
        data_detail = str(e)
    _, _, data_hint = _install_from_plan(plan, "data")
    items.append(
        _item(
            id="data",
            name="Thư mục data",
            ok=data_ok,
            required=True,
            detail=data_detail,
            hint=data_hint or "Lưu project, cache OCR, TTS, file xuất.",
        )
    )

    required_missing = [i for i in items if i["required"] and not i["ok"]]
    optional_missing = [i for i in items if not i["required"] and not i["ok"]]
    return {
        "ok": len(required_missing) == 0,
        "platform": f"{system} {machine}",
        "python": platform.python_version(),
        "device": device,
        "items": items,
        "requiredMissing": [i["id"] for i in required_missing],
        "optionalMissing": [i["id"] for i in optional_missing],
        "summary": (
            "Sẵn sàng"
            if not required_missing
            else f"Thiếu {len(required_missing)} thành phần bắt buộc"
        ),
        "fast": fast,
    }
