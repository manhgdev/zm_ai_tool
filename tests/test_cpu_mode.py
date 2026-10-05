import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import accel, runtime_active


class CpuModeTests(unittest.TestCase):
    def test_cpu_precedes_cached_cuda_and_environment(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ZM_AI_TOOL_HOME': root, 'ZM_AIO_TORCH_DEVICE': 'cuda'}), patch.object(sys, 'platform', 'win32'), patch.dict(accel._cache, {'torch_device': 'cuda'}):
            path = Path(root) / 'runtime/current.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'computeMode': 'cpu'}))
            self.assertEqual(accel.preferred_torch_device(), 'cpu')
            from pipeline.gpu.manager import _compute_backend
            from pipeline.asr.speaker import preferred_diarization_provider
            from pipeline.export.stem import _demucs_backend_wanted
            from pipeline.core.system_check.probe import _nvidia_present
            self.assertEqual(_compute_backend('nvidia', 'cuda'), 'cpu')
            with patch.dict(os.environ, {'SPEAKER_DIARIZATION_PROVIDER': 'cuda'}):
                self.assertEqual(preferred_diarization_provider(), 'cpu')
            self.assertEqual(_demucs_backend_wanted(), 'cpu')
            self.assertFalse(_nvidia_present())
            path.write_text('{}')
            self.assertFalse(runtime_active.cpu_mode_enabled())


if __name__ == '__main__':
    unittest.main()
