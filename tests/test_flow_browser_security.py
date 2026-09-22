import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.browser import BrowserManager


class FlowBrowserSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_flow_keeps_chrome_sandbox_and_does_not_mask_automation(self):
        context = SimpleNamespace(add_init_script=AsyncMock())
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
        with tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome.exe')), patch(
            'pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))
        ):
            manager = BrowserManager(headless=True, profile_dir=Path(raw) / 'flow-profile')
            await manager.start()
            self.assertTrue(launch.call_args.kwargs['chromium_sandbox'])
            self.assertEqual(launch.call_args.kwargs['args'], ['--lang=en-US'])
            context.add_init_script.assert_not_called()

    async def test_blocked_launch_stops_driver_without_disabling_sandbox(self):
        launch = AsyncMock(side_effect=RuntimeError('blocked by policy'))
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
        with tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome.exe')), patch(
            'pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))
        ):
            manager = BrowserManager(headless=True, profile_dir=Path(raw))
            with self.assertRaisesRegex(RuntimeError, 'blocked by policy'):
                await manager.start()
            playwright.stop.assert_awaited_once()
            self.assertEqual(launch.await_count, 1)


if __name__ == '__main__':
    unittest.main()
