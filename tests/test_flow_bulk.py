import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow import store


class BulkStoreTest(unittest.TestCase):
    def test_cancel_and_delete_write_once_and_preserve_unselected_rows(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            rows = [{'id': str(i), 'status': 'queued'} for i in range(1000)]
            rows.append({'id': 'done', 'status': 'done'})
            store._write('jobs', rows)
            ids = {str(i) for i in range(1000)}
            with patch.object(store, '_write', wraps=store._write) as write:
                self.assertEqual(store.cancel_active_jobs(ids, 1), 1000)
                self.assertEqual(write.call_count, 1)
                self.assertEqual(store.cancel_active_jobs(ids, 2), 0)
                self.assertEqual(write.call_count, 1)
            with patch.object(store, '_write', wraps=store._write) as write:
                self.assertEqual(len(store.delete_rows('jobs', ids)), 1000)
                self.assertEqual(write.call_count, 1)
            self.assertEqual(store.list_rows('jobs'), [{'id': 'done', 'status': 'done'}])
