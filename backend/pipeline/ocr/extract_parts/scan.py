"""Paddle/RapidOCR hardsub extract — scan."""
from __future__ import annotations

"""RapidOCR extract — hardsub đáy + mid/vertical/labels.

Tách khỏi asr.py (Whisper) và đường dịch/phụ đề burn layout.
Không sửa logic — chỉ di chuyển.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any

from pipeline.core.jobs import check_cancel, run_cmd
from pipeline.core.project import cache_frames, set_status
from pipeline.core.resources import adaptive_workers
from pipeline.ocr.overlay_cover import mid_bottom_cutoff

# giới hạn tổng luồng OCR phụ — tránh 100% CPU (để UI/OS ~5–10%)
_ocr_sem: threading.Semaphore | None = None
_ocr_sem_n: int = 0


from .runtime import (
    _cpu_budget,
    _ocr_pool_workers,
    _ocr_semaphore,
    _rapidocr_gpu_kwargs,
    _rapidocr_labels,
)
from .textutil import *  # noqa: F403
from .merge import *  # noqa: F403

def _ocr_vertical_titles(
    video: Path,
    *,
    project_id: str | None,
    video_end: float,
) -> list[dict[str, Any]]:
    """OCR tiêu đề dọc — đo start/end theo ms (bám khung thật, không hardcode 1.35s)."""
    import cv2

    try:
        ocr = _rapidocr_labels()
    except ImportError:
        return []

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    try:
        vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1080)
        vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1920)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
        # coarse 100ms đầu/cuối; refine 40ms chỉ quanh hit
        step_ms = 200
        windows: list[tuple[int, int]] = [(0, min(int(video_end * 1000), 5000))]
        if video_end > 8.0:
            end0 = max(0, int(video_end * 1000) - 2500)
            end1 = int(video_end * 1000)
            if end0 > windows[0][1] + 500:
                windows.append((end0, end1))

        hits: list[tuple[float, str]] = []
        for w0, w1 in windows:
            t_ms = w0
            while t_ms <= w1:
                check_cancel(project_id)
                with _ocr_semaphore():
                    cap.set(cv2.CAP_PROP_POS_MSEC, float(t_ms))
                    ok, frame = cap.read()
                    if not ok:
                        break
                    text = _ocr_vertical_from_frame(frame, ocr, vw, vh)
                if text:
                    hits.append((t_ms / 1000.0, text))
                t_ms += step_ms
        # refine mép cụm (±120ms, step 40ms) — không full 25fps
        if hits:
            refine_ms = 40
            extra: list[tuple[float, str]] = []
            for t0, tx0 in hits:
                for d in (-120, -80, -40, 40, 80, 120):
                    t_ms = int(round(t0 * 1000 + d))
                    if t_ms < 0 or t_ms > int(video_end * 1000):
                        continue
                    cap.set(cv2.CAP_PROP_POS_MSEC, float(t_ms))
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    text = _ocr_vertical_from_frame(frame, ocr, vw, vh)
                    if text and (_ocr_same(text, tx0) or _ocr_sim(text, tx0) >= 0.72):
                        extra.append((t_ms / 1000.0, text))
            hits.extend(extra)
            hits.sort(key=lambda x: x[0])
    finally:
        cap.release()

    if not hits:
        return []

    # Gom cụm liên tiếp (cùng chữ, gap ≤ 220ms) → 1 segment / cụm
    segs: list[dict[str, Any]] = []
    i = 0
    while i < len(hits):
        t0, tx0 = hits[i]
        window = [tx0]
        j = i + 1
        while j < len(hits):
            t1, tx1 = hits[j]
            if t1 - hits[j - 1][0] > 0.22:
                break
            if _ocr_same(tx0, tx1) or _ocr_same(window[-1], tx1):
                window.append(tx1)
                tx0 = _ocr_pick_best(window)
                j += 1
                continue
            break
        best = _ocr_pick_best(window)
        if best and sum(1 for c in best if _is_cjk(c)) >= 2:
            # end = last hit + 1 frame (ms); không pad 1.35s
            t_start = hits[i][0]
            t_end = hits[j - 1][0] + step_ms / 1000.0
            t_end = min(video_end, max(t_end, t_start + step_ms / 1000.0))
            segs.append(
                _ocr_seg(len(segs) + 1, t_start, t_end, best, layout="vertical")
            )
        i = j
    return segs


def _ocr_vertical_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> str:
    text, _bbox = _ocr_vertical_item_from_frame(frame_bgr, ocr, vw, vh)
    return text


def _ocr_vertical_item_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> tuple[str, dict[str, int] | None]:
    """Title dọc CJK + bbox cột sát ink."""
    import cv2

    y0, y1 = int(vh * 0.06), int(vh * 0.78)
    x0, x1 = int(vw * 0.02), int(vw * 0.98)
    roi = frame_bgr[y0:y1, x0:x1]
    if roi.size == 0:
        return "", None
    scale = 1.8
    img = cv2.resize(roi, (int(roi.shape[1] * scale), int(roi.shape[0] * scale)))
    result, _ = ocr(img)
    cands: list[tuple[float, str, float, float, float, float]] = []
    for row in result or []:
        try:
            box, text = row[0], str(row[1] or "").strip()
        except (IndexError, TypeError):
            continue
        if not text:
            continue
        cjk = sum(1 for c in text if _is_cjk(c))
        if cjk < 2:
            continue
        if cjk < len(text) * 0.7:
            continue
        bw, bh = _ocr_box_wh(box)
        if bh < 8 or bw < 2:
            continue
        tall = bh > bw * 1.15
        short_stack = cjk <= 8 and bh >= bw * 0.85
        if not (tall or short_stack):
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            bx0 = min(xs) / scale + x0
            by0 = min(ys) / scale + y0
            bx1 = max(xs) / scale + x0
            by1 = max(ys) / scale + y0
            xc = ((bx0 + bx1) * 0.5 - x0) / max(1.0, (x1 - x0))
        except (TypeError, ValueError, IndexError):
            continue
        edge = min(xc, 1.0 - xc)
        edge_bonus = 40.0 if edge < 0.22 else (15.0 if edge < 0.35 else -20.0)
        score = cjk * 10 + bh / max(1.0, bw) + edge_bonus
        cands.append((score, text, bx0, by0, bx1, by1))
    if not cands:
        return "", None
    cands.sort(key=lambda x: -x[0])
    parts = [cands[0][1]]
    boxes = [cands[0][2:]]
    for sc, tx, *xy in cands[1:3]:
        if sc >= cands[0][0] * 0.45 and not _ocr_same(parts[0], tx):
            if tx not in parts[0] and parts[0] not in tx:
                parts.append(tx)
                boxes.append(tuple(xy))  # type: ignore[arg-type]
    text = _ocr_join_lines(parts)
    bx0 = min(b[0] for b in boxes)
    by0 = min(b[1] for b in boxes)
    bx1 = max(b[2] for b in boxes)
    by1 = max(b[3] for b in boxes)
    return text, _xyxy_to_bbox(bx0, by0, bx1, by1, vw, vh, pad=3)


def _ocr_scan_stamps(
    video: Path,
    stamps: list[float],
    *,
    project_id: str | None,
    workers: int,
    reader: Any,
) -> list[tuple[float, str]]:
    """OCR song song theo mốc thời gian — seek cụm thưa, không walk full video."""
    if not stamps:
        return []
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1080)
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1920)
    targets: dict[int, list[tuple[int, float]]] = {}
    for i, t in enumerate(stamps):
        targets.setdefault(max(0, int(round(t * fps))), []).append((i, t))

    w = min(_ocr_pool_workers(workers, cap=_cpu_budget(0.9)), len(stamps))
    out: list[tuple[float, str] | None] = [None] * len(stamps)
    _scan_engine_lock = threading.Lock()
    _tls = threading.local()
    sem = _ocr_semaphore()

    def _worker(item: tuple[int, float, Any]) -> tuple[int, float, str]:
        idx, t, frame = item
        check_cancel(project_id)
        with sem:
            check_cancel(project_id)
            if getattr(_tls, "ocr", None) is None:
                with _scan_engine_lock:
                    if getattr(_tls, "ocr", None) is None:
                        try:
                            _tls.ocr = _rapidocr_labels()
                        except Exception:
                            _tls.ocr = _rapidocr_labels(use_cuda=False)
            try:
                text = reader(frame, _tls.ocr, vw, vh)
            except Exception:
                _tls.ocr = _rapidocr_labels(use_cuda=False)
                text = reader(frame, _tls.ocr, vw, vh)
            return idx, t, text or ""

    def _collect(done: Any) -> None:
        for fut in done:
            i, t, text = fut.result()
            if text:
                out[i] = (t, text)

    # Cụm frame gần nhau → đọc tuần tự; khoảng cách lớn → seek (tránh decode thừa).
    ordered = sorted(targets)
    runs: list[list[int]] = []
    for fi in ordered:
        if runs and fi - runs[-1][-1] <= 2:
            runs[-1].append(fi)
        else:
            runs.append([fi])

    pending: set[Any] = set()
    try:
        with ThreadPoolExecutor(max_workers=w, thread_name_prefix="ocr-scan") as pool:
            for run in runs:
                check_cancel(project_id)
                start, end = run[0], run[-1]
                # 1 khung / cách xa: seek theo ms (ổn định hơn POS_FRAMES trên mp4)
                if start == end:
                    cap.set(cv2.CAP_PROP_POS_MSEC, (start / fps) * 1000.0)
                    ok, frame = cap.read()
                    if not ok:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, float(start))
                        ok, frame = cap.read()
                    if not ok:
                        continue
                    snap = frame.copy()
                    for i, t in targets[start]:
                        pending.add(pool.submit(_worker, (i, t, snap)))
                else:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, float(start))
                    got = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or -1)
                    if got >= 0 and abs(got - start) > 3:
                        cap.set(cv2.CAP_PROP_POS_MSEC, (start / fps) * 1000.0)
                    for expect in range(start, end + 1):
                        ok, frame = cap.read()
                        if not ok:
                            break
                        hits = targets.get(expect)
                        if not hits:
                            continue
                        snap = frame.copy()
                        for i, t in hits:
                            pending.add(pool.submit(_worker, (i, t, snap)))
                        if len(pending) >= w * 2:
                            done, pending = wait(pending, return_when=FIRST_COMPLETED)
                            _collect(done)
                if len(pending) >= w * 2:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    _collect(done)
            _collect(as_completed(pending))
    finally:
        cap.release()
    return [x for x in out if x is not None]


def _ocr_edge_stamps(
    hits: list[tuple[float, str]],
    video_end: float,
    coarse_step: float,
    refine_step: float,
    *,
    layout: str,
) -> list[float]:
    """Refine only cluster edges; coarse hits preserve the stable interior."""
    clusters: list[list[tuple[float, str]]] = []
    for hit in hits:
        if clusters:
            prev = clusters[-1][-1]
            same = _ocr_same(prev[1], hit[1]) or _ocr_sim(prev[1], hit[1]) >= 0.72
            if layout == "label" and not same:
                same = _ocr_label_overlap(prev[1], hit[1]) >= 0.5
            if hit[0] - prev[0] <= coarse_step * 1.5 and same:
                clusters[-1].append(hit)
                continue
        clusters.append([hit])

    stamps: set[float] = set()
    pad = coarse_step
    for cluster in clusters:
        for edge in {cluster[0][0], cluster[-1][0]}:
            t = max(0.0, edge - pad)
            end = min(video_end, edge + pad)
            while t <= end + 1e-6:
                stamps.add(round(t, 3))
                t += refine_step
    return sorted(stamps)


def _ocr_mid_hardsubs(
    video: Path,
    *,
    project_id: str | None,
    video_end: float,
    workers: int = 2,
) -> list[dict[str, Any]]:
    """Hardsub ngắn giữa khung (1–4 CJK) — layout=horizontal, TTS bình thường.

    Coarse 2.5fps + refine 0.1s quanh hit (không full-video 10fps).
    """
    # coarse: ~2.5 fps — đủ bắt flash ≥0.4s; 1 chữ ngắn refine sau
    coarse = 0.5
    stamps = [i * coarse for i in range(int(video_end / coarse) + 1)]
    stamps = [t for t in stamps if t <= max(0.0, video_end - 0.02)]
    coarse_hits = _ocr_scan_stamps(
        video,
        stamps,
        project_id=project_id,
        workers=workers,
        reader=_ocr_mid_hardsub_from_frame,
    )
    if not coarse_hits:
        return []

    # refine ±0.35s quanh mỗi hit (0.1s) — gộp vùng trùng
    refine_step = 0.1
    refine_stamps = _ocr_edge_stamps(
        coarse_hits, video_end, coarse, refine_step, layout="horizontal"
    )
    timed = _ocr_scan_stamps(
        video,
        refine_stamps,
        project_id=project_id,
        workers=workers,
        reader=_ocr_mid_hardsub_from_frame,
    )
    return _ocr_cluster_hits(
        sorted(coarse_hits + timed),
        video_end=video_end,
        step=refine_step,
        layout="horizontal",
        gap=coarse * 1.25,
        min_hold=0.2,
    )


def _ocr_mid_hardsub_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> str:
    text, _bbox = _ocr_mid_item_from_frame(frame_bgr, ocr, vw, vh)
    return text


def _ocr_mid_item_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> tuple[str, dict[str, int] | None]:
    """Chữ CJK ngắn giữa khung + bbox sát ink (frame coords)."""
    import cv2

    y0, y1 = int(vh * 0.20), int(vh * 0.78)
    x0, x1 = int(vw * 0.10), int(vw * 0.90)
    roi = frame_bgr[y0:y1, x0:x1]
    if roi.size == 0:
        return "", None
    rh, rw = roi.shape[:2]
    sc = 1.0
    if max(rh, rw) > 900:
        sc = 900 / max(rh, rw)
        roi = cv2.resize(roi, (int(rw * sc), int(rh * sc)))
    result, _ = ocr(roi)
    # (score, cy, cx, text, x0,y0,x1,y1 frame)
    candidates: list[tuple[float, float, float, str, float, float, float, float]] = []
    for row in result or []:
        try:
            box, text = row[0], str(row[1] or "").strip()
        except (IndexError, TypeError):
            continue
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        cjk = sum(1 for c in compact if _is_cjk(c))
        confidence = float(row[2]) if len(row) > 2 else 1.0
        if cjk < 1 or cjk > 32:
            continue
        if confidence < (0.45 if cjk == 1 else 0.25):
            continue
        if cjk < len(compact) * 0.55:
            continue
        if len(compact) > 40:
            continue
        bw, bh = _ocr_box_wh(box)
        if bw < 6 or bh < 6:
            continue
        if bh > bw * 1.25:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            bx0 = min(xs) / sc + x0
            by0 = min(ys) / sc + y0
            bx1 = max(xs) / sc + x0
            by1 = max(ys) / sc + y0
            cx = ((bx0 + bx1) * 0.5 - x0) / max(1.0, (x1 - x0))
            cy = ((by0 + by1) * 0.5 - y0) / max(1.0, (y1 - y0))
        except (TypeError, ValueError, IndexError):
            continue
        center = 1.0 - min(1.0, abs(cx - 0.5) * 1.2)
        score = cjk * 8 + center * 4 + min(bw, bh) / 15.0 + (bw * bh) / max(1, rw * rh) * 30
        candidates.append((score, cy, cx, compact, bx0, by0, bx1, by1))
    if not candidates:
        return "", None
    best = max(candidates, key=lambda item: item[0])
    nearby = [
        item
        for item in candidates
        if abs(item[1] - best[1]) <= 0.10 and abs(item[2] - best[2]) <= 0.32
    ]
    nearby.sort(key=lambda item: (item[1], item[2]))
    text = _ocr_join_lines([item[3] for item in nearby])
    bx0 = min(item[4] for item in nearby)
    by0 = min(item[5] for item in nearby)
    bx1 = max(item[6] for item in nearby)
    by1 = max(item[7] for item in nearby)
    return text, _xyxy_to_bbox(bx0, by0, bx1, by1, vw, vh, pad=3)


def _classify_overlay_detections(
    dets: list[tuple[str, float, float, float, float, float]],
    vw: int,
    vh: int,
) -> dict[str, list[tuple[str, dict[str, int]]]]:
    """Phân vertical / mid / label từ danh sách detect (text, conf, bx0..by1).

    Vertical cột trước — glyph 1 chữ nằm trong cột không thành mid (chặn 尔→Bạn).
    """
    if vw < 8 or vh < 8:
        return {"vertical": [], "mid": [], "label": []}

    # (text, conf, bx0, by0, bx1, by1, cx, cy, bw, bh, cjk)
    items: list[tuple[str, float, float, float, float, float, float, float, float, float, int]] = []
    for text, conf, bx0, by0, bx1, by1 in dets:
        compact = re.sub(r"\s+", "", text or "")
        if not compact:
            continue
        cjk = sum(1 for c in compact if _is_cjk(c))
        if cjk < 1 or cjk > 32:
            continue
        if cjk < max(1, len(compact) * 0.5):
            continue
        if conf < (0.75 if cjk == 1 else 0.45):
            continue
        bw = max(1.0, bx1 - bx0)
        bh = max(1.0, by1 - by0)
        if bw < 4 or bh < 4:
            continue
        if by0 > vh * 0.82:
            continue
        cx = (bx0 + bx1) * 0.5
        cy = (by0 + by1) * 0.5
        items.append((compact, conf, bx0, by0, bx1, by1, cx, cy, bw, bh, cjk))

    def _is_vert_shape(
        cx: float, cy: float, bw: float, bh: float, cjk: int
    ) -> bool:
        if bw > vw * 0.28 or bh > vh * 0.55:
            return False
        tall = bh > bw * 1.15
        short_stack = cjk >= 2 and cjk <= 10 and bh >= bw * 0.85
        # RapidOCR hay tách từng glyph cột — seed 1 chữ hẹp mép
        edge = min(cx / vw, 1.0 - cx / vw)
        glyph_seed = cjk == 1 and bw < vw * 0.14 and bh < vh * 0.14 and edge < 0.28
        return tall or short_stack or glyph_seed

    vert_raw = [it for it in items if _is_vert_shape(it[6], it[7], it[8], it[9], it[10])]
    # gom cột theo cx
    vert_groups: list[list[tuple]] = []
    for it in sorted(vert_raw, key=lambda x: (x[6], x[7])):
        placed = False
        for g in vert_groups:
            g_cx = sum(x[6] for x in g) / len(g)
            g_bw = max(x[8] for x in g)
            if abs(it[6] - g_cx) < max(vw * 0.06, g_bw * 0.7):
                g.append(it)
                placed = True
                break
        if not placed:
            vert_groups.append([it])

    verticals: list[tuple[str, dict[str, int]]] = []
    vert_boxes: list[tuple[float, float, float, float]] = []
    for g in vert_groups:
        g.sort(key=lambda x: x[7])
        # bỏ cột quá yếu (1 glyph nhỏ)
        if sum(x[10] for x in g) < 2 and max(x[9] for x in g) < vh * 0.08:
            continue
        multi = [x for x in g if x[10] >= 2]
        if multi:
            # RapidOCR đôi lúc trả cả cụm + glyph rác cạnh — lấy cụm ≥2
            joined = _ocr_pick_best([x[0] for x in multi])
        else:
            joined = _ocr_join_lines([x[0] for x in g])
        if sum(1 for c in joined if _is_cjk(c)) < 2:
            continue
        # bỏ đuôi 1 glyph lạ dính cụm (花木紫工 → 花木紫)
        if len(_ocr_norm(joined)) > 3:
            stem = _ocr_norm(joined)[:3]
            rest = _ocr_norm(joined)[3:]
            if rest and all(len(p) == 1 for p in rest):
                joined = stem
        bx0 = min(x[2] for x in g)
        by0 = min(x[3] for x in g)
        bx1 = max(x[4] for x in g)
        by1 = max(x[5] for x in g)
        bb = _xyxy_to_bbox(bx0, by0, bx1, by1, vw, vh, pad=3)
        verticals.append((joined, bb))
        vert_boxes.append((bx0, by0, bx1, by1))

    def _in_vert_col(cx: float, cy: float) -> bool:
        for vx0, vy0, vx1, vy1 in vert_boxes:
            pad_x = max(12.0, (vx1 - vx0) * 0.35)
            pad_y = max(36.0, (vy1 - vy0) * 0.12)
            if (
                vx0 - pad_x <= cx <= vx1 + pad_x
                and vy0 - 8.0 <= cy <= vy1 + pad_y
            ):
                return True
        return False

    remain = [it for it in items if not _in_vert_col(it[6], it[7])]

    # mid: ngang, giữa khung, không cột
    mid_cands: list[tuple[float, tuple]] = []
    for it in remain:
        text, conf, bx0, by0, bx1, by1, cx, cy, bw, bh, cjk = it
        if bh > bw * 1.25:
            continue
        if not (vh * 0.18 < cy < vh * mid_bottom_cutoff(vw, vh)):
            continue
        if not (vw * 0.12 < cx < vw * 0.88):
            continue
        if conf < (0.85 if cjk == 1 else 0.55):
            continue
        # 1 glyph: chỉ nhận nếu rõ giữa khung (không mép watermark)
        if cjk == 1 and abs(cx / vw - 0.5) > 0.22:
            continue
        center = 1.0 - min(1.0, abs(cx / vw - 0.5) * 1.2)
        score = cjk * 8 + center * 4 + min(bw, bh) / 15.0
        mid_cands.append((score, it))

    mids: list[tuple[str, dict[str, int]]] = []
    if mid_cands:
        # Gom theo vị trí: mỗi cụm riêng biệt (cách nhau >8% height hoặc >30% width)
        # → 1 mid riêng với bbox chính xác của cụm đó.
        mid_cands_sorted = sorted(mid_cands, key=lambda x: (x[1][7], x[1][6]))  # sort by cy, cx
        mid_groups: list[list[tuple]] = []
        for _score, it in mid_cands_sorted:
            placed = False
            for g in mid_groups:
                g_cx = sum(x[6] for x in g) / len(g)
                g_cy = sum(x[7] for x in g) / len(g)
                if abs(it[7] - g_cy) <= vh * 0.08 and abs(it[6] - g_cx) <= vw * 0.30:
                    g.append(it)
                    placed = True
                    break
            if not placed:
                mid_groups.append([it])
        for g in mid_groups:
            g.sort(key=lambda x: (x[7], x[6]))
            tx = _ocr_join_lines([x[0] for x in g])
            bx0 = min(x[2] for x in g)
            by0 = min(x[3] for x in g)
            bx1 = max(x[4] for x in g)
            by1 = max(x[5] for x in g)
            mids.append((tx, _xyxy_to_bbox(bx0, by0, bx1, by1, vw, vh, pad=3)))
        used = set(id(x) for _, x in mid_cands)
        remain = [it for it in remain if id(it) not in used]

    # label: cạnh / graphic giữa (không đụng mid đã lấy)
    labels: list[tuple[str, dict[str, int]]] = []
    lab_parts: list[tuple] = []
    for it in remain:
        text, conf, bx0, by0, bx1, by1, cx, cy, bw, bh, cjk = it
        if len(text) > 28:
            continue
        if cy > vh * 0.70 and bw > vw * 0.35 and bh < vh * 0.09:
            continue
        side = cx < vw * 0.36 or cx > vw * 0.64
        tall_col = bh > bw * 1.2 and bw < vw * 0.28 and bh < vh * 0.45
        mid_graphic = (
            vh * 0.08 < cy < vh * 0.75
            and bw < vw * 0.55
            and bh < vh * 0.30
            and 1 <= cjk <= 14
            and not (bw > vw * 0.48 and bh < vh * 0.07)
        )
        multi_line_mid = (
            vh * 0.08 < cy < vh * 0.75
            and cjk >= 4
            and bw < vw * 0.85
            and bh < vh * 0.14
        )
        if not (side or tall_col or mid_graphic or multi_line_mid):
            continue
        # không label lại cột vertical đã có
        if _in_vert_col(cx, cy):
            continue
        lab_parts.append(it)

    lab_parts.sort(key=lambda x: (x[7], x[6]))
    groups: list[list[tuple]] = []
    for p in lab_parts:
        placed = False
        for g in groups:
            g_cx = sum(x[6] for x in g) / len(g)
            g_cy = max(x[7] for x in g)
            g_bw = max(x[8] for x in g)
            if abs(p[6] - g_cx) < max(vw * 0.08, g_bw * 0.45) and abs(p[7] - g_cy) < vh * 0.06:
                g.append(p)
                placed = True
                break
        if not placed:
            groups.append([p])
    for g in groups:
        g.sort(key=lambda x: x[7])
        texts = [x[0] for x in g]
        joined = _ocr_join_lines(texts) if len(texts) > 1 else texts[0]
        if sum(1 for c in joined if _is_cjk(c)) < 1:
            continue
        bx0 = min(x[2] for x in g)
        by0 = min(x[3] for x in g)
        bx1 = max(x[4] for x in g)
        by1 = max(x[5] for x in g)
        labels.append((joined, _xyxy_to_bbox(bx0, by0, bx1, by1, vw, vh, pad=3)))

    return {"vertical": verticals, "mid": mids, "label": labels}


def _ocr_overlay_boxes_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> dict[str, list[tuple[str, dict[str, int]]]]:
    """1 OCR ROI (trừ dải đáy hardsub) → mid / vertical / label theo bbox."""
    y1 = int(vh * 0.82)
    roi = frame_bgr[0:y1, :]
    if roi.size == 0:
        return {"vertical": [], "mid": [], "label": []}
    result, _ = ocr(roi)
    dets: list[tuple[str, float, float, float, float, float]] = []
    for row in result or []:
        try:
            box, text = row[0], str(row[1] or "").strip()
        except (IndexError, TypeError):
            continue
        if not text:
            continue
        conf = float(row[2]) if len(row) > 2 else 1.0
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        # ROI gốc (0,0) → frame coords
        dets.append((text, conf, min(xs), min(ys), max(xs), max(ys)))
    return _classify_overlay_detections(dets, vw, vh)


def _ocr_overlay_labels(
    video: Path,
    *,
    project_id: str | None,
    video_end: float,
    workers: int = 2,
) -> list[dict[str, Any]]:
    """Nhãn graphic / nguyên liệu cột bên — layout=label, không TTS.

    Coarse 0.35s + refine 0.15s quanh hit → timing ổn, không mảnh 0.3s.
    """
    coarse = 0.6
    stamps = [i * coarse for i in range(int(video_end / coarse) + 1)]
    stamps = [t for t in stamps if t <= max(0.0, video_end - 0.02)]
    coarse_hits = _ocr_scan_stamps(
        video,
        stamps,
        project_id=project_id,
        workers=workers,
        reader=_ocr_labels_from_frame,
    )
    if not coarse_hits:
        return []

    # refine ±0.4s quanh hit
    refine_step = 0.15
    refine_stamps = _ocr_edge_stamps(
        coarse_hits, video_end, coarse, refine_step, layout="label"
    )
    timed = _ocr_scan_stamps(
        video,
        sorted(set(refine_stamps)),
        project_id=project_id,
        workers=workers,
        reader=_ocr_labels_from_frame,
    )
    return _ocr_cluster_hits(
        sorted(coarse_hits + timed),
        video_end=video_end,
        step=refine_step,
        layout="label",
        gap=coarse * 1.25,
        min_hold=0.4,
    )


def _ocr_labels_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> str:
    items = _ocr_label_items_from_frame(frame_bgr, ocr, vw, vh)
    if not items:
        return ""
    if len(items) == 1:
        return items[0][0]
    # legacy path: nối tạm; dual-scan dùng items riêng
    return "·".join(t for t, _ in items[:6])


def _ocr_label_items_from_frame(
    frame_bgr: Any, ocr: Any, vw: int, vh: int
) -> list[tuple[str, dict[str, int]]]:
    """Mỗi khối nhãn → (text, bbox) riêng — không gộp cả khung thành 1 chuỗi."""
    y1 = int(vh * 0.86)
    roi = frame_bgr[0:y1, :]
    if roi.size == 0:
        return []
    result, _ = ocr(roi)
    # (cy, cx, bw, bh, text, x0,y0,x1,y1)
    parts: list[tuple[float, float, float, float, str, float, float, float, float]] = []
    for row in result or []:
        try:
            box, text = row[0], str(row[1] or "").strip()
        except (IndexError, TypeError):
            continue
        if not text:
            continue
        cjk = sum(1 for c in text if _is_cjk(c))
        if cjk < 1:
            continue
        compact = re.sub(r"\s+", "", text)
        confidence = float(row[2]) if len(row) > 2 else 1.0
        if confidence < (0.45 if cjk == 1 else 0.25):
            continue
        if cjk < max(1, len(compact) * 0.5):
            continue
        bw, bh = _ocr_box_wh(box)
        if cjk == 1:
            if bw < max(8, vw * 0.01) or bh < max(8, vh * 0.01):
                continue
        elif bw < 4 or bh < 4:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            bx0, by0 = min(xs), min(ys)
            bx1, by1 = max(xs), max(ys)
            cx = (bx0 + bx1) * 0.5
            cy = (by0 + by1) * 0.5
        except (TypeError, ValueError, IndexError):
            continue
        if cy > vh * 0.70 and bw > vw * 0.35 and bh < vh * 0.09:
            continue
        if bh > vh * 0.40 and bh > bw * 1.8 and vw * 0.35 < cx < vw * 0.65:
            continue
        if len(compact) > 28:
            continue
        side = cx < vw * 0.36 or cx > vw * 0.64
        tall_col = bh > bw * 1.2 and bw < vw * 0.28 and bh < vh * 0.45
        mid_graphic = (
            vh * 0.08 < cy < vh * 0.75
            and bw < vw * 0.55
            and bh < vh * 0.30
            and 1 <= cjk <= 14
            and not (bw > vw * 0.48 and bh < vh * 0.07)
        )
        multi_line_mid = (
            vh * 0.08 < cy < vh * 0.75
            and cjk >= 4
            and bw < vw * 0.85
            and bh < vh * 0.14
        )
        if not (side or tall_col or mid_graphic or multi_line_mid):
            continue
        parts.append((cy, cx, float(bw), float(bh), compact, bx0, by0, bx1, by1))
    if not parts:
        return []

    parts.sort(key=lambda x: (x[0], x[1]))
    groups: list[list[tuple[float, float, float, float, str, float, float, float, float]]] = []
    for p in parts:
        placed = False
        for g in groups:
            g_cx = sum(x[1] for x in g) / len(g)
            g_cy = max(x[0] for x in g)
            g_bw = max(x[2] for x in g)
            # chỉ gộp dòng chồng sát (cùng khối), không gộp 2 nhãn cạnh
            if abs(p[1] - g_cx) < max(vw * 0.08, g_bw * 0.45) and abs(p[0] - g_cy) < vh * 0.06:
                g.append(p)
                placed = True
                break
        if not placed:
            groups.append([p])

    out: list[tuple[str, dict[str, int]]] = []
    for g in groups:
        g.sort(key=lambda x: x[0])
        texts: list[str] = []
        for _cy, _cx, _bw, _bh, t, *_rest in g:
            if any(_ocr_same(t, u) or _ocr_sim(t, u) >= 0.8 for u in texts):
                continue
            texts.append(t)
        if not texts:
            continue
        if len(texts) >= 2 and all(len(t) <= 6 for t in texts):
            joined = "·".join(texts)
        elif len(texts) >= 2:
            joined = "".join(texts) if all(len(t) <= 8 for t in texts) else " ".join(texts)
        else:
            joined = texts[0]
        bx0 = min(p[5] for p in g)
        by0 = min(p[6] for p in g)
        bx1 = max(p[7] for p in g)
        by1 = max(p[8] for p in g)
        out.append((joined, _xyxy_to_bbox(bx0, by0, bx1, by1, vw, vh, pad=3)))
    return out


def _ocr_seg(
    index: int,
    start: float,
    end: float,
    text: str,
    *,
    layout: str = "horizontal",
    bbox: dict[str, int] | None = None,
) -> dict[str, Any]:
    lay = layout if layout in ("horizontal", "vertical", "label", "mid") else "horizontal"
    # vertical/label/mid flash: cho phép ngắn
    min_dur = 0.04 if lay in ("vertical", "label", "mid") else 0.35
    # title dọc / nhãn: mặc định không lồng tiếng (UI có tích bật lại)
    dub_default = False if lay in ("vertical", "label") else True
    seg: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "index": index,
        "start": float(start),
        "end": float(max(end, start + min_dur)),
        "source": text,
        "translation": "",
        "voice": "",
        "layout": lay,
        "dub": dub_default,
    }
    if bbox and bbox.get("w", 0) >= 8 and bbox.get("h", 0) >= 8:
        seg["bbox"] = {
            "x": int(bbox["x"]),
            "y": int(bbox["y"]),
            "w": int(bbox["w"]),
            "h": int(bbox["h"]),
        }
    return seg


__all__ = [
    '_ocr_sem',
    '_ocr_sem_n',
    '_ocr_vertical_titles',
    '_ocr_vertical_from_frame',
    '_ocr_vertical_item_from_frame',
    '_ocr_scan_stamps',
    '_ocr_edge_stamps',
    '_ocr_mid_hardsubs',
    '_ocr_mid_hardsub_from_frame',
    '_ocr_mid_item_from_frame',
    '_classify_overlay_detections',
    '_ocr_overlay_boxes_from_frame',
    '_ocr_overlay_labels',
    '_ocr_labels_from_frame',
    '_ocr_label_items_from_frame',
    '_ocr_seg',
]
