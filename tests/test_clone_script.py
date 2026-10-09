import copy
import json
from unittest.mock import patch

import pytest

from pipeline.mt import review
from pipeline.orchestrate import asr_translate as pipeline


def cues(count=2):
    return [{"id": str(i), "source": f"Line {i}", "draft": f"Draft {i}",
             "start": i, "end": i + 1, "speaker": f"SPEAKER_{i % 2:02d}"} for i in range(count)]


def test_whole_context_and_prior_revisions_across_batches():
    original = cues(27)
    snapshot = copy.deepcopy(original)
    seen = []

    def request(prompt, provider, **kwargs):
        data = json.loads(prompt.split("\n", 1)[1])
        seen.append(data)
        return json.dumps({"translation": [{"id": i, "text": f"Dịch {i}"}
                                          for i in reversed(data["requested_ids"])]})

    with patch.object(review, "_request_review", side_effect=request):
        output = review.translate_script(original, "vi", translator="openai")
    assert [row["id"] for row in output] == [row["id"] for row in original]
    assert original == snapshot
    assert len(seen) == 2
    assert len(seen[0]["full_script"]) == len(seen[1]["full_script"]) == 27
    assert seen[1]["full_script"][0]["draft"] == "Dịch 0"
    assert seen[0]["full_script"][-1]["speaker"] == "SPEAKER_00"


@pytest.mark.parametrize("values", [[], [{"id": "0", "text": "Hello"}],
    [{"id": "0", "text": "Hello"}, {"id": "0", "text": "Bye"}],
    [{"id": "0", "text": "Hello"}, {"id": "other", "text": "Bye"}],
    [{"id": "0", "text": "Hello"}, {"id": "1", "text": " "}],
    [{"id": "0", "text": "Hello"}, {"id": "1", "text": 3}],
])
def test_invalid_ai_response_never_changes_input(values):
    original = cues()
    with patch.object(review, "_request_review", return_value=json.dumps({"translation": values})):
        with pytest.raises(RuntimeError, match="INVALID_RESPONSE"):
            review.translate_script(original, "vi", translator="openai")
    assert original == cues()


def test_cancel_stops_before_next_batch():
    with patch.object(review, "_request_review", return_value=json.dumps({"translation": [
        {"id": row["id"], "text": "Xin chào"} for row in cues(24)]})) as request:
        checks = iter([False, True])
        with pytest.raises(RuntimeError, match="Cancelled"):
            review.translate_script(cues(25), "vi", translator="openai", cancelled=lambda: next(checks))
    assert request.call_count == 1


@pytest.mark.parametrize("engine", ["whisper", "capcut", "paddleocr", "subtitle"])
def test_diarization_runs_on_each_source_and_reuses_turns(tmp_path, engine):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    wav = tmp_path / "audio.wav"
    rows = [{**row, "translation": row.pop("draft"), "voice": "system"} for row in cues()]
    rows.append({"id": "label", "start": 0, "end": 2, "source": "Title", "layout": "label", "voice": "system"})
    before = [(row["id"], row["start"], row["end"], row["source"]) for row in rows]
    settings = {"engine": engine, "speakerDiarization": True, "speakerCount": 2}
    meta = {}
    turns = [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}, {"start": 1, "end": 2, "speaker": "SPEAKER_01"}]
    with patch.object(pipeline, "cache_audio", return_value=wav), \
         patch.object(pipeline, "extract_audio", side_effect=lambda *a, **kw: wav.write_bytes(b"audio")) as extract, \
         patch.object(pipeline, "set_status"), patch.object(pipeline, "check_cancel"), \
         patch("pipeline.asr.speaker.diarize_audio", return_value=turns) as diarize:
        pipeline._attach_project_speakers("p", video, rows, settings, meta, engine, "tag")
        assert [row.get("speaker") for row in rows] == ["SPEAKER_00", "SPEAKER_01", None]
        rows[0]["voice"] = "user-voice"
        pipeline._attach_project_speakers("p", video, rows, settings, meta, engine, "tag")
        assert rows[0]["voice"] == "user-voice"
        assert diarize.call_count == extract.call_count == 1
        settings["speakerCount"] = 3
        pipeline._attach_project_speakers("p", video, rows, settings, meta, engine, "tag")
        assert diarize.call_count == 2
    assert [(row["id"], row["start"], row["end"], row["source"]) for row in rows] == before


def test_whisper_word_timed_cue_is_split_before_speaker_profiles(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    wav = tmp_path / "audio.wav"
    rows = [{
        "id": "cue",
        "start": 0.0,
        "end": 1.2,
        "source": "Hello there again",
        "translation": "",
        "voice": "system",
        "words": [
            {"word": "Hello", "start": 0.0, "end": 0.4},
            {"word": "there", "start": 0.4, "end": 0.8},
            {"word": "again", "start": 0.8, "end": 1.2},
        ],
    }]
    settings = {"engine": "whisper", "speakerDiarization": True, "speakerCount": 2}
    turns = [{"start": 0.0, "end": 0.75, "speaker": "SPEAKER_00"},
             {"start": 0.75, "end": 1.3, "speaker": "SPEAKER_01"}]
    with patch.object(pipeline, "cache_audio", return_value=wav), \
         patch.object(pipeline, "extract_audio", side_effect=lambda *a, **kw: wav.write_bytes(b"audio")), \
         patch.object(pipeline, "set_status"), patch.object(pipeline, "check_cancel"), \
         patch("pipeline.asr.speaker.diarize_audio", return_value=turns):
        pipeline._attach_project_speakers("p", video, rows, settings, {}, "whisper", "tag")
    assert [(row["source"], row["speaker"]) for row in rows] == [
        ("Hello there", "SPEAKER_00"),
        ("again", "SPEAKER_01"),
    ]
