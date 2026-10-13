import sys
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from api.routes.flow import _labs_cookies
from pipeline.flow.service import _account_cookie_records


class FlowAccountCookieTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
