"""Tests for GPU/CPU detection and fallback — accel.py + vieneu guards."""
from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# accel.preferred_torch_device
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_accel_cache():
    """Reset accel._cache between tests to avoid stale cached device values."""
    mod = sys.modules.get("pipeline.core.accel")
    if mod:
        with getattr(mod, "_lock"):
            getattr(mod, "_cache").clear()
    yield
    if mod:
        with getattr(mod, "_lock"):
            getattr(mod, "_cache").clear()


def _import_accel():
    """Import or reload pipeline.core.accel (avoids stale module state)."""
    if "pipeline.core.accel" in sys.modules:
        return importlib.reload(sys.modules["pipeline.core.accel"])
    return importlib.import_module("pipeline.core.accel")


class TestPreferredTorchDevice:
    """preferred_torch_device must not force cuda when torch reports CPU-only."""

    def test_nvidia_present_but_torch_cpu_only(self):
        """nvidia-smi succeeds but torch.cuda.is_available() → False ⇒ device = cpu."""
        fake_torch = MagicMock()
        fake_torch.cuda.is_available.return_value = False
        fake_torch.backends.mps.is_available.return_value = False

        with patch.dict(sys.modules, {"torch": fake_torch}), \
             patch("pipeline.core.accel._nvidia_smi", return_value=True), \
             patch("pipeline.core.runtime_active.cpu_mode_enabled", return_value=False), \
             patch("pipeline.core.runtime_active._hardware_cpu_only", return_value=False):
            accel = _import_accel()
            device = accel.preferred_torch_device(refresh=True)
            assert device == "cpu", f"Expected cpu, got {device}"

    def test_nvidia_present_and_cuda_available(self):
        """nvidia-smi + torch.cuda.is_available() → True ⇒ device = cuda."""
        fake_torch = MagicMock()
        fake_torch.cuda.is_available.return_value = True

        with patch.dict(sys.modules, {"torch": fake_torch}), \
             patch("pipeline.core.accel._nvidia_smi", return_value=True), \
             patch("pipeline.core.runtime_active.cpu_mode_enabled", return_value=False), \
             patch("pipeline.core.runtime_active._hardware_cpu_only", return_value=False):
            accel = _import_accel()
            device = accel.preferred_torch_device(refresh=True)
            assert device == "cuda"

    def test_cpu_mode_enabled_returns_cpu(self):
        """When user explicitly chose CPU mode → always cpu."""
        with patch("pipeline.core.runtime_active.cpu_mode_enabled", return_value=True):
            accel = _import_accel()
            device = accel.preferred_torch_device(refresh=True)
            assert device == "cpu"

    def test_no_nvidia_no_torch(self):
        """No GPU at all → cpu."""
        with patch.dict(sys.modules, {"torch": None}), \
             patch("pipeline.core.accel._nvidia_smi", return_value=False), \
             patch("pipeline.core.runtime_active.cpu_mode_enabled", return_value=False), \
             patch("pipeline.core.runtime_active._hardware_cpu_only", return_value=False):
            accel = _import_accel()
            device = accel.preferred_torch_device(refresh=True)
            assert device == "cpu"


# ---------------------------------------------------------------------------
# _is_cuda_error (vieneu_frozen)
# ---------------------------------------------------------------------------

class TestIsCudaError:
    """_is_cuda_error must identify CUDA/GPU errors accurately."""

    def test_cuda_unavailable(self):
        from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
        assert _is_cuda_error(RuntimeError("CUDA_UNAVAILABLE: torch.cuda.is_available() is False"))

    def test_torch_not_compiled(self):
        from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
        assert _is_cuda_error(AssertionError("Torch not compiled with CUDA enabled"))

    def test_device_mismatch(self):
        from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
        assert _is_cuda_error(RuntimeError("VIENEU_DEVICE_MISMATCH: requested pytorch/cuda, loaded onnx/cpu"))

    def test_non_cuda_error(self):
        from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
        assert not _is_cuda_error(RuntimeError("VieNeu worker timeout"))

    def test_file_not_found(self):
        from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
        assert not _is_cuda_error(FileNotFoundError("model.pt"))

    def test_cancelled(self):
        from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
        assert not _is_cuda_error(RuntimeError("Cancelled"))


# ---------------------------------------------------------------------------
# computeMode: no more 'auto'
# ---------------------------------------------------------------------------

