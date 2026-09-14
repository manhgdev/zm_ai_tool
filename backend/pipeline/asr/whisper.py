"""Whisper ASR. RapidOCR sống ở pipeline.ocr.extract — re-export để tương thích."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core.jobs import Cancelled, check_cancel
from ..core.project import set_status
from ..core.resources import adaptive_workers, progress_msg
from ..ocr.extract import (  # noqa: F401 — public re-exports for run.py / burn / tests
    asr_paddleocr,
    _ocr_join_lines,
    _rapidocr_gpu_kwargs,
    _rapidocr_labels,
    prepare_cuda_dlls as _prepare_cuda_dlls,
)

# 1 model / process; reload khi đổi cpu_threads (Luồng).
_whisper = None
_whisper_lock = __import__("threading").Lock()
_whisper_threads: int = 0
_MLX_MODEL: str | None = None  # mlx-whisper model name khi dùng GPU Metal


def _mlx_model_name() -> str:
    """MLX Community — large-v3-turbo: nhanh nhất trên Apple Silicon Metal."""
    return "mlx-community/whisper-large-v3-turbo"


def _mlx_whisper_available() -> bool:
    """True khi ASR có thể dùng MLX trên GPU Apple, không nạp model."""
    if (
        sys.platform != "darwin"
        or platform.machine().lower() not in {"arm64", "aarch64"}
    ):
        return False
    try:
        import mlx_whisper  # noqa: F401  # type: ignore[import-not-found]
    except (ImportError, OSError, RuntimeError):
        return False
    return True


def _mlx_transcribe(
    wav: "Path",
    source_lang: str,
) -> "list[dict] | None":
    """Transcribe bằng mlx-whisper trên Metal GPU. Trả None nếu không khả dụng."""
    if not _mlx_whisper_available():
        return None
    try:
        import mlx_whisper  # type: ignore[import]
    except (ImportError, OSError, RuntimeError):
        return None
    lang = None if source_lang in ("", "auto") else source_lang
    model_repo = _mlx_model_name()

    def _do_transcribe(local_only: bool) -> "list[dict] | None":
        import os as _os
        # Sanitize broken macOS proxy env (NO_PROXY=::1 → httpx crash "Invalid port: ':1'")
        for _k in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY",
                   "http_proxy", "HTTPS_PROXY", "https_proxy"):
            _v = _os.environ.get(_k, "")
            if _v and any(tok.strip().startswith(":") for tok in _v.split(",")):
                _os.environ.pop(_k, None)
        if local_only:
            _os.environ["HF_HUB_OFFLINE"] = "1"
        else:
            _os.environ.pop("HF_HUB_OFFLINE", None)
        kw: dict = dict(
            path_or_hf_repo=model_repo,
            language=lang,
            word_timestamps=True,
            verbose=False,
        )
        result = mlx_whisper.transcribe(str(wav), **kw)
        rows: list[dict] = []
        for idx, seg in enumerate(result.get("segments") or []):
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            words = [
                {"word": w.get("word", ""), "start": float(w.get("start", 0)), "end": float(w.get("end", 0))}
                for w in (seg.get("words") or [])
            ]
            rows.append({
                "id": str(uuid.uuid4()),
                "index": idx,
                "start": float(seg.get("start", 0)),
                "end": float(seg.get("end", 0)),
                "source": text,
                "translation": "",
                "voice": "",
                "words": words,
            })
        return rows if rows else None

    try:
        from pipeline.core.app_log import append_log
        append_log(f"[whisper] mlx-whisper transcribe ({model_repo}) Metal GPU")
    except Exception:
        pass

    # Thử cache local trước (tránh network); rồi cho phép download nếu chưa có.
    try:
        rows = _do_transcribe(local_only=True)
        if rows is not None:
            return rows
    except Exception:
        pass
    try:
        return _do_transcribe(local_only=False)
    except Exception as exc:
        try:
            from pipeline.core.app_log import append_log
            append_log(f"[whisper] mlx-whisper failed ({exc!r}) — fallback CPU")
        except Exception:
            pass
        return None  # fallback → faster-whisper CPU


def _win_ntstatus(code: int) -> str:
    hints = {
        3221226356: " STATUS_HEAP_CORRUPTION (CUDA/cuDNN)",
        3221226505: " STATUS_STACK_BUFFER_OVERRUN (CUDA/cuDNN)",
    }
    return hints.get(int(code), "")

# Siết biên segment theo word timestamps — KHÔNG tách text (tránh 1 câu → nhiều mảnh).
_WORD_PAD_START = 0.08
_WORD_PAD_END = 0.18
_WORD_MIN_PROB = 0.01  # giữ gần hết words — tránh mất chữ Trung 1 ký tự
_MAX_SEG_DUR = 14.0  # trần nếu không có words / words lỗi
_MIN_SEG_DUR = 0.12


def _resolve_asr_workers(workers: int | None) -> int:
    return adaptive_workers(workers, kind="cpu", cap=16)


def whisper_loaded() -> bool:
    """True nếu model đã nạp trong process (không cần tải lại)."""
    return _whisper is not None


def get_whisper(workers: int = 2):
    """Load 1 lần / process — không reload khi đổi workers."""
    global _whisper, _whisper_threads
    thr = _resolve_asr_workers(workers)
    with _whisper_lock:
        if _whisper is not None:
            return _whisper

        cpu_only = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() == "-1"
        device = "cpu"
        compute = "int8"
        try:
            from pipeline.core.app_log import append_log

            append_log("[whisper] loading model…")
        except Exception:
            pass
        if not cpu_only:
            try:
                from pipeline.core.cuda_dll import prefer_torch_cudnn

                prefer_torch_cudnn()
            except Exception:
                pass
            try:
                from pipeline.core.accel import preferred_torch_device

                if preferred_torch_device() == "cuda":
                    device, compute = "cuda", "float16"
            except Exception:
                pass
            if device != "cuda":
                try:
                    _prepare_cuda_dlls()
                    import ctranslate2

                    if ctranslate2.get_cuda_device_count() > 0:
                        device, compute = "cuda", "float16"
                except (ImportError, RuntimeError, OSError):
                    pass

        try:
            from faster_whisper import WhisperModel
        except OSError:
            try:
                from pipeline.core.cuda_dll import prefer_torch_cudnn

                prefer_torch_cudnn()
            except Exception:
                pass
            from faster_whisper import WhisperModel  # noqa: F811

        from pipeline.core.config import sanitize_httpx_no_proxy

        sanitize_httpx_no_proxy()
        # CUDA: ít CPU thread + num_workers>1 (batch decode). CPU: thr theo auto.
        if device == "cuda":
            import os as _os

            cpu_threads = max(2, min(4, (_os.cpu_count() or 4) // 3))
            # 2–4 worker CTranslate2 trên GPU (không = thr CPU)
            num_workers = max(1, min(4, thr if thr > 0 else 2))
        else:
            import os as _os

            cpu_threads = thr if thr > 0 else max(1, (os.cpu_count() or 4) // 2)
            cpu_threads = max(1, min(cpu_threads, max(1, int((os.cpu_count() or 4) * 0.85))))
            if os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() == "-1":
                cpu_threads = min(4, cpu_threads)
            num_workers = 1
        _whisper = WhisperModel(
            "base",
            device=device,
            compute_type=compute,
            cpu_threads=cpu_threads,
            num_workers=num_workers,
        )
        # Gắn meta để progress hiển thị đúng
        try:
            _whisper._vc_device = device  # type: ignore[attr-defined]
            _whisper._vc_threads = thr  # type: ignore[attr-defined]
            _whisper._vc_num_workers = num_workers  # type: ignore[attr-defined]
        except Exception:
            pass
        _whisper_threads = thr
        try:
            from pipeline.core.app_log import append_log

            append_log(
                f"[whisper] loaded device={device} compute={compute} "
                f"cpu_threads={cpu_threads} workers={num_workers}"
            )
        except Exception:
            pass
        return _whisper


def warm_whisper(workers: int = 0) -> str:
    """Nạp model nền (startup), trừ MLX vốn nạp model theo lần nhận dạng."""
    if _mlx_whisper_available():
        try:
            from pipeline.core.app_log import append_log

            append_log("[whisper] MLX ready; skip Faster-Whisper CPU preload")
        except Exception:
            pass
        return "mlx-ready"
    get_whisper(workers or 2)
    return "ok"


def reset_whisper() -> None:
    """Unload Whisper after cancellation so CPU/GPU memory is returned."""
    global _whisper, _whisper_threads
    with _whisper_lock:
        model, _whisper = _whisper, None
        _whisper_threads = None
    try:
        inner = getattr(model, "model", None)
        unload = getattr(inner, "unload_model", None)
        if callable(unload):
            unload()
    except Exception:
        pass
    import gc

    gc.collect()


def _word_parts(seg: Any) -> list[tuple[float, float, str, float]]:
    """[(start, end, word, prob), ...] — bỏ token rỗng / xác suất quá thấp."""
    raw = getattr(seg, "words", None) or []
    out: list[tuple[float, float, str, float]] = []
    for w in raw:
        text = (getattr(w, "word", None) or "").strip()
        if not text:
            continue
        try:
            s = float(getattr(w, "start", 0) or 0)
            e = float(getattr(w, "end", s) or s)
            p = float(getattr(w, "probability", 1.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if e < s:
            s, e = e, s
        if p < _WORD_MIN_PROB and len(text) <= 1:
            continue
        out.append((s, e, text, p))
    return out


def _tighten_bounds(
    seg_start: float,
    seg_end: float,
    parts: list[tuple[float, float, str, float]],
) -> tuple[float, float]:
    """Chỉ siết start/end theo từ đầu–cuối; giữ nguyên 1 câu (không tách text)."""
    s0 = max(0.0, float(seg_start))
    e0 = max(s0 + _MIN_SEG_DUR, float(seg_end))
    if not parts:
        if e0 - s0 > _MAX_SEG_DUR:
            e0 = s0 + _MAX_SEG_DUR
        return s0, e0

    # Bỏ tail/head word xác suất thấp nếu kéo biên vô lý
    usable = [p for p in parts if p[3] >= _WORD_MIN_PROB or len(p[2]) > 1]
    if not usable:
        usable = parts
    ws = usable[0][0]
    we = usable[-1][1]
    start = max(0.0, ws - _WORD_PAD_START)
    end = max(start + _MIN_SEG_DUR, we + _WORD_PAD_END)
    # Không nới quá biên Whisper (trừ pad nhỏ)
    start = max(start, s0 - 0.05)
    end = min(end, e0 + 0.05)
    # Chỉ co khi words gọn hơn segment thô (cắt silence 2 đầu)
    if end - start < e0 - s0:
        return start, max(start + _MIN_SEG_DUR, end)
    if e0 - s0 > _MAX_SEG_DUR:
        return s0, s0 + _MAX_SEG_DUR
    return s0, e0


def _segments_from_whisper(seg: Any) -> list[dict[str, Any]]:
    """1 segment Whisper → 1+ segment: tách khi có khoảng im > _SPLIT_GAP giữa words."""
    text = (getattr(seg, "text", None) or "").strip()
    if not text:
        return []
    seg_start = float(getattr(seg, "start", 0) or 0)
    seg_end = float(getattr(seg, "end", seg_start) or seg_start)
    parts = _word_parts(seg)

    # Không có word timestamps → giữ nguyên 1 segment
    if not parts:
        start, end = _tighten_bounds(seg_start, seg_end, parts)
        return [
            {
                "id": str(uuid.uuid4()),
                "index": 0,
                "start": start,
                "end": end,
                "source": text,
                "translation": "",
                "voice": "",
            }
        ]

    # Tìm điểm cắt: gap giữa word[i].end → word[i+1].start > threshold
    _SPLIT_GAP = 0.7  # giây — khoảng im đủ lâu để tách câu
    groups: list[list[tuple[float, float, str, float]]] = [[]]
    for i, wp in enumerate(parts):
        groups[-1].append(wp)
        if i < len(parts) - 1:
            gap = parts[i + 1][0] - wp[1]
            if gap >= _SPLIT_GAP:
                groups.append([])

    # Mỗi group → 1 segment
    out: list[dict[str, Any]] = []
    for group in groups:
        if not group:
            continue
        # `_word_parts` trims Whisper's leading spaces, so joining without a
        # separator collapsed the app's source transcript and its translation
        # units (the web/SRT path did not use this code).
        g_text = " ".join(w[2] for w in group).strip()
        if not g_text:
            continue
        g_start = max(0.0, group[0][0] - _WORD_PAD_START)
        g_end = max(g_start + _MIN_SEG_DUR, group[-1][1] + _WORD_PAD_END)
        out.append(
            {
                "id": str(uuid.uuid4()),
                "index": 0,
                "start": g_start,
                "end": g_end,
                "source": g_text,
                "translation": "",
                "voice": "",
            }
        )
    return out if out else [
        {
            "id": str(uuid.uuid4()),
            "index": 0,
            "start": seg_start,
            "end": seg_end,
            "source": text,
            "translation": "",
            "voice": "",
        }
    ]


def asr_whisper(
    wav: Path,
    source_lang: str,
    *,
    workers: int = 2,
    project_id: str | None = None,
    on_progress: Callable[[int, float], None] | None = None,
) -> list[dict[str, Any]]:
    """Whisper CUDA. Windows/frozen: subprocess — native crash không tắt API."""
    inproc = os.environ.get("ZM_AI_TOOL_WHISPER_INPROCESS", "").strip() == "1"
    if not inproc and (getattr(sys, "frozen", False) or sys.platform == "win32"):
        if on_progress:
            on_progress(0, 0.0)
        return _asr_via_runtime_subprocess(wav, source_lang, workers=workers, project_id=project_id)
    return asr_whisper_inprocess(
        wav, source_lang, workers=workers, project_id=project_id,
        on_progress=on_progress,
    )


def _runtime_whisper_python() -> str | None:
    try:
        from pipeline.core.accel import _runtime_python

        return _runtime_python()
    except Exception:
        return None


def _asr_via_runtime_subprocess(
    wav: Path,
    source_lang: str,
    *,
    workers: int,
    project_id: str | None,
) -> list[dict[str, Any]]:
    """Chạy Whisper CUDA trong .venv-runtime. Native crash chỉ giết worker."""
    py = _runtime_whisper_python()
    if not py:
        raise RuntimeError("Thiếu .venv-runtime — không chạy Whisper CUDA")
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    pipeline_root: Path | None = None
    for cand in [
        Path(meipass) if meipass else None,
        exe_dir / "_internal" if exe_dir else None,
        exe_dir if exe_dir else None,
        Path(__file__).resolve().parents[2],
    ]:
        if cand and (cand / "pipeline" / "asr" / "whisper.py").is_file():
            pipeline_root = cand
            break
    if pipeline_root is None:
        raise RuntimeError("Không tìm thấy pipeline cho Whisper CUDA worker")
    worker_src = """# vc-whisper-worker
