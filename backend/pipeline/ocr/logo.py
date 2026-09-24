"""Detect one persistent edge logo from a few OCR anchor frames."""
from __future__ import annotations

import math
import re
from pathlib import Path
from statistics import median
from typing import Any

from pipeline.core.jobs import check_cancel

_LOGO_DETECTION_VERSION = 2


# OCR often garbles 哔哩哔哩 → 叽咕 / 吡哩; UID watermarks are CJK + digits.
_PLATFORM_MARKS = (
    "生成",
    "veo",
    "grok",
    "kling",
    "哔哩",
    "bilibili",
    "b站",
    "抖音",
    "douyin",
    "tiktok",
    "西瓜",
    "快手",
)
_UID_WATERMARK = re.compile(r"[\u4e00-\u9fff]{2,}.{0,12}\d{3,}")


def _branding_text(text: str) -> bool:
    """@handle, AI生成, Bilibili/Douyin corner marks, CJK+UID."""
    compact = "".join(str(text or "").split()).casefold()
    if not compact:
        return False
    if compact.startswith("@"):
        return True
    if any(mark in compact for mark in _PLATFORM_MARKS):
        return True
    return bool(_UID_WATERMARK.search(compact))


def _padded_normalized_box(
    box: tuple[int, int, int, int], fw: int, fh: int
) -> dict[str, float]:
    x0, y0, x1, y1 = box
    pad_x = max(4, round((x1 - x0) * 0.15))
    pad_y = max(4, round((y1 - y0) * 0.18))
    x0, y0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    x1, y1 = min(fw, x1 + pad_x), min(fh, y1 + pad_y)
    return {
        "x": round(x0 / fw, 6),
        "y": round(y0 / fh, 6),
        "w": round((x1 - x0) / fw, 6),
        "h": round((y1 - y0) / fh, 6),
    }


def _moving_branding_tracks(
    samples: list[list[dict[str, Any]]], times: list[float], fw: int, fh: int
) -> list[dict[str, Any]]:
    """Return short, time-bound masks for watermarks that change position."""
    if not samples or len(samples) != len(times):
        return []
    detections: list[tuple[int, dict[str, Any]]] = []
    for sample_index, items in enumerate(samples):
        for item in items:
            if _branding_text(str(item.get("text") or "")):
                detections.append((sample_index, item))
    if not detections:
        return []

    def key(item: dict[str, Any]) -> str:
        text = str(item.get("text") or "").strip()
        compact = "".join(text.split()).casefold()
        if text.startswith("@"):
            return "@handle"
        if "生成" in compact:
            return "generated"
        if "哔哩" in compact or "bilibili" in compact or "b站" in compact:
            return "bilibili"
        if "抖音" in compact or "douyin" in compact:
            return "douyin"
        return compact[:16] or "brand"

    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for detection in detections:
        grouped.setdefault(key(detection[1]), []).append(detection)

    probe_gap = (times[1] - times[0]) if len(times) > 1 else 0.25
    tracks: list[dict[str, Any]] = []
    for group in grouped.values():
        group.sort(key=lambda item: item[0])
        runs: list[list[tuple[int, dict[str, Any]]]] = []
        for item in group:
            if not runs or times[item[0]] - times[runs[-1][-1][0]] > probe_gap * 2.2:
                runs.append([item])
            else:
                runs[-1].append(item)
        for run in runs:
            text0 = str(run[0][1].get("text") or "")
            # 生成/AI watermark: one OCR hit is enough. @handle still needs 2
            # to avoid a one-frame misread becoming a mask.
            if len(run) < 2 and not _branding_text(text0):
                continue
            for index, (sample_index, item) in enumerate(run):
                prev_index = run[index - 1][0] if index else sample_index
                next_index = run[index + 1][0] if index + 1 < len(run) else sample_index
                # A brand visible in the very first probe can already be on
                # frame 0.  Do not leave the initial half-probe uncovered.
                start = (
                    0.0
                    if index == 0 and sample_index == 0
                    else max(0.0, times[sample_index] - probe_gap * 0.5)
                ) if index == 0 else (times[prev_index] + times[sample_index]) * 0.5
                end = min(times[-1] + probe_gap * 0.5, times[sample_index] + probe_gap * 0.5) if index + 1 == len(run) else (times[sample_index] + times[next_index]) * 0.5
                # Cover the union between adjacent positions.  This costs a little
                # more background area, but prevents a fast TikTok watermark from
                # escaping between two otherwise correct tracking probes.
                boxes = [item["box"]]
                if index + 1 < len(run):
                    boxes.append(run[index + 1][1]["box"])
                x0 = min(box[0] for box in boxes)
                y0 = min(box[1] for box in boxes)
                x1 = max(box[2] for box in boxes)
                y1 = max(box[3] for box in boxes)
                tracks.append(
                    {
                        "start": round(start, 3),
                        "end": round(max(start + 0.04, end), 3),
                        "bbox": _padded_normalized_box((x0, y0, x1, y1), fw, fh),
                        "text": str(item.get("text") or ""),
                        "confidence": round(float(item.get("confidence") or 0.0), 4),
                    }
                )
    return tracks