class TestComputeModeNoAuto:
    """Backend must never write 'auto' as computeMode — only 'cpu' or 'gpu'."""

    _root = Path(__file__).resolve().parent.parent

    def test_runtime_install_uses_gpu(self):
        source = (self._root / "backend/pipeline/core/runtime_install.py").read_text(encoding="utf-8")
        assert "'computeMode': 'cpu' if explicit_cpu else 'auto'" not in source, \
            "runtime_install.py still has computeMode = 'auto'"

    def test_system_api_uses_gpu(self):
        source = (self._root / "backend/api/routes/system.py").read_text(encoding="utf-8")
        assert "computeMode'] = 'cpu' if payload.mode == 'cpu' else 'auto'" not in source, \
            "system.py still has computeMode = 'auto'"
        assert "cpu_mode_enabled() else 'auto'" not in source, \
            "system.py hardware status still has 'auto'"


@pytest.fixture
def worker_call(tmp_path, monkeypatch):
    from pipeline.tts.engines import vieneu, vieneu_frozen as worker
    monkeypatch.setattr(worker, 'runtime_python', lambda: Path(sys.executable))
    monkeypatch.setattr(worker, 'resolve_backend', lambda: ('pytorch', 'cuda'))
    monkeypatch.setattr(vieneu, 'current_mode', lambda: 'v3turbo')
    monkeypatch.setattr('pipeline.core.jobs.is_cancelled', lambda _: False)
    monkeypatch.setattr(worker, '_all_workers', [])
    release = MagicMock()
    monkeypatch.setattr(worker, '_release', release)
    args = dict(text='hello', voice='voice', out_wav=tmp_path / 'out.wav')
    return worker, args, release


def test_worker_gpu_success(worker_call, monkeypatch):
    worker, args, release = worker_call
    gpu = MagicMock()
    acquire = MagicMock(return_value=gpu)
    monkeypatch.setattr(worker, '_acquire', acquire)
    worker.synthesize(**args)
    assert acquire.call_count == 1
    assert acquire.call_args.args == ('pytorch', 'cuda')
    release.assert_called_once_with(gpu)


@pytest.mark.parametrize('phase', ['init', 'synth'])
def test_worker_retries_cpu_once(worker_call, monkeypatch, phase):
    worker, args, release = worker_call
    gpu, cpu = MagicMock(), MagicMock()
    error = RuntimeError('CUDA out of memory')
    acquire = MagicMock(side_effect=[error, cpu] if phase == 'init' else [gpu, cpu])
    if phase == 'synth':
        gpu.synth.side_effect = error
    monkeypatch.setattr(worker, '_acquire', acquire)
    worker.synthesize(**args)
    assert [call.args for call in acquire.call_args_list] == [('pytorch', 'cuda'), ('onnx', 'cpu')]
    release.assert_called_once_with(cpu)
    if phase == 'synth':
        gpu.close.assert_called_once()


@pytest.mark.parametrize('error', [FileNotFoundError('cuda-model.pt'), ValueError('CUDA model invalid'), RuntimeError('worker timeout')])
@pytest.mark.parametrize('phase', ['init', 'synth'])
def test_worker_preserves_non_gpu_error(worker_call, monkeypatch, error, phase):
    worker, args, release = worker_call
    gpu = MagicMock()
    gpu.synth.side_effect = error
    acquire = MagicMock(side_effect=error) if phase == 'init' else MagicMock(return_value=gpu)
    monkeypatch.setattr(worker, '_acquire', acquire)
    with pytest.raises(type(error)) as caught:
        worker.synthesize(**args)
    assert caught.value is error
    assert acquire.call_count == 1
    release.assert_not_called()


def test_worker_cpu_failure_does_not_retry(worker_call, monkeypatch):
    worker, args, _ = worker_call
    cpu = MagicMock()
    cpu.synth.side_effect = RuntimeError('CUDA named CPU failure')
    acquire = MagicMock(side_effect=[RuntimeError('CUDA unavailable'), cpu])
    monkeypatch.setattr(worker, '_acquire', acquire)
    with pytest.raises(RuntimeError, match='CPU failure'):
        worker.synthesize(**args)
    assert acquire.call_count == 2
    cpu.close.assert_called_once()


def test_worker_cancelled_before_retry(worker_call, monkeypatch):
    from pipeline.core.jobs import Cancelled
    worker, args, _ = worker_call
    gpu = MagicMock()
    gpu.synth.side_effect = Cancelled('CUDA job cancelled')
    acquire = MagicMock(return_value=gpu)
    monkeypatch.setattr(worker, '_acquire', acquire)
    with pytest.raises(Cancelled):
        worker.synthesize(**args)
    assert acquire.call_count == 1


