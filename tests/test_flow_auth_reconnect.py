import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow._flow._api import FlowAPI
from pipeline.flow._flow._exceptions import AuthError
from pipeline.flow.service import _session_needs_login


class FlowAuthReconnectTests(unittest.IsolatedAsyncioTestCase):
    def test_batch_check_401_message_triggers_session_login(self):
        err = Exception(
            "HTTP 401 on batchCheckAsyncVideoGenerationStatus: authentication failed. "
            "Session cookies may be expired — re-open the browser."
        )
        self.assertTrue(_session_needs_login(err))

    async def test_fetch_refreshes_bearer_and_retries_on_401(self):
        api = FlowAPI(MagicMock(), project_id="proj")
        ok = MagicMock()
        ok.status = 200
        ok.json = AsyncMock(return_value={"operations": []})
        unauthorized = MagicMock()
        unauthorized.status = 401
        unauthorized.text = AsyncMock(return_value="unauthenticated")

        ctx = MagicMock()
        ctx.post = AsyncMock(side_effect=[unauthorized, ok])
        api._bm.context.request = ctx
        api._get_auth_headers = AsyncMock(return_value={"authorization": "Bearer ya29.old"})
        api._force_refresh_session = AsyncMock(return_value="ya29.new")

        result = await api._fetch("POST", "video:batchCheckAsyncVideoGenerationStatus", {"media": []})
        self.assertEqual(result, {"operations": []})
        self.assertEqual(ctx.post.await_count, 2)
        api._force_refresh_session.assert_awaited_once()

    async def test_fetch_raises_auth_error_after_refresh_still_401(self):
        api = FlowAPI(MagicMock(), project_id="proj")
        unauthorized = MagicMock()
        unauthorized.status = 401
        unauthorized.text = AsyncMock(return_value="unauthenticated")
        ctx = MagicMock()
        ctx.post = AsyncMock(return_value=unauthorized)
        api._bm.context.request = ctx
        api._get_auth_headers = AsyncMock(return_value={"authorization": "Bearer ya29.old"})
        api._force_refresh_session = AsyncMock(return_value="ya29.new")

        with self.assertRaises(AuthError):
            await api._fetch("POST", "video:batchCheckAsyncVideoGenerationStatus", {"media": []})
        self.assertEqual(ctx.post.await_count, 2)


if __name__ == "__main__":
    unittest.main()
