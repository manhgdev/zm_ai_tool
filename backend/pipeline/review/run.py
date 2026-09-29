"""Review orchestrator using parallel, fully finalized Review parts."""
from __future__ import annotations

import hashlib
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pipeline.clone_run.headless import _copy_output
from pipeline.clone_run.open_source import open_local_video
from pipeline.core.jobs import check_cancel, run_cmd, share_cancel
from pipeline.core.media import _ff_bin, ffprobe_duration, video_size
from pipeline.core.project import ensure_layout, save_meta, set_status
from pipeline.export.burn import cover_and_burn
from pipeline.export.mux_audio import mux_dub
from pipeline.queue.store import get as get_queue_job, mutate
from pipeline.review.adapter import (
    _fallback_review_bbox,
    apply_edit_plan,
    caption_export_settings,
    locate_review_caption_band,
)
from pipeline.review.cache import INVALIDATE_FROM, STAGES, load_json, movie_root, run_dir, save_json
from pipeline.review.compose import compose_video, concat_parts
from pipeline.review.inspect import inspect_media
from pipeline.review.llm import list_ollama_models, pick_llm
from pipeline.review.match import match_voice, resolve_build_mode
from pipeline.review.scenes import detect_scenes
from pipeline.review.script import NARRATION, scrub_script, write_script
from pipeline.review.story import build_story
from pipeline.review.transcript import load_transcript
from pipeline.review.vision import analyze_scenes
from pipeline.tts import tts_segment


REVIEW_PLAN_VERSION = 26
REVIEW_STORY_VERSION = 8
REVIEW_FINALIZE_VERSION = 8
REVIEW_MATCH_VERSION = 6
WINDOW_SOURCE_VERSION = 1

# Stable protocol consumed and localized by the client. Never display it raw.
PROGRESS_HEARTBEAT_PREFIX = "__VC_PROGRESS__"


import time as _time


class _timed:
    """Context manager: log elapsed time for a stage to app_log and return seconds."""
    def __init__(self, label: str):
        self.label = label
        self.elapsed = 0.0
    def __enter__(self):
        self._t = _time.monotonic()
        try:
            from pipeline.core.app_log import append_log
            append_log(f"[pipeline] ▶ {self.label}")
        except Exception:
            pass
        return self
    def __exit__(self, *_):
        self.elapsed = _time.monotonic() - self._t
        try:
            from pipeline.core.app_log import append_log
            append_log(f"[pipeline] ✔ {self.label} {self.elapsed:.1f}s")
        except Exception:
            pass


@contextmanager
def _progress_heartbeat(job_id: str, stage: str, *, interval_sec: float = 8.0, emit: bool = False):
    """Optionally report an elapsed heartbeat for a blocking stage.

    Review stages now publish their own meaningful counts/progress.  Generic
    heartbeats are disabled by default because their whole-pipeline percentage
    was being mistaken for the percentage of Whisper or visual analysis.
    """
    if not emit:
        yield
        return
    stop = threading.Event()
    started_at = _time.monotonic()

    def report() -> None:
        while not stop.wait(max(0.01, interval_sec)):
            elapsed = max(1, round(_time.monotonic() - started_at))
            current = get_queue_job(job_id) or {}
            progress = max(0, min(100, round(float(current.get("progress") or 0) * 100)))
            _note(job_id, f"{PROGRESS_HEARTBEAT_PREFIX}|{stage}|{elapsed}|{progress}")

    worker = threading.Thread(target=report, name=f"review-progress-{stage}", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=0.1)


