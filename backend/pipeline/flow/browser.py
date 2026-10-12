"""Desktop browser adapter for Flow.

Flow's upstream helper defaults to Playwright's bundled Chromium.  Desktop
packages do not ship that 200+ MB runtime, so use the user's installed Google
Chrome instead and keep the persistent account profile inside ZM AI TOOL.
"""
from __future__ import annotations

import os
import asyncio
import socket
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
_profile_slots: dict[str, int] = {}


def _visible_window_geometry(
    index: int,
    screen_width: int,
    screen_height: int,
) -> tuple[int, int, int, int]:
    """Split screen into 2 columns, allow horizontal overlap, full height."""

    columns = 2
    width = min(screen_width, max(1080, screen_width // columns))
    height = screen_height

    # Even index: right; odd index: left
    column = index % columns

    if column == 0:
        x = screen_width - width
    else:
        x = 0

    y = 0

    return x, y, width, height



def _visible_window_args(*, fullscreen: bool = False) -> list[str]:
    """Start compactly; the page then supplies the real monitor work area."""
    # Do not create Tk/AppKit from a Flow worker thread.  macOS aborts the
    # process when Tk initializes Cocoa off the main thread (SIGABRT/-6).
    return [
        "--window-position=0,0",
        *( ["--start-maximized"] if fullscreen else ["--window-size=1080,800"] ),
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


def free_local_port() -> int:
    """Reserve a localhost port number for a visible manual session."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BrowserManager:
    """Subset of flow-py's browser contract backed by installed Google Chrome."""

    def __init__(self, *, headless: bool = True, profile_dir: Path | None = None, slow_mo: int = 0, google_login_compat: bool = False, fullscreen: bool = False, page_zoom: float = 1.0, cdp_url: str | None = None, remote_debugging_port: int | None = None, restore_last_session: bool = False) -> None:
        self.cdp_url = cdp_url  # flow-py checks this optional upstream attribute.
        # Old clients can still request visible Chrome. A headless Linux VPS
        # has no window server; normalize here for every Flow caller.
        self.headless = headless or (sys.platform.startswith('linux')
                                    and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')))
        self.profile_dir = Path(profile_dir or Path.cwd() / ".flow-profile")
        self.slow_mo = slow_mo
        self.google_login_compat = google_login_compat
        self.fullscreen = fullscreen
        self.page_zoom = page_zoom
        self.remote_debugging_port = remote_debugging_port
        self.restore_last_session = restore_last_session
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._cdp_browser: Any = None
        self._page: Page | None = None
        self._owns_page = False
        self._zoom_applied = False
        self._window_placed = False
        self._profile_lock = profile_lock(self.profile_dir)
        self._owns_profile = False
        # ponytail: captured only for visible windows; restored in stop().
        self._prior_app: Any = None

    @classmethod
    def from_cdp(cls, cdp_url: str, *, new_page: bool = False) -> "BrowserManager":
        # CDP attaches to the already visible Chrome profile used by Flow.
        # Keep this distinct from headless CDP consumers so page zoom/layout
        # adjustments run on the real account tab.
        manager = cls(headless=False, cdp_url=cdp_url, page_zoom=1.0)
        manager._owns_page = new_page
        manager._window_placed = True  # owner already placed it; re-placing makes it jump
        return manager

    async def start(self) -> "BrowserManager":
        if self.cdp_url:
            if not await asyncio.to_thread(network_available):
                raise RuntimeError('FLOW_NETWORK_UNAVAILABLE: Internet connection is unavailable')
            try:
                self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
                self._cdp_browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
                contexts = self._cdp_browser.contexts
                if not contexts:
                    raise RuntimeError("FLOW_CDP_NO_CONTEXT: Existing Chrome has no browser context")
                self._ctx = contexts[0]
                return self
            except BaseException:
                await self.stop()
                raise
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
            # Imported Cookie headers have no expiry metadata. Restore the
            # native session so Chrome keeps these session cookies on reopen.
            if self.restore_last_session:
                launch_args.append("--restore-last-session")
            if self.remote_debugging_port:
                self.cdp_url = f"http://127.0.0.1:{int(self.remote_debugging_port)}"
                launch_args.extend([
                    f"--remote-debugging-port={int(self.remote_debugging_port)}",
                    "--remote-debugging-address=127.0.0.1",
                ])
            # Keep the same browser compatibility surface for every Flow
            # session, including direct image/video workers.
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
            # Keep headless and visible Flow sessions consistent. Chromium's
            # automation flag can survive the launch argument and is exposed
            # as navigator.webdriver on pages opened after context startup.
            # Flow treats that signal as unusual activity during generation.
            await self._ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
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
        # CDP is an attachment to the user's already-running Chrome. Stopping
        # Playwright must only disconnect its transport; Browser.close() would
        # terminate the user's one visible session and invalidate the next job.
        if self._owns_page and self._page:
            try:
                await self._page.close()
            except Exception:
                pass
        if self.cdp_url and self._cdp_browser:
            self._cdp_browser = None
        elif self._ctx:
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
        self._cdp_browser = None
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
            # Request only pid metadata during iteration. On macOS, asking
            # psutil for cmdline in process_iter can raise PermissionError or
            # SystemError from KERN_PROCARGS2 for unrelated protected PIDs.
            for proc in psutil.process_iter(["pid"]):
                try:
                    if proc.pid == os.getpid():
                        continue
                    cmdline = " ".join(proc.cmdline() or [])
                    if profile not in cmdline:
                        continue
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=0.5)
                    except psutil.TimeoutExpired:
                        proc.kill()
                except (psutil.Error, OSError, PermissionError, SystemError):
                    continue
        except (ImportError, psutil.Error, OSError, PermissionError, SystemError):
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
        # Reuse one initial blank page and close only extra blank tabs created
        # by the profile. Never leave about:blank tabs visible to the user.
        blank_pages = [candidate for candidate in pages if candidate.url in {"", "about:blank"}]
        if len(blank_pages) > 1:
            for extra in blank_pages[1:]:
                try:
                    await extra.close()
                except Exception:
                    pass
            pages = self.context.pages
        if self.cdp_url:
            if self._owns_page:
                self._page = next((candidate for candidate in pages if candidate.url in {"", "about:blank"}), None) or await self.context.new_page()
                if self._page.url in {"", "about:blank"}:
                    await self._page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                if not self.headless:
                    if not self._window_placed:
                        await self._place_visible_window(self._page)
                    await self._apply_visible_zoom(self._page)
                return self._page
            for candidate in pages:
                if "flow.google.com" in candidate.url or "labs.google" in candidate.url:
                    self._page = candidate
                    for extra in list(self.context.pages):
                        if extra is candidate or extra.url not in {"", "about:blank"}:
                            continue
                        try:
                            await extra.close()
                        except Exception:
                            pass
                    if not self.headless:
                        if not self._window_placed:
                            await self._place_visible_window(candidate)
                        await candidate.bring_to_front()
                        await self._apply_visible_zoom(candidate)
                    return candidate
        # Prefer an existing Flow tab. Chrome profiles often start with a
        # blank tab beside it; close those blanks instead of exposing them.
        self._page = next((candidate for candidate in pages
                           if "flow.google.com" in candidate.url or "labs.google" in candidate.url), None)
        if self._page is None:
            self._page = pages[0] if pages else await self.context.new_page()
        for extra in list(self.context.pages):
            if extra is self._page or extra.url not in {"", "about:blank"}:
                continue
            try:
                await extra.close()
            except Exception:
                pass
        if self._page.url in {"", "about:blank"}:
            await self._page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
        for extra in list(self.context.pages):
            if extra is not self._page and extra.url in {"", "about:blank"}:
                try:
                    await extra.close()
                except Exception:
                    pass
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
                await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'normal'}})
                try:
                    area = await page.evaluate("() => ({l: screen.availLeft || 0, t: screen.availTop || 0, w: screen.availWidth, h: screen.availHeight})")
                    await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {
                        'left': int(area['l']), 'top': int(area['t']), 'width': int(area['w']), 'height': int(area['h'])}})
                except Exception:
                    await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'maximized'}})
                self._window_placed = True
            finally:
                await session.detach()
            return
        global _visible_window_index
        area = await page.evaluate("""() => ({
            left: screen.availLeft || 0, top: screen.availTop || 0,
            // Use the physical screen height. macOS can report a reduced
            // availHeight when another app owns the menu/dock area, which
            // made Flow windows appear as short floating rectangles.
            width: screen.width, height: screen.height * (window.devicePixelRatio || 1)
        })""")
        with _visible_window_guard:
            key = str(self.profile_dir)
            if key not in _profile_slots:
                _profile_slots[key] = _visible_window_index
                _visible_window_index += 1
            index = _profile_slots[key]
        x, y, width, height = _visible_window_geometry(index, area['width'], area['height'])
        new_cdp_session = getattr(self.context, "new_cdp_session", None)
        if new_cdp_session is None:
            # Lightweight test doubles and non Chromium contexts may not
            # expose CDP; keep the visible fallback local to the document.
            await page.evaluate("(scale) => { document.documentElement.style.zoom = String(scale); }", self.page_zoom)
            self._zoom_applied = True
            return
        session = await new_cdp_session(page)
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
        if self.page_zoom == 1.0:
            return
        # Keep the profile and Chrome settings untouched.  Applying the scale
        # only to the active tab avoids opening chrome://settings, which can
        # interfere with Flow's anti-automation checks.
        new_cdp_session = getattr(self.context, "new_cdp_session", None)
        if new_cdp_session is None:
            evaluate = getattr(page, "evaluate", None)
            if evaluate is not None:
                await evaluate("(scale) => { document.documentElement.style.zoom = String(scale); }", self.page_zoom)
            self._zoom_applied = True
            return
        # CSS zoom changes the actual desktop layout and is visible in Flow's
        # narrow/wide responsive surfaces; pageScaleFactor only simulates pinch
        # zoom and leaves the page layout effectively at 100%.
        evaluate = getattr(page, "evaluate", None)
        if evaluate is not None:
            await evaluate("(scale) => { document.documentElement.style.zoom = String(scale); }", self.page_zoom)
        self._zoom_applied = True
