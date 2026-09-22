import hashlib
import io
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.core.runtime_download import Wheel, RuntimeInstallError, classify_error, download_wheel


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {'Content-Length': str(len(data))}
    def geturl(self):
        return 'https://example.invalid/wheel'


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = b'verified-wheel-content'
        self.wheel = Wheel('test', '1', 'test-1-py3-none-any.whl', 'https://example.invalid/wheel', hashlib.sha256(self.data).hexdigest(), len(self.data))
        self.stop = threading.Event()
        self.report = Mock()
    def run_download(self, opener):
        return download_wheel(self.wheel, self.root, self.report, self.stop, opener=opener, sleep=lambda _: None)

    def test_verified_cache_skips_network(self):
        self.run_download(lambda *a, **k: Response(self.data))
        opener = Mock(side_effect=AssertionError('network should not run'))
        self.assertEqual(self.run_download(opener).read_bytes(), self.data)
        self.assertTrue(self.report.call_args.args[3])

    def test_resume_and_server_ignoring_range(self):
        for partial_response in (True, False):
            with self.subTest(partial_response=partial_response):
                target = self.wheel.path(self.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.with_suffix('.whl.part').write_bytes(self.data[:3])
                def open_request(request, **kwargs):
                    self.assertEqual(request.get_header('Range'), 'bytes=3-')
                    return Response(self.data[3:], 206, {'Content-Range': f'bytes 3-{len(self.data)-1}/{len(self.data)}'}) if partial_response else Response(self.data)
                self.assertEqual(self.run_download(open_request).read_bytes(), self.data)

    def test_bad_checksum_removes_only_bad_partial(self):
        sentinel = self.root / 'active-runtime'
        sentinel.write_text('keep')
        with self.assertRaises(RuntimeInstallError) as error:
            self.run_download(lambda *a, **k: Response(b'x' * len(self.data)))
        self.assertEqual(error.exception.code, 'CHECKSUM_MISMATCH')
        self.assertFalse(self.wheel.path(self.root).with_suffix('.whl.part').exists())
        self.assertTrue(sentinel.exists())

    def test_retry_limit(self):
        open_request = Mock(side_effect=urllib.error.URLError(socket.gaierror('DNS error')))
        with self.assertRaises(RuntimeInstallError) as error:
            self.run_download(open_request)
        self.assertEqual(error.exception.code, 'DNS_FAILED')
        self.assertEqual(open_request.call_count, 3)
        self.assertIn('attempt', error.exception.diagnostics)

    def test_http_failures_retry_only_transient_codes(self):
        for status, expected, attempts in [(401, 'HTTP_UNAUTHORIZED', 1), (403, 'HTTP_FORBIDDEN', 1), (404, 'HTTP_NOT_FOUND', 1), (407, 'PROXY_AUTH_FAILED', 1), (429, 'HTTP_RATE_LIMITED', 3), (503, 'INDEX_UNAVAILABLE', 3)]:
            opener = Mock(side_effect=urllib.error.HTTPError(self.wheel.url, status, 'test', {}, None))
            with self.subTest(status=status), self.assertRaises(RuntimeInstallError) as error:
                self.run_download(opener)
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(opener.call_count, attempts)

    def test_tls_timeout_disk_and_resolver_classification(self):
        for exception, code, retry in [
            (urllib.error.URLError(ssl.SSLCertVerificationError('bad cert')), 'TLS_CERTIFICATE_FAILED', False),
            (TimeoutError(), 'NETWORK_TIMEOUT', True),
            (OSError(28, 'disk full'), 'DISK_FULL', False),
            (RuntimeError('No solution found'), 'DEPENDENCY_RESOLUTION_FAILED', False),
        ]:
            self.assertEqual(classify_error(exception), (code, retry))

    def test_short_body_retries_with_range_and_real_byte_counts(self):
        responses = [Response(self.data[:3], headers={'Content-Length': str(len(self.data))}),
                     Response(self.data[3:], 206, {'Content-Range': f'bytes 3-{len(self.data)-1}/{len(self.data)}'})]
        opener = Mock(side_effect=responses)
        self.assertEqual(self.run_download(opener).read_bytes(), self.data)
        self.assertEqual(opener.call_args.args[0].get_header('Range'), 'bytes=3-')
        self.assertEqual(self.report.call_args.args[1:3], (len(self.data), len(self.data)))
