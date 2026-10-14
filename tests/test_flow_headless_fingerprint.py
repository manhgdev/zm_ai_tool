"""Compare headed vs true-headless Chrome fingerprints (requires local Chrome).

Run manually when changing BrowserManager launch:
  PYTHONPATH=backend:. FLOW_FINGERPRINT_PROBE=1 \\
    backend/.venv/bin/python -m unittest tests.test_flow_headless_fingerprint -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline.flow.browser import chrome_executable


_FINGERPRINT_JS = """() => ({
  userAgent: navigator.userAgent,
  webdriver: navigator.webdriver,
  plugins: navigator.plugins.length,
  mimeTypes: navigator.mimeTypes.length,
  languages: [...navigator.languages],
  language: navigator.language,
  chrome: typeof window.chrome,
  chromeRuntime: !!(window.chrome && window.chrome.runtime),
  outerWidth: window.outerWidth,
  outerHeight: window.outerHeight,
  platform: navigator.platform,
  hardwareConcurrency: navigator.hardwareConcurrency,
  maxTouchPoints: navigator.maxTouchPoints,
})"""


async def _probe(headless: bool) -> dict:
    from playwright.async_api import async_playwright

    executable = chrome_executable()
    if executable is None:
        raise unittest.SkipTest("Google Chrome not found")
    with tempfile.TemporaryDirectory() as raw:
        profile = Path(raw) / ("h" if headless else "v")
        profile.mkdir()
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                str(profile),
                executable_path=str(executable),
                headless=headless,
                viewport={"width": 1080, "height": 800} if headless else None,
                no_viewport=not headless,
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                args=[
                    "--lang=en-US",
                    "--disable-extensions",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1080,800",
                ],
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("about:blank")
            data = await page.evaluate(_FINGERPRINT_JS)
            await ctx.close()
            return data
        finally:
            await pw.stop()


async def _probe_browser_manager(headless: bool) -> dict:
    """Fingerprint through production BrowserManager (after UA patch)."""
    from pipeline.flow.browser import BrowserManager

    with tempfile.TemporaryDirectory() as raw:
        manager = BrowserManager(headless=headless, profile_dir=Path(raw) / ("h" if headless else "v"))
        await manager.start()
        try:
            page = await manager.page()
            await page.goto("about:blank")
            return await page.evaluate(_FINGERPRINT_JS)
        finally:
            await manager.stop()


class HeadlessFingerprintProbe(unittest.IsolatedAsyncioTestCase):
    async def test_print_headed_vs_true_headless_diff(self):
        if not os.environ.get("FLOW_FINGERPRINT_PROBE"):
            self.skipTest("Set FLOW_FINGERPRINT_PROBE=1 to run the live Chrome probe")
        headed = await _probe(False)
        headless = await _probe(True)
        diff = {
            key: {"headed": headed.get(key), "headless": headless.get(key)}
            for key in sorted(set(headed) | set(headless))
            if headed.get(key) != headless.get(key)
        }
        print("\n=== RAW LAUNCH DIFF (no UA override) ===")
        print(json.dumps(diff, indent=2, ensure_ascii=False))
        self.assertIn("HeadlessChrome", headless["userAgent"])

    async def test_browser_manager_headless_matches_headed_ua(self):
        if not os.environ.get("FLOW_FINGERPRINT_PROBE"):
            self.skipTest("Set FLOW_FINGERPRINT_PROBE=1 to run the live Chrome probe")
        headed = await _probe_browser_manager(False)
        headless = await _probe_browser_manager(True)
        diff = {
            key: {"headed": headed.get(key), "headless": headless.get(key)}
            for key in sorted(set(headed) | set(headless))
            if headed.get(key) != headless.get(key)
        }
        print("\n=== BrowserManager DIFF ===")
        print(json.dumps(diff, indent=2, ensure_ascii=False))
        self.assertNotIn("HeadlessChrome", headless["userAgent"])
        self.assertIn("Chrome/", headless["userAgent"])
        self.assertEqual(headed["userAgent"], headless["userAgent"])
        self.assertIsNone(headless["webdriver"])
        self.assertEqual(headed["plugins"], headless["plugins"])


if __name__ == "__main__":
    unittest.main()
