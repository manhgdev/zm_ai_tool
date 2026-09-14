"""Demucs stem separation + original audio extract/cache."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..core.jobs import kill_process_tree, register_process, run_cmd, unregister_process
from ..core.media import _has_audio_stream, ffprobe_duration, h264_encoder_args
from ..core.project import ensure_layout, out_final, set_status

# Per-project stem locks (mất khi tách file từ mux.py)
_stem_locks: dict[str, threading.Lock] = {}
_stem_locks_guard = threading.Lock()
_stem_running: set[str] = set()
_STEM_STALE_SEC = 45 * 60  # progress «running» quá lâu → coi crash

_TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu124"
_TORCH_ROCM_INDEX = "https://download.pytorch.org/whl/rocm6.2"
_TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_PCT_RE = re.compile(r"(\d{1,3})\s*%")
_MLX_SEPARATE_PY = r"""
import sys
from pathlib import Path
import numpy as np
import soundfile as sf

src = Path(sys.argv[1])
out_root = Path(sys.argv[2])
from demucs_mlx import Separator

sep = Separator(model="htdemucs", shifts=1, overlap=0.25)
_origin, stems = sep.separate_audio_file(str(src))
sr = int(getattr(sep, "sample_rate", None) or getattr(sep, "samplerate", None) or 44100)
track = out_root / "htdemucs" / src.stem
track.mkdir(parents=True, exist_ok=True)

def to_sf(audio):
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 2 and a.shape[0] <= 8 and a.shape[0] < a.shape[-1]:
        a = a.T
    return a

for name, audio in stems.items():
    sf.write(str(track / f"{name}.wav"), to_sf(audio), sr)

parts = [to_sf(stems[k]).astype(np.float64) for k in stems if str(k) != "vocals"]
if not parts:
    raise SystemExit("no non-vocal stems")
mix = parts[0]
for p in parts[1:]:
    n = min(mix.shape[0], p.shape[0])
    mix = mix[:n] + p[:n]
peak = float(np.max(np.abs(mix))) if mix.size else 1.0
if peak > 1.0:
    mix = mix / peak
