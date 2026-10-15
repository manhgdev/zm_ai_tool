"""Generation result identity, tile ownership, progress, and media recovery."""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any

from . import store
from .constants import (
    _GENERATION_WAIT_CAP,
    _IMAGE_GENERATION_TIMEOUT_S,
    _PROJECT_MIGRATION_RECOVERY_WINDOW_S,
    _VIDEO_DONE_STATUSES,
    _VIDEO_FAIL_STATUSES,
)
from .errors import _classify_visible_flow_error, _is_automation_block_error, _rpc_public_error
from .projects import _flow_project_changed_at, _flow_project_id


def _media_prompt_matches(media: dict[str, Any], prompt: str) -> bool:
    """Match a Flow media record to the exact prompt that created it."""
    expected = re.sub(r'\s+', ' ', str(prompt or '').strip()).casefold()
    if not expected:
        return False
    request_data = ((media or {}).get("mediaMetadata") or {}).get("requestData") or {}
    for item in request_data.get("promptInputs") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("textInput") or "").strip()
        if not text:
            parts = ((item.get("structuredPrompt") or {}).get("parts") or [])
            text = " ".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()
        normalized = re.sub(r'\s+', ' ', text).strip().casefold()
        if normalized == expected or expected in normalized or normalized in expected:
            return True
    for key in ('prompt', 'promptText', 'textPrompt'):
        normalized = re.sub(r'\s+', ' ', str(media.get(key) or '')).strip().casefold()
        if normalized and (normalized == expected or expected in normalized or normalized in expected):
            return True
    return False


def _media_created_timestamp(media: dict[str, Any]) -> float:
    raw = str(((media or {}).get("mediaMetadata") or {}).get("createTime") or "").strip()
    if not raw:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _captured_video_ids(response: dict[str, Any]) -> list[str]:
    """Return stable media IDs from either observed Flow video response shape."""
    values: list[str] = []
    for job in response.get("jobs") or []:
        media = job.get("mediaId") or {}
        value = media.get("mediaName") or media.get("name")
        if value:
            values.append(str(value))
    for media in response.get("media") or []:
        value = media.get("name") or media.get("mediaName")
        if value:
            values.append(str(value))
    return list(dict.fromkeys(values))


def _video_fife_url(media: dict[str, Any] | None) -> str:
    """Extract a downloadable video URL from project-media / poll payloads."""
    if not isinstance(media, dict):
        return ""
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    candidates: list[Any] = [
        video,
        video.get("generatedVideo") if isinstance(video, dict) else None,
        video.get("video") if isinstance(video, dict) else None,
        media.get("generatedVideo"),
        media,
    ]
    for block in candidates:
        if not isinstance(block, dict):
            continue
        for key in ("fifeUrl", "fife_url", "url", "uri", "downloadUrl", "download_url"):
            value = str(block.get(key) or "").strip()
            if value.startswith("http"):
                return value
    return ""


def _image_fife_url(media: dict[str, Any] | None) -> str:
    """Extract a downloadable image URL from project-media payloads."""
    if not isinstance(media, dict):
        return ""
    image = media.get("image") if isinstance(media.get("image"), dict) else {}
    candidates: list[Any] = [
        image,
        image.get("generatedImage") if isinstance(image, dict) else None,
        media.get("generatedImage"),
        media,
    ]
    for block in candidates:
        if not isinstance(block, dict):
            continue
        for key in ("fifeUrl", "fife_url", "url", "uri", "downloadUrl", "download_url"):
            value = str(block.get(key) or "").strip()
            if value.startswith("http"):
                return value
    return ""


def _video_media_status(media: dict[str, Any] | None) -> str:
    if not isinstance(media, dict):
        return ""
    return str(
        (((media.get("mediaMetadata") or {}).get("mediaStatus") or {}).get("mediaGenerationStatus"))
        or ""
    )


