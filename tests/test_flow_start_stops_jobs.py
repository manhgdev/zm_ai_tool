import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow import store
from pipeline.flow.service import FlowService


class FlowStartTest(unittest.TestCase):
    def test_leftover_jobs_are_stopped_not_relaunched(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            store._write('jobs', [{'id': 'q', 'status': 'queued'}, {'id': 'p', 'status': 'processing', 'progress': 40},
                                  {'id': 'f', 'status': 'failed'}])
            service = FlowService()
            with patch.object(service, '_run_sync') as run:
                service.start()
            run.assert_not_called()
            rows = {row['id']: row for row in store.list_rows('jobs')}
            for job_id in ('q', 'p'):
                self.assertEqual((rows[job_id]['status'], rows[job_id]['stage']), ('cancelled', 'stopped'))
            self.assertEqual(rows['f']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
