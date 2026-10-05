from unittest.mock import patch

from pipeline.tts import studio
from pipeline.tts.engines import vieneu


def test_cancel_publishes_terminal_state_before_worker_finishes():
    job_id = "cancel-test"
    with studio._jobs_lock:
        studio._cancel_flags.pop(job_id, None)
        studio._running.pop(job_id, None)
        studio._job_progress.pop(job_id, None)
        studio._running[job_id] = True
        studio._job_progress[job_id] = {
            "current": 0,
            "total": 1,
            "pct": 2,
            "message": "Đang nạp model",
        }
    try:
        with patch.object(studio, "_publish_job_progress"):
            studio.request_cancel(job_id)
            progress = studio.get_job_progress(job_id)
            assert progress["error"] == "cancelled"
            assert progress["done"] is True
            assert progress["running"] is False

            # A late worker exception must not turn a user cancellation into
            # a generic TTS error.
            studio.set_job_error(job_id, RuntimeError("late model error"))
            assert studio.get_job_progress(job_id)["error"] == "cancelled"
    finally:
        with studio._jobs_lock:
            studio._cancel_flags.pop(job_id, None)
            studio._running.pop(job_id, None)
            studio._job_progress.pop(job_id, None)


def test_linux_cpu_tts_with_cancel_check_uses_killable_worker():
    with patch.object(vieneu.sys, "platform", "linux"), \
         patch.object(vieneu, "_resolve_backend", return_value=("onnx", "cpu")):
        assert vieneu._use_cancelable_linux_worker(lambda: False) is True
