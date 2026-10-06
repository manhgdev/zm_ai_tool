from unittest.mock import patch

import pytest

from pipeline.tts import studio


@pytest.mark.parametrize("warning", [None, "Speaker separation unavailable"])
def test_complete_preserves_transcript_and_speaker_metadata(warning):
    job_id = "speaker-completion-test"
    srt = "1\n00:00:01,200 --> 00:00:02,400\nHello\n"
    cues = [{"start": 1.2, "end": 2.4, "text": "Hello", "speaker": "SPEAKER_00"}]
    speakers = [{"id": "SPEAKER_00", "label": "Speaker 1", "voice": ""}]
    try:
        with patch.object(studio, "_publish_job_progress") as publish:
            studio._running[job_id] = True
            studio.set_job_complete(
                job_id, text="Hello", srt=srt, speaker_cues=cues,
                speakers=speakers, speaker_warning=warning,
            )
            result = studio.get_job_progress(job_id)
            assert result["text"] == "Hello"
            assert result["srt"] == srt
            assert result["speaker_cues"] == cues
            assert result["speakers"] == speakers
            assert result["speaker_warning"] == warning
            assert result["done"] is True
            assert result["running"] is False
            publish.assert_called_once_with(job_id)
    finally:
        with studio._jobs_lock:
            studio._running.pop(job_id, None)
            studio._job_progress.pop(job_id, None)
