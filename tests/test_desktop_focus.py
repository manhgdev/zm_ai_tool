import ctypes
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core import desktop_window


class DesktopFocusTests(unittest.TestCase):
    def test_auth_close_only_focuses_visible_non_minimized_window(self):
        for minimized in (False, True):
            user32 = Mock()
            user32.IsWindowVisible.return_value = True
            user32.IsIconic.return_value = minimized
            user32.GetWindowThreadProcessId.side_effect = lambda hwnd, owner: setattr(owner._obj, 'value', 42)
            user32.EnumWindows.side_effect = lambda callback, arg: callback(123, arg)
            dll = SimpleNamespace(user32=user32, kernel32=SimpleNamespace(GetCurrentProcessId=lambda: 42))
            callback = Mock()
            with patch.dict(os.environ, {'ZM_AI_TOOL_DESKTOP': '1'}), patch.object(sys, 'platform', 'win32'), patch.object(ctypes, 'windll', dll, create=True), patch.object(ctypes, 'WINFUNCTYPE', lambda *args: lambda fn: fn, create=True), patch.object(desktop_window, '_restore_callback', callback):
                desktop_window.request_desktop_foreground()
            callback.assert_not_called()
            user32.ShowWindow.assert_not_called()
            self.assertEqual(user32.SetForegroundWindow.call_count, 0 if minimized else 1)


if __name__ == '__main__':
    unittest.main()
