"""Regression: committed duration must not skip resolution selection."""
import unittest
from unittest.mock import AsyncMock, patch
from pipeline.flow.service import FlowService, _FLOW_CONTROL_SELECTOR


class Locator:
    def __init__(self, page, items):
        self.page, self.items = page, items

    def filter(self, has_text):
        return Locator(self.page, [s for s in self.items if has_text.search(s)])

    async def count(self): return len(self.items)
    def nth(self, index): return Locator(self.page, self.items[index:index + 1])
    async def is_visible(self, **kwargs): return bool(self.items)
    async def inner_text(self): return self.items[0]
    async def get_attribute(self, name):
        if name == 'aria-checked':
            return str(self.items[0] == '16:9' or self.page.selected == self.items[0]).lower()
        return None
    async def scroll_into_view_if_needed(self): pass
    async def click(self, **kwargs):
        if self.items[0] == '360p': self.page.selected = '360p'
    async def evaluate(self, script): return False
    def locator(self, selector):
        raise AssertionError('An actionable radio must not be resolved to a child radio')


class Page:
    selected = '720p'
    def locator(self, selector):
        if selector == "[role='radiogroup'] [role='radio']":
            return Locator(self, ['360p', '720p'])
        if selector == '.settings-trigger-button, .settings-summary':
            return Locator(self, [f'Video · {self.selected} · 4s'])
        if selector == _FLOW_CONTROL_SELECTOR:
            return Locator(self, ['16:9'])
        return Locator(self, [])


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_360p_is_clicked_even_when_duration_only_has_committed_summary(self):
        page = Page()
        with patch('pipeline.flow.service._dismiss_flow_whats_new', new=AsyncMock()), patch('pipeline.flow.service.asyncio.sleep', new=AsyncMock()):
            await FlowService()._prepare_ui_format(page, '16:9', '4', '360p', 'Omni 1.1 Flash')
        self.assertEqual(page.selected, '360p')
