"""FFmpeg runner for cleaner jobs."""
import subprocess
import threading
import time
import zlib
import tempfile
from pathlib import Path

import sys

from pipeline.core.media import _ff_bin, ffprobe_duration, h264_encoder_args, h264_hardware_encoder
from pipeline.core.artifact_cache import ArtifactCache
from pipeline.cleaner.cleaner_jobs import (
    update_job,
    append_job_log,
    register_proc,
    unregister_proc,
    get_job,
)

CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)) if sys.platform == "win32" else 0
_CACHE = ArtifactCache("video-cleaner", version=6)


def _pixel_masks(detection: dict, width: int, height: int) -> list[tuple[int, int, int, int]]:
    raw_masks = detection.get("masks") or [detection.get("bbox")]
    boxes: list[tuple[int, int, int, int]] = []
    for mask in raw_masks:
        if not isinstance(mask, dict):
            continue
        x = max(0, min(width - 1, round(float(mask.get("x") or 0) * width)))
        y = max(0, min(height - 1, round(float(mask.get("y") or 0) * height)))
        right = min(width, x + round(float(mask.get("w") or 0) * width))
        bottom = min(height, y + round(float(mask.get("h") or 0) * height))
        if right > x and bottom > y:
            boxes.append((x, y, right, bottom))
    return boxes


def _inpaint_image(input_path: str, output: str, job_id: str) -> None:
    """Process still images locally; never route an image through video code."""
    from pipeline.core.runtime_site import ensure_cv2
    from pipeline.cleaner.sparkle import find_sparkle, remove_sparkle
    from pipeline.export.cover_mask import _inpaint_region
    import numpy as np

    cv2 = ensure_cv2()
    # imdecode/fromfile supports Windows paths containing non-ASCII characters.
    image = cv2.imdecode(np.fromfile(input_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError("Không đọc được ảnh đầu vào / Cannot decode input image")
    if image.dtype != np.uint8:
        raise RuntimeError('CLEANER_IMAGE_DEPTH_UNSUPPORTED')
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    overlay = find_sparkle(image[:, :, :3])
    if overlay:
        remove_sparkle(image, overlay)
        append_job_log(job_id, f"Local alpha recovery: box={overlay.box}, opacity={overlay.opacity:.3f}, match={overlay.score:.3f}")
    else:
        detection = _detect_logo_with_retry(input_path, job_id) or {}
        boxes = _pixel_masks(detection, image.shape[1], image.shape[0])
        if not boxes:
            # Absence of a detection must never authorize erasing an arbitrary
            # rectangle of scene content or returning a fake cleaned result.
            raise RuntimeError('CLEANER_LOGO_NOT_DETECTED')
        for box in boxes:
            rgb = image[:, :, :3].copy()
            image[:, :, :3] = _inpaint_region(rgb, box)
    ext = Path(output).suffix.lower()
    params = [int(cv2.IMWRITE_JPEG_QUALITY), 98] if ext in {".jpg", ".jpeg"} else []
    if ext == '.webp':
        params = [int(cv2.IMWRITE_WEBP_QUALITY), 101]  # lossless
    ok, encoded = cv2.imencode(ext, image, params)
    if not ok:
        raise RuntimeError("Không thể ghi ảnh đã xử lý / Cannot write cleaned image")
    encoded.tofile(output)


def _inpaint_video(input_path: str, output: str, job_id: str) -> None:
    from pipeline.core.runtime_site import ensure_cv2
    from pipeline.cleaner.sparkle import find_sparkle, remove_sparkle
    from pipeline.export.cover_mask import _inpaint_region
    cv2 = ensure_cv2()
    cap = cv2.VideoCapture(input_path)
    encoder = None
    try:
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError('Cannot decode source video')
        height, width = frame.shape[:2]
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30)
        total = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        # A video overlay must be consistent across anchor frames before the
        # same alpha map is applied. This path supports static corner marks.
        overlay = find_sparkle(frame)
        if overlay:
            for position in sorted({total // 2, max(0, total - 2)}):
                cap.set(cv2.CAP_PROP_POS_FRAMES, position)
                decoded, anchor = cap.read()
                other = find_sparkle(anchor) if decoded else None
                if other is None or max(abs(a - b) for a, b in zip(overlay.box, other.box)) > 3 or abs(overlay.opacity - other.opacity) > .08:
                    overlay = None
                    break
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError('Cannot decode source video')
        boxes = []
        if overlay:
            append_job_log(job_id, f"Local alpha recovery (static video): box={overlay.box}, opacity={overlay.opacity:.3f}")
        else:
            detection = _detect_logo_with_retry(input_path, job_id) or {}
            boxes = _pixel_masks(detection, width, height)
            if not boxes:
                raise RuntimeError('CLEANER_LOGO_NOT_DETECTED')
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
                if overlay:
                    remove_sparkle(frame, overlay)
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


def run_cleaner_job_sync(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    input_path = job["input_path"]
    output_path = job["output_path"]
    method = job["method"]
    options = job["options"]
    is_image = Path(input_path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    
    update_job(job_id, {"status": "processing", "startedAt": time.time(), "progress": 0})
    append_job_log(job_id, f"Bắt đầu xử lý · {method}")
    
    try:
        cache_key = _CACHE.key(
            inputs=[Path(input_path)], settings=options, values={"method": method},
        )
        if method != 'logo' and _CACHE.restore(cache_key, {"output": Path(output_path)}):
            out_size = Path(output_path).stat().st_size
            update_job(job_id, {
                "status": "done", "progress": 100.0, "outputSize": out_size,
                "finishedAt": time.time(),
            })
            append_job_log(job_id, "Hoàn thành · Cache")
            return
        duration_s = ffprobe_duration(input_path) or (1.0 if is_image else 100.0)
        if method == 'logo':
            if is_image:
                append_job_log(job_id, 'Xử lý ảnh bằng OpenCV local / Processing image with local OpenCV')
                _inpaint_image(input_path, output_path, job_id)
            else:
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
