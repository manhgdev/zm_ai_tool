"""FFmpeg runner for cleaner jobs."""
import subprocess
import threading
import time
import zlib
import tempfile
from pathlib import Path

import sys

from pipeline.core.media import _ff_bin, ffprobe_duration, h264_encoder_args, h264_hardware_encoder, video_size
from pipeline.core.artifact_cache import ArtifactCache
from pipeline.cleaner.cleaner_jobs import (
    update_job,
    append_job_log,
    register_proc,
    unregister_proc,
    get_job,
)

CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)) if sys.platform == "win32" else 0
_CACHE = ArtifactCache("video-cleaner", version=3)


def _inpaint_video(input_path: str, output: str, job_id: str) -> None:
    import cv2
    from pipeline.export.cover_mask import _inpaint_region
    detection = _detect_logo_with_retry(input_path, job_id) or {}
    masks = detection.get('masks') or [detection.get('bbox')]
    masks = [m for m in masks if isinstance(m, dict)]
    if not masks:
        raise RuntimeError('Không nhận diện được logo / No logo detected')
    cap = cv2.VideoCapture(input_path)
    encoder = None
    try:
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError('Cannot decode source video')
        height, width = frame.shape[:2]
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30)
        total = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        boxes = []
        for m in masks:
            x = max(0, min(width, round(float(m.get('x', 0)) * width)))
            y = max(0, min(height, round(float(m.get('y', 0)) * height)))
            right = min(width, x + round(float(m.get('w', 0)) * width))
            bottom = min(height, y + round(float(m.get('h', 0)) * height))
            if right > x and bottom > y:
                boxes.append((x, y, right, bottom))
        if not boxes:
            raise RuntimeError('Invalid logo mask')
        # File-backed stderr prevents pipe deadlock while feeding raw frames.
        with tempfile.TemporaryFile() as errors:
            encoder = subprocess.Popen([
                _ff_bin('ffmpeg'), '-y', '-loglevel', 'error', '-f', 'rawvideo',
                '-pix_fmt', 'bgr24', '-s', f'{width}x{height}', '-r', str(fps),
                '-i', '-', '-i', input_path, '-map', '0:v:0', '-map', '1:a?',
                '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                *h264_encoder_args(quality=18), '-c:a', 'copy',
                '-movflags', '+faststart', output,
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                creationflags=CREATE_NO_WINDOW)
            register_proc(job_id, encoder)
            count = 0
            while ok:
                if (get_job(job_id) or {}).get('status') == 'cancelled':
                    return
                for box in boxes:
                    frame = _inpaint_region(frame, box)
                encoder.stdin.write(frame.tobytes())
                count += 1
                if count % max(1, round(fps)) == 0:
                    update_job(job_id, {'progress': min(99, count * 100 / total)})
                ok, frame = cap.read()
            encoder.stdin.close()
            encoder.wait(timeout=60)
            if encoder.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read().decode('utf-8', 'replace')[-2000:])
    finally:
        cap.release()
        if encoder and encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        unregister_proc(job_id)

# Gemini's sparkle watermark is intentionally icon-only, so an OCR-only probe
# cannot describe it.  This small lower-right region matches that mark without
# affecting the usual caption area at the bottom of vertical videos.
_ICON_ONLY_WATERMARK_MASK = {"x": 0.80, "y": 0.89, "w": 0.08, "h": 0.06}


def _retryable_ocr_load_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return isinstance(exc, zlib.error) or (
        "decompressing data" in message and "header" in message
    )


def _detect_logo_with_retry(input_path: str, job_id: str | None = None):
    """Retry one transient frozen/runtime decompression failure during OCR load."""
    from pipeline.ocr.logo import detect_logo_bbox_inprocess

    try:
        return detect_logo_bbox_inprocess(input_path)
    except Exception as exc:
        if not _retryable_ocr_load_error(exc):
            raise
        if job_id:
            append_job_log(
                job_id,
                "OCR nạp lỗi tạm thời; đang thử lại · Temporary OCR load error; retrying",
            )
        # A partial first import must not remain cached as the shared locator.
        try:
            from pipeline.ocr import locate

            locate._locate_ocr = None
        except Exception:
            pass
        time.sleep(0.2)
        return detect_logo_bbox_inprocess(input_path)


