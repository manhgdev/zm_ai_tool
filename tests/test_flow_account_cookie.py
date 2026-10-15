import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from api.routes.flow import _labs_cookies
from pipeline.flow.service import _account_cookie_records


class FlowAccountCookieTests(unittest.TestCase):
    def test_open_session_seeds_jar_when_cookie_db_empty(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService, _seed_stored_cookies

        class _Page:
            url = ""

            async def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return True

        class _Browser:
            def __init__(self, **_kwargs):
                self.cdp_url = "http://127.0.0.1:1"
                self.page_instance = _Page()
                self.context = MagicMock(pages=[])
                self.context.add_cookies = AsyncMock()
                self.stopped = False

            async def start(self):
                return self

            async def page(self):
                return self.page_instance

            async def stop(self):
                self.stopped = True

        async def _done(*_args, **_kwargs):
            return None

        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            jar = [{"name": "SID", "value": "live", "domain": ".google.com", "path": "/", "secure": True,
                    "expires": time.time() + 86400}]
            store._write("accounts", [{
                "id": "a", "status": "online", "authMethod": "chrome", "cookie": jar, "projectId": "p",
            }])
            account = store.get_row("accounts", "a")
            self.assertTrue(_seed_stored_cookies({**account, "id": "a"}))
            browsers = []

            def make_browser(**kwargs):
                browser = _Browser(**kwargs)
                browsers.append(browser)
                return browser

            service = FlowService()
            with patch("pipeline.flow.browser.BrowserManager", side_effect=make_browser), \
                 patch("pipeline.flow.browser.free_local_port", return_value=9333), \
                 patch.object(service, "_ensure_session_profile", return_value=Path(directory) / "session"), \
                 patch.object(service, "_confirm_open_session", AsyncMock(return_value=True)), \
                 patch.object(service, "_persist_browser_cookies", AsyncMock(return_value=True)), \
                 patch("pipeline.flow.service.asyncio.to_thread", new=_done), \
                 patch("pipeline.flow.service.threading.Thread") as thread:
                service.open_account_session("a", kind="image", headless=True)
                thread.call_args.kwargs["target"]()
            self.assertEqual(len(browsers), 1)
            browsers[0].context.add_cookies.assert_awaited()
            self.assertTrue(browsers[0].stopped)

    def test_cookie_header_is_scoped_to_google(self):
        cookies = _labs_cookies("Cookie: SID=abc%3D; __Secure-3PSID=xyz==;")
        self.assertEqual({cookie["domain"] for cookie in cookies}, {".google.com"})
        self.assertEqual({cookie["path"] for cookie in cookies}, {"/"})
        self.assertEqual(len(cookies), 2)

    def test_cookie_header_rejects_control_characters(self):
        with self.assertRaisesRegex(HTTPException, "FLOW_COOKIE_INVALID"):
            _labs_cookies("SID=ab\ncd")

    def test_header_cookies_get_persistent_expiry(self):
        # No expiry => Playwright session cookie => Chrome never saves it to the profile, so cloned job profiles log out.
        records = _account_cookie_records(_labs_cookies("SID=abc; HSID=xyz") + [{"name": "A", "value": "1", "domain": ".google.com", "expires": -1}])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["expires"] > time.time() + 300 * 86400 for record in records))
        kept = _account_cookie_records([{"name": "B", "value": "1", "domain": ".google.com", "expires": 2000000000}])
        self.assertEqual(kept[0]["expires"], 2000000000)

    def test_secure_cookies_default_samesite_none(self):
        records = _account_cookie_records([
            {"name": "__Secure-1PSID", "value": "x", "domain": ".google.com", "path": "/", "secure": True},
            {"name": "plain", "value": "y", "domain": ".example.com", "path": "/"},
        ])
        by_name = {item["name"]: item for item in records}
        self.assertEqual(by_name["__Secure-1PSID"]["sameSite"], "None")
        self.assertNotIn("sameSite", by_name["plain"])

    def test_cookies_for_fresh_profile_forces_samesite_none_on_secure_google(self):
        from pipeline.flow.service import _cookies_for_fresh_profile
        out = _cookies_for_fresh_profile([
            {"name": "__Secure-1PSID", "value": "x", "domain": ".google.com", "path": "/", "secure": True, "sameSite": "Lax"},
            {"name": "SID", "value": "y", "domain": ".google.com", "path": "/", "secure": True, "sameSite": "Lax"},
            {"name": "_ga", "value": "z", "domain": ".flow.google.com", "path": "/", "secure": False, "sameSite": "Lax"},
        ])
        by_name = {item["name"]: item for item in out}
        self.assertEqual(by_name["__Secure-1PSID"]["sameSite"], "None")
        self.assertEqual(by_name["SID"]["sameSite"], "None")
        self.assertEqual(by_name["_ga"]["sameSite"], "Lax")

    def test_merge_updates_new_cookies_and_keeps_stored_ones(self):
        from pipeline.flow.service import _merge_cookie_records
        merged = _merge_cookie_records(
            [
                {"name": "SID", "value": "old", "domain": ".google.com", "path": "/"},
                {"name": "pref", "value": "keep", "domain": ".google.com", "path": "/"},
            ],
            [{"name": "SID", "value": "new", "domain": ".google.com", "path": "/"}],
        )
        by_name = {item["name"]: item["value"] for item in merged}
        self.assertEqual(by_name, {"SID": "new", "pref": "keep"})

    def test_reset_profile_is_seeded_from_stored_cookies(self):
        from pipeline.flow.service import _seed_stored_cookies
        with tempfile.TemporaryDirectory() as raw, \
             patch("pipeline.flow.service.store.profile_dir", return_value=Path(raw)):
            chrome = {"id": "a", "authMethod": "chrome"}
            self.assertFalse(_seed_stored_cookies(chrome))
            chrome_jar = {
                "id": "a", "authMethod": "chrome",
                "cookie": [{"name": "SID", "value": "x", "domain": ".google.com", "path": "/"}],
            }
            # Empty profile → seed JSON. Non-empty Cookies DB → use disk (no CookieMismatch).
            self.assertTrue(_seed_stored_cookies(chrome_jar))
            network = Path(raw) / "Default" / "Network"
            network.mkdir(parents=True)
            (network / "Cookies").write_bytes(b"")
            self.assertTrue(_seed_stored_cookies(chrome_jar))
            (network / "Cookies").write_bytes(b"x" * 64)
            self.assertFalse(_seed_stored_cookies(chrome_jar))
            self.assertTrue(_seed_stored_cookies({"id": "a", "authMethod": "cookie"}))

    def test_reset_with_jar_rmtrees_profile_and_connects_f12_cookies(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService, _seed_stored_cookies
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            jar = [
                {"name": "SID", "value": "tok", "domain": ".google.com", "path": "/", "secure": True, "sameSite": "Lax"},
                {"name": "__Secure-1PSID", "value": "x", "domain": ".google.com", "path": "/", "secure": True, "sameSite": "Lax"},
            ]
            store._write("accounts", [{"id": "a", "status": "online", "projectId": "p", "cookie": jar}])
            service = FlowService()
            main = Path(store.profile_dir("a"))
            sessions = [service._session_profile_dir("a", kind) for kind in ("image", "video")]
            runtime = store.root() / "runtime-profiles" / "a"
            dead_job = runtime / "old-job"
            old_files = ["Default/Login Data", "Default/Cookies", "Local State", "Default/IndexedDB/x", "Cache/x"]
            for profile in (main, *sessions, dead_job):
                for name in old_files:
                    (profile / name).parent.mkdir(parents=True, exist_ok=True)
                    (profile / name).write_text("x")
            with patch.object(service, "connect") as connect, \
                 patch.object(service, "_sync_runtime_cookies") as sync:
                service.reset_account_session("a")
            # Brand-new empty profile; identity files gone.
            self.assertTrue(main.is_dir())
            self.assertFalse((main / "Default" / "Login Data").exists())
            self.assertFalse((main / "Default" / "Cookies").exists())
            self.assertFalse((main / "Local State").exists())
            self.assertFalse(any(profile.exists() for profile in sessions))
            self.assertFalse(runtime.exists())
            synced = [call.args[1] for call in sync.call_args_list]
            self.assertEqual(synced[:2], sessions)
            self.assertIn(dead_job, synced)
            connect.assert_called_once()
            injected = connect.call_args.kwargs.get("cookies") or []
            by_name = {item["name"]: item for item in injected}
            self.assertEqual(by_name["SID"]["sameSite"], "None")
            self.assertEqual(by_name["__Secure-1PSID"]["sameSite"], "None")
            saved = store.get_row("accounts", "a")
            self.assertEqual(saved["projectId"], None)
            # Stored jar keeps Playwright Lax; only inject path forces None.
            self.assertEqual(saved["cookie"][0]["sameSite"], "Lax")
            self.assertTrue(_seed_stored_cookies({**saved, "id": "a", "authMethod": "chrome"}))

    def test_reset_without_jar_keeps_login_files(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "online", "projectId": "p"}])
            service = FlowService()
            main = Path(store.profile_dir("a"))
            for name in ("Default/Login Data", "Default/Preferences", "Local State", "Default/IndexedDB/x"):
                (main / name).parent.mkdir(parents=True, exist_ok=True)
                (main / name).write_text("x")
            with patch.object(service, "connect") as connect, patch.object(service, "_sync_runtime_cookies"):
                service.reset_account_session("a", fresh_cookie_profile=True)
            self.assertTrue((main / "Default" / "Login Data").exists())
            self.assertTrue((main / "Local State").exists())
            self.assertFalse((main / "Default" / "IndexedDB").exists())
            connect.assert_called_once_with("a", headless=True, open_ui_on_failure=False)
            self.assertIsNone(connect.call_args.kwargs.get("cookies"))

    def test_reset_keeps_processing_job_runtime_clone(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "online", "projectId": "p", "cookie": [{"name": "SID"}]}])
            store._write("jobs", [{"id": "live", "accountId": "a", "status": "processing", "kind": "image"}])
            service = FlowService()
            live = store.root() / "runtime-profiles" / "a" / "live"
            stale = store.root() / "runtime-profiles" / "a" / "stale"
            for path in (live, stale):
                path.mkdir(parents=True)
                (path / "marker").write_text("x")
            with patch.object(service, "connect"), patch.object(service, "_sync_runtime_cookies"):
                service.reset_account_session("a", keep_running_jobs=True)
            self.assertTrue(live.exists())
            self.assertFalse(stale.exists())
            self.assertEqual(store.get_row("accounts", "a")["projectId"], "p")

    def test_unusual_activity_reset_keeps_running_jobs_and_their_project(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "online", "projectId": "p"}])
            store._write("jobs", [{"id": "sibling", "accountId": "a", "status": "processing"}])
            service = FlowService()
            with patch.object(service, "connect") as connect:
                with self.assertRaises(RuntimeError):
                    service.reset_account_session("a")
                service.reset_account_session("a", keep_running_jobs=True)
            connect.assert_called_once()
            self.assertEqual(store.get_row("accounts", "a")["projectId"], "p")

    def test_import_cookies_merges_with_stored(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{
                "id": "a", "label": "Acc", "status": "online",
                "cookie": [
                    {"name": "SID", "value": "old", "domain": ".google.com", "path": "/"},
                    {"name": "pref", "value": "keep", "domain": ".google.com", "path": "/"},
                ],
            }])
            with patch("pipeline.flow.service.threading.Thread"):
                FlowService().connect("a", cookies=[{"name": "SID", "value": "new", "domain": ".google.com", "path": "/"}])
            saved = {item["name"]: item["value"] for item in store.get_row("accounts", "a")["cookie"]}
            self.assertEqual(saved, {"SID": "new", "pref": "keep"})


class FlowAccountCookiePrepareTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_login_wall_reports_expired_and_keeps_cookies(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{
                "id": "a", "label": "Acc", "status": "online",
                "cookie": [
                    {"name": "SID", "value": "dead", "domain": ".google.com", "expires": time.time() + 86400},
                    {"name": "pref", "value": "keep", "domain": ".google.com", "expires": time.time() + 86400},
                ],
            }])
            page = MagicMock(url="https://accounts.google.com/ServiceLogin")
            page.goto = AsyncMock()
            context = MagicMock()
            context.add_cookies = AsyncMock()
            context.cookies = AsyncMock(return_value=[
                {"name": "SID", "value": "dead", "domain": ".google.com", "expires": time.time() + 86400},
                {"name": "pref", "value": "keep", "domain": ".google.com", "expires": time.time() + 86400},
            ])
            browser = MagicMock(context=context)
            browser.start = AsyncMock()
            browser.stop = AsyncMock()
            browser.page = AsyncMock(return_value=page)
            service = FlowService()
            with patch("pipeline.flow.browser.BrowserManager", return_value=browser), \
                 patch("pipeline.flow.service.asyncio.sleep", AsyncMock()), \
                 patch.object(service, "connect") as connect:
                result = await service.prepare_account_cookies("a")
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "FLOW_COOKIE_EXPIRED")
            # Same as pressing Connect, after the probe Chrome closed.
            connect.assert_called_once_with("a", headless=False)
            browser.stop.assert_awaited()
            self.assertTrue(result["loginOpened"])
            saved = store.get_row("accounts", "a")
            self.assertEqual({item["name"] for item in saved.get("cookie") or []}, {"SID", "pref"})

    async def test_open_session_sign_in_binds_a_project_like_connect(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{"id": "a", "status": "reconnect", "projectId": None}])
            page = MagicMock(url="https://labs.google/fx/tools/flow")
            context = MagicMock(pages=[page])
            context.cookies = AsyncMock(return_value=[
                {"name": "SID", "value": "live", "domain": ".google.com", "expires": time.time() + 86400},
            ])
            service = FlowService()
            with patch.object(service, "_create_flow_project_ui", AsyncMock(return_value="new-project")), \
                 patch.object(page, "locator", side_effect=RuntimeError("no gallery link")):
                self.assertTrue(await service._confirm_open_session(MagicMock(context=context), "a", page))
            saved = store.get_row("accounts", "a")
            self.assertEqual((saved["status"], saved["projectId"], saved["imageProjectId"]), ("online", "new-project", "new-project"))

    async def test_persist_browser_cookies_refreshes_accounts_json_jar(self):
        from pipeline.flow import store
        from pipeline.flow.service import FlowService
        with tempfile.TemporaryDirectory() as directory, patch.object(store, "ROOT", Path(directory)):
            store._write("accounts", [{
                "id": "a", "status": "online",
                "cookie": [
                    {"name": "SID", "value": "old", "domain": ".google.com", "path": "/"},
                    {"name": "pref", "value": "keep", "domain": ".google.com", "path": "/"},
                ],
            }])
            context = MagicMock()
            context.cookies = AsyncMock(return_value=[
                {"name": "SID", "value": "rotated", "domain": ".google.com", "path": "/", "expires": time.time() + 86400},
                {"name": "HSID", "value": "new", "domain": ".google.com", "path": "/", "expires": time.time() + 86400},
            ])
            self.assertTrue(await FlowService()._persist_browser_cookies("a", MagicMock(context=context)))
            by_name = {item["name"]: item["value"] for item in store.get_row("accounts", "a")["cookie"]}
            self.assertEqual(by_name, {"SID": "rotated", "pref": "keep", "HSID": "new"})


if __name__ == "__main__":
    unittest.main()
