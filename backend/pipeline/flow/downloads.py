"""Flow video download menus, quality selection, and download recovery."""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from . import store
from .constants import _GENERATION_WAIT_CAP, _VIDEO_GENERATION_TIMEOUT_S
from .errors import _classify_visible_flow_error
from .media import (
    _flow_generation_progress,
    _media_created_timestamp,
    _video_fife_url,
    _video_media_ready,
)


def _video_download_qualities_for_plan(plan: str | None) -> list[str]:
    """Download menu tiers by Flow plan (includes 360p for faster/smaller files)."""
    normalized = str(plan or "Free").strip()
    if normalized == "Ultra":
        return ["360p", "720p", "1080p", "4K"]
    if normalized in {"Pro", "Plus"} or normalized.lower() == "plus":
        return ["360p", "720p", "1080p"]
    return ["360p", "720p"]


def _video_download_quality(settings: dict[str, Any] | None, plan: str | None = None) -> str:
    """Map job settings to Flow download menu labels, clamped to account plan.

    When ``plan`` is omitted, only map the label (callers that already clamped).
    """
    raw = str((settings or {}).get("quality") or "").strip().lower()
    if raw in {"4k", "2160", "2160p"} or "4k" in raw:
        preferred = "4K"
    elif raw in {"high", "1080", "1080p"} or "1080" in raw:
        preferred = "1080p"
    elif raw in {"360", "360p", "low", "fast"} or "360" in raw:
        preferred = "360p"
    else:
        preferred = "720p"
    if plan is None:
        return preferred
    allowed = _video_download_qualities_for_plan(plan)
    return preferred if preferred in allowed else allowed[-1]


def _pick_video_menu_item(items: list[str], preferred: str) -> int | None:
    """Index of the Flow download menu item for ``preferred`` — no silent downgrade.

    Items read like "720p Kích thước gốc" / "1080p Upscaled"; the leading token is
    the tier. 360p/720p may fall back to the original-size item, never 1080p/4K.
    """
    tier = str(preferred or "").strip().lower()
    def menu_text(value: Any) -> str:
        # Flow sometimes returns Vietnamese accents in decomposed Unicode
        # form (e.g. ``Kích thước gốc``), so normalize before matching.
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", value).strip().lower()

    texts = [menu_text(item) for item in items]
    for index, text in enumerate(texts):
        if re.match(rf"{re.escape(tier)}(?![0-9a-z])", text):
            return index
    if tier in {"360p", "720p"}:
        for index, text in enumerate(texts):
            if "goc" in text or "original" in text:
                return index
    return None


class FlowDownloadsMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    async def _download_video_via_flow_menu(
        self, page, tile_index: int, output: Path, quality: str, *, job_id: str,
    ) -> None:
        """Retry only this media download; never re-enter generation."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        current = store.get_row("jobs", job_id) or {}
        if str(output) in (current.get("outputs") or []) and self._outputs_exist([output]):
            return
        for attempt in range(1, 4):
            self._check_cancel(job_id)
            store.patch_row("jobs", job_id, {"stage": "downloading", "updatedAt": time.time()})
            try:
                await self._download_video_via_flow_menu_once(page, tile_index, output, quality)
                error = self._output_validation_error([output])
                if error:
                    raise RuntimeError(error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from ._flow._download_errors import is_transient_download_error
                if attempt < 3 and (
                    isinstance(exc, PlaywrightTimeoutError) or is_transient_download_error(exc)
                ):
                    self._log("warning", "download_retry", job_id=job_id,
                              account_id=str(current.get("accountId") or ""), message=str(exc),
                              details={"stage": "downloading", "attempt": attempt,
                                       "path": str(output), "quality": quality})
                    await asyncio.sleep(min(2.0, float(attempt)))
                    continue
                raise RuntimeError(f"FLOW_DOWNLOAD_FAILED: {exc}") from exc
            # Persist each finished part before starting the next one.
            saved = list((store.get_row("jobs", job_id) or {}).get("outputs") or [])
            if str(output) not in saved:
                saved.append(str(output))
            store.patch_row("jobs", job_id, {"outputs": saved, "updatedAt": time.time()})
            return

    async def _download_video_via_flow_menu_once(
        self,
        page,
        tile_index: int,
        output: Path,
        quality: str,
    ) -> None:
        """Open Flow's download menu and pick the exact requested tier (real file)."""
        preferred = _video_download_quality({"quality": quality})
        output.parent.mkdir(parents=True, exist_ok=True)
        grid_url = str(page.url or "")
        # Close any lingering modals/drawers/backdrops before interacting with the tile
        try:
            for _ in range(3):
                backdrop = page.locator(".cdk-overlay-backdrop")
                if await backdrop.count() > 0 and await backdrop.first.is_visible():
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                else:
                    break
        except Exception:
            pass
        tile = page.locator("flow-grid-tile-container").nth(tile_index)
        try:
            await tile.locator(".thumbnail").first.click(timeout=8_000)
        except Exception:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass
            await tile.locator(".thumbnail").first.click(force=True, timeout=10_000)
        dl_btn = page.locator(
            'button[aria-label*="Tải xuống"], button[aria-label*="Download"], '
            'button[aria-label*="download"], button[title*="Download"], '
            'button[data-testid*="download" i], [role="button"][aria-label*="Download"], '
            'button:has(mat-icon:text-is("download")), '
            'button:has(mat-icon:text-is("file_download"))'
        ).first
        try:
            # Legacy Flow exposes the download button directly in the editor.
            # Do not open the three-dot menu: its "Upload content" action is
            # adjacent and can be selected by automation on this page.
            await dl_btn.wait_for(state="visible", timeout=10_000)
            await dl_btn.click()
            menu_items = page.get_by_role("menuitem")
            await menu_items.first.wait_for(state="visible", timeout=5_000)
            texts = [await menu_items.nth(i).inner_text() for i in range(await menu_items.count())]
            index = _pick_video_menu_item(texts, preferred)
            if index is None or await menu_items.nth(index).get_attribute("aria-disabled") == "true":
                raise RuntimeError(
                    f"FLOW_DOWNLOAD_QUALITY_UNAVAILABLE: menu has no {preferred} (menu: {texts})"
                )
            # Upscaled tiers render server-side first (~1–2 min).
            async with page.expect_download(timeout=300_000) as dl_info:
                await menu_items.nth(index).click()
            dl = await dl_info.value
            partial = output.with_name(output.name + ".part")
            try:
                await dl.save_as(str(partial))
                if self._output_validation_error([partial]):
                    raise RuntimeError("FLOW_OUTPUT_EMPTY: downloaded video is empty")
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
        finally:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            # Clicking a thumbnail opens /edit/<id>; return to the grid so the next
            # tile index still points at the same list.
            if "/edit/" in str(page.url or "") and grid_url and "/edit/" not in grid_url:
                try:
                    await page.goto(grid_url, wait_until="domcontentloaded", timeout=20_000)
                    await page.locator("flow-grid-tile-container").first.wait_for(timeout=10_000)
                except Exception:
                    pass

    async def _download_video_from_project_media(
        self,
        api,
        job: dict[str, Any],
        output: Path,
        output_index: int,
    ) -> str:
        """Download a completed video directly when Flow's menu is broken.

        The project payload contains an authenticated FIFE URL after Flow has
        finished rendering. This path preserves the generated media and avoids
        submitting a duplicate job just because the edit-page menu changed.
        """
        data = await api.get_project_data()
        submission_at = float(job.get("submissionStartedAt") or 0)
        baseline_ids = {str(value) for value in job.get("baselineMediaIds") or []}
        candidates: list[dict[str, Any]] = []
        for media in data.get("projectContents", {}).get("media", []):
            if not isinstance(media, dict) or not media.get("name"):
                continue
            media_id = str(media.get("name"))
            if media_id in baseline_ids or not _video_media_ready(media):
                continue
            url = _video_fife_url(media)
            if not url.startswith("http"):
                continue
            created_at = _media_created_timestamp(media)
            if submission_at and created_at and created_at < submission_at - 30:
                continue
            candidates.append({"id": media_id, "url": url, "createdAt": created_at})
        candidates.sort(key=lambda item: float(item.get("createdAt") or 0), reverse=True)
        if not candidates:
            raise RuntimeError("FLOW_DOWNLOAD_URL_MISSING: completed video URL was not exposed by Flow")
        selected = candidates[min(max(0, int(output_index) - 1), len(candidates) - 1)]
        await api.download(str(selected["url"]), output)
        error = self._output_validation_error([output])
        if error:
            raise RuntimeError(error)
        return str(selected["id"])

    async def _download_upscaled_video_api(
        self,
        api,
        media_id: str,
        output: Path,
        *,
        settings: dict[str, Any] | None,
        preferred: str,
        job_id: str,
    ) -> None:
        """Real Flow upsample API (1080p free). Used when the download menu fails."""
        from ._flow._api import (
            VIDEO_AR_LANDSCAPE,
            VIDEO_AR_PORTRAIT,
            VIDEO_RES_1080P,
        )

        if preferred != "1080p":
            raise RuntimeError(
                f"FLOW_DOWNLOAD_QUALITY_UNAVAILABLE: API upsample only covers 1080p, not {preferred}"
            )
        if not media_id or str(media_id).startswith("dom-video:"):
            raise RuntimeError("FLOW_DOWNLOAD_QUALITY_UNAVAILABLE: no media id for upsample")
        try:
            data = await api.get_project_data()
        except Exception as exc:
            raise RuntimeError(f"FLOW_UPSCALE_PROJECT_MISSING: {exc}") from exc
        workflow_id = ""
        for media in data.get("projectContents", {}).get("media", []):
            if str((media or {}).get("name") or "") == str(media_id):
                workflow_id = str(media.get("workflowId") or "")
                break
        if not workflow_id:
            raise RuntimeError("FLOW_UPSCALE_WORKFLOW_MISSING: video has no workflowId")
        ratio = str((settings or {}).get("ratio") or "16:9")
        aspect = VIDEO_AR_PORTRAIT if ratio in {"9:16", "3:4", "2:3"} else VIDEO_AR_LANDSCAPE
        _up_job, status = await api.upscale_and_wait(
            media_id,
            workflow_id,
            timeout_s=_VIDEO_GENERATION_TIMEOUT_S,
            resolution=VIDEO_RES_1080P,
            aspect_ratio=aspect,
            on_poll=lambda _s, elapsed: store.patch_row(
                "jobs", job_id, {"progress": min(95, 90 + int(elapsed / 60)), "updatedAt": time.time()}
            ),
        )
        url = status.fife_url or await self._resolve_video_fife_url(api, _up_job.media_name)
        if not url:
            raise RuntimeError(f"FLOW_DOWNLOAD_URL_MISSING: upsampled {_up_job.media_name}")
        await api.download(url, output)

    async def _wait_and_download_flow_videos(
        self,
        page,
        job: dict[str, Any],
        count: int,
        baseline_thumbs: list[str],
        job_id: str,
        timeout_s: int = _VIDEO_GENERATION_TIMEOUT_S,
        api=None,
    ) -> list[str]:
        """Wait for this job's new Flow video tiles, then download via the quality menu."""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        hard_deadline = time.monotonic() + timeout_s * _GENERATION_WAIT_CAP
        last_pct = -1
        expected_count = max(1, min(4, int(count or 1)))
        started_at = time.monotonic()
        known = set(baseline_thumbs or [])
        tile_indexes: list[int] = []
        while deadline is None or time.monotonic() < deadline:
            self._check_cancel(job_id)
            flow_error = await self._claim_visible_flow_error(page, job, expected_count)
            if flow_error:
                store.patch_row("jobs", job_id, {
                    "submissionStartedAt": None,
                    "submissionProjectId": None,
                    "baselineMediaIds": [],
                    "mediaIds": [],
                    "resumeOnly": False,
                    "progress": 0,
                    "updatedAt": time.time(),
                })
                raise RuntimeError(_classify_visible_flow_error(flow_error))
            tiles = await page.evaluate("""() =>
                [...document.querySelectorAll('flow-grid-tile-container')].slice(0, 24).map((t, index) => {
                    const text = (t.innerText || '').trim().replace(/\\s+/g, ' ');
                    // Only video tiles: uploaded start frames are image tiles.
                    const thumb = t.querySelector('flow-video-tile .thumbnail');
                    // A finished tile mounts a playback progressbar on hover;
                    // only a pending tile's progress means "still generating".
                    const pending = !!t.querySelector('flow-pending-tile') || (!thumb && !!t.querySelector(
                        '[role="progressbar"], mat-progress-spinner, mat-spinner, .loading, .generating'));
                    // Flow exposes % via aria-valuenow or text; an indeterminate
                    // spinner can pin aria-valuenow at 0 while the text advances.
                    const bar = t.querySelector('[role="progressbar"][aria-valuenow]');
                    const ariaPct = Number(bar?.getAttribute('aria-valuenow'));
                    const pctMatch = (text + ' ' + (t.textContent || '')).match(/(\\d+)\\s*%/);
                    const pct = pending ? Math.round(Math.max(
                        bar && Number.isFinite(ariaPct) ? ariaPct : -1,
                        pctMatch ? parseInt(pctMatch[1], 10) : -1,
                    )) : -1;
                    return {
                        index,
                        label: t.getAttribute('aria-label') || '',
                        thumb: thumb ? (thumb.currentSrc || thumb.src || '') : '',
                        pct,
                        busy: pending,
                    };
                })
            """) or []
            elapsed = time.monotonic() - started_at
            current_progress = int((store.get_row("jobs", job_id) or {}).get("progress") or 0)
            pcts = [tile["pct"] for tile in tiles if tile.get("pct", -1) >= 0]
            patch = {"stage": "generating", "updatedAt": time.time()}
            if pcts:
                # Real Flow tile % only; no time-based guessing.
                patch["progress"] = max(current_progress, _flow_generation_progress(max(pcts)))
                if max(pcts) > last_pct and deadline is not None:
                    last_pct = max(pcts)
                    deadline = max(deadline, min(hard_deadline, time.monotonic() + timeout_s))
            store.patch_row("jobs", job_id, patch)
            # aria-label is a Flow-generated short title, not the prompt. The grid is
            # newest-first, so this submission owns the first video/pending tiles;
            # an older run finishing further down must not be claimed.
            ours = [
                tile for tile in tiles
                if tile.get("thumb") or tile.get("pct", -1) >= 0 or tile.get("busy")
            ][:expected_count]
            finished = [
                tile for tile in ours
                if tile.get("thumb") and tile["thumb"] not in known
                and tile.get("pct", -1) < 0 and not tile.get("busy")
            ]
            if len(finished) >= expected_count:
                claimed = self._claim_media_ids(
                    [f"flow-thumb:{tile['thumb']}" for tile in finished], expected_count,
                )
                if claimed:
                    tile_indexes = [
                        tile["index"] for tile in finished
                        if f"flow-thumb:{tile['thumb']}" in claimed
                    ]
                    store.patch_row("jobs", job_id, {"mediaIds": claimed, "updatedAt": time.time()})
                    break
            await asyncio.sleep(1.5)
        else:
            raise RuntimeError(
                f"FLOW_AUTOMATION_FAILED: video result was not observed after submit within {timeout_s}s"
            )

        store.patch_row("jobs", job_id, {"stage": "downloading", "updatedAt": time.time()})
        account = store.get_row("accounts", str(job.get("accountId") or "")) or {}
        quality = _video_download_quality(
            job.get("settings") if isinstance(job.get("settings"), dict) else {},
            str(account.get("plan") or ""),
        )
        outputs: list[str] = []
        for output_index, tile_index in enumerate(tile_indexes, 1):
            self._check_cancel(job_id)
            output = self._output_path(job, output_index, "mp4")
            try:
                await self._download_video_via_flow_menu(page, tile_index, output, quality, job_id=job_id)
            except Exception as menu_exc:
                if api is None:
                    raise
                try:
                    await self._download_video_from_project_media(api, job, output, output_index)
                except Exception as api_exc:
                    raise menu_exc from api_exc
            outputs.append(str(output))
            self._log(
                "success", "output_downloaded",
                job_id=job_id, account_id=str(job.get("accountId") or ""),
                details={"outputIndex": output_index, "path": str(output), "quality": quality},
            )
        return outputs

    async def _resolve_video_fife_url(self, api, media_id: str) -> str:
        """Prefer project payload URL when status polling lags behind the UI."""
        if not media_id or str(media_id).startswith("dom-video:"):
            return ""
        try:
            data = await api.get_project_data()
        except Exception:
            return ""
        for media in data.get("projectContents", {}).get("media", []):
            if str((media or {}).get("name") or "") != str(media_id):
                continue
            return _video_fife_url(media)
        return ""
