"""Shared server-sent event stream for long-running jobs."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from pipeline.core.realtime import realtime

router = APIRouter()


def _register_snapshots() -> None:
    if getattr(_register_snapshots, "done", False):
        return
    from pipeline.drawing import jobs as drawing_jobs
    from pipeline.flow import service as flow_service
    from pipeline.automation import service as automation_service
    from pipeline.srt_export import list_jobs as list_srt_export_jobs
    from pipeline.cleaner.cleaner_jobs import list_jobs as list_cleaner_jobs
    from pipeline.srt_image import list_jobs as list_srt_image_jobs
    from pipeline.download.ytdlp_jobs import list_jobs as list_download_jobs
    from pipeline.flow import series as flow_series

    realtime.register_snapshot("flow", lambda: {
        "jobs": flow_service.service.jobs(),
        "accounts": flow_service.service.accounts(),
        "logs": flow_service.service.logs(),
    })
    realtime.register_snapshot("drawing", lambda: {"jobs": drawing_jobs.list_jobs()})
    realtime.register_snapshot("automation", lambda: {"jobs": automation_service.service.list_jobs()})
    realtime.register_snapshot("srt-export", lambda: {"jobs": list_srt_export_jobs()})
    realtime.register_snapshot("cleaner", lambda: {"jobs": list_cleaner_jobs()})
    realtime.register_snapshot("srt-image", lambda: {"jobs": list_srt_image_jobs()})
    realtime.register_snapshot("download", lambda: {"jobs": list_download_jobs()})
    realtime.register_snapshot("series", lambda: {"items": flow_series.list_series()})
    _register_snapshots.done = True


@router.get("/api/events")
async def events(request: Request, topics: str = "", after: int = 0) -> StreamingResponse:
    _register_snapshots()
    selected = frozenset(item.strip() for item in topics.split(",") if item.strip())
    try:
        after = max(after, int(request.headers.get("last-event-id") or "0"))
    except ValueError:
        after = 0

    async def stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        subscriber = realtime.subscribe(loop, selected, after)
        try:
            yield "event: connected\ndata: {}\n\n"
            for event in realtime.snapshot(selected):
                yield realtime.sse(event)
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), timeout=20)
                    yield realtime.sse(event)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            realtime.unsubscribe(subscriber)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
