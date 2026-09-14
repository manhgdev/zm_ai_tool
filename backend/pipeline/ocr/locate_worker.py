"""Worker subprocess cho OCR locate — spawn process riêng, crash không kéo tắt app.

Logic OCR probe nằm ở locate.py; file này chỉ lo:
- chọn interpreter CÓ rapidocr/cv2 (ưu tiên CUDA) — probe thật, không tin sys.executable
- spawn + register_process (Huỷ kill được) + kẹp affinity/priority (không đơ máy)
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from .logo import detect_logo_bbox_inprocess as _bundled_logo_detector  # noqa: F401

_LOCATE_TIMEOUT_SECONDS = 120


def _python_can_ocr(exe: str) -> tuple[bool, str]:
    """(OCR usable, accelerator) — probe providers in a clean process."""
    try:
        proc = subprocess.run(
            [
                exe,
                "-c",
                "import cv2, rapidocr_onnxruntime, onnxruntime as ort;"
                "p=ort.get_available_providers();"
                "print('CUDA' if 'CUDAExecutionProvider' in p else "
                "('DirectML' if 'DmlExecutionProvider' in p else "
                "('CoreML' if 'CoreMLExecutionProvider' in p else 'CPU')))",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                if sys.platform == "win32"
                else 0
            ),
        )
    except (OSError, subprocess.SubprocessError):
        return False, "CPU"
    if proc.returncode != 0:
        return False, "CPU"
    mode = (proc.stdout or "").strip().splitlines()
    return True, mode[-1] if mode else "CPU"


@lru_cache(maxsize=1)
def _dev_worker_python() -> str:
    """Interpreter chay worker OCR (dev).

    sys.executable co the la Python he thong (khong co rapidocr/cv2, chi ORT CPU)
    khi server duoc khoi dong ngoai .venv — khi do OCR am tham chay CPU hoac
    tra 0 box. Uu tien interpreter co du goi VA co CUDA.
    """
    repo_backend = Path(__file__).resolve().parents[2]
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    exe_name = "python.exe" if sys.platform == "win32" else "python"
    candidates: list[str] = [sys.executable]
    for venv in (repo_backend / ".venv", repo_backend.parent / ".venv"):
        cand = venv / bin_dir / exe_name
        if cand.is_file() and str(cand) not in candidates:
            candidates.append(str(cand))

    usable: list[str] = []
    for exe in candidates:
        ok, mode = _python_can_ocr(exe)
        if ok and mode != "CPU":
            if exe != sys.executable:
                _log_worker_python(exe, mode)
            return exe
        if ok:
            usable.append(exe)
    if usable:
        # Khong co CUDA o dau — chay CPU nhung phai bao ro, dung im lang.
        _log_worker_python(usable[0], "CPU (khong thay GPU provider)")
        return usable[0]
    return sys.executable


def _log_worker_python(exe: str, mode: str) -> None:
    try:
        from pipeline.core.app_log import append_log

        append_log(f"[locate] worker python={exe} -> {mode}")
    except Exception:
        pass


def _uv_run_cmd() -> list[str] | None:
    """Trả prefix command [uv, run, --python, venv_path] để chạy worker
    trong .venv-runtime mà không phụ thuộc vào system Python đã cài.
    """
    if not getattr(sys, "frozen", False):
        # Keep RapidOCR/ONNX in a clean process: CTranslate2 Whisper may have
        # already loaded an incompatible cuDNN DLL into the server process.
        return [_dev_worker_python()]
    home = (os.environ.get("ZM_AI_TOOL_HOME") or "").strip()
    if not home:
        if sys.platform == "win32":
            home = str(Path(os.environ.get("LOCALAPPDATA", "")) / "ZM_AI_TOOL")
        else:
            home = str(Path.home() / ".local" / "share" / "ZM_AI_TOOL")
    venv = Path(home) / ".venv-runtime"
    if not venv.is_dir():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    uv_name = "uv.exe" if sys.platform == "win32" else "uv"
    uv: Path | None = None
    for candidate in filter(None, [
        exe_dir / uv_name if exe_dir else None,
        Path(meipass) / uv_name if meipass else None,
        exe_dir / "_internal" / uv_name if exe_dir else None,
    ]):
        if candidate.is_file():
            uv = candidate
            break
    if uv is None:
        uv_path = shutil.which("uv")
        uv = Path(uv_path) if uv_path else None
    if uv is None:
        return None
    return [str(uv), "run", "--no-config", "--no-python-downloads", "--python", str(venv)]


def _locate_via_runtime_subprocess(
    video: Path | str,
    segments: list[dict[str, Any]],
    *,
    only_missing: bool,
    stable: bool,
    analysis_region: Any,
    project_id: str | None = None,
    status_workers: int = 0,
) -> int | None:
    """Chạy attach_speech_hardsub_boxes trong process runtime riêng.

    Crash/native OpenCV recursion chỉ giết worker — app desktop sống.
    Trả số bbox gắn, hoặc None nếu không spawn được (gọi in-process).
    """
    uv_cmd = _uv_run_cmd()
    if uv_cmd is None:
        return None
    # Bundle onedir: _MEIPASS/pipeline/… ; dev: backend/
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    pipeline_root: Path | None = None
    for cand in [
        Path(meipass) if meipass else None,
        exe_dir / "_internal" if exe_dir else None,
        exe_dir if exe_dir else None,
        Path(__file__).resolve().parents[2],
    ]:
        if cand and (cand / "pipeline" / "ocr" / "locate.py").is_file():
            pipeline_root = cand
            break

    if pipeline_root is None:
        return None

    payload = {
        "video": str(Path(video).resolve()),
        "segments": segments,
        "only_missing": only_missing,
        "stable": stable,
        "analysis_region": analysis_region,
        # Con ghi status «Định vị OCR · x/y» trực tiếp — cha đang block chờ nên
        # không đua ghi meta.
        "project_id": project_id,
        "status_workers": int(status_workers or 0),
    }
    # Worker script file — tránh quoting -c trên Windows
    worker_src = '''# vc-locate-worker
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
raw = Path(sys.argv[2]).read_text(encoding="utf-8-sig")
data = json.loads(raw)
from pipeline.ocr.locate import attach_speech_hardsub_boxes_inprocess
n = attach_speech_hardsub_boxes_inprocess(
    data["video"],
    data["segments"],
    only_missing=bool(data.get("only_missing", True)),
    project_id=data.get("project_id"),
    stable=bool(data.get("stable", False)),
    analysis_region=data.get("analysis_region"),
    status_workers=int(data.get("status_workers") or 0),
)
Path(sys.argv[3]).write_text(
    json.dumps({"n": int(n), "segments": data["segments"]}, ensure_ascii=False),
    encoding="utf-8",
)
'''
    try:
        with tempfile.TemporaryDirectory(prefix="vc-locate-") as td:
            tdir = Path(td)
            pin = tdir / "in.json"
            pout = tdir / "out.json"
            wpy = tdir / "worker.py"
            pin.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            wpy.write_text(worker_src, encoding="utf-8")
            from pipeline.core.runtime_site import subprocess_environment

            env = subprocess_environment()
            env["PYTHONPATH"] = str(pipeline_root) + os.pathsep + env.get("PYTHONPATH", "")
            if meipass:
                env["ZM_AI_TOOL_MEIPASS"] = str(meipass)
            # Con phải dùng ĐÚNG data dir của cha (dev: backend/, frozen: home
            # launcher đã set) — fallback LOCALAPPDATA cũ làm set_status của
            # worker ghi vào public/ ma, UI không thấy «Định vị OCR · x/y».
            from pipeline.core.config import DATA, PUBLIC_DATA, SERVER_ROOT

            env["ZM_AI_TOOL_HOME"] = str(SERVER_ROOT)
            env["ZM_AI_TOOL_DATA"] = str(DATA)
            env["ZM_AI_TOOL_PUBLIC_DATA"] = str(PUBLIC_DATA)
            env.pop("ZM_AI_TOOL_DESKTOP", None)
            
            # Windows: KHÔNG dùng MSMF vì MSMF tự động bóp méo khung hình/chèn viền đen (letterboxing)
            # làm lệch tọa độ Bbox. Ép dùng FFmpeg với threads=1.
            env["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "threads;1"
            env["OPENCV_FFMPEG_MULTITHREADED"] = "0"
            if sys.platform == "win32":
                env["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"
                env["OPENCV_VIDEOIO_PRIORITY_FFMPEG"] = "100"
            cmd = [*uv_cmd, str(wpy), str(pipeline_root), str(pin), str(pout)]
            kw: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": _LOCATE_TIMEOUT_SECONDS,
                "cwd": str(pipeline_root),
                "env": env,
            }
            if sys.platform == "win32":
                kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            # Popen + register: «Huỷ» phải giết được worker (subprocess.run
            # không đăng ký nên worker OCR cứ chạy tiếp, ăn CPU sau khi huỷ).
            from pipeline.core.jobs import (
                is_cancelled,
                kill_process_tree,
                register_process,
                unregister_process,
            )

            kw.pop("capture_output", None)
            popen_kw = dict(kw)
            popen_kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            timeout_s = popen_kw.pop("timeout", 900)
            proc_h = subprocess.Popen(cmd, **popen_kw)
            register_process(project_id, proc_h)
            # Decode video (OpenCV/FFmpeg) tự lấy ~8/12 core → treo cả máy.
            # Kẹp worker vào 60% core + ưu tiên thấp: máy vẫn mượt khi OCR chạy.
            try:
                from pipeline.core.winproc import limit_process_cpu

                limit_process_cpu(proc_h, fraction=0.6)
            except Exception:
                pass
            try:
                out_s, err_s = proc_h.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc_h)
                out_s, err_s = "", "timeout"
            finally:
                unregister_process(project_id, proc_h)
            if project_id and is_cancelled(project_id):
                kill_process_tree(proc_h)
                return -1
            proc = subprocess.CompletedProcess(cmd, proc_h.returncode, out_s, err_s)
            if proc.returncode != 0 or not pout.is_file():
                err = (proc.stderr or proc.stdout or "")[-1500:]
                try:
                    from pipeline.core.app_log import append_log

                    append_log(
                        f"[locate-subprocess] fail code={proc.returncode} cmd={cmd[0]}\n{err}",
                    )
                except Exception:
                    pass
                return -1  # spawn ok nhưng lỗi — phân biệt với None (không tìm được python)
            out = json.loads(pout.read_text(encoding="utf-8"))
            segs_out = out.get("segments")
            if isinstance(segs_out, list) and len(segs_out) == len(segments):
                for dst, src in zip(segments, segs_out):
                    if not isinstance(src, dict) or not isinstance(dst, dict):
                        continue
                    for k in (
                        "bbox",
                        "bboxInherited",
                        "bboxDetected",
                        "layout",
                        "captionLayout",
                        "coverStart",
                        "coverEnd",
                        "_probeAnchored",
                    ):
                        if k in src:
                            dst[k] = src[k]
            n_res = int(out.get("n") or 0)
            try:
                from pipeline.core.app_log import append_log

                append_log(f"[locate-subprocess] ok n={n_res}")
            except Exception:
                pass
            return n_res
    except Exception as e:
        try:
            from pipeline.core.app_log import append_exception

            append_exception("[locate-subprocess] exception", e)
        except Exception:
            pass
        return -1  # exception khi spawn — phân biệt với None


_warm_worker_lock = threading.Lock()
_warm_worker: subprocess.Popen[str] | None = None
_warm_worker_temp: tempfile.TemporaryDirectory[str] | None = None
_warm_worker_key: tuple[str, ...] | None = None

_WARM_WORKER_SRC = '''# vc-locate-warm-worker
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from pipeline.ocr.locate import attach_speech_hardsub_boxes_inprocess
from pipeline.ocr.logo import detect_logo_bbox_inprocess
for line in sys.stdin:
    request = json.loads(line)
    pin, pout = Path(request["in"]), Path(request["out"])
    try:
        data = json.loads(pin.read_text(encoding="utf-8-sig"))
        if data.get("op") == "logo":
            result = {"logo": detect_logo_bbox_inprocess(
                data["video"],
                project_id=data.get("project_id"),
                segments=data.get("segments"),
            )}
        else:
            n = attach_speech_hardsub_boxes_inprocess(
                data["video"],
                data["segments"],
                only_missing=bool(data.get("only_missing", True)),
                project_id=data.get("project_id"),
                stable=bool(data.get("stable", False)),
                analysis_region=data.get("analysis_region"),
                status_workers=int(data.get("status_workers") or 0),
            )
            result = {"n": int(n), "segments": data["segments"]}
    except BaseException as exc:
        result = {"error": type(exc).__name__ + ": " + str(exc)}
    pout.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
'''


def _stop_warm_worker() -> None:
    global _warm_worker, _warm_worker_temp, _warm_worker_key
    proc, temp = _warm_worker, _warm_worker_temp
    _warm_worker = None
    _warm_worker_temp = None
    _warm_worker_key = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if temp is not None:
        try:
            temp.cleanup()
        except Exception:
            pass


atexit.register(_stop_warm_worker)


def _warm_worker_env(pipeline_root: Path, meipass: str | None) -> dict[str, str]:
    from pipeline.core.config import DATA, PUBLIC_DATA, SERVER_ROOT
    from pipeline.core.runtime_site import subprocess_environment

    env = subprocess_environment()
    env["PYTHONPATH"] = str(pipeline_root) + os.pathsep + env.get("PYTHONPATH", "")
    if meipass:
        env["ZM_AI_TOOL_MEIPASS"] = str(meipass)
    env["ZM_AI_TOOL_HOME"] = str(SERVER_ROOT)
    env["ZM_AI_TOOL_DATA"] = str(DATA)
    env["ZM_AI_TOOL_PUBLIC_DATA"] = str(PUBLIC_DATA)
    env.pop("ZM_AI_TOOL_DESKTOP", None)
    env["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "threads;1"
    env["OPENCV_FFMPEG_MULTITHREADED"] = "0"
    if sys.platform == "win32":
        env["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"
        env["OPENCV_VIDEOIO_PRIORITY_FFMPEG"] = "100"
    return env


def _get_warm_worker(
    uv_cmd: list[str], pipeline_root: Path, meipass: str | None
) -> tuple[subprocess.Popen[str], Path]:
    global _warm_worker, _warm_worker_temp, _warm_worker_key
    key = (*uv_cmd, str(pipeline_root))
    if (
        _warm_worker is not None
        and _warm_worker.poll() is None
        and _warm_worker_temp is not None
        and _warm_worker_key == key
    ):
        return _warm_worker, Path(_warm_worker_temp.name)

    _stop_warm_worker()
    temp = tempfile.TemporaryDirectory(prefix="vc-locate-warm-")
    tdir = Path(temp.name)
    script = tdir / "worker.py"
    script.write_text(_WARM_WORKER_SRC, encoding="utf-8")
    cmd = [*uv_cmd, str(script), str(pipeline_root)]
    kw: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "cwd": str(pipeline_root),
        "env": _warm_worker_env(pipeline_root, meipass),
    }
    if sys.platform == "win32":
        kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    proc = subprocess.Popen(cmd, **kw)
    try:
        from pipeline.core.winproc import limit_process_cpu

        limit_process_cpu(proc, fraction=0.6)
    except Exception:
        pass
    _warm_worker = proc
    _warm_worker_temp = temp
    _warm_worker_key = key
    return proc, tdir


def _copy_worker_segments(
    segments: list[dict[str, Any]], out: dict[str, Any]
) -> int:
    segs_out = out.get("segments")
    if isinstance(segs_out, list) and len(segs_out) == len(segments):
        for dst, src in zip(segments, segs_out):
            if not isinstance(src, dict) or not isinstance(dst, dict):
                continue
            for key in (
                "bbox",
                "bboxInherited",
                "bboxDetected",
                "layout",
                "captionLayout",
                "coverStart",
                "coverEnd",
                "_probeAnchored",
            ):
                if key in src:
                    dst[key] = src[key]
    return int(out.get("n") or 0)


# Override the one-shot implementation above. Keeping it as a fallback avoids
# widening the failure surface for unusual frozen/runtime installations.
_locate_via_runtime_subprocess_oneshot = _locate_via_runtime_subprocess


def _locate_via_runtime_subprocess(
    video: Path | str,
    segments: list[dict[str, Any]],
    *,
    only_missing: bool,
    stable: bool,
    analysis_region: Any,
    project_id: str | None = None,
    status_workers: int = 0,
) -> int | None:
    uv_cmd = _uv_run_cmd()
    if uv_cmd is None:
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    pipeline_root = next(
        (
            cand
            for cand in (
                Path(meipass) if meipass else None,
                exe_dir / "_internal" if exe_dir else None,
                exe_dir,
                Path(__file__).resolve().parents[2],
            )
            if cand and (cand / "pipeline" / "ocr" / "locate.py").is_file()
        ),
        None,
    )
    if pipeline_root is None:
        return None

    payload = {
        "video": str(Path(video).resolve()),
        "segments": segments,
        "only_missing": only_missing,
        "stable": stable,
        "analysis_region": analysis_region,
        "project_id": project_id,
        "status_workers": int(status_workers or 0),
    }
    from pipeline.core.jobs import (
        is_cancelled,
        kill_process_tree,
        register_process,
        unregister_process,
    )

    with _warm_worker_lock:
        try:
            proc, tdir = _get_warm_worker(uv_cmd, pipeline_root, meipass)
            token = uuid.uuid4().hex
            pin, pout = tdir / f"{token}.in.json", tdir / f"{token}.out.json"
            pin.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"in": str(pin), "out": str(pout)}) + "\n")
            proc.stdin.flush()
            register_process(project_id, proc)
            deadline = time.monotonic() + _LOCATE_TIMEOUT_SECONDS
            while not pout.is_file():
                if project_id and is_cancelled(project_id):
                    kill_process_tree(proc)
                    _stop_warm_worker()
                    return -1
                if proc.poll() is not None:
                    _stop_warm_worker()
                    return -1
                if time.monotonic() >= deadline:
                    kill_process_tree(proc)
                    _stop_warm_worker()
                    return -1
                time.sleep(0.05)
            out = json.loads(pout.read_text(encoding="utf-8"))
            if out.get("error"):
                raise RuntimeError(str(out["error"]))
            result = _copy_worker_segments(segments, out)
            try:
                from pipeline.core.app_log import append_log

                append_log(f"[locate-warm-worker] ok n={result}")
            except Exception:
                pass
            return result
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            _stop_warm_worker()
            try:
                from pipeline.core.app_log import append_exception

                append_exception("[locate-warm-worker] fallback one-shot", exc)
            except Exception:
                pass
            return _locate_via_runtime_subprocess_oneshot(
                video,
                segments,
                only_missing=only_missing,
                stable=stable,
                analysis_region=analysis_region,
                project_id=project_id,
                status_workers=status_workers,
            )
        finally:
            unregister_process(project_id, locals().get("proc"))
            for path_name in ("pin", "pout"):
                path = locals().get(path_name)
                if isinstance(path, Path):
                    path.unlink(missing_ok=True)


def _detect_logo_via_runtime_subprocess(
    video: Path | str,
    *,
    project_id: str | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Detect one persistent logo in the same warm OCR process."""
    uv_cmd = _uv_run_cmd()
    if uv_cmd is None:
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    pipeline_root = next(
        (
            cand
            for cand in (
                Path(meipass) if meipass else None,
                exe_dir / "_internal" if exe_dir else None,
                exe_dir,
                Path(__file__).resolve().parents[2],
            )
            if cand and (cand / "pipeline" / "ocr" / "logo.py").is_file()
        ),
        None,
    )
    if pipeline_root is None:
        return None
    from pipeline.core.jobs import (
        is_cancelled,
        kill_process_tree,
        register_process,
        unregister_process,
    )

    with _warm_worker_lock:
        try:
            proc, tdir = _get_warm_worker(uv_cmd, pipeline_root, meipass)
            token = uuid.uuid4().hex
            pin, pout = tdir / f"{token}.in.json", tdir / f"{token}.out.json"
            pin.write_text(
                json.dumps(
                    {
                        "op": "logo",
                        "video": str(Path(video).resolve()),
                        "project_id": project_id,
                        "segments": segments or [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"in": str(pin), "out": str(pout)}) + "\n")
            proc.stdin.flush()
            register_process(project_id, proc)
            deadline = time.monotonic() + 900
            while not pout.is_file():
                if project_id and is_cancelled(project_id):
                    kill_process_tree(proc)
                    _stop_warm_worker()
                    return None
                if proc.poll() is not None or time.monotonic() >= deadline:
                    kill_process_tree(proc)
                    _stop_warm_worker()
                    return None
                time.sleep(0.05)
            out = json.loads(pout.read_text(encoding="utf-8"))
            if out.get("error"):
                raise RuntimeError(str(out["error"]))
            logo = out.get("logo")
            return logo if isinstance(logo, dict) else None
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            _stop_warm_worker()
            try:
                from pipeline.core.app_log import append_exception

                append_exception("[logo-warm-worker] detection failed", exc)
            except Exception:
                pass
            return None
        finally:
            unregister_process(project_id, locals().get("proc"))
            for path_name in ("pin", "pout"):
                path = locals().get(path_name)
                if isinstance(path, Path):
                    path.unlink(missing_ok=True)
