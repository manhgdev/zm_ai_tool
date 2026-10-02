import asyncio
import io
import json
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import UploadFile

from api.routes.tts_studio import _transcript_rows_to_srt, _transcript_rows_to_text, _transcript_word_styles, api_tts_studio_transcribe
from pipeline.capcut_stt import subtitle_cues
from pipeline.export.srt import parse_srt, SRT_STYLES
from pipeline.asr.whisper import _segments_from_whisper
from pipeline.tts.studio import get_job_progress


def timed_row():
    words = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen".split()
    starts = [0.0, 0.2, 0.65, 0.96, 1.3, 2.1, 2.8, 3.7, 4.71, 5.2, 5.65, 6.03, 6.7, 7.11]
    return {
        "source": " ".join(words), "start": 0.0, "end": 8.0,
        "words": [{"word": word, "start": start, "end": round(start + 0.15, 3)} for word, start in zip(words, starts)],
    }


class CapCutTimelineTest(unittest.TestCase):
    def test_source_and_translation_keep_each_capcut_timestamp(self):
        source, translated = subtitle_cues({"utterances": [
            {"text": "hê lô Anh", "start_time": 0, "end_time": 380, "translation_text": "hello everyone"},
            {"text": "em hôm Nay mình", "start_time": 380, "end_time": 1500, "translation_text": "today I will"},
        ]})
        self.assertEqual(source, [
            {"start": 0.0, "end": 0.38, "text": "hê lô Anh"},
            {"start": 0.38, "end": 1.5, "text": "em hôm Nay mình"},
        ])
        self.assertEqual(translated[0]["start"], source[0]["start"])
        self.assertEqual(translated[1]["end"], source[1]["end"])

    def test_srt_serializer_does_not_estimate_inner_timing(self):
        rows = [
            {"text": "hê lô Anh em hôm Nay mình", "start": 0.0, "end": 1.5},
            {"text": "sẽ hướng dẫn", "start": 1.5, "end": 2.1},
        ]
        srt = _transcript_rows_to_srt(rows)
        self.assertIn("00:00:00,000 --> 00:00:01,500", srt)
        self.assertIn("00:00:01,500 --> 00:00:02,100", srt)
        self.assertNotIn("00:00:00,750", srt)

    def test_txt_preserves_adjacent_capcut_cue_lines(self):
        rows = [
            {"text": "hê lô Anh", "start": 0, "end": 0.38},
            {"text": "em hôm Nay mình", "start": 0.38, "end": 1.5},
            {"text": "sẽ hướng dẫn", "start": 1.5, "end": 2.1},
        ]
        self.assertEqual(_transcript_rows_to_text(rows), "hê lô Anh\nem hôm Nay mình\nsẽ hướng dẫn")
        self.assertEqual(_transcript_rows_to_text(rows, separator=" "), "hê lô Anh em hôm Nay mình sẽ hướng dẫn")

    def test_whisper_style_srt_splits_with_word_timestamps(self):
        row = timed_row()
        before = copy.deepcopy(row)
        styles = _transcript_word_styles([row])
        cues = styles["hard"].split("\n\n")
        self.assertEqual(len(cues), 2)
        self.assertIn("00:00:00,000 --> 00:00:03,850", cues[0])
        self.assertIn("00:00:04,710 --> 00:00:07,260", cues[1])
        self.assertEqual(set(styles), set(SRT_STYLES))
        for style, srt in styles.items():
            with self.subTest(style=style):
                parsed = parse_srt(srt)
                text = " ".join(c["text"] for c in parsed)
                self.assertEqual(text, row["source"])
                if len(parsed) > 1:
                    words = {w["word"]: w for w in row["words"]}
                    for cue in parsed:
                        tokens = cue["text"].split()
                        self.assertEqual(cue["start"], words[tokens[0]]["start"])
                        self.assertEqual(cue["end"], words[tokens[-1]]["end"])
        self.assertEqual(row, before)

    def test_missing_invalid_or_mismatched_word_times_never_invent_cues(self):
        for variant in ("missing", "mismatch", "negative", "overlap", "nan", "zero", "out_of_bounds"):
            row = timed_row()
            if variant == "missing":
                row.pop("words")
            elif variant == "mismatch":
                row["source"] += " edited"
            elif variant == "negative":
                row["words"][0]["start"] = -1
            elif variant == "overlap":
                row["words"][1]["start"] = 0.05
            elif variant == "nan":
                row["words"][0]["end"] = float("nan")
            elif variant == "zero":
                row["words"][0]["end"] = 0
            else:
                row["words"][-1]["end"] = 9
            with self.subTest(variant=variant):
                self.assertNotIn("v916", _transcript_word_styles([row]))
                self.assertEqual(_transcript_rows_to_srt([row]).count(" --> "), 1)

    def test_faster_whisper_keeps_words_through_pause_grouping_and_json(self):
        row = timed_row()
        segment = SimpleNamespace(text=row["source"], start=row["start"], end=row["end"],
                                  words=[SimpleNamespace(**w, probability=1.0) for w in row["words"]])
        rows = json.loads(json.dumps(_segments_from_whisper(segment)))
        self.assertGreater(len(rows), 1)
        self.assertEqual([w for r in rows for w in r["words"]], row["words"])
        self.assertEqual(" ".join(r["source"] for r in rows), row["source"])

    def test_whisper_route_publishes_timed_styles_and_keeps_source_on_translation_error(self):
        for translate, fails in ((False, False), (True, False), (True, True)):
            rows = [timed_row()]
            with self.subTest(translate=translate, fails=fails), tempfile.TemporaryDirectory() as directory, \
                    patch("pipeline.tts.voice_store.TTS_TEMP", Path(directory)), \
                    patch("api.routes.tts_studio.ensure_vieneu_dirs"), \
                    patch("pipeline.core.media.extract_audio"), \
                    patch("threading.Thread") as thread, \
                    patch("pipeline.asr.asr_whisper", return_value=rows), \
                    patch("pipeline.mt.api.translate_segments", return_value=["Bản dịch"],
                          side_effect=RuntimeError("quota") if fails else None) as mt, \
                    patch("pipeline.tts.studio._publish_job_progress"):
                thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
                with io.BytesIO(b"mock audio") as content:
                    job = asyncio.run(api_tts_studio_transcribe(
                        engine="whisper", translate=translate,
                        file=UploadFile(filename="sample.wav", file=content),
                    ))
                payload = get_job_progress(job["id"])
                self.assertTrue(payload["done"])
                self.assertNotIn("error", payload)
                self.assertEqual(payload["srt_styles"], _transcript_word_styles(rows))
                self.assertEqual(payload["srt"], _transcript_rows_to_srt(rows))
                if fails:
                    self.assertEqual(payload["translation_error"], "quota")
                elif translate:
                    self.assertEqual(parse_srt(payload["translated_srt"])[0]["start"], rows[0]["start"])
                    self.assertEqual(parse_srt(payload["translated_srt"])[0]["end"], rows[0]["end"])
                else:
                    mt.assert_not_called()

    def test_capcut_route_preserves_raw_cues(self):
        rows = [
            {"text": "hê lô Anh", "start": 0, "end": 0.38},
            {"text": "em hôm Nay mình", "start": 0.38, "end": 1.5},
            {"text": "sẽ hướng dẫn", "start": 1.5, "end": 2.1},
        ]
        for translate in (False, True):
            with self.subTest(translate=translate), tempfile.TemporaryDirectory() as directory, \
                    patch("pipeline.tts.voice_store.TTS_TEMP", Path(directory)), \
                    patch("api.routes.tts_studio.ensure_vieneu_dirs"), \
                    patch("threading.Thread") as thread, \
                    patch("pipeline.capcut_stt.transcribe_and_translate", return_value=(rows, [])), \
                    patch("pipeline.mt.api.translate_segments", return_value=["Hello", "Today", "Guide"]) as mt, \
                    patch("pipeline.tts.studio.set_job_complete") as complete:
                # Run the actual background route synchronously, without cloud calls.
                thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
                with io.BytesIO(b"mock audio") as content:
                    asyncio.run(api_tts_studio_transcribe(
                        engine="capcut", translate=translate,
                        file=UploadFile(filename="sample.wav", file=content),
                    ))
                complete.assert_called_once()
                payload = complete.call_args.kwargs
                self.assertEqual(payload["srt"], _transcript_rows_to_srt(rows))
                self.assertEqual(payload["text"], "hê lô Anh\nem hôm Nay mình\nsẽ hướng dẫn")
                if translate:
                    self.assertEqual(payload["translated_text"], "Hello\nToday\nGuide")
                    times = lambda s: [line for line in s.splitlines() if " --> " in line]
                    self.assertEqual(times(payload["srt"]), times(payload["translated_srt"]))
                else:
                    mt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