def _video_media_ready(media: dict[str, Any] | None) -> bool:
    """True when Flow has finished the clip (status and/or download URL).

    Veo Lite often marks SUCCESSFUL before ``video`` is attached, or leaves a
    fifeUrl while status stays non-terminal — either means we can proceed.
    """
    if not isinstance(media, dict):
        return False
    status = _video_media_status(media)
    if status in _VIDEO_FAIL_STATUSES:
        return False
    if status in _VIDEO_DONE_STATUSES:
        return True
    if status.endswith(("_SUCCESS", "_SUCCESSFUL", "_COMPLETE")):
        return True
    return bool(_video_fife_url(media))


def _flow_generation_progress(pct: int) -> int:
    """Map Flow's own 0-100 tile % onto the job's generating band (15-99).

    The job is already at 15% when Flow starts counting from 0%, so a plain
    max() hides the first part of every generation.
    """
    return 15 + round(min(100, max(0, int(pct))) * 84 / 100)


def _captured_image_items(response: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize both current and legacy batchGenerateImages response shapes."""
    values: list[dict[str, str]] = []
    for media in response.get("media") or []:
        generated = (media.get("image") or {}).get("generatedImage") or {}
        media_id = media.get("name") or media.get("mediaName")
        src = generated.get("fifeUrl") or media.get("fifeUrl")
        if media_id and src:
            values.append({"id": str(media_id), "src": str(src)})
    for generated in response.get("generatedImages") or []:
        media_id = generated.get("mediaName") or generated.get("name")
        src = generated.get("fifeUrl") or generated.get("fife_url")
        if media_id and src:
            values.append({"id": str(media_id), "src": str(src)})
    return list({item["id"]: item for item in values}.values())


async def _await_with_job_progress(
    awaitable,
    job_id: str,
    *,
    timeout_s: int,
    start: int = 20,
    ceiling: int = 75,
):
    """Await a Flow response while keeping an accepted job visibly alive."""
    task = asyncio.create_task(awaitable)
    started = time.monotonic()
    try:
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=2.0)
            if done:
                break
            elapsed = time.monotonic() - started
            progress = min(ceiling, start + int(elapsed / max(1, timeout_s) * (ceiling - start)))
            current = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            store.patch_row("jobs", job_id, {
                "stage": "generating",
                "progress": max(current, progress),
                "updatedAt": time.time(),
            })
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class FlowMediaMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    def _claim_media_ids(self, candidates: list[str], expected_count: int) -> list[str]:
        """Atomically assign project media so concurrent jobs cannot share one output."""
        with self._guard:
            persisted = {
                str(media_id)
                for job in self.jobs()
                for media_id in (job.get("mediaIds") or [])
                if str(media_id)
            }
            used = persisted | self._claimed_media_ids
            selected = [media_id for media_id in candidates if media_id not in used][:expected_count]
            if len(selected) == expected_count:
                self._claimed_media_ids.update(selected)
                return selected
            return []

    def _release_media_ids(self, media_ids: Any) -> None:
        """Release transient claims when an attempt is discarded for retry."""
        with self._guard:
            self._claimed_media_ids.difference_update(
                str(media_id) for media_id in (media_ids or []) if str(media_id)
            )

    async def _raise_if_new_flow_error(self, page, baseline_errors: set[str]) -> None:
        for item in await self._scan_visible_errors(page, 24):
            text = str(item.get("text") or "")
            # The project grid is shared: a sibling job's unusual-activity
            # tile is not this submit's error (ours arrives in its RPC reply).
            if item.get("tile") and _is_automation_block_error(text):
                continue
            if text and text not in baseline_errors:
                raise RuntimeError(_classify_visible_flow_error(text))

    async def _project_media_elements(self, page) -> list[dict[str, Any]]:
        """Read generated media tiles from Flow's current Angular DOM."""
        try:
            items = await page.evaluate(
                """() => {
                    const nodes = [
                        ...document.querySelectorAll(
                            'img[data-media-id], video[data-media-id], [data-media-id] img, [data-media-id] video, video[src], video source'
                        ),
                    ];
                    return nodes.map(element => {
                        const host = element.closest('[data-media-id]');
                        const id = element.getAttribute('data-media-id')
                            || (host && host.getAttribute('data-media-id'))
                            || '';
                        const media = element.tagName.toLowerCase() === 'source'
                            ? (element.parentElement || element)
                            : element;
                        const src = media.currentSrc || media.src || element.src || '';
                        return {
                            id,
                            tag: media.tagName.toLowerCase(),
                            src,
                            width: media.naturalWidth || media.videoWidth || 0,
                            height: media.naturalHeight || media.videoHeight || 0,
                            readyState: media.readyState || 0,
                        };
                    }).filter(item => item.src && (item.id || item.tag === 'video' || item.tag === 'img'))
                    .map(item => ({...item, id: item.id || `flow-src:${item.src}`}));
                }"""
            )
            return [item for item in (items or []) if isinstance(item, dict)]
        except Exception:
            return []

    async def _find_existing_project_media(
        self,
        api,
        page,
        job: dict[str, Any],
        kind: str,
        count: int,
    ) -> list[dict[str, Any]]:
        """Recover a request that succeeded while the old interceptor timed out."""
        try:
            data = await api.get_project_data()
        except Exception:
            data = {}
        submission_time = float(job.get("submissionStartedAt") or 0)
        # Recovery mode: submissionStartedAt is known → use time filter, not prompt match.
        # Pre-check mode: no submissionStartedAt → require prompt match to avoid false positives.
        recovery_mode = bool(submission_time or job.get("mediaIds") or job.get("resumeOnly"))
        minimum_time = (submission_time - 30) if submission_time else (float(job.get("createdAt") or 0) - 60)
        records: list[dict[str, Any]] = []
        used_ids = set()
        if not job.get("mediaIds") and (job.get("resumeOnly") or job.get("submissionStartedAt")):
            used_ids = {str(mid) for row in self.jobs() if row.get("id") != job.get("id") for mid in row.get("mediaIds") or []}
            used_ids.update(self._claimed_media_ids)
        for media in data.get("projectContents", {}).get("media", []):
            if not isinstance(media, dict) or not media.get("name"):
                continue
            if str(media['name']) in used_ids:
                continue
            # Flow has returned completed images both under `image` and
            # under `generatedImage`; accept either shape during recovery.
            if kind == "image" and not (media.get("image") or media.get("generatedImage") or _image_fife_url(media)):
                continue
            if kind == "video" and not _video_media_ready(media):
                continue
            media_ids = set(job.get("mediaIds") or [])
            if not media_ids and str(media['name']) in set(job.get("baselineMediaIds") or []):
                continue
            if media_ids and str(media['name']) not in media_ids:
                continue
            # Ownership order: persisted mediaIds → rpcMediaIds → prompt match
            # → recoveryOnly/resume time+baseline claim (Flow often omits prompt
            # metadata on late tiles). Parallel first-pass stays prompt-strict.
            created_at = _media_created_timestamp(media)
            rpc_owned = str(media["name"]) in {str(name) for name in job.get("rpcMediaIds") or []}
            id_owned = bool(media_ids and str(media["name"]) in media_ids)
            prompt_owned = _media_prompt_matches(media, str(job.get("prompt") or ""))
            recovery_relaxed = bool(
                job.get("recoveryOnly")
                or job.get("resumeOnly")
                or (submission_time and not job.get("parallelSession"))
            )
            if not (id_owned or rpc_owned or prompt_owned or recovery_relaxed):
                continue
            if not id_owned and not rpc_owned and not prompt_owned and recovery_relaxed:
                # Relaxed path still requires a post-submit timestamp window.
                if created_at and created_at < minimum_time:
                    continue
            elif not media_ids and created_at and created_at < minimum_time:
                continue
            records.append(media)
        if not records:
            # DOM recovery is safe only for an already-submitted request:
            # baseline and global claims keep concurrent jobs from taking
            # old/duplicate outputs. Fresh parallel probes must wait for API.
            if not (job.get("mediaIds") or job.get("resumeOnly") or job.get("submissionStartedAt") or job.get("recoveryOnly")):
                return []
            expected_tag = "img" if kind == "image" else "video"
            baseline_ids = {str(media_id) for media_id in job.get("baselineMediaIds") or []}
            account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
            project_changed_at = _flow_project_changed_at(account, kind)
            # Jobs queued around a forced project replacement share a new,
            # initially empty gallery. Concurrent submissions can make one
            # worker snapshot another worker's fresh output as its baseline;
            # global media claims are authoritative during this short cohort.
            if (
                project_changed_at
                and float(job.get("createdAt") or 0)
                <= project_changed_at + _PROJECT_MIGRATION_RECOVERY_WINDOW_S
            ):
                baseline_ids.clear()
            required_ids = {str(media_id) for media_id in job.get("mediaIds") or []}
            found: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in await self._project_media_elements(page):
                media_id = str(item.get("id") or "")
                src = str(item.get("src") or "")
                if (
                    not media_id
                    or media_id in seen
                    or media_id in used_ids
                    or media_id in baseline_ids
                    or (required_ids and media_id not in required_ids)
                    or item.get("tag") != expected_tag
                    or not src.startswith("http")
                ):
                    continue
                if kind == "image" and int(item.get("width") or 0) <= 0:
                    continue
                seen.add(media_id)
                found.append(item)
                if len(found) >= max(1, int(count or 1)):
                    break
            if kind == "video" and len(found) < max(1, int(count or 1)):
                baseline_thumbs = {str(value) for value in job.get("baselineThumbs") or []}
                clean_baseline_thumbs = {u.split("?")[0].strip() for u in baseline_thumbs if u}
                try:
                    tiles = await page.evaluate("""() =>
                        [...document.querySelectorAll('flow-grid-tile-container')].slice(0, 24).map((tile, index) => {
                            const thumb = tile.querySelector('.thumbnail');
                            const text = (tile.innerText || '').trim().replace(/\\s+/g, ' ');
                            const pct = text.match(/(\\d+)%/);
                            return {
                                index,
                                thumb: thumb ? (thumb.currentSrc || thumb.src || '') : '',
                                pct: pct ? Number(pct[1]) : -1,
                                busy: !!tile.querySelector('[role="progressbar"], mat-progress-spinner, mat-spinner, .loading, .generating'),
                            };
                        })
                    """) or []
                except Exception:
                    tiles = []
                for tile in tiles:
                    thumb = str(tile.get("thumb") or "")
                    clean_thumb = thumb.split("?")[0].strip()
                    if (not thumb.startswith("http") or thumb in baseline_thumbs or (clean_thumb and clean_thumb in clean_baseline_thumbs)
                            or tile.get("busy") or int(tile.get("pct") or -1) >= 0):
                        continue
                    media_id = f"flow-thumb:{thumb}"
                    if media_id in used_ids or media_id in seen:
                        continue
                    seen.add(media_id)
                    found.append({"id": media_id, "tag": "video", "src": thumb,
                                  "tileIndex": int(tile.get("index") or 0)})
                    if len(found) >= max(1, int(count or 1)):
                        break
            return found
        by_id = {
            str(item.get("id")): item
            for item in await self._project_media_elements(page)
            if item.get("id") and str(item.get("src") or "").startswith("http")
        }
        # Project data is authoritative for identity; the DOM supplies the
        # authenticated, downloadable media URL.
        found: list[dict[str, Any]] = []
        for media in sorted(records, key=lambda item: _media_created_timestamp(item)):
            item = by_id.get(str(media.get("name")))
            if kind in {'image', 'video'}:
                url = _image_fife_url(media) if kind == "image" else _video_fife_url(media)
                if url.startswith('https://'):
                    item = {'id': str(media['name']), 'tag': 'img' if kind == 'image' else 'video', 'src': url}
            if item:
                found.append(item)
            if len(found) >= max(1, int(count or 1)):
                break
        return found

    async def _recover_submitted_media(self, api, page, job, timeout_s=_IMAGE_GENERATION_TIMEOUT_S):
        """Poll the original project and persist ownership before downloading."""
        count = len(job.get("mediaIds") or []) or max(1, min(4, int((job.get("settings") or {}).get("count", 1))))
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        empty_checks = 0
        # Flow may acknowledge the request before it mounts a generating tile.
        # Do not recreate an image on the first empty DOM probe.
        submission_grace_deadline = time.monotonic() + 8 if job.get("kind") == "image" else 0
        while deadline is None or time.monotonic() < deadline:
            self._check_cancel(job["id"])
            visible_error = await self._claim_visible_flow_error(page, job, count)
            if visible_error:
                classified = _classify_visible_flow_error(visible_error)
                if classified.startswith("FLOW_GENERATION_REJECTED:"):
                    store.patch_row("jobs", job["id"], {
                        "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "mediaIds": [], "resumeOnly": False,
                        "updatedAt": time.time(),
                    })
                raise RuntimeError(classified)
            # Recovery may need to claim media records whose request metadata
            # is absent in Flow's project payload. Keep this flag local to the
            # recovery probe so the initial parallel poll remains strict.
            recovery_job = {**job, "recoveryOnly": True}
            items = await self._find_existing_project_media(api, page, recovery_job, job["kind"], count)
            if len(items) >= count:
                ids = list(job.get("mediaIds") or [])
                if not ids:
                    ids = self._claim_media_ids([str(item['id']) for item in items], count)
                if ids:
                    store.patch_row("jobs", job["id"], {"mediaIds": ids, "stage": "downloading", "updatedAt": time.time()})
                    return ids
            # Recovery runs against an existing submission. The first visible
            # tile can belong to an older job (including an unusual-activity
            # banner), so classifying it here would attach the wrong error to
            # this submission. Initial submit polling still checks visible
            # errors in _wait_for_project_media().
            if job.get("kind") == "image" and page is not None and deadline is not None:
                try:
                    tile_state = await page.evaluate("""() => {
                        const tiles = [...document.querySelectorAll('flow-grid-tile-container')];
                        let percent = -1;
                        let generating = false;
                        for (const tile of tiles) {
                            if (tile.querySelector('[data-media-id], img, video')) continue;
                            const text = tile.innerText || '';
                            const match = text.match(/(\\d+)\\s*%/);
                            const bar = tile.querySelector('[role="progressbar"][aria-valuenow]');
                            const aria = Number(bar?.getAttribute('aria-valuenow'));
                            const value = Math.max(bar && Number.isFinite(aria) ? aria : -1, match ? Number(match[1]) : -1);
                            if (Number.isFinite(value) && value >= 0) percent = Math.max(percent, Math.min(99, value));
                            if (bar || tile.querySelector('mat-progress-spinner, mat-spinner, .generating') || match) generating = true;
                        }
                        return { generating, percent };
                    }""") or {}
                    generating = bool(tile_state.get("generating"))
                    percent = int(tile_state.get("percent") or -1)
                    if generating and percent >= 0:
                        store.patch_row("jobs", job["id"], {
                            "stage": "generating", "progress": _flow_generation_progress(percent),
                            "updatedAt": time.time(),
                        })
                except Exception:
                    generating = False
                # Keep polling only while Flow visibly reports an active tile.
                # A completed tile was handled above; everything else is a
                # failed or lost submission and should surface immediately.
                if generating:
                    deadline = max(deadline, time.monotonic() + 5)
            empty_checks += 1
            if empty_checks >= 1 and page is not None and time.monotonic() >= submission_grace_deadline:
                try:
                    has_pending = bool(await page.evaluate("""() =>
                        [...document.querySelectorAll('flow-grid-tile-container')].some(tile =>
                            !tile.querySelector('[data-media-id]') && Boolean(
                                tile.querySelector('[role="progressbar"], mat-progress-spinner, mat-spinner, .loading, .generating')
                                || /(?:generating|đang tạo|processing|%)/i.test(tile.innerText || '')
                            )
                        )
                    """))
                except Exception:
                    has_pending = True
                # An empty grid during the first recovery probes is not proof
                # that submit failed; Flow can mount the tile after navigation
                # settles. Keep the browser alive until the real recovery
                # timeout instead of closing Chrome and recreating too early.
            store.patch_row("jobs", job["id"], {"stage": "recovering", "updatedAt": time.time()})
            await asyncio.sleep(5)
        if job.get("kind") == "image":
            baseline = {str(x) for x in job.get("baselineMediaIds") or []}
            reliable = False
            try:
                data = await api.get_project_data()
                names = {str(m.get("name")) for m in data.get("projectContents", {}).get("media", []) if isinstance(m, dict)}
                reliable = bool(names) and (not baseline or bool(names & baseline))
            except Exception:
                reliable = False
            if reliable:
                raise RuntimeError("FLOW_RESULT_NOT_FOUND: submitted image result not found and nothing is generating; recreating")
        raise RuntimeError("FLOW_GENERATION_TIMEOUT: original result is not ready; retry continues recovery")

    @staticmethod
    async def _scan_visible_errors(page, limit: int) -> list[dict[str, Any]]:
        """Visible failed-tile / rejection texts on the Flow grid (newest first)."""
        if page is None:
            return []
        try:
            return await page.evaluate("""(limit) => {
                const visible = (node) => !!node && node.getClientRects().length > 0;
                const textOf = (node) => (node?.innerText || '').trim().replace(/\\s+/g, ' ');
                const tiles = [...document.querySelectorAll('flow-grid-tile-container')]
                    .slice(0, Math.max(1, limit || 1));
                const tileErrors = tiles.map((tile, index) => {
                    const candidate = tile.querySelector(
                        '.error-message, [role="alert"], [data-testid*="error" i], [class*="error" i]'
                    );
                    if (visible(candidate)) {
                        const text = textOf(candidate);
                        // Prompt tiles can carry role=alert/error classes for
                        // accessibility and include icon names such as
                        // play_circle/keyboard_return. Require actual error
                        // language before claiming a generation rejection.
                        if (text && /(?:error|failed|failure|unsuccessful|rejected|blocked|sensitive|safety|try again|not available|abnormal activity|suspicious activity|unusual activity|lỗi|thất bại|không thành công|hoạt động bất thường|hoạt động đáng ngờ|nhạy cảm|an toàn|thử lại)/i.test(text)) {
                            return { index, text, tile: true };
                        }
                    }
                    // Prompt/preview tiles contain icon names and prompt text.
                    // Only an explicit error element above proves rejection.
                    return null;
                }).filter(Boolean);
                if (tileErrors.length) return tileErrors;
                // Some Flow builds render the rejection in a visible dialog
                // attached to the generation surface instead of the tile.
                const dialog = [...document.querySelectorAll(
                    '[role="dialog"], [role="alert"], mat-snack-bar-container, .mat-mdc-snack-bar-container'
                )].find(node => visible(node) && /(?:abnormal activity|hoạt động bất thường|suspicious activity|unusual activity|please wait a few seconds|vui lòng chờ vài giây|credit|tín dụng|quota|hạn mức)/i.test(textOf(node)));
                const text = textOf(dialog);
                return text ? [{ index: 0, text }] : [];
            }""", max(1, int(limit or 1))) or []
        except Exception:
            return []

    async def _remember_visible_errors(self, page, job_id: str) -> None:
        """Errors already on screen at submit time belong to older jobs, not this one."""
        # Duplicates kept: identical failed tiles are told apart by count.
        texts = sorted(
            text for item in await self._scan_visible_errors(page, 24)
            if (text := str(item.get("text") or "").strip())
        )
        store.patch_row("jobs", job_id, {"baselineErrorTexts": texts})

    async def _claim_visible_flow_error(
        self,
        page,
        job: dict[str, Any],
        tile_limit: int,
    ) -> str:
        """Claim one visible failed Flow tile so concurrent jobs do not share it.

        Failed tiles carry no id and identical texts (every unusual-activity
        tile reads the same), so tiles are counted: a job owns a failure only
        while more tiles with that text are visible than at its submit, minus
        those other jobs (or this attempt) already claimed since then.
        """
        errors = await self._scan_visible_errors(page, max(tile_limit, 24))
        if not errors:
            return ""
        account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
        project_id = _flow_project_id(
            account, str(job.get("kind") or "image"), legacy_for_video=True,
        )
        job_id = str(job.get("id") or "")
        current_job = store.get_row("jobs", job_id) or job
        current_submission_at = float(current_job.get("submissionStartedAt") or 0)
        baseline = Counter(str(text) for text in current_job.get("baselineErrorTexts") or [])
        visible = Counter(text for item in errors if (text := str(item.get("text") or "").strip()))
        with self._guard:
            for text, count in visible.items():
                claimed = sum(
                    1 for signature in self._claimed_error_tiles
                    if (parts := signature.split("\x1f"))[:2] == [project_id, text]
                    and len(parts) == 4 and float(parts[3]) >= current_submission_at
                )
                if count - baseline[text] - claimed <= 0:
                    continue
                signature = "\x1f".join((project_id, text, job_id, repr(time.time())))
                self._claimed_error_tiles.add(signature)
                store.patch_row("jobs", str(job.get("id") or ""), {
                    "lastFlowErrorSignature": signature,
                    "lastGenerationRejectedSubmissionAt": current_submission_at,
                    "updatedAt": time.time(),
                })
                return text
        return ""

    @staticmethod
    async def _flow_tile_percent(page, expected_count: int) -> int:
        """Real % on incomplete Flow tiles, or -1 when Flow shows none.

        Prefer tiles that are still generating so a finished sibling cannot
        hide this job's rising percentage and starve the wait deadline.
        """
        try:
            values = await page.evaluate("""(n) => {
                const all = [...document.querySelectorAll('flow-grid-tile-container')];
                const incomplete = all.filter((tile) => !tile.querySelector(
                    '[data-media-id], img[src^="http"], video[src^="http"]'
                ));
                const pool = (incomplete.length ? incomplete : all).slice(0, n);
                return pool.map((t) => {
                    const bar = t.querySelector('[role="progressbar"][aria-valuenow]');
                    const aria = parseFloat(bar?.getAttribute('aria-valuenow'));
                    const m = ((t.innerText || '') + ' ' + (t.textContent || '')).match(/(\\d+)\\s*%/);
                    return Math.round(Math.max(isNaN(aria) ? -1 : aria, m ? parseInt(m[1], 10) : -1));
                });
            }""", max(1, expected_count)) or []
        except Exception:
            return -1
        values = [v for v in values if v >= 0]
        return max(values) if values else -1

    @staticmethod
    async def _flow_pending_generation(page) -> bool:
        """True when a grid tile is visibly generating without completed media."""
        if page is None:
            return False
        try:
            return bool(await page.evaluate("""() =>
                [...document.querySelectorAll('flow-grid-tile-container')].some((tile) => {
                    if (tile.querySelector('[data-media-id], img[src^="http"], video[src^="http"]')) return false;
                    return Boolean(
                        tile.querySelector('[role="progressbar"], mat-progress-spinner, mat-spinner, .generating')
                        || /(?:generating|đang tạo|processing|\\d+\\s*%)/i.test(tile.innerText || '')
                    );
                })
            """))
        except Exception:
            return False

    async def _wait_for_project_media(
        self,
        page,
        baseline_ids: set[str],
        kind: str,
        expected_count: int,
        job_id: str,
        timeout_s: int = _IMAGE_GENERATION_TIMEOUT_S,
        api=None,
        job: dict[str, Any] | None = None,
        rpc_responses: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Wait for media tiles after a current Flow Angular submit."""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        hard_deadline = time.monotonic() + timeout_s * _GENERATION_WAIT_CAP
        last_pct = -1
        next_project_check = 0.0
        rpc_names: list[str] = []
        account_id = str((job or {}).get("accountId") or (store.get_row("jobs", job_id) or {}).get("accountId") or "")
        store.patch_row("jobs", job_id, {
            "stage": "generating",
            "progress": int((store.get_row("jobs", job_id) or {}).get("progress") or 0),
            "updatedAt": time.time(),
        })
        while deadline is None or time.monotonic() < deadline:
            self._check_cancel(job_id)
            if rpc_responses:
                for response in list(rpc_responses):
                    try:
                        classified = _rpc_public_error(re.sub(r"\s+", " ", await response.text())[:2000])
                    except Exception:
                        classified = ""
                    if classified:
                        raise RuntimeError(classified)
                rpc_items, names = await self._rpc_media(rpc_responses)
                if names != rpc_names:
                    rpc_names = names
                    store.patch_row("jobs", job_id, {"rpcMediaIds": names, "updatedAt": time.time()})
                    if job is not None:
                        job = {**job, "rpcMediaIds": names}
                rpc_items = [item for item in rpc_items if item["id"] not in baseline_ids]
                if kind == "image" and len(rpc_items) >= max(1, expected_count):
                    return rpc_items[:max(1, expected_count)]
            items = await self._project_media_elements(page)
            if api is not None and job is not None and time.monotonic() >= next_project_check:
                next_project_check = time.monotonic() + 4
                fresh_job = store.get_row("jobs", job_id) or job
                recovered = await self._find_existing_project_media(api, page, fresh_job, kind, expected_count)
                recovered = [item for item in recovered if str(item['id']) not in baseline_ids]
                if len(recovered) >= max(1, expected_count):
                    return recovered[:max(1, expected_count)]
            # Parallel tabs must never claim a DOM tile without API ownership
            # metadata: the tile can belong to a sibling job in the same Flow
            # project. Waiting/retrying is safer than downloading the wrong image.
            if job and job.get("parallelSession"):
                items = []
            fresh: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in items:
                if str(item.get('id')) in seen:
                    continue
                seen.add(str(item.get('id')))
                if item.get('tag') != ('img' if kind == 'image' else 'video'):
                    continue
                if str(item.get("id")) in baseline_ids:
                    continue
                src = str(item.get("src") or "")
                if not src.startswith("http"):
                    continue
                if kind == "image" and not src.startswith("https://"):
                    continue
                fresh.append(item)
            if len(fresh) >= max(1, expected_count):
                return fresh  # caller claims the first unclaimed N (siblings may own earlier ones)
            # Do not scan document.body.innerText here. Flow keeps hidden
            # accessibility copy and banners in the page shell; a body-wide
            # match can turn unrelated text into FLOW_AUTOMATION_BLOCKED.
            # _claim_visible_flow_error() below inspects only visible tile errors.
            flow_error = await self._claim_visible_flow_error(
                page,
                job or (store.get_row("jobs", job_id) or {"id": job_id}),
                expected_count,
            )
            if flow_error:
                classified = _classify_visible_flow_error(flow_error)
                if classified.startswith("FLOW_GENERATION_REJECTED:"):
                    store.patch_row("jobs", job_id, {
                        "submissionStartedAt": None, "submissionProjectId": None,
                        "baselineMediaIds": [], "mediaIds": [], "resumeOnly": False,
                        "updatedAt": time.time(),
                    })
                store.patch_row("jobs", job_id, {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "progress": 0,
                    "updatedAt": time.time(),
                })
                raise RuntimeError(classified)
            current_progress = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            pct = await self._flow_tile_percent(page, expected_count)
            # Keep waiting while Flow still owns this submission (RPC names or
            # a pending tile), even when % is mute — still capped by hard_deadline.
            submission_alive = bool(rpc_names) or await self._flow_pending_generation(page)
            # Only expose a real Flow tile percentage. Missing progress means
            # the submission is not healthy and must be handled by timeout or
            # recovery instead of being presented as advancing work.
            patch = {"stage": "generating", "updatedAt": time.time()}
            if pct >= 0:
                patch["progress"] = max(current_progress, _flow_generation_progress(pct))
                if pct > last_pct and deadline is not None:
                    last_pct = pct
                    deadline = max(deadline, min(hard_deadline, time.monotonic() + timeout_s))
                if pct >= 99:
                    # Flow reports 99% before the image URL/naturalWidth is
                    # mounted. Keep collecting this submitted result instead
                    # of treating the near-complete job as lost and charging
                    # a duplicate retry.
                    deadline = max(deadline or 0, time.monotonic() + 20)
            elif submission_alive and deadline is not None:
                deadline = max(deadline, min(hard_deadline, time.monotonic() + timeout_s))
            store.patch_row("jobs", job_id, patch)
            await asyncio.sleep(1)
        raise RuntimeError(
            f"FLOW_GENERATION_TIMEOUT: no completed {kind} media appeared after submit"
        )

    async def _flow_tile_thumbs(self, page) -> list[str]:
        """Thumbnail URLs already on the grid, so old clips are never re-claimed."""
        try:
            return list(await page.evaluate("""() =>
                [...document.querySelectorAll('flow-grid-tile-container .thumbnail')]
                    .map(img => img.currentSrc || img.src || '')
                    .filter(Boolean)
            """) or [])
        except Exception:
            return []
