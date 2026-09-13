"""VieNeu-TTS local engine — verified against vieneu 3.2.3 (V3TurboVieNeuTTS).

Voice ids:
  vn:<voice_name>          built-in preset (e.g. vn:Phạm Tuyên)
  vn:clone:<id>            user clone

Lazy-load: model only constructed on first synthesize/clone.
Preset list can be read from package assets without loading ONNX graphs.
"""
from __future__ import annotations

import json
import hashlib
import importlib.metadata
import logging
import os
import subprocess
import sys
import threading
import warnings
from pathlib import Path
from typing import Any, Callable

from ..schemas import PREFIX_VIENEU
from .. import voice_store
from .. import zmtss_catalog
_lock = threading.Lock()
_client: Any = None
_client_err: str | None = None
_load_state = "cold"  # cold | loading | ready | error
_reference_lock = threading.Lock()
_reference_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
_clone_lock = threading.Lock()
_clone_cache: dict[str, tuple[int, int]] = {}


def _subprocess_env() -> dict[str, str]:
    """Use the bounded desktop environment for ffmpeg and GPU probes."""
    try:
        from pipeline.core.runtime_site import subprocess_environment

        return subprocess_environment()
    except Exception:
        return os.environ.copy()


def _normalize_clone_reference(source: Path, destination: Path) -> None:
    """Convert an uploaded reference to the exact WAV format VieNeu expects.

    Use a sibling temporary file so a failed/terminated FFmpeg process never
    leaves a partial clone registered as a valid voice.  Capturing stderr is
    essential on Windows: exit 0xFFFFFFFF alone does not identify whether the
    MP3 is invalid, a codec is missing, or FFmpeg itself was interrupted.
    """
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError("File mẫu giọng trống hoặc không tồn tại")
    temporary = destination.with_name(f".{destination.stem}.convert-{os.getpid()}.wav")
    temporary.unlink(missing_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-map", "0:a:0?", "-vn",
        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            check=False,
            env=_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Không tìm thấy FFmpeg để đọc file mẫu giọng") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg xử lý file mẫu quá lâu; hãy thử file ngắn hơn 30 giây") from exc
    if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size < 1024:
        detail = (result.stderr or result.stdout or "không có thông tin từ FFmpeg").strip()
        detail = detail[-1200:]
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "Không chuyển được file mẫu thành WAV 48 kHz. "
            f"FFmpeg exit {result.returncode}: {detail}"
        )
    temporary.replace(destination)


def _sanitize_no_proxy(env: Any = os.environ) -> None:
    """Drop bare IPv6 loopback entries that current httpx parses as port ``:1``."""
    broken = {"::1", "::1/128", "[::1]", "[::1]/128"}
    for name in ("NO_PROXY", "no_proxy"):
        raw = env.get(name)
        if raw:
            env[name] = ",".join(part for part in raw.split(",") if part.strip() not in broken)


def _prepare_cuda_weight_load(backend: str, device: str) -> bool:
    """Initialize CUDA; stage safetensors through CPU if pinned memory is unavailable.

    Some Windows torch builds expose CUDA inference but do not register a pinned
    host allocator early enough for safetensors' direct-to-CUDA loader.
    """
    if backend != "pytorch" or not str(device).startswith("cuda"):
        return False
    import torch

    torch.cuda.init()
    try:
        torch.empty(1, dtype=torch.uint8, pin_memory=True)
        return False
    except RuntimeError as exc:
        if "pin_memory allocator" not in str(exc) and "pin memory" not in str(exc):
            raise
    import safetensors.torch as safe_torch

    if getattr(safe_torch.load_model, "_videoclone_cpu_stage", False):
        return True
    original = safe_torch.load_model

    def load_model_cpu_stage(model, filename, strict=True, device="cpu"):
        target = str(device)
        result = original(model, filename, strict=strict, device="cpu")
        if target.startswith("cuda"):
            model.to(target)
        return result

    load_model_cpu_stage._videoclone_cpu_stage = True  # type: ignore[attr-defined]
    safe_torch.load_model = load_model_cpu_stage
    return True


