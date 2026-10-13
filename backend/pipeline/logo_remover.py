"""Xử lý vùng logo/watermark sau khi ảnh đã được tải xuống.

Fix 28 bổ sung Flow Watermark Auto Mask: tự dò biểu tượng sparkle của Flow ở góc phải dưới, thay vì xóa một hình chữ nhật cố định.
Tool chỉ crop một vùng nhỏ quanh logo, chạy model trên vùng đó rồi blend lại vào
ảnh gốc để cho kết quả tự nhiên hơn khi chạy batch.

Nếu chưa có model hoặc model lỗi, tool có thể tự fallback sang Smart Texture Fill.
Chỉ dùng cho ảnh người dùng sở hữu hoặc có quyền chỉnh sửa.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from pipeline.core.runtime_site import ensure_cv2

# cv2/numpy/onnxruntime(CPU) đã đóng gói sẵn trong APP (cùng phiên bản với runtime) nên import luôn chạy được;
# khi .venv-runtime đã cài, ensure_cv2 ưu tiên site-packages của nó.
cv2 = ensure_cv2()
import numpy as np  # noqa: E402

ORT_IMPORT_ERROR = ""
try:
    import onnxruntime as ort
except Exception as exc:  # noqa: BLE001
    ort = None
    ORT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class LogoRemovalError(RuntimeError):
    """Không thể xử lý vùng logo trên ảnh."""



def _resource_path(name: str) -> Path:
    # Dev: backend/pipeline/assets. Bản build: build.mjs copy cả thư mục `pipeline`
    # vào _MEIPASS/pipeline nên đường dẫn theo file module vẫn đúng ở cả hai chế độ.
    return Path(__file__).resolve().parent / "assets" / name

_FLOW_TEMPLATE_CACHE: np.ndarray | None = None

def _load_flow_template() -> np.ndarray:
    global _FLOW_TEMPLATE_CACHE
    if _FLOW_TEMPLATE_CACHE is not None:
        return _FLOW_TEMPLATE_CACHE.copy()
    path = _resource_path("flow_watermark_template.png")
    template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) if path.is_file() else None
    if template is None or template.size == 0:
        size = 160
        template = np.zeros((size, size), dtype=np.uint8)
        cx = cy = size // 2
        rx, ry = int(size * 0.40), int(size * 0.44)
        ix, iy = int(size * 0.10), int(size * 0.11)
        points = np.array([[cx, cy-ry],[cx+ix,cy-iy],[cx+rx,cy],[cx+ix,cy+iy],[cx,cy+ry],[cx-ix,cy+iy],[cx-rx,cy],[cx-ix,cy-iy]], dtype=np.int32)
        cv2.fillPoly(template, [points], 255)
        template = cv2.GaussianBlur(template, (5,5), 0)
    _FLOW_TEMPLATE_CACHE = template
    return template.copy()


# Model LaMa mặc định đi kèm dự án (rỗng nếu chưa tải → tự rơi về inpaint OpenCV).
DEFAULT_LAMA_MODEL = str(_resource_path("lama_fp32.onnx")) if _resource_path("lama_fp32.onnx").is_file() else ""

VALID_CORNERS = {"bottom_right", "bottom_left", "top_right", "top_left"}
VALID_MODES = {"auto", "smart_texture", "inpaint", "lama_local", "lama_plus"}


def _ensure_ort():
    """Thử import lại onnxruntime khi dùng (runtime có thể vừa được cài/kích hoạt sau khi app khởi động)."""
    global ort, ORT_IMPORT_ERROR
    if ort is None:
        try:
            import onnxruntime as loaded
            ort, ORT_IMPORT_ERROR = loaded, ""
        except Exception as exc:  # noqa: BLE001
            ORT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    return ort


def lama_runtime_available() -> bool:
    """ONNX Runtime đã import được trong process hiện tại hay chưa."""
    return _ensure_ort() is not None


def lama_runtime_error() -> str:
    """Chi tiết lỗi import ONNX Runtime để log/debug bản EXE Windows."""
    return ORT_IMPORT_ERROR


@dataclass(frozen=True)
class LogoRemovalSettings:
    enabled: bool = False
    corner: str = "bottom_right"
    width_percent: int = 18
    height_percent: int = 12
    margin_percent: int = 1
    inpaint_radius: int = 7
    keep_original: bool = True
    mode: str = "auto"
    lama_model_path: str = ""
    auto_fallback: bool = True

    def normalized(self) -> "LogoRemovalSettings":
        corner = self.corner if self.corner in VALID_CORNERS else "bottom_right"
        mode = self.mode if self.mode in VALID_MODES else "auto"
        return LogoRemovalSettings(
            enabled=bool(self.enabled),
            corner=corner,
            width_percent=max(1, min(40, int(self.width_percent))),
            height_percent=max(1, min(30, int(self.height_percent))),
            margin_percent=max(0, min(10, int(self.margin_percent))),
            inpaint_radius=max(2, min(30, int(self.inpaint_radius))),
            keep_original=bool(self.keep_original),
            mode=mode,
            lama_model_path=str(self.lama_model_path or ""),
            auto_fallback=bool(self.auto_fallback),
        )


@dataclass(frozen=True)
class LogoRemovalResult:
    output_path: str
    backup_path: str | None
    region: tuple[int, int, int, int]
    mode_used: str = "smart_texture"


def _read_image(path: Path) -> np.ndarray:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    except Exception as exc:  # noqa: BLE001
        raise LogoRemovalError(f"Không đọc được ảnh: {exc}") from exc
    if image is None or image.size == 0:
        raise LogoRemovalError("File tải về không phải ảnh hợp lệ hoặc ảnh bị hỏng.")
    if image.ndim not in {2, 3}:
        raise LogoRemovalError("Định dạng ảnh không được hỗ trợ.")
    return image


def _region_for(image: np.ndarray, settings: LogoRemovalSettings) -> tuple[int, int, int, int]:
    height, width = image.shape[:2]
    region_w = max(2, round(width * settings.width_percent / 100))
    region_h = max(2, round(height * settings.height_percent / 100))
    margin_x = round(width * settings.margin_percent / 100)
    margin_y = round(height * settings.margin_percent / 100)

    if "right" in settings.corner:
        x2 = max(1, width - margin_x)
        x1 = max(0, x2 - region_w)
    else:
        x1 = min(width - 1, margin_x)
        x2 = min(width, x1 + region_w)

    if "bottom" in settings.corner:
        y2 = max(1, height - margin_y)
        y1 = max(0, y2 - region_h)
    else:
        y1 = min(height - 1, margin_y)
        y2 = min(height, y1 + region_h)

    if x2 - x1 < 2 or y2 - y1 < 2:
        raise LogoRemovalError("Vùng xóa logo quá nhỏ so với kích thước ảnh.")
    return x1, y1, x2, y2


def _legacy_mask(image: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = region
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y1:y2, x1:x2] = 255
    kernel_size = max(3, int(min(x2 - x1, y2 - y1) * 0.025))
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    return cv2.dilate(mask, kernel, iterations=1)


def _candidate_boxes(shape: tuple[int, int], region: tuple[int, int, int, int], corner: str) -> list[tuple[int, int, int, int, str]]:
    h, w = shape
    x1, y1, x2, y2 = region
    rw, rh = x2 - x1, y2 - y1
    gap = max(6, round(min(rw, rh) * 0.08))
    boxes: list[tuple[int, int, int, int, str]] = []

    def add(sx1: int, sy1: int, name: str) -> None:
        sx2, sy2 = sx1 + rw, sy1 + rh
        if sx1 < 0 or sy1 < 0 or sx2 > w or sy2 > h:
            return
        overlap_x = max(0, min(sx2, x2) - max(sx1, x1))
        overlap_y = max(0, min(sy2, y2) - max(sy1, y1))
        if overlap_x * overlap_y > 0:
            return
        boxes.append((sx1, sy1, sx2, sy2, name))

    if "right" in corner:
        add(x1 - rw - gap, y1, "left")
        add(x1 - 2 * rw - 2 * gap, y1, "far_left")
    else:
        add(x2 + gap, y1, "right")
        add(x2 + rw + 2 * gap, y1, "far_right")
    if "bottom" in corner:
        add(x1, y1 - rh - gap, "above")
        add(x1, y1 - 2 * rh - 2 * gap, "far_above")
    else:
        add(x1, y2 + gap, "below")
        add(x1, y2 + rh + 2 * gap, "far_below")
    dx = -rw - gap if "right" in corner else rw + gap
    dy = -rh - gap if "bottom" in corner else rh + gap
    add(x1 + dx, y1 + dy, "diagonal")
    for frac, name in ((0.45, "offset_a"), (0.8, "offset_b")):
        ox = int(rw * frac)
        oy = int(rh * frac)
        sx1 = x1 - rw - gap if "right" in corner else x2 + gap
        sy1 = y1 - oy if "bottom" in corner else y1 + oy
        add(sx1, sy1, name)
        sx1 = x1 - ox if "right" in corner else x1 + ox
        sy1 = y1 - rh - gap if "bottom" in corner else y2 + gap
        add(sx1, sy1, name + "_v")
    unique=[]
    seen=set()
    for item in boxes:
        key=item[:4]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _ring_stats(image: np.ndarray, region: tuple[int, int, int, int]) -> tuple[np.ndarray, np.ndarray]:
    x1, y1, x2, y2 = region
    h, w = image.shape[:2]
    pad = max(4, round(min(x2 - x1, y2 - y1) * 0.15))
    ax1, ay1 = max(0, x1 - pad), max(0, y1 - pad)
    ax2, ay2 = min(w, x2 + pad), min(h, y2 + pad)
    outer = image[ay1:ay2, ax1:ax2]
    mask = np.ones(outer.shape[:2], dtype=bool)
    rx1, ry1 = x1 - ax1, y1 - ay1
    rx2, ry2 = x2 - ax1, y2 - ay1
    mask[ry1:ry2, rx1:rx2] = False
    pixels = outer[mask]
    if pixels.size == 0:
        pixels = outer.reshape(-1, 3)
    return pixels.mean(axis=0), pixels.std(axis=0) + 1.0


def _boundary_score(image: np.ndarray, source: np.ndarray, region: tuple[int, int, int, int]) -> float:
    x1, y1, x2, y2 = region
    h, w = image.shape[:2]
    rw, rh = x2 - x1, y2 - y1
    strip = max(2, min(8, round(min(rw, rh) * 0.05)))
    diffs=[]
    def mse(a: np.ndarray, b: np.ndarray) -> float:
        if a.size == 0 or b.size == 0 or a.shape != b.shape:
            return 0.0
        aa=a.astype(np.float32)
        bb=b.astype(np.float32)
        return float(np.mean((aa-bb)**2))
    if y1 >= strip:
        diffs.append(mse(source[:strip, :], image[y1-strip:y1, x1:x2]))
    if y2 + strip <= h:
        diffs.append(mse(source[-strip:, :], image[y2:y2+strip, x1:x2]))
    if x1 >= strip:
        diffs.append(mse(source[:, :strip], image[y1:y2, x1-strip:x1]))
    if x2 + strip <= w:
        diffs.append(mse(source[:, -strip:], image[y1:y2, x2:x2+strip]))
    ring_mean, ring_std = _ring_stats(image, region)
    src_pixels = source.reshape(-1, 3).astype(np.float32)
    src_mean = src_pixels.mean(axis=0)
    src_std = src_pixels.std(axis=0) + 1.0
    color_penalty = float(np.mean((src_mean - ring_mean) ** 2))
    texture_penalty = float(np.mean((src_std - ring_std) ** 2)) * 0.6
    return (sum(diffs) / max(1, len(diffs))) + color_penalty * 0.35 + texture_penalty


def _match_color(source: np.ndarray, image: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    ring_mean, ring_std = _ring_stats(image, region)
    src = source.astype(np.float32)
    src_mean = src.reshape(-1, 3).mean(axis=0)
    src_std = src.reshape(-1, 3).std(axis=0) + 1.0
    normalized = (src - src_mean) * (ring_std / src_std) + ring_mean
    mixed = src * 0.45 + normalized * 0.55
    return np.clip(mixed, 0, 255).astype(np.uint8)


def _smart_texture_fill(working: np.ndarray, region: tuple[int, int, int, int], settings: LogoRemovalSettings) -> np.ndarray:
    x1, y1, x2, y2 = region
    rw, rh = x2 - x1, y2 - y1
    candidates = _candidate_boxes(working.shape[:2], region, settings.corner)
    if not candidates:
        raise LogoRemovalError("Không tìm được vùng texture lân cận phù hợp.")
    ranked=[]
    for sx1, sy1, sx2, sy2, name in candidates:
        patch = working[sy1:sy2, sx1:sx2].copy()
        if patch.shape[:2] != (rh, rw):
            continue
        score = _boundary_score(working, patch, region)
        ranked.append((score, patch, name))
    if not ranked:
        raise LogoRemovalError("Không có candidate texture hợp lệ.")
    ranked.sort(key=lambda item: item[0])
    patch = _match_color(ranked[0][1], working, region)
    feather = max(6, int(settings.inpaint_radius))
    pad = max(feather + 4, round(min(rw, rh) * 0.15))
    padded = cv2.copyMakeBorder(working, pad, pad, pad, pad, borderType=cv2.BORDER_REFLECT_101)
    px1, py1, px2, py2 = x1 + pad, y1 + pad, x2 + pad, y2 + pad
    mask = np.zeros((rh, rw), dtype=np.uint8)
    inset = max(1, min(feather // 3, max(1, min(rw, rh) // 12)))
    cv2.rectangle(mask, (inset, inset), (rw - 1 - inset, rh - 1 - inset), 255, -1)
    k = max(3, feather | 1)
    mask = cv2.GaussianBlur(mask, (k, k), 0)
    _, clone_mask = cv2.threshold(mask, 28, 255, cv2.THRESH_BINARY)
    center = ((px1 + px2) // 2, (py1 + py2) // 2)
    try:
        cloned = cv2.seamlessClone(patch, padded, clone_mask, center, cv2.NORMAL_CLONE)
    except cv2.error as exc:
        raise LogoRemovalError(f"Không thể hòa texture lân cận: {exc}") from exc
    result = cloned[pad:pad + working.shape[0], pad:pad + working.shape[1]].copy()
    roi_result = result[y1:y2, x1:x2].astype(np.float32)
    roi_orig = working[y1:y2, x1:x2].astype(np.float32)
    alpha = mask.astype(np.float32) / 255.0
    alpha = np.clip(alpha * 1.08, 0.0, 1.0)[:, :, None]
    result[y1:y2, x1:x2] = np.clip(roi_result * alpha + roi_orig * (1.0 - alpha), 0, 255).astype(np.uint8)
    return result


def _legacy_inpaint(working: np.ndarray, region: tuple[int, int, int, int], settings: LogoRemovalSettings) -> np.ndarray:
    mask = _legacy_mask(working, region)
    try:
        return cv2.inpaint(working, mask, settings.inpaint_radius, cv2.INPAINT_TELEA)
    except Exception as exc:  # noqa: BLE001
        raise LogoRemovalError(f"OpenCV không thể xóa vùng logo: {exc}") from exc


def _build_local_logo_mask(roi: np.ndarray, settings: LogoRemovalSettings) -> np.ndarray:
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    masks=[]
    for block in (15, 21, 31):
        try:
            th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 7)
            masks.append(th)
        except Exception:
            pass
    # bright watermark hypothesis
    _, th_bright = cv2.threshold(gray, max(180, int(np.percentile(gray, 88))), 255, cv2.THRESH_BINARY)
    masks.append(th_bright)
    # dark watermark hypothesis
    _, th_dark = cv2.threshold(gray, min(80, int(np.percentile(gray, 12))), 255, cv2.THRESH_BINARY_INV)
    masks.append(th_dark)
    mask = np.zeros_like(gray)
    for m in masks:
        mask = cv2.bitwise_or(mask, m)
    # emphasize high-contrast strokes in probable logo corner crop
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3,3), np.uint8))
    _, grad_mask = cv2.threshold(grad, max(18, int(np.percentile(grad, 82))), 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_and(mask, cv2.dilate(grad_mask, np.ones((3,3), np.uint8), iterations=1))
    # clean noise
    min_area = max(18, (w*h)//350)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    cleaned = np.zeros_like(mask)
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area >= min_area:
            cleaned[labels == i] = 255
    if cleaned.sum() == 0:
        cleaned = _legacy_mask(roi, (0,0,w,h))
    # slightly expand to catch anti-aliased edges but avoid huge box
    kernel = np.ones((3,3), np.uint8)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=1)
    cleaned = cv2.dilate(cleaned, kernel, iterations=1)
    return cleaned


def _expand_crop_bounds(working: np.ndarray, region: tuple[int, int, int, int], settings: LogoRemovalSettings) -> tuple[int,int,int,int]:
    x1,y1,x2,y2 = region
    h,w = working.shape[:2]
    rw, rh = x2-x1, y2-y1
    pad_x = max(18, int(rw * 0.55))
    pad_y = max(18, int(rh * 0.55))
    return max(0, x1-pad_x), max(0, y1-pad_y), min(w, x2+pad_x), min(h, y2+pad_y)


def _build_residual_watermark_mask(roi: np.ndarray) -> np.ndarray:
    """Bắt phần watermark sáng/mờ còn sót sau pass 1, ví dụ biểu tượng sparkle."""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    if h < 4 or w < 4:
        return np.zeros_like(gray)
    blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
    detail = cv2.subtract(gray, blur)
    bright_thr = max(170, int(np.percentile(gray, 90)))
    _, bright = cv2.threshold(gray, bright_thr, 255, cv2.THRESH_BINARY)
    _, detail_mask = cv2.threshold(detail, max(8, int(np.percentile(detail, 85))), 255, cv2.THRESH_BINARY)
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    _, grad_mask = cv2.threshold(grad, max(10, int(np.percentile(grad, 75))), 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_or(cv2.bitwise_and(bright, cv2.dilate(grad_mask, np.ones((3,3), np.uint8), iterations=1)), detail_mask)
    focus = np.zeros_like(mask)
    # ưu tiên góc phải dưới, nơi watermark Flow thường nằm
    focus[int(h*0.20):, int(w*0.18):] = 255
    mask = cv2.bitwise_and(mask, focus)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    min_area = max(12, (h * w) // 500)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    cleaned = np.zeros_like(mask)
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area >= min_area:
            cleaned[labels == i] = 255
    return cleaned


def _mask_coverage(mask: np.ndarray) -> float:
    return float(np.count_nonzero(mask)) / float(mask.size or 1)


def _inflate_mask(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    k = max(1, int(radius))
    kernel = np.ones((k * 2 + 1, k * 2 + 1), np.uint8)
    return cv2.dilate(mask, kernel, iterations=1)


def _flow_template_evidence(search: np.ndarray) -> np.ndarray:
    """Bản đồ bằng chứng dành riêng cho watermark sparkle trắng/xám bán trong suốt."""
    hsv = cv2.cvtColor(search, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY).astype(np.float32)
    smooth = cv2.GaussianBlur(gray, (0, 0), 4.0)
    local_bright = np.clip(gray - smooth, 0, None)
    if float(local_bright.max()) > 0:
        local_bright = local_bright / float(local_bright.max())
    low_sat = 1.0 - hsv[:, :, 1].astype(np.float32) / 255.0
    value = hsv[:, :, 2].astype(np.float32) / 255.0
    whiteness = low_sat * value
    evidence = local_bright * 0.65 + whiteness * 0.35
    return np.clip(evidence * 255.0, 0, 255).astype(np.uint8)


def _refine_mask_by_color(working: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Template chỉ cho vị trí/cỡ; hình thật của sparkle lấy theo màu xám của chính nó.

    Lấy màu tại tâm mask, gom vùng liền kề có màu gần đó trong cửa sổ quanh mask rồi
    chỉ nhận kết quả nếu diện tích hợp lý; ngược lại giữ nguyên mask template.
    """
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return mask
    h, w = mask.shape[:2]
    cx, cy = int(xs.mean()), int(ys.mean())
    r = max(30, int(max(xs.max() - xs.min(), ys.max() - ys.min()) * 0.9))
    x1, y1, x2, y2 = max(0, cx - r), max(0, cy - r), min(w, cx + r), min(h, cy + r)
    win = working[y1:y2, x1:x2].astype(np.int16)
    px, py = cx - x1, cy - y1
    star = np.median(win[max(0, py - 2):py + 3, max(0, px - 2):px + 3].reshape(-1, 3), axis=0)
    close = (np.abs(win - star).max(axis=2) <= 22).astype(np.uint8) * 255
    close = cv2.morphologyEx(close, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, labels, _stats, _ = cv2.connectedComponentsWithStats(close, 8)
    label = labels[py, px]
    if label == 0:
        return mask
    comp = (labels == label).astype(np.uint8) * 255
    area, base = int(np.count_nonzero(comp)), int(np.count_nonzero(mask))
    if not (0.5 * base <= area <= 1.6 * base):
        return mask
    comp = cv2.dilate(comp, np.ones((5, 5), np.uint8), iterations=1)
    refined = np.zeros_like(mask)
    refined[y1:y2, x1:x2] = comp
    return refined


def _flow_template_match(working: np.ndarray) -> tuple[np.ndarray, tuple[int,int,int,int], float, bool]:
    """Căn template watermark Flow trong một cửa sổ nhỏ ở góc phải dưới."""
    h, w = working.shape[:2]
    sx1, sy1 = int(w * 0.84), int(h * 0.72)
    sx2, sy2 = min(w, int(w * 0.997)), min(h, int(h * 0.985))
    search = working[sy1:sy2, sx1:sx2]
    full = np.zeros((h, w), dtype=np.uint8)
    if search.size == 0:
        return full, (sx1, sy1, sx2, sy2), 0.0, False

    evidence = _flow_template_evidence(search)
    base = _load_flow_template()
    # Kích thước thực tế đo trên ảnh Flow 1376x768: khoảng 48-62 x 52-66 px.
    base_w = max(28, int(round(w * 0.040)))
    base_h = max(30, int(round(h * 0.075)))
    expected_cx = w * 0.932
    expected_cy = h * 0.872

    best = None
    for scale in (0.86, 0.94, 1.00, 1.08, 1.16):
        tw = max(18, int(round(base_w * scale)))
        th = max(20, int(round(base_h * scale)))
        if tw >= evidence.shape[1] or th >= evidence.shape[0]:
            continue
        templ = cv2.resize(base, (tw, th), interpolation=cv2.INTER_AREA)
        templ_bin = (templ > 20).astype(np.uint8) * 255
        result = cv2.matchTemplate(evidence, templ_bin, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        cx = sx1 + loc[0] + tw / 2.0
        cy = sy1 + loc[1] + th / 2.0
        dx = (cx - expected_cx) / max(1.0, w * 0.035)
        dy = (cy - expected_cy) / max(1.0, h * 0.055)
        positional_penalty = 0.045 * (dx * dx + dy * dy)
        adjusted = float(score) - positional_penalty
        if best is None or adjusted > best[0]:
            best = (adjusted, float(score), loc, tw, th, templ)

    detected = bool(best is not None and best[1] >= 0.20)
    if best is not None and detected:
        _, raw_score, loc, tw, th, templ = best
        x1, y1 = sx1 + int(loc[0]), sy1 + int(loc[1])
        local = (templ > 28).astype(np.uint8) * 255
        # Ăn hết halo bán trong suốt nhưng không phá rộng ra nền.
        dilate_px = max(2, int(round(min(w, h) * 0.0035)))
        local = cv2.dilate(local, np.ones((dilate_px * 2 + 1, dilate_px * 2 + 1), np.uint8), iterations=1)
        full[y1:y1+th, x1:x1+tw] = local
        score = raw_score
    else:
        # fallback hoàn toàn deterministic: dùng vị trí chuẩn Flow, không threshold theo nội dung ảnh.
        tw, th = base_w, base_h
        cx, cy = int(round(expected_cx)), int(round(expected_cy))
        x1 = max(0, min(w-tw, cx - tw//2))
        y1 = max(0, min(h-th, cy - th//2))
        templ = cv2.resize(base, (tw, th), interpolation=cv2.INTER_AREA)
        local = (templ > 24).astype(np.uint8) * 255
        local = cv2.dilate(local, np.ones((5,5), np.uint8), iterations=1)
        full[y1:y1+th, x1:x1+tw] = local
        score = 0.0

    full = _refine_mask_by_color(working, full)
    ys, xs = np.where(full > 0)
    if len(xs) == 0:
        return full, (sx1, sy1, sx2, sy2), score, detected
    pad = max(10, int(round(min(w, h) * 0.014)))
    bbox = (max(0, int(xs.min())-pad), max(0, int(ys.min())-pad), min(w, int(xs.max())+pad+1), min(h, int(ys.max())+pad+1))
    return full, bbox, score, detected


def _flow_watermark_mask(working: np.ndarray) -> tuple[np.ndarray, tuple[int,int,int,int], bool]:
    mask, bbox, _score, detected = _flow_template_match(working)
    return mask, bbox, detected


def _flow_residual_score(working: np.ndarray) -> float:
    """Điểm template còn sót sau xử lý; dùng để quyết định pass 2."""
    _mask, _bbox, score, detected = _flow_template_match(working)
    return float(score if detected else 0.0)


def _inpaint_with_mask(working: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    """Fallback precise mask: ưu tiên Navier-Stokes để nối đường/texture tốt hơn Telea."""
    if not np.any(mask):
        raise LogoRemovalError("Không tạo được mask watermark Flow.")
    r = max(3, min(10, int(radius)))
    try:
        return cv2.inpaint(working, mask, r, cv2.INPAINT_NS)
    except Exception:
        try:
            return cv2.inpaint(working, mask, r, cv2.INPAINT_TELEA)
        except Exception as exc:  # noqa: BLE001
            raise LogoRemovalError(f"Không thể inpaint watermark Flow: {exc}") from exc


def _lama_fill_with_mask(working: np.ndarray, full_mask: np.ndarray, settings: LogoRemovalSettings) -> np.ndarray:
    """LaMa Flow-specific: crop rộng nhiều ngữ cảnh, ghép lại chỉ trong mask."""
    ys, xs = np.where(full_mask > 0)
    if len(xs) == 0:
        raise LogoRemovalError("Mask watermark Flow rỗng.")
    h, w = working.shape[:2]
    bw = int(xs.max() - xs.min() + 1)
    bh = int(ys.max() - ys.min() + 1)
    # rộng hơn Fix28 để AI nhìn được đường sàn/quần áo/cột gỗ xung quanh watermark
    pad_x = max(120, int(bw * 2.6))
    pad_y = max(120, int(bh * 2.6))
    x1 = max(0, int(xs.min()) - pad_x)
    y1 = max(0, int(ys.min()) - pad_y)
    x2 = min(w, int(xs.max()) + pad_x + 1)
    y2 = min(h, int(ys.max()) + pad_y + 1)
    crop = working[y1:y2, x1:x2].copy()
    mask = full_mask[y1:y2, x1:x2].copy()
    inp, mask_f, orig_size = _prepare_lama_input(crop, mask)
    restored = _run_lama_session(inp, mask_f, settings.lama_model_path)
    oh, ow = orig_size
    restored = restored[:oh, :ow]
    feather = max(3, ((settings.inpaint_radius // 2) * 2 + 1))
    alpha = cv2.GaussianBlur(mask, (feather, feather), 0).astype(np.float32) / 255.0
    alpha = np.clip(alpha * 1.04, 0.0, 1.0)[..., None]
    mixed = np.clip(restored.astype(np.float32) * alpha + crop.astype(np.float32) * (1.0 - alpha), 0, 255).astype(np.uint8)
    result = working.copy()
    result[y1:y2, x1:x2] = mixed
    return result


def _flow_auto_cleanup(working: np.ndarray, settings: LogoRemovalSettings) -> tuple[np.ndarray, str, tuple[int, int, int, int]]:
    """Fix29: template mask Flow -> auto align -> LaMa crop -> residual pass."""
    mask, bbox, score, detected = _flow_template_match(working)
    can_use_lama = bool(settings.lama_model_path and Path(settings.lama_model_path).expanduser().is_file() and _ensure_ort() is not None)
    if can_use_lama:
        try:
            processed = _lama_fill_with_mask(working, mask, settings)
            # Nếu watermark vẫn còn tương quan rõ, pass 2 bằng mask nở rất nhẹ.
            residual = _flow_residual_score(processed)
            if residual >= max(0.24, score * 0.72):
                stronger = cv2.dilate(mask, np.ones((5,5), np.uint8), iterations=1)
                processed = _lama_fill_with_mask(processed, stronger, settings)
                mode = "flow_template_lama_2pass"
            else:
                mode = "flow_template_lama"
            return processed, mode + ("_aligned" if detected else "_fixed"), bbox
        except LogoRemovalError as exc:
            if not settings.auto_fallback:
                raise
            # Đừng giả vờ LaMa đã chạy: mode phải nói rõ AI đã lỗi và đang fallback.
            fallback_mode = "flow_template_ns_lama_fallback_aligned" if detected else "flow_template_ns_lama_fallback_fixed"
            processed = _inpaint_with_mask(working, mask, settings.inpaint_radius)
            return processed, fallback_mode, bbox
    processed = _inpaint_with_mask(working, mask, settings.inpaint_radius)
    model_exists = bool(settings.lama_model_path and Path(settings.lama_model_path).expanduser().is_file())
    if model_exists and _ensure_ort() is None:
        mode = "flow_template_ns_onnxruntime_unavailable"
    elif not model_exists:
        mode = "flow_template_ns_no_model"
    else:
        mode = "flow_template_ns"
    return processed, mode + ("_aligned" if detected else "_fixed"), bbox


def _prepare_lama_input(roi: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, tuple[int,int]]:
    h, w = roi.shape[:2]
    pad_h = (8 - h % 8) % 8
    pad_w = (8 - w % 8) % 8
    if pad_h or pad_w:
        roi = cv2.copyMakeBorder(roi, 0, pad_h, 0, pad_w, borderType=cv2.BORDER_REFLECT_101)
        mask = cv2.copyMakeBorder(mask, 0, pad_h, 0, pad_w, borderType=cv2.BORDER_CONSTANT, value=0)
    rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    inp = np.transpose(rgb, (2, 0, 1))[None, ...]
    mask_f = (mask.astype(np.float32) / 255.0)[None, None, ...]
    return inp, mask_f, (h, w)


_LAMA_SESSION_CACHE: dict[str, object] = {}


def _get_lama_session(model_path: str, cpu_only: bool = False):
    """Tái sử dụng ONNX session giữa nhiều ảnh để batch nhanh hơn."""
    if _ensure_ort() is None:
        raise LogoRemovalError("Chưa cài onnxruntime.")
    mp = Path(model_path).expanduser().resolve()
    key = str(mp)
    cached = None if cpu_only else _LAMA_SESSION_CACHE.get(key)
    if cached is not None:
        return cached
    if not mp.is_file():
        raise LogoRemovalError(f"Không tìm thấy model LaMa ONNX: {mp}")
    providers = []
    try:
        available = set() if cpu_only else set(ort.get_available_providers())
        if "DmlExecutionProvider" in available:
            providers.append("DmlExecutionProvider")
        if "CUDAExecutionProvider" in available:
            providers.append("CUDAExecutionProvider")
        if "CPUExecutionProvider" in available:
            providers.append("CPUExecutionProvider")
    except Exception:
        providers = ["CPUExecutionProvider"]
    if not providers:
        providers = ["CPUExecutionProvider"]
    try:
        session = ort.InferenceSession(key, providers=providers)
    except Exception as exc:  # noqa: BLE001
        raise LogoRemovalError(f"Không mở được model LaMa ONNX: {exc}") from exc
    _LAMA_SESSION_CACHE[key] = session
    return session


def clear_lama_session_cache() -> None:
    _LAMA_SESSION_CACHE.clear()


def _resize_nchw(tensor: np.ndarray, width: int, height: int, nearest: bool = False) -> np.ndarray:
    arr = tensor[0]
    channels = []
    interpolation = cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR
    for channel in arr:
        channels.append(cv2.resize(channel, (width, height), interpolation=interpolation))
    return np.stack(channels, axis=0)[None, ...].astype(np.float32)


def _run_lama_session(inp: np.ndarray, mask: np.ndarray, model_path: str) -> np.ndarray:
    """Chạy LaMa ONNX; hỗ trợ model cố định 512x512 và output [0,255]."""
    session = _get_lama_session(model_path)
    inputs = session.get_inputs()
    if len(inputs) < 2:
        raise LogoRemovalError("Model ONNX không đúng định dạng LaMa (cần image và mask).")

    original_h, original_w = int(inp.shape[2]), int(inp.shape[3])
    image_meta = None
    mask_meta = None
    for meta in inputs:
        shape = list(meta.shape or [])
        channel = shape[1] if len(shape) >= 2 else None
        name = str(meta.name).lower()
        if image_meta is None and (channel == 3 or "image" in name or "img" in name):
            image_meta = meta
        elif mask_meta is None and (channel == 1 or "mask" in name):
            mask_meta = meta
    if image_meta is None:
        image_meta = inputs[0]
    if mask_meta is None:
        mask_meta = next((m for m in inputs if m.name != image_meta.name), inputs[1])

    expected_h = original_h
    expected_w = original_w
    shape = list(image_meta.shape or [])
    if len(shape) >= 4:
        if isinstance(shape[2], int) and shape[2] > 0:
            expected_h = int(shape[2])
        if isinstance(shape[3], int) and shape[3] > 0:
            expected_w = int(shape[3])

    run_inp = inp.astype(np.float32)
    run_mask = mask.astype(np.float32)
    if (expected_h, expected_w) != (original_h, original_w):
        run_inp = _resize_nchw(run_inp, expected_w, expected_h, nearest=False)
        run_mask = _resize_nchw(run_mask, expected_w, expected_h, nearest=True)

    feed = {image_meta.name: run_inp, mask_meta.name: run_mask}
    try:
        outputs = session.run(None, feed)
    except Exception as exc:  # noqa: BLE001
        # GPU (DirectML/CUDA) có thể mở được nhưng lỗi lúc chạy (GPU cũ, thiếu VRAM/DLL) → thử lại bằng CPU.
        if session.get_providers()[0] == "CPUExecutionProvider":
            raise LogoRemovalError(f"Chạy model LaMa thất bại: {exc}") from exc
        try:
            outputs = _get_lama_session(model_path, cpu_only=True).run(None, feed)
        except Exception as cpu_exc:  # noqa: BLE001
            raise LogoRemovalError(f"Chạy model LaMa thất bại: {exc}; CPU: {cpu_exc}") from cpu_exc
    if not outputs:
        raise LogoRemovalError("Model LaMa không trả về dữ liệu.")

    out = np.asarray(outputs[0])
    if out.ndim == 4:
        out = out[0]
    if out.ndim != 3 or out.shape[0] not in {1, 3}:
        raise LogoRemovalError("Output model LaMa không đúng định dạng.")
    if out.shape[0] == 1:
        out = np.repeat(out, 3, axis=0)
    out = np.transpose(out, (1, 2, 0)).astype(np.float32)

    # Carve lama_fp32.onnx trả RGB trong [0,255]. Một số export khác trả [0,1].
    finite_max = float(np.nanmax(out)) if out.size else 0.0
    if finite_max <= 2.0:
        out = out * 255.0
    out = np.clip(out, 0.0, 255.0).astype(np.uint8)
    if out.shape[:2] != (original_h, original_w):
        out = cv2.resize(out, (original_w, original_h), interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(out, cv2.COLOR_RGB2BGR)


def _lama_local_fill(working: np.ndarray, region: tuple[int,int,int,int], settings: LogoRemovalSettings) -> np.ndarray:
    """LaMa local với cleanup 2 pass cho watermark còn sót."""
    cx1, cy1, cx2, cy2 = _expand_crop_bounds(working, region, settings)
    crop = working[cy1:cy2, cx1:cx2].copy()
    x1,y1,x2,y2 = region
    local_region = (x1-cx1, y1-cy1, x2-cx1, y2-cy1)
    lx1,ly1,lx2,ly2 = local_region

    def run_pass(base_crop: np.ndarray, full_mask: np.ndarray) -> np.ndarray:
        inp, mask_f, orig_size = _prepare_lama_input(base_crop, full_mask)
        restored = _run_lama_session(inp, mask_f, settings.lama_model_path)
        oh, ow = orig_size
        restored = restored[:oh, :ow]
        blur_k = max(5, (settings.inpaint_radius * 2 + 1) | 1)
        alpha = cv2.GaussianBlur(full_mask, (blur_k, blur_k), 0).astype(np.float32) / 255.0
        alpha = alpha[..., None]
        return np.clip(restored.astype(np.float32) * alpha + base_crop.astype(np.float32) * (1.0 - alpha), 0, 255).astype(np.uint8)

    # pass 1: mask chính trong ROI logo
    local_mask = np.zeros(crop.shape[:2], dtype=np.uint8)
    refined = _build_local_logo_mask(crop[ly1:ly2, lx1:lx2], settings)
    local_mask[ly1:ly2, lx1:lx2] = refined
    local_mask = _inflate_mask(local_mask, 1)
    pass1_crop = run_pass(crop, local_mask)

    # pass 2: dò phần watermark sáng/mờ còn sót, chỉ xử lý vùng nhỏ để tránh thô
    residual_mask = _build_residual_watermark_mask(pass1_crop[ly1:ly2, lx1:lx2])
    coverage = _mask_coverage(residual_mask)
    final_crop = pass1_crop
    if 0.0008 <= coverage <= 0.35:
        second_mask = np.zeros(crop.shape[:2], dtype=np.uint8)
        second_mask[ly1:ly2, lx1:lx2] = _inflate_mask(residual_mask, 2)
        union_mask = cv2.bitwise_or(local_mask, second_mask)
        final_crop = run_pass(pass1_crop, union_mask)

    result = working.copy()
    result[cy1:cy2, cx1:cx2] = final_crop
    return result


def _encode_to_file(path: Path, image: np.ndarray) -> None:
    ext = path.suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        raise LogoRemovalError(f"Không hỗ trợ ghi định dạng ảnh '{ext or '(không có đuôi)'}'.")
    encode_ext = ".jpg" if ext == ".jpeg" else ext
    params=[]
    if encode_ext == ".jpg":
        params=[cv2.IMWRITE_JPEG_QUALITY, 96]
    elif encode_ext == ".png":
        params=[cv2.IMWRITE_PNG_COMPRESSION, 3]
    elif encode_ext == ".webp":
        params=[cv2.IMWRITE_WEBP_QUALITY, 96]
    ok, buffer = cv2.imencode(encode_ext, image, params)
    if not ok:
        raise LogoRemovalError("Không thể mã hóa ảnh sau khi xử lý.")
    tmp_path = path.with_name(path.name + ".logo_tmp")
    try:
        buffer.tofile(str(tmp_path))
        verify = _read_image(tmp_path)
        if verify.shape[:2] != image.shape[:2]:
            raise LogoRemovalError("Ảnh sau xử lý có kích thước không hợp lệ.")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def remove_logo_from_file(image_path: str | Path, settings: LogoRemovalSettings) -> LogoRemovalResult:
    settings = settings.normalized()
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise LogoRemovalError(f"Không tìm thấy file ảnh: {path}")
    image = _read_image(path)
    region = _region_for(image, settings)
    alpha: np.ndarray | None = None
    if image.ndim == 2:
        working = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        source_mode = "gray"
    elif image.shape[2] == 4:
        working = image[:, :, :3]
        alpha = image[:, :, 3]
        source_mode = "bgra"
    elif image.shape[2] == 3:
        working = image
        source_mode = "bgr"
    else:
        raise LogoRemovalError("Số kênh màu của ảnh không được hỗ trợ.")
    mode_used = settings.mode
    if settings.mode == "auto":
        # Fix 28: chế độ Auto dành cho ảnh Flow không còn phụ thuộc ROI cũ đã lưu trong config.
        # Tool tự dò watermark sparkle, xử lý đúng mask và trả bbox thực tế cho log.
        try:
            processed, mode_used, flow_bbox = _flow_auto_cleanup(working, settings)
            region = flow_bbox
        except LogoRemovalError:
            # Lớp dự phòng cuối cùng cho ảnh không giống preset Flow.
            try:
                processed = _smart_texture_fill(working, region, settings)
                mode_used = "smart_texture_auto_final_fallback"
            except LogoRemovalError:
                processed = _legacy_inpaint(working, region, settings)
                mode_used = "inpaint_auto_final_fallback"
    elif settings.mode == "lama_plus":
        try:
            processed = _lama_local_fill(working, region, settings)
            mode_used = "lama_plus"
        except LogoRemovalError:
            if settings.auto_fallback:
                processed = _smart_texture_fill(working, region, settings)
                mode_used = "smart_texture_fallback_from_lama_plus"
            else:
                raise
    elif settings.mode == "lama_local":
        try:
            processed = _lama_local_fill(working, region, settings)
            mode_used = "lama_local"
        except LogoRemovalError:
            if settings.auto_fallback:
                processed = _smart_texture_fill(working, region, settings)
                mode_used = "smart_texture_fallback"
            else:
                raise
    elif settings.mode == "smart_texture":
        try:
            processed = _smart_texture_fill(working, region, settings)
        except LogoRemovalError:
            processed = _legacy_inpaint(working, region, settings)
            mode_used = "inpaint_fallback"
    else:
        processed = _legacy_inpaint(working, region, settings)
    if source_mode == "gray":
        processed = cv2.cvtColor(processed, cv2.COLOR_BGR2GRAY)
    elif source_mode == "bgra" and alpha is not None:
        processed = np.dstack([processed, alpha])
    backup_path: Path | None = None
    if settings.keep_original:
        originals_dir = path.parent / "_originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        backup_path = originals_dir / path.name
        if backup_path.exists():
            stem = path.stem
            suffix = path.suffix
            n = 2
            while True:
                candidate = originals_dir / f"{stem}_original_{n}{suffix}"
                if not candidate.exists():
                    backup_path = candidate
                    break
                n += 1
        shutil.copy2(path, backup_path)
    try:
        _encode_to_file(path, processed)
    except Exception:
        if backup_path is not None and backup_path.is_file():
            shutil.copy2(backup_path, path)
        raise
    return LogoRemovalResult(output_path=str(path), backup_path=str(backup_path) if backup_path is not None else None, region=region, mode_used=mode_used)
