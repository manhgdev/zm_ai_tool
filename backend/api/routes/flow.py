"""Google Flow account and generation API."""
from __future__ import annotations

import shutil
import subprocess
import sys
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pipeline.flow import service, series
from pipeline.flow import store
from pipeline.flow.series_runner import runner as series_runner

router = APIRouter(prefix="/api/flow", tags=["flow"])


class AccountIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    email: str = Field(default="", max_length=160)
    plan: str = Field(default="Free", pattern="^(Pro|Ultra|Free)$")
    projectId: str = Field(default="", max_length=160)
    isDefault: bool = False


class GenerateIn(BaseModel):
    prompts: list[str] = Field(min_length=1)
    kind: str = Field(pattern="^(image|video)$")
    mode: str = Field(default="text", pattern="^(text|edit|reference|frame)$")
    accountId: str
    settings: dict[str, Any] = {}
    sourceFiles: list[str] = []
    seriesContext: dict[str, Any] | None = None


class RetryIn(BaseModel):
    accountId: str | None = None
    settings: dict[str, Any] = {}


class OutputFolderIn(BaseModel):
    outputDir: str = Field(min_length=1, max_length=1000)
    kind: str = Field(default="", pattern="^(|image|video)$")


class SeriesIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    bible: str = Field(default="", max_length=12000)
    anchorAssets: list[str] = Field(default_factory=list, max_length=3)


class EpisodeIn(BaseModel):
    title: str = Field(default="", max_length=160)
    state: str = Field(default="", max_length=5000)


class SceneIn(BaseModel):
    title: str = Field(default="", max_length=160)
    prompt: str = Field(default="", max_length=12000)
    timecode: str = Field(default="", max_length=120)
    continuityEnabled: bool | None = None
    referenceAssetIds: list[str] | None = None
    promptOverride: str | None = Field(default=None, max_length=12000)


class ScriptIn(BaseModel):
    text: str = Field(min_length=1, max_length=300000)
    bible: str = Field(default="", max_length=12000)


class SeriesDraftIn(BaseModel):
    idea: str = Field(min_length=1, max_length=12000)
    provider: str = Field(default="openrouter", pattern="^(openai|gemini|openrouter|grok)$")
    episodes: int = Field(default=1, ge=1, le=10)
    scenesPerEpisode: int = Field(default=3, ge=1, le=10)


class SeriesGenerationIn(BaseModel):
    artifact: str = Field(pattern="^(keyframe|video)$")
    accountId: str
    settings: dict[str, Any] = {}
    promptOverride: str = Field(default="", max_length=12000)


class SeriesAnchorGenerationIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    accountId: str
    settings: dict[str, Any] = {}


