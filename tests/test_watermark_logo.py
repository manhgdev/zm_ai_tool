import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.ocr.logo import _branding_token, _corner_graphic_masks, _is_corner, _moving_branding_tracks, pick_logo_detection


class WatermarkDetectionTests(unittest.TestCase):
    def _hit(self, text: str, box: tuple[int, int, int, int], sample: int, confidence: float = 0.95):
        return {
            "text": text,
            "box": box,
            "polygon": [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
            "confidence": confidence,
            "sample": sample,
        }

    def test_brand_tokens_and_corner_gate_are_aspect_ratio_independent(self):
        self.assertEqual(_branding_token("Veo 3.1"), "veo")
        self.assertEqual(_branding_token("@creator_123"), "@handle")
        self.assertEqual(_branding_token("UID: 123456"), "uid")
        self.assertIsNone(_branding_token("A beautiful Klingon scene"))
        self.assertIsNone(_branding_token("Klingon"))
        for cx, cy in ((0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)):
            self.assertTrue(_is_corner(cx, cy))
        self.assertFalse(_is_corner(0.9, 0.5))

    def test_video_requires_two_confirmed_samples(self):
        one = [[self._hit("Veo", (850, 30, 940, 65), 0)]]
        self.assertIsNone(pick_logo_detection(one, 1000, 600))
        two = [
            [self._hit("Veo", (850, 30, 940, 65), 0)],
            [self._hit("Veo", (852, 31, 942, 66), 1)],
        ]
        result = pick_logo_detection(two, 1000, 600)
        self.assertIsNotNone(result)
        self.assertEqual(result["token"], "veo")
        self.assertEqual(result["kind"], "static")
        self.assertIn("polygon", result)

    def test_ordinary_corner_caption_is_not_selected(self):
        samples = [
            [self._hit("SALE", (850, 30, 940, 65), 0)],
            [self._hit("SALE", (850, 30, 940, 65), 1)],
        ]
        self.assertIsNone(pick_logo_detection(samples, 1000, 600))

    def test_moving_brand_track_requires_two_samples(self):
        samples = [
            [self._hit("Kling", (20, 30, 110, 64), 0)],
            [self._hit("Kling", (880, 500, 970, 534), 1)],
        ]
        tracks = _moving_branding_tracks(samples, [0.0, 1.0], 1000, 600)
        self.assertTrue(tracks)
        self.assertLessEqual(tracks[0]["start"], 0.0)
        self.assertGreaterEqual(tracks[-1]["end"], 1.0)

    def test_corner_icon_requires_repeated_video_samples(self):
        icon0 = self._hit("✦", (900, 520, 930, 550), 0)
        icon1 = self._hit("✦", (901, 521, 931, 551), 1)
        self.assertFalse(_corner_graphic_masks([[icon0]], 1000, 600))
        self.assertTrue(_corner_graphic_masks([[icon0], [icon1]], 1000, 600))


class InpaintMaskTests(unittest.TestCase):
    def test_inpaint_preserves_pixels_outside_mask(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV is supplied by the managed AI runtime")
        from pipeline.export.cover_mask import _inpaint_region

        image = np.zeros((80, 120, 3), dtype=np.uint8)
        image[:, :] = (30, 80, 140)
        image[20:32, 90:108] = (255, 255, 255)
        before = image.copy()
        _inpaint_region(image, (88, 18, 110, 34))
        self.assertTrue(np.array_equal(image[:10], before[:10]))
        self.assertFalse(np.array_equal(image[20:32, 90:108], before[20:32, 90:108]))


if __name__ == "__main__":
    unittest.main()
