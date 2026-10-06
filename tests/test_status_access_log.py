import ast
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class StatusAccessLogTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'backend/api/app.py'
        tree = ast.parse(source.read_text())
        node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'StatusAccessFilter')
        namespace = {'logging': logging, 'os': os}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        self.filter = namespace['StatusAccessFilter']()

    def record(self, method='GET', path='/api/tts/status', status=200):
        return logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                                 ('127.0.0.1:123', method, path, '1.1', status), None)

    def test_only_successful_routine_reads_are_suppressed(self):
        with patch.dict(os.environ, {'ZM_AI_TOOL_ACCESS_LOG': ''}):
            self.assertFalse(self.filter.filter(self.record()))
            self.assertFalse(self.filter.filter(self.record(path='/api/events?topics=tts')))
            for record in [self.record(status=500), self.record(status=403), self.record(method='POST'),
                           self.record(path='/api/config'), self.record(path='/api/tts/studio/history')]:
                self.assertTrue(self.filter.filter(record))

    def test_debug_override_and_unrecognized_records_are_preserved(self):
        with patch.dict(os.environ, {'ZM_AI_TOOL_ACCESS_LOG': '1'}):
            self.assertTrue(self.filter.filter(self.record()))
        self.assertTrue(self.filter.filter(logging.LogRecord('uvicorn.access', logging.ERROR, '', 0, 'error', (), None)))


if __name__ == '__main__':
    unittest.main()
