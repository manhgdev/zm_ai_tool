from pipeline.mt import review


def test_translation_review_key_changes_when_draft_changes():
    settings = {"translationReviewTranslator": "ollama", "targetLang": "vi"}
    first = review.translation_review_key(
        [{"id": "s1", "source": "Hello", "translation": "Xin chao"}], settings
    )
    second = review.translation_review_key(
        [{"id": "s1", "source": "Hello", "translation": "Xin chào"}], settings
    )

    assert first != second


def test_polish_translations_keeps_segment_order(monkeypatch):
    seen = []

    def fake_review(source, draft, target, provider, **_kwargs):
        seen.append((source, draft, target, provider))
        return f"reviewed:{draft}"

    monkeypatch.setattr(review, "_review_one", fake_review)
    result = review.polish_translations(
        ["one", "two"],
        ["mot", "hai"],
        "vi",
        translator="ollama",
        workers=2,
    )

    assert result == ["reviewed:mot", "reviewed:hai"]
    assert {item[0] for item in seen} == {"one", "two"}