def _logo_filter(input_path: str, job_id: str | None = None) -> str:
    """Return an FFmpeg delogo filter from OCR, with an icon-only fallback."""
    detection = _detect_logo_with_retry(input_path, job_id)
    bbox = (detection or {}).get("bbox")
    # A visible Gemini sparkle has no readable text.  Give the cleaner a
    # conservative fallback rather than failing every icon-only video before
    # FFmpeg starts.  Textual watermark detections always take priority.
    if not isinstance(bbox, dict):
        bbox = _ICON_ONLY_WATERMARK_MASK
    width, height = video_size(Path(input_path))
    if width < 1 or height < 1:
        raise RuntimeError("Không đọc được kích thước video để xoá logo")
    raw_masks = (detection or {}).get("masks")
    masks = raw_masks if isinstance(raw_masks, list) and raw_masks else [bbox]
    filters: list[str] = []
    for mask in masks:
        if not isinstance(mask, dict):
            continue
        # FFmpeg delogo rejects a region touching the top/left boundary even
        # when its dimensions are valid. Keep a one-pixel border and clip it.
        x = max(1, min(width - 3, round(float(mask.get("x") or 0) * width)))
        y = max(1, min(height - 3, round(float(mask.get("y") or 0) * height)))
        w = max(2, min(width - x - 1, round(float(mask.get("w") or 0) * width)))
        h = max(2, min(height - y - 1, round(float(mask.get("h") or 0) * height)))
        filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")
    if not filters:
        raise RuntimeError("Không có vùng logo/watermark hợp lệ để xoá")
    # FFmpeg applies each static mask sequentially; this covers a wordmark and
    # a separate corner glyph in the same video.
    return ",".join(filters)


