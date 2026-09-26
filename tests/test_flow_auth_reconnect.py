import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow._flow._api import FlowAPI
from pipeline.flow._flow._exceptions import AuthError, GenerationError
from pipeline.flow.service import FlowService, _session_needs_login


class FlowAuthReconnectTests(unittest.IsolatedAsyncioTestCase):
    def test_login_wall_triggers_session_login(self):
        err = Exception(
            "LOGIN_REQUIRED: HTTP 401 on batchCheckAsyncVideoGenerationStatus: "
            "session redirected to login"
        )
        self.assertTrue(_session_needs_login(err))

    def test_missing_bearer_alone_does_not_trigger_chrome_login(self):
        err = Exception(
            "LOGIN_REQUIRED: Could not obtain Bearer token — re-login required"
        )
        self.assertFalse(_session_needs_login(err))

    def test_api_auth_rejected_without_wall_does_not_trigger_chrome(self):
        err = Exception(
            "HTTP 401 on batchCheck: API auth rejected "
            "(Flow session cookie still valid — avoid reconnect storm)."
        )
        self.assertFalse(_session_needs_login(err))

    async def test_fetch_without_bearer_still_sends_request(self):
        api = FlowAPI(MagicMock(), project_id="proj")
        unauthorized = MagicMock()
        unauthorized.status = 401
        unauthorized.text = AsyncMock(return_value="unauthenticated")
        ctx = MagicMock()
        ctx.post = AsyncMock(return_value=unauthorized)
        api._bm.context.request = ctx
        api._get_auth_headers = AsyncMock(return_value={"content-type": "text/plain"})
        page = MagicMock()
        page.url = "https://flow.google.com/project/proj"
        api._bm.page = AsyncMock(return_value=page)

        with self.assertRaises(GenerationError) as ctx_err:
            await api._fetch("POST", "video:batchCheckAsyncVideoGenerationStatus", {"media": []})
        self.assertIn("avoid reconnect storm", str(ctx_err.exception))
        ctx.post.assert_awaited_once()

    async def test_fetch_401_on_login_wall_raises_auth_error(self):
        api = FlowAPI(MagicMock(), project_id="proj")
        unauthorized = MagicMock()
        unauthorized.status = 401
        unauthorized.text = AsyncMock(return_value="unauthenticated")
        ctx = MagicMock()
        ctx.post = AsyncMock(return_value=unauthorized)
        api._bm.context.request = ctx
        api._get_auth_headers = AsyncMock(return_value={"authorization": "Bearer ya29.old"})
        page = MagicMock()
        page.url = "https://accounts.google.com/ServiceLogin"
        api._bm.page = AsyncMock(return_value=page)

        with self.assertRaises(AuthError) as ctx_err:
            await api._fetch("POST", "video:batchCheckAsyncVideoGenerationStatus", {"media": []})
        self.assertIn("LOGIN_REQUIRED", str(ctx_err.exception))

    async def test_get_bearer_token_optional_returns_empty(self):
        api = FlowAPI(MagicMock(), project_id="proj")
        api._bearer_token = ""
        api._bearer_token_ts = 0.0
        page = MagicMock()
        page.url = "https://flow.google.com/project/proj"
        page.evaluate = AsyncMock(return_value=None)
        page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("no cdp"))
        api._bm.page = AsyncMock(return_value=page)

        token = await api._get_bearer_token(required=False)
        self.assertEqual(token, "")

    def test_ui_interceptor_seeds_api_bearer(self):
        from pipeline.flow._flow._ui_interceptor import UIInterceptor

        api = MagicMock()
        interceptor = UIInterceptor(api)
        request = MagicMock()
        request.url = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"
        request.headers = {"authorization": "Bearer ya29.live"}
        request.method = "POST"
        request.post_data = '{"media": []}'
        interceptor._on_request(request)
        api.seed_bearer_token.assert_called_once_with("ya29.live")

    def test_ensure_shared_reconnect_is_single_flight(self):
        service = FlowService()
        account = {"id": "acc1", "status": "online", "projectId": "p1"}

        def connect_side_effect(account_id, force_interactive=False):
            service._connecting_accounts.add(account_id)
            return account

        connect = MagicMock(side_effect=connect_side_effect)
        with patch("pipeline.flow.service.store.get_row", return_value=dict(account)), \
             patch("pipeline.flow.service.store.patch_row") as patch_row, \
             patch.object(service, "connect", connect), \
             patch.object(service, "_log"):
            service._ensure_shared_reconnect("acc1", job_id="j1", reason="LOGIN_REQUIRED: redirected to login")
            service._ensure_shared_reconnect("acc1", job_id="j2", reason="LOGIN_REQUIRED: redirected to login")
        connect.assert_called_once_with("acc1", force_interactive=True)
        self.assertTrue(any(
            call.args[0] == "accounts" and call.args[2].get("status") == "reconnect"
            for call in patch_row.call_args_list
        ))

    def test_ensure_shared_reconnect_skips_when_already_connecting(self):
        service = FlowService()
        service._connecting_accounts.add("acc1")
        connect = MagicMock()
        with patch("pipeline.flow.service.store.get_row", return_value={"id": "acc1", "status": "reconnect"}), \
             patch.object(service, "connect", connect):
            service._ensure_shared_reconnect("acc1")
        connect.assert_not_called()

    def test_ensure_bearer_miss_uses_headless_not_force_chrome(self):
        service = FlowService()
        account = {"id": "acc1", "status": "reconnect", "projectId": "p1"}
        connect = MagicMock(return_value=account)
        with patch("pipeline.flow.service.store.get_row", return_value=dict(account)), \
             patch.object(service, "connect", connect), \
             patch.object(service, "_log"):
            service._ensure_shared_reconnect(
                "acc1",
                reason="LOGIN_REQUIRED: Could not obtain Bearer token — re-login required",
            )
        connect.assert_called_once_with("acc1", force_interactive=False)

    def test_requeue_auth_failed_jobs_only_session_errors(self):
        service = FlowService()
        jobs = [
            {
                "id": "a1",
                "accountId": "acc1",
                "status": "action_required",
                "error": "LOGIN_REQUIRED: session redirected to login",
            },
            {
                "id": "a2",
                "accountId": "acc1",
                "status": "action_required",
                "error": "FLOW_CREDITS_EMPTY: out",
            },
            {
                "id": "a3",
                "accountId": "acc2",
                "status": "action_required",
                "error": "LOGIN_REQUIRED: redirected to login",
            },
        ]
        patched = []

        def patch_row(table, row_id, data):
            patched.append((table, row_id, dict(data)))

        with patch(
            "pipeline.flow.service.store.get_row",
            return_value={"id": "acc1", "status": "online", "projectId": "p1"},
        ), patch("pipeline.flow.service.store.list_rows", return_value=jobs), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "_log"), \
             patch("pipeline.flow.service.threading.Thread") as thread_cls:
            count = service._requeue_auth_failed_jobs("acc1")
        self.assertEqual(count, 1)
        self.assertEqual(patched[0][1], "a1")
        self.assertEqual(patched[0][2]["status"], "queued")
        thread_cls.assert_called_once()

    def test_zero_credits_precede_session_sync_error(self):
        service = FlowService()
        with patch(
            "pipeline.flow.service.store.get_row",
            return_value={"id": "acc1", "status": "online", "projectId": "p1", "credits": 0},
        ), patch.object(service, "sync_credits_for_account") as sync:
            with self.assertRaises(ValueError) as ctx:
                service._verify_account_plan_before_enqueue("acc1")
        self.assertIn("FLOW_CREDITS_EMPTY", str(ctx.exception))
        sync.assert_not_called()

    def test_verified_account_cost_blocks_when_balance_is_lower(self):
        from pipeline.flow.service import _ensure_flow_credits_for_job

        with self.assertRaises(ValueError) as ctx:
            _ensure_flow_credits_for_job(
                {
                    "credits": 4,
                    "capabilityStatus": "verified",
                    "capabilityCatalog": {
                        "video": {"models": [{"name": "Omni 1.1 Flash", "creditCost": 10}]},
                    },
                },
                "video",
                {"model": "Omni 1.1 Flash"},
            )
        self.assertIn("FLOW_CREDITS_INSUFFICIENT", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
