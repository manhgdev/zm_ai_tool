"""CI smoke: the pinned bundle stack (numpy/cv2/onnxruntime) must run the real LaMa model.

Usage: python scripts/lama_smoke.py [path/to/lama_fp32.onnx]
"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
model = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "backend/pipeline/assets/lama_fp32.onnx"

import numpy as np  # noqa: E402

from pipeline import logo_remover as lr  # noqa: E402

image = np.random.default_rng(0).integers(0, 255, (360, 640, 3), dtype=np.uint8)
mask = np.zeros(image.shape[:2], np.uint8)
mask[300:340, 520:620] = 255
out = lr._lama_fill_with_mask(image, mask, lr.LogoRemovalSettings(lama_model_path=str(model)))
assert out.shape == image.shape and out.dtype == np.uint8
assert not np.array_equal(out[300:340, 520:620], image[300:340, 520:620]), "LaMa did not change the masked area"
assert np.array_equal(out[:100], image[:100]), "LaMa changed pixels far from the mask"
import cv2  # noqa: E402
import onnxruntime  # noqa: E402

print("LaMa smoke OK", np.__version__, cv2.__version__, onnxruntime.__version__, onnxruntime.get_available_providers())
