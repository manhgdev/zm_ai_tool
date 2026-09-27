"""Desktop browser adapter for Flow.

Flow's upstream helper defaults to Playwright's bundled Chromium.  Desktop
packages do not ship that 200+ MB runtime, so use the user's installed Google
Chrome instead and keep the persistent account profile inside ZM AI TOOL.
"""
from __future__ import annotations

import os
import asyncio
import threading
import sys
from pathlib import Path
from typing import Any

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
    """Return a compact four-window column anchored on the right.

    Each group fills one vertical column from top to bottom. Later groups move
    one column left, keeping every account window visible and clickable.
    """
    width = max(300, screen_width // 4)
    # Leave room for Chrome's native title bar/frame; launch dimensions are
    # outer-window hints and can grow slightly on macOS.
    gap = max(16, min(28, screen_height // 36))
    height = max(120, screen_height // 5)
    width = min(width, screen_width)
    height = min(height, screen_height)
    row = index % 4
    group = index // 4
    x = max(0, screen_width - width - group * (width + gap))
    y = row * (height + gap)
    return x, y, width, height


def _visible_window_args() -> list[str]:
    """Place visible Flow windows in four-row columns from right to left."""
    global _visible_window_index
    with _visible_window_guard:
        index = _visible_window_index
        _visible_window_index += 1
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        width = root.winfo_screenwidth()
        height = root.winfo_screenheight()
        root.destroy()
    except Exception:
        width = 1440
        height = 900
    x, y, _, _ = _visible_window_geometry(index, width, height)
    return [
        f"--window-position={x},{y}",
        "--new-window",
    ]


def profile_lock(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _profile_guard:
        return _profile_locks.setdefault(key, threading.Lock())


def chrome_executable() -> Path | None:
    """Locate a supported Chrome installation without relying on PATH."""
    candidates: list[Path]
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    elif sys.platform == "win32":
        roots = [Path(os.environ.get("PROGRAMFILES", "")), Path(os.environ.get("PROGRAMFILES(X86)", "")), Path(os.environ.get("LOCALAPPDATA", ""))]
        candidates = [root / "Google/Chrome/Application/chrome.exe" for root in roots if str(root)]
    else:
        candidates = [Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"), Path("/usr/bin/chromium")]
    return next((path for path in candidates if path.is_file()), None)


class BrowserManager:
    """Subset of flow-py's browser contract backed by installed Google Chrome."""

    def __init__(self, *, headless: bool, profile_dir: Path, slow_mo: int = 0) -> None:
        self.cdp_url = None  # flow-py checks this optional upstream attribute.
        self.headless = headless
        self.profile_dir = Path(profile_dir)
        self.slow_mo = slow_mo
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._zoom_applied = False
        self._profile_lock = profile_lock(self.profile_dir)
        self._owns_profile = False

    async def start(self) -> "BrowserManager":
        executable = chrome_executable()
        if executable is None:
            raise RuntimeError("FLOW_CHROME_REQUIRED: Google Chrome was not found. Install Google Chrome, then connect again.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        while not self._profile_lock.acquire(blocking=False):
            await asyncio.sleep(0.1)
        self._owns_profile = True
        visible_launch_owned = False
        if not self.headless:
            while not _visible_launch_guard.acquire(blocking=False):
                await asyncio.sleep(0.05)
            visible_launch_owned = True
        try:
            self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
            visible_args = [] if self.headless else _visible_window_args()
            self._ctx = await asyncio.wait_for(
                self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    executable_path=str(executable),
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    viewport={"width": 1440, "height": 900},
                    accept_downloads=True,
                    locale="en-US",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    chromium_sandbox=True,
                    args=["--lang=en-US", "--disable-blink-features=AutomationControlled",
                          "--disable-infobars"] + visible_args,
                ),
                timeout=60,
            )
            if visible_args and visible_launch_owned:
                _visible_launch_guard.release()
                visible_launch_owned = False
            await self._ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            # Capture ya29 if/when Flow emits an aisandbox Authorization header.
            await self._ctx.add_init_script(
                """
                (() => {
                  if (window.__zmFlowBearerHook) return;
                  window.__zmFlowBearerHook = true;
                  window.__zmFlowBearer = null;
                  const grab = (h) => {
                    if (!h) return;
                    let v = null;
                    try {
                      if (typeof h.get === 'function')
                        v = h.get('Authorization') || h.get('authorization');
                      else
                        v = h.Authorization || h.authorization;
                    } catch (e) {}
                    if (typeof v === 'string' && v.startsWith('Bearer ya29.'))
                      window.__zmFlowBearer = v.slice(7);
                  };
                  const ofetch = window.fetch.bind(window);
                  window.fetch = function(input, init) {
                    try {
                      if (input && typeof Request !== 'undefined' && input instanceof Request)
                        grab(input.headers);
                      grab(init && init.headers);
                    } catch (e) {}
                    return ofetch.apply(this, arguments);
                  };
                  const setHeader = XMLHttpRequest.prototype.setRequestHeader;
                  XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
                    if (String(k).toLowerCase() === 'authorization' && String(v).startsWith('Bearer ya29.'))
                      window.__zmFlowBearer = String(v).slice(7);
                    return setHeader.apply(this, arguments);
                  };
                })();
                """
            )
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
            await self._page.bring_to_front()
            await self._apply_visible_zoom(self._page)
        return self._page

    async def _apply_visible_zoom(self, page: Page) -> None:
        if self._zoom_applied:
            return
        key = "Meta+-" if sys.platform == "darwin" else "Control+-"
        try:
            for _ in range(5):
                await page.keyboard.press(key)
            self._zoom_applied = True
        except Exception:
            pass
