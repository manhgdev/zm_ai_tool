"""Desktop window focus helpers for external auth flows."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

_restore_callback: Callable[[], None] | None = None


def register_restore_callback(callback: Callable[[], None]) -> None:
    """Register the GUI-layer restore callback without importing pywebview here."""
    global _restore_callback
    _restore_callback = callback


def request_desktop_foreground() -> None:
    """Keep the desktop app visible after an external browser login closes."""
    if os.environ.get("ZM_AI_TOOL_DESKTOP") != "1":
        return
    if _restore_callback is not None:
        try:
            _restore_callback()
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    (
                        'tell application "System Events" to set frontmost of '
                        f"(first process whose unix id is {os.getpid()}) to true"
                    ),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass
        return
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = kernel32.GetCurrentProcessId()
        SW_RESTORE = 9
        SW_SHOW = 5

        def enum_proc(hwnd, _lparam):  # noqa: ANN001
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not user32.IsWindowVisible(hwnd):
                return True
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.SetForegroundWindow(hwnd)
            return False

        user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_proc), 0)
    except Exception:
        pass
