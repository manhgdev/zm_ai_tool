"""Desktop browser adapter for Flow.

Flow's upstream helper defaults to Playwright's bundled Chromium.  Desktop
packages do not ship that 200+ MB runtime, so use the user's installed Google
Chrome instead and keep the persistent account profile inside ZM AI TOOL.
"""
from __future__ import annotations

import os
import asyncio
import subprocess
import threading
import sys
import signal
import shutil
from pathlib import Path
from typing import Any

from pipeline.core.network import network_available


def _capture_active_window() -> Any:
    """Return an opaque token representing the current foreground window.

    macOS   -> {name, bundle} dict via System Events
    Windows -> HWND integer via ctypes user32
    Other   -> None (no-op)
    """
    # ponytail: stdlib-only on both platforms; no extra dependency.
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 "tell application \"System Events\"\n"
                 "  set p to first process whose frontmost is true\n"
                 "  return (name of p) & \"||\" & (bundle identifier of p)\n"
                 "end tell"],
                capture_output=True, text=True, timeout=3,
            )
            raw = result.stdout.strip()
            if "||" in raw:
                pname, bundle = raw.split("||", 1)
                return {"name": pname.strip(), "bundle": bundle.strip()}
            return None
        except Exception:
            return None
    elif sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            return int(hwnd) if hwnd else None
        except Exception:
            return None
    return None


def _restore_active_window(token: Any) -> None:
    """Bring the previously captured window back to the foreground (best-effort)."""
    if not token:
        return
    if sys.platform == "darwin":
        name = token.get("name", "") if isinstance(token, dict) else ""
        bundle = token.get("bundle", "") if isinstance(token, dict) else str(token)
        # Primary: set frontmost directly via System Events — works for Electron
        # and any app that does not implement the AppleScript activate handler.
        restored = False
        if name:
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f"tell application \"System Events\"\n"
                     f"  set frontmost of first process whose name is \"{name}\" to true\n"
                     f"end tell"],
                    capture_output=True, timeout=5,
                )
                restored = True
            except Exception:
                pass
        # Fallback: classic activate by bundle id
        if not restored and bundle:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application id "{bundle}" to activate'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
    elif sys.platform == "win32":
        try:
            import ctypes
            hwnd = token
            # SW_RESTORE = 9: unminimise if needed, then bring to front.
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

FLOW_BASE_URL = "https://flow.google.com"
_profile_guard = threading.Lock()
_profile_locks: dict[str, Any] = {}
_visible_window_guard = threading.Lock()
_visible_launch_guard = threading.Lock()
_visible_window_index = 0