def _torch_cuda_ready() -> bool:
    try:
        from pipeline.core.accel import preferred_torch_device

        return preferred_torch_device() == "cuda"
    except Exception:
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False


def _nvidia_present() -> bool:
    """GPU vật lý có (không cần torch) — để báo thiếu PyTorch CUDA."""
    try:
        import shutil
        import subprocess

        if not shutil.which("nvidia-smi"):
            return False
        r = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=8,
            env=_subprocess_env(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0,
        )
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False


def _resolve_backend() -> tuple[str, str]:
    """(backend, device) — CUDA/MPS → pytorch; không GPU → ONNX/CPU.

    CapCut / ElevenLabs cloud — chỉ VieNeu local dùng GPU máy.
    """
    from pipeline.core.accel import preferred_vieneu_backend

    return preferred_vieneu_backend()


def available() -> bool:
    if os.environ.get("VIENEU_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("vieneu") is not None
    except Exception:
        return False


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version("vieneu")
    except Exception:
        return ""


def _assets_voices_path() -> Path | None:
    try:
        import vieneu

        root = Path(vieneu.__file__).resolve().parent
        p = root / "assets" / "voices_v3_turbo.json"
        return p if p.is_file() else None
    except Exception:
        return None


def list_preset_from_assets() -> list[dict[str, str]]:
    """Preset metadata from SDK assets without loading the neural model."""
    path = _assets_voices_path()
    if not path:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict[str, str]] = []
    for name, v in (data.get("presets") or {}).items():
        meta = v or {}
        description = str(meta.get("description") or "")
        # ponytail: SDK only embeds accent in its human description; keep this
        # exact-token parser until the SDK exposes a dedicated accent field.
        accent = next(
            (part for part in description.split(" · ") if part in {"Bắc", "Trung", "Nam"}),
            "",
        )
        out.append(
            {
                "id": str(name),
                "name": str(name),
                "description": description,
                "gender": str(meta.get("gender") or ""),
                "style": str(meta.get("style") or ""),
                "accent": accent,
                "language": "vi",
            }
        )
    return out


