import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipeline.flow.service import FlowService


class PeriodicRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_refresh_is_bounded_and_does_not_navigate(self):
        service = FlowService()
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={'status': 200, 'url': 'https://flow.google.com/project/p'})
        browser = MagicMock()
        browser.page = AsyncMock(return_value=page)
        with patch('pipeline.flow.service.time') as clock:
            clock.monotonic.side_effect = [100, 101, 160]
            for _ in range(3):
                self.assertTrue(await service._refresh_running_session('a', browser, 'p'))
        self.assertEqual(page.evaluate.await_count, 2)
        self.assertIn("credentials: 'include'", page.evaluate.call_args.args[0])
        page.goto.assert_not_called()
        page.reload.assert_not_called()

    async def test_expiry_reconnects_headless_only_once(self):
        service = FlowService()
        service._log = MagicMock()
        service._ensure_shared_reconnect = MagicMock()
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={'status': 401})
        browser = MagicMock()
        browser.page = AsyncMock(return_value=page)
        with patch('pipeline.flow.service.time') as clock:
            clock.monotonic.side_effect = [100, 160]
            for _ in range(2):
                self.assertFalse(await service._refresh_running_session('a', browser, 'p'))
        service._ensure_shared_reconnect.assert_called_once()
        self.assertFalse(service._ensure_shared_reconnect.call_args.kwargs['interactive'])
