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
            with studio._jobs_lock:
                studio._cancel_flags.pop(job_id, None)  # worker finally cleanup
            studio.set_job_error(job_id, RuntimeError("late model error"))
            assert studio.get_job_progress(job_id)["error"] == "cancelled"
            studio.set_job_complete(job_id)
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


def test_capcut_segment_keeps_cancel_callback(tmp_path):
    import pytest
    from pipeline.tts import manager
    callback = lambda: False
    with patch.object(manager, '_capcut_tts', side_effect=RuntimeError('provider reached')) as synth:
        with pytest.raises(RuntimeError, match='provider reached'):
            manager.tts_segment('Hello', 'cc:voice:resource', tmp_path / 'out.wav', None, 'none', cancel_check=callback)
    assert synth.call_args.kwargs['cancel_check'] is callback


def test_capcut_cancel_interrupts_inflight_http_and_closes_client():
    import asyncio
    import threading
    import httpx
    import pytest
    from pipeline.tts import capcut
    from pipeline.core.jobs import Cancelled

    cancelled = threading.Event()
    closed_request = threading.Event()
    async def blocked(request):
        cancelled.set()
        try:
            await asyncio.sleep(30)
        finally:
            closed_request.set()
    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    with patch.object(capcut.httpx, 'AsyncClient', return_value=client):
        with pytest.raises(Cancelled):
            capcut._request('POST', 'https://example.test/new', cancel_check=cancelled.is_set)
    assert closed_request.is_set()
    assert client.is_closed


def test_capcut_deadline_covers_connection_wait():
    import asyncio
    import time
    import httpx
    import pytest
    from pipeline.tts import capcut

    async def blocked(request):
        await asyncio.sleep(30)
    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    with patch.object(capcut.httpx, 'AsyncClient', return_value=client):
        with pytest.raises(TimeoutError, match='CAPCUT_TTS_TIMEOUT'):
            capcut._request('POST', 'https://example.test/new', deadline=time.monotonic() + 0.05)
    assert client.is_closed


def test_capcut_http_rejection_is_not_retried(tmp_path):
    import httpx
    import pytest
    from pipeline.tts import capcut
    response = httpx.Response(403, json={'ret': 403, 'errmsg': 'Forbidden'})
    with patch.object(capcut, 'load_device', return_value=dict(capcut.DEFAULT_DEVICE)), \
         patch.object(capcut, '_throttle_create'), patch.object(capcut, '_request', return_value=response) as request:
        with pytest.raises(RuntimeError, match='HTTP 403'):
            capcut.synthesize_mp3('Hello', 'voice', 'resource', tmp_path / 'out.mp3')
    assert request.call_count == 1


def test_capcut_uses_one_deadline_for_create_poll_download(tmp_path):
    import httpx
    from pipeline.tts import capcut
    responses = [
        httpx.Response(200, json={'data': {'tasks': [{'id': 'task', 'token': 'token'}]}}),
        httpx.Response(200, json={'data': {'tasks': [{'status': 'done', 'audio_url': 'https://example.test/audio.mp3'}]}}),
        httpx.Response(200, content=b'audio', request=httpx.Request('GET', 'https://example.test/audio.mp3')),
    ]
    with patch.object(capcut, 'load_device', return_value=dict(capcut.DEFAULT_DEVICE)), \
         patch.object(capcut, '_throttle_create'), patch.object(capcut, '_request', side_effect=responses) as request:
        result = capcut.synthesize_mp3('Hello', 'voice', 'resource', tmp_path / 'out.mp3')
    assert result.read_bytes() == b'audio'
    assert len({call.kwargs['deadline'] for call in request.call_args_list}) == 1


def test_capcut_provider_error_remains_error_not_cancelled(tmp_path):
    import pytest

    job_id = 'capcut-error-test'
    with patch.object(studio, 'TTS_OUTPUT', tmp_path), patch.object(studio, 'ensure_vieneu_dirs'), \
         patch.object(studio, '_find_cached_job', return_value=None), patch.object(studio, '_publish_job_progress'), \
         patch.object(studio, 'tts_segment', side_effect=RuntimeError('CapCut HTTP 403')):
        with pytest.raises(RuntimeError, match='CapCut HTTP 403') as error:
            studio.synth_text_job(text='Hello', voice='cc:voice:resource', auto_split=False, job_id=job_id)
        studio.set_job_error(job_id, error.value)
        assert studio.get_job_progress(job_id)['error'] == 'CapCut HTTP 403'
    with studio._jobs_lock:
        studio._job_progress.pop(job_id, None)
