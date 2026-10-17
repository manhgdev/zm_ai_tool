"""Tests for GPU/CPU detection and fallback — accel.py + vieneu guards."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
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
        source = (self._root / "backend/pipeline/core/runtime_install.py").read_text()
        assert "'computeMode': 'cpu' if explicit_cpu else 'auto'" not in source, \
            "runtime_install.py still has computeMode = 'auto'"

    def test_system_api_uses_gpu(self):
        source = (self._root / "backend/api/routes/system.py").read_text()
        assert "computeMode'] = 'cpu' if payload.mode == 'cpu' else 'auto'" not in source, \
            "system.py still has computeMode = 'auto'"
        assert "cpu_mode_enabled() else 'auto'" not in source, \
            "system.py hardware status still has 'auto'"
