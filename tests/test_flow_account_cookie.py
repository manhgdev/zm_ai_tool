import sys
import unittest
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from api.routes.flow import _labs_cookies


class FlowAccountCookieTests(unittest.TestCase):
    def test_cookie_header_is_scoped_to_google(self):
        cookies = _labs_cookies("Cookie: SID=abc%3D; __Secure-3PSID=xyz==;")
        self.assertEqual({cookie["domain"] for cookie in cookies}, {".google.com"})
        self.assertEqual({cookie["path"] for cookie in cookies}, {"/"})
        self.assertEqual(len(cookies), 2)

    def test_cookie_header_rejects_control_characters(self):
        with self.assertRaisesRegex(HTTPException, "FLOW_COOKIE_INVALID"):
            _labs_cookies("SID=ab\ncd")


if __name__ == "__main__":
    unittest.main()