class SeriesAssetIn(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    locked: bool | None = None


class SeriesRunIn(BaseModel):
    accountId: str
    settings: dict[str, Any] = {}
    imageModel: str = Field(default="Nano Banana 2", max_length=80)
    episodeId: str = Field(default="", max_length=120)   # empty = whole series
    autoApprove: bool = True
    mode: str = Field(default="full", pattern="^(full|keyframes_only|videos_only)$")


@router.get("/accounts")
def accounts_list():
    return {"accounts": service.accounts()}


@router.post("/accounts")
def accounts_create(body: AccountIn):
    return service.save_account(body.model_dump())


@router.put("/accounts/{account_id}")
def accounts_update(account_id: str, body: AccountIn):
    if not store.get_row("accounts", account_id):
        raise HTTPException(404, "Flow account not found")
    return service.save_account(body.model_dump(), account_id)


@router.delete("/accounts/{account_id}")
def accounts_delete(account_id: str):
    try:
        removed = service.delete_account(account_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not removed:
        raise HTTPException(404, "Flow account not found")
    return {"ok": True}


@router.post("/accounts/{account_id}/connect")
def accounts_connect(account_id: str):
    try:
        return service.connect(account_id)
    except KeyError as exc:
        raise HTTPException(404, "Flow account not found") from exc


@router.post("/accounts/sync")
def accounts_sync_all():
    """Fire-and-forget credit sync for all connected accounts."""
    return {"accounts": service.sync_all_credits()}


@router.post("/accounts/{account_id}/sync")
async def accounts_sync_one(account_id: str):
    """Sync credits for a single account using its existing browser profile."""
    try:
        return await service.sync_credits_for_account(account_id)
    except KeyError as exc:
        raise HTTPException(404, "Flow account not found") from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        # FlowAuthError (401) and other flow-py exceptions: account status already
        # patched to 'reconnect' inside service. Return 409 so the frontend knows
        # to prompt re-connection rather than showing a generic 500.
        exc_name = type(exc).__name__
        if "Auth" in exc_name or "401" in str(exc):
            raise HTTPException(409, f"FLOW_SESSION_EXPIRED: {exc}") from exc
        raise HTTPException(500, str(exc)) from exc



@router.post("/assets")
async def asset_upload(files: list[UploadFile] = File(...)):
    if not files or len(files) > 3:
        raise HTTPException(400, "Upload between one and three reference images")
    folder = store.root() / "uploads" / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in files:
        suffix = Path(item.filename or "image.png").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(400, f"Unsupported image type: {item.filename}")
        output = folder / f"{len(saved) + 1:02d}{suffix}"
        with output.open("wb") as handle:
            shutil.copyfileobj(item.file, handle)
        saved.append({"name": item.filename, "path": str(output)})
    return {"files": saved}


@router.get("/jobs")
def jobs_list():
    # Include the account snapshot so the active-job poll also refreshes
    # credits without adding another recurring request from the frontend.
    return {"jobs": service.jobs(), "accounts": service.accounts()}


@router.get("/logs")
def logs_list():
    return {"logs": service.logs()}


@router.delete("/logs")
def logs_clear():
    service.clear_logs()
    return {"ok": True}


@router.post("/jobs")
def jobs_create(body: GenerateIn):
    try:
        return {"jobs": service.enqueue(body.model_dump())}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/series")
def series_list():
    return {"items": series.list_series()}


@router.post("/series")
def series_create(body: SeriesIn):
    try:
        item = series.create_series(body.title, body.bible, body.description)
        if body.anchorAssets:
            item = series.update_series(item["id"], {"anchorAssets": body.anchorAssets}) or item
        return item
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/series/import")
def series_import(body: ScriptIn):
    result = series.create_from_script(body.text, body.bible)
    if not result.get("ok"):
        raise HTTPException(422, detail=result.get("errors") or [])
    return result


@router.post("/series/draft")
def series_draft(body: SeriesDraftIn):
    """Ask an already configured cloud provider for reviewable Series TXT."""
    from pipeline.flow.series_ai import draft_script

    try:
        return {"text": draft_script(
            provider=body.provider, idea=body.idea,
            episodes=body.episodes, scenes_per_episode=body.scenesPerEpisode,
        )}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/series/{series_id}")
def series_get(series_id: str):
    item = series.get_series(series_id)
    if not item:
        raise HTTPException(404, "Series not found")
    return item


@router.put("/series/{series_id}")
def series_update(series_id: str, body: SeriesIn):
    try:
        item = series.update_series(series_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not item:
        raise HTTPException(404, "Series not found")
    return item


@router.delete("/series/{series_id}")
def series_delete(series_id: str):
    if not series.delete_series(series_id):
        raise HTTPException(404, "Series not found")
    return {"ok": True}


@router.post("/series/{series_id}/assets")
async def series_asset_upload(series_id: str, file: UploadFile = File(...), label: str = "", locked: bool = True):
    try:
        return series.add_asset(series_id, file.filename or "anchor.png", await file.read(), label=label, locked=locked)
    except KeyError as exc:
        raise HTTPException(404, "Series not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/series/{series_id}/assets/{asset_id}")
def series_asset_file(series_id: str, asset_id: str):
    path = series.asset_path(series_id, asset_id)
    if not path:
        raise HTTPException(404, "Series asset not found")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "image/png")


@router.delete("/series/{series_id}/assets/{asset_id}")
def series_asset_delete(series_id: str, asset_id: str):
    if not series.delete_asset(series_id, asset_id):
        raise HTTPException(404, "Series asset not found")
    return {"ok": True}


@router.put("/series/{series_id}/assets/{asset_id}")
def series_asset_update(series_id: str, asset_id: str, body: SeriesAssetIn):
    item = series.update_asset(series_id, asset_id, label=body.label, locked=body.locked)
    if not item:
        raise HTTPException(404, "Series asset not found")
    return item


@router.post("/series/{series_id}/episodes")
def series_episode_create(series_id: str, body: EpisodeIn):
    item = series.create_episode(series_id, body.title, body.state)
    if not item:
        raise HTTPException(404, "Series not found")
    return item


@router.put("/series/{series_id}/episodes/{episode_id}")
def series_episode_update(series_id: str, episode_id: str, body: EpisodeIn):
    item = series.update_episode(series_id, episode_id, body.model_dump())
    if not item:
        raise HTTPException(404, "Episode not found")
    return item


@router.delete("/series/{series_id}/episodes/{episode_id}")
def series_episode_delete(series_id: str, episode_id: str):
    if not series.delete_episode(series_id, episode_id):
        raise HTTPException(404, "Episode not found")
    return {"ok": True}


@router.post("/series/{series_id}/episodes/{episode_id}/scenes")
def series_scene_create(series_id: str, episode_id: str, body: SceneIn):
    item = series.create_scene(series_id, episode_id, body.prompt, body.timecode)
    if not item:
        raise HTTPException(404, "Episode not found")
    if body.title or body.promptOverride is not None or body.referenceAssetIds is not None or body.continuityEnabled is not None:
        item = series.update_scene(series_id, episode_id, item["id"], body.model_dump(exclude_none=True)) or item
    return item


@router.put("/series/{series_id}/episodes/{episode_id}/scenes/{scene_id}")
def series_scene_update(series_id: str, episode_id: str, scene_id: str, body: SceneIn):
    item = series.update_scene(series_id, episode_id, scene_id, body.model_dump(exclude_none=True))
    if not item:
        raise HTTPException(404, "Scene not found")
    return item


@router.delete("/series/{series_id}/episodes/{episode_id}/scenes/{scene_id}")
def series_scene_delete(series_id: str, episode_id: str, scene_id: str):
    if not series.delete_scene(series_id, episode_id, scene_id):
        raise HTTPException(404, "Scene not found")
    return {"ok": True}


@router.post("/series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/approve-keyframe")
def series_scene_approve_keyframe(series_id: str, episode_id: str, scene_id: str, job_id: str = "", output_index: int = 0):
    series_obj = series.get_series(series_id)
    if not series_obj:
        raise HTTPException(404, "Series not found")
    target_scene = None
    for ep in series_obj.get("episodes") or []:
        if str(ep.get("id")) == episode_id:
            for sc in ep.get("scenes") or []:
                if str(sc.get("id")) == scene_id:
                    target_scene = dict(sc)
                    break

    job = store.get_row("jobs", job_id) if job_id else None
    if job:
        job_status = str(job.get("status") or "")
        if job_status in ("queued", "processing", "submitting", "generating"):
            raise HTTPException(400, "Ảnh keyframe đang được tạo, vui lòng đợi hoàn tất.")
        if job_status in ("failed", "action_required"):
            series.update_scene(series_id, episode_id, scene_id, {"status": "error", "error": str(job.get("error") or "Job thất bại")})
            raise HTTPException(400, f"Job tạo ảnh đã thất bại: {job.get('error') or 'Lỗi không xác định'}")

    outputs = list((job or {}).get("outputs") or [])
    output_path = ""
    if 0 <= output_index < len(outputs) and Path(str(outputs[output_index])).is_file():
        output_path = str(outputs[output_index])
    elif target_scene and target_scene.get("keyframeOutput") and Path(str(target_scene.get("keyframeOutput"))).is_file():
        output_path = str(target_scene.get("keyframeOutput"))

    if not output_path:
        # Reset invalid awaiting_keyframe status to draft
        if target_scene and target_scene.get("status") == "awaiting_keyframe" and not target_scene.get("approvedKeyframe"):
            series.update_scene(series_id, episode_id, scene_id, {"status": "draft", "keyframeOutput": "", "keyframeJobId": ""})
        raise HTTPException(404, "Ảnh keyframe chưa được tạo hoặc file không tồn tại trên ổ đĩa. Vui lòng bấm 'Tạo keyframe' lại.")

    try:
        item = series.approve_keyframe(series_id, episode_id, scene_id, output_path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not item:
        raise HTTPException(404, "Scene not found")
    return item


@router.post("/series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/generate")
def series_scene_generate(series_id: str, episode_id: str, scene_id: str, body: SeriesGenerationIn):
    try:
        context = series.generation_context(series_id, episode_id, scene_id, body.artifact, body.promptOverride)
        payload = {
            "prompts": [context["prompt"]],
            "inputIndex": int(context.get("sceneIndex") or 1),
            "kind": "image" if body.artifact == "keyframe" else "video",
            "mode": "reference" if body.artifact == "keyframe" and context.get("sourceFiles") else "text",
            "accountId": body.accountId,
            "settings": {**body.settings, "outputDir": context["outputDir"]},
            "sourceFiles": context.get("sourceFiles") or [],
            "seriesContext": context,
        }
        return {"jobs": service.enqueue(payload)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/series/{series_id}/anchors/generate")
def series_anchor_generate(series_id: str, body: SeriesAnchorGenerationIn):
    """Generate one locked Series anchor image with the selected Flow account."""
    item = series.get_series(series_id)
    if not item:
        raise HTTPException(404, "Series not found")
    try:
        settings = dict(body.settings)
        settings["count"] = 1
        return {"jobs": service.enqueue({
            "prompts": [body.prompt], "kind": "image", "mode": "text",
            "accountId": body.accountId, "settings": settings, "sourceFiles": [],
            "seriesContext": {
                "artifact": "anchor", "seriesId": series_id,
                "seriesTitle": item["title"], "seriesSlug": item["slug"],
                "anchorLabel": body.prompt[:120],
            },
        })}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/series/{series_id}/run")
def series_run_start(series_id: str, body: SeriesRunIn):
    """Start a Series automation run (keyframe → approve → video)."""
    try:
        run_id = series_runner.start_run(
            series_id=series_id,
            episode_ids=[body.episodeId] if body.episodeId else None,
            account_id=body.accountId,
            settings=body.settings,
            image_model=body.imageModel,
            auto_approve=body.autoApprove,
            mode=body.mode,
        )
        return {"runId": run_id, "status": "running"}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/series/{series_id}/run/{run_id}")
def series_run_status(series_id: str, run_id: str):
    """Poll the status of a Series automation run."""
    snap = series_runner.get_run(run_id)
    if snap is None:
        # Run not found in memory (server restarted or run never existed).
        # Return a terminal "cancelled" payload so the frontend stops polling
        # instead of retrying forever with 404 errors.
        return {"runId": run_id, "status": "cancelled", "total": 0, "done": 0,
                "currentSceneId": "", "currentStep": "", "errors": []}
    return snap


@router.post("/series/{series_id}/run/{run_id}/stop")
def series_run_stop(series_id: str, run_id: str):
    """Stop a running Series automation run after the current scene."""
    if not series_runner.stop_run(run_id):
        raise HTTPException(404, "Run not found")
    return {"ok": True}


@router.post("/jobs/cancel-all")
def jobs_cancel_all():
    return {"ok": True, "cancelled": service.cancel_all()}


@router.post("/jobs/cancel-folder")
def jobs_cancel_folder(body: OutputFolderIn):
    return {
        "ok": True,
        "cancelled": service.cancel_output_folder_jobs(body.outputDir, body.kind),
        "jobs": service.jobs(),
    }


@router.delete("/jobs")
def jobs_delete_all():
    return {"ok": True, "deleted": service.delete_all_jobs()}


@router.post("/jobs/delete-folder")
def jobs_delete_folder(body: OutputFolderIn):
    return {
        "ok": True,
        "deleted": service.delete_output_folder_jobs(body.outputDir, body.kind),
        "jobs": service.jobs(),
    }


@router.post("/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str):
    job = service.cancel(job_id)
    if not job:
        raise HTTPException(404, "Flow job not found")
    return job


@router.post("/jobs/{job_id}/retry")
def jobs_retry(job_id: str, body: RetryIn | None = None):
    payload = body.model_dump() if body else {}
    job = service.retry(job_id, payload)
    if not job:
        raise HTTPException(404, "Flow job not found")
    return job


@router.delete("/jobs/{job_id}")
def jobs_delete(job_id: str):
    removed = service.delete_job(job_id)
    if not removed:
        raise HTTPException(404, "Flow job not found")
    return {"ok": True}


@router.get("/jobs/{job_id}/outputs/{output_index}")
def jobs_output(job_id: str, output_index: int, download: bool = False):
    job = store.get_row("jobs", job_id)
    outputs = list(job.get("outputs") or []) if job else []
    if output_index < 0 or output_index >= len(outputs):
        raise HTTPException(404, "Flow output not found")
    path = Path(outputs[output_index])
    if not path.is_file():
        raise HTTPException(404, "Flow output file is missing")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name if download else None)


@router.post("/jobs/{job_id}/outputs/{output_index}/reveal")
def jobs_output_reveal(job_id: str, output_index: int):
    job = store.get_row("jobs", job_id)
    outputs = list(job.get("outputs") or []) if job else []
    if output_index < 0 or output_index >= len(outputs):
        raise HTTPException(404, "Flow output not found")
    path = Path(outputs[output_index])
    if not path.is_file():
        raise HTTPException(404, "Flow output file is missing")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
    return {"ok": True, "path": str(path)}


@router.post("/open-folder")
def flow_open_folder(output_dir: str = "", kind: str = "video"):
    service = FlowService()
    dummy_job = {"kind": kind or "video", "settings": {"outputDir": output_dir}}
    folder = service._output_folder(dummy_job, create=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return {"ok": True, "path": str(folder)}
