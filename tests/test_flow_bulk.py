import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow import store
from pipeline.flow.service import FlowService


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


class BulkDeleteServiceTest(unittest.TestCase):
    def test_delete_all_clears_db_without_calling_jobs(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            store._write('jobs', [
                {
                    'id': str(i),
                    'status': 'queued',
                    'accountId': 'a',
                    'kind': 'video',
                    'queueOrder': i,
                    'settings': {'outputDir': 'bulk'},
                    'outputs': [],
                }
                for i in range(50)
            ])
            service = FlowService()
            with patch.object(service, '_output_folder', return_value=Path(raw) / 'out'), \
                    patch.object(service, 'jobs', side_effect=AssertionError('jobs() must not run on delete-all path')):
                deleted = service.delete_all_jobs()
            self.assertEqual(deleted, 50)
            self.assertEqual(store.list_rows('jobs'), [])

    def test_delete_all_acquires_lock_while_waiter_uses_list_rows(self):
        """Workers must not call self.jobs() under the condition — delete-all stays fast."""
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            store._write('jobs', [{
                'id': '1',
                'status': 'queued',
                'accountId': 'a',
                'kind': 'video',
                'queueOrder': 0,
                'settings': {'outputDir': 'x'},
                'outputs': [],
            }])
            service = FlowService()
            stop = threading.Event()

            def waiter():
                with service._account_condition:
                    while not stop.is_set():
                        store.list_rows('jobs')
                        service._account_condition.wait(timeout=0.05)

            thread = threading.Thread(target=waiter, daemon=True)
            thread.start()
            time.sleep(0.1)
            started = time.monotonic()
            with patch.object(service, '_output_folder', return_value=Path(raw) / 'out'):
                deleted = service.delete_all_jobs()
            elapsed = time.monotonic() - started
            stop.set()
            with service._account_condition:
                service._account_condition.notify_all()
            thread.join(timeout=2)
            self.assertEqual(deleted, 1)
            self.assertLess(elapsed, 2.0)
            self.assertEqual(store.list_rows('jobs'), [])

    def test_delete_folder_returns_before_disk_cleanup(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            out_dir = Path(raw) / 'flow-out'
            out_dir.mkdir()
            media = out_dir / 'clip.mp4'
            media.write_bytes(b'video')
            store._write('jobs', [{
                'id': 'j1',
                'status': 'done',
                'accountId': 'a',
                'kind': 'video',
                'settings': {'outputDir': 'folder-a'},
                'outputs': [str(media)],
            }])
            service = FlowService()
            hold = threading.Event()
            entered = threading.Event()

            def slow_rmtree(*args, **kwargs):
                entered.set()
                hold.wait(timeout=5)
                # Skip real rmtree — only need to prove API returned before cleanup ran.
                return None

            with patch.object(service, '_output_folder', return_value=out_dir), \
                    patch('pipeline.flow.service.shutil.rmtree', side_effect=slow_rmtree):
                started = time.monotonic()
                deleted = service.delete_output_folder_jobs('folder-a', 'video')
                elapsed = time.monotonic() - started
                self.assertEqual(deleted, 1)
                self.assertLess(elapsed, 1.0)
                self.assertEqual(store.list_rows('jobs'), [])
                # Keep patches alive until the background thread hits rmtree.
                self.assertTrue(entered.wait(timeout=2))
                hold.set()
                time.sleep(0.05)
