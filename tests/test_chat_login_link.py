import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


class LinkLoginTests(unittest.TestCase):
    def test_link_endpoint_never_requests_browser(self):
        from api.routes import chat
        with patch.object(chat.service, 'open_browser_login', return_value={'loginId': 'test', 'authorizationUrl': 'https://example.invalid'}) as login:
            result = chat.oauth_login_link('account')
        login.assert_called_once_with('account', open_browser=False)
        self.assertEqual(result['loginId'], 'test')
