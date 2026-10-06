"""Audio8-TTS-0.1B local ONNX adapter.

The standalone Audio8 runtime is kept outside the application dependency set;
set ``AUDIO8_RUNTIME_DIR`` to its ``onnx_runtime_0_1b_int8`` directory and
``AUDIO8_MODEL_DIR``/``AUDIO8_VOICES_DIR`` to its model and voice stores.
"""
from __future__ import annotations

import os
import subprocess
import urllib.request
from pathlib import Path
import tempfile
import zipfile


def _paths() -> tuple[Path, Path, Path]:
    from pipeline.core.config import DATA
    root = DATA / "audio8"
    runtime = Path(os.environ.get("AUDIO8_RUNTIME_DIR", str(root / "source/onnx_runtime_0_1b_int8")))
    model = Path(os.environ.get("AUDIO8_MODEL_DIR", str(root / "model")))
    voices = Path(os.environ.get("AUDIO8_VOICES_DIR", str(root / "voices")))
    runtime, model, voices = (p.expanduser().resolve() for p in (runtime, model, voices))
    return runtime, model, voices


def available() -> bool:
    _, model, voices = _paths()
    return (model / "runtime_manifest.json").is_file() and voices.is_dir()


def _ensure_installed(runtime_dir: Path, model_dir: Path, voices_dir: Path) -> None:
    """Install the standalone runtime/model only when an Audio8 voice is used."""
    if (runtime_dir / "arktts_runtime" / "runtime.py").is_file() and (model_dir / "runtime_manifest.json").is_file():
        voices_dir.mkdir(parents=True, exist_ok=True)
        return
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    import sys
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "numpy<3", "scipy<2", "onnxruntime>=1.30", "tokenizers<0.23", "soundfile<1", "huggingface_hub"], check=True, timeout=1800)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "audio8.zip"
        urllib.request.urlretrieve("https://github.com/Edge0-AI/Audio8_TTS/archive/refs/heads/master.zip", archive)
        with zipfile.ZipFile(archive) as zf:
            prefix = next(name for name in zf.namelist() if name.endswith("onnx_runtime_0_1b_int8/arktts_runtime/runtime.py"))
            root = prefix.split("onnx_runtime_0_1b_int8/", 1)[0] + "onnx_runtime_0_1b_int8/"
            for name in zf.namelist():
                if name.startswith(root):
                    target = runtime_dir / name[len(root):]
                    if name.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(name))
    from huggingface_hub import snapshot_download
    snapshot_download("Audio8/audio8-TTS-0.1B-ONNX-INT8", local_dir=str(model_dir))
    voices_dir.mkdir(parents=True, exist_ok=True)


def _ensure_zmtss_voice(name: str, runtime_dir: Path, model_dir: Path, voices_dir: Path) -> None:
    target = voices_dir / name
    if (target / "meta.json").is_file() and (target / "codes.npy").is_file():
        return
    from pipeline.tts import zmtss_catalog
    item = zmtss_catalog.get(f"zmt:{name}") or zmtss_catalog.get(name)
    if not item:
        raise RuntimeError(f"Audio8 voice not found: {name}")
    from arktts_runtime.registration import VoiceRegistration
    manifest = __import__("json").loads((model_dir / "runtime_manifest.json").read_text(encoding="utf-8"))
    with tempfile.NamedTemporaryFile(suffix=".mp3") as handle, tempfile.NamedTemporaryFile(suffix=".wav") as normalized:
        request = urllib.request.Request(zmtss_catalog.remote_url(item), headers={"User-Agent": "zmAI-Audio8/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            handle.write(response.read())
        handle.flush()
        converted = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", handle.name,
             "-ac", "1", "-ar", str(manifest["sample_rate"]), "-c:a", "pcm_s16le", normalized.name],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if converted.returncode != 0:
            raise RuntimeError(f"Không chuẩn hoá audio mẫu Audio8 / Could not normalize Audio8 reference: {(converted.stderr or '').strip()[-400:]}")
        VoiceRegistration(model_dir / "registration", voices_dir, manifest["model_fingerprint"]).register(
            Path(normalized.name).read_bytes(), Path(normalized.name).name,
            str(item.get("script") or "").strip(), name, overwrite=True,
        )


def synthesize(text: str, voice: str, out_wav: Path, *, max_new_tokens: int = 256) -> None:
    runtime_dir, model_dir, voices_dir = _paths()
    if not available():
        try:
            _ensure_installed(runtime_dir, model_dir, voices_dir)
        except Exception as exc:
            raise RuntimeError(f"Audio8-TTS-0.1B chưa sẵn sàng / model-runtime bootstrap failed: {exc}") from exc
    import sys
    runtime_root = str(runtime_dir)
    if runtime_root not in sys.path:
        sys.path.insert(0, runtime_root)
    from arktts_runtime.runtime import ArkTtsRuntime
    import soundfile as sf

    name = voice.removeprefix("audio8:").removeprefix("zmt:")
    _ensure_zmtss_voice(name, runtime_dir, model_dir, voices_dir)
    runtime = ArkTtsRuntime(model_dir, voices_dir, threads=5)
    audio, _ = runtime.synthesize(text=text, voice=name, max_new_tokens=max_new_tokens,
                                  temperature=0.7, top_p=0.9, top_k=50, seed=42)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), audio, int(runtime.manifest["sample_rate"]))
