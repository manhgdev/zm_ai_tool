"""Paddle/RapidOCR hardsub extract — api."""
from __future__ import annotations

"""RapidOCR extract — hardsub đáy + mid/vertical/labels.

Tách khỏi asr.py (Whisper) và đường dịch/phụ đề burn layout.
Không sửa logic — chỉ di chuyển.
"""

import os
import re
import shutil
import subprocess
import uuid
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any

from pipeline.core.jobs import check_cancel, run_cmd
from pipeline.core.project import cache_frames, set_status
from pipeline.core.resources import adaptive_workers

# giới hạn tổng luồng OCR phụ — tránh 100% CPU (để UI/OS ~5–10%)
_ocr_sem: threading.Semaphore | None = None
_ocr_sem_n: int = 0


import json
import tempfile

from .runtime import *  # noqa: F403
from .textutil import *  # noqa: F403
from .merge import *  # noqa: F403
from .scan import *  # noqa: F403


def _asr_paddleocr_via_runtime_subprocess(
    video: Path,
    project_id: str | None = None,
    *,
    reuse_frames: bool = False,
    tag: str = "full",
    workers: int = 2,
    source_lang: str = "auto",
    analysis_region: Any = None,
    stable: bool = False,
) -> list[dict[str, Any]] | None:
    from pipeline.ocr.locate import _uv_run_cmd

    uv_cmd = _uv_run_cmd()
    if uv_cmd is None:
        try:
            from pipeline.core.app_log import append_log

            append_log("[ocr-subprocess] _uv_run_cmd is None")
        except Exception:
            pass
        return None

    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    pipeline_root: Path | None = None
    for cand in [
        Path(meipass) if meipass else None,
        exe_dir / "_internal" if exe_dir else None,
        exe_dir if exe_dir else None,
        Path(__file__).resolve().parents[3],
    ]:
        if cand and (cand / "pipeline" / "ocr" / "extract_parts" / "api.py").is_file():
            pipeline_root = cand
            break

    if pipeline_root is None:
        try:
            from pipeline.core.app_log import append_log

            append_log(f"[ocr-subprocess] pipeline_root not found meipass={meipass} exe={exe_dir}")
        except Exception:
            pass
        return None

    payload = {
        "video": str(Path(video).resolve()),
        "project_id": project_id,
        "reuse_frames": reuse_frames,
        "tag": tag,
        "workers": workers,
        "source_lang": source_lang,
        "analysis_region": analysis_region,
        "stable": stable,
    }

    worker_src = """# vc-ocr-worker
import json, sys, os
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
raw = Path(sys.argv[2]).read_text(encoding="utf-8-sig")
data = json.loads(raw)
from pipeline.ocr.extract_parts.api import asr_paddleocr_inprocess
segs = asr_paddleocr_inprocess(
    Path(data["video"]),
    data.get("project_id"),
    reuse_frames=bool(data.get("reuse_frames", False)),
    tag=str(data.get("tag", "full")),
    workers=int(data.get("workers", 2)),
    source_lang=str(data.get("source_lang", "auto")),
    analysis_region=data.get("analysis_region"),
    stable=bool(data.get("stable", False)),
)
Path(sys.argv[3]).write_text(
    json.dumps({"segments": segs}, ensure_ascii=False),
    encoding="utf-8",
)
"""
    try:
        with tempfile.TemporaryDirectory(prefix="vc-ocr-") as td:
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
            if not env.get("ZM_AI_TOOL_HOME"):
                if sys.platform == "win32":
                    env["ZM_AI_TOOL_HOME"] = str(Path(os.environ.get("LOCALAPPDATA", "")) / "ZM_AI_TOOL")
                else:
                    env["ZM_AI_TOOL_HOME"] = str(Path.home() / ".local" / "share" / "ZM_AI_TOOL")
            env.pop("ZM_AI_TOOL_DESKTOP", None)
            # (libavcodec/pthread_frame.c:173). Cả hai env var để cover các version OpenCV khác nhau.
            env["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "threads;1"
            env["OPENCV_FFMPEG_MULTITHREADED"] = "0"
            # Windows: KHÔNG dùng MSMF vì MSMF tự động bóp méo khung hình/chèn viền đen (letterboxing)
            # làm lệch tọa độ Bbox. Ép dùng FFmpeg với threads=1.
            if sys.platform == "win32":
                env["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"
                env["OPENCV_VIDEOIO_PRIORITY_FFMPEG"] = "100"
            cmd = [*uv_cmd, str(wpy), str(pipeline_root), str(pin), str(pout)]
            kw: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 1800,
                "cwd": str(pipeline_root),
                "env": env,
            }
            if sys.platform == "win32":
                kw["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            proc = subprocess.run(cmd, **kw)
            if proc.returncode != 0 or not pout.is_file():
                err = (proc.stderr or proc.stdout or "")[-1500:]
                try:
                    from pipeline.core.app_log import append_log

                    append_log(f"[ocr-subprocess] fail code={proc.returncode}\n{err}")
                except Exception:
                    pass
                return None
            out = json.loads(pout.read_text(encoding="utf-8"))
            segs = out.get("segments")
            if isinstance(segs, list):
                try:
                    from pipeline.core.app_log import append_log

                    append_log(f"[ocr-subprocess] ok segs={len(segs)}")
                except Exception:
                    pass
                return segs
    except Exception as e:
        try:
            from pipeline.core.app_log import append_exception

            append_exception("[ocr-subprocess] exception", e)
        except Exception:
            pass
    return None


def asr_paddleocr(
    video: Path,
    project_id: str | None = None,
    *,
    reuse_frames: bool = False,
    tag: str = "full",
    workers: int = 2,
    source_lang: str = "auto",
    analysis_region: Any = None,
    stable: bool = False,
) -> list[dict[str, Any]]:
    """OCR hardsubs on screen (RapidOCR). Nhiều khung song song theo `workers`."""
    if getattr(sys, "frozen", False) or sys.platform == "win32":
        res = _asr_paddleocr_via_runtime_subprocess(
            video,
            project_id,
            reuse_frames=reuse_frames,
            tag=tag,
            workers=workers,
            source_lang=source_lang,
            analysis_region=analysis_region,
            stable=stable,
        )
        if res is not None:
            return res
        raise RuntimeError("OCR CUDA worker không chạy được — API không OCR in-process")
    return asr_paddleocr_inprocess(
        video,
        project_id,
        reuse_frames=reuse_frames,
        tag=tag,
        workers=workers,
        source_lang=source_lang,
        analysis_region=analysis_region,
        stable=stable,
    )


def asr_paddleocr_inprocess(
    video: Path,
    project_id: str | None = None,
    *,
    reuse_frames: bool = False,
    tag: str = "full",
    workers: int = 2,
    source_lang: str = "auto",
    analysis_region: Any = None,
    stable: bool = False,
) -> list[dict[str, Any]]:
    """OCR hardsubs on screen (RapidOCR). Nhiều khung song song theo `workers`."""
    try:
        from pipeline.ocr.extract_parts.runtime import prepare_cuda_dlls
        prepare_cuda_dlls()
        try:
            from pipeline.core.runtime_site import ensure_cv2
            ensure_cv2()
        except Exception:
            pass
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "OCR chưa cài. pip install rapidocr-onnxruntime — hoặc dùng Faster-Whisper."
            f" (detail: {e})"
        ) from e

    pid = project_id or video.parent.name
    # A track OCR run must never share its extraction directory with another
    # in-flight run for the same project. Concurrent requests previously
    # removed frames while RapidOCR was reading them.
    frame_tag = tag if reuse_frames else f"{tag}-{uuid.uuid4().hex[:10]}"
    frames = cache_frames(pid, frame_tag)
    # crop_v4: hardsub đáy (ổn định ~99%) — tiêu đề dọc = pass riêng
    crop_mark = frames / ".crop_v5"
    fps_mark = frames / ".fps"
    need_extract = (
        not reuse_frames
        or not any(frames.glob("*.jpg"))
        or not crop_mark.exists()
    )
    # Độ dài ước lượng để chọn fps (video vài tiếng không quét 2fps)
    dur_hint = 0.0
    try:
        from pipeline.core.media import ffprobe_duration

        dur_hint = float(ffprobe_duration(video) or 0.0)
    except Exception:
        dur_hint = 0.0
    from pipeline.ocr.overlay_scan import adaptive_bottom_fps

    _fvh, _fvw = 0, 0
    if need_extract:
        fps = adaptive_bottom_fps(dur_hint if dur_hint > 0 else 120.0)
        if frames.exists():
            shutil.rmtree(frames)
        frames.mkdir(parents=True)

        w = h = 0
        try:
            probe = subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "csv=p=0:s=x",
                    str(video),
                ],
                text=True,
            ).strip()
            w, h = (int(x) for x in probe.split("x"))
        except (subprocess.SubprocessError, ValueError):
            pass
        _fvh = h  # chiều cao frame; w bị overwrite bởi _ocr_pool_workers bên dưới
        _fvw = w

        # A saved rectangle is only configuration state.  Crop extraction only
        # when the caller explicitly enables the constrained-locate mode.
        if stable and analysis_region and isinstance(analysis_region, dict):
            rx = max(0.0, min(1.0, float(analysis_region.get("x", 0.0))))
            ry = max(0.0, min(1.0, float(analysis_region.get("y", 0.0))))
            rw = max(0.05, min(1.0, float(analysis_region.get("w", 1.0))))
            rh = max(0.05, min(1.0, float(analysis_region.get("h", 1.0))))
            vf = f"fps={fps:g},crop=iw*{rw:g}:ih*{rh:g}:iw*{rx:g}:ih*{ry:g},scale=iw*2:ih*2"
        else:
            # Quét Full Khung (Full ROI) 1 LẦN DUY NHẤT — không cắt xén, đọc toàn bộ chữ.
            # Không dùng scale=iw*2:ih*2 vì full frame 2x quá to và chậm.
            vf = f"fps={fps:g}"
        run_cmd(
            project_id,
            ["ffmpeg", "-y", "-i", str(video), "-vf", vf, str(frames / "%06d.jpg")],
        )
        crop_mark.write_text("v4\n", encoding="utf-8")
        fps_mark.write_text(f"{fps:g}\n", encoding="utf-8")
    else:
        try:
            fps = float((fps_mark.read_text(encoding="utf-8") or "2").strip() or 2)
        except (OSError, ValueError):
            fps = 2.0
        if fps <= 0:
            fps = 2.0
    jpgs = sorted(frames.glob("*.jpg"))
    if jpgs and _fvh == 0:
        import cv2
        _img = cv2.imread(str(jpgs[0]))
        if _img is not None:
            _fvh, _fvw = _img.shape[:2]

    total = max(1, len(jpgs))
    n = len(jpgs)
    w_req = int(workers or 0)
    gpu_kwargs = _rapidocr_gpu_kwargs()
    gpu_ocr = bool(gpu_kwargs["det_use_cuda"] or gpu_kwargs["det_use_dml"])
    from pipeline.core.resources import pack_gpu_workers

    if gpu_ocr:
        gpu_cap = pack_gpu_workers(per_job_mb=450, reserve_mb=350, hard_max=20)
    else:
        gpu_cap = min(16, _cpu_budget(0.92))
    w = _ocr_pool_workers(w_req, cap=gpu_cap, gpu=gpu_ocr)
    w = max(1, min(w, n if n else 1))
    _limit_onnx_threads()

    # Mỗi worker 1 engine RapidOCR (ONNX không share session an toàn giữa thread).
    # Lỏng hơn default: 1 chữ CJK (行) không bị min_height=30 bỏ sót.
    _tls = threading.local()
    _engine_init_lock = threading.Lock()

    def _engine() -> Any:
        eng = getattr(_tls, "ocr", None)
        if eng is None:
            with _engine_init_lock:
                eng = getattr(_tls, "ocr", None)
                if eng is None:
                    try:
                        eng = _rapidocr_labels()
                    except Exception as exc:
                        try:
                            from pipeline.core.app_log import append_exception

                            append_exception("[ocr-gpu-init] RapidOCR GPU init failed, fallback kwargs", exc)
                        except Exception:
                            pass
                        from rapidocr_onnxruntime import RapidOCR  # type: ignore

                        eng = RapidOCR(**_rapidocr_gpu_kwargs())
                    _tls.ocr = eng
        return eng

    timed_bottom: list[tuple[float, str]] = [(-1.0, "")] * n
    timed_mid: list[tuple[float, str]] = [(-1.0, "")] * n
    timed_vert: list[tuple[float, str]] = [(-1.0, "")] * n
    done = 0
    done_lock = threading.Lock()
    sem = _ocr_semaphore()

    def _ocr_one(i: int, img: Path) -> tuple[int, dict[str, str]]:
        check_cancel(project_id)
        with sem:
            try:
                result, _ = _engine()(str(img))
            except Exception:
                _tls.ocr = _rapidocr_labels(use_cuda=False)
                result, _ = _tls.ocr(str(img))
        bottom_lines, mid_lines, vert_lines = [], [], []
        for row in result or []:
            box = row[0] if row else None
            text = str(row[1] or "").strip()
            if not text:
                continue
            confidence = float(row[2]) if len(row) > 2 else 1.0
            if confidence < 0.5:
                continue
            if not _hardsub_line_keep(text, source_lang):
                continue
            
            is_vert = False
            is_mid = False
            if _fvh > 0 and box:
                if stable and analysis_region and isinstance(analysis_region, dict):
                    bottom_lines.append(text)
                    continue

                ys = [float(p[1]) for p in box]
                xs = [float(p[0]) for p in box]
                ncy = (min(ys) + max(ys)) / 2.0 / _fvh
                bh = max(ys) - min(ys)
                bw = max(xs) - min(xs)
                is_vert = (bw > 0 and bh > bw * 1.5)
                
                portrait = _fvh > _fvw > 0 if _fvw > 0 else False
                band = 0.18 if portrait else 0.22
                y0 = 1.0 - band
                if not is_vert:
                    if ncy < y0:
                        is_mid = True
            
            if is_vert:
                vert_lines.append(text)
            elif is_mid:
                mid_lines.append(text)
            else:
                bottom_lines.append(text)
                
        return i, {
            "bottom": _ocr_join_lines(bottom_lines),
            "mid": _ocr_join_lines(mid_lines),
            "vert": _ocr_join_lines(vert_lines)
        }

    from pipeline.core.resources import progress_msg, run_with_adaptive_workers

    def _ocr_job(item: tuple[int, Path]) -> tuple[int, dict[str, str]]:
        return _ocr_one(item[0], item[1])

    def _ocr_prog(cur: int, tot: int, w_now: int) -> None:
        if not project_id:
            return
        if cur % max(1, w_now) != 0 and cur != tot:
            return
        pct = 15 + int(22 * cur / max(1, tot))
        set_status(
            project_id,
            step="asr",
            progress=pct,
            message=progress_msg("OCR", cur, tot, workers=w_now),
            running=True,
        )

    if project_id:
        set_status(
            project_id,
            step="asr",
            progress=15,
            message=progress_msg("OCR", 0, total, workers=w),
            running=True,
        )
    rows = run_with_adaptive_workers(
        list(enumerate(jpgs)),
        _ocr_job,
        kind="gpu" if gpu_ocr else "cpu",
        requested=w_req if w_req > 0 else None,
        cap=max(w, gpu_cap if gpu_ocr else min(16, _cpu_budget(0.92))),
        thread_name_prefix="ocr-asr",
        on_progress=_ocr_prog,
        cancel_check=lambda: check_cancel(project_id),
    )
    for i, pair in enumerate(rows):
        if not pair:
            continue
        ii, texts = pair
        t = float(ii) / fps
        timed_bottom[ii] = (t, texts["bottom"])
        timed_mid[ii] = (t, texts["mid"])
        timed_vert[ii] = (t, texts["vert"])

    video_end = (len(jpgs) / fps) if jpgs else 0.0
    segs = _ocr_segments_from_timeline(timed_bottom, video_end) if any(t for _, t in timed_bottom) else []
    mid_segs = _ocr_segments_from_timeline(timed_mid, video_end) if any(t for _, t in timed_mid) else []
    vert_segs = _ocr_segments_from_timeline(timed_vert, video_end) if any(t for _, t in timed_vert) else []

    if mid_segs:
        for m in mid_segs:
            m["layout"] = "mid"
        segs.extend(mid_segs)
    
    if vert_segs:
        for v in vert_segs:
            v["layout"] = "vertical"
        segs.extend(vert_segs)

    segs = sorted(segs, key=lambda x: x["start"])
    # Must happen before speech-bbox location.  Location deliberately searches
    # subtitle bands and would otherwise move an edge watermark into a caption.
    segs = _drop_non_caption_branding(segs)

    # RapidOCR hay nhầm 免/兔… — sửa trên chữ nguồn trước khi dịch ngôn ngữ
    looks_zh = sum(1 for s in segs if any(_is_cjk(c) for c in s["source"])) >= max(
        1, len(segs) // 2
    )
    if looks_zh:
        fixed = _ocr_fix_zh([s["source"] for s in segs], project_id=project_id)
        for seg, src in zip(segs, fixed):
            # Vertical = watermark cột: giữ nguyên (đừng strip 花木紫 rồi còn rác 工).
            seg["source"] = src
        # fix_zh sau merge → label mảnh (花水業→花木紫) trùng watermark dọc;
        # gộp vào vertical kẻo burn ẩn chữ dọc khi has_label.
        segs = _fold_duplicate_watermark_labels(segs)
        segs = _fold_vertical_column_flickers(segs)
        segs = _drop_mid_in_watermark_column(segs)
    if segs:
        try:
            from pipeline.ocr.locate import attach_speech_hardsub_boxes_inprocess

            attach_speech_hardsub_boxes_inprocess(
                video,
                segs,
                only_missing=False,
                project_id=project_id,
                stable=stable,
                analysis_region=analysis_region,
            )
        except Exception:
            pass
    return segs
