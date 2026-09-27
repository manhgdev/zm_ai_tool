"""Tests for _flow_control_selected_from_attrs.

Regression: Omni 1.1 Flash duration buttons expose
  ariaChecked='false' (the *string* "false", not empty)
and the helper must NOT treat that as selected.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.flow.service import _flow_control_selected_from_attrs  # noqa: E402


class FlowControlSelectedFromAttrsTests(unittest.TestCase):
    def _call(self, *, aria_selected=None, aria_checked=None, data_state=None, aria_pressed=None):
        return _flow_control_selected_from_attrs(
            aria_selected=aria_selected,
            aria_checked=aria_checked,
            data_state=data_state,
            aria_pressed=aria_pressed,
        )

    # --- regressions from the Omni 1.1 Flash FLOW_SETTING_MISMATCH incident ---
    def test_aria_checked_string_false_is_not_selected(self):
        """ariaChecked='false' (string) must not be treated as selected."""
        self.assertFalse(self._call(aria_checked="false"))

    def test_all_empty_is_not_selected(self):
        self.assertFalse(self._call(aria_selected="", aria_checked="", aria_pressed=""))

    def test_mat_button_toggle_unselected_attrs(self):
        """Exact attr snapshot from the failing job controls[] debug output."""
        self.assertFalse(self._call(
            aria_selected="",
            aria_checked="false",
            aria_pressed="",
            data_state=None,
        ))

    # --- positive cases ---
    def test_aria_checked_true_is_selected(self):
        self.assertTrue(self._call(aria_checked="true"))

    def test_aria_selected_true_is_selected(self):
        self.assertTrue(self._call(aria_selected="true"))

    def test_aria_pressed_true_is_selected(self):
        self.assertTrue(self._call(aria_pressed="true"))

    def test_data_state_on_is_selected(self):
        self.assertTrue(self._call(data_state="on"))

    def test_data_state_checked_is_selected(self):
        self.assertTrue(self._call(data_state="checked"))


if __name__ == "__main__":
    unittest.main()
