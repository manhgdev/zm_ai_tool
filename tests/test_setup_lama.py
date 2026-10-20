"""Run: python tests/test_setup_lama.py (no model download required)."""
import hashlib
import io
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_lama


def check_setup_lama():
    payload = b"verified model data"
    with tempfile.TemporaryDirectory() as directory, patch.object(
        setup_lama, "MODEL_SHA256", hashlib.sha256(payload).hexdigest()
    ):
        destination = Path(directory) / "assets/lama.onnx"
        # A new model is published only after its complete contents are verified.
        with patch.object(setup_lama, "urlopen", return_value=io.BytesIO(payload)) as download:
            assert setup_lama.ensure_model(destination) == destination
            assert download.call_count == 1
        assert destination.read_bytes() == payload
        assert not list(destination.parent.glob("*.part"))
        # Rerunning setup reuses a valid model without network access.
        with patch.object(setup_lama, "urlopen", side_effect=AssertionError("Unexpected download")):
            setup_lama.ensure_model(destination)
        # A corrupt transfer must preserve the previous file and remove partial data.
        destination.write_bytes(b"previous file")
        with patch.object(setup_lama, "urlopen", return_value=io.BytesIO(b"truncated")):
            try:
                setup_lama.ensure_model(destination)
            except RuntimeError as exc:
                assert "Invalid LaMa download" in str(exc)
            else:
                raise AssertionError("Corrupt download was accepted")
        assert destination.read_bytes() == b"previous file"
        assert not list(destination.parent.glob("*.part"))
        # Network errors also leave the previous file intact.
        with patch.object(setup_lama, "urlopen", side_effect=OSError("offline")):
            try:
                setup_lama.ensure_model(destination)
            except OSError:
                pass
            else:
                raise AssertionError("Network failure was ignored")
        assert destination.read_bytes() == b"previous file"
        assert not list(destination.parent.glob("*.part"))
        # A later successful retry replaces that file atomically.
        with patch.object(setup_lama, "urlopen", return_value=io.BytesIO(payload)):
            setup_lama.ensure_model(destination)
        assert destination.read_bytes() == payload
    print("LaMa setup checks passed")


if __name__ == "__main__":
    check_setup_lama()
