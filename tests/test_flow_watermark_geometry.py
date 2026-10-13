import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
try:
    import cv2
    import numpy as np
    from pipeline import logo_remover as lr
except Exception:  # Mac dev python without the AI stack
    lr = None


@unittest.skipIf(lr is None, 'needs numpy/cv2')
class FlowWatermarkGeometryTest(unittest.TestCase):
    def test_sparkle_is_found_at_fixed_pixel_margin_for_every_ratio(self):
        # Real Flow 1K images: sparkle centre ~94px from the right and ~99px from the bottom for all ratios.
        templ = cv2.resize(lr._load_flow_template(), (55, 58), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        for w, h in ((1376, 768), (768, 1376), (1200, 896), (896, 1200), (1024, 1024)):
            with self.subTest(size=(w, h)):
                rng = np.random.default_rng(1)
                img = cv2.GaussianBlur(rng.integers(40, 120, (h, w, 3), dtype=np.uint8), (0, 0), 2)
                x, y = w - 94 - 27, h - 99 - 29
                for dx, strength in ((-60, 1.0), (0, 0.45)):  # stronger look-alike 60px away, faint real logo
                    roi = img[y:y + 58, x + dx:x + dx + 55].astype(np.float32)
                    img[y:y + 58, x + dx:x + dx + 55] = (roi + (235 - roi) * strength * templ[..., None]).astype(np.uint8)
                mask, _bbox, _score, detected = lr._flow_template_match(img)
                ys, xs = np.where(mask > 0)
                self.assertTrue(detected)
                self.assertLess(abs((w - xs.mean()) - 94), 10)
                self.assertLess(abs((h - ys.mean()) - 99), 10)


if __name__ == '__main__':
    unittest.main()