def _hf_token() -> str:
    return (
        (os.environ.get("HF_TOKEN") or "").strip()
        or (os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip()
        or (os.environ.get("HUGGINGFACE_HUB_TOKEN") or "").strip()
    )


class _DropHfUnauthFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "unauthenticated requests" in msg:
            return False
        if "HF_TOKEN" in msg and "rate limit" in msg.lower():
            return False
        return True


def _apply_hf_token() -> None:
    """Wire HF_TOKEN for Hub; silence unauth rate-limit nag when no token."""
    token = _hf_token()
    if token:
        os.environ.setdefault("HF_TOKEN", token)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", token)
        try:
            from huggingface_hub import login

            login(token=token, add_to_git_credential=False)
        except Exception:
            pass
        return
    # No token: anonymous downloads still work; hide HF's rate-limit marketing warning.
    warnings.filterwarnings("ignore", message=r".*unauthenticated requests to the HF Hub.*")
    warnings.filterwarnings("ignore", message=r".*set a HF_TOKEN.*")
    try:
        for name in (
            "huggingface_hub",
            "huggingface_hub.utils",
            "huggingface_hub.utils._http",
            "transformers",
            "transformers.utils",
        ):
            log = logging.getLogger(name)
            if not any(isinstance(f, _DropHfUnauthFilter) for f in log.filters):
                log.addFilter(_DropHfUnauthFilter())
    except Exception:
        pass


def _register_vieneu_transformers() -> None:
    """Register custom HF model_type so from_pretrained does not warn.

    Checkpoint config.json has model_type=vieneu_v3 but transformers does not
    know that class until we register it (SDK forgets this step).
    """
    try:
        from transformers import AutoConfig, AutoModel
        from vieneu._v3_turbo_engine.configuration_v3_turbo import VieNeuV3TurboConfig
        from vieneu._v3_turbo_engine.modeling_v3_turbo import VieNeuV3TurboForTTS

        AutoConfig.register("vieneu_v3", VieNeuV3TurboConfig)
        # backbone is custom PreTrainedModel; AutoModel path used by some loaders
        try:
            AutoModel.register(VieNeuV3TurboConfig, VieNeuV3TurboForTTS)
        except Exception:
            pass
    except Exception:
        # onnx-only / missing torch path — warning may still appear, non-fatal
        pass


def get_client() -> Any:
    """Lazy singleton — only when synth/clone needs the model."""
    global _client, _client_err, _load_state
    if getattr(sys, "frozen", False):
        from . import vieneu_frozen
        # Frozen APP must keep torch/ONNX out of the UI/API process.  The
        # runtime worker performs the real import and reports a short error;
        # loading native extensions here can abort the whole desktop process.
        with _lock:
            if _load_state != "ready":
                _load_state = "loading"
                try:
                    ok, detail = vieneu_frozen.probe()
                    if not ok:
                        raise RuntimeError(detail)
                    _load_state = "ready"
                    _client_err = None
                except Exception as exc:
                    _load_state = "error"
                    _client_err = str(exc)
                    raise RuntimeError(f"Không khởi tạo được VieNeu: {exc}") from exc
        return None  # synthesize() uses subprocess on frozen builds
    if not available():
        raise RuntimeError(
            "Chưa cài VieNeu-TTS. Trong backend/.venv chạy: pip install vieneu onnxruntime soundfile soxr sea-g2p"
        )
    with _lock:
        if _client is not None:
            return _client
        if _client_err and _load_state == "error":
            # allow one retry after error
            pass
        _load_state = "loading"
        try:
            _sanitize_no_proxy()
            from pipeline.core.system_check import ensure_runtime_torch, ensure_runtime_transformers
            from pipeline.core.runtime_site import bootstrap_ai_runtime, install_runtime_meta_path

            install_runtime_meta_path()
            bootstrap_ai_runtime()
            ensure_runtime_torch()
            ensure_runtime_transformers()
            # Prefetch torch cuDNN before any other CUDA package binds the DLL name
            try:
                from ...core.cuda_dll import prefer_torch_cudnn

                prefer_torch_cudnn()
            except Exception:
                pass
            _apply_hf_token()
            _register_vieneu_transformers()
            from vieneu import Vieneu

            backend, device = _resolve_backend()
            # Wrong ctranslate2 cuDNN → copy torch's DLL in, stay on CUDA.
            if backend == "pytorch" and device == "cuda":
                try:
                    from ...core.cuda_dll import cudnn_healthy, prefer_torch_cudnn

                    prefer_torch_cudnn()
                    if not cudnn_healthy():
                        print(
                            "[vieneu] cuDNN thiếu cudnnGetLibConfig — "
                            "đã gắn DLL torch, giữ CUDA",
                            flush=True,
                        )
                except Exception:
                    pass
            _prepare_cuda_weight_load(backend, device)
            kwargs: dict[str, Any] = {
                "mode": "v3turbo",
                "backend": backend,
                "device": device,
            }
            precision = (os.environ.get("VIENEU_PRECISION") or "int8").strip().lower()
            if precision in ("int8", "fp32"):
                kwargs["precision"] = precision
            voice_store.ensure_vieneu_dirs()
            # Prefer our voices.json for cloned voices if SDK supports path
            voices_path = voice_store.VOICES_JSON
            _client = Vieneu(**kwargs)
            # Re-enroll clones from disk
            for item in voice_store.load_cloned():
                ref = item.get("ref")
                clone_id = item.get("id")
                if not ref or not clone_id:
                    continue
                path = voice_store.VIENEU_ROOT / str(ref)
                if not path.is_file():
                    continue
                try:
                    _client.add_voice(str(clone_id), str(path), denoise=False, save=False)
                    stat = path.stat()
                    _clone_cache[str(clone_id)] = (stat.st_mtime_ns, stat.st_size)
                except Exception:
                    pass
            try:
                if voices_path.is_file():
                    # SDK save_voices path — optional reload
                    pass
            except Exception:
                pass
            _client_err = None
            _load_state = "ready"
            return _client
        except Exception as e:
            _client = None
            _client_err = str(e)
            _load_state = "error"
            raise RuntimeError(f"Không khởi tạo được VieNeu: {e}") from e


def status() -> dict[str, Any]:
    installed = available()
    presets = list_preset_from_assets() if installed else []
    out: dict[str, Any] = {
        "id": "vieneu",
        "name": "VieNeu Local",
        "local": True,
        "installed": installed,
        "ready": installed and _load_state in ("ready", "cold"),
        "loaded": _load_state == "ready",
        "loadState": _load_state,
        "device": "—",
        "model": "VieNeu-TTS-v3-Turbo",
        "version": package_version(),
        "message": "",
        "presetCount": len(presets),
        "installHint": (
            "pip install vieneu onnxruntime soundfile soxr sea-g2p && "
            "pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124 && "
            "pip install transformers"
        ),
        "cloneRequiresPytorch": True,
    }
    if not installed:
        out["ready"] = False
        out["message"] = "Chưa cài — " + out["installHint"]
        return out
    if getattr(sys, "frozen", False):
        from . import vieneu_frozen

        ok, detail = vieneu_frozen.probe()
        backend, device = vieneu_frozen.resolve_backend()
        out["ready"] = ok
        out["loaded"] = ok
        out["loadState"] = "ready" if ok else "error"
        if ok:
            if backend == "pytorch" and device == "cuda":
                out["device"] = "CUDA (runtime)"
                out["message"] = "Sẵn sàng — TTS PyTorch/CUDA qua runtime venv"
            elif backend == "pytorch" and device == "mps":
                out["device"] = "Apple GPU (MPS runtime)"
                out["message"] = "TTS PyTorch/MPS runtime"
            else:
                out["device"] = "ONNX/CPU (runtime)"
                out["message"] = "Sẵn sàng — TTS ONNX/CPU qua runtime venv"
        else:
            out["message"] = f"Lỗi runtime: {detail[:180]}"
        return out
    if _load_state == "error" and _client_err:
        out["ready"] = False
        out["message"] = f"Lỗi load: {_client_err[:180]}"
        return out
    if _load_state == "ready" and _client is not None:
        be = str(getattr(_client, "backend", "onnx") or "onnx").lower()
        if be == "pytorch":
            try:
                from pipeline.core.accel import preferred_torch_device, accel_label

                d = preferred_torch_device()
                if d == "cuda":
                    try:
                        import torch

                        out["device"] = f"CUDA · {torch.cuda.get_device_name(0)}"
                    except Exception:
                        out["device"] = "CUDA"
                elif d == "mps":
                    out["device"] = "Apple GPU (MPS)"
                else:
                    out["device"] = "PyTorch/CPU"
            except Exception:
                out["device"] = "PyTorch"
        else:
            out["device"] = "ONNX/CPU"
        out["message"] = "Sẵn sàng (model đã nạp)"
    elif _load_state == "loading":
        out["message"] = "Đang nạp model…"
    else:
        try:
            from pipeline.core.accel import preferred_torch_device

            d = preferred_torch_device()
            if d == "cuda":
                out["device"] = "CUDA (lazy)"
                out["message"] = "Đã cài — lần tạo giọng đầu nạp PyTorch/CUDA"
            elif d == "mps":
                out["device"] = "Apple GPU (lazy)"
                out["message"] = "Đã cài — lần tạo giọng đầu nạp PyTorch/MPS"
            elif _nvidia_present():
                out["device"] = "CPU (thiếu torch CUDA)"
                out["message"] = (
                    "Có NVIDIA nhưng torch chưa CUDA — vào Thiết lập → Cài gói AI. "
                    "pip install torch torchaudio --index-url "
                    "https://download.pytorch.org/whl/cu124"
                )
            else:
                out["device"] = "ONNX/CPU (lazy)"
                out["message"] = "Không GPU — model nạp CPU khi tạo giọng lần đầu"
        except Exception:
            out["device"] = "ONNX/CPU"
    return out


def list_voices(lang: str | None = None) -> list[dict[str, Any]]:
    requested_language = voice_store.normalize_voice_language(lang)
    out: list[dict[str, Any]] = []
    for item in voice_store.load_reference_voices():
        voice_id = str(item["id"])
        ref_path = voice_store.reference_path(item)
        out.append(
            {
                **item,
                "id": voice_id,
                "name": str(item.get("name") or voice_id),
                "engine": "zmai",
                "type": "zmAI",
                "available": ref_path.is_file(),
                "tags": voice_store.normalize_voice_tags(item.get("tags")),
                "language": voice_store.normalize_voice_language(item.get("language")),
                "favorite": bool(item.get("favorite")),
                "previewUrl": f"/api/tts/voices/{voice_id}/preview" if ref_path.is_file() else None,
            }
        )
    # ZMTTS remains online until selected. Its demo is fetched and registered
    # as a local zmAI reference immediately before the first synthesis.
    for item in zmtss_catalog.voices():
        language = zmtss_catalog.language_code(item.get("language"))
        if requested_language and language != requested_language:
            continue
        remote_id = str(item["id"])
        voice_id = f"{zmtss_catalog.VOICE_ID_PREFIX}{remote_id}"
        local = voice_store.get_reference_voice(voice_id)
        path = voice_store.reference_path(local or {})
        out.append({
            "id": voice_id,
            "name": voice_store.clean_display_name(str(item.get("name") or remote_id), fallback=remote_id),
            "engine": "zmai", "type": "zmAI", "mode": "remote-reference",
            "available": path.is_file(), "language": language,
            "source": "zmtss", "previewUrl": zmtss_catalog.remote_url(item),
        })
    # The live client also contains user clones, which are appended separately below.
    presets = list_preset_from_assets()
    for preset in presets:
        voice_id = preset["id"]
        out.append(
            {
                **preset,
                "id": f"{PREFIX_VIENEU}{voice_id}",
                "name": f"VieNeu · {preset['name']}",
                "engine": "vieneu",
                "type": "preset",
            }
        )
    for item in voice_store.load_cloned():
        cid = str(item.get("id") or "")
        name = str(item.get("name") or cid)
        if not cid:
            continue
        out.append(
            {
                "id": f"{PREFIX_VIENEU}clone:{cid}",
                "name": voice_store.clean_display_name(name, fallback=cid),
                "engine": "clone",
                "type": "clone",
                "tags": voice_store.normalize_voice_tags(item.get("tags")),
                "language": voice_store.normalize_voice_language(item.get("language")),
                "favorite": bool(item.get("favorite")),
                "previewUrl": f"/api/tts/voices/{PREFIX_VIENEU}clone:{cid}/preview",
            }
        )
    return out


def preview_path(voice: str) -> Path | None:
    """Return an existing reference WAV for preview; presets have no source audio."""
    parsed = parse_voice(voice)
    if not parsed:
        return None
    kind, voice_id = parsed
    if kind == "reference":
        item = voice_store.get_reference_voice(voice_id)
        path = voice_store.reference_path(item or {})
    elif kind == "clone":
        item = next((x for x in voice_store.load_cloned() if x.get("id") == voice_id), None)
        if not item:
            return None
        path = voice_store.VIENEU_ROOT / str(item.get("ref") or "")
    else:
        return None
    return path if path.is_file() else None


def parse_voice(voice: str) -> tuple[str, str] | None:
    if not voice:
        return None
    if str(voice).startswith(zmtss_catalog.VOICE_ID_PREFIX):
        return "remote-reference", str(voice)
    direct = voice_store.get_reference_voice(str(voice))
    if direct:
        return "reference", str(direct["id"])
    if not str(voice).startswith(PREFIX_VIENEU):
        return None
    rest = str(voice)[len(PREFIX_VIENEU) :]
    if rest.startswith("clone:"):
        return "clone", rest[6:].strip()
    return "preset", rest.strip()


def _ensure_remote_reference(voice_id: str) -> None:
    """Materialize a selected ZMTTS demo as a local VieNeu reference once."""
    if voice_store.get_reference_voice(voice_id):
        return
    item = zmtss_catalog.get(voice_id)
    if not item:
        raise RuntimeError(f"Không tìm thấy giọng ZMTTS '{voice_id}' trên GitHub")
    filename = zmtss_catalog.local_filename(item)
    path = voice_store.REFERENCE_ROOT / filename
    zmtss_catalog.download_reference(item, path)
    entries = [entry for entry in voice_store._read_reference_raw() if entry.get("id") != voice_id]
    name = voice_store.clean_display_name(str(item.get("name") or item["id"]), fallback=str(item["id"]))
    entries.append({
        "id": voice_id, "name": name, "label": name, "type": "zmAI", "engine": "vieneu",
        "mode": "reference", "language": zmtss_catalog.language_code(item.get("language")),
        "ref_file": filename, "hidden": False, "favorite": False, "tags": [], "source": "ZMTTS",
    })
    voice_store.save_reference_voices(entries)


def reference_cache_token(voice: str) -> str:
    """Fingerprint reference content for encoded/audio-preview cache invalidation."""
    parsed = parse_voice(voice)
    if not parsed or parsed[0] != "reference":
        return ""
    item = voice_store.get_reference_voice(parsed[1])
    path = voice_store.reference_path(item or {})
    try:
        stat = path.stat()
        return f"{parsed[1]}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return f"{parsed[1]}:missing"


def _enable_torchaudio_soundfile_fallback() -> None:
    """Keep VieNeu reference WAV loading working with torchaudio builds needing TorchCodec."""
    import torchaudio

    if getattr(torchaudio.load, "_videoclone_soundfile_fallback", False):
        return
    native_load = torchaudio.load

    def load(uri: Any, *args: Any, **kwargs: Any):
        try:
            return native_load(uri, *args, **kwargs)
        except (ImportError, RuntimeError) as exc:
            if "torchcodec" not in str(exc).lower() or not isinstance(uri, (str, os.PathLike)):
                raise
            import soundfile as sf
            import torch

            frame_offset = int(kwargs.get("frame_offset", 0) or 0)
            num_frames = int(kwargs.get("num_frames", -1) or -1)
            channels_first = bool(kwargs.get("channels_first", True))
            samples, sample_rate = sf.read(
                str(uri),
                start=max(0, frame_offset),
                frames=num_frames if num_frames > 0 else -1,
                dtype="float32",
                always_2d=True,
            )
            waveform = torch.from_numpy(samples.T if channels_first else samples)
            return waveform, sample_rate

    load._videoclone_soundfile_fallback = True  # type: ignore[attr-defined]
    torchaudio.load = load


def _encoded_reference(client: Any, voice_id: str) -> dict[str, Any]:
    item = voice_store.get_reference_voice(voice_id)
    if not item:
        raise RuntimeError(f"Không tìm thấy cấu hình giọng zmAI '{voice_id}'.")
    path = voice_store.reference_path(item)
    if not path.is_file():
        raise RuntimeError(
            f"Thiếu file reference cho giọng {item.get('name') or voice_id}: {path}"
        )
    stat = path.stat()
    with _reference_lock:
        cached = _reference_cache.get(voice_id)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        try:
            vieneu_version = importlib.metadata.version("vieneu")
        except importlib.metadata.PackageNotFoundError:
            vieneu_version = "unknown"
        token = f"{voice_id}:{stat.st_mtime_ns}:{stat.st_size}:{vieneu_version}"
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        disk_cache = voice_store.CACHE_DIR / f"zmai-reference-{digest}.npz"
        try:
            import numpy as np

            with np.load(disk_cache, allow_pickle=False) as saved:
                encoded = {
                    "speaker_emb": saved["speaker_emb"],
                    "codes": saved["codes"] if bool(saved["has_codes"][0]) else None,
                    "style": "tu_nhien",
                }
            _reference_cache[voice_id] = (stat.st_mtime_ns, stat.st_size, encoded)
            return encoded
        except (OSError, KeyError, ValueError):
            disk_cache.unlink(missing_ok=True)
        try:
            from pipeline.core.system_check import ensure_torchaudio

            ensure_torchaudio()
            _enable_torchaudio_soundfile_fallback()
            speaker_emb, ref_codes = client.encode_reference(path, denoise=False)
        except Exception as e:
            msg = str(e)
            if "torchaudio" in msg.lower():
                msg = (
                    f"{msg} — VieNeu cần torchaudio để encode giọng zmAI; "
                    "thử Thiết lập → Cài gói AI"
                )
            raise RuntimeError(
                f"Không encode được reference của giọng {item.get('name') or voice_id} "
                f"({path}): {msg}"
            ) from e
        encoded = {
            "speaker_emb": speaker_emb,
            "codes": ref_codes,
            "style": "tu_nhien",
        }
        try:
            import numpy as np

            voice_store.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temp = disk_cache.with_name(f".{disk_cache.name}.{os.getpid()}.tmp")
            with temp.open("wb") as handle:
                np.savez(
                    handle,
                    speaker_emb=np.asarray(speaker_emb),
                    codes=np.asarray(ref_codes) if ref_codes is not None else np.empty(0, dtype=np.int64),
                    has_codes=np.asarray([ref_codes is not None], dtype=np.bool_),
                )
            temp.replace(disk_cache)
        except (OSError, TypeError, ValueError):
            temp.unlink(missing_ok=True) if "temp" in locals() else None
        _reference_cache[voice_id] = (stat.st_mtime_ns, stat.st_size, encoded)
        return encoded


def _register_clone(client: Any, clone_id: str, path: Path) -> None:
    stat = path.stat()
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _clone_lock:
        if _clone_cache.get(clone_id) == fingerprint:
            return
        client.add_voice(clone_id, str(path), denoise=False, save=False)
        _clone_cache[clone_id] = fingerprint


def synthesize(
    text: str,
    voice: str,
    out_wav: Path,
    *,
    style: str = "tu_nhien",
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    parsed = parse_voice(voice)
    if not parsed:
        raise ValueError(f"Không phải giọng VieNeu: {voice}")
    kind, name = parsed
    if kind == "remote-reference":
        _ensure_remote_reference(name)
        kind = "reference"
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    text = (text or ".").strip() or "."
    if style not in ("tu_nhien", "tin_tuc", "doc_truyen"):
        style = "tu_nhien"

    if getattr(sys, "frozen", False):
        from . import vieneu_frozen

        if cancel_check and cancel_check():
            raise RuntimeError("Job đã hủy")
        get_client()  # prepares PyTorch/MPS or CUDA before resolving the worker
        backend, device = vieneu_frozen.resolve_backend()
        clone_ref = None
        voice_arg = name
        if kind == "clone":
            entry = next((x for x in voice_store.load_cloned() if x.get("id") == name), None)
            ref = entry.get("ref") if entry else None
            ref_path = voice_store.VIENEU_ROOT / str(ref) if ref else None
            if not ref_path or not ref_path.is_file():
                raise RuntimeError(f"Thiếu file clone: {name}")
            clone_ref = str(ref_path)
        elif kind == "reference":
            item = voice_store.get_reference_voice(name)
            ref_path = voice_store.reference_path(item or {})
            if not item or not ref_path.is_file():
                raise RuntimeError(f"Thiếu file reference zmAI: {name}")
            # ponytail: subprocess add_voice giống clone — encode_reference in-process không chạy được trên frozen.
            clone_ref = str(ref_path)
        vieneu_frozen.synthesize(
            text=text,
            voice=voice_arg,
            out_wav=out_wav,
            style=style,
            backend=backend,
            device=device,
            clone_ref=clone_ref,
        )
        return

    client = get_client()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    text = (text or ".").strip() or "."
    if style not in ("tu_nhien", "tin_tuc", "doc_truyen"):
        style = "tu_nhien"

    infer_kwargs: dict[str, Any] = {"style": style}
    if kind == "reference":
        infer_kwargs["voice"] = _encoded_reference(client, name)
    elif kind == "clone":
        entry = next((x for x in voice_store.load_cloned() if x.get("id") == name), None)
        ref = entry.get("ref") if entry else None
        ref_path = voice_store.VIENEU_ROOT / str(ref) if ref else None
        if ref_path and ref_path.is_file():
            _register_clone(client, name, ref_path)
        infer_kwargs["voice"] = name
    else:
        infer_kwargs["voice"] = name

    if cancel_check:
        import numpy as np

        stream = client.infer_stream(text, **infer_kwargs)
        chunks: list[Any] = []
        try:
            for chunk in stream:
                if cancel_check():
                    raise RuntimeError("Job đã hủy")
                chunks.append(chunk)
        finally:
            if cancel_check() and hasattr(stream, "close"):
                stream.close()
        if cancel_check():
            raise RuntimeError("Job đã hủy")
        audio = np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)
    else:
        try:
            audio = client.infer(text, **infer_kwargs)
        except Exception:
            if kind != "clone" or not ref_path:
                raise
            audio = client.infer(text, ref_audio=str(ref_path), denoise=False, style=style)

    try:
        client.save(audio, str(out_wav))
    except Exception:
        import numpy as np

        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        _write_wav_pcm16(out_wav, arr, 48000)


def _write_wav_pcm16(path: Path, samples: Any, sr: int) -> None:
    import wave

    import numpy as np

    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def clone_voice(
    name: str,
    ref_path: Path,
    *,
    denoise: bool = True,
    transcript: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Register cloned voice. PyTorch path preferred for denoise; ONNX may fail."""
    _ = transcript  # reserved for future ref_text APIs
    voice_store.ensure_vieneu_dirs()
    display = name.strip() or "clone"
    existing = {str(x.get("id") or "") for x in voice_store.load_cloned()}
    safe = voice_store.make_clone_id(display, existing)
    dest = voice_store.CLONED_DIR / f"{safe}.wav"
    if ref_path.resolve() != dest.resolve():
        import shutil
        import subprocess

        if ref_path.suffix.lower() == ".wav":
            shutil.copy2(ref_path, dest)
        else:
            _normalize_clone_reference(ref_path, dest)
    # Frozen desktop uses a short-lived runtime subprocess for each synthesis.
    # It deliberately returns no in-process VieNeu client; the subprocess
    # receives this reference at synthesis time and calls add_voice itself.
    # Calling add_voice here used to dereference None and made cloning fail
    # even when the configured PyTorch GPU runtime was healthy.
    client = get_client()
    if client is not None:
        try:
            client.add_voice(safe, str(dest), denoise=bool(denoise), save=False)
        except Exception as e1:
            try:
                client.add_voice(safe, str(dest), denoise=False, save=False)
            except Exception as e2:
                backend, device = _resolve_backend()
                raise RuntimeError(
                    "Không thể đăng ký giọng clone với VieNeu "
                    f"({backend}/{device}). Chi tiết: {e2 or e1}"
                ) from e2
        # Không ghi SDK presets vào voices.json — sẽ xóa hết danh sách clone.
        try:
            client.save_voices(str(voice_store.SDK_VOICES_JSON))
        except Exception:
            pass
    clean_tags = voice_store.normalize_voice_tags(tags, strict=True)
    stat = dest.stat()
    _clone_cache[safe] = (stat.st_mtime_ns, stat.st_size)
    voice_store.add_cloned(safe, display, f"cloned/{safe}.wav", tags=clean_tags)
    return {
        "id": f"{PREFIX_VIENEU}clone:{safe}",
        "name": voice_store.clean_display_name(display, fallback=safe),
        "tags": clean_tags,
    }


def warm() -> str:
    try:
        if getattr(sys, "frozen", False):
            from . import vieneu_frozen

            ok, detail = vieneu_frozen.probe()
            return "ready" if ok else f"err:{detail}"
        get_client()
        return str(status().get("device") or "ready")
    except Exception as e:
        return f"err:{e}"


def reset_client() -> None:
    """Bỏ model đã nạp — dùng sau khi cài PyTorch CUDA để load lại GPU."""
    global _client, _client_err, _load_state
    with _lock:
        _client = None
        _client_err = None
        _load_state = "cold"
    with _reference_lock:
        _reference_cache.clear()
    with _clone_lock:
        _clone_cache.clear()
    # Release cached model tensors after a cancelled GPU job instead of leaving
    # their RAM/VRAM reserved until the application exits.
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
