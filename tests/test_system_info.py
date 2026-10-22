"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_system_info.py"""
import json
import os
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.core import system_info as info


class SystemInfoTest(unittest.TestCase):
    def test_partial_inventory_keeps_all_gpus_disks_and_no_secrets(self):
        partitions = [SimpleNamespace(device="disk0", mountpoint="/", fstype="apfs"),
                      SimpleNamespace(device="disk1", mountpoint="/locked", fstype="ntfs")]
        gpu = {"name": "NVIDIA test", "kind": "nvidia", "vramMb": 8192, "driver": "560.1", "accel": "cuda"}
        with patch.object(info, "_machine", return_value={"cpu": "Test CPU"}), \
             patch.object(info, "_command", return_value=None), \
             patch.object(info, "detect_device", return_value={"accel": "cuda", "gpus": [{**gpu, "index": 0}, {**gpu, "index": 1}]}), \
             patch("psutil.disk_partitions", return_value=partitions), \
             patch("psutil.disk_usage", side_effect=[SimpleNamespace(total=1000, used=600, free=400, percent=60), PermissionError()]), \
             patch("psutil.cpu_freq", side_effect=NotImplementedError()), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "not-for-the-report", "ZM_AI_TOOL_VERSION": "8.6.83"}):
            report = info.system_info()
        sections = report["sections"]
        self.assertTrue(report["collectedAt"])
        self.assertEqual(len([s for s in sections if s["id"] == "gpu"]), 2)
        disks = [s for s in sections if s["id"] == "disk"]
        self.assertEqual([s["name"] for s in disks], ["/", "/locked"])
        self.assertIsNone(next(row["value"] for row in disks[1]["rows"] if row["id"] == "free"))
        gpu_rows = next(s["rows"] for s in sections if s["id"] == "gpu")
        self.assertEqual(next(row["value"] for row in gpu_rows if row["id"] == "vram"), 8192 * 1024**2)
        self.assertNotIn("not-for-the-report", json.dumps(report))
        self.assertTrue(next(s for s in sections if s["id"] == "pythonPackages")["rows"])

    def test_route_returns_backend_information_in_app_and_web(self):
        from api.routes.system import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        expected = {"collectedAt": "now", "sections": []}
        for mode in ("0", "1"):
            with self.subTest(web_worker=mode), patch.dict(os.environ, {"ZM_AI_TOOL_WEB_WORKER": mode}), \
                 patch.object(info, "system_info", return_value=expected) as collect:
                response = client.get("/api/system/info")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), expected)
                collect.assert_called_once_with()

    def test_web_worker_serves_information_through_its_private_boundary(self):
        from api.routes.system import router
        from web import browser_route_allowed, protect_worker

        app = FastAPI()
        app.include_router(router)
        client = TestClient(protect_worker(app, Path(__file__).parent, "test-worker-secret"))
        expected = {"collectedAt": "now", "sections": [{"id": "cpu", "rows": [{"id": "model", "value": "Backend CPU"}]}]}
        with patch.dict(os.environ, {"ZM_AI_TOOL_WEB_WORKER": "1"}), patch.object(info, "system_info", return_value=expected) as collect:
            self.assertEqual(client.get("/api/system/info").status_code, 403)
            collect.assert_not_called()
            response = client.get("/api/system/info", headers={"x-zm-worker": "test-worker-secret"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), expected)
            collect.assert_called_once_with()
        self.assertTrue(browser_route_allowed("/api/system/info", "GET"))
        self.assertFalse(browser_route_allowed("/api/system/info", "POST"))


if __name__ == "__main__":
    unittest.main()
