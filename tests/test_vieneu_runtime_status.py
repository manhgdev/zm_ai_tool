import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.tts.engines import vieneu


class RuntimeStatusTest(unittest.TestCase):
    def test_frozen_availability_checks_runtime_not_gui_import_path(self):
        with patch.object(sys, 'frozen', True, create=True), patch.dict(os.environ, {'VIENEU_DISABLED': ''}), patch('pipeline.core.system_check.probe._runtime_mod_ok', return_value=(True, 'ok')) as probe, patch('importlib.util.find_spec', side_effect=AssertionError('GUI must not probe package')):
            self.assertTrue(vieneu.available())
            probe.assert_called_once_with('vieneu')

    def test_missing_runtime_is_not_reported_installed(self):
        with patch.object(sys, 'frozen', True, create=True), patch.dict(os.environ, {'VIENEU_DISABLED': ''}), patch('pipeline.core.system_check.probe._runtime_mod_ok', return_value=(False, 'missing')):
            self.assertFalse(vieneu.available())
