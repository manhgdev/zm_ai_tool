"""Guard: frozen desktop must never import torch into the UI/API process."""
from __future__ import annotations

import builtins
from unittest import mock


def test_reset_client_frozen_never_imports_torch(monkeypatch):
    import pipeline.tts.engines.vieneu as vieneu

    monkeypatch.setattr(vieneu.sys, "frozen", True, raising=False)
    shutdown = mock.Mock()
    fake_frozen = mock.Mock()
    fake_frozen.shutdown_all_workers = shutdown

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "torch" or (isinstance(name, str) and name.startswith("torch.")):
            raise AssertionError("frozen reset_client must not import torch")
        if name == "pipeline.tts.engines.vieneu_frozen" or (
            name == "pipeline.tts.engines" and fromlist and "vieneu_frozen" in fromlist
        ):
            return mock.Mock(vieneu_frozen=fake_frozen)
        if name.endswith("vieneu_frozen") or name == ".vieneu_frozen":
            return fake_frozen
        return real_import(name, globals, locals, fromlist, level)

    with vieneu._lock:
        vieneu._client = object()
        vieneu._load_state = "ready"

    with mock.patch.object(vieneu, "__import__", create=True):
        pass

    # Patch the relative import used inside reset_client.
    import pipeline.tts.engines as engines_pkg

    monkeypatch.setattr(engines_pkg, "vieneu_frozen", fake_frozen, raising=False)
    with mock.patch.dict("sys.modules", {"pipeline.tts.engines.vieneu_frozen": fake_frozen}):
        with mock.patch("builtins.__import__", side_effect=guarded_import):
            # Relative `from . import vieneu_frozen` resolves via sys.modules.
            vieneu.reset_client()

    assert vieneu._client is None
    assert vieneu._load_state == "cold"
    shutdown.assert_called_once()
