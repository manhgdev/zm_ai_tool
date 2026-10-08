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
    monkeypatch.setattr(review, "_ollama_model_for_review", lambda *args, **kwargs: "test-model")
    result = review.polish_translations(
        ["one", "two"],
        ["mot", "hai"],
        "vi",
        translator="ollama",
        workers=2,
    )

    assert result == ["reviewed:mot", "reviewed:hai"]
    assert {item[0] for item in seen} == {"one", "two"}


def test_translate_script_uses_one_validated_batch(monkeypatch):
    prompts = []

    def fake_request(prompt, provider, **_kwargs):
        prompts.append(prompt)
        return '{"translation": [{"id": "a", "text": "Xin chào"}, {"id": "b", "text": "Tạm biệt"}]}'

    monkeypatch.setattr(review, "_request_review", fake_request)
    result = review.translate_script([
        {"id": "a", "source": "Hello", "draft": "Xin chao", "start": 0, "end": 1, "speaker": "SPEAKER_00"},
        {"id": "b", "source": "Bye", "draft": "Tam biet", "start": 1, "end": 2, "speaker": "SPEAKER_01"},
    ], "vi", translator="openai")

    assert result == [{"id": "a", "translation": "Xin chào"}, {"id": "b", "translation": "Tạm biệt"}]
    assert len(prompts) == 1
    assert '"requested_ids": ["a", "b"]' in prompts[0]
