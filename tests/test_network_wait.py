import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from pipeline.core import network
from pipeline.core.jobs import Cancelled
from pipeline.flow import store
from pipeline.flow.service import FlowService, _FlowNetworkInterrupted, _network_ready_for_flow_job
from pipeline.mt import cloud
from pipeline.tts import capcut
from pipeline import capcut_stt


class NetworkWaitTests(unittest.TestCase):
    def test_wait_is_cancellable_and_does_not_probe_when_already_cancelled(self):
        with patch.object(network, 'network_available') as probe:
            with self.assertRaises(Cancelled):
                network.wait_for_network(cancel_check=lambda: True)
        probe.assert_not_called()

    def test_offline_wait_preserves_timeout_budget_and_reports_once(self):
        clock = [100.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(network, 'network_available', side_effect=[False, False, True]) as probe, patch.object(
            network.time, 'monotonic', side_effect=lambda: clock[0]
        ), patch.object(network.time, 'sleep', side_effect=advance), patch.object(network._clock, 'paused', 0.0, create=True):
            report = Mock()
            before = network.active_monotonic()
            paused = network.wait_for_network(on_wait=report, initial_delay=0.5)
            self.assertGreaterEqual(paused, 1.35 - 1e-6)
            self.assertAlmostEqual(network.active_monotonic(), before)
            report.assert_called_once()
            self.assertEqual(probe.call_count, 3)

    def test_cancel_during_wait(self):
        cancelled = [False]
        def report(_):
            cancelled[0] = True
        with patch.object(network, 'network_available', return_value=False), patch.object(network.time, 'sleep') as sleep:
            with self.assertRaises(Cancelled):
                network.wait_for_network(cancel_check=lambda: cancelled[0], on_wait=report)
        sleep.assert_not_called()

    def test_probe_cache_and_independent_fallback(self):
        with patch.object(network, '_checked_at', float('-inf')), patch.object(network.socket, 'create_connection', side_effect=[OSError(), io.BytesIO()]) as connect:
            self.assertTrue(network.network_available())
            self.assertTrue(network.network_available())
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(connect.call_args_list[1].args[0][0], 'www.microsoft.com')

    def test_local_services_do_not_require_internet(self):
        for url in ['http://localhost:11434', 'http://127.0.0.1/v1', 'http://10.0.0.5/v1', 'http://[::1]:8000']:
            self.assertFalse(network.is_remote_url(url))
        self.assertTrue(network.is_remote_url('https://api.example.com'))


class FlowNetworkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = patch.object(store, 'ROOT', Path(directory.name))
        root.start()
        self.addCleanup(root.stop)
        self.service = FlowService()
        store._write('accounts', [{'id': 'a', 'status': 'online', 'projectId': 'p', 'plan': 'Pro'}])
        store._write('jobs', [{'id': 'j', 'accountId': 'a', 'status': 'processing', 'kind': 'video',
                              'queueOrder': 0, 'settings': {}, 'progress': 73,
                              'submissionStartedAt': 123, 'mediaIds': ['original'], 'outputs': ['part.mp4']}])

    def test_wait_preserves_progress_identity_and_is_cancellable(self):
        def wait(**kwargs):
            kwargs['on_wait'](5)
            row = store.get_row('jobs', 'j')
            self.assertEqual(row['stage'], 'waiting_network')
            self.assertEqual(row['progress'], 73)
            self.assertEqual(row['mediaIds'], ['original'])
            self.service._cancelled.add('j')
            store.patch_row('jobs', 'j', {'status': 'cancelled'})
            self.assertTrue(kwargs['cancel_check']())
            raise Cancelled()
        with patch('pipeline.flow.service.wait_for_network', side_effect=wait):
            with self.assertRaises(asyncio.CancelledError):
                _network_ready_for_flow_job(self.service, 'j')
        self.assertEqual(store.get_row('jobs', 'j')['status'], 'cancelled')

    def test_resume_uses_original_media_and_does_not_spend_retry_budget(self):
        async def run_once(*args, **kwargs):
            row = store.get_row('jobs', 'j')
            self.assertEqual(row['submissionStartedAt'], 123)
            self.assertEqual(row['outputs'], ['part.mp4'])
            if run.await_count == 1:
                raise _FlowNetworkInterrupted()
            store.patch_row('jobs', 'j', {'status': 'done'})
        with patch('pipeline.flow.service._network_ready_for_flow_job', return_value=5), patch.object(
            self.service, '_run_once', side_effect=run_once
        ) as run:
            asyncio.run(self.service._run('j'))
        self.assertEqual(run.await_count, 2)
        self.assertEqual(store.get_row('jobs', 'j').get('autoRetryCount', 0), 0)

    def test_offline_browser_error_is_not_auth_failure(self):
        browser = Mock(start=AsyncMock(side_effect=RuntimeError('ERR_INTERNET_DISCONNECTED')), stop=AsyncMock())
        with patch('pipeline.flow.browser.BrowserManager', return_value=browser), patch(
            'pipeline.flow.service.network_available', return_value=False
        ), patch.object(self.service, '_ensure_shared_reconnect') as reconnect:
            with self.assertRaises(_FlowNetworkInterrupted):
                asyncio.run(self.service._run_once('j'))
        reconnect.assert_not_called()
        browser.stop.assert_awaited_once()
        self.assertEqual(store.get_row('jobs', 'j')['stage'], 'waiting_network')
        self.assertEqual(store.get_row('accounts', 'a')['status'], 'online')

    def test_probe_true_net_err_still_waits_network(self):
        """TCP probe can stay true while Chrome cannot reach Flow."""
        browser = Mock(
            start=AsyncMock(side_effect=RuntimeError(
                'Page.goto: net::ERR_INTERNET_DISCONNECTED at https://flow.google.com/project/p'
            )),
            stop=AsyncMock(),
        )
        with patch('pipeline.flow.browser.BrowserManager', return_value=browser), patch(
            'pipeline.flow.service.network_available', return_value=True
        ), patch.object(self.service, '_ensure_shared_reconnect') as reconnect:
            with self.assertRaises(_FlowNetworkInterrupted):
                asyncio.run(self.service._run_once('j'))
        reconnect.assert_not_called()
        self.assertEqual(store.get_row('jobs', 'j')['stage'], 'waiting_network')
        self.assertEqual(store.get_row('jobs', 'j')['submissionStartedAt'], 123)
        self.assertEqual(store.get_row('jobs', 'j')['mediaIds'], ['original'])
        self.assertEqual(store.get_row('accounts', 'a')['status'], 'online')

    def test_probe_true_goto_timeout_still_waits_network(self):
        browser = Mock(
            start=AsyncMock(side_effect=RuntimeError(
                'Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to "https://flow.google.com/"'
            )),
            stop=AsyncMock(),
        )
        with patch('pipeline.flow.browser.BrowserManager', return_value=browser), patch(
            'pipeline.flow.service.network_available', return_value=True
        ), patch.object(self.service, '_ensure_shared_reconnect') as reconnect:
            with self.assertRaises(_FlowNetworkInterrupted):
                asyncio.run(self.service._run_once('j'))
        reconnect.assert_not_called()
        self.assertEqual(store.get_row('jobs', 'j')['stage'], 'waiting_network')
        self.assertEqual(store.get_row('accounts', 'a')['status'], 'online')

    def test_browser_never_launches_when_offline(self):
        from pipeline.flow.browser import BrowserManager
        with patch('pipeline.flow.browser.chrome_executable', return_value='/unused/chrome'), patch(
            'pipeline.flow.browser.network_available', return_value=False
        ), patch('pipeline.flow.browser.async_playwright') as launch:
            browser = BrowserManager(headless=True, profile_dir=store.ROOT / 'profile')
            with self.assertRaisesRegex(RuntimeError, 'FLOW_NETWORK_UNAVAILABLE'):
                asyncio.run(browser.start())
        launch.assert_not_called()


class ProviderNetworkTests(unittest.TestCase):
    def test_cloud_provider_down_is_bounded_while_internet_is_online(self):
        for fn in [cloud._openai_compatible_chat, cloud._gemini_generate]:
            with self.subTest(provider=fn.__name__), patch.object(cloud, 'wait_for_network', return_value=0), patch.object(
                cloud.httpx, 'Client'
            ) as client, patch.object(cloud.time, 'sleep'):
                post = client.return_value.__enter__.return_value.post
                post.side_effect = httpx.ConnectError('provider down')
                with self.assertRaisesRegex(RuntimeError, 'NETWORK_UNAVAILABLE'):
                    fn(base_url='https://example.com', api_key='test', model='test', prompt='test')
                self.assertEqual(post.call_count, 3)

    def test_cloud_outage_does_not_spend_transport_retries(self):
        with patch.object(cloud, 'wait_for_network', side_effect=[0, 10, 0, 10, 0, 10, 0]), patch.object(cloud.httpx, 'Client') as client:
            post = client.return_value.__enter__.return_value.post
            post.side_effect = [httpx.ConnectError('offline')] * 3 + [httpx.Response(200, json={'choices': [{'message': {'content': 'translated'}}]})]
            result = cloud._openai_compatible_chat(base_url='https://example.com', api_key='test', model='test', prompt='test')
        self.assertEqual(result, 'translated')

    def test_tts_resumes_readonly_request_but_not_ambiguous_create(self):
        for path, expected_calls in [('/common_task/query', 2), ('/common_task/new', 1)]:
            with self.subTest(path=path), patch.object(capcut, 'wait_for_network', side_effect=[0, 20, 0]), patch.object(capcut.httpx, 'AsyncClient') as client:
                request = AsyncMock(side_effect=[httpx.ReadError('offline'), httpx.Response(200)])
                client.return_value.__aenter__.return_value.request = request
                if expected_calls == 2:
                    self.assertEqual(capcut._request('POST', 'https://example.com' + path).status_code, 200)
                else:
                    with self.assertRaises(httpx.ReadError):
                        capcut._request('POST', 'https://example.com' + path)
                self.assertEqual(request.await_count, expected_calls)

    def test_stt_provider_down_does_not_loop_and_cancel_reaches_wait(self):
        client = Mock()
        client.request.side_effect = httpx.ConnectError('provider down')
        check = Mock(return_value=False)
        with patch.object(capcut_stt, 'wait_for_network', return_value=0) as wait:
            with self.assertRaises(httpx.ConnectError):
                capcut_stt._json(client, 'GET', 'https://example.com', headers={}, check=check)
        self.assertEqual(client.request.call_count, 1)
        self.assertIs(wait.call_args.kwargs['cancel_check'], check)

    def test_stt_upload_retry_rewinds_body(self):
        stream = io.BytesIO(b'original-body')
        bodies = []
        def request(*args, **kwargs):
            bodies.append(kwargs['content'].read())
            if len(bodies) == 1:
                raise httpx.WriteError('offline')
            return httpx.Response(200, json={'code': 0})
        with patch.object(capcut_stt, 'wait_for_network', side_effect=[0, 10, 0]):
            capcut_stt._json(Mock(request=request), 'POST', 'https://example.com?phase=transfer', headers={}, content=stream)
        self.assertEqual(bodies, [b'original-body', b'original-body'])


if __name__ == '__main__':
    unittest.main()
