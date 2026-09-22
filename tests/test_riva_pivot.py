import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.mt.cloud import translate_cloud


class RivaPivotTests(unittest.TestCase):
    def translate(self, source, target, replies):
        with patch('pipeline.core.app_config.provider_credentials', return_value={'baseUrl': 'https://example.invalid', 'model': 'nvidia/riva-translate-4b-instruct'}), patch('pipeline.core.app_config.provider_api_keys', return_value=['test']), patch('pipeline.mt.cloud._openai_compatible_chat', side_effect=replies) as chat:
            result = translate_cloud(['你好'], target, 'nvidia', source_lang=source, workers=1)
        return result, chat.call_args_list

    def test_chinese_to_vietnamese_pivots_via_english(self):
        result, calls = self.translate('zh', 'vi', ['Hello', 'Xin chào'])
        self.assertEqual(result, ['Xin chào'])
        self.assertEqual([c.kwargs['system_msg'] for c in calls], ['zh-cn-en', 'en-vi'])
        self.assertEqual(calls[1].kwargs['prompt'], 'Hello')

    def test_english_target_needs_only_one_request(self):
        result, calls = self.translate('zh', 'en', ['Hello'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, ['Hello'])

    def test_empty_pivot_fails_instead_of_translating_empty_text(self):
        with self.assertRaisesRegex(RuntimeError, 'INVALID_RESPONSE'):
            self.translate('zh', 'vi', [''])
