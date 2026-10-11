import sys
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.browser import BrowserManager, _visible_window_geometry, profile_lock, chrome_executable
from pipeline.flow.service import _PROJECT_RE, FlowService


class ChromeDiscoveryTests(unittest.TestCase):
    def test_linux_system_and_path_binaries(self):
        for binary in ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
                       '/opt/google/chrome/chrome', '/usr/bin/chromium-browser',
                       '/custom/bin/google-chrome']:
            with self.subTest(binary=binary), patch('sys.platform', 'linux'), \
                 patch('pipeline.flow.browser.shutil.which', return_value=binary), \
                 patch.object(Path, 'glob', return_value=[]), \
                 patch.object(Path, 'is_file', lambda path: str(path) == binary), \
                 patch('pipeline.flow.browser.os.access', return_value=True):
                self.assertEqual(chrome_executable(), Path(binary))

    def test_linux_cache_fallbacks_require_executable_files(self):
        for relative in ['puppeteer/chrome/linux-147.0/chrome-linux64/chrome',
                         'ms-playwright/chromium-1234/chrome-linux/chrome',
                         'ms-playwright/chromium-1234/chrome-linux64/chrome']:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as raw:
                binary = Path(raw) / relative
                binary.parent.mkdir(parents=True)
                binary.touch()
                with patch('sys.platform', 'linux'), \
                     patch.dict(os.environ, {'XDG_CACHE_HOME': raw, 'PUPPETEER_CACHE_DIR': '', 'PLAYWRIGHT_BROWSERS_PATH': ''}), \
                     patch('pipeline.flow.browser.shutil.which', return_value=None), \
                     patch.object(Path, 'is_file', lambda path: path == binary), \
                     patch('pipeline.flow.browser.os.access', return_value=True):
                    self.assertEqual(chrome_executable(), binary)
                    with patch('pipeline.flow.browser.os.access', return_value=False):
                        self.assertIsNone(chrome_executable())

    def test_system_install_has_priority_over_cache(self):
        with patch('sys.platform', 'linux'), patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'glob', return_value=[Path('/cache/chrome')]), \
             patch('pipeline.flow.browser.os.access', return_value=True), \
             patch('pipeline.flow.browser.shutil.which', return_value=None):
            self.assertEqual(chrome_executable(), Path('/usr/bin/google-chrome'))

    def test_custom_cache_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / 'chrome/linux-147.0/chrome-linux64/chrome'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch('sys.platform', 'linux'), \
                 patch.dict(os.environ, {'PUPPETEER_CACHE_DIR': raw}), \
                 patch('pipeline.flow.browser.shutil.which', return_value=None), \
                 patch.object(Path, 'is_file', lambda path: path == binary), \
                 patch('pipeline.flow.browser.os.access', return_value=True):
                self.assertEqual(chrome_executable(), binary)


