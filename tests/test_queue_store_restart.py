import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.queue import store


class RestartTest(unittest.TestCase):
    def test_leftover_running_and_queued_jobs_wait_for_resume(self):
        jobs = [{'id': 'a', 'status': 'running'}, {'id': 'b', 'status': 'queued'},
                {'id': 'c', 'status': 'paused'}, {'id': 'd', 'status': 'done'}]
        saved = []
        with patch.object(store, 'load_all', return_value=jobs), patch.object(store, 'save_all', saved.append):
            store.mark_interrupted()
        self.assertEqual([job['status'] for job in saved[0]], ['interrupted', 'interrupted', 'paused', 'done'])


if __name__ == '__main__':
    unittest.main()
