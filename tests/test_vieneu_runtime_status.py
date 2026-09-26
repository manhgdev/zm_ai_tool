"""Windows frozen TTS: status/synth/reset stay outside the UI process torch path."""
from __future__ import annotations

import builtins
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pipeline.tts.engines import vieneu


class FrozenTtsPathTest(unittest.TestCase):
    def test_available_uses_runtime_files_not_import_probe(self):
        with tempfile.TemporaryDirectory() as td:
            site = Path(td) / "Lib" / "site-packages"
            (site / "vieneu").mkdir(parents=True)
            py = Path(td) / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True)
            py.write_text("", encoding="utf-8")

            with (
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.dict(os.environ, {"VIENEU_DISABLED": ""}),
                mock.patch("pipeline.core.runtime_active.runtime_python", return_value=py),
                mock.patch("pipeline.core.runtime_active.runtime_site", return_value=site),
                mock.patch(
                    "pipeline.core.system_check.probe._runtime_mod_ok",
                    side_effect=AssertionError("status must not import-probe vieneu"),
                ),
                mock.patch(
                    "importlib.util.find_spec",
                    side_effect=AssertionError("GUI must not find_spec vieneu"),
                ),
            ):
                self.assertTrue(vieneu.available())

    def test_missing_runtime_not_installed(self):
        missing = Path("/tmp/zm-ai-tool-missing-runtime-python")
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.dict(os.environ, {"VIENEU_DISABLED": ""}),
            mock.patch("pipeline.core.runtime_active.runtime_python", return_value=missing),
        ):
            self.assertFalse(vieneu.available())

    def test_status_does_not_probe_worker(self):
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(vieneu, "available", return_value=True),
            mock.patch.object(vieneu, "list_preset_from_assets", return_value=[]),
            mock.patch("pipeline.tts.engines.vieneu_frozen.probe", side_effect=AssertionError("status must not probe")),
            mock.patch("pipeline.tts.engines.vieneu_frozen.has_ready_worker", return_value=False),
            mock.patch("pipeline.tts.engines.vieneu_frozen.resolve_backend", side_effect=AssertionError("cold status must not resolve")),
        ):
            with vieneu._lock:
                vieneu._load_state = "cold"
                vieneu._client_err = None
            out = vieneu.status()
            self.assertTrue(out["installed"])
            self.assertEqual(out["loadState"], "cold")
            self.assertFalse(out["ready"])

    def test_synthesize_uses_frozen_worker(self):
        out = Path(tempfile.mkdtemp()) / "out.wav"
        synth = mock.Mock()
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(vieneu, "get_client", return_value=None),
            mock.patch("pipeline.tts.engines.vieneu_frozen.resolve_backend", return_value=("onnx", "cpu")),
            mock.patch("pipeline.tts.engines.vieneu_frozen.synthesize", synth),
        ):
            vieneu.synthesize("xin chào", "vn:Phạm Tuyên", out, style="tu_nhien")
        synth.assert_called_once()
        kwargs = synth.call_args.kwargs
        self.assertEqual(kwargs["text"], "xin chào")
        self.assertEqual(kwargs["voice"], "Phạm Tuyên")
        self.assertEqual(kwargs["backend"], "onnx")

    def test_reset_client_frozen_never_imports_torch(self):
        shutdown = mock.Mock()
        fake_frozen = mock.Mock(shutdown_all_workers=shutdown)
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "torch" or (isinstance(name, str) and name.startswith("torch.")):
                raise AssertionError("frozen reset_client must not import torch")
            return real_import(name, globals, locals, fromlist, level)

        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.dict("sys.modules", {"pipeline.tts.engines.vieneu_frozen": fake_frozen}),
            mock.patch("builtins.__import__", side_effect=guarded_import),
        ):
            with vieneu._lock:
                vieneu._client = object()
                vieneu._load_state = "ready"
            vieneu.reset_client()
        self.assertIsNone(vieneu._client)
        self.assertEqual(vieneu._load_state, "cold")
        shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
