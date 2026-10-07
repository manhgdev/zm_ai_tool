from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.tts.engines import moss_tts_nano


class MossTtsNanoPathTest(unittest.TestCase):
    def test_python_keeps_venv_launcher_instead_of_resolving_system_python(self):
        with tempfile.TemporaryDirectory() as raw:
            launcher = Path(raw) / "venv" / "bin" / "python"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(sys.executable)
            with patch.dict(os.environ, {"MOSS_TTS_NANO_PYTHON": str(launcher)}):
                selected = moss_tts_nano._python()
            self.assertEqual(selected, launcher.absolute())
            self.assertNotEqual(selected, selected.resolve())


if __name__ == "__main__":
    unittest.main()
