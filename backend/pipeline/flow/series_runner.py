"""Background orchestrator for Series Flow automation.

Runs keyframe -> auto-approve -> video sequentially for each scene in
episode order, maintaining character and continuity across scenes.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import store

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 4.0   # seconds between job-status checks
_TERMINAL = {"done", "failed", "cancelled", "action_required"}
_SKIP_STATUSES = {"complete"}


def _id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


class _Run:
    """Single automation run tracked in memory."""

    def __init__(self, run_id: str, total: int) -> None:
        self.run_id = run_id
        self.status = "running"
        self.total = total
        self.done = 0
        self.current_scene_id = ""
        self.current_step = ""
        self.errors: list[dict[str, str]] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runId": self.run_id,
                "status": self.status,
                "total": self.total,
                "done": self.done,
                "currentSceneId": self.current_scene_id,
                "currentStep": self.current_step,
                "errors": list(self.errors),
            }

    def set_current(self, scene_id: str, step: str) -> None:
        with self._lock:
            self.current_scene_id = scene_id
            self.current_step = step

    def mark_done(self) -> None:
        with self._lock:
            self.done += 1

    def add_error(self, scene_id: str, error: str) -> None:
        with self._lock:
            self.errors.append({"sceneId": scene_id, "error": error})

    def finish(self, status: str) -> None:
        with self._lock:
            self.status = status
            self.current_scene_id = ""
            self.current_step = ""


class SeriesRunner:
    """Singleton that tracks and executes Series automation runs."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        self._guard = threading.Lock()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        return run.snapshot() if run else None

    def stop_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        run.stop()
        return True

    def start_run(
        self,
        *,
        series_id: str,
        episode_ids: list[str] | None,
        account_id: str,
        settings: dict[str, Any],
        image_model: str = "Nano Banana 2",
        auto_approve: bool = True,
        mode: str = "full",
    ) -> str:
        from . import series as series_mod
        from .service import service

        s = series_mod.get_series(series_id)
        if not s:
            raise KeyError("Series not found")
        if not store.get_row("accounts", account_id):
            raise ValueError("Flow account not found")

        scenes_to_run: list[tuple[dict, dict]] = []
        for episode in s.get("episodes") or []:
            if episode_ids and str(episode.get("id")) not in episode_ids:
                continue
            for scene in episode.get("scenes") or []:
                status = str(scene.get("status") or "draft")
                if status in _SKIP_STATUSES:
                    continue
                scenes_to_run.append((dict(episode), dict(scene)))

        run_id = _id()
        run = _Run(run_id, len(scenes_to_run))
        with self._guard:
            self._runs[run_id] = run

        if mode == "keyframes_only":
            # Keyframes can run in parallel; video continuity cannot.
            artifact = "keyframe"
            for episode, scene in scenes_to_run:
                try:
                    ctx = series_mod.generation_context(series_id, str(episode["id"]), str(scene["id"]), artifact)
                except Exception:
                    continue
                job_settings = {
                    **settings,
                    "outputDir": ctx["outputDir"],
                    "count": 1,
                    "model": image_model,
                }
                input_index = int(scene.get("index") or ctx.get("sceneIndex") or 1)
                jobs = service.enqueue({
                    "prompts": [ctx["prompt"]],
                    "inputIndex": input_index,
                    "kind": "image",
                    "mode": "reference" if ctx.get("sourceFiles") else "text",
                    "accountId": account_id,
                    "settings": job_settings,
                    "sourceFiles": ctx.get("sourceFiles") or [],
                    "seriesContext": ctx,
                })
                if jobs:
                    series_mod.register_job(jobs[0])
            run.mark_done()
        else:
            # full + videos_only: finish each video before the next so end frames chain.
            threading.Thread(
                target=self._orchestrate,
                args=(run, series_id, scenes_to_run, account_id, settings, image_model, auto_approve, mode),
                daemon=True,
                name=f"series-run-{run_id}",
            ).start()

        return run_id

    def _process_scene(
        self,
        run: _Run,
        series_id: str,
        episode: dict[str, Any],
        scene: dict[str, Any],
        account_id: str,
        settings: dict[str, Any],
        image_model: str,
        auto_approve: bool,
        mode: str,
        *,
        count_progress: bool = True,
    ) -> None:
        from . import series as series_mod
        from .service import service

        scene_id = str(scene.get("id") or "")
        episode_id = str(episode.get("id") or "")

        try:
            if run.should_stop():
                return

            if mode != "videos_only":
                # Check fresh keyframe status
                fresh_series = series_mod.get_series(series_id)
                fresh_scene = None
                for ep in (fresh_series or {}).get("episodes") or []:
                    if str(ep.get("id")) == episode_id:
                        for sc in ep.get("scenes") or []:
                            if str(sc.get("id")) == scene_id:
                                fresh_scene = sc
                                break
                has_keyframe = bool((fresh_scene or scene).get("approvedKeyframe"))
                if not has_keyframe:
                    run.set_current(scene_id, "generating_keyframe")
                    ctx = series_mod.generation_context(series_id, episode_id, scene_id, "keyframe")
                    img_settings = {**settings, "model": image_model, "count": 1, "outputDir": ctx["outputDir"]}
                    done_job: dict = {}
                    for _attempt in range(3):  # retry up to 3× for transient 403/reCAPTCHA
                        jobs = service.enqueue({
                            "prompts": [ctx["prompt"]], "kind": "image",
                            "mode": "reference" if ctx.get("sourceFiles") else "text",
                            "accountId": account_id, "settings": img_settings,
                            "sourceFiles": ctx.get("sourceFiles") or [], "seriesContext": ctx,
                        })
                        done_job = self._poll_job(str(jobs[0]["id"]), run)
                        if run.should_stop():
                            return
                        if done_job.get("status") == "cancelled" or run.should_stop():
                            run.stop()
                            return
                        err = str(done_job.get("error") or "")
                        if done_job.get("status") == "done" or ("403" not in err and "reCAPTCHA" not in err):
                            break
                        # Transient reCAPTCHA — wait for token refresh and retry
                        import time as _time
                        _time.sleep(30)
                    if done_job.get("status") != "done":
                        raise RuntimeError(str(done_job.get("error") or "Keyframe job failed"))
                    if auto_approve:
                        run.set_current(scene_id, "approving_keyframe")
                        outputs = list(done_job.get("outputs") or [])
                        if outputs:
                            series_mod.approve_keyframe(series_id, episode_id, scene_id, str(outputs[0]))

            if run.should_stop():
                return

            if mode != "keyframes_only":
                # Reload scene to get fresh approvedKeyframe
                fresh_series = series_mod.get_series(series_id)
                scene_cur = None
                if fresh_series:
                    for ep in fresh_series.get("episodes") or []:
                        if str(ep.get("id")) == episode_id:
                            for sc in ep.get("scenes") or []:
                                if str(sc.get("id")) == scene_id:
                                    scene_cur = dict(sc)

                if not (scene_cur and scene_cur.get("approvedKeyframe")):
                    # Try to use first series anchor asset as start frame
                    anchor_ids = (fresh_series or {}).get("anchorAssets") or []
                    anchor_path: str | None = None
                    for asset_id in anchor_ids:
                        resolved = series_mod.asset_path(series_id, str(asset_id))
                        if resolved and resolved.is_file():
                            anchor_path = str(resolved)
                            break
                    if anchor_path:
                        series_mod.approve_keyframe(series_id, episode_id, scene_id, anchor_path)
                        fresh_series = series_mod.get_series(series_id)
                        if fresh_series:
                            for ep in fresh_series.get("episodes") or []:
                                if str(ep.get("id")) == episode_id:
                                    for sc in ep.get("scenes") or []:
                                        if str(sc.get("id")) == scene_id:
                                            scene_cur = dict(sc)
                    if not (scene_cur and scene_cur.get("approvedKeyframe")):
                        # Omni Flash is text-to-video — keyframe/start frame optional.
                        if not re.search(r"omni|flash", str(settings.get("model") or ""), re.I):
                            raise RuntimeError("No approved keyframe - add an anchor asset or generate a keyframe first")

                # If video already completed, finish
                if scene_cur and scene_cur.get("videoOutput") and Path(str(scene_cur.get("videoOutput"))).is_file() and scene_cur.get("status") == "complete":
                    run.mark_done()
                    return

                run.set_current(scene_id, "generating_video")
                ctx = series_mod.generation_context(series_id, episode_id, scene_id, "video")
                vid_settings = {**settings, "count": 1, "outputDir": ctx["outputDir"]}
                source_files = list(ctx.get("sourceFiles") or [])
                model_name = str(vid_settings.get("model") or "")
                # Omni Flash is text-to-video in Flow UI (360p/720p + duration). It does
                # not expose the Veo Frames start-image upload — forcing frame mode fails
                # with "start image upload control was not found". Continuity stays in prompt.
                is_omni = bool(re.search(r"omni|flash", model_name, re.I))
                if is_omni:
                    source_files = []
                    video_mode = "text"
                else:
                    video_mode = "frame" if source_files else "text"
                time.sleep(15)
                for _vid_attempt in range(3):
                    jobs = service.enqueue({
                        "prompts": [ctx["prompt"]],
                        "kind": "video",
                        "mode": video_mode,
                        "accountId": account_id,
                        "settings": vid_settings,
                        "sourceFiles": source_files,
                        "seriesContext": ctx,
                    })
                    done_job = self._poll_job(str(jobs[0]["id"]), run)
                    if run.should_stop():
                        return
                    if done_job.get("status") == "cancelled" or run.should_stop():
                        run.stop()
                        return
                    _err = str(done_job.get("error") or "")
                    if done_job.get("status") == "done" or ("403" not in _err and "reCAPTCHA" not in _err):
                        break
                    _log.warning(
                        "reCAPTCHA on video generation for %s (attempt %d/3), waiting 45 s…",
                        scene_id, _vid_attempt + 1,
                    )
                    time.sleep(45)
                if done_job.get("status") != "done":
                    raise RuntimeError(str(done_job.get("error") or "Video job failed"))

            if count_progress:
                run.mark_done()

        except Exception as exc:
            run.add_error(scene_id, str(exc))
            if count_progress:
                run.mark_done()

    def _orchestrate(
        self,
        run: _Run,
        series_id: str,
        scenes: list[tuple[dict, dict]],
        account_id: str,
        settings: dict[str, Any],
        image_model: str,
        auto_approve: bool,
        mode: str,
    ) -> None:
        try:
            # A scene's actual final frame is the first reference for the next
            # keyframe, so a full run must finish each video before proceeding.
            for episode, scene in scenes:
                if run.should_stop() or run.errors:
                    break
                self._process_scene(
                    run, series_id, episode, scene, account_id, settings,
                    image_model, auto_approve, mode=mode, count_progress=True,
                )

            if run.should_stop():
                run.finish("cancelled")
            elif run.errors:
                run.finish("done_with_errors")
            else:
                run.finish("done")

        except Exception as exc:
            run.add_error("", str(exc))
            run.finish("failed")

    def _poll_job(self, job_id: str, run: _Run) -> dict[str, Any]:
        while not run.should_stop():
            job = store.get_row("jobs", job_id)
            if not job:
                time.sleep(_POLL_INTERVAL)
                continue
            if str(job.get("status") or "") in _TERMINAL:
                return job
            time.sleep(_POLL_INTERVAL)
        return store.get_row("jobs", job_id) or {"status": "cancelled"}


runner = SeriesRunner()
