import sys
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow.browser import (
    BrowserManager,
    _chrome_major_version,
    _is_google_chrome_binary,
    _visible_window_geometry,
    chrome_executable,
    profile_lock,
)
from pipeline.flow.service import _PROJECT_RE, FlowService


class ChromeDiscoveryTests(unittest.TestCase):
    def test_profile_process_checks_and_kills_do_not_match_sibling_profiles(self):
        from pipeline.flow.browser import chrome_profile_running

        with tempfile.TemporaryDirectory() as raw:
            profile = (Path(raw) / "account").resolve()
            owned = Mock(pid=101, cmdline=Mock(return_value=["chrome", f"--user-data-dir={profile}"]))
            siblings = [Mock(pid=102, cmdline=Mock(return_value=["chrome", f"--user-data-dir={profile}-image"])),
                        Mock(pid=103, cmdline=Mock(return_value=["chrome", f"--user-data-dir={profile}-other"]))]
            with patch("psutil.process_iter", return_value=siblings):
                self.assertFalse(chrome_profile_running(profile))
            with patch("psutil.process_iter", return_value=[owned, *siblings]):
                self.assertTrue(chrome_profile_running(profile))
                BrowserManager(profile_dir=profile).kill_now()
            owned.send_signal.assert_called_once()
            for sibling in siblings:
                sibling.send_signal.assert_not_called()
                sibling.kill.assert_not_called()

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

    def test_windows_policy_prefers_automation_chrome(self):
        with tempfile.TemporaryDirectory() as raw:
            google = Path(raw) / 'Google/Chrome/Application/chrome.exe'
            cft = Path(raw) / 'Google/Chrome for Testing/Application/chrome.exe'
            google.parent.mkdir(parents=True)
            cft.parent.mkdir(parents=True)
            google.touch()
            cft.touch()
            with patch('sys.platform', 'win32'), \
                 patch.dict(os.environ, {
                     'PROGRAMFILES': raw,
                     'PROGRAMFILES(X86)': '',
                     'LOCALAPPDATA': raw,
                     'PLAYWRIGHT_BROWSERS_PATH': str(Path(raw) / 'empty-pw'),
                 }, clear=False), \
                 patch('pipeline.flow.browser._windows_chrome_user_data_dir_policy', return_value=r'C:\ChromeProfiles'), \
                 patch('pipeline.flow.browser.os.access', return_value=True):
                self.assertEqual(chrome_executable(), cft)

    def test_google_chrome_binary_detection(self):
        self.assertTrue(_is_google_chrome_binary(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')))
        self.assertFalse(_is_google_chrome_binary(Path(r'C:\Users\a\AppData\Local\Google\Chrome for Testing\Application\chrome.exe')))
        self.assertFalse(_is_google_chrome_binary(Path(r'C:\Users\a\AppData\Local\ms-playwright\chromium-1\chrome-win64\chrome.exe')))

    def test_chrome_major_version_never_spawns_browser(self):
        # Spawning Chrome for --product-version opens the user's real session
        # ("Mở trong phiên trình duyệt hiện tại") on macOS.
        with patch('pipeline.flow.browser.subprocess.run') as run:
            self.assertEqual(_chrome_major_version(Path('/missing/chrome')), '155.0.0.0')
            run.assert_not_called()

    def test_chrome_major_version_reads_macos_plist(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            exe = root / 'Google Chrome.app' / 'Contents' / 'MacOS' / 'Google Chrome'
            exe.parent.mkdir(parents=True)
            exe.touch()
            plist = root / 'Google Chrome.app' / 'Contents' / 'Info.plist'
            plist.write_bytes(
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
                b'<plist version="1.0"><dict>'
                b'<key>CFBundleShortVersionString</key><string>144.0.7559.96</string>'
                b'</dict></plist>'
            )
            with patch('sys.platform', 'darwin'), patch('pipeline.flow.browser.subprocess.run') as run:
                self.assertEqual(_chrome_major_version(exe), '144.0.0.0')
                run.assert_not_called()


class WindowsChromeIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_branded_chrome_when_user_data_policy_set(self):
        context = SimpleNamespace(add_init_script=AsyncMock())
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
        with tempfile.TemporaryDirectory() as raw, \
             patch('sys.platform', 'win32'), \
             patch('pipeline.flow.browser._windows_chrome_user_data_dir_policy', return_value=r'C:\Managed\Chrome'), \
             patch('pipeline.flow.browser.chrome_executable', return_value=Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')), \
             patch('pipeline.flow.browser.network_available', return_value=True), \
             patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
            manager = BrowserManager(headless=True, profile_dir=Path(raw) / 'flow')
            with self.assertRaisesRegex(RuntimeError, 'FLOW_CHROME_POLICY_USER_DATA_DIR'):
                await manager.start()
            launch.assert_not_awaited()

    async def test_refuses_system_chrome_user_data_as_profile(self):
        chrome_home = Path(tempfile.mkdtemp()) / 'Google' / 'Chrome' / 'User Data'
        chrome_home.mkdir(parents=True)
        with patch('pipeline.flow.browser._default_chrome_user_data_dir', return_value=chrome_home), \
             patch('pipeline.flow.browser._windows_chrome_user_data_dir_policy', return_value=None), \
             patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome.exe')), \
             patch('pipeline.flow.browser.network_available', return_value=True):
            manager = BrowserManager(headless=True, profile_dir=chrome_home / 'Default')
            with self.assertRaisesRegex(RuntimeError, 'FLOW_CHROME_PROFILE_FORBIDDEN'):
                await manager.start()


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
            _owns_chrome_process=False,
            stop=AsyncMock(side_effect=RuntimeError("tab close failed")),
            kill_now=Mock(),
        )
        with patch.object(service, "_log"):
            await service._close_job_browser(browser, "job-2")
        browser.stop.assert_awaited_once()
        browser.kill_now.assert_not_called()

    async def test_job_browser_cleanup_kills_spawned_chrome_owned_by_flow(self):
        service = FlowService()
        browser = SimpleNamespace(
            cdp_url="http://127.0.0.1:9222",
            _owns_chrome_process=True,
            stop=AsyncMock(side_effect=RuntimeError("tab close failed")),
            kill_now=Mock(),
        )
        with patch.object(service, "_log"):
            await service._close_job_browser(browser, "job-spawn")
        browser.kill_now.assert_called_once()

    async def test_cdp_attach_uses_existing_flow_page_without_closing_remote_context(self):
        flow_page = SimpleNamespace(
            url='https://flow.google.com/project/project-id',
            is_closed=lambda: False,
            bring_to_front=AsyncMock(),
        )
        context = SimpleNamespace(pages=[flow_page], close=AsyncMock(), add_init_script=AsyncMock())
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

    async def test_remote_debugging_port_spawns_chrome_then_attaches_cdp(self):
        """Connect/login path: real Chrome subprocess + connect_over_cdp."""
        context = SimpleNamespace(pages=[], add_init_script=AsyncMock(), close=AsyncMock())
        native_session = SimpleNamespace(send=AsyncMock())
        cdp_browser = SimpleNamespace(
            contexts=[context], close=AsyncMock(),
            new_browser_cdp_session=AsyncMock(return_value=native_session),
        )
        connect = AsyncMock(return_value=cdp_browser)
        launch = AsyncMock()
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=connect, launch_persistent_context=launch),
            stop=AsyncMock(),
        )
        fake_proc = Mock(poll=Mock(return_value=None), terminate=Mock(), kill=Mock(), wait=Mock())
        with tempfile.TemporaryDirectory() as raw, \
             patch('pipeline.flow.browser.network_available', return_value=True), \
             patch('pipeline.flow.browser.chrome_executable', return_value=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')), \
             patch('pipeline.flow.browser._cdp_endpoint_ready', return_value=True), \
             patch('pipeline.flow.browser.subprocess.Popen', return_value=fake_proc) as popen, \
             patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
            manager = BrowserManager(
                headless=False,
                profile_dir=Path(raw) / 'spawn-profile',
                remote_debugging_port=9333,
                restore_last_session=True,
            )
            await manager.start()
            launch.assert_not_awaited()
            connect.assert_awaited_once_with('http://127.0.0.1:9333')
            self.assertTrue(manager._owns_chrome_process)
            self.assertEqual(manager.cdp_url, 'http://127.0.0.1:9333')
            argv = popen.call_args.args[0]
            self.assertEqual(argv[0], str(Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')))
            self.assertIn('--remote-debugging-port=9333', argv)
            self.assertIn('--restore-last-session', argv)
            self.assertNotIn('--disable-blink-features=AutomationControlled', argv)
            self.assertNotIn('--headless=new', argv)
            self.assertFalse(any(arg.startswith('--user-agent=') for arg in argv))
            context.add_init_script.assert_not_awaited()
            with patch.object(manager, 'kill_now') as killer:
                await manager.stop()
            native_session.send.assert_awaited_once_with('Browser.close')
            cdp_browser.close.assert_not_awaited()
            fake_proc.wait.assert_called()
            killer.assert_called_once()
            self.assertFalse(manager._owns_chrome_process)

    async def test_spawned_headless_uses_native_chrome_ua(self):
        context = SimpleNamespace(pages=[], add_init_script=AsyncMock(), close=AsyncMock())
        cdp_browser = SimpleNamespace(contexts=[context], close=AsyncMock())
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(
                connect_over_cdp=AsyncMock(return_value=cdp_browser),
                launch_persistent_context=AsyncMock(),
            ),
            stop=AsyncMock(),
        )
        fake_proc = Mock(poll=Mock(return_value=0), terminate=Mock(), kill=Mock(), wait=Mock())
        with tempfile.TemporaryDirectory() as raw, \
             patch('pipeline.flow.browser.network_available', return_value=True), \
             patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome')), \
             patch('pipeline.flow.browser._cdp_endpoint_ready', return_value=True), \
             patch('pipeline.flow.browser.subprocess.Popen', return_value=fake_proc) as popen, \
             patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
            manager = BrowserManager(headless=True, profile_dir=Path(raw), remote_debugging_port=9444)
            await manager.start()
            argv = popen.call_args.args[0]
            self.assertIn('--headless=new', argv)
            self.assertFalse(any(arg.startswith('--user-agent=') for arg in argv))
            await manager.stop()

    async def test_cdp_job_tab_isolated_and_closed_without_closing_chrome(self):
        flow_page = SimpleNamespace(
            url='https://flow.google.com/project/project-id',
            is_closed=lambda: False,
            bring_to_front=AsyncMock(),
        )
        job_page = SimpleNamespace(
            url='', is_closed=lambda: False, close=AsyncMock(),
            goto=AsyncMock(), bring_to_front=AsyncMock(),
        )
        context = SimpleNamespace(
            pages=[flow_page], new_page=AsyncMock(return_value=job_page), close=AsyncMock(),
            add_init_script=AsyncMock(),
        )
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
            self.assertTrue(manager._chromeless)
        with patch.object(sys, 'platform', 'linux'), patch.dict(os.environ, {'DISPLAY': ':99'}):
            manager = BrowserManager(headless=False, profile_dir=Path('/tmp/flow-visible-test'))
            self.assertFalse(manager.headless)
            self.assertFalse(manager._chromeless)

    async def test_vps_without_display_still_launches_true_headless(self):
        context = SimpleNamespace(add_init_script=AsyncMock())
        launch = AsyncMock(return_value=context)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
        with patch.object(sys, 'platform', 'linux'), \
             patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}, clear=False), \
             patch('pipeline.flow.browser.network_available', return_value=True), \
             tempfile.TemporaryDirectory() as raw, \
             patch('pipeline.flow.browser.chrome_executable', return_value=Path('chrome')), \
             patch('pipeline.flow.browser.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))):
            manager = BrowserManager(headless=True, profile_dir=Path(raw) / 'vps')
            await manager.start()
            self.assertTrue(launch.call_args.kwargs['headless'])
            await manager.stop()

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
            for hide_ui in (False, True):
                context.add_init_script.reset_mock()
                manager = BrowserManager(headless=hide_ui, profile_dir=Path(raw) / 'flow-profile')
                await manager.start()
                self.assertTrue(launch.call_args.kwargs['chromium_sandbox'])
                self.assertEqual(manager.headless, hide_ui)
                self.assertEqual(manager._chromeless, hide_ui)
                self.assertEqual(launch.call_args.kwargs['headless'], hide_ui)
                if hide_ui:
                    # True Playwright headless with the installed Chrome's native UA.
                    self.assertEqual(launch.call_args.kwargs['viewport'], {'width': 1080, 'height': 800})
                    self.assertNotIn('no_viewport', launch.call_args.kwargs)
                    self.assertNotIn('user_agent', launch.call_args.kwargs)
                    self.assertNotIn('--window-position=0,0', launch.call_args.kwargs['args'])
                else:
                    self.assertIsNone(launch.call_args.kwargs['viewport'])
                    self.assertTrue(launch.call_args.kwargs['no_viewport'])
                    self.assertNotIn('user_agent', launch.call_args.kwargs)
                    self.assertIn('--window-size=1080,800', launch.call_args.kwargs['args'])
                    self.assertIn('--window-position=0,0', launch.call_args.kwargs['args'])
                self.assertNotIn('--disable-blink-features=AutomationControlled', launch.call_args.kwargs['args'])
                self.assertNotIn('--disable-infobars', launch.call_args.kwargs['args'])
                self.assertIn('--disable-extensions', launch.call_args.kwargs['args'])
                self.assertIn('--no-first-run', launch.call_args.kwargs['args'])
                self.assertIn('--no-default-browser-check', launch.call_args.kwargs['args'])
                self.assertIn('--profile-directory=Default', launch.call_args.kwargs['args'])
                self.assertNotIn('--new-window', launch.call_args.kwargs['args'])
                self.assertNotIn('--no-sandbox', launch.call_args.kwargs['args'])
                self.assertEqual(context.add_init_script.await_count, 0)
                self.assertEqual(launch.call_args.kwargs['ignore_default_args'], ['--use-mock-keychain', '--password-store=basic'])
                await manager.stop()

            manager = BrowserManager(headless=True, profile_dir=Path(raw) / 'cookie-profile', restore_last_session=True)
            await manager.start()
            self.assertIn('--restore-last-session', launch.call_args.kwargs['args'])
            await manager.stop()

            manager = BrowserManager(
                headless=False,
                profile_dir=Path(raw) / 'login-profile',
                google_login_compat=True,
            )
            await manager.start()
            self.assertNotIn('--disable-blink-features=AutomationControlled', launch.call_args.kwargs['args'])
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
    def test_session_profile_copy_skips_files_locked_by_live_chrome(self):
        import importlib
        flow_profiles = importlib.import_module("pipeline.flow.profiles")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "account"
            (source / "Default").mkdir(parents=True)
            (source / "Default" / "Preferences").write_text("{}", encoding="utf-8")
            (source / "Default" / "locked.db").write_text("locked", encoding="utf-8")
            (source / "Cache" / "Cache_Data").mkdir(parents=True)
            (source / "Cache" / "Cache_Data" / "data_2").write_text("cache", encoding="utf-8")

            locked = OSError(13, "The process cannot access the file")
            locked.winerror = 32
            real_copy2 = flow_profiles.shutil.copy2

            def copy2(src, dst):
                if Path(src).name == "locked.db":
                    raise locked
                return real_copy2(src, dst)

            service = FlowService()
            with patch("pipeline.flow.service.store.profile_dir", return_value=source), \
                 patch("pipeline.flow.service.store.root", return_value=root / "data"), \
                 patch("pipeline.flow.profiles.shutil.copy2", side_effect=copy2):
                target = service._ensure_session_profile("account", "image")

            self.assertEqual(target, root / "data" / "profiles" / "account-image")
            self.assertEqual((target / "Default" / "Preferences").read_text(encoding="utf-8"), "{}")
            self.assertFalse((target / "Default" / "locked.db").exists())
            self.assertFalse((target / "Cache").exists())

    def test_job_cleanup_never_copies_encrypted_cookie_databases_back(self):
        import asyncio
        from pipeline.flow import store

        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            canonical = store.profile_dir("a") / "Default" / "Cookies"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"verified-native-cookie-db")
            runtime = Path(raw) / "runtime"
            (runtime / "Default").mkdir(parents=True)
            (runtime / "Default" / "Cookies").write_bytes(b"stale-encrypted-cookie-db")
            store._write("accounts", [{"id": "a"}])
            browser = Mock(context=Mock(pages=[]), cdp_url=None)
            browser.stop = AsyncMock()
            asyncio.run(FlowService()._close_job_browser(browser, "j", account_id="a", runtime_profile=runtime))
            self.assertEqual(canonical.read_bytes(), b"verified-native-cookie-db")
            browser.stop.assert_awaited_once()
            browser.kill_now.assert_called_once()

    def test_login_redirect_never_becomes_a_flow_project(self):
        project = '6e98f844-554a-4e93-898c-4b3096681a04'
        for url in [f'https://flow.google.com/project/{project}', f'/project/{project}?x=1']:
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

    def test_non_quota_image_reject_never_quarantines_model_or_account(self):
        from pipeline.flow import store
        from pipeline.flow.errors import _classify_visible_flow_error, _is_flow_quota_exhausted

        tile_msg = (
            "FLOW_QUOTA_EXHAUSTED: Rất tiếc, không tạo được hình ảnh này. "
            "Bạn chưa bị tính phí cho lượt tạo này. refresh undo delete_forever"
        )
        classified = _classify_visible_flow_error(tile_msg)
        self.assertTrue(classified.startswith("FLOW_GENERATION_REJECTED:"), classified)
        self.assertNotIn("refresh undo delete_forever", classified)
        self.assertFalse(_is_flow_quota_exhausted(tile_msg))
        self.assertFalse(_is_flow_quota_exhausted(classified))

        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            future_ts = 9999999999.0
            store._write("accounts", [{
                "id": "acc1",
                "label": "cskh.zm",
                "status": "online",
                "modelSuspensions": {
                    "image:Nano Banana 2.1": {"until": future_ts, "reason": tile_msg},
                    "image:Nano Banana Pro": {"until": future_ts, "reason": "FLOW_QUOTA_EXHAUSTED: Đã đạt hạn mức sử dụng."},
                },
            }])
            service = FlowService()
            self.assertFalse(service._account_suspended("acc1", "image", "Nano Banana 2.1"))
            self.assertTrue(service._account_suspended("acc1", "image", "Nano Banana Pro"))
            service.suspend_model("acc1", "image", "Nano Banana 2.1", tile_msg)
            service.suspend_account("acc1", tile_msg)
            cleaned = service.accounts()[0]
            self.assertNotIn("image:Nano Banana 2.1", cleaned.get("modelSuspensions") or {})
            self.assertIn("image:Nano Banana Pro", cleaned.get("modelSuspensions") or {})
            self.assertIsNone(cleaned.get("suspendedUntil"))

    def test_pro_image_quota_fallback_syncs_image_model_and_clears_stale_notices(self):
        from pipeline.flow import store

        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            store._write("accounts", [{
                "id": "acc1",
                "label": "cskh.zm",
                "status": "online",
                "plan": "Pro",
                "imageProjectId": "6e98f844-554a-4e93-898c-4b3096681a04",
            }])
            store._write("jobs", [
                {
                    "id": "job1",
                    "accountId": "acc1",
                    "kind": "image",
                    "status": "failed",
                    "stage": "failed",
                    "queueOrder": 0,
                    "settings": {"model": "Nano Banana Pro", "imageModel": "Nano Banana Pro"},
                    "recoveryNotice": {"vi": "stale", "en": "stale"},
                },
                {
                    "id": "job2",
                    "accountId": "acc1",
                    "kind": "image",
                    "status": "queued",
                    "stage": "queued",
                    "queueOrder": 1,
                    "settings": {"model": "Nano Banana Pro", "imageModel": "Nano Banana Pro"},
                    "recoveryNotice": {"vi": "stale", "en": "stale"},
                },
            ])
            service = FlowService()
            service._apply_image_quota_fallback("job1", "FLOW_QUOTA_EXHAUSTED: Pro quota hit")
            job1_after = store.get_row("jobs", "job1")
            job2_after = store.get_row("jobs", "job2")
            self.assertEqual(job1_after["settings"]["model"], "Nano Banana 2.1")
            self.assertEqual(job1_after["settings"]["imageModel"], "Nano Banana 2.1")
            self.assertIsNone(job1_after.get("recoveryNotice"))
            self.assertEqual(job2_after["settings"]["model"], "Nano Banana 2.1")
            self.assertEqual(job2_after["settings"]["imageModel"], "Nano Banana 2.1")
            self.assertEqual(job2_after["status"], "queued")
            self.assertIsNone(job2_after.get("recoveryNotice"))

            with patch.object(service, "_run_sync"):
                retried = service.retry("job1", {"accountId": "acc1"})
            self.assertEqual(retried["status"], "queued")
            self.assertEqual(retried["settings"]["model"], "Nano Banana 2.1")
            self.assertEqual(retried["settings"]["imageModel"], "Nano Banana 2.1")

            failed_row = store.patch_row("jobs", "job1", {"status": "failed", "stage": "failed", "error": "FLOW_QUOTA_EXHAUSTED: test"})
            self.assertIsNone(failed_row.get("fallbackNotice"))
            self.assertIsNone(failed_row.get("recoveryNotice"))

    def test_zip_output_folder_bundles_completed_outputs(self):
        import zipfile
        from pipeline.flow import store

        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)), patch.dict(os.environ, {"ZM_AI_TOOL_OUTPUT_ROOT": raw}):
            out_dir = Path(raw) / "flow" / "image" / "batch_01"
            out_dir.mkdir(parents=True)
            img1 = out_dir / "001__j1__image_001.jpg"
            img2 = out_dir / "002__j2__image_002.jpg"
            img1.write_bytes(b"img-one")
            img2.write_bytes(b"img-two")
            store._write("jobs", [
                {
                    "id": "j1",
                    "kind": "image",
                    "inputIndex": 1,
                    "status": "done",
                    "outputFolder": str(out_dir),
                    "outputs": [str(img1)],
                    "settings": {"outputDir": "batch_01"},
                },
                {
                    "id": "j2",
                    "kind": "image",
                    "inputIndex": 2,
                    "status": "done",
                    "outputFolder": str(out_dir),
                    "outputs": [str(img2)],
                    "settings": {"outputDir": "batch_01"},
                },
            ])
            service = FlowService()
            zip_path = service.zip_output_folder("batch_01", "image")
            self.assertTrue(zip_path.is_file())
            self.assertEqual(zip_path, Path(raw) / "flow" / "zip" / "batch_01.zip")
            with zipfile.ZipFile(zip_path, "r") as zf:
                self.assertEqual(zf.namelist(), [img1.name, img2.name])
            first_mtime_ns = zip_path.stat().st_mtime_ns
            with patch.object(zipfile.ZipFile, "write", side_effect=AssertionError("should not rewrite existing zip")):
                reused_path = service.zip_output_folder("batch_01", "image")
            self.assertEqual(reused_path, zip_path)
            self.assertEqual(reused_path.stat().st_mtime_ns, first_mtime_ns)

    def test_each_output_folder_creates_distinct_flow_project_id(self):
        import asyncio
        from pipeline.flow import store

        class _DummyCount:
            async def count(self):
                return 1

        class _DummyPage:
            url = "https://labs.google/fx/tools/flow/project/proj-init"

            def locator(self, _sel):
                return _DummyCount()

        class _DummyApi:
            project_id = "proj-init"
            _project_page_url = ""

        class _DummyClient:
            project_id = "proj-init"
            _project_url = ""

            async def _ensure_project_page(self, _page):
                return None

        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            account = {
                "id": "acc1",
                "label": "Acc 1",
                "status": "online",
                "projectId": "proj-default",
                "imageProjectId": "proj-default",
            }
            store._write("accounts", [account])
            store._write("jobs", [
                {
                    "id": "job_folder_a",
                    "kind": "image",
                    "status": "processing",
                    "accountId": "acc1",
                    "settings": {"outputDir": "folder_a", "ratio": "16:9", "resolution": "1K"},
                },
                {
                    "id": "job_folder_b",
                    "kind": "image",
                    "status": "processing",
                    "accountId": "acc1",
                    "settings": {"outputDir": "folder_b", "ratio": "16:9", "resolution": "1K"},
                },
            ])
            service = FlowService()
            created_ids = iter(["proj-folder-a", "proj-folder-b"])
            seen_avoid: list[set[str]] = []

            async def _fake_create(_page, *, avoid_project_id="", avoid_project_ids=None):
                seen_avoid.append(set(avoid_project_ids or set()))
                return next(created_ids)

            page = _DummyPage()
            api = _DummyApi()
            client = _DummyClient()
            with patch.object(service, "_create_flow_project_ui", side_effect=_fake_create), \
                 patch("pipeline.flow.projects._dismiss_flow_whats_new", new=AsyncMock()):
                acc_after_a = asyncio.run(
                    service._ensure_account_project_page(
                        page, api=api, client=client, account=account, job_id="job_folder_a", kind="image",
                    )
                )
                self.assertEqual(client.project_id, "proj-folder-a")
                acc_after_b = asyncio.run(
                    service._ensure_account_project_page(
                        page, api=api, client=client, account=acc_after_a, job_id="job_folder_b", kind="image",
                    )
                )
                self.assertEqual(client.project_id, "proj-folder-b")

            self.assertNotEqual(
                acc_after_b.get("laneProject_image_folder_a_16-9_1K"),
                acc_after_b.get("laneProject_image_folder_b_16-9_1K"),
            )
            self.assertIn("proj-folder-a", seen_avoid[1])


if __name__ == '__main__':
    unittest.main()

