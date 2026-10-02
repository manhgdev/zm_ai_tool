import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pipeline import capcut_stt as capcut


class CapCutRawPayloadTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.media = self.root / "sample.wav"
        self.media.write_bytes(b"mock audio")
        self.raw = self.root / "raw"
        self.stack.enter_context(patch.object(capcut, "_CAPCUT_RAW_DIR", self.raw))
        self.stack.enter_context(patch.object(capcut, "_CAPCUT_CACHE_DIR", self.root / "cache"))
        self.stack.enter_context(patch.object(capcut, "load_device", return_value={}))
        self.stack.enter_context(patch.object(capcut.httpx, "Client"))
        self.stack.enter_context(patch.object(capcut, "_upload", return_value=("vid", "upload-md5", 1500)))
        self.payload = {"utterances": [{
            "text": "hê lô Anh em", "start_time": 0, "end_time": 1500,
            "words": [{"text": "em", "start_time": 380, "end_time": 580}],
            "unknown_attribute": {"speaker": 2},
        }], "extra_metadata": [1, None, "nguyên bản"]}

    def run_cloud(self, payload):
        responses = [
            {"data": {"tasks": [{"id": "job", "token": "private-token"}]}},
            {"data": {"tasks": [{"id": "job", "token": "private-token",
                                  "status": "succeed", "payload": payload}]}},
        ]
        with patch.object(capcut, "_post_task", side_effect=responses):
            return capcut.transcribe_and_translate(
                self.media, "vi", "en", require_translation=False, force_refresh=True,
            )

    def archives(self):
        return sorted(self.raw.glob("*/*.json"))

    def test_preserves_full_payload_before_parser_without_task_credentials(self):
        parser = capcut.subtitle_cues

        def inspect(payload):
            files = self.archives()
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(record["payload"], self.payload)
            self.assertEqual(record["task_id"], "job")
            self.assertEqual(record["source_md5"], capcut._file_hashes(self.media, None)[0])
            self.assertEqual(record["request"]["words_per_line"], 15)
            self.assertEqual(record["response"]["data"]["tasks"][0]["payload"], self.payload)
            self.assertNotIn("token", record["response"]["data"]["tasks"][0])
            self.assertEqual(record["filename"], "sample.wav")
            self.assertNotIn("private-token", files[0].read_text(encoding="utf-8"))
            return parser(payload)

        with patch.object(capcut, "subtitle_cues", side_effect=inspect):
            source, translated = self.run_cloud(self.payload)
        self.assertEqual(source, [{"start": 0.0, "end": 1.5, "text": "hê lô Anh em"}])
        self.assertEqual(translated, [])

    def test_string_payload_and_repeated_runs_keep_original_archives(self):
        raw_string = json.dumps(self.payload, ensure_ascii=False, indent=4)
        self.run_cloud(raw_string)
        original = {file: file.read_bytes() for file in self.archives()}
        self.run_cloud(self.payload)
        self.assertEqual(len(self.archives()), 2)
        for file, body in original.items():
            self.assertEqual(file.read_bytes(), body)
            self.assertEqual(json.loads(body)["payload"], raw_string)
        self.assertFalse(list(self.raw.rglob("*.tmp")))

    def test_parser_failure_still_leaves_raw_evidence(self):
        with self.assertRaises(capcut.CapCutSttError):
            self.run_cloud("{malformed payload")
        self.assertEqual(json.loads(self.archives()[0].read_text())["payload"], "{malformed payload")

    def test_archive_disk_failure_warns_but_keeps_transcript(self):
        with patch.object(capcut.os, "replace", side_effect=OSError("disk full")), \
                self.assertLogs(capcut.__name__, level="WARNING") as logs:
            source, _ = self.run_cloud(self.payload)
        self.assertEqual(source[0]["text"], "hê lô Anh em")
        self.assertIn("Cannot save CapCut raw payload", logs.output[0])
        self.assertFalse(self.archives())
        self.assertFalse(list(self.raw.rglob("*.tmp")))

    def test_cache_hit_does_not_fabricate_raw_payload(self):
        self.run_cloud(self.payload)
        before = {file: file.read_bytes() for file in self.archives()}
        with patch.object(capcut, "_post_task") as cloud:
            capcut.transcribe_and_translate(self.media, "vi", "en", require_translation=False)
        cloud.assert_not_called()
        self.assertEqual({file: file.read_bytes() for file in self.archives()}, before)


if __name__ == "__main__":
    unittest.main()