def _visible_window_geometry(
    index: int,
    screen_width: int,
    screen_height: int,
) -> tuple[int, int, int, int]:
    """Return compact columns anchored on the right, fitting the work area.

    Each group fills one vertical column from top to bottom. Later groups move
    one column left, keeping every account window visible and clickable.
    """
    # Leave room for Chrome's native title bar/frame; launch dimensions are
    # outer-window hints and can grow slightly on macOS.
    gap = max(16, min(28, screen_height // 36))
    columns = max(1, screen_width // 500)
    horizontal_gap = min(gap, max(0, screen_width - columns * 500) // max(1, columns - 1))
    width = (screen_width - horizontal_gap * (columns - 1)) // columns
    # Keep readable cells on larger work areas instead of limiting every screen to two rows.
    rows = max(1, (screen_height + gap) // (420 + gap))
    height = (screen_height - gap * (rows - 1)) // rows
    width = min(width, screen_width)
    height = min(height, screen_height)
    row = index % rows
    # ponytail: wrap full grids; overlap is preferable to off-screen windows.
    group = (index // rows) % columns
    x = 0 if group == columns - 1 else screen_width - width - group * (width + horizontal_gap)
    y = row * (height + gap)
    return x, y, width, height


def _visible_window_args(*, fullscreen: bool = False) -> list[str]:
    """Start compactly; the page then supplies the real monitor work area."""
    # Do not create Tk/AppKit from a Flow worker thread.  macOS aborts the
    # process when Tk initializes Cocoa off the main thread (SIGABRT/-6).
    return [
        "--window-position=0,0",
        *( ["--start-maximized"] if fullscreen else ["--window-size=500,300"] ),
        "--new-window",
    ]


def profile_lock(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _profile_guard:
        return _profile_locks.setdefault(key, threading.Lock())


def chrome_executable() -> Path | None:
    """Locate Chrome from system installs, PATH, or known automation caches."""
    candidates: list[Path]
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    elif sys.platform == "win32":
        roots = [Path(os.environ.get("PROGRAMFILES", "")), Path(os.environ.get("PROGRAMFILES(X86)", "")), Path(os.environ.get("LOCALAPPDATA", ""))]
        candidates = [root / "Google/Chrome/Application/chrome.exe" for root in roots if str(root)]
    else:
        candidates = [Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"), Path("/opt/google/chrome/chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")]
        candidates.extend(Path(found) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if (found := shutil.which(name)))
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        puppeteer_cache = Path(os.environ.get("PUPPETEER_CACHE_DIR") or cache / "puppeteer")
        playwright_cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or cache / "ms-playwright")
        candidates.extend(sorted(puppeteer_cache.glob("chrome/linux-*/chrome-linux64/chrome"), reverse=True))
        candidates.extend(sorted(playwright_cache.glob("chromium-*/chrome-linux/chrome"), reverse=True))
        candidates.extend(sorted(playwright_cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True))
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


class BrowserManager:
    """Subset of flow-py's browser contract backed by installed Google Chrome."""

    def __init__(self, *, headless: bool, profile_dir: Path, slow_mo: int = 0, google_login_compat: bool = False, fullscreen: bool = False, page_zoom: float = 0.4) -> None:
        self.cdp_url = None  # flow-py checks this optional upstream attribute.
        # Old clients can still request visible Chrome. A headless Linux VPS
        # has no window server; normalize here for every Flow caller.
        self.headless = headless or (sys.platform.startswith('linux')
                                    and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')))
        self.profile_dir = Path(profile_dir)
        self.slow_mo = slow_mo
        self.google_login_compat = google_login_compat
        self.fullscreen = fullscreen
        self.page_zoom = page_zoom
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._zoom_applied = False
        self._window_placed = False
        self._profile_lock = profile_lock(self.profile_dir)
        self._owns_profile = False
        # ponytail: captured only for visible windows; restored in stop().
        self._prior_app: Any = None

    async def start(self) -> "BrowserManager":
        executable = chrome_executable()
        if executable is None:
            raise RuntimeError("FLOW_CHROME_REQUIRED: Google Chrome was not found. Install Google Chrome, then connect again.")
        # All callers (jobs, login, credit sync, project recovery) pass here.
        # Workers own the cancellable wait; synchronous API calls fail promptly.
        if not await asyncio.to_thread(network_available):
            raise RuntimeError('FLOW_NETWORK_UNAVAILABLE: Internet connection is unavailable')
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        while not self._profile_lock.acquire(blocking=False):
            await asyncio.sleep(0.1)
        self._owns_profile = True
        visible_launch_owned = False
        if not self.headless:
            self._prior_app = _capture_active_window()
            while not _visible_launch_guard.acquire(blocking=False):
                await asyncio.sleep(0.05)
            visible_launch_owned = True
        try:
            self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
            visible_args = [] if self.headless else _visible_window_args(fullscreen=self.fullscreen)
            launch_args = ["--lang=en-US", "--disable-extensions"]
            if self.google_login_compat:
                launch_args.append("--disable-blink-features=AutomationControlled")
            launch_args.extend(["--disable-infobars", *visible_args])
            # Linux root cannot use Chromium's sandbox. Keep it enabled for all
            # non-root sessions; do not retry arbitrary failures unsandboxed.
            sandbox = not (sys.platform.startswith("linux") and getattr(os, "geteuid", lambda: -1)() == 0)
            self._ctx = await asyncio.wait_for(
                self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    executable_path=str(executable),
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    viewport={"width": 1440, "height": 900} if self.headless else None,
                    no_viewport=not self.headless,
                    accept_downloads=True,
                    locale="en-US",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    chromium_sandbox=sandbox,
                    args=launch_args,
                ),
                timeout=60,
            )
            if visible_args and visible_launch_owned:
                _visible_launch_guard.release()
                visible_launch_owned = False
            # Observe requests through UIInterceptor; do not replace page APIs.
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                "FLOW_BROWSER_START_TIMEOUT: Google Chrome did not start within 60 seconds"
            ) from exc
        except BaseException:
            await self.stop()
            raise
        finally:
            if visible_launch_owned:
                _visible_launch_guard.release()
        return self

    async def stop(self) -> None:
        if self._ctx:
            try:
                await self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._ctx = None
        self._pw = None
        self._page = None
        if self._owns_profile:
            self._owns_profile = False
            self._profile_lock.release()
        if not self.headless and self._prior_app:
            _restore_active_window(self._prior_app)
            self._prior_app = None

    def kill_now(self) -> None:
        """Synchronously terminate Chrome processes owned by this profile.

        Cancellation can arrive from the API thread while Playwright is
        blocked in another event loop, so awaiting ``stop()`` is not enough.
        The runtime profile is unique per job, making the command-line match
        safe and preventing unrelated Chrome accounts from being killed.
        """
        profile = str(self.profile_dir.resolve())
        try:
            import psutil  # type: ignore
            for proc in psutil.process_iter(["pid", "cmdline"]):
                if proc.pid == os.getpid():
                    continue
                try:
                    cmdline = " ".join(proc.info.get("cmdline") or [])
                    if profile not in cmdline:
                        continue
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=0.5)
                    except psutil.TimeoutExpired:
                        proc.kill()
                except (psutil.Error, OSError, PermissionError):
                    continue
        except ImportError:
            return

    @property
    def context(self) -> BrowserContext:
        if not self._ctx:
            raise RuntimeError("BrowserManager not started")
        return self._ctx

    async def page(self) -> Page:
        if self._page and not self._page.is_closed():
            if not self.headless:
                await self._apply_visible_zoom(self._page)
            return self._page
        pages = self.context.pages
        self._page = pages[0] if pages else await self.context.new_page()
        if not self.headless:
            if not self._window_placed:
                await self._place_visible_window(self._page)
            await self._page.bring_to_front()
            await self._apply_visible_zoom(self._page)
        return self._page

    async def _place_visible_window(self, page: Page) -> None:
        if self.fullscreen:
            session = await self.context.new_cdp_session(page)
            try:
                window_id = (await session.send('Browser.getWindowForTarget'))['windowId']
                await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'maximized'}})
                self._window_placed = True
            finally:
                await session.detach()
            return
        global _visible_window_index
        area = await page.evaluate("""() => ({
            left: screen.availLeft || 0, top: screen.availTop || 0,
            width: screen.availWidth, height: screen.availHeight
        })""")
        with _visible_window_guard:
            index = _visible_window_index
            _visible_window_index += 1
        x, y, width, height = _visible_window_geometry(index, area['width'], area['height'])
        session = await self.context.new_cdp_session(page)
        try:
            window = await session.send('Browser.getWindowForTarget')
            window_id = window['windowId']
            await session.send('Browser.setWindowBounds', {
                'windowId': window_id, 'bounds': {'windowState': 'normal'},
            })
            await session.send('Browser.setWindowBounds', {
                'windowId': window_id, 'bounds': {
                    'left': area['left'] + x, 'top': area['top'] + y,
                    'width': width, 'height': height,
                },
            })
            # Chrome may enforce a larger native minimum; clamp its actual frame.
            actual = (await session.send('Browser.getWindowBounds', {'windowId': window_id}))['bounds']
            await session.send('Browser.setWindowBounds', {
                'windowId': window_id, 'bounds': {
                    'left': area['left'] + max(0, min(x, area['width'] - actual['width'])),
                    'top': area['top'] + max(0, min(y, area['height'] - actual['height'])),
                },
            })
            self._window_placed = True
        finally:
            await session.detach()

    async def _apply_visible_zoom(self, page: Page) -> None:
        if self._zoom_applied:
            return
        # Playwright key events do not trigger Chrome's native zoom shortcuts.
        # Use Chrome's own settings page; closing it preserves the Flow tab.
        settings_page = await self.context.new_page()
        try:
            await settings_page.goto('chrome://settings/appearance', wait_until='domcontentloaded')
            await settings_page.evaluate("""() => new Promise((resolve, reject) => {
                    chrome.settingsPrivate.setDefaultZoom(%s, () => {
                    if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
                    else resolve();
                });
            })""" % self.page_zoom)
            self._zoom_applied = True
        finally:
            await settings_page.close()
