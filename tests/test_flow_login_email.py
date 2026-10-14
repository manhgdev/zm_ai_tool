import unittest
from unittest.mock import AsyncMock, Mock

from pipeline.flow.service import FlowService


class GoogleLoginEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_google_redirect_fills_email(self):
        service = FlowService()
        field = Mock(is_visible=AsyncMock(return_value=True),
                     input_value=AsyncMock(return_value=''), fill=AsyncMock())
        button = Mock(click=AsyncMock())
        page = Mock(url='https://flow.google.com/about')
        page.locator.side_effect = lambda selector: Mock(first=field if 'input[' in selector else button)
        self.assertFalse(await service._fill_google_login_email(page, 'saved@gmail.com'))
        page.url = 'https://accounts.google.com/v3/signin/identifier'
        self.assertTrue(await service._fill_google_login_email(page, 'saved@gmail.com'))
        field.fill.assert_awaited_once_with('saved@gmail.com')
        button.click.assert_awaited_once()

    async def test_does_not_submit_a_different_account(self):
        field = Mock(is_visible=AsyncMock(return_value=True),
                     input_value=AsyncMock(return_value='other@gmail.com'), fill=AsyncMock())
        page = Mock(url='https://accounts.google.com/v3/signin/identifier')
        page.locator.return_value.first = field
        self.assertFalse(await FlowService()._fill_google_login_email(page, 'saved@gmail.com'))
        field.fill.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
