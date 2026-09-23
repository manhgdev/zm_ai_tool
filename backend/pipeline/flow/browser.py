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
        try:
            self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
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
                          "--disable-infobars"],
                ),
                timeout=60,
            )
            await self._ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                "FLOW_BROWSER_START_TIMEOUT: Google Chrome did not start within 60 seconds"
            ) from exc
        except BaseException:
            await self.stop()
            raise
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
            return self._page
        pages = self.context.pages
        self._page = pages[0] if pages else await self.context.new_page()
        return self._page