def run_review_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job["id"])
    src = Path(str(job.get("source") or ""))
    settings = dict(job.get("settings_snapshot") or {})
    # v6 adds part-specific evidence so cached generic scripts are rebuilt.
    settings["reviewPlanVersion"] = REVIEW_PLAN_VERSION
    settings["reviewMatchVersion"] = REVIEW_MATCH_VERSION
    mode = resolve_build_mode(settings)
    settings["buildMode"] = mode
    review_mode = str(settings.get("reviewMode") or "llm").strip().lower()
    if review_mode not in {"llm", "cloud", "translate"}:
        review_mode = "llm"
    settings["reviewMode"] = review_mode
    review_provider = str(settings.get("reviewProvider") or "gemini").strip().lower()
    if review_provider not in {"gemini", "grok", "openai", "deepseek", "openrouter", "groq", "nvidia", "mistral"}:
        review_provider = "gemini"
    settings["reviewProvider"] = review_provider
    source_lang = str(settings.get("sourceLang") or "auto")
    recognition_engine = str(settings.get("recognitionEngine") or "whisper").strip().lower()
    if recognition_engine not in {"whisper", "capcut"}:
        recognition_engine = "whisper"
    settings["recognitionEngine"] = recognition_engine
    lang = str(settings.get("language") or "vi")
    voice = str(settings.get("voice") or "system")
    _note(job_id, f"Nguồn: {src}", stage="metadata", progress=0.04)
    _note(job_id, f"Cài đặt: nhận dạng={recognition_engine} gốc={source_lang} thoại={lang} mode={mode} voice={voice} caption={settings.get('captionMode') or 'off'}")
    check_cancel(job_id)
    with _progress_heartbeat(job_id, "metadata"):
        meta = inspect_media(src)
    root = movie_root(src)
    save_json(root / "metadata.json", meta)
    duration = float(meta.get("duration") or 0)
    _note(
        job_id,
        f"Metadata: {_mmss(duration)} · {int(meta.get('width') or 0)}x{int(meta.get('height') or 0)} · cache {root.name}",
    )
    windows = _windows(duration, mode, settings)
    nwin = max(1, len(windows))
    window_duration = sum(max(0.0, end - start) for start, end in windows)
    available_models = list_ollama_models()
    requested_model = str(settings.get("reviewModel") or "auto").strip()
    requested_cloud_model = str(settings.get("reviewCloudModel") or "").strip()
    if review_mode == "llm" and requested_model not in {"", "auto"} and requested_model not in available_models:
        raise RuntimeError("REVIEW_LLM_MODEL_UNAVAILABLE")
    if review_mode == "cloud":
        from pipeline.core.app_config import load_app_config

        cloud = load_app_config()["cloud"].get(review_provider) or {}
        if not str(cloud.get("apiKey") or "").strip():
            raise RuntimeError(f"REVIEW_CLOUD_KEY_REQUIRED:{review_provider}")
    review_model = _select_review_model(
        review_mode,
        review_provider,
        requested_model,
        available_models,
        cloud_model=requested_cloud_model,
    )
    if review_mode == "llm" and not review_model:
        raise RuntimeError("REVIEW_LLM_REQUIRED")
    settings["reviewModel"] = review_model or "translate"
    _note(
        job_id,
        (
            f"Review script: AI dựng mạch từ timeline thoại · model {review_model}"
            if review_model
            else "Review script: dịch tuần tự transcript, không dùng LLM"
        ),
    )

    with _timed("scene_detect") as _t_scenes:
        scenes = _cached_or(
            root / "scenes.json", lambda: detect_scenes(src, duration, job_id=job_id),
            job_id, "scenes", 0.12, "Cắt cảnh",
        )
    _note(job_id, f"Cảnh: {len(scenes) if isinstance(scenes, list) else 0} đoạn · {_t_scenes.elapsed:.0f}s")
    # Earlier CapCut runs cached the untranslated ASR although CapCut already
    # supplied timed target-language cues. Keep that legacy cache isolated so
    # a retry rebuilds its evidence and script from the translated transcript.
    capcut_target_cache = "_capcut_target_v1" if recognition_engine == "capcut" else ""
    with _timed("transcript") as _t_tr:
        transcript = _cached_or(
            root / f"transcript_{recognition_engine}_{source_lang}_{lang}{capcut_target_cache}.json",
            lambda: load_transcript(
                src, root, job_id=job_id, duration=duration,
                sidecar=str(settings.get("subtitleFile") or ""),
                source_lang=source_lang,
                recognition_engine=recognition_engine,
                target_lang=lang,
            ),
            job_id,
            "transcript",
            0.28,
            f"Transcript ({recognition_engine}:{source_lang})",
        )
    sample = _clip((transcript[0].get("text") if transcript else "") or "")
    _note(job_id, f"Transcript: {len(transcript)} câu · {_t_tr.elapsed:.0f}s" + (f" · ví dụ: {sample}" if sample else ""))
    with _timed("visual_analysis") as _t_vis:
        visuals = _cached_or(
            root / "visual_analysis" / f"scenes_grounded_v2_{review_mode}_{source_lang}{capcut_target_cache}.json",
            lambda: analyze_scenes(src, scenes, transcript, root, job_id=job_id, use_vision=False),
            job_id,
            "vision",
            0.42,
            "Lập chỉ mục cảnh",
        )
    _note(job_id, f"Chỉ mục cảnh: {len(visuals) if isinstance(visuals, list) else 0} cảnh · {_t_vis.elapsed:.0f}s")

    run_id = str(job.get("runId") or uuid.uuid4().hex[:8])
    rd = run_dir(root, run_id)
    save_json(rd / "settings.json", settings)
    mutate(job_id, {"runId": run_id})

    def story_progress(stage: str, done: int, total: int, workers: int) -> None:
        label = "Tóm tắt mốc thoại" if stage == "blocks" else "Tóm tắt chương"
        stage_progress = 0.42 + 0.10 * (done / max(1, total))
        task_percent = round(done * 100 / max(1, total))
        overall_percent = round(stage_progress * 100)
        _note(
            job_id,
            f"{label}: {done}/{total} ({task_percent}%) · tiến trình {overall_percent}% · {workers} luồng",
            stage="story_graph",
            progress=stage_progress,
        )

    if review_mode != "translate":
        story_builder = lambda: build_story(
            visuals, transcript=transcript, language=lang, model=review_model,
            on_progress=story_progress, title=src.name, job_id=job_id,
        )
    else:
        story_builder = lambda: _faithful_story(visuals)
    story_model_key = re.sub(r"[^A-Za-z0-9._-]+", "_", review_model or "translate")
    with _timed("story_graph") as _t_story:
        story = _cached_or(
            root / f"story_graph_v{REVIEW_STORY_VERSION}_{review_mode}_{story_model_key}_{lang}_{source_lang}{capcut_target_cache}.json",
            story_builder,
            job_id,
            "story_graph",
            0.42,
            f"Cốt truyện ({lang})",
        )
    ctx = story.get("movie_context") or {}
    save_json(rd / "movie_context.json", ctx)
    save_json(rd / "chapter_summaries.json", story.get("chapters") or [])
    save_json(rd / "segment_summaries.json", story.get("blocks") or [])
    _note(job_id, f"Cốt truyện: {len(story.get('chapters') or [])} chương · {len(story.get('blocks') or [])} khối · {_t_story.elapsed:.0f}s · { _clip(ctx.get('logline') or '') }", progress=0.52)

    start, prev_rd, changed = _resume_point(root, settings)
    reuse_script = _reuse(start, "script")
    reuse_tts = _reuse(start, "tts")
    reuse_match = _reuse(start, "matching")
    reuse_tl = _reuse(start, "timeline")
    prev_scripts = _load_scripts(prev_rd, nwin) if (reuse_script or reuse_tts or reuse_match) else None
    scripts_scrubbed = False
    if prev_scripts:
        cleaned: list[dict[str, Any]] = []
        for row in prev_scripts:
            scrubbed = scrub_script(row, lang)
            if not scrubbed:
                cleaned = []
                break
            scripts_scrubbed = scripts_scrubbed or scrubbed != row
            cleaned.append(scrubbed)
        prev_scripts = cleaned or None
    if reuse_script and not prev_scripts:
        reuse_script = False
    if not reuse_script:
        reuse_tts = reuse_match = reuse_tl = False
    if scripts_scrubbed:
        reuse_tts = False
    prev_voice = load_json(prev_rd / "voice.json") if prev_rd and reuse_tts else None
    if reuse_tts and (not prev_scripts or not _voice_wavs_ok(prev_scripts, prev_voice)):
        reuse_tts = False
    if not reuse_tts:
        reuse_match = reuse_tl = False
    finalize_key = _finalize_key(settings)
    script_settings_key = _script_settings_key(settings)
    if reuse_match and not _parts_complete(prev_rd, nwin, finalize_key):
        reuse_match = False
    if not reuse_match:
        reuse_tl = False
    _note(job_id, _resume_log(start, changed, reuse_script=reuse_script, reuse_tts=reuse_tts, reuse_match=reuse_match, reuse_tl=reuse_tl))

    factor = NARRATION.get(str(settings.get("narration") or "default"), 1.0)
    parts_meta = [
        {
            "index": i + 1,
            "sourceStart": a,
            "sourceEnd": b,
            "outputDuration": None,
            "expectedOutputDuration": _script_duration(mode, a, b, window_duration, settings, factor),
            "status": "pending",
        }
        for i, (a, b) in enumerate(windows)
    ]
    _refresh_part_timeline(parts_meta, factor=factor)
    _note(job_id, f"Dựng: {nwin} phần · {_fmt_range(0, duration)}", parts=parts_meta, stage="script", progress=0.56)

    # Each queued Review is a separate edit/export run. Reusing the project of
    # an earlier job aliases cancellation and lets a later run stop its FFmpeg.
    # Analysis cache remains shared by source fingerprint in ``review_cache``.
    project_id = open_local_video(str(src), kind="review", reuse_existing=False)
    share_cancel(job_id, project_id)
    tts_dir = ensure_layout(project_id) / "tts"
    orig_pct = float(settings.get("originalAudioPct") or 0)
    _note(job_id, f"Project {project_id} · TTS voice={voice} · originalAudio={orig_pct:.0f}%", projectId=project_id)

    all_voiced: list[dict[str, Any]] = []
    combined_segs: list[dict[str, Any]] = []
    raw_part_files: list[Path] = []
    part_files: list[Path] = []
    t_voice = 0.0

    if reuse_script and prev_scripts:
        for i, script in enumerate(prev_scripts):
            save_json(rd / f"script_{i:02d}.json", script)

    if reuse_match and prev_rd:
        prev_plan = load_json(prev_rd / "edit_plan.json") or {}
        combined_segs = [dict(s) for s in (prev_plan.get("segments") or []) if isinstance(s, dict)]
        t_voice = float(prev_plan.get("duration") or 0)
        all_voiced = [dict(v) for v in (load_json(prev_rd / "voice.json") or []) if isinstance(v, dict)]
        cached_durations = _part_durations(combined_segs, nwin)
        for pi in range(nwin):
            raw_part = prev_rd / f"raw_part_{pi:02d}.mp4"
            part = prev_rd / f"part_{pi:02d}.mp4"
            raw_part_files.append(raw_part)
            part_files.append(part)
            parts_meta[pi]["status"] = "done"
            parts_meta[pi]["output"] = str(part)
            parts_meta[pi]["outputDuration"] = cached_durations[pi]
        _refresh_part_timeline(parts_meta, factor=factor)
        _note(job_id, f"Ghép hình: cache {nwin} phần", parts=parts_meta, stage="matching", progress=0.84)
    else:
        part_results: dict[int, tuple[Path, dict[str, Any], list[dict[str, Any]]]] = {}
        pending: list[tuple[int, float, float]] = []
        for pi, (w0, w1) in enumerate(windows):
            check_cancel(job_id)
            raw_part = rd / f"raw_part_{pi:02d}.mp4"
            part = rd / f"part_{pi:02d}.mp4"
            script_dur = _script_duration(mode, w0, w1, window_duration, settings, factor)
            cached_script = load_json(rd / f"script_{pi:02d}.json") or {}
            cached_plan = load_json(rd / f"plan_{pi:02d}.json") or {}
            cached_voice = load_json(rd / f"voice_{pi:02d}.json") or []
            cached_final = load_json(rd / f"final_{pi:02d}.json") or {}
            if (
                not scripts_scrubbed
                and _part_cache_matches(
                    cached_script, script_dur,
                    source_start=w0,
                    source_end=w1,
                    settings_key=script_settings_key,
                )
                and _media_artifact_ok(raw_part)
                and _media_artifact_ok(part)
                and isinstance(cached_plan, dict)
                and cached_plan.get("segments")
                and isinstance(cached_voice, list)
                and cached_voice
                and cached_final.get("finalizeKey") == finalize_key
            ):
                voiced = [dict(v) for v in cached_voice if isinstance(v, dict)]
                part_results[pi] = (part, cached_plan, voiced)
                parts_meta[pi].update({
                    "status": "done",
                    "output": str(part),
                    "outputDuration": float(cached_plan.get("duration") or 0),
                })
                _refresh_part_timeline(parts_meta, factor=factor)
                _note(job_id, f"Phần {pi + 1}/{nwin}: giữ kết quả đã xong", parts=parts_meta)
            else:
                pending.append((pi, w0, w1))

        # Window work is independent in accumulate mode. Keep aggregate nested
        # TTS/FFmpeg work bounded while preserving source-index assembly below.
        _outer_workers, bounded_tts_workers, bounded_compose_workers = _accumulate_worker_limits(len(pending))
        # Ceilings only — TTS/compose pools elastically fill up to these.
        tts_workers = bounded_tts_workers if mode == "accumulate" else None
        compose_workers = bounded_compose_workers if mode == "accumulate" else None
        parts_lock = threading.Lock()

        def build_part(item: tuple[int, float, float]) -> tuple[int, Path, dict[str, Any], list[dict[str, Any]]]:
            pi, w0, w1 = item
            check_cancel(job_id)
            with parts_lock:
                parts_meta[pi]["status"] = "running"
                _refresh_part_timeline(parts_meta, factor=factor)
                _note(
                    job_id,
                    f"Phần {pi + 1}/{nwin} {_fmt_range(w0, w1)} — bắt đầu xử lý",
                    parts=parts_meta,
                    stage=f"Phần {pi + 1}/{nwin}",
                    progress=0.10 + 0.46 * (pi / nwin),
                )
            raw_part = rd / f"raw_part_{pi:02d}.mp4"
            part = rd / f"part_{pi:02d}.mp4"
            part_source = src
            part_meta = meta
            part_transcript = transcript
            # Analyze the source once. Each cumulative part receives only its
            # timeline slice, while clip timestamps remain in the full source.
            # This avoids repeating Whisper, vision and LLM work per window.
            vis = _slice_visuals(visuals, w0, w1) if nwin > 1 else visuals
            story_part = _slice_story(story, vis) if nwin > 1 else story
            part_transcript = [
                row for row in transcript
                if float(row.get("end") or 0) >= w0
                and float(row.get("start") or 0) <= w1
            ]
            script_dur = _script_duration(mode, w0, w1, window_duration, settings, factor)
            with parts_lock:
                _note(
                    job_id,
                    f"Phần {pi + 1}/{nwin} {_fmt_range(w0, w1)} — "
                    f"{'kịch bản cache' if reuse_script else 'viết kịch bản'} · "
                    f"TTS {tts_workers or 1} / FFmpeg {compose_workers or 1} luồng",
                    parts=parts_meta,
                    stage=f"Phần {pi + 1}/{nwin}",
                    progress=0.56 + 0.28 * (pi / nwin),
                )
            if reuse_script and prev_scripts:
                script = dict(prev_scripts[pi])
            else:
                _note(
                    job_id,
                    f"Phần {pi + 1}: LLM đang viết kịch bản (~{round(script_dur*5.0):.0f} từ · {review_model})…",
                    parts=parts_meta, stage=f"LLM {pi+1}/{nwin}",
                    progress=0.56 + 0.28 * (pi / nwin),
                )
            with _progress_heartbeat(job_id, "script"), _timed(f"script_p{pi}") as _t_sc:
                script = write_script(
                    story_part,
                    duration_sec=script_dur,
                    style=str(settings.get("style") or "normal"),
                    language=lang,
                    spoiler=str(settings.get("spoiler") or "none"),
                    narration=str(settings.get("narration") or "default"),
                    notes=str(settings.get("notes") or ""),
                    genre=str(settings.get("genre") or ""),
                    visuals=vis,
                    source_transcript=part_transcript,
                    job_id=job_id,
                    use_llm=review_mode != "translate",
                    llm_model=review_model,
                )
            script["reviewPlanVersion"] = REVIEW_PLAN_VERSION
            script["settingsKey"] = script_settings_key
            script["targetDurationSec"] = round(script_dur, 3)
            script["sourceStart"] = round(w0, 3)
            script["sourceEnd"] = round(w1, 3)
            script["windowSourceVersion"] = WINDOW_SOURCE_VERSION
            save_json(rd / f"script_{pi:02d}.json", script)
            segs = script.get("segments") or []
            natural_duration = float(script.get("naturalDurationSec") or script_dur)
            _note(job_id, f"Kịch bản phần {pi + 1}: {len(segs)} đoạn · mục tiêu tự nhiên {natural_duration:.0f}s · {_t_sc.elapsed:.0f}s" + (" · cache" if reuse_script else ""))
            if segs:
                _note(job_id, f"Đoạn 1: {_clip(segs[0].get('text') or '', 120)}")
            if reuse_tts:
                voiced = _attach_prev_voice(script, prev_voice, pi) or []
                _note(job_id, f"TTS phần {pi + 1}: cache {len(voiced)} file")
            else:
                with _progress_heartbeat(job_id, "tts"), _timed(f"tts_p{pi}") as _t_tts:
                    voiced = _tts_parallel(
                        segs,
                        voice=voice,
                        tts_dir=tts_dir,
                        pi=pi,
                        lang=lang,
                        job_id=job_id,
                        project_id=project_id,
                        max_workers=tts_workers,
                    )
                tts_sum = sum(float(v.get("duration") or 0) for v in voiced)
                _note(job_id, f"TTS xong phần {pi + 1}: {len(voiced)} file · {tts_sum:.1f}s audio · {_t_tts.elapsed:.0f}s real")
            # Keep the generated voice at its natural pace. A selected duration
            # guides the script; it must never slow a short narration.
            voiced = _cap_voiced_duration(voiced, natural_duration)
            match_visuals = _visuals_for_match(vis, w0, w1)
            if not vis:
                _note(
                    job_id,
                    f"Phần {pi + 1}: không có cảnh nhận diện — dùng toàn bộ phạm vi nguồn làm cảnh dự phòng",
                )
            with _progress_heartbeat(job_id, "matching"):
                plan = match_voice(
                    voiced,
                    match_visuals,
                    style=str(settings.get("style") or "normal"),
                    spoiler=str(settings.get("spoiler") or "none"),
                    mode=mode,
                    keep_sec=float(settings.get("keepSec") or 4),
                    skip_sec=float(settings.get("skipSec") or 10),
                    pause_pace=str(settings.get("pausePace") or "balanced"),
                )
            clip_count = sum(len(seg.get("clips") or []) for seg in plan.get("segments") or [])
            _note(
                job_id,
                f"Ghép hình phần {pi + 1}: {clip_count} clip từ {len(match_visuals)} cảnh",
            )
            with _progress_heartbeat(job_id, "compose"), _timed(f"compose_p{pi}") as _t_comp:
                compose_video(
                    part_source,
                    plan,
                    raw_part,
                    ratio=str(settings.get("ratio") or "16:9"),
                    width=int(part_meta.get("width") or 1920),
                    height=int(part_meta.get("height") or 1080),
                    job_id=job_id,
                    original_pct=100.0 if orig_pct > 0.5 else 0.0,
                    clip_workers=compose_workers,
                    fallback_start=w0,
                    fallback_end=w1,
                )
            _note(job_id, f"FFmpeg compose phần {pi + 1}: {clip_count} clip · {_t_comp.elapsed:.0f}s")
            check_cancel(job_id)
            # The source bbox lives in source pixels (for example 1280×720),
            # while this part is composed at the export resolution (1920×1080).
            # Locate after crop/scale so both the blur and replacement caption
            # use the coordinates actually rendered to the viewer.
            # Prefer the cached subtitle band from project meta (written by
            # _apply_fixed_review_bboxes at the end of the PREVIOUS run).
            # Fresh locate on raw_part is non-deterministic and can return
            # only 1 row for short clips, causing incorrect cover placement.
            from pipeline.core.project import load_meta as _load_meta
            _proj_meta = _load_meta(project_id) or {}
            _cached_band = (_proj_meta.get("settings") or {}).get("subtitleBand")
            if _cached_band and isinstance(_cached_band, dict) and _cached_band.get("h", 0) > 0:
                part_caption_bbox = dict(_cached_band)
            else:
                part_caption_bbox = locate_review_caption_band(raw_part)

            audio_segments = _part_export_segments(
                plan, raw_part, caption_bbox=part_caption_bbox,
            )
            caption_segments = _review_caption_cues(audio_segments)
            caption_flags = caption_export_settings(settings)
            render_caption_segments = caption_segments
            if caption_flags["coverHardsubs"]:
                # Cover mode must hide the old subtitle lane continuously,
                # including pauses between narration cues.
                render_caption_segments = [
                    _review_cover_lane(raw_part, part_caption_bbox),
                    *caption_segments,
                ]
            mux_source = raw_part
            if caption_flags["burnSubs"] or caption_flags["coverHardsubs"]:
                _note(
                    job_id,
                    f"Hiệu ứng làm mờ dải chữ & chèn phụ đề phần {pi + 1} ({len(caption_segments)} câu)…",
                    stage=f"Mờ & Phụ đề {pi + 1}/{nwin}",
                    progress=0.85 + 0.05 * (pi / nwin),
                )
                burned = rd / f"burned_part_{pi:02d}.mp4"
                with _progress_heartbeat(job_id, "captions"):
                    cover_and_burn(
                        raw_part,
                        render_caption_segments,
                        burned,
                        cover=bool(caption_flags["coverHardsubs"]),
                        burn=bool(caption_flags["burnSubs"]),
                        subtitle_font_size=int(settings.get("subtitleFontSize") or 0),
                        subtitle_font_family=str(settings.get("subtitleFontFamily") or "system"),
                        project_id=project_id,
                        workers=int(compose_workers or 0),
                        caption_placement=str(caption_flags["captionPlacement"]),
                        cover_mask_style=str(settings.get("coverMaskStyle") or "blur"),
                        cover_mask_color=str(settings.get("coverMaskColor") or "#000000"),
                        cover_mask_opacity=int(settings.get("coverMaskOpacity", 0)),
                        caption_text_color=str(settings.get("captionTextColor") or "#ffffff"),
                        caption_bg_style=str(settings.get("captionBgStyle") or "none"),
                        caption_bg_color=str(settings.get("captionBgColor") or "#000000"),
                        caption_bg_opacity=int(settings.get("captionBgOpacity", 55)),
                        caption_stroke=bool(settings.get("captionStroke", True)),
                    )
                _note(job_id, f"Hiệu ứng & phụ đề phần {pi + 1}: hoàn thành")
                mux_source = burned
            check_cancel(job_id)
            _note(
                job_id,
                f"Lồng ghép âm thanh & nhạc nền phần {pi + 1} ({len(audio_segments)} đoạn thoại)…",
                stage=f"Lồng tiếng {pi + 1}/{nwin}",
                progress=0.90 + 0.04 * (pi / nwin),
            )
            with _progress_heartbeat(job_id, "audio_mix"):
                mux_dub(
                    project_id,
                    mux_source,
                    audio_segments,
                    original_audio_mode="original" if orig_pct > 0.5 else "mute",
                    original_audio_volume=max(0.0, min(1.0, orig_pct / 100.0)),
                    allow_video_slowdown=False,
                    match="preferAudio" if mode == "stretch" else "preferVideo",
                    max_tts_speed=4.0,
                    allow_external_audio=True,
                    destination=part,
                    namespace=f"review_{run_id}_part_{pi:02d}",
                )
            _note(job_id, f"Lồng tiếng phần {pi + 1}: hoàn thành")
            save_json(rd / f"plan_{pi:02d}.json", plan)
            save_json(rd / f"voice_{pi:02d}.json", voiced)
            save_json(rd / f"final_{pi:02d}.json", {
                "reviewPlanVersion": REVIEW_PLAN_VERSION,
                "finalizeKey": finalize_key,
                "raw": str(raw_part),
                "finished": str(part),
            })
            part_duration = float(plan.get("duration") or 0)
            with parts_lock:
                parts_meta[pi].update({
                    "status": "done",
                    "output": str(part),
                    "outputDuration": part_duration,
                })
                _refresh_part_timeline(parts_meta, factor=factor)
                _note(job_id, f"Phần {pi + 1} xong · {part.name} · {part.stat().st_size if part.is_file() else 0} bytes", parts=parts_meta)
            return pi, part, plan, voiced

        if mode == "accumulate" and len(pending) > 1:
            _note(
                job_id,
                f"Dựng tuần tự {len(pending)} phần · phần hiện tại dùng "
                f"TTS {bounded_tts_workers} / FFmpeg {bounded_compose_workers} luồng",
            )
        # Strictly finish and persist each part before starting the next one.
        # A one-worker executor still pre-queues later parts after an exception.
        rows = [build_part(item) for item in pending]
        for pi, part, plan, voiced in rows:
            part_results[pi] = (part, plan, voiced)
        raw_part_files = [rd / f"raw_part_{pi:02d}.mp4" for pi in range(nwin)]
        for pi in range(nwin):
            part, plan, voiced = part_results[pi]
            _append_part_plan(combined_segs, plan, t_voice)
            t_voice += float(plan.get("duration") or 0)
            all_voiced.extend(voiced)
            part_files.append(part)

    save_json(rd / "voice.json", [
        {"id": v["id"], "text": v.get("text") or "", "duration": v["duration"], "audio": v["audio"]}
        for v in all_voiced
    ])
    combined = {"type": "review", "duration": round(t_voice, 3), "mode": mode, "segments": combined_segs}
    save_json(rd / "edit_plan.json", combined)

    compiled = ensure_layout(project_id) / "cache" / "review_compiled.mp4"
    _note(job_id, "Timeline Editor: ghép phần thô → review_compiled.mp4", stage="timeline", progress=0.86, parts=parts_meta)
    prev_compiled = Path(str((load_json(root / "pipeline.json") or {}).get("compiled") or ""))
    if reuse_tl and compiled.is_file() and compiled.stat().st_size > 0:
        _note(job_id, "Timeline: cache review_compiled.mp4")
    elif reuse_tl and prev_compiled.is_file():
        shutil.copy2(prev_compiled, compiled)
        _note(job_id, "Timeline: lấy compiled lần trước")
    elif len(raw_part_files) == 1:
        with _progress_heartbeat(job_id, "timeline"):
            shutil.copy2(raw_part_files[0], compiled)
    else:
        with _progress_heartbeat(job_id, "timeline"):
            concat_parts(raw_part_files, compiled, job_id=job_id)
    combined["source"] = str(src)
    project_meta = apply_edit_plan(project_id, compiled, combined, settings=settings, voice=voice)
    # raw parts preserve the pre-burn subtitle pixels.  Reuse their measured
    # output-space box for Live Preview instead of the original source-space
    # box or OCRing the already-burned translated timeline.
    compiled_caption_bbox = locate_review_caption_band(raw_part_files[0]) if raw_part_files else None
    _apply_fixed_review_bboxes(
        project_id, project_meta, compiled, settings,
        caption_bbox=compiled_caption_bbox,
    )
    save_json(rd / "project.json", {"projectId": project_id})

    # Always finish the deliverable: concat per-part muxed videos (TTS + cuts)
    # into outputDir. review_compiled.mp4 above stays for optional Editor.
    check_cancel(job_id)
    final_join = ensure_layout(project_id) / "cache" / "review_final.mp4"
    _note(job_id, "Xuất: nối các phần đã TTS + cắt → video hoàn thiện", stage="render", progress=0.94)
    if len(part_files) == 1:
        with _progress_heartbeat(job_id, "export"):
            shutil.copy2(part_files[0], final_join)
    else:
        with _progress_heartbeat(job_id, "export"):
            concat_parts(
                part_files,
                final_join,
                job_id=job_id,
                reencode_fallback=False,
            )
    check_cancel(job_id)
    with _progress_heartbeat(job_id, "export"):
        dest = _copy_output(final_join, str(src), settings, job)
    _note(job_id, f"Xuất: {dest}")
    artifact_run_id = prev_rd.name if reuse_match and prev_rd else run_id
    cache_refs = {"root": str(root), "run": artifact_run_id}
    mutate(job_id, {"checkpoints": list(STAGES), "cacheRefs": cache_refs, "parts": parts_meta})
    _save_pipeline(
        root,
        settings,
        artifact_run_id,
        compiled=str(compiled) if compiled.is_file() else "",
        project_id=project_id,
    )
    set_status(
        project_id,
        step="export",
        progress=100,
        message="",
        running=False,
        error=None,
    )
    return {"output": dest, "projectId": project_id, "cacheRefs": cache_refs}