def _full_clip_branding_tracks(
    samples: list[list[dict[str, Any]]],
    fw: int,
    fh: int,
    duration: float,
) -> list[dict[str, Any]]:
    """Watermark nằm im một góc → một track 0…hết clip, không cắt vụn theo probe."""
    hits = [
        item
        for dets in samples
        for item in dets
        if _branding_text(str(item.get("text") or ""))
    ]
    if not hits:
        return []
    box0 = hits[0]["box"]
    if any(not _same_logo_box(box0, item["box"], fw, fh) for item in hits[1:]):
        return []
    boxes = [item["box"] for item in hits]
    x0 = int(median(box[0] for box in boxes))
    y0 = int(median(box[1] for box in boxes))
    x1 = int(median(box[2] for box in boxes))
    y1 = int(median(box[3] for box in boxes))
    texts = [str(item.get("text") or "") for item in hits]
    text = max(texts, key=lambda value: (texts.count(value), len(value)))
    conf = sum(float(item.get("confidence") or 0) for item in hits) / len(hits)
    return [
        {
            "start": 0.0,
            "end": round(max(float(duration), 0.04), 3),
            "bbox": _padded_normalized_box((x0, y0, x1, y1), fw, fh),
            "text": text,
            "confidence": round(conf, 4),
        }
    ]


def _corner_graphic_masks(
    samples: list[list[dict[str, Any]]], fw: int, fh: int
) -> list[dict[str, float]]:
    """Find small non-text glyph watermarks that corner OCR cannot name.

    Some generators use a simple sparkle/arrow icon instead of a wordmark.
    RapidOCR commonly returns a one-character symbol for these.  Restrict this
    fallback to a small bottom-right mark so ordinary on-screen text is never
    selected merely because it is near an edge.
    """
    masks: list[dict[str, float]] = []
    for items in samples:
        for item in items:
            text = str(item.get("text") or "").strip()
            if not text or text.isalnum() or len(text) > 3:
                continue
            x0, y0, x1, y1 = item["box"]
            cx, cy = (x0 + x1) / (2 * fw), (y0 + y1) / (2 * fh)
            bw, bh = x1 - x0, y1 - y0
            if cx < 0.78 or cy < 0.78 or bw > fw * 0.16 or bh > fh * 0.16:
                continue
            padded = _padded_normalized_box((x0, y0, x1, y1), fw, fh)
            if not any(
                abs(padded["x"] - current["x"]) < 0.03
                and abs(padded["y"] - current["y"]) < 0.03
                for current in masks
            ):
                masks.append(padded)
    return masks


