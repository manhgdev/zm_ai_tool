from pipeline.asr.speaker import assign_speakers, normalize_speaker_turns, split_segments_by_speaker


def turn(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker}


def test_aggregate_disjoint_turns_and_do_not_double_count_duplicates():
    row = {"start": 0, "end": 4}
    turns = [turn(0, 1, "A"), turn(1, 2, "A"), turn(2, 3, "A"), turn(3, 4, "B")]
    assign_speakers([row], turns)
    assert row["speaker"] == "A"
    # Repeated/overlapping turns from A must not turn a mixed cue into A.
    assign_speakers([row], [turn(0, 2, "A")] * 5 + [turn(2, 4, "B")])
    assert "speaker" not in row


def test_mixed_tiny_silent_and_zero_duration_cues_remain_unassigned():
    for turns in ([turn(0, 2, "A"), turn(2, 4, "B")], [turn(0, .1, "A")], [],
                  [turn(0, 4, "A"), turn(0, 4, "B")]):
        row = {"start": 0, "end": 4, "speaker": "stale"}
        assign_speakers([row], turns)
        assert "speaker" not in row
    row = {"start": 1, "end": 1}
    assign_speakers([row], [turn(0, 4, "A")])
    assert "speaker" not in row


def test_dominant_voice_is_kept_when_caption_has_padding():
    row = {"start": 0, "end": 4}
    assign_speakers([row], [turn(.2, 1.5, "A"), turn(1.5, 1.8, "B")])
    assert row["speaker"] == "A"


def test_word_timed_cue_splits_at_speaker_boundary_and_preserves_clock():
    rows = [{
        "id": "cue",
        "start": 0.0,
        "end": 1.2,
        "source": "Hello there again",
        "words": [
            {"word": "Hello", "start": 0.0, "end": 0.4},
            {"word": "there", "start": 0.4, "end": 0.8},
            {"word": "again", "start": 0.8, "end": 1.2},
        ],
    }]
    split = split_segments_by_speaker(rows, [turn(0.0, 0.75, "A"), turn(0.75, 1.3, "B")])
    assert [(row["source"], row["speaker"], row["start"], row["end"]) for row in split] == [
        ("Hello there", "A", 0.0, 0.8),
        ("again", "B", 0.8, 1.2),
    ]
    assert [word for row in split for word in row["words"]] == rows[0]["words"]
    assert split[0]["id"] != split[1]["id"]


def test_word_without_clear_turn_stays_unassigned_in_mixed_cue():
    rows = [{
        "start": 0.0,
        "end": 1.0,
        "source": "one two",
        "words": [
            {"word": "one", "start": 0.0, "end": 0.5},
            {"word": "two", "start": 0.5, "end": 1.0},
        ],
    }]
    split = split_segments_by_speaker(rows, [turn(0.0, 0.25, "A"), turn(0.75, 1.0, "B")])
    assert len(split) == 1
    assert "speaker" not in split[0]


def test_ambiguous_word_is_kept_as_unassigned_middle_piece():
    rows = [{
        "start": 0.0,
        "end": 1.5,
        "source": "one two three",
        "words": [
            {"word": "one", "start": 0.0, "end": 0.4},
            {"word": "two", "start": 0.4, "end": 0.9},
            {"word": "three", "start": 0.9, "end": 1.5},
        ],
    }]
    split = split_segments_by_speaker(rows, [turn(0.0, 0.4, "A"), turn(1.0, 1.5, "A")])
    assert [(row["source"], row.get("speaker")) for row in split] == [
        ("one", "A"), ("two", None), ("three", "A"),
    ]


def test_diarization_turns_drop_noise_and_join_same_speaker_gaps():
    turns = normalize_speaker_turns([
        turn(0.0, 0.05, "A"),
        turn(0.2, 0.5, "A"),
        turn(0.55, 0.9, "A"),
        turn(1.0, 1.1, "B"),
    ])
    assert turns == [{"start": 0.2, "end": 0.9, "speaker": "A"}]