def test_worker_probe_falls_back(worker_call, monkeypatch):
    worker, _, release = worker_call
    cpu = MagicMock(backend='onnx', device='cpu')
    acquire = MagicMock(side_effect=[RuntimeError('CUDA unavailable'), cpu])
    monkeypatch.setattr(worker, '_acquire', acquire)
    assert worker.probe() == (True, 'v3turbo/onnx/cpu')
    release.assert_called_once_with(cpu)


@pytest.mark.parametrize('device', ['cpu', 'cuda', 'mps'])
def test_frozen_device_probe_is_authoritative(monkeypatch, device):
    from pipeline.core import accel, runtime_active
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(runtime_active, 'cpu_mode_enabled', lambda: False)
    monkeypatch.setattr(accel, '_runtime_python', lambda: sys.executable)
    monkeypatch.setattr(accel, '_probe_torch_device_in', lambda _: device)
    monkeypatch.setattr(accel, '_nvidia_smi', lambda: True)
    monkeypatch.delenv('ZM_AIO_TORCH_DEVICE', raising=False)
    monkeypatch.delenv('TORCH_DEVICE', raising=False)
    assert accel.preferred_torch_device(refresh=True) == device


@pytest.fixture
def direct_client(monkeypatch):
    from pipeline.tts.engines import vieneu, vieneu_frozen
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.setattr(vieneu, '_client', None)
    monkeypatch.setattr(vieneu, 'available', lambda: True)
    monkeypatch.setattr(vieneu, '_resolve_backend', lambda: ('pytorch', 'cuda'))
    monkeypatch.setattr(vieneu, 'current_mode', lambda: 'v3turbo')
    monkeypatch.setattr(vieneu, '_register_vieneu_transformers', lambda: None)
    monkeypatch.setattr(vieneu, '_apply_hf_token', lambda: None)
    monkeypatch.setattr(vieneu.voice_store, 'ensure_vieneu_dirs', lambda: None)
    monkeypatch.setattr(vieneu.voice_store, 'load_cloned', lambda: [])
    for module, name in [('pipeline.core.memory_guard', 'ensure_budget'),
                         ('pipeline.core.system_check', 'ensure_runtime_torch'),
                         ('pipeline.core.system_check', 'ensure_runtime_transformers'),
                         ('pipeline.core.runtime_site', 'install_runtime_meta_path'),
                         ('pipeline.core.runtime_site', 'bootstrap_ai_runtime')]:
        monkeypatch.setattr(f'{module}.{name}', lambda: None)
    snapshot = MagicMock()
    monkeypatch.setattr(vieneu_frozen, '_enable_onnx_snapshot_paths', snapshot)
    monkeypatch.setattr(vieneu_frozen, '_pinned_moss_tokenizer', lambda: '/reviewed-codec')
    factory = MagicMock()
    monkeypatch.setitem(sys.modules, 'vieneu', ModuleType('vieneu'))
    sys.modules['vieneu'].Vieneu = factory
    return vieneu, factory, snapshot


@pytest.mark.parametrize('phase', ['prepare', 'constructor'])
def test_direct_load_falls_back(direct_client, monkeypatch, phase):
    vieneu, factory, snapshot = direct_client
    error = RuntimeError('CUDA unavailable')
    prepare = MagicMock(side_effect=error) if phase == 'prepare' else MagicMock()
    monkeypatch.setattr(vieneu, '_prepare_cuda_weight_load', prepare)
    cpu = MagicMock()
    factory.side_effect = [cpu] if phase == 'prepare' else [error, cpu]
    assert vieneu.get_client() is cpu
    assert factory.call_args.kwargs['device'] == 'cpu'
    assert factory.call_args.kwargs['backend'] == 'onnx'
    assert 'moss_tokenizer' not in factory.call_args.kwargs
    snapshot.assert_called_once()


def test_direct_load_uses_pinned_codec(direct_client, monkeypatch):
    vieneu, factory, _ = direct_client
    monkeypatch.setattr(vieneu, '_prepare_cuda_weight_load', lambda *_: None)
    vieneu.get_client()
    assert factory.call_args.kwargs['moss_tokenizer'] == '/reviewed-codec'


