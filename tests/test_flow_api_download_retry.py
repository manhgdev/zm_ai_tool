"""CDN download retries for transient TLS / ECONNRESET blips."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow._flow._api import FlowAPI
from pipeline.flow._flow._download_errors import is_transient_download_error
from pipeline.flow._flow._exceptions import GenerationError


class TransientDownloadErrorTest(unittest.TestCase):
    def test_econnreset_is_transient(self) -> None:
        self.assertTrue(is_transient_download_error(
            "Client network socket disconnected before secure TLS connection was established (ECONNRESET)"
        ))
        self.assertFalse(is_transient_download_error("Download failed HTTP 404"))


class FlowApiDownloadRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_econnreset_then_succeeds(self) -> None:
        blip = RuntimeError(
            "Client network socket disconnected before secure TLS connection was established (ECONNRESET)"
        )
        ok = SimpleNamespace(status=200, body=AsyncMock(return_value=b"img"))
        getter = AsyncMock(side_effect=[blip, blip, ok])
        api = FlowAPI(SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=getter))))
        with tempfile.TemporaryDirectory() as raw, patch(
            "pipeline.flow._flow._api.asyncio.sleep", new_callable=AsyncMock
        ):
            path = Path(raw) / "out.jpg"
            result = await api.download("https://flow-content.google/image/x", path)
            self.assertEqual(result.read_bytes(), b"img")
        self.assertEqual(getter.await_count, 3)

    async def test_exhaustion_wraps_flow_download_failed(self) -> None:
        blip = RuntimeError(
            "Client network socket disconnected before secure TLS connection was established (ECONNRESET)"
        )
        getter = AsyncMock(side_effect=blip)
        api = FlowAPI(SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=getter))))
        with tempfile.TemporaryDirectory() as raw, patch(
            "pipeline.flow._flow._api.asyncio.sleep", new_callable=AsyncMock
        ):
            with self.assertRaisesRegex(GenerationError, "FLOW_DOWNLOAD_FAILED"):
                await api.download("https://flow-content.google/image/x", Path(raw) / "out.jpg")
        self.assertEqual(getter.await_count, 3)


if __name__ == "__main__":
    unittest.main()
