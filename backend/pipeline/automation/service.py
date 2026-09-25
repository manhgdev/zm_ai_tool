from __future__ import annotations

import re
import json
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from pipeline.core.app_config import load_app_config
from pipeline.core.output_paths import downloads_folder, safe_output_part, selected_or_default
from .prompts import audio_first_prompt
from .store import AutomationStore


_TOPIC_PROMPT_VERSION = "audio-first-2d-v2"


def clean_subtitle_text(path: Path) -> str:
    """Extract clean spoken dialogue text from an SRT or VTT file."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    clean_lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        if "-->" in line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not clean_lines or clean_lines[-1] != line):
            clean_lines.append(line)
    return " ".join(clean_lines)


class AutomationCancelled(RuntimeError):
    pass


class AutomationService:
    """Durable multi-job queue with stage-level pause/retry controls."""

    def __init__(
        self,
        *,
        store: AutomationStore | None = None,
        runner: Callable[[str], None] | None = None,
        max_workers: int = 2,
    ):
        self.store = store or AutomationStore()
        self.settings_path = self.store.db_path.parent / "settings.json"
        self._runner = runner or self._run_pipeline
        self._executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8)), thread_name_prefix="automation")
        self._futures: dict[str, Future[Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._deleted_jobs: set[str] = set()
        self._lock = threading.RLock()
        self._chat_gate = threading.Lock()

    def get_settings(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "language": "vi",
            "textProvider": "openrouter",
            "textModel": "openrouter/free",
            "chatModel": "GPT-5.6 Sol",
            "systemPrompt": "",
            "promptEngine": "vi",
            "scriptBrief": {
                "niche": "",
                "audience": "",
                "character": "",
                "durationMinutes": 8,
                "videoType": "educational",
                "tone": "Tự nhiên, sắc bén, dễ nghe",
                "platform": "YouTube",
                "primaryGoal": "watch_time",
                "commonMistake": "",
            },
            "tts": {"voice": "system", "speed": 1.0, "volume": 1.0, "pitch": 0.0, "style": "tu_nhien"},
            "flow": {"accountId": "", "model": "Nano Banana 2", "ratio": "16:9", "resolution": "1K", "concurrency": "3", "promptEngine": "vi"},
            "compose": {
                "resolution": "auto", "targetPlatform": "auto", "fps": 30, "crf": 20, "encoder": "auto",
                "effect": "none", "transitionDuration": 0.28, "zoom": "off", "speed": 100, "volume": 100, "previewSeconds": 0,
                "allowMissingMedia": False, "subtitleEnabled": True, "removeMetadata": False,
                "subtitleFontFamily": "system", "subtitleSize": 8, "subtitleOffset": 0, "subtitleMargin": 34,
                "subtitleBackground": "solid", "subtitleColor": "#ffffff", "subtitleBgColor": "#000000", "subtitleOpacity": 55,
                "drawingEnabled": False, "drawingMode": "hand", "drawingTool": "pen", "drawingDetail": 72, "drawingThickness": 2, "drawingStrokeOrder": "natural",
                "delogoEnabled": False, "delogoAuto": True, "delogoX": 80, "delogoY": 82, "delogoW": 18, "delogoH": 12,
                "logoEnabled": False, "logoSource": "text", "logoText": "ZM AI TOOL", "logoIcon": "★", "logoFontSize": 32, "logoColor": "#ffffff", "logoSize": 8, "logoOpacity": 85, "logoX": 88, "logoY": 88, "logoMotion": "fixed", "logoScope": "full", "logoStart": 0, "logoEnd": 10, "logoVisibleSec": 4, "logoHiddenSec": 2, "logoFadeSec": 0.5, "logoSafeMargin": 4,
            },
            "outputDir": "",
        }
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                clean = self._public_settings(saved)
                if "textProvider" not in clean and clean.get("chatModel"):
                    clean["textProvider"] = "chatgpt_web"
                    clean["textModel"] = str(clean["chatModel"])
                return {**defaults, **clean}
        except (OSError, ValueError, TypeError):
            pass
        return defaults

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        clean = self._public_settings(dict(settings or {}))
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings_path.with_suffix(".tmp")
        temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.settings_path)
        return self.get_settings()

    def start(self) -> None:
        """Resume queued work and jobs interrupted by a previous app restart."""
        for job in self.store.list_jobs():
            if job["status"] == "queued":
                self.start_job(job["id"])
            elif job["status"] == "interrupted" and (job.get("error") or {}).get("code") == "AUTOMATION_INTERRUPTED":
                self.store.update_job(job["id"], status="queued", error=None)
                self.start_job(job["id"])

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def create_job(self, input_mode: str, title: str, settings: dict[str, Any], input_data: dict[str, Any] | None = None) -> dict[str, Any]:
        mode = str(input_mode or "").strip().lower()
        if mode not in {"topic", "ai_topic", "youtube", "script", "bundle"}:
            raise ValueError("AUTOMATION_INPUT_MODE_INVALID")
        clean_settings = self._public_settings(settings or {})
        # Persist the provider/model decision with the job. This keeps retries
        # deterministic and prevents a later global setting change from
        # silently switching an already queued job to another provider.
        if not clean_settings.get("textProvider"):
            clean_settings["textProvider"] = "chatgpt_web" if clean_settings.get("chatModel") else "openrouter"
        if not clean_settings.get("textModel"):
            clean_settings["textModel"] = str(clean_settings.get("chatModel") or ("openrouter/free" if clean_settings["textProvider"] == "openrouter" else ""))
        item = self.store.create_job(mode, title, clean_settings, input_data)
        self.store.append_log(item["id"], "info", "Job đã được tạo và xếp hàng.", stage="input")
        return self.public_job(item["id"])

    def public_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        # Never expose local input/output paths or accidental secrets to the UI.
        public = {**job}
        public_input = dict(job.get("input") or {})
        for key in ("script", "audio", "srt", "prompts", "watermark"):
            if public_input.get(key):
                public_input[key] = Path(str(public_input[key])).name
        public["input"] = public_input
        public["artifacts"] = {
            str(name): {
                "available": Path(str(path)).is_file() or Path(str(path)).is_dir(),
                "filename": Path(str(path)).name,
            }
            for name, path in (job.get("artifacts") or {}).items()
            if isinstance(name, str) and path
        }
        public["settings"] = self._public_settings(job.get("settings") or {})
        public["logs"] = self.store.list_logs(job_id)
        return public

    @staticmethod
    def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
        blocked = {"apikey", "api_key", "token", "accesstoken", "refreshtoken", "secret", "password"}
        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {str(k): clean(v) for k, v in value.items() if str(k).replace("-", "").replace("_", "").lower() not in blocked}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        result = clean(settings)
        return result if isinstance(result, dict) else {}

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.public_job(job["id"]) for job in self.store.list_jobs()]

    def list_logs(self, job_id: str) -> list[dict[str, Any]]:
        if not self.store.get_job(job_id):
            raise KeyError(job_id)
        return self.store.list_logs(job_id)

    def start_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        with self._lock:
            current = self._futures.get(job_id)
            if current and not current.done():
                return self.public_job(job_id)
            if job["status"] not in {"queued", "paused", "interrupted", "failed", "cancelled"}:
                return self.public_job(job_id)
            self._cancel[job_id] = threading.Event()
            self.store.update_job(job_id, status="running", error=None)
            self.store.append_log(job_id, "info", "Job bắt đầu/tiếp tục.", stage=str(job.get("stage") or "input"))
            self._futures[job_id] = self._executor.submit(self._execute, job_id)
        return self.public_job(job_id)

    def pause_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        with self._lock:
            event = self._cancel.get(job_id)
            if event:
                event.set()
        if job["status"] in {"running", "queued"}:
            self.store.update_job(job_id, status="paused")
            self.store.append_log(job_id, "warning", "Đã tạm dừng theo yêu cầu.", stage=str(job.get("stage") or "input"))
        return self.public_job(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self.start_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        with self._lock:
            event = self._cancel.get(job_id)
            if event:
                event.set()
        if job["status"] not in {"completed", "cancelled"}:
            self.store.update_job(job_id, status="cancelled")
            self.store.append_log(job_id, "warning", "Đã hủy job.", stage=str(job.get("stage") or "input"))
        return self.public_job(job_id)

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """Cancel active work and remove every artifact owned by this automation job."""
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        with self._lock:
            self._deleted_jobs.add(job_id)
            event = self._cancel.get(job_id)
            if event:
                event.set()
            future = self._futures.get(job_id)
            active_future = bool(future and not future.done())
            if future and future.cancel():
                # A queued future can be cancelled before _execute starts;
                # no worker will run its finally block to clean this marker.
                active_future = False
        # Delete the parent record first. UI polling must never resurrect it
        # while a Flow child/provider is still being cancelled.
        deleted = self.store.delete_job(job_id)
        # Provider cancellation can be slow. Run it out-of-band so DELETE
        # returns immediately; the parent record is already gone above.
        child_ids = list(job.get("child_job_ids") or [])
        def cancel_children() -> None:
            for child_id in child_ids:
                try:
                    from pipeline.flow import service as flow_service
                    flow_service.delete_job(str(child_id))
                except Exception:
                    pass
        if child_ids:
            threading.Thread(target=cancel_children, name=f"automation-delete-{job_id}", daemon=True).start()
        # _compose creates exactly one isolated directory per automation job.
        # Do not remove the configured root because other jobs may share it.
        compose_root = selected_or_default("automation", str((job.get("settings") or {}).get("outputDir") or ""))
        output_dir = compose_root / safe_output_part(job.get("title"), "job") / job_id
        workspace = self.store.jobs_root / job_id
        def finish_cleanup(_future: Future[Any] | None = None) -> None:
            shutil.rmtree(output_dir, ignore_errors=True)
            shutil.rmtree(workspace, ignore_errors=True)
            with self._lock:
                self._futures.pop(job_id, None)
                self._cancel.pop(job_id, None)
                self._deleted_jobs.discard(job_id)

        if active_future and future:
            future.add_done_callback(finish_cleanup)
        else:
            finish_cleanup()
        return {"id": job_id, "deleted": deleted}

    def retry_job(self, job_id: str, *, from_stage: str | None = None, preview_seconds: float | None = None) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] not in {"completed", "paused", "failed", "interrupted"}:
            return self.public_job(job_id)

        workspace = self.store.workspace(job_id)
        artifacts = dict(job.get("artifacts") or {})
        input_data = dict(job.get("input") or {})
        child_job_ids = list(job.get("child_job_ids") or [])
        settings = dict(job.get("settings") or {})

        if preview_seconds is not None:
            compose_cfg = dict(settings.get("compose") or {})
            compose_cfg["previewSeconds"] = max(0.0, float(preview_seconds))
            settings["compose"] = compose_cfg

        # Determine effective stage to restart from
        if from_stage:
            target_stage = str(from_stage).strip().lower()
        elif job["status"] == "completed":
            target_stage = "compose"
        else:
            target_stage = str(job.get("stage") or "compose")

        valid_stages = {"script", "tts", "image_prompt", "flow_images", "compose"}
        if target_stage not in valid_stages:
            target_stage = "compose"

        # Always clear final video when retrying
        artifacts.pop("video", None)
        try:
            (workspace / "output.mp4").unlink(missing_ok=True)
            shutil.rmtree(workspace / "render-work", ignore_errors=True)
        except Exception:
            pass

        # Clear artifacts and workspace files backwards from target_stage
        if target_stage in {"script", "tts", "image_prompt", "flow_images"}:
            artifacts.pop("images", None)
            shutil.rmtree(workspace / "images", ignore_errors=True)
            child_job_ids = []

        if target_stage in {"script", "tts", "image_prompt"}:
            artifacts.pop("prompts", None)
            try:
                (workspace / "image_prompts.txt").unlink(missing_ok=True)
            except Exception:
                pass
            if input_data.get("generatedPrompts"):
                input_data.pop("prompts", None)

        if target_stage in {"script", "tts"}:
            for key in ("audio", "srt", "audioMp3"):
                artifacts.pop(key, None)
            for fname in ("audio.wav", "subtitles.srt", "audio.mp3"):
                try:
                    (workspace / fname).unlink(missing_ok=True)
                except Exception:
                    pass
            if input_data.get("generatedAudio"):
                input_data.pop("audio", None)
            if input_data.get("generatedSrt"):
                input_data.pop("srt", None)

        if target_stage == "script":
            artifacts.pop("script", None)
            try:
                (workspace / "script.txt").unlink(missing_ok=True)
            except Exception:
                pass
            if input_data.get("generatedScript"):
                input_data.pop("script", None)

        # Restore existing workspace artifacts for preserved upstream stages
        stages_order = ["script", "tts", "image_prompt", "flow_images", "compose"]
        target_idx = stages_order.index(target_stage) if target_stage in stages_order else len(stages_order) - 1

        if target_idx >= stages_order.index("compose"):
            image_dir = workspace / "images"
            if image_dir.is_dir() and any(image_dir.iterdir()):
                artifacts["images"] = str(image_dir)

        if target_idx >= stages_order.index("flow_images"):
            prompts_file = workspace / "image_prompts.txt"
            if prompts_file.is_file():
                artifacts["prompts"] = str(prompts_file)
                input_data["prompts"] = str(prompts_file)
                input_data["generatedPrompts"] = True

        if target_idx >= stages_order.index("image_prompt"):
            wav_file = workspace / "audio.wav"
            srt_file = workspace / "subtitles.srt"
            mp3_file = workspace / "audio.mp3"
            if wav_file.is_file():
                artifacts["audio"] = str(wav_file)
                input_data["audio"] = str(wav_file)
                input_data["generatedAudio"] = True
            if srt_file.is_file():
                artifacts["srt"] = str(srt_file)
                input_data["srt"] = str(srt_file)
                input_data["generatedSrt"] = True
            if mp3_file.is_file():
                artifacts["audioMp3"] = str(mp3_file)

        if target_idx >= stages_order.index("tts"):
            script_file = workspace / "script.txt"
            if script_file.is_file():
                artifacts["script"] = str(script_file)
                input_data["script"] = str(script_file)
                input_data["generatedScript"] = True

        # Re-queue terminal Flow children only if we are continuing flow_images
        if (target_stage == "compose" or (target_stage == "flow_images" and child_job_ids)):
            try:
                from pipeline.flow import service as flow_service
                snapshot = {str(row.get("id")): row for row in flow_service.jobs()}
                for child_id in child_job_ids:
                    child = snapshot.get(str(child_id)) or {}
                    if child.get("status") in {"failed", "action_required"}:
                        flow_service.retry(str(child_id))
            except Exception:
                # The Flow module is optional for non-Flow automation jobs.
                pass

        stage_log_messages = {
            "compose": "Đã đưa lại chặng ghép video vào hàng đợi.",
            "flow_images": "Đã đưa lại chặng tạo ảnh Flow và ghép video vào hàng đợi.",
            "image_prompt": "Đã đưa lại chặng prompt ảnh và các chặng tiếp theo vào hàng đợi.",
            "tts": "Đã đưa lại chặng giọng đọc TTS và các chặng tiếp theo vào hàng đợi.",
            "script": "Đã đưa lại chặng kịch bản và toàn bộ quy trình vào hàng đợi.",
        }

        patch: dict[str, Any] = {
            "status": "queued",
            "stage": target_stage,
            "error": None,
            "artifacts": artifacts,
            "input": input_data,
            "child_job_ids": child_job_ids,
        }
        if preview_seconds is not None:
            patch["settings"] = settings
        self.store.update_job(job_id, **patch)
        self.store.append_log(job_id, "info", stage_log_messages.get(target_stage, "Đã đưa lại job vào hàng đợi."), stage=target_stage)
        return self.start_job(job_id)

    def update_job_settings(self, job_id: str, *, title: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        patch: dict[str, Any] = {}
        if title is not None:
            patch["title"] = str(title).strip()[:160] or job["title"]
        if settings is not None:
            patch["settings"] = dict(settings)
            if dict(settings) != dict(job.get("settings") or {}):
                artifacts = dict(job.get("artifacts") or {})
                for key in ("video", "images"):
                    artifacts.pop(key, None)
                patch["artifacts"] = artifacts
                input_data = dict(job.get("input") or {})
                for marker, key in (("generatedScript", "script"), ("generatedAudio", "audio"), ("generatedSrt", "srt"), ("generatedPrompts", "prompts")):
                    if input_data.pop(marker, False):
                        input_data.pop(key, None)
                        artifacts.pop(key, None)
                patch["input"] = input_data
        if patch:
            self.store.update_job(job_id, **patch)
            self.store.append_log(job_id, "info", "Đã cập nhật cài đặt job.", stage=str(job.get("stage") or "input"))
        return self.public_job(job_id)

    def select_topic(self, job_id: str, topic: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job.get("status") != "awaiting_topic":
            raise ValueError("AUTOMATION_TOPIC_NOT_READY")
        selected = str(topic or "").strip()
        if not selected or len(selected) > 2000:
            raise ValueError("AUTOMATION_TOPIC_INVALID")
        input_data = dict(job.get("input") or {})
        candidates = input_data.get("topicCandidates") or []
        if candidates and selected not in {str(item) for item in candidates}:
            raise ValueError("AUTOMATION_TOPIC_NOT_IN_CHOICES")
        input_data["selectedTopic"] = selected
        self.store.update_job(job_id, input=input_data, status="queued", stage="script", error=None)
        self.store.append_log(job_id, "info", "Đã chọn chủ đề, tiếp tục tạo script.", stage="script")
        return self.start_job(job_id)

    def wait_for_idle(self, timeout: float = 0) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            with self._lock:
                active = [future for future in self._futures.values() if not future.done()]
            if not active:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel.get(job_id)
            deleted = job_id in self._deleted_jobs
        return deleted or bool(event and event.is_set())

    def set_stage(self, job_id: str, stage: str, progress: float, message: str = "") -> None:
        self.store.update_job(job_id, status="running", stage=stage, progress=max(0, min(100, float(progress))))
        if message:
            self.store.append_log(job_id, "info", message, stage=stage)

    def save_artifact(self, job_id: str, name: str, path: Path | str, *, stage: str | None = None) -> None:
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        artifacts = dict(job.get("artifacts") or {})
        artifacts[str(name)] = str(target)
        self.store.update_job(job_id, artifacts=artifacts, stage=stage or job.get("stage") or "input")

    def _execute(self, job_id: str) -> None:
        try:
            self._runner(job_id)
            job = self.store.get_job(job_id)
            if job and job["status"] == "running":
                self.store.update_job(job_id, status="completed", stage="done", progress=100, error=None)
                self.store.append_log(job_id, "success", "Job hoàn thành.", stage="done")
        except AutomationCancelled:
            job = self.store.get_job(job_id)
            if job and job["status"] != "cancelled":
                self.store.update_job(job_id, status="paused")
                self.store.append_log(job_id, "warning", "Job đã tạm dừng.", stage=str(job.get("stage") or "input"))
        except Exception as exc:
            job = self.store.get_job(job_id)
            if not job:
                return
            if job["status"] == "cancelled":
                return
            code = self._error_code(exc)
            error = {"code": code, "message": str(exc)[:4000]}
            self.store.update_job(job_id, status="paused", error=error)
            self.store.append_log(job_id, "error", str(exc), stage=str(job.get("stage") or "input"), details={"code": code})
        finally:
            with self._lock:
                self._cancel.pop(job_id, None)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        text = str(exc).strip()
        match = re.match(r"([A-Z][A-Z0-9_]{2,})", text)
        return match.group(1) if match else "AUTOMATION_STAGE_FAILED"

    @staticmethod
    def flow_failure_retryable(status: str, error: str) -> bool:
        """Distinguish a transient Flow submit error from a required action."""
        if str(status) != "failed":
            return False
        return not re.search(
            r"LOGIN_REQUIRED|GENERATION_FAILED|GENERATION_REJECTED|FLOW_EMPTY_OUTPUT|MODEL_UNAVAILABLE|SETTING_MISMATCH|UI_CHANGED",
            str(error or ""),
            re.I,
        )

    def _run_pipeline(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        workspace = self.store.workspace(job_id)
        inputs = dict(job.get("input") or {})
        mode = str(job.get("input_mode") or "topic")
        settings = dict(job.get("settings") or {})
        target_stage = str(job.get("stage") or "script")
        stages_order = ["script", "tts", "image_prompt", "flow_images", "compose"]
        if target_stage not in stages_order:
            target_stage = "script"
        target_idx = stages_order.index(target_stage)

        if target_idx == 0:
            self.set_stage(job_id, "input", 3, "Đang chuẩn bị input cho job.")
        canonical = self._prepare_inputs(job_id, inputs, workspace)
        self._check_cancel(job_id)

        # AI topic mode intentionally pauses after producing choices.
        topic = str(inputs.get("selectedTopic") or inputs.get("topic") or "").strip()
        if mode == "ai_topic" and not inputs.get("selectedTopic") and target_idx == 0:
            candidates_path = workspace / "topic_candidates.json"
            cached_candidates = None
            if candidates_path.is_file():
                try:
                    cached = json.loads(candidates_path.read_text(encoding="utf-8"))
                    if isinstance(cached, dict) and cached.get("promptVersion") == _TOPIC_PROMPT_VERSION:
                        cached_candidates = cached.get("candidates")
                except (OSError, ValueError, TypeError):
                    cached_candidates = None
            if cached_candidates is not None:
                candidates = cached_candidates
            else:
                self.set_stage(job_id, "topic", 8, "Đang tạo 5 chủ đề để bạn chọn.")
                content, _ = self._request_chat(job_id, self._topic_prompt(topic, settings), [])
                candidates = self._topic_candidates(content)
                if not candidates:
                    raise RuntimeError("AUTOMATION_TOPIC_EMPTY")
                candidates_path.write_text(json.dumps({"promptVersion": _TOPIC_PROMPT_VERSION, "candidates": candidates}, ensure_ascii=False, indent=2), encoding="utf-8")
                inputs["topicCandidates"] = candidates
                self.store.update_job(job_id, input=inputs, artifacts={**(job.get("artifacts") or {}), "topicCandidates": str(candidates_path)})
            if not isinstance(candidates, list) or len(candidates) != 5 or any(not str(item).strip() for item in candidates):
                raise RuntimeError("AUTOMATION_TOPIC_INCOMPLETE")
            inputs["topicCandidates"] = candidates
            self.store.update_job(job_id, input=inputs, status="awaiting_topic", stage="topic", progress=8, error=None)
            self.store.append_log(job_id, "info", "Đã tạo 5 chủ đề; đang chờ bạn chọn.", stage="topic", details={"candidates": candidates})
            return

        # Stage: script
        script = canonical.get("script")
        if not script and (workspace / "script.txt").is_file():
            script = workspace / "script.txt"
            canonical["script"] = script
        if target_idx <= stages_order.index("script"):
            if not script and mode == "youtube":
                youtube_url = str(inputs.get("youtubeUrl") or inputs.get("topic") or topic or "").strip()
                if not youtube_url:
                    raise RuntimeError("YOUTUBE_URL_REQUIRED")
                self.set_stage(job_id, "script", 10, f"Đang tải caption từ YouTube: {youtube_url[:40]}…")
                yt_title, caption_text, caption_file = self._fetch_youtube_caption_and_title(youtube_url, workspace)
                if yt_title and (not job.get("title") or job["title"].startswith("http") or job["title"] in {"Job tự động hoá", "Automation job"}):
                    self.store.update_job(job_id, title=yt_title[:120])
                if caption_file and caption_file.is_file():
                    self.save_artifact(job_id, "youtubeCaption", caption_file, stage="input")
                self.set_stage(job_id, "script", 14, f"Đang phân tích caption & viết lại kịch bản: {yt_title[:40]}…")
                content, artifact = self._request_chat(job_id, self._youtube_rewrite_prompt(yt_title, caption_text, settings), [])
                script = workspace / "script.txt"
                self._write_text_result(script, artifact, content)
                self._clean_script_for_tts(script)
                self.save_artifact(job_id, "script", script, stage="script")
                inputs["script"] = str(script); inputs["generatedScript"] = True
                self.store.update_job(job_id, input=inputs)
                canonical["script"] = script
            elif not script and topic:
                self.set_stage(job_id, "script", 12, "Đang tạo script sạch bằng provider AI đã chọn.")
                content, artifact = self._request_chat(job_id, self._script_prompt(topic, settings), [])
                script = workspace / "script.txt"
                self._write_text_result(script, artifact, content)
                self._clean_script_for_tts(script)
                self.save_artifact(job_id, "script", script, stage="script")
                inputs["script"] = str(script); inputs["generatedScript"] = True
                self.store.update_job(job_id, input=inputs)
                canonical["script"] = script
        if script:
            self.store.update_job(job_id, stage="script" if target_idx <= 0 else target_stage, progress=18 if target_idx <= 0 else job.get("progress", 18))

        # Stage: tts
        audio = canonical.get("audio")
        if not audio and (workspace / "audio.wav").is_file():
            audio = workspace / "audio.wav"
            canonical["audio"] = audio
        srt = canonical.get("srt")
        if not srt and (workspace / "subtitles.srt").is_file():
            srt = workspace / "subtitles.srt"
            canonical["srt"] = srt

        if target_idx <= stages_order.index("tts"):
            if not audio and script:
                # Every script source (AI, upload, or checkpoint) must pass
                # through the same narration-only sanitizer before TTS.
                self._clean_script_for_tts(script)
                self.set_stage(job_id, "tts", 24, "Đang tạo audio và SRT bằng TTS.")
                tts_cfg = settings.get("tts") if isinstance(settings.get("tts"), dict) else {}
                from pipeline.tts.studio import synth_text_job, ensure_wav, ensure_mp3
                from pipeline.tts.voice_store import normalize_voice_language
                output_language = str(settings.get("language") or "vi").strip().lower()
                try:
                    output_language = normalize_voice_language(output_language, strict=True)
                except ValueError as exc:
                    raise RuntimeError(f"AUTOMATION_TTS_LANGUAGE_UNSUPPORTED: {settings.get('language') or 'unknown'}") from exc
                result = synth_text_job(
                    text=script.read_text(encoding="utf-8-sig", errors="replace"),
                    voice=str(tts_cfg.get("voice") or "system"),
                    lang=output_language,
                    speed=float(tts_cfg.get("speed") or 1.0), volume=float(tts_cfg.get("volume") or 1.0),
                    pitch=float(tts_cfg.get("pitch") or 0.0), style=str(tts_cfg.get("style") or "tu_nhien"),
                    match_duration="none", auto_split=True, title=job["title"],
                )
                tts_id = str(result.get("id") or "")
                if not tts_id:
                    raise RuntimeError("AUTOMATION_TTS_EMPTY")
                from pipeline.tts.voice_store import TTS_OUTPUT
                source_dir = TTS_OUTPUT / tts_id
                audio = workspace / "audio.wav"
                srt = workspace / "subtitles.srt"
                shutil.copy2(ensure_wav(tts_id), audio)
                shutil.copy2(source_dir / "subs.srt", srt)
                self.save_artifact(job_id, "audio", audio, stage="tts")
                self.save_artifact(job_id, "srt", srt, stage="srt")
                inputs.update({"audio": str(audio), "srt": str(srt), "generatedAudio": True, "generatedSrt": True})
                self.store.update_job(job_id, input=inputs)
                # Keep the MP3 beside the job for the user even if composition uses WAV.
                try:
                    shutil.copy2(ensure_mp3(tts_id), workspace / "audio.mp3")
                    self.save_artifact(job_id, "audioMp3", workspace / "audio.mp3", stage="tts")
                except Exception:
                    pass
        if not srt and not canonical.get("prompts") and not (workspace / "image_prompts.txt").is_file():
            raise RuntimeError("AUTOMATION_SRT_REQUIRED")

        # Stage: image_prompt
        prompts = canonical.get("prompts")
        if not prompts and (workspace / "image_prompts.txt").is_file():
            prompts = workspace / "image_prompts.txt"
            canonical["prompts"] = prompts

        if target_idx <= stages_order.index("image_prompt"):
            if not prompts and (srt or audio):
                self.set_stage(job_id, "image_prompt", 39, "Đang phân tích SRT và tạo prompt ảnh.")
                # Prefer SRT (text) for timing and dialogue; chat LLMs do not accept audio attachments.
                files = [path for path in (srt,) if path] or ([script] if script else [])
                content, artifact = self._request_chat(job_id, self._image_prompt_request(settings), files)
                prompts = workspace / "image_prompts.txt"
                self._write_text_result(prompts, artifact, content)
                # Format prompt file so each prompt is separated by a blank line
                clean = self._extract_prompt_lines(prompts.read_text(encoding="utf-8"))
                if clean:
                    prompts.write_text(clean + "\n", encoding="utf-8")
                self._validate_prompt_file(prompts)
                self.save_artifact(job_id, "prompts", prompts, stage="image_prompt")
                inputs["prompts"] = str(prompts); inputs["generatedPrompts"] = True
                self.store.update_job(job_id, input=inputs)
                canonical["prompts"] = prompts
        elif prompts:
            self._validate_prompt_file(prompts)

        # Stage: flow_images
        image_dir = workspace / "images"
        cached_images = sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif"}) if image_dir.is_dir() else []

        if target_idx > stages_order.index("flow_images") and cached_images:
            # Recomposing only and images already exist on disk
            images = cached_images
            self.store.update_job(job_id, artifacts={**(self.store.get_job(job_id).get("artifacts") or {}), "images": str(image_dir)})
            self.store.append_log(job_id, "info", "Đã dùng lại ảnh Flow từ checkpoint.", stage="compose")
        elif prompts:
            self.set_stage(job_id, "flow_images", 48, "Đang đưa prompt vào Flow để tạo ảnh.")
            images = self._run_flow_images(job_id, prompts, settings, workspace)
        else:
            images = []
        self._check_cancel(job_id)
        if not images:
            raise RuntimeError("AUTOMATION_IMAGES_EMPTY")

        # Stage: compose
        self.set_stage(job_id, "compose", 82, "Đang ghép ảnh, audio và SRT thành video.")
        self._compose(job_id, images, audio, srt, prompts, settings, workspace)
        self.set_stage(job_id, "done", 100, "Đã hoàn thành video cuối.")

    def _check_cancel(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if self.is_cancelled(job_id) or (job and job.get("status") == "paused"):
            raise AutomationCancelled("AUTOMATION_PAUSED")

    def _prepare_inputs(self, job_id: str, inputs: dict[str, Any], workspace: Path) -> dict[str, Path]:
        mapping = {"script": "script", "audio": "audio", "srt": "subtitles", "prompts": "image_prompts"}
        result: dict[str, Path] = {}
        for key, filename in mapping.items():
            raw = str(inputs.get(key) or "").strip()
            if not raw:
                candidates = sorted(workspace.glob(f"{filename}.*"))
                if candidates:
                    result[key] = candidates[0]
                    self.save_artifact(job_id, key, candidates[0], stage="input")
                continue
            source = Path(raw).expanduser().resolve()
            if not source.is_file():
                candidates = sorted(workspace.glob(f"{filename}.*"))
                if candidates:
                    result[key] = candidates[0]
                    self.save_artifact(job_id, key, candidates[0], stage="input")
                    continue
                raise RuntimeError(f"AUTOMATION_INPUT_MISSING: {key}")
            target = workspace / f"{filename}{source.suffix.lower()}"
            if source != target.resolve():
                shutil.copy2(source, target)
            result[key] = target
            self.save_artifact(job_id, key, target, stage="input")
        return result

    def _topic_prompt(self, topic: str, settings: dict[str, Any]) -> str:
        language = "English" if str(settings.get("language") or "vi").lower() == "en" else "Vietnamese"
        if language == "English":
            instruction = f"STAGE 1 — TOPIC SELECTION. When the user only says start or provides no specific topic, generate exactly 5 potentially engaging educational YouTube video ideas in English that fit this Audio-First 2D engine. Return this compact table and nothing else:\n| # | Video Topic |\n|---|---|\n| 1 | ... |\n| 2 | ... |\n| 3 | ... |\n| 4 | ... |\n| 5 | ... |\nThen write exactly: Choose 1-5 to begin. Starting hint: {topic or 'suggest a strong educational topic.'}"
        elif language == "Korean":
            instruction = f"사용자가 시작만 요청했거나 주제를 주지 않았다면 한국어로 매력적인 YouTube 교육 영상 아이디어 5개를 정확히 생성하세요. 다음 표만 출력하세요:\n| 번호 | 영상 주제 |\n|---|---|\n| 1 | ... |\n| 2 | ... |\n| 3 | ... |\n| 4 | ... |\n| 5 | ... |\n그 다음 정확히 ‘1-5 중 하나를 선택하세요.’라고 쓰세요. 시작 힌트: {topic or '매력적인 교육 주제를 제안하세요.'}"
        else:
            instruction = f"GIAI ĐOẠN 1 — CHỌN CHỦ ĐỀ. Khi người dùng chỉ nói bắt đầu hoặc chưa đưa chủ đề cụ thể, hãy tạo đúng 5 ý tưởng video YouTube giáo dục có khả năng thu hút bằng tiếng Việt, phù hợp với engine Audio-First 2D. Chỉ trả về bảng ngắn này và không thêm nội dung khác:\n| # | Chủ đề video |\n|---|---|\n| 1 | ... |\n| 2 | ... |\n| 3 | ... |\n| 4 | ... |\n| 5 | ... |\nSau đó viết đúng: Chọn số 1-5 để bắt đầu. Gợi ý ban đầu: {topic or 'hãy tự đề xuất chủ đề giáo dục có khả năng thu hút cao.'}"
        base = self._audio_first_engine_prompt(settings)
        return base + "\n\n" + instruction

    @staticmethod
    def _audio_first_engine_prompt(settings: dict[str, Any]) -> str:
        """Return the Audio-First engine template or custom system prompt."""
        engine = str(settings.get("promptEngine") or (settings.get("flow") or {}).get("promptEngine") or settings.get("language") or "vi")
        requested_language = str(settings.get("language") or "vi").strip().lower()
        if engine == "vi" and requested_language != "vi":
            engine = requested_language
        if engine == "custom":
            custom = str(settings.get("systemPrompt") or "").strip()
            if custom:
                return custom
            return audio_first_prompt("vi")
        if engine == "ko":
            return audio_first_prompt("vi") + "\n\nIMPORTANT LANGUAGE OVERRIDE: Write every script, label, image prompt, and instruction in natural Korean (한국어). Do not output Vietnamese."
        if engine not in {"vi", "en"}:
            return audio_first_prompt("vi") + f"\n\nIMPORTANT LANGUAGE OVERRIDE: Write every script, topic, label, image prompt, subtitle, and instruction in natural {engine}. Do not output Vietnamese or English unless it is a proper name or required technical term."
        return audio_first_prompt(engine)

    def _script_prompt(self, topic: str, settings: dict[str, Any]) -> str:
        base = self._audio_first_engine_prompt(settings)
        language_code = str(settings.get("language") or "vi").lower()
        language = "English" if language_code == "en" else ("Korean" if language_code == "ko" else "Vietnamese")
        brief = settings.get("scriptBrief") if isinstance(settings.get("scriptBrief"), dict) else {}
        brief_lines = (
            f"Topic: {topic}\nNiche: {brief.get('niche') or 'general education'}\nAudience: {brief.get('audience') or 'curious general viewers'}\nMain character: {brief.get('character') or 'none specified'}\n"
            f"Target duration: {brief.get('durationMinutes') or 8} minutes\nVideo type: {brief.get('videoType') or 'educational'}\nTone: {brief.get('tone') or 'natural and clear'}\n"
            f"Platform: {brief.get('platform') or 'YouTube'}\nPrimary goal: {brief.get('primaryGoal') or 'watch time'}\nCommon mistake to correct: {brief.get('commonMistake') or 'none specified'}"
        )
        brief_lines_vi = (
            f"Chủ đề: {topic}\nNgách: {brief.get('niche') or 'giáo dục đại chúng'}\nĐối tượng: {brief.get('audience') or 'người xem tò mò'}\nNhân vật chính: {brief.get('character') or 'không chỉ định'}\n"
            f"Độ dài mục tiêu: {brief.get('durationMinutes') or 8} phút\nDạng video: {brief.get('videoType') or 'giáo dục'}\nTone: {brief.get('tone') or 'tự nhiên, rõ ràng'}\n"
            f"Nền tảng: {brief.get('platform') or 'YouTube'}\nMục tiêu chính: {brief.get('primaryGoal') or 'thời lượng xem'}\nSai lầm cần sửa: {brief.get('commonMistake') or 'không nêu'}"
        )
        instruction = (
            f"{brief_lines}\nCreate a complete YouTube retention script in THREE clearly labelled parts: 1) five hook options with 1-10 scores and select one; 2) full script following HOOK → MICRO HOOK → curiosity loop → value → pattern interrupts → climax → result → CTA, including [VISUAL]/[B-ROLL] cues; 3) retention editor self-check, direct fixes, hook/result alignment, and three timestamped retention moments with psychological reasons. Keep spoken lines short and natural."
            if language == "English" else (f"주제: {topic}\n장르: {brief.get('niche') or '교육'}\n대상 시청자: {brief.get('audience') or '호기심 많은 시청자'}\n분량: 약 {brief.get('durationMinutes') or 8}분\n영상 형식: {brief.get('videoType') or '교육'}\n톤: {brief.get('tone') or '자연스럽고 명확한 말투'}\n플랫폼: {brief.get('platform') or 'YouTube'}\n핵심 목표: {brief.get('primaryGoal') or '시청 시간'}\n다뤄야 할 흔한 오해: {brief.get('commonMistake') or '없음'}\n5가지 훅을 점수화하고 최적의 훅을 선택한 뒤, 마이크로 훅·호기심 루프·가치 전달·2개 이상의 패턴 브레이크·클라이맥스·결과·CTA가 포함된 완성 대본을 작성하세요. 중요한 지점에는 [이미지]/[B-ROLL]을 표시하고, 마지막에 리텐션 편집자 점검과 3개의 시청 유지 순간을 심리적 이유와 함께 제시하세요. 자연스러운 한국어로만 작성하세요.") if language == "Korean" else
            f"{brief_lines_vi}\nHãy tạo kịch bản YouTube hoàn chỉnh theo ĐÚNG 3 phần có nhãn rõ ràng: 1) 5 phương án hook 3-5 giây, mỗi hook một góc tiếp cận, chấm 1-10 và chọn hook tốt nhất; 2) kịch bản theo HOOK → MICRO HOOK → mở vòng lặp tò mò → bối cảnh → giá trị → ít nhất 2 điểm phá vỡ khuôn mẫu → cao trào → kết quả → CTA, chèn [HÌNH ẢNH]/[B-ROLL] tại điểm quan trọng; 3) tự kiểm tra dưới góc nhìn biên tập viên, sửa trực tiếp điểm dễ tụt retention, xác nhận hook khớp kết quả, và liệt kê 3 khoảnh khắc giữ chân có mốc thời gian cùng lý do tâm lý. Câu nói ngắn, tự nhiên, sẵn sàng đọc thành tiếng."
        )
        return base + "\n\n" + instruction

    def _image_prompt_request(self, settings: dict[str, Any]) -> str:
        base = self._audio_first_engine_prompt(settings)
        language_code = str(settings.get("language") or "vi").lower()
        language = "English" if language_code == "en" else ("Korean" if language_code == "ko" else "Vietnamese")
        instruction = (
            "Read the attached SRT with timecodes (or script), split visual beats by meaning, then output ONLY the image prompt lines. "
            "Format: 001_[00:00:00.000-00:00:05.000] <English description>. Separate each prompt with a blank line. "
            "IMPORTANT: every second of the video must be covered — do NOT stop early, do NOT skip any segment. "
            "No preamble, no explanation, no filename mention — start immediately with line 001."
            if language == "English" else ("첨부된 SRT의 타임코드를 읽고 의미에 따라 비주얼 비트를 나눈 다음 이미지 프롬프트만 출력하세요. 모든 프롬프트는 한국어로 작성하고, 001_[HH:MM:SS.mmm-HH:MM:SS.mmm] 형식을 사용하며 영상 전체 시간을 빠짐없이 포함하세요. 설명이나 서문 없이 001번부터 시작하세요." if language == "Korean" else
            "Đọc file SRT có timecode (hoặc kịch bản) đính kèm, chia visual beat theo ý nghĩa, rồi chỉ xuất ra các dòng prompt ảnh. "
            "Mỗi prompt theo đúng format GIAI ĐOẠN 6: 001_[00:00:00.000-00:00:05.000] <nội dung prompt tiếng Việt>. Mỗi prompt cách nhau 1 dòng trống. "
            "QUAN TRỌNG: phải phủ kín toàn bộ thời lượng video — không được dừng sớm, không được bỏ sót đoạn nào. "
            "Không mở đầu, không giải thích, không nhắc tên file — bắt đầu ngay bằng dòng 001.")
        )
        return base + "\n\n" + instruction

    @staticmethod
    def _topic_candidates(content: str) -> list[str]:
        values: list[str] = []
        for line in str(content or "").splitlines():
            raw = line.strip()
            if not raw:
                continue
            if re.match(r"^\|?\s*#\s*\|", raw) or re.fullmatch(r"\|?\s*[-:|\s]+\|?", raw):
                continue
            # The canonical Audio-First prompt asks for a Markdown table. Also
            # accept numbered/bulleted output from providers that simplify it.
            table = re.match(r"^\|?\s*\d+\s*\|\s*(.*?)\s*\|?\s*$", raw)
            if table:
                clean = table.group(1).strip(" |")
            else:
                clean = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip()
            clean = re.sub(r"^#+\s*", "", clean).strip(" |")
            lowered = clean.casefold()
            if (
                not clean
                or len(clean) > 300
                or lowered in {"chủ đề video", "video topic", "chủ đề", "topic"}
                or re.match(r"^(?:chọn|choose)\b", lowered)
                or not re.search(r"[\wÀ-ỹ]", clean)
                or re.fullmatch(r"[-:|\s]+", clean)
            ):
                continue
            values.append(clean)
        return list(dict.fromkeys(values))[:5]

    def _request_ephemeral_chat(self, prompt: str, settings: dict[str, Any], conversation_title: str = "Ephemeral Chat") -> str:
        from api.routes.chat import service as chat_service
        selected_provider = str(settings.get("textProvider") or "").strip().lower()
        selected_model = str(settings.get("textModel") or settings.get("chatModel") or "").strip()
        if not selected_provider:
            providers_list = chat_service.providers()
            ready_api = [p for p in providers_list if p.get("configured") and p.get("status") == "ready"]
            if ready_api:
                selected_provider = ready_api[0]["id"]
                selected_model = selected_model or (ready_api[0].get("models") or [{}])[0].get("id", "")
            else:
                account = chat_service.primary_account()
                if account and account.get("status") == "connected":
                    selected_provider = "chatgpt_web"
                else:
                    selected_provider = chat_service.DEFAULT_API_PROVIDER
        if selected_provider == "chatgpt_web":
            account = chat_service.primary_account()
            if not account or account.get("status") != "connected":
                raise RuntimeError("CHATGPT_LOGIN_REQUIRED")
            stored_provider = account["id"]
            selected_model = selected_model or str(account.get("last_model") or chat_service.DEFAULT_MODEL)
        else:
            stored_provider = selected_provider
            selected_model = selected_model or (chat_service.DEFAULT_API_MODEL if selected_provider == "openrouter" else "")
            chat_service.resolve_provider(selected_provider, selected_model)
        conversation = chat_service.store.create_conversation(
            conversation_title,
            stored_provider,
            selected_model,
            provider_id=selected_provider,
        )
        content = ""
        try:
            with self._chat_gate:
                for raw in chat_service.stream_message(
                    conversation["id"],
                    {"content": prompt, "attachmentIds": [], "mode": "chat", "provider": selected_provider, "model": selected_model},
                ):
                    for block in str(raw).split("\n\n"):
                        data = ""
                        for line in block.splitlines():
                            if line.startswith("data: "):
                                data = line[6:].strip()
                        if not data:
                            continue
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
                        if payload.get("delta"):
                            content += str(payload.get("delta") or "")
                        elif payload.get("content") is not None:
                            content = str(payload.get("content") or content)
        finally:
            try:
                chat_service.store.delete_conversation(conversation["id"])
            except Exception:
                pass
        return content

    def suggest_topics(self, hint: str = "", settings: dict[str, Any] | None = None) -> list[str]:
        """Generate 5 suggested video topics using the selected chat provider."""
        cfg = settings or {}
        prompt = self._topic_prompt(hint, cfg)
        try:
            content = self._request_ephemeral_chat(prompt, cfg, "Topic Suggestions")
        except Exception:
            content = ""
        return self._topic_candidates(content)

    @staticmethod
    def _fetch_youtube_caption_and_title(url: str, workspace: Path) -> tuple[str, str, Path | None]:
        """Fetch video title and subtitle/caption text from a YouTube URL using yt-dlp."""
        from pipeline.download.ytdlp_jobs import ytdlp_command
        bin_ = ytdlp_command() or ["yt-dlp"]
        raw_url = str(url or "").strip()
        if not raw_url:
            raise ValueError("YOUTUBE_URL_REQUIRED")

        # 1. Fetch metadata (title and description)
        cmd_meta = [*bin_, "--no-playlist", "--skip-download", "--print", "%(title)s", "--print", "%(description)s", raw_url]
        try:
            meta_res = subprocess.run(cmd_meta, capture_output=True, text=True, timeout=25)
            lines = meta_res.stdout.splitlines() if meta_res.returncode == 0 else []
            title = lines[0].strip() if lines else ""
            description = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        except (subprocess.TimeoutExpired, OSError):
            title = ""
            description = ""

        # 2. Extract subtitle / captions without downloading video
        caption_file: Path | None = None
        caption_text = ""
        out_template = str(workspace / "%(id)s")

        # Try native manual subtitles first
        cmd_sub = [
            *bin_,
            "--no-playlist",
            "--skip-download",
            "--write-subs",
            "--sub-langs", "vi,en,all",
            "--convert-subs", "srt",
            "-o", out_template,
            raw_url,
        ]
        try:
            subprocess.run(cmd_sub, capture_output=True, text=True, timeout=35)
        except (subprocess.TimeoutExpired, OSError):
            pass

        candidates = sorted(workspace.glob("*.srt")) + sorted(workspace.glob("*.vtt"))
        if not candidates:
            # Fallback to auto-generated subtitles
            cmd_auto = [
                *bin_,
                "--no-playlist",
                "--skip-download",
                "--write-auto-subs",
                "--sub-langs", "vi,en,all",
                "--convert-subs", "srt",
                "-o", out_template,
                raw_url,
            ]
            try:
                subprocess.run(cmd_auto, capture_output=True, text=True, timeout=35)
            except (subprocess.TimeoutExpired, OSError):
                pass
            candidates = sorted(workspace.glob("*.srt")) + sorted(workspace.glob("*.vtt"))

        if candidates:
            preferred = ["vi", "en", "es", "fr", "de", "pt", "id", "tr", "it", "nl", "ko", "ja"]
            chosen = None
            for code in preferred:
                match = [c for c in candidates if f".{code}." in c.name or c.name.endswith(f".{code}.srt") or c.name.endswith(f".{code}.vtt")]
                if match:
                    chosen = match[0]
                    break
            caption_file = chosen or candidates[0]
            caption_text = clean_subtitle_text(caption_file)

        if not caption_text and description:
            caption_text = description

        if not title:
            title = "YouTube Video"
        return title, caption_text, caption_file

    def _youtube_rewrite_prompt(self, video_title: str, caption_text: str, settings: dict[str, Any]) -> str:
        language = "English" if str(settings.get("language") or "vi").lower() == "en" else "Vietnamese"
        if language == "English":
            return (
                "You are an expert YouTube storyteller and video scriptwriter.\n"
                "Your task is to analyze the source transcript/captions from a YouTube video and REWRITE it into a fresh, captivating, well-structured narration script for a 1-3 minute video.\n\n"
                f"SOURCE VIDEO TITLE: {video_title}\n\n"
                f"SOURCE TRANSCRIPT / CAPTION:\n{caption_text[:12000]}\n\n"
                "STRICT RULES:\n"
                "1. OUTPUT LANGUAGE: MUST be 100% English. Even if the source video or caption is in another language, write the script entirely in natural, engaging English.\n"
                "2. Narrative Arc: Write an engaging story with a high-retention hook in the first 5-10 seconds, compelling narrative pacing, and a punchy outro.\n"
                "3. Length: Around 300 to 600 words (concise, high energy). Do NOT write an overly long essay and NEVER repeat words in a loop.\n"
                "4. Clean Content: Remove all sponsor shoutouts, like/subscribe begging, chatter, and subtitle errors.\n"
                "5. Spoken Voiceover Pacing: Short, rhythmic sentences optimized for TTS voiceover reading.\n"
                "6. Output Format: Output ONLY the spoken narration text. Absolutely NO markdown, NO bold, NO headers (Hook:, Body:, etc.), NO stage directions, NO timecodes, NO AI commentary.\n\n"
                "Begin immediately with the first word of the narration."
            )
        return (
            "Bạn là một biên kịch chuyên nghiệp và chuyên gia kể chuyện video YouTube hàng đầu.\n"
            "Nhiệm vụ của bạn là phân tích nội dung phụ đề/bản ghi âm từ video YouTube và VIẾT LẠI thành một kịch bản lời đọc (voiceover narration) hoàn toàn mới, hấp dẫn, nhịp điệu nhanh và lôi cuốn cho video dài khoảng 1-3 phút.\n\n"
            f"TIÊU ĐỀ VIDEO GỐC: {video_title}\n\n"
            f"NỘI DUNG CAPTION / TRANSCRIPT GỐC:\n{caption_text[:12000]}\n\n"
            "QUY TẮC BẮT BUỘC:\n"
            "1. NGÔN NGỮ ĐẦU RA: Bắt buộc viết hoàn toàn 100% bằng TIẾNG VIỆT tự nhiên, sinh động, dễ hiểu. Tuyệt đối KHÔNG viết bằng tiếng Anh hay bất kỳ ngôn ngữ nào khác (dù video gốc hoặc phụ đề gốc có là tiếng nước ngoài).\n"
            "2. Cấu trúc & Giữ chân người xem: Bắt đầu ngay bằng một câu hook gây tò mò, giật gân trong 5-10 giây đầu. Phát triển câu chuyện hài hước, kịch tính theo sát tình huống chính và kết thúc bất ngờ.\n"
            "3. Độ dài kịch bản: Khoảng 300 đến 600 từ (súc tích, dồn dập, dễ đọc). Tuyệt đối KHÔNG viết lan man, KHÔNG lặp từ hay lặp cụm từ thành vòng lặp vô tận.\n"
            "4. Lọc sạch tạp âm: Loại bỏ toàn bộ phần chào hỏi rườm rà, quảng cáo nhà tài trợ, kêu gọi like/sub, lời nói đệm và các lỗi dịch vô nghĩa.\n"
            "5. Tối ưu cho giọng đọc AI (TTS): Câu văn gãy gọn, tự nhiên, nhịp điệu sinh động, dễ ngắt nghỉ.\n"
            "6. Định dạng đầu ra: CHỈ XUẤT RA DUY NHẤT nội dung văn bản lời đọc thuyết minh bằng tiếng Việt. Tuyệt đối KHÔNG có markdown, KHÔNG in đậm, KHÔNG chia tiêu đề mục (như Mở đầu:, Thân bài:), KHÔNG ghi chú đạo diễn/sân khấu, KHÔNG có timecode, KHÔNG có lời mở đầu hay kết thúc của AI.\n\n"
            "Bắt đầu ngay bằng từ tiếng Việt đầu tiên của lời thuyết minh."
        )

    def preview_youtube_rewrite(self, url: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = settings or {}
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            yt_title, caption_text, caption_file = self._fetch_youtube_caption_and_title(url, tmp)
            prompt = self._youtube_rewrite_prompt(yt_title, caption_text, cfg)
            content = self._request_ephemeral_chat(prompt, cfg, f"YT Rewrite: {yt_title[:30]}")
            clean_script = self._strip_ai_meta(content).strip()
            return {
                "title": yt_title,
                "caption": caption_text[:3000],
                "captionExcerpt": caption_text[:500] if caption_text else "",
                "script": clean_script,
            }

    @staticmethod
    def _strip_ai_meta(text: str) -> str:
        """Remove AI preamble/postamble lines when no artifact file was attached.

        Pattern: lines that are clearly AI commentary rather than content —
        e.g. 'Đã tạo file...', 'Tên file:', 'Hãy tạo audio...', trailing ```.
        ponytail: simple line-filter; upgrade to LLM post-processing if needed.
        """
        # Strip outer fenced code blocks first
        text = re.sub(r"^```(?:txt|text|markdown)?\s*", "", text.strip(), flags=re.I)
        text = re.sub(r"\s*```$", "", text.strip(), flags=re.I)
        # Drop lines that look like AI meta commentary
        _META = re.compile(
            r"^(?:"
            r"(?:Đã tạo|Đã nhận|Đã phân tích|I(?:'ve| have) (?:created|generated|attached|analyzed))"
            r"|(?:Tên file|File name|Filename)\s*:"
            r"|(?:Hãy tạo|Please (?:create|generate|use|send))"
            r"|(?:Bạn có thể|You can)"
            r"|(?:Lưu ý|Note)\s*:"
            r"|```"
            r")",
            re.I,
        )
        lines = [ln for ln in text.splitlines() if not _META.match(ln.strip())]
        return "\n".join(lines).strip()

    @classmethod
    def _clean_script_for_tts(cls, path: Path) -> None:
        """Keep only spoken narration; never send production notes to TTS/SRT."""
        text = cls._strip_ai_meta(path.read_text(encoding="utf-8-sig", errors="replace"))
        # Defensive cleanup for providers that returned an SRT-like response
        # instead of plain script text: timestamps and cue indexes are never
        # spoken and must not become part of the next SRT.
        if "-->" in text:
            text = re.sub(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}", "\n", text)
            text = re.sub(r"(?m)^\s*\d+\s*$", "", text)
        text = re.sub(r"(?i)kịch\s*bản\s*youtube\s*\n\s*hoàn\s*\n\s*chỉnh", "Kịch bản YouTube hoàn chỉnh", text)
        text = re.sub(r"\s*(?:---\s*)?(?=##\s*[123][.)])", "\n", text)
        lines = text.splitlines()
        # The retention template has hook options before section 2 and an
        # editorial self-check after section 3. Keep only spoken section 2.
        start = next((i for i, line in enumerate(lines) if re.match(r"^\s*(?:#+\s*)?(?:BƯỚC\s*)?2\s*[.)-]\s*(?:KỊCH|FULL|SCRIPT)|^\s*(?:KỊCH BẢN|FULL SCRIPT)\b", line, re.I)), 0)
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*(?:#+\s*)?(?:BƯỚC\s*)?3\s*[.)-]\s*(?:TỰ|SELF|RETENTION)|^\s*(?:TỰ KIỂM TRA|SELF[- ]CHECK|RETENTION EDITOR)\b", lines[i], re.I)), len(lines))
        cleaned: list[str] = []
        for line in lines[start:end]:
            value = re.sub(r"^\s*#+\s*|\*\*|__", "", line).strip()
            value = re.sub(r"^(?:2\s*[.)-]\s*)?(?:KỊCH BẢN(?: YOUTUBE)?|FULL SCRIPT)\s*(?:[-:—]\s*)?", "", value, flags=re.I)
            value = re.sub(r"KỊCH BẢN\s+YOUTUBE\s+HOÀN CHỈNH\s*", "", value, flags=re.I)
            if not value or re.match(r"^(?:BƯỚC\s*2\b|PART\s*2\b|KỊCH BẢN(?: YOUTUBE)?|FULL SCRIPT|HOÀN\s*CHỈNH$|HOOK|MICRO HOOK|CTA|HÌNH ẢNH|B-ROLL|VISUAL|[A-ZÀ-Ỹ ]{3,}:)", value, re.I):
                continue
            value = re.sub(r"\[(?:HOOK|MICRO HOOK|MỞ VÒNG LẶP TÒ MÒ|BỐI CẢNH|GIÁ TRỊ|ĐIỂM PHÁ VỠ KHUÔN MẪU|CAO TRÀO|KẾT QUẢ|CTA|HÌNH ẢNH|B-ROLL|VISUAL|IMAGE|SCENE)[^\]]*\]", "", value, flags=re.I)
            value = re.sub(r"\s*[-—]?\s*\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}\s*", " ", value)
            value = re.sub(r"\s*---\s*", " ", value)
            value = re.sub(r"^\s*(?:HOOK|MICRO HOOK|MỞ VÒNG LẶP TÒ MÒ|BỐI CẢNH|GIÁ TRỊ|ĐIỂM PHÁ VỠ KHUÔN MẪU\s*\d*|CAO TRÀO|KẾT QUẢ|CTA)\s*(?:[-:—].*)?$", "", value, flags=re.I)
            value = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", value)
            if value:
                cleaned.append(value)
        result = "\n".join(cleaned).strip()
        result = re.sub(r"(?i)kịch\s*bản\s*youtube\s+hoàn\s+chỉnh", "", result)
        result = re.sub(r"(?im)^\s*(?:script|kịch\s*bản)(?:\s+youtube)?\s*:?\s*$", "", result)
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        if not result:
            raise RuntimeError("AUTOMATION_SCRIPT_CLEAN_EMPTY")
        path.write_text(result + "\n", encoding="utf-8")

    @classmethod
    def _write_text_result(cls, target: Path, artifact: Path | None, content: str) -> None:
        source = artifact if artifact and artifact.is_file() else None
        if source:
            text = source.read_text(encoding="utf-8-sig", errors="replace")
            text = re.sub(r"^```(?:txt|text|markdown)?\s*|\s*```$", "", text.strip(), flags=re.I).strip()
        else:
            # No artifact: AI pasted content inline — strip meta commentary
            text = cls._strip_ai_meta(str(content or ""))
        if not text:
            raise RuntimeError("AUTOMATION_CHAT_EMPTY_ARTIFACT")
        target.write_text(text + "\n", encoding="utf-8")

    @staticmethod
    def _extract_prompt_lines(text: str) -> str:
        """Keep valid timecode prompt blocks (NNN_[HH:MM:SS...] ...).

        A prompt block starts with the NNN_[ marker line and includes all
        subsequent non-blank lines that belong to the same entry (multi-line
        descriptions). Blocks are separated by a blank line in the output.
        """
        blocks: list[str] = []
        current: list[str] = []
        for ln in text.splitlines():
            stripped = ln.strip()
            if re.match(r"^\d{3}_\[", stripped):
                # Start of a new prompt block
                if current:
                    blocks.append("\n".join(current))
                current = [stripped]
            elif current and stripped:
                # Continuation line of the current block
                current.append(stripped)
            else:
                # Blank line → end of current block
                if current:
                    blocks.append("\n".join(current))
                    current = []
        if current:
            blocks.append("\n".join(current))
        return "\n\n".join(blocks)

    @staticmethod
    def _plain_prompt_lines(text: str) -> list[str]:
        return [line.strip() for line in str(text or "").splitlines() if line.strip()]

    @classmethod
    def _has_timed_prompts(cls, path: Path) -> bool:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return bool(cls._extract_prompt_lines(text))

    @staticmethod
    def _validate_prompt_file(path: Path) -> None:
        from pipeline.srt_image import parse_timing_cues_detailed
        try:
            cues = parse_timing_cues_detailed(path)
        except Exception:
            cues = []
        if not cues and not AutomationService._plain_prompt_lines(path.read_text(encoding="utf-8-sig", errors="replace")):
            raise RuntimeError("AUTOMATION_PROMPTS_EMPTY")

    def _request_chat(self, job_id: str, prompt: str, files: list[Path]) -> tuple[str, Path | None]:
        self._check_cancel(job_id)
        from api.routes.chat import service as chat_service
        current_job = self.store.get_job(job_id) or {}
        input_data = dict(current_job.get("input") or {})
        settings = dict(current_job.get("settings") or {})
        selected_provider = str(settings.get("textProvider") or "").strip().lower()
        selected_model = str(settings.get("textModel") or settings.get("chatModel") or "").strip()
        # Jobs created before the provider selector used ChatGPT Codex implicitly.
        if not selected_provider:
            selected_provider = "chatgpt_web" if settings.get("chatModel") else chat_service.DEFAULT_API_PROVIDER
        if selected_provider == "chatgpt_web":
            account = chat_service.primary_account()
            if not account or account.get("status") != "connected":
                raise RuntimeError("CHATGPT_LOGIN_REQUIRED")
            stored_provider = account["id"]
            selected_model = selected_model or str(account.get("last_model") or chat_service.DEFAULT_MODEL)
        else:
            stored_provider = selected_provider
            selected_model = selected_model or (chat_service.DEFAULT_API_MODEL if selected_provider == "openrouter" else "")
            try:
                chat_service.resolve_provider(selected_provider, selected_model)
            except Exception as exc:
                code = getattr(exc, "code", "CHAT_PROVIDER_UNAVAILABLE")
                secret = str(load_app_config().get("cloud", {}).get(selected_provider, {}).get("apiKey") or "")
                detail = exc.safe_message(secret) if hasattr(exc, "safe_message") else str(exc).replace(secret, "[REDACTED]")
                raise RuntimeError(f"{code}: {detail[:500]}") from None
        conversation_id = str(input_data.get("chatConversationId") or "")
        conversation = chat_service.store.get_conversation(conversation_id) if conversation_id else None
        conversation_provider = str(conversation.get("provider_id") or conversation.get("account_id") or "") if conversation else ""
        legacy_web_conversation = bool(
            conversation
            and selected_provider == "chatgpt_web"
            and str(conversation.get("account_id") or "") == str(stored_provider)
        )
        if conversation and conversation_provider != selected_provider and not legacy_web_conversation:
            conversation = None
        if not conversation:
            conversation = chat_service.store.create_conversation(
                current_job.get("title") or "Automation",
                stored_provider,
                selected_model,
                provider_id=selected_provider,
            )
            input_data["chatConversationId"] = conversation["id"]
            self.store.update_job(job_id, input=input_data)
        attachment_ids: list[str] = []
        for path in files:
            if not path or not path.is_file():
                continue
            suffix = path.suffix.lower()
            content_type = "audio/wav" if suffix == ".wav" else "audio/mpeg" if suffix == ".mp3" else "text/plain"
            saved = chat_service.store.save_attachment(conversation["id"], path.name, path.read_bytes(), max_size=100 * 1024 * 1024, content_type=content_type)
            attachment_ids.append(saved["id"])
        artifact_path: Path | None = None
        content = ""
        with self._chat_gate:
            for raw in chat_service.stream_message(conversation["id"], {"content": prompt, "attachmentIds": attachment_ids, "mode": "chat", "provider": selected_provider, "model": selected_model}):
                for block in str(raw).split("\n\n"):
                    event = ""
                    data = ""
                    for line in block.splitlines():
                        if line.startswith("event: "): event = line[7:].strip()
                        elif line.startswith("data: "): data = line[6:].strip()
                    if not data: continue
                    try: payload = json.loads(data)
                    except ValueError: continue
                    if event == "content.delta": content += str(payload.get("delta") or "")
                    elif event == "message.completed" and payload.get("content") is not None: content = str(payload.get("content") or content)
                    elif event == "artifact.completed":
                        artifact = payload.get("artifact") or {}
                        aid = str(artifact.get("id") or "")
                        if aid:
                            try:
                                candidate = chat_service.store.attachment_path(aid)
                                if candidate.is_file() and candidate.suffix.lower() in {".txt", ".md", ".markdown"}: artifact_path = candidate
                            except (KeyError, ValueError):
                                pass
                    elif event == "message.failed":
                        code = str(payload.get("errorCode") or "CHAT_PROVIDER_REQUEST_FAILED")
                        detail = str(payload.get("error") or "").strip()
                        raise RuntimeError(f"{code}: {detail[:500]}" if detail else code)
        return content, artifact_path

    def _run_flow_images(self, job_id: str, prompts: Path, settings: dict[str, Any], workspace: Path) -> list[Path]:
        from pipeline.flow import service as flow_service
        flow_cfg = settings.get("flow") if isinstance(settings.get("flow"), dict) else {}
        account_id = str(flow_cfg.get("accountId") or "")
        account = next((row for row in flow_service.accounts() if str(row.get("id")) == account_id), None) if account_id else None
        if not account or account.get("status") != "online":
            raise RuntimeError("FLOW_LOGIN_REQUIRED")
        text = prompts.read_text(encoding="utf-8-sig", errors="replace")
        prompt_lines = [line.strip() for line in text.splitlines() if re.match(r"^\d{3}_\[", line.strip())]
        if not prompt_lines:
            prompt_lines = self._plain_prompt_lines(text)
        image_dir = workspace / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        cached = sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif"})
        if prompt_lines and len(cached) >= len(prompt_lines):
            self.store.update_job(job_id, artifacts={**(self.store.get_job(job_id).get("artifacts") or {}), "images": str(image_dir)})
            self.store.append_log(job_id, "info", "Đã dùng lại ảnh Flow từ checkpoint.", stage="flow_images")
            return cached[:len(prompt_lines)]
        parent = self.store.get_job(job_id) or {}
        existing_child_ids = [str(value) for value in (parent.get("child_job_ids") or []) if str(value)]
        existing_snapshot = {str(row.get("id")): row for row in flow_service.jobs()}
        if existing_child_ids and all(child_id in existing_snapshot for child_id in existing_child_ids):
            # Resuming a failed parent must observe the already queued Flow
            # children instead of creating a second batch for the same scenes.
            child_ids = existing_child_ids
            self.store.update_job(job_id, artifacts={**(self.store.get_job(job_id).get("artifacts") or {}), "images": str(image_dir)})
        else:
            jobs = flow_service.enqueue({
                "prompts": prompt_lines,
                "kind": "image", "mode": "text", "accountId": account_id,
                "settings": {"model": str(flow_cfg.get("model") or "Nano Banana 2"), "ratio": str(flow_cfg.get("ratio") or "16:9"), "resolution": str(flow_cfg.get("resolution") or "1K"), "count": int(flow_cfg.get("count") or 1), "concurrency": str(flow_cfg.get("concurrency") or "3"), "outputDir": f"automation_{job_id}"},
            })
            child_ids = [str(item["id"]) for item in jobs]
            self.store.update_job(job_id, child_job_ids=child_ids, artifacts={**(self.store.get_job(job_id).get("artifacts") or {}), "images": str(image_dir)})
        outputs: list[Path] = []
        pending = set(child_ids)
        retried_children: set[str] = set()
        while pending:
            try:
                self._check_cancel(job_id)
            except AutomationCancelled:
                # Do not leave provider children consuming the Flow account after
                # the parent job has been paused or cancelled.
                for child_id in pending:
                    try:
                        flow_service.cancel(child_id)
                    except Exception:
                        pass
                raise
            flow_snapshot = {str(row.get("id")): row for row in flow_service.jobs()}
            for child_id in list(pending):
                child = flow_snapshot.get(child_id, {})
                status = str(child.get("status") or "")
                if status in {"failed", "action_required", "cancelled"}:
                    error = str(child.get("error") or child_id)
                    if self.flow_failure_retryable(status, error) and child_id not in retried_children:
                        retried_children.add(child_id)
                        self.store.append_log(job_id, "warning", f"Flow ảnh {child.get('inputIndex') or child_id} lỗi tạm thời, đang thử lại.", stage="flow_images", details={"code": "FLOW_IMAGE_RETRY"})
                        flow_service.retry(child_id)
                        continue
                    raise RuntimeError(f"FLOW_IMAGE_FAILED: {error}")
                if status != "done":
                    continue
                source = Path(str((child.get("outputs") or [""])[0]))
                if not source.is_file():
                    raise RuntimeError(f"FLOW_IMAGE_OUTPUT_MISSING: ảnh {child.get('inputIndex') or child_id} không có file đầu ra")
                target = image_dir / f"{int(child.get('inputIndex') or len(outputs) + 1):03d}{source.suffix.lower() or '.png'}"
                shutil.copy2(source, target)
                outputs.append(target)
                pending.remove(child_id)
                self.set_stage(job_id, "flow_images", 48 + (len(outputs) / max(1, len(child_ids))) * 30, f"Đã tạo {len(outputs)}/{len(child_ids)} ảnh Flow.")
            time.sleep(0.5)
        outputs.sort(key=lambda path: path.name)
        if outputs:
            self.store.update_job(job_id, artifacts={**(self.store.get_job(job_id).get("artifacts") or {}), "images": str(image_dir)})
        return outputs

    def _compose(self, job_id: str, images: list[Path], audio: Path | None, srt: Path | None, prompts: Path | None, settings: dict[str, Any], workspace: Path) -> None:
        from pipeline import srt_image
        existing = Path(str((self.store.get_job(job_id).get("artifacts") or {}).get("video") or ""))
        if existing.is_file():
            self.store.append_log(job_id, "info", "Đã dùng lại video từ checkpoint.", stage="compose")
            return
        compose = settings.get("compose") if isinstance(settings.get("compose"), dict) else {}
        raw_watermark = str((self.store.get_job(job_id).get("input") or {}).get("watermark") or "")
        watermark = Path(raw_watermark) if raw_watermark else None
        if watermark is not None and not watermark.is_file():
            watermark = None
        configured = str(settings.get("outputDir") or "").strip()
        from pipeline.core.output_paths import selected_or_default
        base_dir = selected_or_default("automation", configured)
        output_dir = base_dir / safe_output_part(self.store.get_job(job_id)["title"], "job") / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        subtitle_enabled = bool(compose.get("subtitleEnabled", True))
        options = {
            "resolution": str(compose.get("resolution") or "auto"),
            "targetPlatform": str(compose.get("targetPlatform") or "auto"),
            "fps": int(compose.get("fps") or 30),
            "crf": int(compose.get("crf") or 20),
            "encoder": str(compose.get("encoder") or "auto"),
            "effect": str(compose.get("effect") or "none"),
            "transitionDuration": float(compose.get("transitionDuration") or 0.28),
            "zoom": str(compose.get("zoom") or "off"),
            "speed": float(compose.get("speed") or 100),
            "volume": float(compose.get("volume") or 100),
            "previewSeconds": float(compose.get("previewSeconds") or 0),
            "removeMetadata": bool(compose.get("removeMetadata", False)),
            "allowMissingMedia": bool(compose.get("allowMissingMedia", False)),
            "subtitleFontFamily": str(compose.get("subtitleFontFamily") or "system"),
            "subtitleSize": float(compose.get("subtitleSize") or 8),
            "subtitleOffset": float(compose.get("subtitleOffset") or 0),
            "subtitleMargin": float(compose.get("subtitleMargin") or 34),
            "subtitleBackground": str(compose.get("subtitleBackground") or "solid"),
            "subtitleColor": str(compose.get("subtitleColor") or "#ffffff"),
            "subtitleBgColor": str(compose.get("subtitleBgColor") or "#000000"),
            "subtitleOpacity": float(compose.get("subtitleOpacity") or 55),
            "delogo": {"enabled": bool(compose.get("delogoEnabled", False)), "auto": bool(compose.get("delogoAuto", True)), "x": float(compose.get("delogoX") or 80), "y": float(compose.get("delogoY") or 82), "w": float(compose.get("delogoW") or 18), "h": float(compose.get("delogoH") or 12)},
            "drawing": {"enabled": bool(compose.get("drawingEnabled", False)), "mode": str(compose.get("drawingMode") or "hand"), "tool": str(compose.get("drawingTool") or "pen"), "handId": str(compose.get("drawingHandId") or {"pen": "pen", "marker": "marker", "brush": "marker"}.get(str(compose.get("drawingTool") or "pen"), "default")), "detail": int(compose.get("drawingDetail") or 72), "thickness": int(compose.get("drawingThickness") or 2), "strokeOrder": str(compose.get("drawingStrokeOrder") or "natural"), "resolution": "1080p"},
            "logo": {"enabled": bool(compose.get("logoEnabled", False)), "source": str(compose.get("logoSource") or "text"), "text": str(compose.get("logoText") or "ZM AI TOOL"), "icon": str(compose.get("logoIcon") or "★"), "fontSize": int(compose.get("logoFontSize") or 32), "color": str(compose.get("logoColor") or "#ffffff"), "size": float(compose.get("logoSize") or 8), "opacity": float(compose.get("logoOpacity") or 85), "x": float(compose.get("logoX") or 88), "y": float(compose.get("logoY") or 88), "motion": str(compose.get("logoMotion") or "fixed"), "scope": str(compose.get("logoScope") or "full"), "start": float(compose.get("logoStart") or 0), "end": float(compose.get("logoEnd") or 10), "visibleSec": float(compose.get("logoVisibleSec") or 4), "hiddenSec": float(compose.get("logoHiddenSec") or 2), "fadeSec": float(compose.get("logoFadeSec") or .5), "safeMargin": float(compose.get("logoSafeMargin") or 4)},
        }
        work = workspace / "render-work"
        # The SRT renderer writes its concat manifest into the work directory.
        # Automation owns this directory (unlike the direct SRT upload route),
        # so create it before handing the job to the renderer.
        work.mkdir(parents=True, exist_ok=True)
        timeline = prompts if prompts and self._has_timed_prompts(prompts) else None
        render_job = srt_image.create_job("output.mp4", work, images, audio, timeline, srt if subtitle_enabled else None, options, watermark, output_dir / "output.mp4")
        srt_image.start(render_job["id"])
        while True:
            try:
                self._check_cancel(job_id)
            except AutomationCancelled:
                try:
                    srt_image.cancel(render_job["id"])
                except Exception:
                    pass
                raise
            state = srt_image.get_job(render_job["id"]) or {}
            progress = float(state.get("progress") or 0)
            self.set_stage(job_id, "compose", 82 + progress * 0.17, f"Đang ghép video: {progress:.0f}%.")
            if state.get("status") == "done":
                output = Path(str(state.get("output") or ""))
                self.save_artifact(job_id, "video", output, stage="compose")
                return
            if state.get("status") == "error":
                raise RuntimeError(f"COMPOSE_FAILED: {state.get('error') or 'renderer error'}")
            if state.get("status") == "cancelled":
                raise AutomationCancelled("AUTOMATION_PAUSED")
            time.sleep(0.5)


service = AutomationService()
