"""Flow project identity, creation, and migration recovery."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

from pipeline.core.output_paths import safe_output_part

from . import store
from .constants import _PROJECT_RE
from .controls import _dismiss_flow_whats_new


def _flow_project_field(kind: str) -> str:
    """Return the account field that owns the media project for a job kind."""
    return "videoProjectId" if str(kind or "image") == "video" else "imageProjectId"


def _flow_project_id(account: dict[str, Any], kind: str, *, legacy_for_video: bool = False) -> str:
    """Read a kind-specific project, with legacy account compatibility."""
    field = _flow_project_field(kind)
    specific = str(account.get(field) or "").strip()
    if specific:
        return specific
    legacy = str(account.get("projectId") or "").strip()
    if str(kind or "image") != "video" or legacy_for_video:
        return legacy
    return ""


def _flow_project_changed_at(account: dict[str, Any], kind: str) -> float:
    """Read the migration timestamp for one media project only."""
    specific = account.get(f"{_flow_project_field(kind)}ChangedAt")
    try:
        if specific:
            return float(specific)
    except (TypeError, ValueError):
        pass
    if str(kind or "image") == "image":
        try:
            return float(account.get("projectChangedAt") or 0)
        except (TypeError, ValueError):
            pass
    return 0.0


class FlowProjectsMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    async def _ensure_account_project(self, page, account_id: str, project_id: str = "") -> str:
        """Give a signed-in account a Flow project, like Connect. Never replaces saved IDs."""
        stored = store.get_row("accounts", account_id) or {}
        project_id = project_id or str(stored.get("imageProjectId") or stored.get("projectId") or "").strip()
        if not project_id:
            try:
                href = await page.locator('a[href*="/project/"]').first.get_attribute("href", timeout=3_000)
                found = _PROJECT_RE.search(href or "")
                if found:
                    project_id = found.group(1)
            except Exception:
                pass
        if not project_id:
            project_id = await self._create_flow_project_ui(page)
        patch = {
            key: project_id for key in ("projectId", "imageProjectId")
            if project_id and not str(stored.get(key) or "").strip()
        }
        if patch:
            store.patch_row("accounts", account_id, {**patch, "updatedAt": time.time()})
        return project_id

    async def _create_flow_project_ui(
        self,
        page,
        *,
        avoid_project_id: str = "",
        avoid_project_ids: set[str] | None = None,
    ) -> str:
        """Create a brand-new Flow project via the lobby UI. Returns the new project id or \"\"."""
        from .browser import FLOW_BASE_URL
        avoid = { str(pid).strip() for pid in (avoid_project_ids or set()) if str(pid or "").strip() }
        if str(avoid_project_id or "").strip():
            avoid.add(str(avoid_project_id).strip())
        await page.goto(FLOW_BASE_URL, wait_until="domcontentloaded", timeout=15_000)
        await asyncio.sleep(2)
        match = _PROJECT_RE.search(str(page.url or ""))
        if match and match.group(1) not in avoid:
            return match.group(1)

        candidates = [
            page.get_by_role("button", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I)),
            page.get_by_role("link", name=re.compile(r"new project|create project|tạo dự án|dự án mới", re.I)),
            page.get_by_text(re.compile(r"^\s*(new project|create project|tạo dự án|dự án mới)\s*$", re.I)),
            page.locator(
                '[aria-label*="new project" i], [aria-label*="create project" i], '
                '[aria-label*="dự án mới" i], [aria-label*="tạo dự án" i]'
            ),
            page.locator(
                'button:has-text("New project"), a:has-text("New project"), '
                'button:has-text("Create project"), a:has-text("Create project"), '
                'button:has-text("New Project"), button:has-text("Dự án mới"), '
                'button:has-text("Tạo dự án")'
            ),
            page.locator('[data-testid*="create-project" i], [data-testid*="new-project" i]'),
        ]
        clicked = False
        for locator in candidates:
            try:
                target = locator.first
                if await target.count() == 0:
                    continue
                if not await target.is_visible():
                    continue
                await target.click(force=True)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            return ""

        def _is_replacement(url: str) -> bool:
            found = _PROJECT_RE.search(url or "")
            return bool(found and found.group(1) not in avoid)

        try:
            await page.wait_for_url(_is_replacement, timeout=15_000)
        except Exception:
            create = page.get_by_role(
                "button", name=re.compile(r"^create$|^tạo$|^tạo dự án$|^done$|^xong$|^continue$|^tiếp$", re.I),
            ).first
            if await create.count():
                await create.click(force=True)
                try:
                    await page.wait_for_url(_is_replacement, timeout=15_000)
                except Exception:
                    return ""
            else:
                return ""
        match = _PROJECT_RE.search(str(page.url or ""))
        if match and match.group(1) not in avoid:
            return match.group(1)
        return ""

    @staticmethod
    def _bind_flow_project(api, client, project_id: str) -> None:
        """Point API + client at a replacement project without rebuilding the browser."""
        api.project_id = project_id
        api._project_page_url = f"https://flow.google.com/project/{project_id}" if project_id else ""
        client.project_id = project_id
        client._project_url = f"https://flow.google.com/project/{project_id}"

    async def _ensure_account_project_page(
        self,
        page,
        *,
        api,
        client,
        account: dict[str, Any],
        job_id: str = "",
        kind: str = "image",
    ) -> dict[str, Any]:
        """Open the kind-specific account project; repair it if deleted."""
        current_job = store.get_row("jobs", job_id) if job_id else None
        scoped = bool(current_job)
        settings = (current_job or {}).get("settings") or {}
        folder_key = safe_output_part(
            settings.get("outputDir") or (current_job or {}).get("outputFolder") or "results",
            "results",
        )
        project_field = (
            "laneProject_" + safe_output_part(kind) + "_"
            + folder_key + "_"
            + safe_output_part(settings.get("ratio") or "16:9") + "_"
            + safe_output_part(settings.get("resolution") or "default")
            if scoped else _flow_project_field(kind)
        )
        target_id = str((current_job or {}).get("submissionProjectId") or account.get(project_field) or "") if scoped else _flow_project_id(account, kind)
        if scoped and not target_id:
            account_id = str(account.get("id") or "")
            with self._guard:
                lane_lock = self._project_recovery_locks.setdefault(account_id + ":" + project_field, threading.Lock())
            while not lane_lock.acquire(blocking=False):
                await asyncio.sleep(0.1)
            try:
                persisted = store.get_row("accounts", account_id) or {}
                target_id = str(persisted.get(project_field) or "")
                if not target_id:
                    existing_ids = {
                        str(val).strip()
                        for key, val in {**account, **persisted}.items()
                        if (key in {"projectId", "imageProjectId", "videoProjectId"} or str(key).startswith("laneProject_"))
                        and str(val or "").strip()
                    }
                    if str(client.project_id or "").strip():
                        existing_ids.add(str(client.project_id).strip())
                    target_id = await self._create_flow_project_ui(page, avoid_project_ids=existing_ids)
                    if not target_id:
                        raise RuntimeError("FLOW_PROJECT_NOT_FOUND: Could not create scoped project")
                    store.patch_row("accounts", account_id, {project_field: target_id})
                account = {**account, project_field: target_id}
            finally:
                lane_lock.release()
        legacy_id = str(account.get("projectId") or "").strip()
        if not scoped and kind == "image" and not account.get(project_field) and legacy_id:
            store.patch_row("accounts", str(account.get("id") or ""), {project_field: legacy_id})
            account = {**account, project_field: legacy_id}
            target_id = legacy_id
        elif not scoped and kind == "video" and not target_id:
            # Existing accounts have one legacy project. Preserve it for images
            # and create a dedicated gallery for video on first use.
            new_id = await self._create_flow_project_ui(page, avoid_project_id=legacy_id)
            if not new_id:
                raise RuntimeError(
                    f"FLOW_PROJECT_NOT_FOUND: Could not create the {kind} project "
                    f"(legacy id: {legacy_id or 'none'}; current url: {page.url})"
                )
            changed_at = time.time()
            patch = {
                project_field: new_id,
                f"{project_field}ChangedAt": changed_at,
                "status": "online",
                "error": None,
                "updatedAt": time.time(),
            }
            if kind == "image":
                patch["projectChangedAt"] = changed_at
            store.patch_row("accounts", str(account.get("id") or ""), patch)
            account = {**account, **patch}
            target_id = new_id
        if target_id and str(client.project_id or "") != target_id:
            self._bind_flow_project(api, client, target_id)
        try:
            await client._ensure_project_page(page)
            # A deleted project first loads its own URL, then Angular redirects to
            # /404?reason=project a few seconds later — wait for either outcome.
            for _ in range(16):
                if "/404" in str(page.url or ""):
                    break
                if await page.locator("flow-grid-tile-container, [contenteditable='true'], textarea").count():
                    break
                await asyncio.sleep(0.5)
            if "/404" not in str(page.url or ""):
                await _dismiss_flow_whats_new(page)
                # Flow is a SPA: the project URL can be ready before the
                # composer finishes mounting. Give Angular one short frame
                # before filling prompt/settings; the UI helpers still poll
                # afterwards when a cold profile needs longer.
                await asyncio.sleep(0.3)
                return account
        except RuntimeError as exc:
            if "FLOW_PROJECT_NOT_FOUND" not in str(exc):
                raise
        old_id = str(target_id or client.project_id or "")
        account_id = str(account.get("id") or "")
        with self._guard:
            recovery_lock = self._project_recovery_locks.setdefault(account_id + ":" + project_field, threading.Lock())
        while not recovery_lock.acquire(blocking=False):
            await asyncio.sleep(0.1)
        try:
            # Another worker may have repaired this account while this worker
            # was waiting. Reuse its project instead of creating a second one.
            persisted = store.get_row("accounts", account_id) or {}
            persisted_id = str(persisted.get(project_field) or "") if scoped else _flow_project_id(persisted, kind)
            if persisted_id and persisted_id != old_id:
                self._bind_flow_project(api, client, persisted_id)
                await client._ensure_project_page(page)
                await _dismiss_flow_whats_new(page)
                await asyncio.sleep(0.3)
                return {**account, **persisted}
            self._log(
            "warning",
            "project_missing_creating_replacement",
            job_id=job_id,
            account_id=str(account.get("id") or ""),
            details={"oldProjectId": old_id, "url": str(page.url or "")},
            )
            new_id = await self._create_flow_project_ui(page, avoid_project_id=old_id)
            if not new_id:
                raise RuntimeError(
                    f"FLOW_PROJECT_NOT_FOUND: Could not create a replacement project "
                    f"(old id: {old_id or 'none'}; current url: {page.url})"
                )
            changed_at = time.time()
            patch: dict[str, Any] = {
                project_field: new_id,
                "previousProjectId": old_id,
                f"{project_field}ChangedAt": changed_at,
                "status": "online",
                "error": None,
                "updatedAt": time.time(),
            }
            if kind == "image" and not scoped:
                patch["projectId"] = new_id
                patch["projectChangedAt"] = changed_at
            store.patch_row("accounts", str(account["id"]), patch)
            self._bind_flow_project(api, client, new_id)
            self._log(
                "success",
                "project_replaced",
                job_id=job_id,
                account_id=str(account.get("id") or ""),
                details={"oldProjectId": old_id, "projectId": new_id},
            )
            await client._ensure_project_page(page)
            await _dismiss_flow_whats_new(page)
            await asyncio.sleep(0.3)
            return {**account, **patch}
        finally:
            recovery_lock.release()
