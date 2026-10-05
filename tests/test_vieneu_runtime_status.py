"""Windows frozen TTS: status/synth/reset stay outside the UI process torch path."""
from __future__ import annotations

import builtins
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pipeline.tts.engines import vieneu
from pipeline.core import accel


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
            mock.patch("pipeline.tts.engines.vieneu_frozen", fake_frozen, create=True),
            mock.patch("builtins.__import__", side_effect=guarded_import),
        ):
            with vieneu._lock:
                vieneu._client = object()
                vieneu._load_state = "ready"
            vieneu.reset_client()
        self.assertIsNone(vieneu._client)
        self.assertEqual(vieneu._load_state, "cold")
        shutdown.assert_called_once()

    def test_non_frozen_synthesis_serializes_shared_client(self):
        active = 0
        max_active = 0
        guard = threading.Lock()

        class Client:
            def infer(self, _text, **_kwargs):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return [0.0]

            def save(self, _audio, path):
                Path(path).write_bytes(b"RIFF")

        errors: list[BaseException] = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(sys, "frozen", False, create=True),
            mock.patch.object(vieneu, "get_client", return_value=Client()),
        ):
            def run(index: int) -> None:
                try:
                    vieneu.synthesize("xin chào", "vn:Phạm Tuyên", Path(td) / f"{index}.wav")
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1)

    def test_mps_tts_uses_one_shared_client_worker(self):
        with mock.patch.object(accel, "preferred_torch_device", return_value="mps"):
            self.assertEqual(accel._tts_vram_hard_cap(), 1)


class OnnxSnapshotPathTest(unittest.TestCase):
    def make_snapshot(self, root):
        root = Path(root)
        snapshot = root / 'snapshots' / 'revision' / 'onnx_update'
        snapshot.mkdir(parents=True)
        for name, blob, content in [
            ('vieneu_prefill.onnx', '8b/model', b'graph'),
            ('vieneu_backbone_shared.data', '5c/data', b'weights'),
        ]:
            source = root / 'blobs' / blob
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
            (snapshot / name).symlink_to(source)
        return snapshot

    def test_materializes_split_blob_symlinks_and_reuses_hardlinks(self):
        from pipeline.tts.engines.vieneu_frozen import _materialize_onnx_files
        with tempfile.TemporaryDirectory() as root:
            snapshot = self.make_snapshot(root)
            files = ['vieneu_prefill.onnx', 'vieneu_backbone_shared.data']
            target = _materialize_onnx_files(snapshot, files)
            for name in files:
                self.assertFalse((target / name).is_symlink())
                self.assertEqual((target / name).resolve().parent, target.resolve())
                self.assertTrue((snapshot / name).is_symlink())
                self.assertTrue((target / name).samefile(snapshot / name))
            with mock.patch('os.link', side_effect=AssertionError('cache must be reused')):
                self.assertEqual(_materialize_onnx_files(snapshot, files), target)

    def test_copy_fallback_and_parallel_publication(self):
        from concurrent.futures import ThreadPoolExecutor
        from pipeline.tts.engines.vieneu_frozen import _materialize_onnx_files
        with tempfile.TemporaryDirectory() as root:
            snapshot = self.make_snapshot(root)
            files = ['vieneu_prefill.onnx', 'vieneu_backbone_shared.data']
            with mock.patch('os.link', side_effect=OSError('hardlinks unavailable')), ThreadPoolExecutor(4) as pool:
                targets = list(pool.map(lambda _: _materialize_onnx_files(snapshot, files), range(4)))
            self.assertEqual(len(set(targets)), 1)
            self.assertEqual((targets[0] / files[1]).read_bytes(), b'weights')
            self.assertFalse(any(p.name.startswith('.pending-') for p in targets[0].iterdir()))

    def test_regular_local_bundle_does_not_need_materialization(self):
        from pipeline.tts.engines.vieneu_frozen import _materialize_onnx_files
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'model.onnx').write_bytes(b'graph')
            self.assertEqual(_materialize_onnx_files(directory, ['model.onnx', 'optional.json']), directory)
            with self.assertRaises(ValueError):
                _materialize_onnx_files(directory, ['../outside'])

    def test_real_onnx_session_loads_external_data_from_materialized_bundle(self):
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError:
            self.skipTest('ONNX Runtime is not installed')
        from pipeline.tts.engines.vieneu_frozen import _materialize_onnx_files
        # ONNX IR8/opset13: Identity(x -> y), float[1] initializer x stored
        # externally in vieneu_backbone_shared.data. No onnx dependency/download.
        graph = bytes.fromhex(
            '08083a610a100a017812017922084964656e74697479120865787465726e616c'
            '2a32080110014201786a270a086c6f636174696f6e121b7669656e65755f6261'
            '636b626f6e655f7368617265642e646174617001620f0a0179120a0a080801'
            '12040a0208014202100d'
        )
        with tempfile.TemporaryDirectory() as root:
            snapshot = self.make_snapshot(root)
            (snapshot / 'vieneu_prefill.onnx').resolve().write_bytes(graph)
            (snapshot / 'vieneu_backbone_shared.data').resolve().write_bytes(np.array([2], np.float32).tobytes())
            target = _materialize_onnx_files(snapshot, ['vieneu_prefill.onnx', 'vieneu_backbone_shared.data'])
            session = ort.InferenceSession(str(target / 'vieneu_prefill.onnx'), providers=['CPUExecutionProvider'])
            np.testing.assert_array_equal(session.run(None, {})[0], [2])

    def test_fetch_hook_is_idempotent_and_included_in_worker(self):
        from types import SimpleNamespace
        from pipeline.tts.engines import vieneu_frozen
        class Engine:
            _fetch = staticmethod(mock.Mock())
        original = Engine._fetch
        original._zm_ai_onnx_paths = False
        module = SimpleNamespace(OnnxV3LiteEngine=Engine)
        with tempfile.TemporaryDirectory() as root:
            original.return_value = self.make_snapshot(root)
            with mock.patch.dict(sys.modules, {'vieneu._v3_turbo_engine.onnx_runtime_lite': module}):
                vieneu_frozen._enable_onnx_snapshot_paths()
                patched = Engine._fetch
                vieneu_frozen._enable_onnx_snapshot_paths()
                self.assertIs(Engine._fetch, patched)
                directory = Engine._fetch('repo', ['vieneu_prefill.onnx', 'vieneu_backbone_shared.data'], 'onnx_update')
                self.assertFalse((directory / 'vieneu_prefill.onnx').is_symlink())
                original.assert_called_once_with('repo', ['vieneu_prefill.onnx', 'vieneu_backbone_shared.data'], 'onnx_update')
        self.assertTrue(vieneu_frozen._WORKER_SCRIPT.startswith(vieneu_frozen._ONNX_CACHE_SCRIPT))
        compile(vieneu_frozen._WORKER_SCRIPT, '<vieneu-worker>', 'exec')


if __name__ == "__main__":
    unittest.main()
