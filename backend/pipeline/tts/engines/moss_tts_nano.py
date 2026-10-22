"""MOSS-TTS-Nano ONNX CPU adapter in an isolated, on-demand runtime.

Existing catalog/clone IDs resolve to the original reference WAV; MOSS encodes
that reference with its own tokenizer.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
import urllib.request
import wave
import zipfile
from pipeline.core.resource_lock import shared_resource_lock

SOURCE_REVISION = "8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb"
SOURCE_URL = f"https://github.com/OpenMOSS/MOSS-TTS-Nano/archive/{SOURCE_REVISION}.zip"
TTS_REPO = "OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX"
CODEC_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"
_lock = threading.Lock()
_log = logging.getLogger(__name__)


def _paths() -> tuple[Path, Path, Path]:
    from pipeline.core.config import DATA, RESOURCE_DATA
    root = RESOURCE_DATA / "moss_tts_nano"
    return tuple(Path(os.environ.get(key, str(default))).expanduser().resolve() for key, default in (
        ("MOSS_TTS_NANO_RUNTIME_DIR", root / "source"),
        ("MOSS_TTS_NANO_MODEL_DIR", root / "models"),
        ("MOSS_TTS_NANO_VOICES_DIR", DATA / "moss_tts_nano" / "references"),
    ))


def _python() -> Path:
    from pipeline.core.config import RESOURCE_DATA
    # Keep the venv launcher path itself.  Resolving it follows the symlink to
    # Homebrew/system Python, which makes pip target the externally-managed
    # interpreter instead of the isolated MOSS environment.
    configured = Path(os.environ.get("MOSS_TTS_NANO_PYTHON", str(
        RESOURCE_DATA / "moss_tts_nano" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    ))).expanduser()
    return Path(os.path.abspath(configured))


def available() -> bool:
    """Only a successful load probe and intact model files count as installed."""
    source, model, _ = _paths()
    try:
        state = json.loads((model / "zm-ready.json").read_text(encoding="utf-8"))
        return bool(
            state["revision"] == SOURCE_REVISION and state["python"] == str(_python())
            and state["source"] == str(source) and _python().is_file()
            and (source / "infer_onnx.py").is_file() and state["files"]
            and all((model / name).is_file() and (model / name).stat().st_size == size
                    for name, size in state["files"].items())
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _run(command: list[str], *, timeout: int = 1800, cancel_check=None, progress=None) -> None:
    """Bound subprocess lifetime and release model RAM at the end of each job."""
    from pipeline.core.jobs import Cancelled
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel_check and cancel_check():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError("MOSS-TTS-Nano: quá thời gian xử lý / operation timed out")
                time.sleep(0.2)
            output.seek(0)
            detail = output.read().decode("utf-8", errors="replace")[-8000:]
            if progress and detail:
                progress(90, detail)
            if process.returncode:
                _log.error("MOSS-TTS-Nano subprocess exit=%s\n%s", process.returncode, detail)
                raise RuntimeError(f"MOSS-TTS-Nano exit={process.returncode}: {detail}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def install(progress: Callable | None = None) -> dict:
    """Install pinned upstream source, CPU dependencies and both ONNX models."""
    with _lock, shared_resource_lock("moss"):
        if available():
            return {"message": "MOSS-TTS-Nano đã cài / installed", "needsRestart": False}
        source, model, refs = _paths()
        def report(pct, message):
            _log.info("MOSS-TTS-Nano: %s", message)
            if progress:
                progress(pct, message)

        report(5, "MOSS-TTS-Nano: môi trường riêng / isolated Python environment")
        python = _python()
        if not python.is_file():
            if os.environ.get("MOSS_TTS_NANO_PYTHON"):
                raise RuntimeError("MOSS_TTS_NANO_PYTHON không tồn tại / interpreter does not exist")
            if getattr(sys, "frozen", False):
                from pipeline.core.runtime_active import runtime_home, runtime_python
                candidate = runtime_python()
                if not candidate.is_file():
                    # A failed/first runtime install has no current.json yet;
                    # use the bundled patch interpreter directly instead of the
                    # stale .venv-runtime path (which causes WinError 2).
                    candidate = (runtime_home() / "runtime" / "python" /
                                 "cpython-3.12.10-windows-x86_64-none" / "python.exe")
                if not candidate.is_file():
                    raise RuntimeError(
                        "Không tìm thấy Python runtime để tạo MOSS-TTS-Nano / "
                        "Python runtime is missing; install the AI runtime first"
                    )
                base_python = str(candidate)
            else:
                base_python = sys.executable
            _run([base_python, "-I", "-m", "venv", *(["--copies"] if os.name == "nt" else []), str(python.parent.parent)])
        # Upstream's ONNX frontend imports torch/torchaudio for reference loading.
        # Keep their CPU wheels out of the application's GPU environment.
        report(15, "MOSS-TTS-Nano: cài thư viện CPU / installing CPU dependencies")
        torch_cmd = [str(python), "-m", "pip", "install", "torch==2.7.0", "torchaudio==2.7.0"]
        if sys.platform != "darwin":
            torch_cmd += ["--index-url", "https://download.pytorch.org/whl/cpu"]
        _run(torch_cmd)
        if os.name == "nt":
            # libtorchaudio.pyd needs VCOMP140.DLL; ship it beside the extension
            # like the main Windows runtime (msvc-runtime + _ensure_openmp).
            _run([str(python), "-m", "pip", "install", "msvc-runtime==14.44.35112"])
            from pipeline.core.runtime_install import _ensure_openmp
            _ensure_openmp(python.parent.parent)
        _run([str(python), "-m", "pip", "install", "numpy<3", "soundfile<1",
              "sentencepiece>=0.2,<1", "onnxruntime>=1.20,<2", "huggingface_hub<2"])
        report(35, "MOSS-TTS-Nano: tải mã nguồn chính thức / downloading official source")
        with tempfile.TemporaryDirectory(prefix="zm-moss-install-") as tmp:
            archive = Path(tmp) / "source.zip"
            with urllib.request.urlopen(SOURCE_URL, timeout=60) as response, archive.open("wb") as out:
                shutil.copyfileobj(response, out)
            with zipfile.ZipFile(archive) as bundle:
                prefix = f"MOSS-TTS-Nano-{SOURCE_REVISION}/"
                for entry in bundle.infolist():
                    if not entry.filename.startswith(prefix) or entry.is_dir():
                        continue
                    relative = Path(entry.filename[len(prefix):])
                    target = (source / relative).resolve()
                    if source not in target.parents:
                        raise ValueError("Unsafe MOSS source archive path")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.read(entry))
        report(50, "MOSS-TTS-Nano: tải model TTS + codec / downloading TTS + codec models")
        _run([str(python), "-c",
              "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
              "from onnx_tts_runtime import _download_default_browser_onnx_assets; "
              "_download_default_browser_onnx_assets(Path(sys.argv[2]))", str(source), str(model)])
        report(85, "MOSS-TTS-Nano: kiểm tra nạp ONNX CPU / probing ONNX CPU sessions")
        _run([str(python), "-c",
              "import sys; sys.path.insert(0, sys.argv[1]); from onnx_tts_runtime import OnnxTtsRuntime; "
              "OnnxTtsRuntime(model_dir=sys.argv[2], thread_count=1, execution_provider='cpu', output_dir=sys.argv[3])",
              str(source), str(model), str(refs)], timeout=300)
        files = {str(p.relative_to(model)): p.stat().st_size for p in model.rglob("*")
                 if p.is_file() and not any(x.startswith(".") for x in p.relative_to(model).parts)
                 and p.name != "zm-ready.json"}
        (model / "zm-ready.json").write_text(json.dumps({
            "revision": SOURCE_REVISION, "python": str(python), "source": str(source), "files": files,
        }), encoding="utf-8")
        refs.mkdir(parents=True, exist_ok=True)
        report(100, "MOSS-TTS-Nano: đã kiểm tra CPU / CPU probe passed")
        return {"message": "MOSS-TTS-Nano đã cài / installed", "needsRestart": False}


def reference_path(voice: str) -> Path:
    from pipeline.tts import voice_store, zmtss_catalog
    name = voice.removeprefix("moss:")
    entry = voice_store.get_reference_voice(name)
    if entry:
        path = voice_store.reference_path(entry).resolve()
        if not any(root.resolve() in path.parents for root in (voice_store.REFERENCE_ROOT, voice_store.SHARED_REFERENCE_ROOT)) or not path.is_file():
            raise ValueError("MOSS: audio mẫu không hợp lệ / invalid reference audio")
        return path
    if name.startswith("vn:clone:"):
        entry = next((v for v in voice_store.load_cloned() if v["id"] == name[9:]), None)
        if entry:
            path = (voice_store.VIENEU_ROOT / entry["ref"]).resolve()
            if voice_store.VIENEU_ROOT.resolve() in path.parents and path.is_file():
                return path
    item = zmtss_catalog.get(name)
    if not item:
        raise ValueError(f"MOSS-TTS-Nano: không tìm thấy giọng / voice not found: {name}")
    _, _, refs = _paths()
    path = refs / zmtss_catalog.local_filename(item)
    if not path.is_file():
        zmtss_catalog.download_reference(item, path)
    return path


def synthesize(text: str, voice: str, out_wav: Path, *, cancel_check=None, on_progress=None) -> None:
    if not text.strip():
        raise ValueError("MOSS-TTS-Nano: văn bản rỗng / empty text")
    if not available():
        install()
    # Serialize heavy CPU jobs. Subprocess exit frees model memory rather than
    # holding one runtime per voice or per concurrent cue in the backend.
    while not _lock.acquire(timeout=0.2):
        if cancel_check and cancel_check():
            from pipeline.core.jobs import Cancelled
            raise Cancelled()
    try:
        source, model, _ = _paths()
        ref = reference_path(voice)
        try:
            threads = max(1, int(os.environ.get("MOSS_TTS_NANO_THREADS", "4")))
        except ValueError:
            threads = 4
        out_wav = Path(out_wav).resolve()
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        _log.info("MOSS-TTS-Nano CPU start voice=%r text_len=%d threads=%d", voice, len(text), threads)
        if on_progress:
            on_progress(0.05)
        with tempfile.TemporaryDirectory(prefix="zm-moss-synth-", dir=out_wav.parent) as tmp:
            text_file, generated = Path(tmp) / "text.txt", Path(tmp) / "output.wav"
            text_file.write_text(text, encoding="utf-8")
            _run([str(_python()), str(source / "infer_onnx.py"), "--model-dir", str(model),
                  "--text-file", str(text_file), "--prompt-audio-path", str(ref),
                  "--output-audio-path", str(generated), "--execution-provider", "cpu",
                  "--cpu-threads", str(threads), "--seed", "42", "--disable-wetext-processing",
                  "--disable-normalize-tts-text"], cancel_check=cancel_check)
            with wave.open(str(generated), "rb") as wav:
                if wav.getnframes() <= 0 or wav.getframerate() <= 0:
                    raise RuntimeError("MOSS-TTS-Nano: audio rỗng / empty audio")
                duration = wav.getnframes() / wav.getframerate()
            generated.replace(out_wav)
        _log.info("MOSS-TTS-Nano CPU complete duration=%.3fs output=%s", duration, out_wav)
        if on_progress:
            on_progress(1.0)
    except Exception:
        _log.exception("MOSS-TTS-Nano failed voice=%r", voice)
        raise
    finally:
        _lock.release()
