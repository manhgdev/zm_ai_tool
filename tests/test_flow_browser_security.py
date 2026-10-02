import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.browser import BrowserManager, _visible_window_geometry


class FlowBrowserGeometryTest(unittest.TestCase):
    def test_first_four_windows_fill_right_column_top_to_bottom(self):
        positions = [_visible_window_geometry(i, 1440, 900) for i in range(4)]

        self.assertEqual(positions, [
            (1080, 0, 360, 180),
            (1080, 205, 360, 180),
            (1080, 410, 360, 180),
            (1080, 615, 360, 180),
        ])
        self.assertEqual(len({(x, y) for x, y, _, _ in positions}), 4)

    def test_small_screen_keeps_windows_inside_screen(self):
        positions = [_visible_window_geometry(i, 800, 600) for i in range(4)]

        for x, y, width, height in positions:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + width, 800)
            self.assertLessEqual(y + height, 600)
        self.assertEqual(len({(x, y) for x, y, _, _ in positions}), 4)

    def test_next_group_moves_to_left_column(self):
        first = _visible_window_geometry(0, 1440, 900)
        next_group = _visible_window_geometry(4, 1440, 900)

        self.assertNotEqual(first[:2], next_group[:2])
        self.assertGreaterEqual(next_group[0], 0)
        self.assertGreaterEqual(next_group[1], 0)


class FlowBrowserSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_visible_page_does_not_keep_stealing_focus(self):
        page = SimpleNamespace(
            is_closed=lambda: False,
            bring_to_front=AsyncMock(),
            keyboard=SimpleNamespace(press=AsyncMock()),
        )
        manager = BrowserManager(headless=False, profile_dir=Path('/tmp/flow-focus-test'))
        manager._page = page

        await manager.page()
        await manager.page()

        page.bring_to_front.assert_not_awaited()
        self.assertEqual(page.keyboard.press.await_count, 5)

    async def test_flow_restores_launch_compatibility_without_disabling_sandbox(self):
        context = SimpleNamespace(add_init_script=AsyncMock())
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
        with tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome.exe')), patch(
            'pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))
        ):
            for headless in (False, True):
                context.add_init_script.reset_mock()
                manager = BrowserManager(headless=headless, profile_dir=Path(raw) / 'flow-profile')
                await manager.start()
                self.assertTrue(launch.call_args.kwargs['chromium_sandbox'])
                self.assertEqual(launch.call_args.kwargs['headless'], headless)
                self.assertNotIn('--disable-blink-features=AutomationControlled', launch.call_args.kwargs['args'])
                self.assertNotIn('--no-sandbox', launch.call_args.kwargs['args'])
                context.add_init_script.assert_any_await(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                await manager.stop()

            manager = BrowserManager(
                headless=False,
                profile_dir=Path(raw) / 'login-profile',
                google_login_compat=True,
            )
            await manager.start()
            self.assertIn('--disable-blink-features=AutomationControlled', launch.call_args.kwargs['args'])
            await manager.stop()

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