import json, os, sys
from pathlib import Path
os.environ.setdefault("TQDM_DISABLE", "1")
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
data = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
from pipeline.asr.whisper import asr_whisper_inprocess
rows = asr_whisper_inprocess(
    Path(data["wav"]),
    data.get("source_lang") or "auto",
    workers=int(data.get("workers") or 2),
    project_id=data.get("project_id"),
)
Path(sys.argv[3]).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
"""
    from pipeline.core.config import DATA, PUBLIC_DATA, SERVER_ROOT
    from pipeline.core.jobs import (
        is_cancelled,
        kill_process_tree,
        register_process,
        unregister_process,
    )

    def _run() -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="vc-whisper-") as td:
            tdir = Path(td)
            pin, pout, wpy = tdir / "in.json", tdir / "out.json", tdir / "worker.py"
            payload = {
                "wav": str(Path(wav).resolve()),
                "source_lang": source_lang,
                "workers": workers,
                "project_id": project_id,
            }
            pin.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            wpy.write_text(worker_src, encoding="utf-8")
            from pipeline.core.runtime_site import subprocess_environment

            env = subprocess_environment()
            env["PYTHONPATH"] = str(pipeline_root) + os.pathsep + env.get("PYTHONPATH", "")
            env["ZM_AI_TOOL_HOME"] = str(SERVER_ROOT)
            env["ZM_AI_TOOL_DATA"] = str(DATA)
            env["ZM_AI_TOOL_PUBLIC_DATA"] = str(PUBLIC_DATA)
            env.pop("ZM_AI_TOOL_DESKTOP", None)
            env["ZM_AI_TOOL_WHISPER_INPROCESS"] = "1"
            if meipass:
                env["ZM_AI_TOOL_MEIPASS"] = str(meipass)
            kw: dict[str, Any] = {
                "cwd": str(pipeline_root),
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            }
            if sys.platform == "win32":
                kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            cmd = [py, str(wpy), str(pipeline_root), str(pin), str(pout)]
            try:
                from pipeline.core.app_log import append_log

                append_log(f"[whisper] worker CUDA {py}")
            except Exception:
                pass
            proc = subprocess.Popen(cmd, **kw)
            register_process(project_id, proc)
            try:
                _out_b, err_b = proc.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc)
                raise RuntimeError("Whisper CUDA worker timeout")
            finally:
                unregister_process(project_id, proc)
            if project_id and is_cancelled(project_id):
                raise Cancelled("whisper cancelled")
            err = (err_b or b"").decode("utf-8", "replace")[-2000:]
            if proc.returncode:
                try:
                    from pipeline.core.app_log import append_log

                    append_log(f"[whisper] worker fail code={proc.returncode}\n{err}")
                except Exception:
                    pass
                raise RuntimeError(
                    f"Whisper CUDA worker exit {proc.returncode}"
                    f"{_win_ntstatus(proc.returncode)}\n{err}"
                )
            if not pout.is_file():
                raise RuntimeError("Whisper CUDA worker không ghi kết quả")
            rows = json.loads(pout.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise RuntimeError("Whisper CUDA worker trả dữ liệu lỗi")
            return rows

    return _run()


def asr_whisper_inprocess(
    wav: Path,
    source_lang: str,
    *,
    workers: int = 2,
    project_id: str | None = None,
    on_progress: Callable[[int, float], None] | None = None,
) -> list[dict[str, Any]]:
    """Whisper 1 lần cả file; siết start/end theo word timestamps."""
    import time

    thr = _resolve_asr_workers(workers)
    cached = whisper_loaded()
    mlx_ready = _mlx_whisper_available()
    if mlx_ready:
        initial_status = "Apple GPU (MLX)"
    elif not cached:
        initial_status = "tải model"
    else:
        initial_status = "nhận dạng"
    if project_id:
        set_status(
            project_id,
            step="asr",
            progress=18 if not cached else 22,
            message=progress_msg(
                "Whisper",
                workers=thr,
                extra=initial_status,
            ),
            running=True,
        )
    lang = None if source_lang in ("", "auto") else source_lang
    try:
        from pipeline.core.app_log import append_log

        append_log("[whisper] transcribe start")
    except Exception:
        pass
    # Chặn decoder lặp một token (đặc biệt tiếng Trung: "嗚嗚嗚…") rồi nuốt
    # trọn cửa sổ 30 giây.  Penalty nhẹ giữ được tiếng đệm thật, còn n-gram=3
    # buộc decoder thoát khỏi vòng lặp trước khi lời thoại phía sau bị mất.
    # Faster-Whisper/CTranslate2 không hỗ trợ MPS. Trên Apple Silicon phải thử
    # MLX trước để không nạp model CPU vô ích rồi mới nhận dạng bằng GPU.
    if mlx_ready:
        mlx_rows = _mlx_transcribe(wav, source_lang or "auto")
        if mlx_rows is not None:
            if on_progress:
                end = max((float(row.get("end") or 0) for row in mlx_rows), default=0.0)
                on_progress(len(mlx_rows), end)
            try:
                from pipeline.core.app_log import append_log
                append_log(f"[whisper] mlx done: {len(mlx_rows)} segments (Metal GPU)")
            except Exception:
                pass
            if project_id:
                set_status(
                    project_id,
                    step="asr",
                    progress=50,
                    message=progress_msg(
                        "Whisper xong",
                        len(mlx_rows),
                        workers=thr,
                        extra="Apple GPU (MLX)",
                    ),
                    running=True,
                )
            return mlx_rows
    # Fallback khi MLX không cài/không tải được model: CUDA nếu có, rồi CPU.
    model = get_whisper(thr)
    if project_id:
        device = getattr(getattr(model, "model", None), "device", "cpu")
        dev = str(getattr(model, "_vc_device", None) or device or "cpu")
        nw = int(getattr(model, "_vc_num_workers", 1) or 1)
        set_status(
            project_id,
            step="asr",
            progress=22,
            message=progress_msg(
                "Whisper",
                workers=thr,
                extra=("CUDA" if dev == "cuda" else "CPU")
                + (f" · {nw} worker" if dev == "cuda" and nw > 1 else "")
                + (" · cache" if cached else ""),
            ),
            running=True,
        )
    segments, _info = model.transcribe(
        str(wav),
        language=lang,
        # VAD OFF: video anime có nhạc nền → VAD bỏ sót câu ngắn.
        # Word-split (_SPLIT_GAP) tách segment dài thay VAD.
        vad_filter=False,
        beam_size=1,
        best_of=1,
        temperature=0.0,
        repetition_penalty=1.15,
        no_repeat_ngram_size=3,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=True,
    )
    out: list[dict[str, Any]] = []
    last_report = 0.0
    try:
        for seg in segments:
            check_cancel(project_id)
            rows = _segments_from_whisper(seg)
            if not rows:
                continue
            out.extend(rows)
            # heartbeat — Whisper hay đứng % ở 22; message đổi để UI không tưởng đơ
            if project_id:
                now = time.monotonic()
                if len(out) == 1 or now - last_report >= 1.5:
                    last_report = now
                    t_end = float(rows[-1]["end"])
                    pct = min(48, 22 + len(out))
                    set_status(
                        project_id,
                        step="asr",
                        progress=pct,
                        message=progress_msg("Whisper", len(out), workers=thr, extra=f"~{t_end:.0f}s"),
                        running=True,
                    )
                    if on_progress:
                        on_progress(len(out), t_end)
    except Cancelled:
        close = getattr(segments, "close", None)
        if callable(close):
            close()
        reset_whisper()
        raise
    # re-index + sort (sau split gap)
    out.sort(key=lambda s: (float(s.get("start") or 0), float(s.get("end") or 0)))
    for i, row in enumerate(out, start=1):
        row["index"] = i
    # Không chồng end lên start câu sau (TTS/caption sạch)
    for i in range(len(out) - 1):
        nxt = float(out[i + 1]["start"])
        cur_end = float(out[i]["end"])
        if cur_end > nxt - 0.02:
            out[i]["end"] = max(float(out[i]["start"]) + _MIN_SEG_DUR, nxt - 0.02)
    if project_id and out:
        set_status(
            project_id,
            step="asr",
            progress=50,
            message=progress_msg("Whisper xong", len(out), workers=thr),
            running=True,
        )
    if on_progress:
        on_progress(len(out), max((float(row.get("end") or 0) for row in out), default=0.0))
    return out