@pytest.mark.parametrize('backend,download_error', [('onnx', False), ('pytorch', False), ('pytorch', True)])
def test_worker_codec_revision_boundary(monkeypatch, tmp_path, backend, download_error):
    from pipeline.tts.engines.vieneu_frozen import _WORKER_SCRIPT

    snapshot = MagicMock(return_value=str(tmp_path / 'reviewed-codec'))
    if download_error:
        snapshot.side_effect = FileNotFoundError('pinned revision unavailable')
    hub = ModuleType('huggingface_hub')
    hub.snapshot_download = snapshot
    sdk = ModuleType('vieneu')
    model = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device='cpu')]))
    sdk.Vieneu = MagicMock(return_value=SimpleNamespace(backend=backend, engine=SimpleNamespace(device='cpu', model=model)))
    monkeypatch.setitem(sys.modules, 'huggingface_hub', hub)
    monkeypatch.setitem(sys.modules, 'vieneu', sdk)
    for name in ('TQDM_DISABLE', 'HF_HUB_DISABLE_PROGRESS_BARS', 'TRANSFORMERS_VERBOSITY', 'TOKENIZERS_PARALLELISM'):
        monkeypatch.setenv(name, '')
    namespace = {'__name__': 'worker_test'}
    exec(_WORKER_SCRIPT, namespace)
    replies = []
    namespace['_out'] = replies.append
    for name in ('_register', '_enable_torchaudio_soundfile_fallback', '_prepare_cuda_weight_load', '_enable_onnx_snapshot_paths'):
        namespace[name] = lambda *_: None
    request = json.dumps({'op': 'init', 'backend': backend, 'device': 'cpu'}) + '\n'
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(request.encode())))
    namespace['main']()

    assert len(replies) == 1 and replies[0]['ok'] is not download_error
    if backend == 'onnx':
        snapshot.assert_not_called()
        assert 'moss_tokenizer' not in sdk.Vieneu.call_args.kwargs
    else:
        snapshot.assert_called_once()
        assert snapshot.call_args.args == ('OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano',)
        assert snapshot.call_args.kwargs['revision'] == '6aa02b01e445cc585582cf0ba480bc3ea6c8dd68'
        if download_error:
            sdk.Vieneu.assert_not_called()  # Never retry unpinned main after a failed download.
        else:
            assert sdk.Vieneu.call_args.kwargs['moss_tokenizer'] == snapshot.return_value


def test_direct_load_preserves_missing_file(direct_client, monkeypatch):
    vieneu, factory, _ = direct_client
    monkeypatch.setattr(vieneu, '_prepare_cuda_weight_load', MagicMock())
    error = FileNotFoundError('cuda-model.pt')
    factory.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        vieneu.get_client()
    assert caught.value.__cause__ is error
    assert factory.call_count == 1


@pytest.mark.parametrize('failure', ['cuda', 'cancel', 'missing'])
def test_direct_synthesis_retry(tmp_path, monkeypatch, failure):
    from pipeline.tts.engines import vieneu
    from pipeline.core.jobs import Cancelled
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    gpu = MagicMock()
    gpu.engine.device = 'cuda:0'
    monkeypatch.setattr(vieneu, '_client', gpu)
    error = {'cuda': RuntimeError('CUDA out of memory'), 'cancel': Cancelled('CUDA cancelled'),
             'missing': FileNotFoundError('cuda-model.pt')}[failure]
    synth = MagicMock(side_effect=[error, None])
    monkeypatch.setattr(vieneu, '_synthesize_once', synth)
    reset, get = MagicMock(), MagicMock()
    monkeypatch.setattr(vieneu, 'reset_client', reset)
    monkeypatch.setattr(vieneu, 'get_client', get)
    fn = vieneu.synthesize.__wrapped__
    if failure == 'cuda':
        fn('hello', 'vn:voice', tmp_path / 'out.wav')
        assert synth.call_count == 2
        get.assert_called_once_with(cpu_fallback=True)
        reset.assert_called_once()
    else:
        with pytest.raises(type(error)) as caught:
            fn('hello', 'vn:voice', tmp_path / 'out.wav')
        assert caught.value is error
        get.assert_not_called()


@pytest.mark.parametrize('message', [
    'Traceback (most recent call last):\n  File "cuda.py", line 1\nFileNotFoundError: cuda-model.pt',
    'Traceback (most recent call last):\n  File "cuda.py", line 1\nRuntimeError: model download timed out',
    'Cancelled CUDA job',
])
def test_rpc_traceback_does_not_misclassify_non_cuda(message):
    from pipeline.tts.engines.vieneu_frozen import _is_cuda_error
    assert not _is_cuda_error(RuntimeError(message))


def test_worker_reuses_actual_cpu_backend(worker_call, monkeypatch):
    from pipeline.tts.engines import vieneu_frozen as worker
    # Use the real resolver instead of the fixture's requested CUDA backend.
    import importlib
    resolver = importlib.reload(worker).resolve_backend
    cpu = MagicMock(mode='v3turbo', backend='onnx', device='cpu')
    cpu.alive.return_value = True
    monkeypatch.setattr(worker, '_all_workers', [cpu])
    assert resolver() == ('onnx', 'cpu')
