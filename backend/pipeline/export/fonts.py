"""Bundled caption font resolution shared with the frontend preview."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_VI_PROBE = "ĂÂĐÊÔƠƯăâđêôơưáàảãạắằấầếềốồớờứừýỳệỗộỹỐồủỹ"

_REF_FONT_NAMES = ("NotoSans-Bold.ttf",)


def _glyph_ink(font: Any, ch: str, size: int = 48) -> int:
    from PIL import Image, ImageDraw

    img = Image.new("L", (size * 3, size * 3), 0)
    ImageDraw.Draw(img).text((4, 4), ch, font=font, fill=255)
    return int(sum(img.getdata()))


def _system_font_dirs() -> list[Path]:
    """Chỉ font đi kèm app; không phụ thuộc font cài trên máy."""
    dirs: list[Path] = []
    # Bundled fonts first: preview and export must use the exact same bytes.
    here = Path(__file__).resolve()
    for rel in ("fonts", "assets/fonts", "../../fonts"):
        dirs.append((here.parent / rel).resolve())
    try:
        # Frozen app: _internal/pipeline/export/fonts.py → _internal/dist/fonts.
        dirs.append((here.parents[2] / "dist" / "fonts").resolve())
    except IndexError:
        pass
    try:
        # Repository / frontend build.
        repo_root = here.parents[3]
        dirs.append((repo_root / "frontend" / "public" / "fonts").resolve())
        dirs.append((repo_root / "frontend" / "dist" / "fonts").resolve())
    except IndexError:
        pass
    env = os.environ.get("ZM_AIO_FONT_DIR") or os.environ.get("FONT_DIR")
    if env:
        dirs.append(Path(env))
    out: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            key = str(d)
            if key in seen or not d.is_dir():
                continue
            seen.add(key)
            out.append(d)
        except OSError:
            continue
    return out


_font_file_index: dict[str, str] | None = None


def _font_index() -> dict[str, str]:
    """Map lowercase filename → absolute path (quét 1 lần)."""
    global _font_file_index
    if _font_file_index is not None:
        return _font_file_index
    idx: dict[str, str] = {}
    for root in _system_font_dirs():
        try:
            # Linux: fonts nằm sâu (truetype/dejavu/...)
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
                    continue
                name = p.name.lower()
                # first win — bundled dirs come before host fonts
                idx.setdefault(name, str(p.resolve()))
        except OSError:
            continue
    _font_file_index = idx
    return idx


def _resolve_font_name(name: str) -> str | None:
    """Tìm file font theo tên (không path tuyệt đối)."""
    n = (name or "").strip()
    if not n:
        return None
    # Cho phép path tuyệt đối nếu caller truyền (tests / config)
    p = Path(n)
    if p.is_file():
        return str(p.resolve())
    idx = _font_index()
    hit = idx.get(n.lower())
    if hit:
        return hit
    # stem match: "Arial Bold" → arial bold.ttf
    stem = Path(n).stem.lower()
    for k, v in idx.items():
        if Path(k).stem.lower() == stem:
            return v
    return None


def _resolve_font_names(*names: str) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        path = _resolve_font_name(n)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return tuple(out)


def _ref_ink_map(sample: str, size: int = 48) -> dict[str, int]:
    from PIL import ImageFont

    need = {c for c in sample if not c.isspace()}
    for p in _resolve_font_names(*_REF_FONT_NAMES):
        try:
            font = ImageFont.truetype(p, size)
        except OSError:
            continue
        m = {ch: _glyph_ink(font, ch, size) for ch in need}
        if all(v > 200 for v in m.values()):
            return m
    return {}


def _font_covers_text(path: str, sample: str, size: int = 48) -> bool:
    """True if font draws sample glyphs (not empty / not tofu □)."""
    try:
        from PIL import ImageFont
    except Exception:
        return Path(path).exists()
    try:
        font = ImageFont.truetype(path, size)
    except OSError:
        return False
    need = {c for c in sample if not c.isspace()}
    if not need:
        return True
    ref = _ref_ink_map(sample, size)
    try:
        question_sig = (
            font.getbbox("?"),
            round(float(font.getlength("?")), 3),
            bytes(font.getmask("?")),
        )
    except Exception:
        question_sig = None
    # tofu box ink is nearly constant across missing VI glyphs
    inks: list[int] = []
    for ch in need:
        ink = _glyph_ink(font, ch, size)
        inks.append(ink)
        if ink < 30:
            return False
        if hasattr(font, "getbbox"):
            bb = font.getbbox(ch)
            if not bb or bb[2] <= bb[0]:
                return False
        # Some fonts map unsupported Unicode directly to their visible "?"
        # glyph, so non-empty ink alone is not proof of coverage.
        if question_sig is not None and ch != "?":
            try:
                if (
                    font.getbbox(ch),
                    round(float(font.getlength(ch)), 3),
                    bytes(font.getmask(ch)),
                ) == question_sig:
                    return False
            except Exception:
                pass
        if ref:
            r = ref.get(ch, 0)
            if r > 200 and ink < max(80, int(r * 0.35)):
                return False
    if len(inks) >= 6:
        # many missing glyphs → identical square tofu ink
        uniq = {round(v / 500) for v in inks if v > 0}
        if len(uniq) <= 2 and min(inks) > 1000:
            # if almost all glyphs share ~same ink, likely tofu fallback
            lo, hi = min(inks), max(inks)
            if hi > 0 and (hi - lo) / hi < 0.08 and not ref:
                return False
    return True


_font_cache: dict[str, str] = {}


def _pick_font(candidates: tuple[str, ...], *, sample: str = _VI_PROBE, cache_key: str = "") -> str:
    key = cache_key or sample
    hit = _font_cache.get(key)
    if hit and Path(hit).exists():
        return hit
    for p in candidates:
        if Path(p).exists() and _font_covers_text(p, sample):
            _font_cache[key] = p
            return p
    for p in candidates:
        if Path(p).exists():
            _font_cache[key] = p
            return p
    fallback = _resolve_font_name("NotoSans-Bold.ttf")
    if fallback:
        _font_cache[key] = fallback
        return fallback
    raise FileNotFoundError(
        "Thiếu font đi kèm frontend/dist/fonts/NotoSans-Bold.ttf"
    )


# preset id (CAPTION_FONT_PRESETS FE) → đúng file đi kèm app
_FONT_PRESET_NAMES: dict[str, tuple[str, ...]] = {
    "system": ("NotoSans-Bold.ttf",),
    "segoe": ("Inter-Bold.ttf",),
    "arial": ("Arimo-Bold.ttf",),
    "bold": ("ArchivoBlack-Regular.ttf",),
    "helvetica": ("Roboto-Bold.ttf",),
    "verdana": ("OpenSans-Bold.ttf",),
    "tahoma": ("Carlito-Bold.ttf",),
    "trebuchet": ("FiraSans-Bold.ttf",),
    "rounded": ("Nunito-Bold.ttf",),
    "impact": ("Anton-Regular.ttf",),
    "georgia": ("Merriweather-Bold.ttf",),
    "times": ("Tinos-Bold.ttf",),
    "palatino": ("Literata-Bold.ttf",),
    "garamond": ("EBGaramond-Bold.ttf",),
    "courier": ("CourierPrime-Bold.ttf",),
    "mono": ("NotoSansMono-Bold.ttf",),
    "comic": ("ComicNeue-Bold.ttf",),
    "cjk": ("NotoSansSC-Bold.ttf",),
    "meiryo": ("NotoSansJP-Bold.ttf",),
    "malgun": ("NotoSansKR-Bold.ttf",),
}

_SUBTITLE_BOLD_NAMES = ("NotoSans-Bold.ttf",)
_SUBTITLE_VERT_NAMES = ("NotoSans-Bold.ttf",)


def _font_for_preset(preset_id: str) -> str:
    """Trả đúng font bundle của preset FE; preset lạ dùng Noto Sans bundle."""
    names = _FONT_PRESET_NAMES.get(preset_id or "") or ()
    candidates = _resolve_font_names(*names)
    if candidates:
        return _pick_font(candidates, cache_key=f"preset_{preset_id}")
    return _subtitle_font()


def _subtitle_font() -> str:
    """Font caption mặc định, cùng bytes với preview."""
    return _pick_font(
        _resolve_font_names(*_SUBTITLE_BOLD_NAMES),
        cache_key="sub_bold",
    )


def _subtitle_font_vertical() -> str:
    """Font đậm title dọc (VI/Latin)."""
    return _pick_font(
        _resolve_font_names(*_SUBTITLE_VERT_NAMES),
        cache_key="vert",
    )

