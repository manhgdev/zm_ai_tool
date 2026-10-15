"""Output paths, file validation, and Flow logo removal."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from pipeline.core.output_paths import safe_output_part
from pipeline.core.output_paths import selected_or_default

from . import store
from .constants import POST_PROGRESS_END, POST_PROGRESS_START


def _selected_flow_folder(selected: Path, kind: str) -> Path:
    """Map a picker path to one concrete Flow kind folder.

    Desktop pickers return the actual absolute directory. Keep that path
    intact, adding the image/video segment exactly once for the canonical
    ``.../flow`` layout instead of silently remapping it to the default home
    directory.
    """
    parts = selected.parts
    lowered = [part.casefold() for part in parts]
    kind_lower = kind.casefold()
    for index, part in enumerate(lowered):
        if part != "flow" or index + 1 >= len(parts):
            continue
        next_part = lowered[index + 1]
        flow_root = Path(*parts[: index + 1])
        tail = parts[index + 2 :]
        if next_part in {"image", "video"}:
            if next_part == kind_lower:
                return selected
            return flow_root / kind / Path(*tail) if tail else flow_root / kind
        break
    if selected.name.casefold() == "flow":
        return selected / kind
    if selected.parent.name.casefold() == "flow":
        return selected.parent / kind / selected.name
    return selected / kind


class FlowOutputsMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    def _migrate_legacy_kind_output_folder(self, job: dict[str, Any]) -> dict[str, Any]:
        """Move legacy flat Flow outputs into ``flow/<kind>/<user-name>``."""
        settings = job.get("settings")
        kind = str(job.get("kind") or "")
        if not isinstance(settings, dict) or kind not in {"video", "image"}:
            return job
        original = str(settings.get("outputDir") or "").strip()
        normalized = original.replace("\\", "/").rstrip("/")
        suffixes = (f"/{kind}", f"-{kind}")
        base = next((normalized[: -len(suffix)] for suffix in suffixes if normalized.endswith(suffix)), normalized)
        if not base:
            return job
        migrated = dict(job)
        migrated_settings = dict(settings)
        migrated_settings["outputDir"] = base
        migrated["settings"] = migrated_settings
        target_folder = self._output_folder(migrated)
        migrated_outputs = []
        source_folders: set[Path] = set()
        for raw_output in job.get("outputs") or []:
            output = Path(str(raw_output))
            target = target_folder / output.name
            if output != target and output.is_file():
                source_folders.add(output.parent)
                if not target.exists():
                    shutil.move(str(output), str(target))
            migrated_outputs.append(str(target if target.exists() else output))
        for source_folder in source_folders:
            try:
                source_folder.rmdir()
            except OSError:
                pass
        migrated["outputs"] = migrated_outputs
        migrated["outputFolder"] = str(target_folder)
        if (
            original != base
            or migrated_outputs != list(job.get("outputs") or [])
            or (bool(job.get("outputFolder")) and str(job.get("outputFolder")) != str(target_folder))
        ):
            store.patch_row("jobs", str(job.get("id") or ""), {
                "settings": migrated_settings,
                "outputs": migrated_outputs,
                "outputFolder": str(target_folder),
            })
        return migrated

    async def _remove_flow_logos(self, job_id: str, outputs: list[str]) -> str:
        """Remove the Flow logo from downloaded media; return "" on success or the failure text.

        Files already cleaned (``logoDone``) are skipped, so a retry never inpaints twice.
        """
        store.patch_row("jobs", job_id, {"stage": "removing_logo", "outputs": outputs, "updatedAt": time.time()})
        try:
            import importlib
            logo_mod = await asyncio.to_thread(importlib.import_module, "pipeline.logo_remover")
            cfg = logo_mod.LogoRemovalSettings(
                enabled=True, keep_original=False, mode="auto", lama_model_path=logo_mod.DEFAULT_LAMA_MODEL)
        except Exception as exc:  # thiếu/cài lỗi runtime
            self._log("warning", "logo_remove_failed", job_id=job_id, message=str(exc))
            return str(exc) or exc.__class__.__name__
        done = [str(p) for p in (store.get_row("jobs", job_id) or {}).get("logoDone") or []]
        todo = [p for p in outputs if str(p) not in done]
        errors: list[str] = []

        def report(index: int, fraction: float) -> None:
            store.patch_row("jobs", job_id, {
                "progress": round(POST_PROGRESS_START + (POST_PROGRESS_END - POST_PROGRESS_START) * (index + fraction) / len(todo), 1),
                "updatedAt": time.time(),
            })

        if todo:
            report(0, 0)
        for index, out_path in enumerate(todo):
            try:
                if Path(out_path).suffix.lower() == ".mp4":
                    from pipeline.cleaner.cleaner_ffmpeg import _inpaint_video
                    target = Path(out_path)
                    temporary = target.with_name(target.stem + ".logo_tmp.mp4")
                    try:
                        await asyncio.to_thread(
                            _inpaint_video, out_path, str(temporary), job_id, flow_logo=True,
                            on_progress=lambda f, i=index: report(i, f),
                        )
                        self._check_cancel(job_id)
                        error = self._output_validation_error([str(temporary)])
                        if error:
                            raise RuntimeError(error)
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    mode_used = "static_video_alpha_recovery"
                else:
                    result = await asyncio.to_thread(logo_mod.remove_logo_from_file, out_path, cfg)
                    mode_used = result.mode_used
                self._log("info", "logo_removed", job_id=job_id, message=mode_used)
                done.append(str(out_path))
                store.patch_row("jobs", job_id, {"logoDone": done})
                report(index, 1)
            except Exception as exc:
                self._log("warning", "logo_remove_failed", job_id=job_id, message=str(exc))
                errors.append(str(exc) or exc.__class__.__name__)
        return errors[0] if errors else ""

    def _fail_logo_removal(self, job_id: str, outputs: list[str], error: str, *, account_id: str = "") -> None:
        """Downloaded files stay on the job; Retry re-runs only the logo step (see retry())."""
        current = store.get_row("jobs", job_id) or {}
        if current.get("status") == "cancelled" or job_id in self._cancelled:
            return
        store.patch_row("jobs", job_id, {
            "status": "failed", "stage": "failed", "progress": 100, "outputs": outputs,
            "error": f"FLOW_LOGO_REMOVE_FAILED: {error[:300]}", "updatedAt": time.time(),
        })
        self._log("error", "job_logo_failed", job_id=job_id, account_id=account_id, message=error)

    async def _logo_only_retry(self, job_id: str) -> None:
        job = store.get_row("jobs", job_id) or {}
        outputs = [str(p) for p in job.get("outputs") or []]
        error = await self._remove_flow_logos(job_id, outputs)
        if error:
            self._fail_logo_removal(job_id, outputs, error, account_id=str(job.get("accountId") or ""))
            return
        current = store.get_row("jobs", job_id) or job
        settings = dict(current.get("settings") or {})
        settings["removeLogo"] = True
        store.patch_row("jobs", job_id, {
            "status": "done", "stage": "done", "progress": 100, "error": None,
            "settings": settings, "updatedAt": time.time(),
        })
        if job.get("seriesContext"):
            from . import series
            series.mark_job_complete(job, outputs)
        self._log("success", "job_completed", job_id=job_id, account_id=str(job.get("accountId") or ""), details={"outputCount": len(outputs)})

    @staticmethod
    def _outputs_exist(outputs: Any) -> bool:
        """Return true only when every declared output is a readable non-empty file.

        A Flow job must never become ``done`` merely because a media id or a
        download URL was returned.  The final file check is deliberately kept
        at the service boundary so it covers both image and video workers.
        """
        paths = [Path(str(value)) for value in (outputs or []) if str(value).strip()]
        if not paths:
            return False
        try:
            return all(path.is_file() and path.stat().st_size > 0 for path in paths)
        except OSError:
            return False

    def _outputs_ready_for_completion(self, job: dict[str, Any], *, account_plan: str = "") -> bool:
        """Return true when downloaded files are safe to expose as a completed job.

        A stale worker may finish after a retry has changed the row token.  The
        files and logo markers are the durable result in that case; do not put
        an already-complete job back into the queue.
        """
        outputs = [str(value) for value in (job.get("outputs") or []) if str(value).strip()]
        if not self._outputs_exist(outputs):
            return False
        settings = job.get("settings") or {}
        if not (settings.get("removeLogo") and account_plan != "Ultra"):
            return True
        done = {str(value) for value in (job.get("logoDone") or [])}
        return bool(outputs) and all(path in done for path in outputs)

    def _finalize_existing_outputs(self, job_id: str, job: dict[str, Any], *, account_id: str = "", account_plan: str = "") -> None:
        if not self._outputs_ready_for_completion(job, account_plan=account_plan):
            return
        outputs = list(job.get("outputs") or [])
        store.patch_row("jobs", job_id, {
            "status": "done", "stage": "done", "progress": 100,
            "outputs": outputs, "error": None, "updatedAt": time.time(),
        })
        if job.get("seriesContext"):
            from . import series
            series.mark_job_complete(job, outputs)
        self._log("success", "job_completed", job_id=job_id, account_id=account_id,
                  details={"outputCount": len(outputs), "recovered": True})

    @classmethod
    def _output_validation_error(cls, outputs: Any) -> str | None:
        paths = [Path(str(value)) for value in (outputs or []) if str(value).strip()]
        if not paths:
            return "FLOW_EMPTY_OUTPUT: no output file was downloaded"
        for path in paths:
            try:
                if not path.is_file():
                    return f"FLOW_OUTPUT_MISSING: {path}"
                if path.stat().st_size <= 0:
                    return f"FLOW_OUTPUT_EMPTY: {path}"
            except OSError as exc:
                return f"FLOW_OUTPUT_UNREADABLE: {path}: {exc}"
        return None

    @staticmethod
    def _image_ratio_validation_error(outputs: Any, ratio: str) -> str | None:
        """Reject a Flow image whose downloaded dimensions do not match its job."""
        expected = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4}.get(str(ratio or "").strip())
        if expected is None:
            return None
        try:
            from pipeline.core.runtime_site import ensure_cv2
            import numpy as np
            cv2 = ensure_cv2()
            for value in outputs or []:
                path = Path(str(value))
                image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                if image is None or image.shape[1] <= 0 or image.shape[0] <= 0:
                    return f"FLOW_IMAGE_RATIO_INVALID: {path}"
                actual = image.shape[1] / image.shape[0]
                if abs(actual - expected) / expected > 0.04:
                    return f"FLOW_IMAGE_RATIO_MISMATCH: expected {ratio}, got {image.shape[1]}x{image.shape[0]}"
        except Exception as exc:
            return f"FLOW_IMAGE_RATIO_CHECK_FAILED: {exc}"
        return None

    def _output_folder(self, job: dict[str, Any], *, create: bool = True) -> Path:
        """Return ``flow/<kind>/<user-name>`` without hidden job folders."""
        settings = job.get("settings") or {}
        selected = Path(str(settings.get("outputDir") or "results")).expanduser()
        kind = safe_output_part(job.get("kind") or "video", "video")
        flow_tab = f"flow-{kind}"  # → ~/Downloads/ZM_AI_TOOL/flow/video/ or .../flow/image/
        series_context = job.get("seriesContext") or {}
        if series_context:
            # Series artifacts share one ``flow/series/<slug>`` namespace and
            # remain split by kind below it.  This keeps deletion/reveal paths
            # compatible with existing Series data while regular jobs use the
            # ``flow/image`` and ``flow/video`` roots above.
            root = selected_or_default("flow", "")
            series_root = root / "series" / safe_output_part(series_context.get("seriesSlug") or series_context.get("seriesTitle") or "series", "series") / kind
            folder = series_root / "anchors" if series_context.get("artifact") == "anchor" else series_root / f"tap-{int(series_context.get('episodeIndex') or 1):02d}"
        elif selected.is_absolute() and os.environ.get("ZM_AI_TOOL_DESKTOP") == "1":
            folder = _selected_flow_folder(selected, kind)

        elif selected.is_absolute():
            # Browser jobs are downloaded from the backend's public tree; do
            # not let a client-supplied absolute path escape that sandbox.
            root = selected_or_default(flow_tab, "")
            folder = root / safe_output_part(selected.name or "results", "results")

        else:
            root = selected_or_default(flow_tab, "")
            folder = root / safe_output_part(selected, "results")
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _display_output_folder(self, job: dict[str, Any]) -> Path:
        """Return the real worker destination shown in the queue UI."""
        return self._output_folder(job, create=False)

    def _output_path(self, job: dict[str, Any], output_index: int, suffix: str) -> Path:
        folder = self._output_folder(job)
        settings = job.get("settings") or {}
        prefix = safe_output_part(settings.get("filePrefix") or job["kind"], str(job["kind"]))
        safe_suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix).lower() or ("mp4" if job["kind"] == "video" else "png")
        series_context = job.get("seriesContext") or {}
        if series_context and series_context.get("sceneIndex"):
            scene_idx = int(series_context.get("sceneIndex") or 1)
            variant = f"_{output_index:02d}" if max(1, int(settings.get("count", 1))) > 1 else ""
            return folder / f"canh-{scene_idx:03d}__{job['id'][:8]}__{prefix}{variant}.{safe_suffix}"
        input_index = int(job["inputIndex"])
        variant = f"_{output_index:02d}" if max(1, int(settings.get("count", 1))) > 1 else ""
        return folder / f"{input_index:03d}__{job['id']}__{prefix}_{input_index:03d}{variant}.{safe_suffix}"
