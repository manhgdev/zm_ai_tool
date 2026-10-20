"""Provision the same LaMa model used by desktop release builds."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from urllib.request import urlopen

MODEL_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
MODEL_PATH = Path(__file__).resolve().parents[1] / "backend/pipeline/assets/lama_fp32.onnx"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(destination: Path = MODEL_PATH) -> Path:
    if destination.is_file() and _sha256(destination) == MODEL_SHA256:
        print("Model LaMa đã sẵn sàng / LaMa model is ready", flush=True)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    print("Đang tải model LaMa (~208 MB)... / Downloading LaMa model (~208 MB)...", flush=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".lama-", suffix=".part", delete=False) as target:
            temporary = Path(target.name)
            with urlopen(MODEL_URL, timeout=120) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    target.write(chunk)
        if _sha256(temporary) != MODEL_SHA256:
            raise RuntimeError("Model LaMa tải về không hợp lệ; chạy lại setup. / Invalid LaMa download; rerun setup.")
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


if __name__ == "__main__":
    ensure_model()
