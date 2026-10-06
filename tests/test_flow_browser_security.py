import sys
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.browser import BrowserManager, _visible_window_geometry, profile_lock
from pipeline.flow.service import _PROJECT_RE, FlowService


class FlowBrowserGeometryTest(unittest.TestCase):
    def test_first_two_windows_fill_right_column_top_to_bottom(self):
        positions = [_visible_window_geometry(i, 1440, 900) for i in range(2)]

        self.assertEqual(positions, [
            (733, 0, 707, 437),
            (733, 462, 707, 437),
        ])
        self.assertEqual(len({(x, y) for x, y, _, _ in positions}), 2)

    def test_small_screen_keeps_windows_inside_screen(self):
        positions = [_visible_window_geometry(i, 800, 600) for i in range(100)]

        for x, y, width, height in positions:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + width, 800)
            self.assertLessEqual(y + height, 600)
        self.assertEqual(len({(x, y) for x, y, _, _ in positions}), 1)

    def test_next_group_moves_to_left_column(self):
        first = _visible_window_geometry(0, 1440, 900)
        next_group = _visible_window_geometry(2, 1440, 900)

        self.assertNotEqual(first[:2], next_group[:2])
        self.assertGreaterEqual(next_group[0], 0)
        self.assertGreaterEqual(next_group[1], 0)

    def test_six_windows_fill_screen_to_left_edge(self):
        positions = [_visible_window_geometry(i, 1512, 949) for i in range(6)]
        self.assertEqual(len(set(positions)), 6)
        self.assertEqual([positions[i][0] for i in (0, 2, 4)], [1012, 506, 0])
        for x, y, width, height in positions:
            self.assertLessEqual(x + width, 1512)
            self.assertLessEqual(y + height, 949)
        self.assertEqual(_visible_window_geometry(6, 1512, 949), positions[0])
        self.assertEqual(positions[0], (1012, 0, 500, 461))

    def test_large_work_areas_add_rows_and_columns(self):
        for screen_width, screen_height, columns, rows in (
            (2560, 1440, 5, 3), (3840, 2160, 7, 4),
        ):
            with self.subTest(size=(screen_width, screen_height)):
                positions = [_visible_window_geometry(i, screen_width, screen_height)
                             for i in range(columns * rows)]
                self.assertEqual(len({x for x, _, _, _ in positions}), columns)
                self.assertEqual(len({y for _, y, _, _ in positions}), rows)
                for x, y, width, height in positions:
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + width, screen_width)
                    self.assertLessEqual(y + height, screen_height)
                    self.assertGreaterEqual(width, 500)
                    self.assertGreaterEqual(height, 420)


class FlowBrowserSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_linux_without_display_ignores_visible_browser_request(self):
        with patch.object(sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}):
            manager = BrowserManager(headless=False, profile_dir=Path('/tmp/flow-headless-test'))
            self.assertTrue(manager.headless)
        with patch.object(sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY': ':99'}):
            manager = BrowserManager(headless=False, profile_dir=Path('/tmp/flow-visible-test'))
            self.assertFalse(manager.headless)

    async def test_existing_visible_page_does_not_keep_stealing_focus(self):
        page = SimpleNamespace(
            is_closed=lambda: False,
            bring_to_front=AsyncMock(),
            keyboard=SimpleNamespace(press=AsyncMock()),
        )
        manager = BrowserManager(headless=False, profile_dir=Path('/tmp/flow-focus-test'))
        manager._page = page
        settings_page = SimpleNamespace(goto=AsyncMock(), evaluate=AsyncMock(), close=AsyncMock())
        manager._ctx = SimpleNamespace(new_page=AsyncMock(return_value=settings_page))

        await manager.page()
        await manager.page()

        page.bring_to_front.assert_not_awaited()
        page.keyboard.press.assert_not_awaited()
        manager._ctx.new_page.assert_awaited_once()
        self.assertIn('setDefaultZoom(0.4', settings_page.evaluate.call_args.args[0])
        settings_page.close.assert_awaited_once()

    async def test_window_uses_work_area_offset_and_clamps_native_size(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={
            'left': -1440, 'top': 25, 'width': 1440, 'height': 875,
        }))
        session = SimpleNamespace(send=AsyncMock(side_effect=[
            {'windowId': 7}, {}, {}, {'bounds': {'width': 740, 'height': 260}}, {},
        ]), detach=AsyncMock())
        manager = BrowserManager(headless=False, profile_dir=Path('/tmp/flow-bounds-test'))
        manager._ctx = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
        with patch('pipeline.flow.browser._visible_window_index', 0):
            await manager._place_visible_window(page)
        self.assertEqual(session.send.await_args_list[-1].args[1], {
            'windowId': 7, 'bounds': {'left': -740, 'top': 25},
        })
        session.detach.assert_awaited_once()
        self.assertTrue(manager._window_placed)

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
                self.assertEqual(launch.call_args.kwargs['viewport'],
                                 {'width': 1440, 'height': 900} if headless else None)
                self.assertEqual(launch.call_args.kwargs['no_viewport'], not headless)
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


class SessionTests(unittest.TestCase):
    def test_login_redirect_never_becomes_a_flow_project(self):
        project = '6e98f844-554a-4e93-898c-4b3096681a04'
        for url in [f'https://flow.google.com/project/{project}', f'/project/{project}?x=1', f'https://labs.google/fx/tools/flow/project/{project}']:
            self.assertEqual(_PROJECT_RE.search(url).group(1), project)
        for url in [f'https://accounts.google.com/ServiceLogin?continue=https://flow.google.com/project/{project}&followup=https:', f'https://evil.test/project/{project}', f'https://flow.google.com/project/{project}&followup=https:']:
            self.assertIsNone(_PROJECT_RE.search(url))

    def test_snapshot_owns_same_lock_as_browser(self):
        with tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.service.store.profile_dir', return_value=Path(raw)):
            service = FlowService()
            def copy(account, job):
                self.assertTrue(profile_lock(Path(raw)).locked())
                return Path(raw)
            with patch.object(service, '_clone_closed_profile', side_effect=copy):
                service._clone_runtime_profile('account', 'job')
            self.assertFalse(profile_lock(Path(raw)).locked())


if __name__ == '__main__':
    unittest.main()
