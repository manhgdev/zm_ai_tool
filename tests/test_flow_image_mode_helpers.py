from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.flow.service import (  # noqa: E402
    _image_input_type_for_mode,
    _match_model_choice,
    _patch_batch_generate_image_inputs,
    _prompt_with_reference_strength,
)


class ImageModeHelpersTests(unittest.TestCase):
    def test_input_types(self):
        self.assertEqual(_image_input_type_for_mode("edit"), "IMAGE_INPUT_TYPE_BASE_IMAGE")
        self.assertEqual(_image_input_type_for_mode("reference"), "IMAGE_INPUT_TYPE_REFERENCE")
        self.assertEqual(_image_input_type_for_mode("text"), "IMAGE_INPUT_TYPE_REFERENCE")

    def test_prompt_strength_prefixes(self):
        high = _prompt_with_reference_strength("a cat", "edit", 90)
        low = _prompt_with_reference_strength("a cat", "reference", 20)
        self.assertIn("a cat", high)
        self.assertIn("preserving identity", high.lower())
        self.assertIn("loosely inspired", low.lower())
        plain = _prompt_with_reference_strength("a cat", "text", 90)
        self.assertEqual(plain, "a cat")

    def test_patch_injects_and_types_inputs(self):
        body = {"requests": [{"structuredPrompt": {"parts": [{"text": "hi"}]}}]}
        patched = _patch_batch_generate_image_inputs(
            body, mode="edit", media_names=["media-1", "media-2"],
        )
        inputs = patched["requests"][0]["imageInputs"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["imageInputType"], "IMAGE_INPUT_TYPE_BASE_IMAGE")
        self.assertEqual(inputs[0]["name"], "media-1")

        body2 = {
            "requests": [{
                "imageInputs": [{"mediaName": "a"}, {"mediaName": "b"}],
            }],
        }
        patched2 = _patch_batch_generate_image_inputs(
            body2, mode="reference", media_names=[],
        )
        for item in patched2["requests"][0]["imageInputs"]:
            self.assertEqual(item["imageInputType"], "IMAGE_INPUT_TYPE_REFERENCE")
            self.assertEqual(item["name"], item["mediaName"])


    def test_model_choice_does_not_pick_lite_for_full_model(self):
        self.assertFalse(_match_model_choice("Nano Banana 2", "🍌 Nano Banana 2 Lite"))
        self.assertTrue(_match_model_choice("Nano Banana 2", "🍌 Nano Banana 2"))
        self.assertTrue(_match_model_choice("Nano Banana 2 Lite", "Nano Banana 2 Lite"))
        self.assertFalse(_match_model_choice("Veo 3.1 - Fast", "Veo 3.1 - Lite"))


if __name__ == "__main__":
    unittest.main()
