"""Split long text for TTS engines — sentence-aware (VI/EN)."""
from __future__ import annotations

import re

# Kết thúc câu: . ! ? … rồi space + chữ (kể cả sau năm 2025.)
# Không tách số thập phân / nghìn: 100.000  12.6  12,6%
_SENT_END = re.compile(
    r"(?<!\d[.,]\d)"  # không ngay sau pattern số.số (bảo hiểm)
    r"(?<!\d)([\!\?\…]+)"  # ! ? … luôn tách nếu không dính số lạ
    r"|(?<!\d[.,])"  # chấm câu
    r"(\.+)"
    r"(?!\d)"  # không phải 100.000 / 12.6
    r"(?=\s+[\"'“”‘’]?[\wÀ-ỹ]|\s*$)",
)


def split_sentences(text: str, max_chars: int = 280, *, by_sentence: bool = True) -> list[str]:
    """Tách text thành phần TTS.

    - Luôn tách theo đoạn trống (blank line) — mỗi đoạn = ít nhất 1 part.
    - ``by_sentence`` (autoSplit): tiếp tục tách theo . ! ? … trong từng đoạn.
    - Câu dài > max_chars: cắt tại khoảng trắng gần max.
    """
    raw = (text or "").strip()
    if not raw:
        return ["."]

    paragraphs = re.split(r"\n\s*\n+", raw.replace("\r\n", "\n"))
    sentences: list[str] = []

    for para in paragraphs:
        para = re.sub(r"[ \t\n]+", " ", para).strip()
        if not para:
            continue
        if not by_sentence:
            sentences.append(para)
            continue
        # 1) Tách ! ? …
        # 2) Tách . không bị kẹp giữa 2 chữ số
        parts = re.split(
            r"(?<=[!?\…])\s+|(?<=\.)(?!\d)\s+(?=[\"'“”‘’]?[^\s\d])",
            para,
        )
        for p in parts:
            p = p.strip()
            if p:
                sentences.append(p)

    if not sentences:
        return [raw]

    # Cắt câu quá dài tại khoảng trắng
    out: list[str] = []
    for s in sentences:
        while len(s) > max_chars:
            cut = s.rfind(" ", 0, max_chars)
            if cut < max_chars // 3:
                cut = max_chars
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)

    # Gộp mẩu cực ngắn (< 24 ký tự) vào câu trước nếu còn chỗ
    # (không gộp qua ranh giới đoạn — chỉ trong cùng lần tách câu)
    if not by_sentence:
        return out or ["."]

    merged: list[str] = []
    for s in out:
        if merged and len(s) < 24 and len(merged[-1]) + 1 + len(s) <= max_chars:
            merged[-1] = f"{merged[-1]} {s}"
        else:
            merged.append(s)
    return merged or ["."]
