"""Local removal of a translucent white four-point overlay, without synthesis.

For I = (1-a)B + a*255, recover B instead of destroying its texture by inpainting.
Only accept a matched contour whose edge discontinuity decreases after recovery.
Opaque logos still need the ordinary OCR/inpaint path.
"""
from dataclasses import dataclass
from typing import Any


@dataclass
class SparkleOverlay:
    box: tuple[int, int, int, int]
    mask: Any
    opacity: float
    score: float


def _mask(n, x, y, width, height, power, opacity, blur):
    import cv2
    import numpy as np

    yy, xx = np.mgrid[:n * 4, :n * 4].astype(np.float32) / 4
    shape = (abs((xx - x) / (width / 2)) ** power
             + abs((yy - y) / (height / 2)) ** power <= 1).astype(np.float32)
    return cv2.GaussianBlur(cv2.resize(shape, (n, n), interpolation=cv2.INTER_AREA), (0, 0), blur)


def find_sparkle(frame) -> SparkleOverlay | None:
    """Fit location, contour and opacity; never use a fixed erase rectangle."""
    import cv2
    import numpy as np

    original_h, original_w = frame.shape[:2]
    # ponytail: this recognizer supports a static lower-right white sparkle,
    # not arbitrary symbols. Other glyphs need their own verified templates.
    scale = min(1.0, 1024 / min(original_h, original_w))
    working = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
    h, w = working.shape[:2]
    ox, oy = int(w * .78), int(h * .72)
    gray = cv2.cvtColor(working[oy:, ox:], cv2.COLOR_BGR2GRAY).astype(np.float32)
    best = None
    for size in range(max(12, round(min(h, w) * .04)), round(min(h, w) * .10), 2):
        n = size + 12
        if min(gray.shape) < n:
            continue
        m = _mask(n, n // 2, n // 2, size, size, .65, 0, .65)
        template = m - cv2.GaussianBlur(m, (0, 0), size * .15)
        target = cv2.GaussianBlur(gray, (0, 0), .65) - cv2.GaussianBlur(gray, (0, 0), size * .15)
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED))
        if best is None or score > best[0]:
            best = (score, x + ox + n // 2, y + oy + n // 2, size)
    if best is None or best[0] < .36:
        return None
    score, cx, cy, size = best
    n = round(size * 1.6)
    x0, y0 = cx - n // 2, cy - n // 2
    if x0 < 0 or y0 < 0 or x0 + n > w or y0 + n > h:
        return None
    crop = working[y0:y0 + n, x0:x0 + n, :3].astype(np.float32)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    before = gray - cv2.GaussianBlur(gray, (0, 0), 1.5)

    def evaluate(params):
        m = _mask(n, *params)
        a = params[5] * m[..., None]
        corrected = (crop - 255 * a) / (1 - a)
        g = cv2.cvtColor(corrected, cv2.COLOR_BGR2GRAY)
        residual = g - cv2.GaussianBlur(g, (0, 0), 1.5)
        edge = ((m > .05) & (m < .95)).astype(np.uint8)
        band = cv2.dilate(edge, np.ones((3, 3), np.uint8)) > 0
        delta = np.minimum(residual ** 2, 900) - np.minimum(before ** 2, 900)
        loss = float(delta[band].mean()) + 2 * float(np.mean(np.minimum(corrected, 0) ** 2))
        return loss, m, delta, band

    best_fit = None
    for power in (.6, .65, .75, .85):
        for opacity in (.15, .2, .25, .3, .35, .4):
            params = [n // 2, n // 2, size, size, power, opacity, .5]
            loss = evaluate(params)[0]
            if best_fit is None or loss < best_fit[0]:
                best_fit = loss, params
    loss, params = best_fit
    for step_scale in (1, .5, .25):
        for _ in range(4):
            for j, step in enumerate((1, 1, 2, 2, .04, .04, .2)):
                for sign in (-1, 1):
                    candidate = params.copy()
                    candidate[j] += sign * step * step_scale
                    if not (.1 <= candidate[5] <= .45 and .55 <= candidate[4] <= .9 and .3 <= candidate[6] <= 1.5):
                        continue
                    if not .85 <= candidate[2] / candidate[3] <= 1.15:
                        continue
                    if abs(candidate[0] - n // 2) > 4 or abs(candidate[1] - n // 2) > 4:
                        continue
                    if not all(size * .8 <= candidate[k] <= size * 1.2 for k in (2, 3)):
                        continue
                    candidate_loss = evaluate(candidate)[0]
                    if candidate_loss < loss:
                        loss, params = candidate_loss, candidate
    loss, m, delta, band = evaluate(params)
    # Require improvement around the contour, not just a lamp or stripe that
    # happens to correlate with one arm. Failed fits leave the source untouched.
    quadrants = [(slice(y, y + n // 2), slice(x, x + n // 2))
                 for y in (0, n // 2) for x in (0, n // 2)]
    gains = [float(delta[q][band[q]].mean()) if band[q].any() else 0 for q in quadrants]
    if loss > -10 or sum(g < -4 for g in gains) < 4:
        return None
    box = (round(x0 / scale), round(y0 / scale), round((x0 + n) / scale), round((y0 + n) / scale))
    if scale < 1:
        m = cv2.resize(m, (box[2] - box[0], box[3] - box[1]), interpolation=cv2.INTER_LINEAR)
    return SparkleOverlay(box, m, float(params[5]), float(score))


def remove_sparkle(frame, overlay: SparkleOverlay):
    """Recover existing pixels; fix only the 1px antialiased contour afterward."""
    import cv2
    import numpy as np

    x0, y0, x1, y1 = overlay.box
    crop = frame[y0:y1, x0:x1, :3]
    if crop.shape[:2] != overlay.mask.shape:
        raise ValueError('CLEANER_INVALID_LOGO_MASK')
    a = overlay.opacity * overlay.mask[..., None]
    restored = np.rint(np.clip((crop.astype(np.float32) - 255 * a) / (1 - a), 0, 255)).astype(np.uint8)
    binary = (overlay.mask > .5).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    edge = cv2.dilate(binary, kernel) - cv2.erode(binary, kernel)
    restored = cv2.inpaint(restored, edge * 255, 2, cv2.INPAINT_TELEA)
    changed = (overlay.mask > .001) | (edge > 0)
    crop[changed] = restored[changed]
    return frame