def _select_review_model(
    review_mode: str,
    review_provider: str,
    requested_model: str,
    available_models: list[str],
    *,
    cloud_model: str = "",
) -> str | None:
    """Resolve exactly the provider selected by the Review writing mode.

    ``reviewProvider`` stays in a draft so users can switch modes without
    losing their Cloud selection. It must not turn an Ollama run into a Cloud
    run when the writing mode is ``llm``.
    """
    if review_mode == "cloud":
        # Cloud Review is selected and persisted with the Review project;
        # `reviewModel` remains reserved for the Ollama selector.
        model_name = cloud_model or {
            "gemini": "gemini-2.5-flash",
            "grok": "grok-3-mini",
            "openai": "gpt-4o-mini",
        }.get(review_provider, "")
        return f"cloud:{review_provider}:{model_name}"
    if review_mode in {"llm", "ai"}:
        if requested_model not in {"", "auto"} and requested_model in available_models:
            return requested_model
        return pick_llm(available_models)
    return None


def _part_export_segments(
    plan: dict[str, Any], video: Path, *, caption_bbox: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Convert one match plan to timing with one stable Review subtitle lane."""
    bbox = dict(caption_bbox or locate_review_caption_band(video))
    out: list[dict[str, Any]] = []
    for index, seg in enumerate(plan.get("segments") or []):
        start = float(seg.get("voice_start") or 0)
        end = float(seg.get("voice_end") or start)
        audio = Path(str(seg.get("audio") or ""))
        text = str(seg.get("text") or "")
        # Duration-filler footage has no synthetic narration or caption. Do
        # not pass an empty path to mux_dub (the project TTS folder itself can
        # otherwise be mistaken for an audio file).
        if not str(seg.get("audio") or "").strip() and not text.strip():
            continue
        out.append({
            "id": str(seg.get("voice_id") or f"voice_{index:03d}"),
            "start": start,
            "end": end,
            "source": "",
            "translation": text,
            "audio": str(audio),
            "audioPath": str(audio),
            "audioFile": audio.name,
            "audioDuration": float(seg.get("audio_duration") or max(0.0, end - start)),
            "ttsSpeed": float(seg.get("tts_speed") or 1.0),
            "bbox": dict(bbox),
            "bboxInherited": True,
            "layout": "horizontal",
        })
    return out


def _review_caption_cues(
    audio_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Each review narration segment displays its exact spoken text for its full duration."""
    cues: list[dict[str, Any]] = []
    for segment in audio_segments:
        text = str(segment.get("translation") or "").strip()
        if not text:
            continue
        cues.append({
            **segment,
            "translation": text,
            "dubSubtitle": text,
        })
    return cues


def _review_cover_lane(video: Path, bbox: dict[str, int]) -> dict[str, Any]:
    """A continuous mask-only cue for Review's fixed old-subtitle lane."""
    duration = max(0.04, float(ffprobe_duration(video) or 0.04))
    return {
        "id": "review-cover-lane",
        "start": 0.0,
        "end": duration,
        "maskOnly": True,
        "bbox": dict(bbox),
        "bboxInherited": True,
        "layout": "horizontal",
    }


def _cap_voiced_duration(
    voiced: list[dict[str, Any]], target_duration: float
) -> list[dict[str, Any]]:
    """Fit narration pacing while keeping speech natural (never rushing beyond 1.20x)."""
    total = sum(max(0.0, float(row.get("duration") or 0)) for row in voiced)
    target = max(1.0, float(target_duration or 0))
    if total <= target + 0.05 or total <= 0:
        return voiced
    scale = target / total
    # Cap max speed to 1.20x (energetic human pace, never rushed/chipmunk)
    speed = min(1.20, max(1.0, 1.0 / scale))
    effective_scale = 1.0 / speed
    fitted: list[dict[str, Any]] = []
    for row in voiced:
        item = dict(row)
        original = max(0.05, float(item.get("duration") or 0.05))
        item["audio_duration"] = original
        item["duration"] = round(original * effective_scale, 3)
        item["ttsSpeed"] = round(speed, 4)
        fitted.append(item)
    return fitted


def _apply_fixed_review_bboxes(
    project_id: str,
    meta: dict[str, Any],
    compiled: Path,
    settings: dict[str, Any],
    *,
    caption_bbox: dict[str, int] | None = None,
) -> None:
    """Persist the same fixed subtitle lane used by Review export and preview.

    Locate always runs on source.mp4 (original hardsub pixels).  OCR can be
    non-deterministic across runs, so we cache the widest result ever found
    in ``settings.subtitleBand`` and use take-wider logic: a new locate
    result can only EXPAND the band, never shrink it.  This means once the
    correct 2-row band is detected, it stays correct for all future previews.
    """
    if not caption_export_settings(settings)["burnSubs"]:
        return

    from pipeline.core.media import video_size
    from pipeline.core.project import ensure_layout

    width, height = video_size(compiled)
    settings_stored = meta.get("settings") or {}

    # --- locate -----------------------------------------------------------
    source = ensure_layout(project_id) / "source.mp4"
    if source.exists():
        new_band = dict(locate_review_caption_band(source))
    else:
        new_band = dict(caption_bbox or locate_review_caption_band(compiled))

    # --- take-wider: merge with cached band --------------------------------
    # OCR is non-deterministic; a weaker run must not shrink a correct band.
    cached = settings_stored.get("subtitleBand") or {}
    if cached and cached.get("h", 0) > 0:
        # Expand to the union of cached and new detect.
        new_top = min(new_band["y"], cached["y"])
        new_bot = max(new_band["y"] + new_band["h"], cached["y"] + cached["h"])
        new_band = {"x": 0, "y": new_top, "w": width, "h": new_bot - new_top}
    # Persist the widest known band for future runs.
    settings_stored["subtitleBand"] = dict(new_band)

    bbox = new_band

    # --- update all segment bboxes ----------------------------------------
    for segment in meta.get("segments") or []:
        segment["bbox"] = dict(bbox)
        segment["bboxInherited"] = True
        segment["layout"] = "horizontal"
    meta["bboxLocateVersion"] = 4

    # --- blur band = full subtitle zone, centered --------------------------
    # Full band: both lines are always covered.  The gaussian kernel
    # radiates outward from the center, so a wide band covers all rows.
    settings_stored["blurBandAutoRegion"] = {
        "x": 0.0,
        "y": round(bbox["y"] / max(1, height), 4),
        "w": 1.0,
        "h": round(bbox["h"] / max(1, height), 4),
    }
    settings_stored["blurBandAutoRegionVersion"] = 1
    if not settings_stored.get("blurBandMode"):
        settings_stored["blurBandMode"] = "auto"
    meta["settings"] = settings_stored

    save_meta(project_id, meta)


def _faithful_story(visuals: list[dict[str, Any]]) -> dict[str, Any]:
    """Chronological source beats for a translation-led Review without an LLM."""
    blocks: list[dict[str, Any]] = []
    for index in range(0, len(visuals), 20):
        chunk = visuals[index : index + 20]
        if not chunk:
            continue
        text = " ".join(
            str(scene.get("transcript") or scene.get("description") or "").strip()
            for scene in chunk
        ).strip()
        blocks.append({
            "scene_ids": [scene.get("scene_id") for scene in chunk],
            "start": float(chunk[0].get("start") or 0),
            "end": float(chunk[-1].get("end") or 0),
            "summary": text[:2400],
            "characters": [],
            "events": [],
            "importance": max(float(scene.get("plot_score") or 0) for scene in chunk),
        })
    events = [
        {
            "event_id": f"evt_{index:03d}",
            "summary": block["summary"],
            "scene_ids": block["scene_ids"],
            "start": block["start"],
            "end": block["end"],
            "importance": block["importance"],
            "spoiler_level": 0,
        }
        for index, block in enumerate(blocks)
    ]
    return {
        "blocks": blocks,
        "chapters": list(blocks),
        "movie_context": {"logline": "", "themes": [], "characters": []},
        "story_graph": {"events": events, "highlights": [], "climax": [], "ending": []},
    }


def _windows(duration: float, mode: str, settings: dict[str, Any]) -> list[tuple[float, float]]:
    duration = max(duration, 1.0)
    if mode != "accumulate":
        return [(0.0, duration)]
    step = max(60.0, float(settings.get("chunkMinutes") or 15) * 60.0)
    # ponytail: cap 40 parts so a 12h file cannot spawn unbounded LLM/TTS loops. Upgrade: stream parts as child jobs.
    full_parts = max(1, int(duration // step))
    remainder = duration - full_parts * step
    part_count = full_parts if 0 < remainder < step * 0.5 else full_parts + bool(remainder > 0.5)
    part_count = min(40, int(part_count))
    balanced = duration / part_count
    return [
        (round(index * balanced, 6), round(duration if index == part_count - 1 else (index + 1) * balanced, 6))
        for index in range(part_count)
    ]


def _accumulate_worker_limits(part_count: int) -> tuple[int, int, int]:
    """One Review window at a time; inner TTS/FFmpeg use adaptive ceilings."""
    # ponytail: outer=1 avoids multi-part disk thrash; inner caps are ceilings
    # for run_with_adaptive_workers (requested=0), not fixed thread counts.
    del part_count
    import os
    cores = max(1, os.cpu_count() or 4)
    # VideoToolbox/NVENC clip jobs are GPU-bound and short — more workers = faster.
    compose_cap = max(12, min(24, int(cores * 0.90)))
    return 1, 24, compose_cap


def _script_settings_key(settings: dict[str, Any]) -> str:
    """Stable fingerprint of settings that affect script / TTS / matching output.
    Any change here forces a rebuild from script stage onward."""
    keys = (
        "style", "scriptStyle", "narration", "reviewMode", "reviewModel", "reviewCloudModel", "reviewProvider",
        "buildMode", "genre", "pausePace", "voice", "language", "spoiler",
        "notes", "durationSec", "chunkMinutes", "keepSec", "skipSec",
        "reviewPlanVersion",
    )
    return "|".join(f"{k}={_norm_setting(settings.get(k))}" for k in keys)


def _part_cache_matches(
    script: object,
    target_duration: float,
    *,
    source_start: float | None = None,
    source_end: float | None = None,
    settings_key: str | None = None,
) -> bool:
    """Reject old plans while preserving finalized interrupted work."""
    if not isinstance(script, dict) or script.get("reviewPlanVersion") != REVIEW_PLAN_VERSION:
        return False
    # Settings fingerprint check: if key is provided and differs, must rebuild.
    if settings_key and script.get("settingsKey") and script.get("settingsKey") != settings_key:
        return False
    try:
        cached_target = float(script.get("targetDurationSec") or 0)
    except (TypeError, ValueError):
        return False
    if not (cached_target > 0 and abs(cached_target - target_duration) <= 0.05):
        return False
    if source_start is None or source_end is None:
        return True
    try:
        return (
            script.get("windowSourceVersion") == WINDOW_SOURCE_VERSION
            and abs(float(script.get("sourceStart")) - source_start) <= 0.05
            and abs(float(script.get("sourceEnd")) - source_end) <= 0.05
        )
    except (TypeError, ValueError):
        return False


def _finalize_key(settings: dict[str, Any]) -> str:
    keys = (
        "reviewPlanVersion",
        "subtitle",
        "captionMode",
        "originalAudioPct",
        "subtitleFontSize",
        "subtitleFontFamily",
        "coverMaskStyle",
        "coverMaskColor",
        "coverMaskOpacity",
        "captionTextColor",
        "captionBgStyle",
        "captionBgColor",
        "captionBgOpacity",
        "captionStroke",
        "quality",
    )
    settings_key = "|".join(f"{key}={_norm_setting(settings.get(key))}" for key in keys)
    return f"finalizeVersion={REVIEW_FINALIZE_VERSION}|{settings_key}"


def _media_artifact_ok(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and ffprobe_duration(path) > 0.05
    except (OSError, RuntimeError, ValueError):
        return False


def _materialize_window(src: Path, cache_dir: Path, start: float, end: float, job_id: str) -> Path:
    """Create a locally timed source so every window can be analyzed alone."""
    duration = max(0.01, end - start)
    source = cache_dir / "source.mp4"
    manifest = cache_dir / "source.json"
    expected = {
        "version": WINDOW_SOURCE_VERSION,
        "source": str(src.resolve()),
        "start": round(start, 3),
        "end": round(end, 3),
    }
    cached = load_json(manifest) or {}
    if cached == expected and _media_artifact_ok(source):
        actual = ffprobe_duration(source)
        if abs(actual - duration) <= max(1.0, duration * 0.02):
            return source

    cache_dir.mkdir(parents=True, exist_ok=True)
    source.unlink(missing_ok=True)
    # Accurate re-encode is required: stream-copy starts at a keyframe and
    # leaves local scene/transcript timestamps shifted.
    run_cmd(job_id, [
        _ff_bin("ffmpeg"), "-y", "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
        str(source),
    ])
    if not _media_artifact_ok(source):
        raise RuntimeError(f"REVIEW_WINDOW_CUT_FAILED:{start:.3f}-{end:.3f}")
    actual = ffprobe_duration(source)
    if abs(actual - duration) > max(1.0, duration * 0.02):
        source.unlink(missing_ok=True)
        raise RuntimeError(f"REVIEW_WINDOW_CUT_DURATION_INVALID:{start:.3f}-{end:.3f}")
    save_json(manifest, expected)
    return source


def _script_duration(mode: str, w0: float, w1: float, duration: float, settings: dict[str, Any], factor: float) -> float:
    if mode == "accumulate":
        # `durationSec` is the total review length. Source windows only make
        # the work resumable/parallel-friendly; each gets its proportional
        # share so concatenating them does not recreate the whole movie.
        target = max(30.0, float(settings.get("durationSec") or 900))
        share = max(0.0, w1 - w0) / max(1.0, duration)
        return target * share
    if mode == "fixed":
        return max(30.0, float(settings.get("durationSec") or 900) * factor)
    if mode == "smart":
        keep = max(0.4, float(settings.get("keepSec") or 4))
        skip = max(0.0, float(settings.get("skipSec") or 10))
        return max(30.0, duration * (keep / (keep + skip)) * factor)
    return max(30.0, float(settings.get("durationSec") or 900) * factor)


def _slice_story(story: dict[str, Any], visuals: list[dict[str, Any]]) -> dict[str, Any]:
    if not visuals:
        return story
    ids = {int(v["scene_id"]) for v in visuals if v.get("scene_id") is not None}
    t0 = min(float(v.get("start") or 0) for v in visuals)
    t1 = max(float(v.get("end") or 0) for v in visuals)

    def overlap(item: dict[str, Any]) -> bool:
        s0, s1 = float(item.get("start") or 0), float(item.get("end") or 0)
        if s1 > s0:
            return s1 > t0 and s0 < t1
        scene_ids = {int(x) for x in (item.get("scene_ids") or []) if str(x).isdigit() or isinstance(x, int)}
        return bool(scene_ids & ids)

    graph = dict(story.get("story_graph") or {})
    graph["acts"] = [a for a in (graph.get("acts") or []) if overlap(a)]
    graph["events"] = [
        ev for ev in (graph.get("events") or [])
        if overlap(ev) or {int(x) for x in (ev.get("scene_ids") or []) if str(x).isdigit() or isinstance(x, int)} & ids
    ]
    return {
        **story,
        "blocks": [b for b in (story.get("blocks") or []) if overlap(b)],
        "chapters": [c for c in (story.get("chapters") or []) if overlap(c)],
        "story_graph": graph,
    }


def _slice_visuals(visuals: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    out = []
    for scene in visuals:
        s0 = float(scene.get("start") or 0)
        s1 = float(scene.get("end") or 0)
        if s1 <= start or s0 >= end:
            continue
        item = dict(scene)
        item["start"] = max(s0, start)
        item["end"] = min(s1, end)
        item["duration"] = round(item["end"] - item["start"], 3)
        out.append(item)
    return out


def _visuals_for_match(
    visuals: list[dict[str, Any]], start: float, end: float,
) -> list[dict[str, Any]]:
    """Guarantee one source-timeline clip when scene indexing produced nothing.

    The fallback is intentionally restricted to the current Review window, so
    cumulative parts never compose footage from a different part of the film.
    """
    if visuals:
        return visuals
    source_start = max(0.0, float(start or 0.0))
    source_end = max(source_start + 0.12, float(end or source_start + 0.12))
    return [{
        "scene_id": -1,
        "start": round(source_start, 3),
        "end": round(source_end, 3),
        "duration": round(source_end - source_start, 3),
        "description": "",
        "transcript": "",
        "plot_score": 0.0,
        "visual_score": 0.0,
        "emotion_score": 0.0,
        "spoiler_score": 0.0,
    }]


def _append_part_plan(combined: list[dict[str, Any]], plan: dict[str, Any], offset: float) -> None:
    for seg in plan.get("segments") or []:
        item = dict(seg)
        item["voice_start"] = round(float(seg.get("voice_start") or 0) + offset, 3)
        item["voice_end"] = round(float(seg.get("voice_end") or 0) + offset, 3)
        combined.append(item)


def _part_durations(segments: list[dict[str, Any]], count: int) -> list[float | None]:
    starts: list[float | None] = [None] * count
    final_end = 0.0
    for seg in segments:
        match = re.match(r"p(\d+)_", str(seg.get("voice_id") or ""))
        if not match:
            continue
        index = int(match.group(1))
        if index >= count:
            continue
        start = float(seg.get("voice_start") or 0)
        starts[index] = start if starts[index] is None else min(float(starts[index]), start)
        final_end = max(final_end, float(seg.get("voice_end") or start))
    durations: list[float | None] = [None] * count
    for index, start in enumerate(starts):
        if start is None:
            continue
        next_start = next((float(value) for value in starts[index + 1:] if value is not None), final_end)
        durations[index] = max(0.0, next_start - float(start))
    return durations


def _refresh_part_timeline(parts: list[dict[str, Any]], *, factor: float = 1.0) -> None:
    cursor = 0.0
    for part in parts:
        source_duration = max(1.0, float(part.get("sourceEnd") or 0) - float(part.get("sourceStart") or 0))
        duration = float(
            part.get("outputDuration")
            or part.get("expectedOutputDuration")
            or source_duration * factor
        )
        part["start"] = round(cursor, 3)
        cursor += max(1.0, duration)
        part["end"] = round(cursor, 3)
        part["label"] = _fmt_range(part["start"], part["end"])


def _fmt_range(start: float, end: float) -> str:
    return f"{_mmss(start)} - {_mmss(end)}"


def _mmss(t: float) -> str:
    t = max(0, int(float(t or 0)))
    return f"{t // 60:02d}:{t % 60:02d}"


def _clip(text: Any, n: int = 180) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _note(job_id: str, msg: str, **patch: Any) -> None:
    mutate(job_id, patch, log=msg)


def _cached_or(path: Path, fn, job_id: str, stage: str, progress: float, label: str = ""):
    tag = label or stage
    cached = load_json(path)
    if cached:
        _note(job_id, f"{tag}: cache ({_qty(cached)}) — {path.name}", stage=stage, progress=progress)
        return cached
    _note(job_id, f"{tag}: đang chạy…", stage=stage, progress=progress)
    with _progress_heartbeat(job_id, stage):
        data = fn()
    save_json(path, data)
    _note(job_id, f"{tag}: xong ({_qty(data)})")
    return data


def _qty(data: Any) -> str:
    if isinstance(data, list):
        return f"{len(data)} mục"
    if isinstance(data, dict):
        return f"{len(data)} khóa"
    return "ok"


def invalidate_from(settings_changed: set[str]) -> str:
    order = list(STAGES)
    earliest = order[-1]
    for key in settings_changed:
        stage = INVALIDATE_FROM.get(key)
        if stage and order.index(stage) < order.index(earliest):
            earliest = stage
    return earliest


def _reuse(start: str, stage: str) -> bool:
    return STAGES.index(start) > STAGES.index(stage)


def _norm_setting(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    return str(value).strip()


def _settings_diff(prev: dict[str, Any], cur: dict[str, Any]) -> set[str]:
    return {key for key in INVALIDATE_FROM if _norm_setting(prev.get(key)) != _norm_setting(cur.get(key))}


def _resume_point(root: Path, settings: dict[str, Any]) -> tuple[str, Path | None, set[str]]:
    prev = load_json(root / "pipeline.json") or {}
    run = str(prev.get("run") or "")
    prev_rd = root / "runs" / run if run else None
    prev_settings = dict(prev.get("settings") or {})
    if not prev_rd or not prev_rd.is_dir():
        runs = sorted(
            (p for p in (root / "runs").glob("*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        prev_rd = runs[0] if runs else None
        if prev_rd:
            prev_settings = load_json(prev_rd / "settings.json") or prev_settings
    if not prev_rd:
        return "metadata", None, set()
    if not prev_settings:
        return "script", prev_rd, set()
    changed = _settings_diff(prev_settings, settings)
    return invalidate_from(changed), prev_rd, changed


def _resume_log(start: str, changed: set[str], **reuse: bool) -> str:
    names = {
        "script": "kịch bản",
        "tts": "TTS / giọng",
        "matching": "ghép hình",
        "timeline": "timeline",
        "render": "xuất video",
        "metadata": "đầu pipeline",
        "transcript": "transcript",
        "story_graph": "cốt truyện",
    }
    keys = ", ".join(sorted(changed)) if changed else "không đổi cài đặt"
    kept = []
    if reuse.get("reuse_script"):
        kept.append("kịch bản")
    if reuse.get("reuse_tts"):
        kept.append("giọng")
    if reuse.get("reuse_match"):
        kept.append("ghép hình")
    if reuse.get("reuse_tl"):
        kept.append("compiled")
    extra = f" · giữ {', '.join(kept)}" if kept else ""
    return f"Cache: làm lại từ {names.get(start, start)} · {keys}{extra}"


def _save_pipeline(root: Path, settings: dict[str, Any], run_id: str, *, compiled: str, project_id: str) -> None:
    save_json(root / "pipeline.json", {
        "settings": {key: settings.get(key) for key in INVALIDATE_FROM},
        "run": run_id,
        "compiled": compiled,
        "projectId": project_id,
    })


def _load_scripts(prev_rd: Path | None, nwin: int) -> list[dict[str, Any]] | None:
    if not prev_rd or nwin < 1:
        return None
    rows = [load_json(prev_rd / f"script_{i:02d}.json") for i in range(nwin)]
    if all(isinstance(s, dict) and (s.get("segments") or []) for s in rows):
        return rows  # type: ignore[return-value]
    return _scripts_from_plan(load_json(prev_rd / "edit_plan.json") or {}, nwin)


def _scripts_from_plan(plan: dict[str, Any], nwin: int) -> list[dict[str, Any]] | None:
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(nwin)]
    for seg in plan.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        vid = str(seg.get("voice_id") or "")
        if len(vid) < 5 or vid[0] != "p" or vid[3] != "_":
            return None
        try:
            pi = int(vid[1:3])
        except ValueError:
            return None
        if pi < 0 or pi >= nwin:
            return None
        buckets[pi].append({
            "id": vid[4:],
            "text": str(seg.get("text") or ""),
            "purpose": "body",
            "visual_intent": "",
            "character_refs": [],
            "event_refs": [],
            "preferred_scene_ids": [],
        })
    if any(not bucket for bucket in buckets):
        return None
    return [{"segments": bucket} for bucket in buckets]


def _attach_prev_voice(script: dict[str, Any], prev_voice: Any, pi: int) -> list[dict[str, Any]] | None:
    by_id = {str(v.get("id")): v for v in (prev_voice or []) if isinstance(v, dict)}
    out: list[dict[str, Any]] = []
    for seg in script.get("segments") or []:
        vid = f"p{pi:02d}_{seg['id']}"
        prev = by_id.get(vid)
        wav = Path(str((prev or {}).get("audio") or ""))
        if (
            not prev
            or str(prev.get("text") or "") != str(seg.get("text") or "")
            or not wav.is_file()
        ):
            return None
        out.append({**seg, "id": vid, "duration": float(prev.get("duration") or 0) or 2.5, "audio": str(wav)})
    return out


def _voice_wavs_ok(scripts: list[dict[str, Any]], prev_voice: Any) -> bool:
    return all(_attach_prev_voice(script, prev_voice, i) for i, script in enumerate(scripts))


def _parts_complete(prev_rd: Path | None, nwin: int, finalize_key: str) -> bool:
    if not prev_rd:
        return False
    for index in range(nwin):
        script = load_json(prev_rd / f"script_{index:02d}.json") or {}
        plan = load_json(prev_rd / f"plan_{index:02d}.json") or {}
        voice = load_json(prev_rd / f"voice_{index:02d}.json") or []
        final = load_json(prev_rd / f"final_{index:02d}.json") or {}
        if (
            script.get("reviewPlanVersion") != REVIEW_PLAN_VERSION
            or not plan.get("segments")
            or not isinstance(voice, list)
            or not voice
            or final.get("reviewPlanVersion") != REVIEW_PLAN_VERSION
            or final.get("finalizeKey") != finalize_key
            or not _media_artifact_ok(prev_rd / f"raw_part_{index:02d}.mp4")
            or not _media_artifact_ok(prev_rd / f"part_{index:02d}.mp4")
        ):
            return False
    return True


def _tts_parallel(
    segs: list[dict[str, Any]],
    *,
    voice: str,
    tts_dir: Path,
    pi: int,
    lang: str,
    job_id: str,
    project_id: str,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    from pipeline.core.accel import tts_local_workers
    from pipeline.core.jobs import set_job_context
    from pipeline.core.resources import adaptive_workers, progress_msg, run_with_adaptive_workers, workers_label
    from pipeline.tts.engines import vieneu as _vieneu

    pending = [
        {
            "i": i,
            "seg": seg,
            "text": str(seg.get("text") or " "),
            "wav": tts_dir / f"p{pi:02d}_{seg['id']}_{hashlib.md5(str(seg.get('text') or ' ').strip().encode('utf-8')).hexdigest()[:8]}.wav",
        }
        for i, seg in enumerate(segs)
    ]
    if not pending:
        return []
    local = bool(_vieneu.parse_voice(voice))
    kind = "tts" if local else "network"
    if local:
        from pipeline.core.accel import _tts_vram_hard_cap
        hard = _tts_vram_hard_cap()
        w0 = tts_local_workers(max_workers, tasks=len(pending))
    else:
        hard = 24
        w0 = adaptive_workers(max_workers, kind="network", cap=hard, tasks=len(pending))
    cap = min(hard, max(1, int(max_workers or hard)), len(pending))
    _note(job_id, f"TTS phần {pi + 1}: {len(pending)} đoạn{workers_label(w0, kind=kind)}")

    def one(job: dict[str, Any]) -> tuple[dict[str, Any], float]:
        set_job_context(job_id)
        check_cancel(job_id)
        dur = tts_segment(job["text"], voice, job["wav"], None, "none", lang=lang)
        return job, float(dur or 0) or 2.5

    def prog(cur: int, total: int, w_now: int) -> None:
        pct = int(cur / max(1, total) * 100)
        _note(
            job_id,
            f"TTS phần {pi + 1}: {cur}/{total} đoạn ({pct}%) · {w_now} luồng",
            stage=f"TTS {cur}/{total}",
            progress=0.70 + 0.14 * (cur / max(1, total)),
        )

    rows = run_with_adaptive_workers(
        pending,
        one,
        kind=kind,
        # Cap is the ceiling; requested=0 lets idle CPU/network raise concurrency.
        requested=0 if max_workers else max_workers,
        cap=cap,
        thread_name_prefix="rv-tts",
        on_progress=prog,
        cancel_check=lambda: check_cancel(job_id),
    )
    by_i: dict[int, tuple[dict[str, Any], float]] = {}
    for row in rows:
        if not row:
            continue
        job, dur = row
        by_i[int(job["i"])] = (job, dur)
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(segs):
        job, dur = by_i[i]
        out.append({**seg, "id": f"p{pi:02d}_{seg['id']}", "duration": dur, "audio": str(job["wav"])})
    return out