sf.write(str(track / "no_vocals.wav"), mix.astype(np.float32), sr)
print("OK", track)
"""


def _num(v: Any, default: float) -> float:
    """JSON null / missing → default (seg.get('x', d) vẫn trả None khi key=null)."""
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _stem_lock(project_id: str) -> threading.Lock:
    with _stem_locks_guard:
        lock = _stem_locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _stem_locks[project_id] = lock
        return lock


def set_stem_progress(
    project_id: str | None,
    progress: int,
    message: str = "",
    *,
    running: bool = True,
) -> None:
    """Tiến độ tách no_vocals (preview) — file riêng, không đè status xuất."""
    if not project_id:
        return
    root = ensure_layout(project_id)
    path = root / "cache" / "stem_progress.json"
    data = {
        "progress": max(0, min(100, int(progress))),
        "message": str(message or ""),
        "running": bool(running),
        "ts": time.time(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read_stem_progress(project_id: str) -> dict[str, Any]:
    path = ensure_layout(project_id) / "cache" / "stem_progress.json"
    if not path.is_file():
        return {"progress": 0, "message": "", "running": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"progress": 0, "message": "", "running": False}
        running = bool(data.get("running"))
        # Process đang tách (cùng worker) luôn true; file có thể stale nếu crash
        if project_id in _stem_running:
            running = True
        else:
            try:
                ts = float(data.get("ts") or 0)
                if running and ts > 0 and (time.time() - ts) > _STEM_STALE_SEC:
                    running = False
            except (TypeError, ValueError):
                pass
        return {
            "progress": max(0, min(100, int(data.get("progress") or 0))),
            "message": str(data.get("message") or ""),
            "running": running,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"progress": 0, "message": "", "running": False}


def _wav_rms(path: Path) -> float:
    """RMS thô pcm_s16le (0..1) — chẩn đoán stem Demucs gần im."""
    import struct

    try:
        raw = subprocess.check_output(
            [
                "ffmpeg", "-v", "error", "-i", str(path),
                "-ac", "1", "-ar", "8000", "-f", "s16le", "-t", "120", "-",
            ],
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return 0.0
    if len(raw) < 4:
        return 0.0
    n = len(raw) // 2
    # lấy mẫu thưa để nhanh
    step = max(1, n // 40000)
    acc = 0.0
    count = 0
    for i in range(0, n, step):
        (sample,) = struct.unpack_from("<h", raw, i * 2)
        acc += float(sample) * float(sample)
        count += 1
    if count <= 0:
        return 0.0
    return (acc / count) ** 0.5 / 32768.0


def _nvidia_smi_ok() -> bool:
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=12)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def _demucs_backend_wanted() -> str:
    """cuda | rocm | mlx | cpu — backend tách lời tối ưu theo máy."""
    if _nvidia_smi_ok():
        return "cuda"
    if _apple_silicon():
        return "mlx"  # demucs-mlx (Metal) — torch MPS thiếu op complex
    if sys.platform.startswith("linux"):
        try:
            from pipeline.core.media import detect_device

            if detect_device().get("accel") == "rocm":
                return "rocm"
        except Exception:
            pass
    return "cpu"


def _torch_device(exe: Path) -> str:
    """cuda | mps | cpu — probe torch trong venv (không gồm mlx)."""
    if not exe.is_file():
        return "cpu"
    try:
        r = subprocess.run(
            [
                str(exe),
                "-c",
                (
                    "import torch\n"
                    "if torch.cuda.is_available():\n"
                    " print('cuda')\n"
                    "elif getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():\n"
                    " print('mps')\n"
                    "else:\n"
                    " print('cpu')\n"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        out = (r.stdout or "").strip().lower()
        if r.returncode == 0 and out in ("cuda", "mps", "cpu"):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return "cpu"


def _has_pkg(exe: Path, import_stmt: str, *, timeout: float = 90) -> bool:
    if not exe.is_file():
        return False
    try:
        r = subprocess.run(
            [str(exe), "-c", import_stmt],
            capture_output=True,
            timeout=timeout,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _demucs_accel(exe: Path) -> str:
    """Backend sẵn có trong venv: mlx | cuda | mps | cpu."""
    if _apple_silicon() and _has_pkg(exe, "import demucs_mlx"):
        return "mlx"
    if _has_pkg(exe, "import demucs, soundfile"):
        return _torch_device(exe)
    return "cpu"


def _demucs_jobs() -> int:
    # Demucs -j: song song preprocess; Apple Silicon nhiều lõi → tới 6
    n = os.cpu_count() or 2
    cap = 6 if _apple_silicon() else 4
    return max(1, min(cap, max(2, n // 2)))


def _pip_install_torch(py: Path, *, accel: str, project_id: str | None) -> None:
    """accel: cuda | rocm | cpu | mac (PyPI macOS arm64 có Metal trong torch)."""
    pip = [str(py), "-m", "pip"]
    if accel in ("cuda", "rocm"):
        label = "CUDA" if accel == "cuda" else "ROCm"
        set_stem_progress(project_id, 10, f"Cài PyTorch {label} (có thể vài phút)…")
        subprocess.run(
            pip + ["uninstall", "-y", "torch", "torchaudio", "torchvision"],
            capture_output=True,
            timeout=300,
        )
        cmd = pip + [
            "install",
            "--upgrade",
            "torch",
            "torchaudio",
            "--index-url",
            _TORCH_CUDA_INDEX if accel == "cuda" else _TORCH_ROCM_INDEX,
        ]
    elif accel == "mac":
        label = "macOS (Metal)"
        set_stem_progress(project_id, 10, f"Cài PyTorch {label}…")
        cmd = pip + ["install", "--upgrade", "torch", "torchaudio"]
    else:
        label = "CPU"
        set_stem_progress(project_id, 10, f"Cài PyTorch {label}…")
        if sys.platform == "darwin":
            cmd = pip + ["install", "--upgrade", "torch", "torchaudio"]
        else:
            cmd = pip + [
                "install",
                "--upgrade",
                "torch",
                "torchaudio",
                "--index-url",
                _TORCH_CPU_INDEX,
            ]
    r_torch = subprocess.run(cmd, capture_output=True, text=True, timeout=2400)
    if r_torch.returncode != 0:
        raise RuntimeError(
            f"Không cài được PyTorch {label} cho Demucs.\n"
            + ((r_torch.stderr or r_torch.stdout or "")[-800:])
        )


def _pip_install_demucs_mlx(py: Path, project_id: str | None) -> None:
    pip = [str(py), "-m", "pip"]
    set_stem_progress(project_id, 12, "Cài demucs-mlx (Apple GPU / Metal)…")
    r = subprocess.run(
        pip + ["install", "--upgrade", "demucs-mlx", "soundfile"],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "Không cài được demucs-mlx.\n" + ((r.stderr or r.stdout or "")[-800:])
        )


def _pip_install_demucs_torch(py: Path, project_id: str | None) -> None:
    pip = [str(py), "-m", "pip"]
    set_stem_progress(project_id, 14, "Cài Demucs…")
    r = subprocess.run(
        pip + ["install", "--upgrade", "demucs", "soundfile"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "Không cài được Demucs.\n" + ((r.stderr or r.stdout or "")[-800:])
        )


def _demucs_root_candidates() -> list[Path]:
    """Ưu tiên ZM_AI_TOOL_HOME (app), rồi backend/, rồi LocalAppData."""
    roots: list[Path] = []
    home = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    if home:
        roots.append(Path(home))
    server = Path(__file__).resolve().parents[2]
    roots.append(server)
    if sys.platform == "win32":
        local_root = Path(os.environ.get("LOCALAPPDATA", "") or "")
        if str(local_root):
            roots.append(local_root / "ZM_AI_TOOL")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / "ZM_AI_TOOL")
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        roots.append(xdg / "ZM_AI_TOOL")
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r.resolve()) if r.exists() else str(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _demucs_py_in(root: Path) -> Path:
    return root / ".venv-demucs" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )


def _demucs_install_root() -> Path:
    """Nơi tạo/cài venv Demucs: app home khi frozen, backend/ khi dev."""
    home = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    if home or getattr(sys, "frozen", False):
        return Path(home or _demucs_root_candidates()[0])
    return Path(__file__).resolve().parents[2]


def _demucs_python(project_id: str | None = None, *, report: bool = True) -> str:
    """Python có demucs: Apple Silicon → demucs-mlx; NVIDIA → torch CUDA; khác → CPU."""
    wanted = _demucs_backend_wanted()

    def _ready(exe: Path) -> bool:
        if wanted == "mlx":
            return _has_pkg(exe, "import demucs_mlx, soundfile")
        return _has_pkg(exe, "import demucs, soundfile")

    def _ensure(exe: Path) -> None:
        if wanted == "mlx":
            if _ready(exe):
                return
            if report and project_id:
                set_status(
                    project_id,
                    step="export",
                    progress=62,
                    message="Đang cài demucs-mlx (Apple Silicon GPU)…",
                    running=True,
                )
            set_stem_progress(project_id, 8, "Nâng cấp Demucs → Apple Metal (MLX)…")
            _pip_install_demucs_mlx(exe, project_id)
            return
        if wanted in ("cuda", "rocm"):
            if _ready(exe) and _torch_device(exe) == "cuda":
                return
            if report and project_id:
                set_status(
                    project_id,
                    step="export",
                    progress=62,
                    message=f"Đang cài PyTorch {wanted.upper()} cho tách lời…",
                    running=True,
                )
            set_stem_progress(project_id, 8, f"Nâng cấp PyTorch → {wanted.upper()}…")
            try:
                _pip_install_torch(exe, accel=wanted, project_id=project_id)
            except RuntimeError:
                set_stem_progress(project_id, 10, f"{wanted.upper()} fail — fallback CPU…")
                _pip_install_torch(exe, accel="cpu", project_id=project_id)
            if _torch_device(exe) != "cuda":
                set_stem_progress(
                    project_id, 10,
                    f"Torch {wanted.upper()} không nhận GPU — fallback CPU…",
                )
                _pip_install_torch(exe, accel="cpu", project_id=project_id)
            if not _has_pkg(exe, "import demucs, soundfile"):
                _pip_install_demucs_torch(exe, project_id)
            return
        # CPU (hoặc Intel Mac)
        if _ready(exe):
            return
        _pip_install_torch(
            exe,
            accel="mac" if sys.platform == "darwin" else "cpu",
            project_id=project_id,
        )
        _pip_install_demucs_torch(exe, project_id)

    # 1) Dùng venv đã có demucs (app home / server / LocalAppData)
    for root in _demucs_root_candidates():
        cand = _demucs_py_in(root)
        if not _ready(cand):
            continue
        if wanted in ("cuda", "rocm") and _torch_device(cand) != "cuda":
            try:
                _ensure(cand)
            except Exception:
                pass
            if _ready(cand) and _torch_device(cand) == "cuda":
                return str(cand)
            continue
        return str(cand)

    if not getattr(sys, "frozen", False):
        cur = Path(sys.executable)
        if _ready(cur) and wanted != "cuda":
            return str(cur)

    # 2) Cài vào root chuẩn (app home khi packaged)
    install_root = _demucs_install_root()
    venv = install_root / ".venv-demucs"
    py = _demucs_py_in(install_root)

    if report and project_id:
        set_status(
            project_id,
            step="export",
            progress=62,
            message="Đang cài Demucs (xóa lời AI) — lần đầu có thể mất vài phút…",
            running=True,
        )
    set_stem_progress(project_id, 4, "Đang cài Demucs / backend GPU (lần đầu)…")
    if not py.is_file():
        if getattr(sys, "frozen", False):
            uv = shutil.which("uv")
            if not uv:
                raise RuntimeError("Bản ứng dụng thiếu uv để cài Demucs")
            subprocess.run(
                [uv, "venv", "--python", "3.12", "--seed", str(venv)],
                check=True,
                capture_output=True,
                timeout=900,
            )
        else:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                check=True,
                capture_output=True,
                timeout=180,
            )
    pip = [str(py), "-m", "pip"]
    set_stem_progress(project_id, 6, "Cài pip / wheel…")
    subprocess.run(pip + ["install", "-U", "pip", "wheel"], capture_output=True, timeout=300)
    _ensure(py)
    if not _ready(py):
        raise RuntimeError(
            f"Đã cài Demucs nhưng import vẫn lỗi — kiểm tra {venv}"
        )
    accel = _demucs_accel(py)
    set_stem_progress(project_id, 16, f"Đã sẵn sàng Demucs ({accel})")
    return str(py)


def _run_demucs_mlx_progress(
    project_id: str,
    python: str,
    source_wav: Path,
    separated: Path,
) -> tuple[int, str]:
    """Apple Silicon: demucs-mlx trên Metal (nhanh hơn torch MPS / CPU)."""
    set_stem_progress(project_id, 18, "Demucs-MLX (Apple GPU) đang tách…")
    separated.mkdir(parents=True, exist_ok=True)
    from ..core.runtime_site import subprocess_environment

    env = subprocess_environment({"PYTHONUNBUFFERED": "1"})
    kw: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    if sys.platform == "win32":
        kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    proc = subprocess.Popen(
        [python, "-c", _MLX_SEPARATE_PY, str(source_wav), str(separated)],
        **kw,
    )
    register_process(project_id, proc)
    assert proc.stdout is not None
    last_pct = 18
    lock = threading.Lock()
    stop_hb = threading.Event()
    err_chunks: list[str] = []

    def _heartbeat() -> None:
        nonlocal last_pct
        while not stop_hb.wait(2.0):
            with lock:
                if last_pct < 88:
                    last_pct = min(88, last_pct + 2)
                    set_stem_progress(
                        project_id, last_pct, f"Demucs-MLX (Apple GPU)… {last_pct}%"
                    )

    hb = threading.Thread(target=_heartbeat, name="stem-mlx-hb", daemon=True)
    hb.start()
    try:
        for line in proc.stdout:
            err_chunks.append(line)
            if len(err_chunks) > 60:
                err_chunks = err_chunks[-30:]
            if line.strip().startswith("OK"):
                with lock:
                    last_pct = 88
                    set_stem_progress(project_id, 88, "Demucs-MLX xong — ghi stem…")
        code = proc.wait(timeout=3600)
    except Exception:
        kill_process_tree(proc)
        raise
    finally:
        unregister_process(project_id, proc)
        stop_hb.set()
        hb.join(timeout=1.0)
    return code, "".join(err_chunks)[-800:]


def _run_demucs_progress(
    project_id: str,
    python: str,
    source_wav: Path,
    separated: Path,
) -> tuple[int, str]:
    """Chạy demucs: mlx (Apple) / cuda / mps / cpu."""
    accel = _demucs_accel(Path(python))
    if accel == "mlx":
        return _run_demucs_mlx_progress(project_id, python, source_wav, separated)

    device = accel if accel in ("cuda", "mps", "cpu") else "cpu"
    jobs = _demucs_jobs()
    # CUDA 6GB: segment 6; MPS thử không segment trước
    segment = "6" if device == "cuda" else None
    set_stem_progress(
        project_id,
        18,
        f"Demucs đang tách ({device}, -j {jobs})…",
    )

    def _launch(seg: str | None) -> subprocess.Popen[str]:
        cmd = [
            python,
            "-m",
            "demucs",
            "--two-stems",
            "vocals",
            "--shifts",
            "1",
            "--overlap",
            "0.25",
            "-j",
            str(jobs),
            "--device",
            device,
            "-o",
            str(separated),
        ]
        if seg:
            cmd.extend(["--segment", seg])
        cmd.append(str(source_wav))
        from ..core.runtime_site import subprocess_environment

        env = subprocess_environment({"PYTHONUNBUFFERED": "1", "TQDM_MINITERS": "1"})
        kw: dict = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        if sys.platform == "win32":
            kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        return subprocess.Popen(cmd, **kw)

    def _consume(proc: subprocess.Popen[str]) -> tuple[int, str]:
        register_process(project_id, proc)
        assert proc.stdout is not None
        last_pct = 18
        lock = threading.Lock()
        stop_hb = threading.Event()
        err_chunks: list[str] = []

        def _heartbeat() -> None:
            nonlocal last_pct
            while not stop_hb.wait(2.5):
                with lock:
                    if last_pct < 88:
                        last_pct = min(88, last_pct + 1)
                        set_stem_progress(
                            project_id, last_pct, f"Demucs ({device})… {last_pct}%"
                        )

        hb = threading.Thread(target=_heartbeat, name="stem-hb", daemon=True)
        hb.start()
        try:
            for line in proc.stdout:
                err_chunks.append(line)
                if len(err_chunks) > 80:
                    err_chunks = err_chunks[-40:]
                m = _PCT_RE.search(line.replace("\r", " "))
                if not m:
                    continue
                raw = max(0, min(100, int(m.group(1))))
                mapped = 18 + int(raw * 0.70)
                with lock:
                    if mapped > last_pct:
                        last_pct = mapped
                        set_stem_progress(
                            project_id, last_pct, f"Demucs ({device})… {last_pct}%"
                        )
            code = proc.wait(timeout=3600)
        except Exception:
            kill_process_tree(proc)
            raise
        finally:
            unregister_process(project_id, proc)
            stop_hb.set()
            hb.join(timeout=1.0)
        return code, "".join(err_chunks)[-600:]

    proc = _launch(segment)
    code, err_tail = _consume(proc)
    # MPS không hỗ trợ → fallback CPU một lần
    mps_fail = (
        code != 0
        and device == "mps"
        and any(
            s in err_tail.lower()
            for s in ("not implemented", "mps", "complex", "backend")
        )
    )
    if mps_fail:
        set_stem_progress(project_id, 18, "MPS không hỗ trợ model — fallback CPU…")
        shutil.rmtree(separated, ignore_errors=True)
        separated.mkdir(parents=True, exist_ok=True)
        device = "cpu"
        proc2 = _launch(None)
        return _consume(proc2)
    oom = code != 0 and any(
        s in err_tail.lower()
        for s in ("out of memory", "cuda out of memory", "cudnn_status")
    )
    if oom and device == "cuda" and segment != "4":
        set_stem_progress(project_id, 18, "GPU thiếu VRAM — thử segment nhỏ hơn…")
        shutil.rmtree(separated, ignore_errors=True)
        separated.mkdir(parents=True, exist_ok=True)
        proc2 = _launch("4")
        code, err_tail = _consume(proc2)
    return code, err_tail


def _audio_cache_key(video: Path) -> str:
    stat = video.stat()
    # v5: bắt buộc Demucs thật; invalidate cache stereotools (v4 trở xuống)
    return hashlib.sha1(
        f"{video.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|v5".encode()
    ).hexdigest()[:12]


def find_cached_no_vocals(project_id: str) -> Path | None:
    """Stem no_vocals đã tách trong cache project (bất kỳ key nào)."""
    root = ensure_layout(project_id)
    cache_dir = root / "cache"
    if not cache_dir.is_dir():
        return None
    best: Path | None = None
    best_mtime = -1.0
    for p in cache_dir.glob("no_vocals_*.wav"):
        try:
            if p.stat().st_size <= 1024:
                continue
            mt = p.stat().st_mtime
            if mt > best_mtime:
                best = p
                best_mtime = mt
        except OSError:
            continue
    return best


def resolve_stem_source_video(project_id: str, video: Path | None = None) -> Path:
    """Video dùng để tách/cache stem = file nguồn gốc (meta.videoPath).

    Preview và export phải cùng key — không dùng work bake / retime
    (mtime/path khác → Demucs chạy lại).
    """
    try:
        from ..core.project import load_meta

        meta = load_meta(project_id) or {}
        src = Path(str(meta.get("videoPath") or ""))
        if src.is_file():
            return src.resolve()
    except Exception:
        pass
    if video is not None and Path(video).is_file():
        return Path(video).resolve()
    raise FileNotFoundError("Thiếu video nguồn để tách xóa lời")


def extract_original_audio(project_id: str, video: Path) -> Path:
    """Trích WAV mono-stereo từ video gốc (cache)."""
    root = ensure_layout(project_id)
    key = _audio_cache_key(video)
    cache = root / "cache" / f"original_{key}.wav"
    if cache.is_file() and cache.stat().st_size > 1024:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        project_id,
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(cache),
        ],
    )
    if not cache.is_file() or cache.stat().st_size < 64:
        raise RuntimeError("Không trích được âm thanh gốc từ video")
    return cache


def separate_vocals(project_id: str, video: Path, *, report: bool = True) -> Path:
    """Stem chỉ lời (Demucs vocals). Chạy no_vocals nếu chưa tách."""
    root = ensure_layout(project_id)
    key = _audio_cache_key(video)
    cache = root / "cache" / f"vocals_{key}.wav"
    if cache.is_file() and cache.stat().st_size > 1024:
        return cache
    # Tách no_vocals cũng ghi vocals_* nếu Demucs có file
    separate_no_vocals(project_id, video, report=report)
    if cache.is_file() and cache.stat().st_size > 1024:
        return cache
    # Fallback: demucs work dir còn lại
    work = root / "cache" / f"demucs_{key}"
    source_wav = work / "source.wav"
    vocals_src = work / "separated" / "htdemucs" / source_wav.stem / "vocals.wav"
    if vocals_src.is_file() and vocals_src.stat().st_size > 1024:
        run_cmd(
            project_id,
            [
                "ffmpeg",
                "-y",
                "-i",
                str(vocals_src),
                "-c:a",
                "pcm_s16le",
                str(cache),
            ],
        )
        if cache.is_file() and cache.stat().st_size > 64:
            return cache
    raise RuntimeError(
        "Chưa có stem giữ lời — chạy tách âm (Xóa lời) trước hoặc Demucs không xuất vocals.wav"
    )


def export_project_audio(
    project_id: str,
    video: Path,
    kind: str,
    *,
    report: bool = False,
) -> Path:
    """kind: original | no_vocals | vocals → Path WAV cache."""
    k = (kind or "original").strip().lower()
    if k in ("original", "source", "full"):
        return extract_original_audio(project_id, video)
    if k in ("no_vocals", "novocals", "bg", "instrumental"):
        return separate_no_vocals(project_id, video, report=report)
    if k in ("vocals", "voice", "speech"):
        return separate_vocals(project_id, video, report=report)
    raise ValueError(f"kind không hợp lệ: {kind}")


def separate_no_vocals(
    project_id: str, video: Path | None = None, *, report: bool = True
) -> Path:
    """Demucs: bỏ stem vocals, giữ nhạc/SFX.

    Không dùng stereotools làm «xóa lời» — filter đó vẫn để lại lời, cache sai.
    Demucs lỗi → nền im (đúng hơn còn lời).
    report=False: gọi từ preview, không ghi đè status job xuất.

    Cache theo videoPath gốc — preview + export dùng chung, không tách lại.
    """
    root = ensure_layout(project_id)
    # 1) Bất kỳ stem đã có trong cache project → dùng ngay (không Demucs lại)
    existing = find_cached_no_vocals(project_id)
    if existing is not None:
        set_stem_progress(project_id, 100, "Đã có stem xóa lời (cache)", running=False)
        return existing

    try:
        stem_video = resolve_stem_source_video(project_id, video)
    except FileNotFoundError:
        if video is None or not Path(video).is_file():
            raise
        stem_video = Path(video).resolve()

    key = _audio_cache_key(stem_video)
    cache = root / "cache" / f"no_vocals_{key}.wav"
    if cache.exists() and cache.stat().st_size > 1024:
        set_stem_progress(project_id, 100, "Đã có stem xóa lời", running=False)
        return cache

    lock = _stem_lock(project_id)
    # Đang tách: đợi job kia xong (không spawn Demucs thứ 2)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        set_stem_progress(project_id, max(2, read_stem_progress(project_id)["progress"]), "Đang tách xóa lời…", running=True)
        lock.acquire(blocking=True)
        acquired = True
    try:
        # Double-check sau lock
        existing = find_cached_no_vocals(project_id)
        if existing is not None:
            set_stem_progress(project_id, 100, "Đã có stem xóa lời (cache)", running=False)
            return existing
        if cache.exists() and cache.stat().st_size > 1024:
            set_stem_progress(project_id, 100, "Đã có stem xóa lời", running=False)
            return cache

        _stem_running.add(project_id)
        set_stem_progress(project_id, 2, "Chuẩn bị tách xóa lời…")
        python = _demucs_python(project_id, report=report)
        work = root / "cache" / f"demucs_{key}"
        work.mkdir(parents=True, exist_ok=True)
        source_wav = work / "source.wav"
        set_stem_progress(project_id, 12, "Trích âm thanh từ video…")
        run_cmd(
            project_id,
            [
                "ffmpeg", "-y", "-i", str(stem_video), "-vn", "-ac", "2", "-ar", "44100",
                str(source_wav),
            ],
        )

        demucs_ok = False
        result: Path | None = None
        separated = work / "separated"
        demucs_err = ""
        try:
            if report:
                set_status(
                    project_id,
                    step="export",
                    progress=66,
                    message="Demucs đang xóa lời (giữ nhạc/SFX) · 1 tiến trình…",
                    running=True,
                )
            code, demucs_err = _run_demucs_progress(
                project_id, python, source_wav, separated
            )
            if code != 0 and not demucs_err:
                demucs_err = f"exit {code}"
            result = separated / "htdemucs" / source_wav.stem / "no_vocals.wav"
            demucs_ok = result.exists() and result.stat().st_size > 1024
            if not demucs_ok and not demucs_err:
                demucs_err = "không thấy file no_vocals.wav sau Demucs"
        except Exception as e:
            demucs_ok = False
            demucs_err = str(e)[:600]

        if demucs_ok and result is not None:
            set_stem_progress(project_id, 92, "Chỉnh mức âm stem…")
            # Video thoại mono: stem gần im — boost nhẹ, KHÔNG trộn lại gốc.
            src_rms = max(_wav_rms(source_wav), 1e-6)
            stem_rms = _wav_rms(result)
            ratio = stem_rms / src_rms
            if ratio >= 0.12:
                gain = min(2.6, max(1.25, 0.72 / max(ratio, 0.12)))
            elif ratio >= 0.02:
                gain = min(3.5, max(1.5, 0.15 / max(ratio, 0.001)))
            else:
                # Gần như không còn nhạc/SFX — giữ gần im (đúng với clip chỉ lời)
                gain = 1.0
            run_cmd(
                project_id,
                [
                    "ffmpeg", "-y", "-i", str(result),
                    "-af",
                    f"volume={gain:.3f},alimiter=limit=0.95:level=disabled",
                    "-c:a", "pcm_s16le", str(cache),
                ],
            )
            # Cache vocals stem cùng lúc (nếu Demucs có file) — dùng cho tải «giữ lời»
            vocals_src = result.parent / "vocals.wav"
            vocals_cache = root / "cache" / f"vocals_{key}.wav"
            if vocals_src.is_file() and vocals_src.stat().st_size > 1024:
                try:
                    if not vocals_cache.is_file() or vocals_cache.stat().st_size < 1024:
                        run_cmd(
                            project_id,
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(vocals_src),
                                "-c:a",
                                "pcm_s16le",
                                str(vocals_cache),
                            ],
                        )
                except Exception:
                    pass
            set_stem_progress(project_id, 100, "Xong xóa lời", running=False)
            shutil.rmtree(work, ignore_errors=True)
            return cache

        # Demucs thất bại: nền im — stereotools cũ để lại lời → lệch setting «Xóa lời».
        if report and project_id:
            set_status(
                project_id,
                step="export",
                progress=68,
                message=(
                    "Demucs lỗi — tạm tắt âm gốc (tránh còn lời). "
                    f"Chi tiết: {demucs_err[:180]}"
                    if demucs_err
                    else "Demucs lỗi — tạm tắt âm gốc (tránh còn lời)."
                ),
                running=True,
            )
        set_stem_progress(
            project_id,
            0,
            f"Lỗi tách: {(demucs_err or 'không rõ')[:120]}",
            running=False,
        )
        dur = max(0.1, ffprobe_duration(source_wav) or ffprobe_duration(video) or 1.0)
        run_cmd(
            project_id,
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{dur:.3f}",
                "-c:a", "pcm_s16le", str(cache),
            ],
        )

        shutil.rmtree(work, ignore_errors=True)
        if not cache.exists():
            raise RuntimeError(
                "Không tạo được track xóa lời (Demucs)."
                + (f" {demucs_err}" if demucs_err else "")
            )
        return cache
    finally:
        _stem_running.discard(project_id)
        if acquired:
            lock.release()
    return cache
