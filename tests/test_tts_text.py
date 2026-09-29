from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.tts.text_split import normalize_tts_text
from pipeline.tts.text_split import split_sentences


def test_normalize_cjk_asr_spacing_and_identifiers() -> None:
    raw = "在 这 峡 谷 深 处 A 7 米 2，H 73 �"
    assert normalize_tts_text(raw) == "在这峡谷深处 A7 米 2，H73"


def test_normalize_keeps_real_latin_word_boundaries() -> None:
    assert normalize_tts_text("今天 test voice 27") == "今天 test voice 27"


def test_normalize_removes_non_printing_controls() -> None:
    assert normalize_tts_text("Xin\u0000chào\u200b bạn �") == "Xinchào bạn"


def test_normalize_preserves_transcript_lines() -> None:
    assert normalize_tts_text("Câu một\nCâu hai") == "Câu một\nCâu hai"


def test_split_sentences_keeps_transcript_lines_separate() -> None:
    assert split_sentences("Câu một\nCâu hai", by_sentence=True) == ["Câu một", "Câu hai"]