class FlowBrowserGeometryTest(unittest.TestCase):
    def test_first_two_windows_fill_one_horizontal_row(self):
        positions = [_visible_window_geometry(i, 1440, 900) for i in range(2)]

        self.assertEqual(positions, [
            (733, 0, 707, 900),
            (0, 0, 707, 900),
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
        next_group = _visible_window_geometry(1, 1440, 900)

        self.assertNotEqual(first[:2], next_group[:2])
        self.assertEqual(next_group[1], 0)
        self.assertGreaterEqual(next_group[0], 0)
        self.assertGreaterEqual(next_group[1], 0)

    def test_six_windows_fill_screen_to_left_edge(self):
        positions = [_visible_window_geometry(i, 1512, 949) for i in range(6)]
        self.assertEqual(len(set(positions)), 3)
        self.assertEqual([positions[i][0] for i in (0, 1, 2)], [1012, 506, 0])
        for x, y, width, height in positions:
            self.assertLessEqual(x + width, 1512)
            self.assertLessEqual(y + height, 949)
        self.assertEqual(_visible_window_geometry(3, 1512, 949), positions[0])
        self.assertEqual(positions[0], (1012, 0, 500, 949))

    def test_large_work_areas_add_rows_and_columns(self):
        for screen_width, screen_height, columns, rows in (
            (2560, 1440, 5, 3), (3840, 2160, 7, 4),
        ):
            with self.subTest(size=(screen_width, screen_height)):
                positions = [_visible_window_geometry(i, screen_width, screen_height)
                             for i in range(columns * rows)]
                self.assertEqual(len({x for x, _, _, _ in positions}), columns)
                self.assertEqual(len({y for _, y, _, _ in positions}), 1)
                for x, y, width, height in positions:
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + width, screen_width)
                    self.assertLessEqual(y + height, screen_height)
                    self.assertGreaterEqual(width, 500)
                    self.assertGreaterEqual(height, 420)


class FlowBrowserSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_job_browser_cleanup_force_kills_owned_browser_after_stop_error(self):
        service = FlowService()
        browser = SimpleNamespace(
            cdp_url=None,
            stop=AsyncMock(side_effect=RuntimeError("Playwright stop failed")),
            kill_now=Mock(),
        )
        with patch.object(service, "_log"):
            await service._close_job_browser(browser, "job-1")
        browser.stop.assert_awaited_once()
        browser.kill_now.assert_called_once()

    async def test_job_browser_cleanup_never_kills_manual_cdp_chrome(self):
        service = FlowService()
        browser = SimpleNamespace(
            cdp_url="http://127.0.0.1:9222",
            stop=AsyncMock(side_effect=RuntimeError("tab close failed")),
            kill_now=Mock(),
        )
        with patch.object(service, "_log"):
            await service._close_job_browser(browser, "job-2")
        browser.stop.assert_awaited_once()
        browser.kill_now.assert_not_called()

    async def test_cdp_attach_uses_existing_flow_page_without_closing_remote_context(self):
        flow_page = SimpleNamespace(url='https://flow.google.com/project/project-id', is_closed=lambda: False)
        context = SimpleNamespace(pages=[flow_page], close=AsyncMock())
        cdp_browser = SimpleNamespace(contexts=[context], close=AsyncMock())
        connect = AsyncMock(return_value=cdp_browser)
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=connect),
            stop=AsyncMock(),
        )
        with patch('pipeline.flow.browser.network_available', return_value=True), \
             patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
            manager = BrowserManager.from_cdp('http://127.0.0.1:4567')
            await manager.start()
            self.assertEqual(await manager.page(), flow_page)
            connect.assert_awaited_once_with('http://127.0.0.1:4567')
            await manager.stop()
            cdp_browser.close.assert_not_awaited()
            context.close.assert_not_awaited()

    async def test_cdp_job_tab_isolated_and_closed_without_closing_chrome(self):
        flow_page = SimpleNamespace(url='https://flow.google.com/project/project-id', is_closed=lambda: False)
        job_page = SimpleNamespace(url='', is_closed=lambda: False, close=AsyncMock())
        context = SimpleNamespace(pages=[flow_page], new_page=AsyncMock(return_value=job_page), close=AsyncMock())
        cdp_browser = SimpleNamespace(contexts=[context], close=AsyncMock())
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=cdp_browser)),
            stop=AsyncMock(),
        )
        with patch('pipeline.flow.browser.network_available', return_value=True), \
             patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
            manager = BrowserManager.from_cdp('http://127.0.0.1:4567', new_page=True)
            await manager.start()
            self.assertEqual(await manager.page(), job_page)
            await manager.stop()
            job_page.close.assert_awaited_once()
            cdp_browser.close.assert_not_awaited()
            context.close.assert_not_awaited()

    async def test_only_linux_root_disables_sandbox(self):
        for platform, uid, sandbox in [('linux', 0, False), ('linux', 1000, True), ('darwin', 0, True), ('win32', 0, True)]:
            context = SimpleNamespace(add_init_script=AsyncMock(), close=AsyncMock())
            launch = AsyncMock(return_value=context)
            playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
            with self.subTest(platform=platform, uid=uid), tempfile.TemporaryDirectory() as raw, \
                 patch('sys.platform', platform), patch('os.geteuid', return_value=uid, create=True), \
                 patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome')), \
                 patch('pipeline.flow.browser.network_available', return_value=True), \
                 patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
                manager = BrowserManager(headless=True, profile_dir=Path(raw))
                await manager.start()
                self.assertEqual(launch.call_args.kwargs['chromium_sandbox'], sandbox)
                self.assertEqual(launch.await_count, 1)
                await manager.stop()
                self.assertFalse(manager._profile_lock.locked())

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
        manager._ctx.new_page.assert_not_awaited()

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
        with patch('sys.platform', 'darwin'), \
             patch('pipeline.flow.browser.network_available', return_value=True), \
             patch('pipeline.flow.browser._capture_active_window', return_value=None), \
             tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome.exe')), patch(
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
                self.assertIn('--disable-extensions', launch.call_args.kwargs['args'])
                self.assertNotIn('--no-sandbox', launch.call_args.kwargs['args'])
                self.assertEqual(context.add_init_script.await_count, 1)
                self.assertNotIn('ignore_default_args', launch.call_args.kwargs)
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
        with patch('os.geteuid', return_value=1000, create=True), \
             patch('pipeline.flow.browser.network_available', return_value=True), \
             tempfile.TemporaryDirectory() as raw, patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome.exe')), patch(
            'pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))
        ):
            manager = BrowserManager(headless=True, profile_dir=Path(raw))
            with self.assertRaisesRegex(RuntimeError, 'blocked by policy'):
                await manager.start()
            playwright.stop.assert_awaited_once()
            self.assertEqual(launch.await_count, 1)
            self.assertTrue(launch.call_args.kwargs['chromium_sandbox'])


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