def run_cleaner_job_sync(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    input_path = job["input_path"]
    output_path = job["output_path"]
    method = job["method"]
    options = job["options"]
    
    update_job(job_id, {"status": "processing", "startedAt": time.time(), "progress": 0})
    append_job_log(job_id, f"Bắt đầu xử lý · {method}")
    
    try:
        cache_key = _CACHE.key(
            inputs=[Path(input_path)], settings=options, values={"method": method},
        )
        if _CACHE.restore(cache_key, {"output": Path(output_path)}):
            out_size = Path(output_path).stat().st_size
            update_job(job_id, {
                "status": "done", "progress": 100.0, "outputSize": out_size,
                "finishedAt": time.time(),
            })
            append_job_log(job_id, "Hoàn thành · Cache")
            return
        duration_s = ffprobe_duration(input_path) or 100.0
        if method == 'logo':
            append_job_log(job_id, 'Tái tạo nền logo từng khung hình / Inpainting logo per frame')
            _inpaint_video(input_path, output_path, job_id)
            if (get_job(job_id) or {}).get('status') == 'cancelled':
                return
            out_size = Path(output_path).stat().st_size
            if out_size == 0:
                raise RuntimeError('Empty inpaint output')
            update_job(job_id, {'status': 'done', 'progress': 100, 'outputSize': out_size, 'finishedAt': time.time()})
            return
        
        # Always resolve the same FFmpeg binary as the rest of the app.  A
        # packaged Windows/macOS app cannot rely on a globally installed ffmpeg.
        cmd = [_ff_bin("ffmpeg"), "-y", "-hide_banner", "-i", input_path]
        
        # Build command
        if method == "metadata":
            if options.get("removeVideoMeta") or options.get("removeAudioMeta") or options.get("removeContainerMeta"):
                cmd.extend(["-map_metadata", "-1"])
            if options.get("removeChapters"):
                cmd.extend(["-map_chapters", "-1"])
            cmd.extend(["-c", "copy"])
        
        elif method == "reencode":
            vcodec = options.get("videoCodec", "libx264")
            audio_mode = str(options.get("audioMode", "copy"))
            crf = str(options.get("crf", 23))
            preset = options.get("preset", "fast")
            
            if vcodec == "copy":
                cmd.extend(["-c:v", "copy"])
            elif vcodec == "libx264" and h264_hardware_encoder():
                cmd.extend(h264_encoder_args(quality=int(crf)))
            else:
                cmd.extend(["-c:v", vcodec, "-preset", preset, "-crf", crf])
                
            audio_args = {
                "copy": ["-c:a", "copy"],
                "aac128": ["-c:a", "aac", "-b:a", "128k"],
                "aac160": ["-c:a", "aac", "-b:a", "160k"],
                "aac192": ["-c:a", "aac", "-b:a", "192k"],
                "none": ["-an"],
            }.get(audio_mode)
            if audio_args is None:
                raise ValueError(f"Chế độ âm thanh không hợp lệ: {audio_mode}")
            cmd.extend(audio_args)
            
            if options.get("faststart"):
                cmd.extend(["-movflags", "+faststart"])
            
            if options.get("removeVideoMeta") or options.get("removeAudioMeta") or options.get("removeContainerMeta"):
                cmd.extend(["-map_metadata", "-1"])
                
        elif method == "optimize":
            if h264_hardware_encoder():
                cmd.extend(h264_encoder_args(fast=True, quality=26))
            else:
                cmd.extend(["-c:v", "libx264", "-preset", "faster", "-crf", "26"])
            cmd.extend(["-movflags", "+faststart", "-c:a", "aac"])

        elif method == "logo":
            append_job_log(job_id, "Đang nhận diện logo bằng OCR · Detecting logo with OCR")
            cmd.extend(["-vf", _logo_filter(input_path, job_id)])
            append_job_log(job_id, "Đã xác định vùng xóa logo · Logo removal region ready")
            if h264_hardware_encoder():
                cmd.extend(h264_encoder_args(quality=20))
            else:
                cmd.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "20"])
            cmd.extend(["-c:a", "copy", "-map_metadata", "-1", "-movflags", "+faststart"])
            
        cmd.append(output_path)
        append_job_log(job_id, "FFmpeg: " + subprocess.list2cmdline(cmd))
        
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        register_proc(job_id, proc)
        
        stderr_tail: list[str] = []
        if proc.stderr:
            for line in proc.stderr:
                cleaned = line.strip()
                if cleaned:
                    stderr_tail.append(cleaned)
                    if len(stderr_tail) > 24:
                        stderr_tail.pop(0)
                if "time=" in line:
                    try:
                        time_str = line.split("time=")[1].split()[0]
                        parts = time_str.split(":")
                        if len(parts) == 3:
                            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
                            current_s = h * 3600 + m * 60 + s
                            progress = min(99.0, (current_s / duration_s) * 100)
                            update_job(job_id, {"progress": progress})
                    except Exception:
                        pass
        
        proc.wait()
        unregister_proc(job_id)
        
        # Check if cancelled during run
        current_job = get_job(job_id)
        if current_job and current_job.get("status") == "cancelled":
            return
            
        if proc.returncode != 0:
            detail = "\n".join(stderr_tail[-6:])
            if detail:
                append_job_log(job_id, "FFmpeg stderr:\n" + detail)
            raise RuntimeError(
                f"FFmpeg thất bại (exit {proc.returncode})"
                + (f": {detail}" if detail else "")
            )
            
        # Success
        out_size = Path(output_path).stat().st_size if Path(output_path).exists() else 0
        try:
            _CACHE.store(cache_key, {"output": Path(output_path)})
        except OSError:
            pass
        update_job(job_id, {
            "status": "done",
            "progress": 100.0,
            "outputSize": out_size,
            "finishedAt": time.time()
        })
        append_job_log(job_id, "Hoàn thành")
        
    except Exception as e:
        unregister_proc(job_id)
        try:
            from pipeline.core.app_log import append_exception

            append_exception(f"[cleaner:{job_id}:{method}] failed", e)
        except Exception:
            pass
        current_job = get_job(job_id)
        if current_job and current_job.get("status") != "cancelled":
            append_job_log(job_id, "LỖI: " + str(e))
            update_job(job_id, {
                "status": "error",
                "error": str(e),
                "finishedAt": time.time()
            })

def start_cleaner_job(job_id: str) -> None:
    threading.Thread(
        target=run_cleaner_job_sync,
        args=(job_id,),
        name=f"cleaner-{job_id}",
        daemon=True
    ).start()