def _parse_logo_rows(
    result: Any,
    sample: int,
    exclude_texts: set[str] | None,
    fw: int,
    fh: int,
    ox: float = 0.0,
    oy: float = 0.0,
    scale: float = 1.0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    inv = 1.0 / max(scale, 1e-6)
    for row in result or []:
        try:
            poly, text = row[0], str(row[1] or "").strip()
            confidence = float(row[2]) if len(row) > 2 else 0.5
            xs = [ox + float(point[0]) * inv for point in poly]
            ys = [oy + float(point[1]) * inv for point in poly]
        except (IndexError, TypeError, ValueError):
            continue
        if not text:
            continue
        normalized = "".join(text.lower().split())
        if exclude_texts and (
            normalized in exclude_texts
            or any(
                len(normalized) >= 2
                and (normalized in source or source in normalized)
                for source in exclude_texts
                if len(source) >= 2
            )
        ):
            continue
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        bw, bh = x1 - x0, y1 - y0
        if bw < 4 or bh < 4:
            continue
        area = bw * bh / max(1.0, float(fw * fh))
        cx, cy = (x0 + x1) * 0.5 / fw, (y0 + y1) * 0.5 / fh
        edge = cx <= 0.28 or cx >= 0.72 or cy <= 0.20 or cy >= 0.80
        branding = _branding_text(text)
        if branding:
            if area < 0.00008 or area > 0.12:
                continue
        elif not edge or area < 0.00008 or area > 0.08 or bw > fw * 0.45 or bh > fh * 0.30:
            continue
        out.append(
            {
                "box": (int(x0), int(y0), int(x1), int(y1)),
                "text": text,
                "confidence": max(0.0, min(1.0, confidence)),
                "sample": sample,
            }
        )
    return out


def _logo_candidates(
    frame_bgr: Any,
    ocr: Any,
    sample: int,
    exclude_texts: set[str] | None = None,
) -> list[dict[str, Any]]:
    h, w = frame_bgr.shape[:2]
    result, _ = ocr(frame_bgr)
    return _parse_logo_rows(result, sample, exclude_texts, w, h)


def _logo_candidates_corners(
    frame_bgr: Any,
    ocr: Any,
    sample: int,
    exclude_texts: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Upscale four corners — static Bilibili/UID marks are too small for full-frame OCR."""
    from pipeline.core.runtime_site import ensure_cv2

    cv2 = ensure_cv2()
    h, w = frame_bgr.shape[:2]
    mh, mw = max(64, h * 18 // 100), max(96, w * 42 // 100)
    corners = (
        (0, 0, min(mw, w), min(mh, h)),
        (max(0, w - mw), 0, w, min(mh, h)),
        (0, max(0, h - mh), min(mw, w), h),
        (max(0, w - mw), max(0, h - mh), w, h),
    )
    out: list[dict[str, Any]] = []
    for ox, oy, x1, y1 in corners:
        crop = frame_bgr[oy:y1, ox:x1]
        if crop.size == 0:
            continue
        rh, rw = crop.shape[:2]
        big = cv2.resize(crop, (rw * 2, rh * 2), interpolation=cv2.INTER_LINEAR)
        result, _ = ocr(big)
        out.extend(_parse_logo_rows(result, sample, exclude_texts, w, h, ox, oy, 2.0))
    return out


def _same_logo_box(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    fw: int,
    fh: int,
) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    acx, acy = (ax0 + ax1) * 0.5, (ay0 + ay1) * 0.5
    bcx, bcy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
    aw, ah = max(1, ax1 - ax0), max(1, ay1 - ay0)
    bw, bh = max(1, bx1 - bx0), max(1, by1 - by0)
    return (
        abs(acx - bcx) <= max(fw * 0.035, max(aw, bw) * 0.45)
        and abs(acy - bcy) <= max(fh * 0.035, max(ah, bh) * 0.45)
        and max(aw, bw) / min(aw, bw) <= 1.8
        and max(ah, bh) / min(ah, bh) <= 1.8
    )


def pick_logo_detection(
    samples: list[list[dict[str, Any]]], fw: int, fh: int
) -> dict[str, Any] | None:
    """Cluster geometrically stable edge OCR boxes and return the best one."""
    clusters: list[list[dict[str, Any]]] = []
    for detections in samples:
        for item in detections:
            match = next(
                (
                    cluster
                    for cluster in clusters
                    if _same_logo_box(cluster[0]["box"], item["box"], fw, fh)
                    and all(existing["sample"] != item["sample"] for existing in cluster)
                ),
                None,
            )
            if match is None:
                clusters.append([item])
            else:
                match.append(item)

    # Ordinary text close to an edge is content, not a watermark. Only an
    # explicit platform/handle mark can be selected by the text detector;
    # icon-only marks go through the separate corner-glyph path below.
    eligible = [
        cluster
        for cluster in clusters
        if any(_branding_text(str(item.get("text") or "")) for item in cluster)
    ]
    if not eligible:
        return None

    def rank(cluster: list[dict[str, Any]]) -> tuple[float, float, float]:
        boxes = [item["box"] for item in cluster]
        cx = median((box[0] + box[2]) * 0.5 / fw for box in boxes)
        cy = median((box[1] + box[3]) * 0.5 / fh for box in boxes)
        edge_distance = min(cx, 1 - cx, cy, 1 - cy)
        confidence = sum(float(item["confidence"]) for item in cluster) / len(cluster)
        return len(cluster), confidence, -edge_distance

    best = max(eligible, key=rank)

    boxes = [item["box"] for item in best]
    x0, y0 = median(box[0] for box in boxes), median(box[1] for box in boxes)
    x1, y1 = median(box[2] for box in boxes), median(box[3] for box in boxes)
    texts = [str(item["text"]) for item in best]
    text = max(texts, key=lambda value: (texts.count(value), len(value)))
    return {
        "version": _LOGO_DETECTION_VERSION,
        "bbox": _padded_normalized_box((int(x0), int(y0), int(x1), int(y1)), fw, fh),
        "samples": len(best),
        "total": len(samples),
        "confidence": round(sum(float(item["confidence"]) for item in best) / len(best), 4),
        "text": text,
    }


def _logo_probe_times(duration: float) -> list[float]:
    """Start / mid / end — logo nằm im không cần dense probe."""
    if duration <= 0:
        return [0.0]
    a = min(0.4, max(0.05, duration * 0.04))
    c = max(a, duration - min(0.8, duration * 0.04))
    return sorted({round(t, 3) for t in (a, duration * 0.5, c)})


def detect_logo_bbox_inprocess(
    video: Path | str,
    *,
    project_id: str | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    from pipeline.core.runtime_site import ensure_cv2, prepare_cv2_import_path
    from pipeline.ocr.locate import _decode_frames_batch, rapidocr_labels

    prepare_cv2_import_path()
    cv2 = ensure_cv2()
    path = Path(video)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 25.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()
    if fw <= 0 or fh <= 0 or frames <= 0:
        return None
    duration = frames / fps
    times = _logo_probe_times(duration)
    ocr = rapidocr_labels()
    exclude_texts = {
        "".join(str(segment.get("source") or "").lower().split())
        for segment in (segments or [])
        if str(segment.get("source") or "").strip()
    }
    requested = [max(0, int(round(t * fps))) for t in times]
    time_by_frame = {frame_index: time for frame_index, time in zip(requested, times)}
    sample_by_frame: dict[int, list[dict[str, Any]]] = {}
    for frame_index, frame in (
        _decode_frames_batch(path, times, fps, fw, fh, use_cuda=False)
    ):
        check_cancel(project_id)
        hits = _logo_candidates(frame, ocr, frame_index, exclude_texts)
        hits.extend(_logo_candidates_corners(frame, ocr, frame_index, exclude_texts))
        sample_by_frame[frame_index] = hits
    ordered_frames = sorted(time_by_frame)
    times = [time_by_frame[frame_index] for frame_index in ordered_frames]
    samples = [sample_by_frame.get(frame_index, []) for frame_index in ordered_frames]
    static = pick_logo_detection(samples, fw, fh)
    graphic_masks = _corner_graphic_masks(samples, fw, fh)
    tracks = _full_clip_branding_tracks(samples, fw, fh, duration)
    if not tracks:
        tracks = _moving_branding_tracks(samples, times, fw, fh)
    if not static and not tracks and not graphic_masks:
        return None
    result: dict[str, Any] = static or {
        "version": _LOGO_DETECTION_VERSION,
        "bbox": None,
        "samples": 0,
        "total": len(samples),
        "confidence": 0.0,
        "text": "",
    }
    if tracks:
        result["tracks"] = tracks
        if not result.get("bbox"):
            result["bbox"] = tracks[0].get("bbox")
            result["text"] = str(tracks[0].get("text") or result.get("text") or "")
    if graphic_masks:
        primary = result.get("bbox")
        if not isinstance(primary, dict):
            result["bbox"] = graphic_masks[0]
            result["text"] = "corner graphic"
            primary = result["bbox"]
        masks = [primary]
        masks.extend(mask for mask in graphic_masks if mask != primary)
        result["masks"] = masks
    result["version"] = _LOGO_DETECTION_VERSION
    return result


def generate_inpaint_preview(
    video: Path | str,
    logo_detection: dict[str, Any],
    project_id: str,
) -> str | None:
    """Render an inpainted video patch covering just the watermark region.

    Produces a small MP4 (only the watermark crop, ~50-200 KB) that the editor
    preview plays in sync with the main video — giving a pixel-perfect match
    with the export ``cv2.inpaint`` result.

    Returns ``/data/<pid>/cache/inpaint_patch.mp4`` or ``None`` on failure.
    """
    bbox = logo_detection.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        import cv2
        import numpy as np
        from pipeline.core.media import video_size
        from pipeline.export.cover_mask import _inpaint_region

        video = Path(video)
        fw, fh = video_size(video)
        if fw <= 0 or fh <= 0:
            return None

        # Normalized bbox → pixel
        x = max(0.0, min(1.0, float(bbox.get("x") or 0)))
        y = max(0.0, min(1.0, float(bbox.get("y") or 0)))
        bw = max(0.0, min(1.0 - x, float(bbox.get("w") or 0)))
        bh = max(0.0, min(1.0 - y, float(bbox.get("h") or 0)))
        if bw < 0.005 or bh < 0.005:
            return None
        x0 = round(x * fw)
        y0 = round(y * fh)
        x1 = round((x + bw) * fw)
        y1 = round((y + bh) * fh)

        # Extended region with padding (same as _inpaint_region)
        pad = max(8, round(min(x1 - x0, y1 - y0) * 0.45))
        ex0, ey0 = max(0, x0 - pad), max(0, y0 - pad)
        ex1, ey1 = min(fw, x1 + pad), min(fh, y1 + pad)
        crop_w, crop_h = ex1 - ex0, ey1 - ey0
        # Ensure even dimensions for codec
        crop_w = crop_w if crop_w % 2 == 0 else crop_w + 1
        crop_h = crop_h if crop_h % 2 == 0 else crop_h + 1

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            return None
        try:
            fps_val = cap.get(cv2.CAP_PROP_FPS) or 30.0

            from pipeline.core.project import project_dir
            cache_dir = project_dir(project_id) / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            out_path = cache_dir / "inpaint_patch.mp4"
            # Feed the processed BGR frames straight to ffmpeg.  The previous
            # OpenCV mp4v -> ffmpeg H.264 double encode shifted colours and
            # created banding that became obvious when the browser overlaid
            # this small patch on the independently decoded source video.
            import subprocess
            encoder = subprocess.Popen(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "rawvideo", "-pix_fmt", "bgr24",
                    "-s", f"{crop_w}x{crop_h}", "-r", f"{fps_val:.8f}",
                    "-i", "-", "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "10",
                    "-pix_fmt", "yuv420p",
                    "-colorspace", "bt709", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-color_range", "tv",
                    "-movflags", "+faststart", str(out_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            frame_count = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                # Inpaint the watermark region
                frame = _inpaint_region(frame, (x0, y0, x1, y1))
                # Crop to the extended region
                patch = frame[ey0 : ey0 + crop_h, ex0 : ex0 + crop_w]
                # Pad if needed (edge frames)
                ph, pw = patch.shape[:2]
                if pw != crop_w or ph != crop_h:
                    canvas = np.zeros((crop_h, crop_w, 3), dtype=np.uint8)
                    canvas[: min(ph, crop_h), : min(pw, crop_w)] = patch[
                        : min(ph, crop_h), : min(pw, crop_w)
                    ]
                    patch = canvas
                if encoder.stdin is None:
                    break
                encoder.stdin.write(patch.tobytes())
                frame_count += 1

            if encoder.stdin is not None:
                encoder.stdin.close()
            encoder.wait(timeout=30)

            if frame_count < 1 or encoder.returncode != 0:
                out_path.unlink(missing_ok=True)
                return None

            if not out_path.exists() or out_path.stat().st_size < 100:
                return None

            # Save placement metadata
            import json
            meta_path = cache_dir / "inpaint_patch.json"
            meta_path.write_text(
                json.dumps({
                    "x": ex0, "y": ey0, "w": crop_w, "h": crop_h,
                    "origX": x0, "origY": y0,
                    "origW": x1 - x0, "origH": y1 - y0,
                }),
                encoding="utf-8",
            )

            # URL version prevents Chromium from retaining an older, differently
            # encoded patch after the project is analysed again.
            return f"/data/{project_id}/cache/inpaint_patch.mp4?v={out_path.stat().st_mtime_ns}"
        finally:
            cap.release()
    except Exception:
        return None


__all__ = ["detect_logo_bbox_inprocess", "pick_logo_detection", "generate_inpaint_preview"]
