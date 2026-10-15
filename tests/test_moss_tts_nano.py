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

    def test_windows_install_ships_openmp_beside_torchaudio(self):
        """Regression: libtorchaudio.pyd fails without VCOMP140.DLL on Windows."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            python = root / "venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            source, model, refs = root / "source", root / "models", root / "refs"
            for path in (source, model, refs):
                path.mkdir()
            calls: list[list[str]] = []
            openmp_roots: list[Path] = []

            def fake_run(command, **_kwargs):
                calls.append(list(command))
                if any("numpy" in part for part in command):
                    raise RuntimeError("stop-after-openmp")

            with patch.object(moss_tts_nano, "_paths", return_value=(source, model, refs)), \
                 patch.object(moss_tts_nano, "_python", return_value=python), \
                 patch.object(moss_tts_nano, "_run", side_effect=fake_run), \
                 patch.object(moss_tts_nano.os, "name", "nt"), \
                 patch("pipeline.core.runtime_install._ensure_openmp",
                       side_effect=lambda env: openmp_roots.append(env)):
                with self.assertRaisesRegex(RuntimeError, "stop-after-openmp"):
                    moss_tts_nano.install()

            self.assertTrue(any(any("torch==2.7.0" in part for part in cmd) for cmd in calls))
            self.assertTrue(any(any("msvc-runtime" in part for part in cmd) for cmd in calls))
            self.assertEqual(openmp_roots, [python.parent.parent])


if __name__ == "__main__":
    unittest.main()
