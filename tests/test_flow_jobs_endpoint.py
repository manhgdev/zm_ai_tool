"""Test Flow jobs_get endpoint."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from api.routes.flow import jobs_get


class FlowJobsEndpointTest(unittest.TestCase):
    def test_jobs_get_found(self):
        mock_job = {
            "id": "job_123",
            "status": "done",
            "progress": 100,
            "settings": {"outputDir": "test_folder"},
        }
        with patch("api.routes.flow.store.get_row", return_value=mock_job):
            res = jobs_get("job_123")
            self.assertEqual(res["id"], "job_123")
            self.assertEqual(res["status"], "done")
            self.assertEqual(res["progress"], 100)

    def test_jobs_get_not_found_raises_404(self):
        with patch("api.routes.flow.store.get_row", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                jobs_get("non_existent")
            self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
