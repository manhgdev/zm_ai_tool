"""Authoritative SSE snapshots remove phantom background jobs without user clicks."""
import asyncio
import json
import unittest
from unittest.mock import patch

from api.routes import events
from pipeline.core.realtime import RealtimeBus


class Request:
    headers = {}

    async def is_disconnected(self):
        return False


class RealtimeReconcileTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_job_is_reconciled_on_the_open_stream(self):
        bus = RealtimeBus()
        jobs = [{"id": "gone", "status": "processing"}]
        bus.register_snapshot("flow", lambda: {"jobs": list(jobs)})
        with patch.object(events, "realtime", bus), patch.object(events, "_register_snapshots"), patch.object(events, "_SNAPSHOT_INTERVAL", 0.03):
            response = await events.events(Request(), topics="flow")
            stream = response.body_iterator
            try:
                await anext(stream)  # Connected notification.
                initial = await anext(stream)
                self.assertIn('"id": "gone"', initial)
                jobs.clear()  # Deliberately drop the delete event.
                await anext(stream)  # Idle heartbeat.
                refreshed = await asyncio.wait_for(anext(stream), timeout=1)
                self.assertIn("event: snapshot", refreshed)
                self.assertIn('"jobs": []', refreshed)
                self.assertEqual(len(bus._subscribers), 1)
            finally:
                await stream.aclose()
            self.assertEqual(len(bus._subscribers), 0)

    async def test_progress_on_another_topic_does_not_delay_reconciliation(self):
        bus = RealtimeBus()
        jobs = [{"id": "gone", "status": "processing"}]
        bus.register_snapshot("flow", lambda: {"jobs": list(jobs)})
        bus.register_snapshot("download", lambda: {"jobs": [{"id": "active", "status": "running"}]})
        with patch.object(events, "realtime", bus), patch.object(events, "_register_snapshots"), patch.object(events, "_SNAPSHOT_INTERVAL", 0.03):
            response = await events.events(Request(), topics="flow,download")
            stream = response.body_iterator
            async def progress():
                while True:
                    bus.publish("download", "download.job.updated", "active", {"id": "active", "status": "running"})
                    await asyncio.sleep(0.005)
            try:
                await anext(stream)
                await anext(stream)
                await anext(stream)
                jobs.clear()
                worker = asyncio.create_task(progress())
                async def find_empty_snapshot():
                    async for chunk in stream:
                        if "event: snapshot" in chunk:
                            data = json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith("data: ")))
                            if data["topic"] == "flow" and not data["payload"]["jobs"]:
                                return
                try:
                    await asyncio.wait_for(find_empty_snapshot(), timeout=1)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            finally:
                await stream.aclose()
            self.assertEqual(len(bus._subscribers), 0)


if __name__ == "__main__":
    unittest.main()
