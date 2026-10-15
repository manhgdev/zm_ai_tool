"""Public desktop downloads must use updater selection for every target OS."""
import unittest
import json
import urllib.error
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from api.routes import system


def release_with(*suffixes):
    names = [f"ZM_AI_TOOL_v8.6.24-{suffix}" for suffix in suffixes]
    return {
        "tag_name": "v8.6.24",
        "assets": [{
            "name": name,
            "browser_download_url": f"https://github.com/{system._UPDATE_REPOSITORY}/releases/download/v8.6.24/{name}",
        } for name in names],
    }


class LandingDownloadTests(unittest.TestCase):
    def setUp(self):
        cache = patch.dict(system._DOWNLOAD_RELEASE_CACHE, {"release": None, "expires": 0.0})
        cache.start()
        self.addCleanup(cache.stop)

    def test_repeated_page_views_share_one_release_fetch(self):
        with patch.object(system, "_latest_release", return_value=release_with("windows-x64-Setup.exe")) as fetch:
            first = system.api_desktop_downloads()
            self.assertEqual(system.api_desktop_downloads(), first)
            fetch.assert_called_once()

    def test_rate_limit_uses_public_feed_and_actual_assets(self):
        class Response:
            def __init__(self, text): self.text = text
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return self.text.encode()

        base = f"https://github.com/{system._UPDATE_REPOSITORY}"
        feed = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="{base}/releases/tag/v8.6.21"/></entry><entry><link href="{base}/releases/tag/v8.6.24"/></entry><entry><link href="https://example.com/releases/tag/v9.0.0"/></entry></feed>'
        assets = ''.join(f'<a href="/{system._UPDATE_REPOSITORY}/releases/download/v8.6.24/{asset["name"]}">Download</a>' for asset in release_with("windows-x64-Setup.exe", "macos-arm64.pkg")["assets"])
        error = urllib.error.HTTPError("https://api.github.com", 403, "rate limit exceeded", {}, None)
        with patch.object(system, "_latest_release", side_effect=error), patch("urllib.request.urlopen", side_effect=[Response(feed), Response(assets)]) as fetch:
            result = system.api_desktop_downloads()
        self.assertEqual(result["version"], "8.6.24")
        self.assertEqual(set(result["downloads"]), {"windows", "macos-arm64"})
        self.assertEqual(fetch.call_args_list[0].args[0].full_url, f"{base}/releases.atom")
        self.assertEqual(fetch.call_args_list[1].args[0].full_url, f"{base}/releases/expanded_assets/v8.6.24")

    def test_known_downloads_survive_temporary_source_outage(self):
        with patch.object(system, "_latest_release", return_value=release_with("macos-arm64.pkg")):
            first = system.api_desktop_downloads()
        system._DOWNLOAD_RELEASE_CACHE["expires"] = 0.0
        with patch.object(system, "_latest_release", side_effect=OSError("offline")), patch.object(system, "_public_download_release", side_effect=OSError("offline")):
            self.assertEqual(system.api_desktop_downloads(), first)

    def test_prerelease_fallback_is_only_for_downloads_when_no_stable_release(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps(self.payload).encode()

        preview = {**release_with("windows-x64-Setup.exe"), "prerelease": True}
        with patch("urllib.request.urlopen", return_value=Response([preview])):
            self.assertEqual(system._latest_release(), {})
            result = system.api_desktop_downloads()
            self.assertTrue(result["prerelease"])
            self.assertIn("windows", result["downloads"])
        stable = {"tag_name": "v8.6.22", "prerelease": False}
        draft = {"tag_name": "v9.0.0", "draft": True}
        with patch("urllib.request.urlopen", return_value=Response([preview, stable, draft])):
            self.assertEqual(system._latest_release(allow_prerelease_fallback=True)["tag_name"], "v8.6.22")

    def test_empty_primary_release_list_falls_back_to_vip(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps(self.payload).encode()

        vip_name = "ZM_AI_TOOL_v8.6.23-windows-x64-Setup.exe"
        vip = {
            "tag_name": "v8.6.23",
            "prerelease": False,
            "assets": [{
                "name": vip_name,
                "browser_download_url": (
                    f"https://github.com/{system._UPDATE_FALLBACK_REPOSITORY}"
                    f"/releases/download/v8.6.23/{vip_name}"
                ),
            }],
        }
        responses = [Response([]), Response([vip])]
        with patch("urllib.request.urlopen", side_effect=responses):
            release = system._latest_release(allow_prerelease_fallback=True)
        self.assertEqual(release["tag_name"], "v8.6.23")
        self.assertEqual(release["_repository"], system._UPDATE_FALLBACK_REPOSITORY)

        with patch.object(system, "_latest_release", return_value=release):
            result = system.api_desktop_downloads()
        self.assertEqual(result["version"], "8.6.23")
        self.assertIn("windows", result["downloads"])
        self.assertIn(system._UPDATE_FALLBACK_REPOSITORY, result["downloads"]["windows"]["url"])

    def test_empty_primary_public_feed_falls_back_to_vip(self):
        class Response:
            def __init__(self, text): self.text = text
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return self.text.encode()

        primary = f"https://github.com/{system._UPDATE_REPOSITORY}"
        vip = f"https://github.com/{system._UPDATE_FALLBACK_REPOSITORY}"
        empty_feed = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        vip_feed = (
            f'<feed xmlns="http://www.w3.org/2005/Atom">'
            f'<entry><link href="{vip}/releases/tag/v8.6.23"/></entry></feed>'
        )
        vip_assets = (
            f'<a href="/{system._UPDATE_FALLBACK_REPOSITORY}/releases/download/v8.6.23/'
            f'ZM_AI_TOOL_v8.6.23-windows-x64-Setup.exe">Download</a>'
        )
        error = urllib.error.HTTPError("https://api.github.com", 403, "rate limit exceeded", {}, None)
        with patch.object(system, "_latest_release", side_effect=error), patch(
            "urllib.request.urlopen",
            side_effect=[Response(empty_feed), Response(vip_feed), Response(vip_assets)],
        ):
            result = system.api_desktop_downloads()
        self.assertEqual(result["version"], "8.6.23")
        self.assertIn("windows", result["downloads"])
        self.assertIn(system._UPDATE_FALLBACK_REPOSITORY, result["downloads"]["windows"]["url"])

    def test_public_links_are_host_independent_and_prefer_installers(self):
        release = release_with(
            "windows-x64-Portable.zip", "windows-x64-Setup.exe",
            "macos-arm64.zip", "macos-arm64.pkg", "macos-x86_64.pkg",
        )
        with patch.object(system, "_latest_release", return_value=release), patch.object(system.sys, "platform", "linux"):
            result = system.api_desktop_downloads()
        self.assertEqual(result["version"], "8.6.24")
        self.assertEqual(set(result["downloads"]), {"windows", "macos-arm64", "macos-x64"})
        for key, suffix in (("windows", "Setup.exe"), ("macos-arm64", "arm64.pkg"), ("macos-x64", "x86_64.pkg")):
            self.assertTrue(result["downloads"][key]["name"].endswith(suffix))
            self.assertEqual(result["downloads"][key]["url"], next(a["browser_download_url"] for a in release["assets"] if a["name"] == result["downloads"][key]["name"]))

    def test_missing_platform_is_not_replaced_with_wrong_architecture(self):
        with patch.object(system, "_latest_release", return_value=release_with("macos-arm64.pkg")):
            self.assertEqual(set(system.api_desktop_downloads()["downloads"]), {"macos-arm64"})

    def test_legacy_intel_asset_name_is_supported(self):
        with patch.object(system, "_latest_release", return_value=release_with("macos-x64.pkg")):
            self.assertIn("macos-x64", system.api_desktop_downloads()["downloads"])

    def test_untrusted_download_url_is_not_exposed(self):
        release = release_with("windows-x64-Setup.exe")
        release["assets"][0]["browser_download_url"] = "https://example.com/installer.exe"
        with patch.object(system, "_latest_release", return_value=release):
            self.assertEqual(system.api_desktop_downloads()["downloads"], {})

    def test_upstream_failure_has_stable_error(self):
        with patch.object(system, "_latest_release", side_effect=OSError("offline")), patch.object(system, "_public_download_release", side_effect=OSError("offline")):
            with self.assertRaises(HTTPException) as raised:
                system.api_desktop_downloads()
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "DOWNLOAD_RELEASE_UNAVAILABLE")

    def test_downloads_are_available_without_license_or_desktop_mode(self):
        from api.app import create_app
        with patch.object(system, "_latest_release", return_value=release_with("windows-x64-Setup.exe")), patch("pipeline.core.license.license_cached_valid", return_value=False) as license_check:
            response = TestClient(create_app()).get("/api/system/downloads")
        self.assertEqual(response.status_code, 200)
        self.assertIn("windows", response.json()["downloads"])
        license_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
