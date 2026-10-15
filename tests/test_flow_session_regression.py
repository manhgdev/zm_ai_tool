"""Selected account and bounded session recovery regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pipeline.flow import store
from pipeline.flow.service import FlowService, _is_automation_block_error


class FlowSessionRegressionTest(unittest.IsolatedAsyncioTestCase):
    def test_reconnected_job_is_replayed_once(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "projectId": "p"}
        job = {"id": "j", "accountId": "a", "status": "action_required",
               "error": "FLOW_LOGIN_REQUIRED: redirected to /about"}

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            (account if table == "accounts" else job).update(data)
            return job

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[job]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch("pipeline.flow.service.threading.Thread") as thread, \
             patch.object(service, "_log"), patch.object(service, "connect") as connect:
            self.assertEqual(service._requeue_auth_failed_jobs("a"), 1)
            job.update(status="action_required", error="FLOW_LOGIN_REQUIRED: redirected to /about")
            service._ensure_shared_reconnect("a", job_id="j", reason=job["error"])
            self.assertEqual(service._requeue_auth_failed_jobs("a"), 0)
            connect.assert_not_called()
            thread.assert_called_once()

    def test_explicit_retry_resets_auth_replay_budget(self):
        service = FlowService()
        account = {"id": "a", "status": "online", "projectId": "p", "plan": "Ultra", "credits": 100}
        job = {"id": "j", "accountId": "a", "kind": "image", "mode": "text",
               "status": "action_required", "authRetryCount": 1, "settings": {}}

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            job.update(data)
            return job

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch("pipeline.flow.service.threading.Thread"), patch.object(service, "_log"):
            retried = service.retry("j")
        self.assertEqual(retried["authRetryCount"], 0)
        self.assertEqual(retried["accountId"], "a")
        self.assertEqual(retried["status"], "queued")

    def test_manual_retry_clears_selected_account_suspension(self):
        service = FlowService()
        account = {
            "id": "a", "status": "online", "projectId": "p", "plan": "Ultra",
            "credits": 100, "suspendedUntil": 9999999999,
            "suspendReason": "FLOW_AUTOMATION_BLOCKED: abnormal activity",
        }
        job = {
            "id": "j", "accountId": "a", "kind": "image", "mode": "text",
            "status": "failed", "settings": {},
        }

        def get_row(table, row_id):
            return account if table == "accounts" else job

        def patch_row(table, row_id, data):
            job.update(data)
            return job

        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.list_rows", return_value=[]), \
             patch("pipeline.flow.service.store.patch_row", side_effect=patch_row), \
             patch.object(service, "clear_account_suspension") as clear, \
             patch("pipeline.flow.service.threading.Thread"), patch.object(service, "_log"):
            service.retry("j", {"manual": True})
            clear.assert_called_once_with("a")
            clear.reset_mock()
            service.retry("j")
            clear.assert_not_called()

    def test_blocked_selected_account_falls_back_after_unusual_activity(self):
        service = FlowService()
        job = {"id": "j", "status": "failed", "accountId": "selected", "randomAccount": False, "allowAccountFallback": False}
        alt = {"id": "other", "label": "other"}
        with patch("pipeline.flow.service.store.get_row", return_value=job), \
             patch("pipeline.flow.service.store.patch_row") as update, \
             patch("pipeline.flow.service.store.list_rows", return_value=[]), \
             patch.object(service, "suspend_account"), \
             patch.object(service, "_pick_eligible_account", return_value=alt), \
             patch("threading.Thread"):
            self.assertTrue(service._try_fallback_account("j", "selected", "FLOW_AUTOMATION_BLOCKED: warning"))
            update.assert_called()

    def test_blocked_selected_account_never_falls_back_for_other_errors(self):
        for reason in ("FLOW_OUTPUT_UNAVAILABLE: unavailable", "FLOW_GENERATION_TIMEOUT: timeout"):
            for explicit in (False, None):
                with self.subTest(reason=reason, explicit=explicit):
                    service = FlowService()
                    job = {"id": "j", "status": "failed", "accountId": "selected", "randomAccount": False}
                    if explicit is not None:
                        job["allowAccountFallback"] = explicit
                    with patch("pipeline.flow.service.store.get_row", return_value=job), \
                         patch("pipeline.flow.service.store.patch_row") as update, \
                         patch.object(service, "suspend_account"), \
                         patch.object(service, "_pick_eligible_account") as picker:
                        self.assertFalse(service._try_fallback_account("j", "selected", reason))
                        picker.assert_not_called()
                        update.assert_not_called()

    def test_generation_timeout_does_not_suspend_account(self):
        service = FlowService()
        account = {"id": "a", "label": "account", "status": "online"}
        with patch("pipeline.flow.service.store.get_row", return_value=account), \
             patch("pipeline.flow.service.store.patch_row") as patch_row:
            result = service.suspend_account("a", "FLOW_GENERATION_TIMEOUT: no completed image media appeared after submit")
        self.assertEqual(result["id"], "a")
        patch_row.assert_not_called()

    def test_unusual_activity_recreates_account_and_reassigns_queued(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            jar = [{"name": "SID", "value": "tok", "domain": ".google.com", "path": "/", "secure": True}]
            account = {"id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100, "cookie": jar}
            store._write("accounts", [account])
            store._write("jobs", [
                {"id": "j", "accountId": "a", "status": "queued", "kind": "image", "queueOrder": 0, "settings": {},
                 "autoRetryCount": 1},
                {"id": "sibling", "accountId": "a", "status": "processing", "kind": "video", "queueOrder": 1, "settings": {}},
                {"id": "q2", "accountId": "a", "status": "queued", "kind": "image", "queueOrder": 2, "settings": {}},
            ])

            attempts = []

            async def run(job_id, **kwargs):
                attempts.append((job_id, store.get_row("jobs", job_id)["accountId"]))
                if len(attempts) == 1:
                    store.patch_row("jobs", job_id, {
                        "status": "failed", "stage": "failed", "submissionStartedAt": 1.0,
                        "error": "FLOW_AUTOMATION_BLOCKED: We noticed some unusual activity",
                    })
                else:
                    store.patch_row("jobs", job_id, {"status": "done", "stage": "done"})

            def fake_recreate(old_id):
                store.put_row("accounts", {
                    "id": "b", "status": "online", "projectId": "p2", "plan": "Pro",
                    "authMethod": "cookie", "credits": 100,
                })
                store.patch_row("accounts", old_id, {"replacedBy": "b", "isDefault": False})
                service._mark_session_ready("b")
                return "b"

            class ImmediateThread:
                def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                    self._target = target
                    self._args = args or ()

                def start(self):
                    if self._target:
                        self._target(*self._args)

            def verify(account_id, kind="video", settings=None):
                return store.get_row("accounts", str(account_id)) or account

            with patch.object(service, "_verify_account_plan_before_enqueue", side_effect=verify), \
                 patch.object(service, "_run", side_effect=run) as runner, \
                 patch.object(service, "_recreate_account_from_cookies", side_effect=fake_recreate) as recreate, \
                 patch.object(service, "_maybe_delete_superseded_account"), \
                 patch("pipeline.flow.service.threading.Thread", ImmediateThread), \
                 patch("pipeline.flow.service.time.sleep") as sleeper:
                service._run_sync("j")
            recreate.assert_called_once_with("a")
            sleeper.assert_any_call(3)
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(attempts[0], ("j", "a"))
            self.assertEqual(attempts[1], ("j", "b"))
            self.assertEqual(store.get_row("jobs", "j")["accountId"], "b")
            self.assertEqual(store.get_row("jobs", "j")["status"], "done")
            self.assertEqual(store.get_row("jobs", "q2")["accountId"], "b")
            self.assertEqual(store.get_row("jobs", "sibling")["accountId"], "a")
            self.assertEqual(store.get_row("jobs", "sibling")["status"], "processing")
            self.assertEqual(store.get_row("accounts", "a")["replacedBy"], "b")

    def test_recreate_account_from_cookies_connects_fresh_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            jar = [
                {"name": "SID", "value": "tok", "domain": ".google.com", "path": "/", "secure": True, "sameSite": "Lax"},
            ]
            store._write("accounts", [{
                "id": "a", "label": "acc", "email": "a@gmail.com", "status": "online",
                "plan": "Pro", "authMethod": "cookie", "cookie": jar, "projectId": "old-p",
            }])
            with patch.object(service, "connect") as connect:
                new_id = service._recreate_account_from_cookies("a")
            self.assertTrue(new_id)
            self.assertNotEqual(new_id, "a")
            new_row = store.get_row("accounts", new_id)
            self.assertEqual(new_row["authMethod"], "cookie")
            self.assertEqual(new_row["status"], "connecting")
            self.assertIsNone(new_row.get("projectId"))
            self.assertEqual(store.get_row("accounts", "a")["replacedBy"], new_id)
            connect.assert_called_once()
            self.assertEqual(connect.call_args.args[0], new_id)
            injected = connect.call_args.kwargs.get("cookies") or []
            self.assertEqual(injected[0]["sameSite"], "None")
            self.assertIsNone(service._recreate_account_from_cookies("missing"))
            store.patch_row("accounts", "a", {"cookie": []})
            # Already replaced — reuse new_id without a second connect storm.
            connect.reset_mock()
            self.assertEqual(service._recreate_account_from_cookies("a"), new_id)
            connect.assert_not_called()

    def test_recreate_account_without_cookie_jar_returns_none(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            store._write("accounts", [{
                "id": "a", "label": "acc", "status": "online", "plan": "Pro", "cookie": [],
            }])
            with patch.object(service, "connect") as connect, \
                 patch.object(service, "save_account") as save:
                self.assertIsNone(service._recreate_account_from_cookies("a"))
            save.assert_not_called()
            connect.assert_not_called()
            self.assertIsNone((store.get_row("accounts", "a") or {}).get("replacedBy"))

    def test_unusual_activity_without_jar_keeps_failed_job(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            store._write("accounts", [
                {"id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100, "cookie": []},
            ])
            store._write("jobs", [
                {"id": "j", "accountId": "a", "status": "queued", "kind": "image", "queueOrder": 0, "settings": {},
                 "autoRetryCount": 1},
            ])

            async def run(job_id, **kwargs):
                store.patch_row("jobs", job_id, {
                    "status": "failed", "stage": "failed", "submissionStartedAt": 1.0,
                    "error": "FLOW_AUTOMATION_BLOCKED: We noticed some unusual activity",
                })

            def verify(account_id, kind="video", settings=None):
                return store.get_row("accounts", str(account_id)) or {}

            with patch.object(service, "_verify_account_plan_before_enqueue", side_effect=verify), \
                 patch.object(service, "_run", side_effect=run), \
                 patch.object(service, "_recreate_account_from_cookies", return_value=None) as recreate, \
                 patch.object(service, "_maybe_delete_superseded_account"), \
                 patch("pipeline.flow.service.time.sleep"):
                service._run_sync("j")
            recreate.assert_called_once_with("a")
            job = store.get_row("jobs", "j")
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["accountId"], "a")
            self.assertIn("FLOW_AUTOMATION_BLOCKED", job["error"])

    def test_unusual_activity_waits_for_new_account_online_before_rerun(self):
        """Reassigned job must not leave admission until the replacement is ready."""
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            jar = [{"name": "SID", "value": "tok", "domain": ".google.com", "path": "/", "secure": True}]
            store._write("accounts", [
                {"id": "a", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100, "cookie": jar},
            ])
            store._write("jobs", [
                {"id": "j", "accountId": "a", "status": "queued", "kind": "image", "queueOrder": 0, "settings": {},
                 "autoRetryCount": 1},
            ])
            attempts = []
            gate = {"ready": False}

            async def run(job_id, **kwargs):
                attempts.append(store.get_row("jobs", job_id)["accountId"])
                if len(attempts) == 1:
                    store.patch_row("jobs", job_id, {
                        "status": "failed", "stage": "failed", "submissionStartedAt": 1.0,
                        "error": "FLOW_AUTOMATION_BLOCKED: We noticed some unusual activity",
                    })
                else:
                    store.patch_row("jobs", job_id, {"status": "done", "stage": "done"})

            def fake_recreate(old_id):
                store.put_row("accounts", {
                    "id": "b", "status": "connecting", "plan": "Pro", "authMethod": "cookie", "credits": 100,
                })
                store.patch_row("accounts", old_id, {"replacedBy": "b"})
                service._connecting_accounts.add("b")
                return "b"

            class ImmediateThread:
                def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                    self._target = target
                    self._args = args or ()

                def start(self):
                    if self._target:
                        self._target(*self._args)

            waits = {"n": 0}

            def wait_side_effect(timeout=None):
                waits["n"] += 1
                if waits["n"] >= 2 and not gate["ready"]:
                    gate["ready"] = True
                    store.patch_row("accounts", "b", {"status": "online", "projectId": "p2"})
                    service._connecting_accounts.discard("b")
                    service._mark_session_ready("b")
                return True

            def verify(account_id, kind="video", settings=None):
                return store.get_row("accounts", str(account_id)) or {}

            with patch.object(service, "_verify_account_plan_before_enqueue", side_effect=verify), \
                 patch.object(service, "_run", side_effect=run) as runner, \
                 patch.object(service, "_recreate_account_from_cookies", side_effect=fake_recreate), \
                 patch.object(service, "_maybe_delete_superseded_account"), \
                 patch("pipeline.flow.service.threading.Thread", ImmediateThread), \
                 patch("pipeline.flow.service.time.sleep"), \
                 patch.object(service._account_condition, "wait", side_effect=wait_side_effect):
                service._run_sync("j")
            self.assertEqual(attempts, ["a", "b"])
            self.assertGreaterEqual(waits["n"], 2)
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(store.get_row("jobs", "j")["status"], "done")

    def test_pick_skips_superseded_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            store._write("accounts", [
                {"id": "old", "status": "online", "projectId": "p", "plan": "Pro", "credits": 100, "replacedBy": "new"},
                {"id": "new", "status": "online", "projectId": "p2", "plan": "Pro", "credits": 100},
            ])
            picked = service._pick_eligible_account(kind="image")
            self.assertEqual(picked["id"], "new")

    def test_maybe_delete_superseded_account_waits_for_idle(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            service = FlowService()
            store._write("accounts", [
                {"id": "old", "status": "online", "replacedBy": "new", "plan": "Pro"},
                {"id": "new", "status": "online", "plan": "Pro", "projectId": "p"},
            ])
            store._write("jobs", [
                {"id": "sib", "accountId": "old", "status": "processing", "kind": "video"},
            ])
            with patch.object(service, "delete_account") as delete:
                service._maybe_delete_superseded_account("old")
                delete.assert_not_called()
                store.patch_row("jobs", "sib", {"status": "done"})
                service._maybe_delete_superseded_account("old")
                delete.assert_called_once_with("old")

    async def test_rising_flow_progress_extends_generation_timeout(self):
        service = FlowService()
        clock = iter(range(0, 10_000, 1))
        result = {"id": "new", "tag": "img", "src": "https://example.test/image.png"}
        # Base timeout is 3s; Flow keeps advancing, so the result at tick ~8 is still collected.
        media = [[]] * 6 + [[result]]
        with patch("pipeline.flow.service.time.monotonic", side_effect=lambda: next(clock)), \
             patch.object(service, "_project_media_elements", AsyncMock(side_effect=media)), \
             patch.object(service, "_claim_visible_flow_error", AsyncMock(return_value="")), \
             patch.object(service, "_flow_tile_percent", AsyncMock(side_effect=[10, 20, 30, 40, 50, 60])), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j", "progress": 0}), \
             patch("pipeline.flow.service.store.patch_row"), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            self.assertEqual(await service._wait_for_project_media(page=None, baseline_ids=set(), kind="image",
                                                                   expected_count=1, job_id="j", timeout_s=3), [result])

    async def test_rpc_names_extend_generation_timeout_without_percent(self):
        """Mute % still slides the soft deadline while RPC ownership is alive."""
        service = FlowService()
        clock = iter(range(0, 10_000, 1))
        result = {"id": "new", "tag": "img", "src": "https://example.test/image.png"}
        # Past the 3s base (no %); rpc ownership must slide the soft deadline.
        media = [[]] * 6 + [[result]]
        response = MagicMock(ok=True)
        response.text = AsyncMock(return_value="")
        with patch("pipeline.flow.service.time.monotonic", side_effect=lambda: next(clock)), \
             patch.object(service, "_project_media_elements", AsyncMock(side_effect=media)), \
             patch.object(service, "_claim_visible_flow_error", AsyncMock(return_value="")), \
             patch.object(service, "_flow_tile_percent", AsyncMock(return_value=-1)), \
             patch.object(service, "_flow_pending_generation", AsyncMock(return_value=False)), \
             patch.object(service, "_rpc_media", AsyncMock(return_value=([], ["rpc-m1"]))), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j", "progress": 0}), \
             patch("pipeline.flow.service.store.patch_row"), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            self.assertEqual(
                await service._wait_for_project_media(
                    page=None, baseline_ids=set(), kind="image", expected_count=1,
                    job_id="j", timeout_s=3, rpc_responses=[response],
                ),
                [result],
            )

    async def test_pending_tile_extends_generation_timeout_without_percent(self):
        service = FlowService()
        clock = iter(range(0, 10_000, 1))
        result = {"id": "new", "tag": "img", "src": "https://example.test/image.png"}
        media = [[]] * 6 + [[result]]
        with patch("pipeline.flow.service.time.monotonic", side_effect=lambda: next(clock)), \
             patch.object(service, "_project_media_elements", AsyncMock(side_effect=media)), \
             patch.object(service, "_claim_visible_flow_error", AsyncMock(return_value="")), \
             patch.object(service, "_flow_tile_percent", AsyncMock(return_value=-1)), \
             patch.object(service, "_flow_pending_generation", AsyncMock(return_value=True)), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j", "progress": 0}), \
             patch("pipeline.flow.service.store.patch_row"), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            self.assertEqual(
                await service._wait_for_project_media(
                    page=None, baseline_ids=set(), kind="image", expected_count=1,
                    job_id="j", timeout_s=3,
                ),
                [result],
            )

    async def test_rpc_owned_media_is_claimed_when_flow_rewrites_the_prompt(self):
        service = FlowService()
        response = MagicMock(ok=True)
        response.json = AsyncMock(return_value={"media": [{
            "name": "m1", "image": {"generatedImage": {"fifeUrl": "https://flow.test/m1.jpg"}},
        }]})
        items, names = await service._rpc_media([response])
        self.assertEqual(items, [{"id": "m1", "src": "https://flow.test/m1.jpg"}])
        api = MagicMock()
        api.get_project_data = AsyncMock(return_value={"projectContents": {"media": [{
            "name": "m1", "image": {"generatedImage": {"fifeUrl": "https://flow.test/m1.jpg"}},
            "mediaMetadata": {"requestData": {"promptInputs": [{"textInput": "Morning mist over lake"}]}},
        }]}})
        job = {"id": "j", "prompt": "Buổi sáng yên bình bên hồ", "submissionStartedAt": 1.0, "rpcMediaIds": names}
        with patch.object(service, "jobs", return_value=[]):
            found = await service._find_existing_project_media(api, None, job, "image", 1)
        self.assertEqual([item["id"] for item in found], ["m1"])

    async def test_unusual_activity_tile_fails_only_the_job_that_owns_it(self):
        service = FlowService()
        page = AsyncMock()
        page.evaluate.return_value = [{"index": 0, "text": "Chúng tôi nhận thấy có hoạt động bất thường"}]
        with patch("pipeline.flow.service.store.get_row", return_value={"projectId": "p"}), \
             patch("pipeline.flow.service.store.patch_row"):
            first = await service._claim_visible_flow_error(page, {"id": "a", "accountId": "acc"}, 1)
            second = await service._claim_visible_flow_error(page, {"id": "b", "accountId": "acc"}, 1)
        self.assertTrue(first)
        # The sibling's Chrome is still generating: it keeps waiting for its image.
        self.assertEqual(second, "")

    async def test_new_unusual_tile_with_the_same_text_as_an_old_one_is_still_claimed(self):
        service = FlowService()
        page = AsyncMock()
        text = "Chúng tôi nhận thấy có hoạt động bất thường"
        page.evaluate.return_value = [{"index": 0, "text": text}]
        rows = {"old": {"id": "old", "accountId": "acc", "submissionStartedAt": 1.0},
                "new": {"id": "new", "accountId": "acc", "submissionStartedAt": 2.0e9, "baselineErrorTexts": [text]}}
        get_row = lambda table, row_id: {"projectId": "p"} if table == "accounts" else rows[row_id]
        with patch("pipeline.flow.service.store.get_row", side_effect=get_row), \
             patch("pipeline.flow.service.store.patch_row"):
            self.assertTrue(await service._claim_visible_flow_error(page, rows["old"], 1))
            # The old tile was already on screen at this submit: not ours.
            self.assertEqual(await service._claim_visible_flow_error(page, rows["new"], 1), "")
            page.evaluate.return_value = [{"index": 0, "text": text}, {"index": 1, "text": text}]
            self.assertEqual(await service._claim_visible_flow_error(page, rows["new"], 1), text)

    async def test_sibling_unusual_tile_does_not_fail_this_submit(self):
        service = FlowService()
        service._scan_visible_errors = AsyncMock(return_value=[{
            "index": 3, "text": "Chúng tôi nhận thấy có hoạt động bất thường", "tile": True,
        }])
        await service._raise_if_new_flow_error(MagicMock(), set())

    async def test_in_flight_generate_rpc_fails_on_unusual_activity_toast(self):
        page = MagicMock()
        page.locator.return_value.inner_text = AsyncMock(return_value="ok")
        service = FlowService()
        service._flow_submit_dom = AsyncMock(return_value={"clicked": True, "score": 90, "disabled": False})
        service._scan_visible_errors = AsyncMock(return_value=[{
            "index": 0, "text": "Chúng tôi nhận thấy có hoạt động bất thường",
        }])
        with self.assertRaisesRegex(RuntimeError, "FLOW_AUTOMATION_BLOCKED"):
            await service._click_flow_submit_and_confirm(
                page, 0, "", set(), [object()], [],
            )

    async def test_batchexecute_unusual_activity_fails_fast_as_automation_blocked(self):
        response = MagicMock(ok=True, status=200)
        response.text = AsyncMock(return_value=')]}\'\n[["wrb.fr","YhhmEf",null,null,null,[7,null,'
                                  '[["type.googleapis.com/google.rpc.ErrorInfo",["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]]]]]]')
        service = FlowService()
        service._flow_submit_dom = AsyncMock(return_value={"clicked": True, "score": 90, "disabled": False})
        with self.assertRaisesRegex(RuntimeError, "FLOW_AUTOMATION_BLOCKED"):
            await service._click_flow_submit_and_confirm(
                MagicMock(), 0, "", set(), [object()], [response],
            )

    def test_image_session_closes_when_only_video_jobs_are_queued(self):
        service = FlowService()
        browser = MagicMock()
        service._manual_sessions["a:image"] = {"browser": browser, "cdp_url": "http://127.0.0.1:1"}
        service._auto_close_sessions.add("a:image")
        with patch("pipeline.flow.service.store.list_rows", return_value=[
            {"id": "video", "accountId": "a", "kind": "video", "status": "queued"},
        ]), patch.object(service, "_log"):
            service._close_auto_session_if_idle("a", "image")
        browser.kill_now.assert_called_once()
        self.assertNotIn("a", service._auto_close_sessions)

    def test_siblings_wait_until_first_job_proves_sign_in(self):
        service = FlowService()
        self.assertFalse(service._session_warmup_wait("a"))
        service._account_active["a"] = 1
        self.assertTrue(service._session_warmup_wait("a"))
        service._mark_session_ready("a")
        self.assertFalse(service._session_warmup_wait("a"))
        service._clear_session_ready("a")
        self.assertTrue(service._session_warmup_wait("a"))
        service._account_active["a"] = 0
        self.assertFalse(service._session_warmup_wait("a"))

    async def test_login_wall_is_cookie_expired_not_account_failure(self):
        service = FlowService()
        page = MagicMock()
        page.url = "https://accounts.google.com/ServiceLogin"
        with self.assertRaisesRegex(RuntimeError, "FLOW_COOKIE_EXPIRED"):
            await service._confirm_logged_in_session(page, "acc")
        self.assertNotIn("acc", service._account_session_ready)
        page.url = "about:blank"
        await service._confirm_logged_in_session(page, "acc")
        self.assertNotIn("acc", service._account_session_ready)
        page.url = "https://flow.google.com/project/abc"
        await service._confirm_logged_in_session(page, "acc")
        self.assertIn("acc", service._account_session_ready)

    def test_stale_reconnect_status_does_not_hold_queue_forever(self):
        service = FlowService()
        with patch("pipeline.flow.service.store.get_row", return_value={
            "id": "a", "status": "reconnect", "error": "login failed",
        }):
            self.assertFalse(service._account_session_blocked("a"))

    async def test_prepare_skips_probe_when_session_ready(self):
        service = FlowService()
        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            store._write("accounts", [{"id": "a", "label": "Acc", "status": "online", "projectId": "p"}])
            service._mark_session_ready("a")
            with patch("pipeline.flow.browser.BrowserManager") as browser_cls:
                result = await service.prepare_account_cookies("a")
            self.assertTrue(result["ok"])
            self.assertEqual(result["code"], "SESSION_READY")
            browser_cls.assert_not_called()

    async def test_confirm_open_session_requires_cookie_persist(self):
        service = FlowService()
        page = MagicMock(url="https://flow.google.com/project/p")
        browser = MagicMock(context=MagicMock(pages=[page]))
        with patch.object(service, "_persist_browser_cookies", AsyncMock(return_value=False)), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "a"}), \
             patch("pipeline.flow.service.store.patch_row") as patch_row:
            self.assertFalse(await service._confirm_open_session(browser, "a", page))
            patch_row.assert_not_called()
            self.assertNotIn("a", service._account_session_ready)

    async def test_login_persist_fail_stays_reconnect_not_online(self):
        service = FlowService()
        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            store._write("accounts", [{"id": "a", "status": "connecting", "projectId": "p"}])
            page = MagicMock()
            page.url = "https://flow.google.com/project/p"
            page.goto = AsyncMock()
            page.is_closed = MagicMock(return_value=False)
            page.evaluate = AsyncMock(return_value=True)
            context = MagicMock(pages=[page])
            browser = MagicMock(context=context, headless=True, _cdp_browser=None)
            browser.start = AsyncMock()
            browser.page = AsyncMock(return_value=page)
            browser.stop = AsyncMock()
            credits = MagicMock(credits=10, email="u@g.com", tier="", sku="", service_tier="")
            api = MagicMock()
            api.get_credits = AsyncMock(return_value=credits)
            api._get_bearer_token = AsyncMock(return_value="")
            with patch("pipeline.flow.browser.BrowserManager", return_value=browser), \
                 patch("pipeline.flow.browser.free_local_port", return_value=1), \
                 patch.object(service, "_fill_google_login_email", AsyncMock(return_value=False)), \
                 patch.object(service, "_ensure_account_project", AsyncMock(return_value="p")), \
                 patch.object(service, "_persist_browser_cookies", AsyncMock(return_value=False)), \
                 patch("pipeline.flow.service.asyncio.sleep", AsyncMock()), \
                 patch("pipeline.flow._flow._api.FlowAPI", return_value=api), \
                 patch.object(service, "_log"):
                await service._login("a", headless=True)
            saved = store.get_row("accounts", "a")
            self.assertEqual(saved["status"], "reconnect")
            self.assertIn("FLOW_COOKIE_SAVE_FAILED", str(saved.get("error") or ""))
            self.assertNotIn("a", service._account_session_ready)
            browser.stop.assert_awaited()

    async def test_login_ok_drops_session_clones_after_stop(self):
        service = FlowService()
        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            store._write("accounts", [{"id": "a", "status": "connecting", "projectId": "p",
                                      "cookie": [{"name": "SID", "value": "x", "domain": ".google.com", "path": "/"}]}])
            for kind in ("image", "video"):
                clone = service._session_profile_dir("a", kind)
                clone.mkdir(parents=True)
                (clone / "marker").write_text("stale", encoding="utf-8")
            cookie_db = store.profile_dir("a") / "Default" / "Network" / "Cookies"
            cookie_db.parent.mkdir(parents=True)
            cookie_db.write_bytes(b"stale-db")
            page = MagicMock()
            page.url = "https://flow.google.com/project/p"
            page.goto = AsyncMock()
            page.is_closed = MagicMock(return_value=False)
            page.evaluate = AsyncMock(return_value=True)
            context = MagicMock(pages=[page])
            browser = MagicMock(context=context, headless=True, _cdp_browser=None)
            browser.start = AsyncMock()
            browser.page = AsyncMock(return_value=page)
            browser.stop = AsyncMock()
            credits = MagicMock(credits=10, email="u@g.com", tier="", sku="", service_tier="")
            api = MagicMock()
            api.get_credits = AsyncMock(return_value=credits)
            api._get_bearer_token = AsyncMock(return_value="")
            with patch("pipeline.flow.browser.BrowserManager", return_value=browser), \
                 patch("pipeline.flow.browser.free_local_port", return_value=1), \
                 patch.object(service, "_fill_google_login_email", AsyncMock(return_value=False)), \
                 patch.object(service, "_ensure_account_project", AsyncMock(return_value="p")), \
                 patch.object(service, "_persist_browser_cookies", AsyncMock(return_value=True)), \
                 patch("pipeline.flow.service.asyncio.sleep", AsyncMock()), \
                 patch("pipeline.flow._flow._api.FlowAPI", return_value=api), \
                 patch.object(service, "_log"), \
                 patch.object(service, "_requeue_auth_failed_jobs", return_value=0):
                await service._login("a", headless=True)
            saved = store.get_row("accounts", "a")
            self.assertEqual(saved["status"], "online")
            self.assertIn("a", service._account_session_ready)
            self.assertFalse(cookie_db.exists())
            for kind in ("image", "video"):
                self.assertFalse(service._session_profile_dir("a", kind).exists())

    async def test_prepare_probe_uses_cdp_port(self):
        service = FlowService()
        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            store._write("accounts", [{
                "id": "a", "label": "Acc", "status": "online", "projectId": "p",
                "cookie": [{"name": "SID", "value": "live", "domain": ".google.com", "path": "/", "expires": 9e9}],
            }])
            page = MagicMock(url="https://flow.google.com/")
            page.goto = AsyncMock()
            context = MagicMock()
            context.add_cookies = AsyncMock()
            context.cookies = AsyncMock(return_value=[
                {"name": "SID", "value": "live", "domain": ".google.com", "expires": 9e9},
            ])
            browser = MagicMock(context=context)
            browser.start = AsyncMock()
            browser.stop = AsyncMock()
            browser.page = AsyncMock(return_value=page)
            with patch("pipeline.flow.browser.BrowserManager", return_value=browser) as browser_cls, \
                 patch("pipeline.flow.browser.free_local_port", return_value=9333), \
                 patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
                result = await service.prepare_account_cookies("a")
            self.assertTrue(result["ok"])
            kwargs = browser_cls.call_args.kwargs
            self.assertEqual(kwargs.get("remote_debugging_port"), 9333)
            context.add_cookies.assert_awaited()

    async def test_job_browser_uses_cdp_and_seeds_jar(self):
        service = FlowService()
        with tempfile.TemporaryDirectory() as raw, patch.object(store, "ROOT", Path(raw)):
            jar = [{"name": "SID", "value": "live", "domain": ".google.com", "path": "/", "secure": True, "expires": 9e9}]
            store._write("accounts", [{
                "id": "a", "status": "online", "projectId": "p", "imageProjectId": "p",
                "authMethod": "chrome", "cookie": jar, "credits": 10, "plan": "Pro",
            }])
            job = {
                "id": "j", "accountId": "a", "kind": "image", "mode": "text",
                "status": "processing", "settings": {"model": "Nano Banana 2", "headless": True},
                "prompt": "hi", "sourceFiles": [],
            }
            store._write("jobs", [job])
            context = MagicMock()
            context.add_cookies = AsyncMock()
            browser = MagicMock(context=context, cdp_url="http://127.0.0.1:1", profile_dir=Path(raw) / "rt")
            browser.start = AsyncMock()
            browser.stop = AsyncMock()
            browser.page = AsyncMock(return_value=MagicMock(url="https://flow.google.com/project/p"))
            captured = {}

            def make_browser(**kwargs):
                captured.update(kwargs)
                return browser

            with patch("pipeline.flow.browser.BrowserManager", side_effect=make_browser), \
                 patch("pipeline.flow.browser.free_local_port", return_value=9444), \
                 patch.object(service, "_clone_runtime_profile", return_value=Path(raw) / "rt"), \
                 patch.object(service, "_check_cancel"), \
                 patch.object(service, "_log"), \
                 patch.object(service, "_start_session_refresh", return_value=None), \
                 patch.object(
                     service, "_ensure_account_project_page",
                     AsyncMock(side_effect=RuntimeError("stop-after-seed")),
                 ), \
                 patch("pipeline.flow._flow._api.FlowAPI"), \
                 patch("pipeline.flow._flow._client.FlowClient"):
                # Errors after browser start are handled inside _run_once; assert seed path only.
                await service._run_once("j")
            self.assertEqual(captured.get("remote_debugging_port"), 9444)
            context.add_cookies.assert_awaited()

    async def test_page_shell_warning_does_not_fail_pending_generation(self):
        service = FlowService()
        page = MagicMock()
        def evaluate(script, *args):
            if "document.body" in script:
                return "Skip to main content home Older generation: suspicious activity"
            return []
        page.evaluate = AsyncMock(side_effect=evaluate)
        result = {"id": "new", "tag": "img", "src": "https://example.test/image.png"}
        with patch.object(service, "_project_media_elements", AsyncMock(side_effect=[[], [result]])), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j"}), \
             patch("pipeline.flow.service.store.patch_row"), \
             patch("pipeline.flow.service.asyncio.sleep", AsyncMock()):
            self.assertEqual(await service._wait_for_project_media(page, set(), "image", 1, "j"), [result])

    async def test_real_visible_tile_block_is_reported(self):
        service = FlowService()
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=[{"index": 0, "text": "Chúng tôi nhận thấy có hoạt động bất thường"}])
        with patch.object(service, "_project_media_elements", AsyncMock(return_value=[])), \
             patch("pipeline.flow.service.store.get_row", return_value={"id": "j"}), \
             patch("pipeline.flow.service.store.patch_row"):
            with self.assertRaisesRegex(RuntimeError, "FLOW_AUTOMATION_BLOCKED"):
                await service._wait_for_project_media(page, set(), "image", 1, "j")


if __name__ == "__main__":
    unittest.main()
