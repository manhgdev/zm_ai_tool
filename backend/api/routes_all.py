"""HTTP routes — domain modules under api.routes.*."""
from __future__ import annotations

from fastapi import APIRouter

from api.routes import queue
from api.routes import review
from api.routes import audio
from api.routes import download
from api.routes import jobs
from api.routes import license
from api.routes import overlays
from api.routes import projects
from api.routes import rendered
from api.routes import segments
from api.routes import system
from api.routes import cleaner
from api.routes import srt_image
from api.routes import srt_export
from api.routes import tts_preview
from api.routes import tts_studio
from api.routes import tts_voices
from api.routes import drawing
from api.routes import flow
from api.routes import chat
from api.routes import automation
from api.routes import events

router = APIRouter()
router.include_router(queue.router)
router.include_router(review.router)
router.include_router(license.router)
router.include_router(audio.router)
router.include_router(download.router)
router.include_router(jobs.router)
router.include_router(overlays.router)
router.include_router(projects.router)
router.include_router(rendered.router)
router.include_router(segments.router)
router.include_router(system.router)
router.include_router(cleaner.router)
router.include_router(srt_image.router)
router.include_router(srt_export.router)
router.include_router(tts_preview.router)
router.include_router(tts_studio.router)
router.include_router(tts_voices.router)
router.include_router(drawing.router)
router.include_router(flow.router)
router.include_router(chat.router)
router.include_router(automation.router)
router.include_router(events.router)

# Legacy schema re-exports
from api.deps import ExportPayload, SegmentIn, Settings, TextOverlayIn  # noqa: E402,F401
