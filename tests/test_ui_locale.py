import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import ui_preferences as preferences


class LocaleTests(unittest.TestCase):
    def test_new_setup_language_overrides_old_preference_but_app_choice_sticks(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(preferences, '_PREFERENCES_PATH', Path(raw) / 'prefs.json'), patch.object(preferences, '_installer_locale', return_value='vi'), patch.object(preferences, '_installer_locale_revision', return_value='new-install'):
            preferences._PREFERENCES_PATH.write_text(json.dumps({'locale': 'en', 'installerLocaleRevision': 'old-install'}))
            self.assertEqual(preferences.load_ui_preferences()['locale'], 'vi')
            preferences.save_ui_preferences(locale='en')
            self.assertEqual(preferences.load_ui_preferences()['locale'], 'en')
            with patch.object(preferences, '_installer_locale_revision', return_value='reinstall'):
                self.assertEqual(preferences.load_ui_preferences()['locale'], 'vi')

    def test_setup_locale_seeds_first_run_but_not_explicit_preference(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(preferences, '_PREFERENCES_PATH', Path(raw) / 'prefs.json'), patch.object(preferences, '_installer_locale', return_value='vi'):
            self.assertEqual(preferences.load_ui_preferences()['locale'], 'vi')
            preferences.save_ui_preferences(locale='en')
            self.assertEqual(preferences.load_ui_preferences()['locale'], 'en')

    def test_old_inno_registry_selection_is_supported(self):
        registry = SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=2, KEY_WOW64_64KEY=4,
                                   OpenKey=MagicMock(side_effect=[OSError('missing new key'), MagicMock()]),
                                   QueryValueEx=MagicMock(return_value=('vi', 1)))
        with patch.object(sys, 'platform', 'win32'), patch.dict(sys.modules, {'winreg': registry}):
            self.assertEqual(preferences._installer_locale(), 'vi')


if __name__ == '__main__':
    unittest.main()
