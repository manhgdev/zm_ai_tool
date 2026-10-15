"""Flow prompt input, submit controls, and submission acknowledgement."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from . import store
from .constants import _FLOW_CONTROL_SELECTOR, _FLOW_SUBMIT_DELAY_MAX_S, _FLOW_SUBMIT_DELAY_MIN_S
from .controls import _flow_control_is_selected, _flow_settings_panel_open
from .errors import _flow_credit_blocked, _redact_flow_diag_url, _rpc_public_error
from .media import _captured_image_items, _captured_video_ids

_log = logging.getLogger("pipeline.flow.service")


def _flow_submit_button_score(text: str = "", aria_label: str = "", title: str = "") -> int:
    """Score the current Flow submit control across locales and UI builds."""
    text_value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    aria_value = re.sub(r"\s+", " ", f"{aria_label or ''} {title or ''}").strip().lower()
    if re.search(r"bắt đầu tạo|tạo ảnh|tạo video|start creating|start generation|generate now", aria_value):
        return 100
    if text_value == "arrow_forward" or "arrow_forward" in text_value:
        return 90
    if re.search(r"\b(create|generate|submit|run)\b", aria_value):
        return 80
    if re.search(r"bắt đầu tạo|tạo ảnh|tạo video|\b(create|generate|submit|run)\b", text_value):
        return 70
    return 0


def _image_input_type_for_mode(mode: str) -> str:
    """Flow batchGenerateImages: edit = base image; reference = reference inputs."""
    return "IMAGE_INPUT_TYPE_BASE_IMAGE" if mode == "edit" else "IMAGE_INPUT_TYPE_REFERENCE"


def _prompt_with_reference_strength(prompt: str, mode: str, strength: Any) -> str:
    """Flow has no public strength field — steer adherence via prompt when refs are used."""
    text = str(prompt or "").strip()
    if mode not in {"edit", "reference"}:
        return text
    try:
        value = max(0, min(100, int(strength)))
    except (TypeError, ValueError):
        value = 70
    if mode == "edit":
        if value >= 75:
            hint = "Edit the source image while preserving identity, composition, and key details."
        elif value >= 40:
            hint = "Edit the source image; keep the main subject recognizable while allowing creative changes."
        else:
            hint = "Use the source image only as loose inspiration; prioritize the prompt."
    else:
        if value >= 75:
            hint = "Strictly preserve character identity, wardrobe, and style from the reference image(s)."
        elif value >= 40:
            hint = "Keep the subject and style from the reference image(s) while following the prompt."
        else:
            hint = "Loosely inspired by the reference image(s); prioritize the prompt."
    if hint.lower() in text.lower():
        return text
    return f"{hint} {text}".strip()


def _patch_batch_generate_image_inputs(body: dict[str, Any], *, mode: str, media_names: list[str]) -> dict[str, Any]:
    """Ensure imageInputs use the correct type for edit vs reference."""
    if not isinstance(body, dict):
        return body
    input_type = _image_input_type_for_mode(mode)
    names = [str(name).strip() for name in media_names if str(name).strip()]
    requests = body.get("requests")
    if not isinstance(requests, list):
        return body
    for request in requests:
        if not isinstance(request, dict):
            continue
        inputs = request.get("imageInputs")
        if not isinstance(inputs, list):
            inputs = []
        if not inputs and names:
            capped = names[:1] if mode == "edit" else names[:3]
            inputs = [
                {
                    "name": name,
                    "mediaName": name,
                    "imageInputType": input_type,
                    "role": "REFERENCE_IMAGE",
                }
                for name in capped
            ]
        else:
            for item in inputs:
                if isinstance(item, dict):
                    item["imageInputType"] = input_type
                    item.setdefault("role", "REFERENCE_IMAGE")
                    if item.get("mediaName") and not item.get("name"):
                        item["name"] = item["mediaName"]
        request["imageInputs"] = inputs
    return body


async def _wait_before_flow_submit() -> float:
    """Jitter only the submit edge; leave concurrency/render/download unchanged."""
    delay = random.uniform(_FLOW_SUBMIT_DELAY_MIN_S, _FLOW_SUBMIT_DELAY_MAX_S)
    await asyncio.sleep(delay)
    return delay


class FlowSubmissionMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    # Score + click the Flow submit control in one DOM pass. Avoids Playwright
    # locators like buttons.nth(109) that go stale when Angular re-renders.
    _FLOW_SUBMIT_DOM_SCRIPT = """(doClick) => {
        const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0
                && style.visibility !== 'hidden' && style.display !== 'none';
        };
        const scoreOf = (btn) => {
            const text = (btn.innerText || btn.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const aria = `${btn.getAttribute('aria-label') || ''} ${btn.getAttribute('title') || ''}`
                .replace(/\\s+/g, ' ').trim().toLowerCase();
            if (/bắt đầu tạo|tạo ảnh|tạo video|start creating|start generation|generate now/.test(aria)) return 100;
            if (text === 'arrow_forward' || text.includes('arrow_forward')) return 90;
            if (/\\b(create|generate|submit|run)\\b/.test(aria)) return 80;
            if (/bắt đầu tạo|tạo ảnh|tạo video|\\b(create|generate|submit|run)\\b/.test(text)) return 70;
            return 0;
        };
        let best = null;
        let bestScore = 0;
        const labels = [];
        for (const el of document.querySelectorAll('[data-zm-flow-submit]')) {
            el.removeAttribute('data-zm-flow-submit');
        }
        for (const btn of document.querySelectorAll('button, [role="button"]')) {
            if (!visible(btn)) continue;
            const label = ((btn.getAttribute('aria-label') || btn.innerText || '') + '').trim().slice(0, 30);
            if (label && labels.length < 15) labels.push(label);
            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
            const score = scoreOf(btn);
            if (score > bestScore) {
                bestScore = score;
                best = btn;
            }
        }
        if (!best) return { clicked: false, score: 0, disabled: false, labels, marked: false };
        best.setAttribute('data-zm-flow-submit', '1');
        if (doClick) best.click();
        return {
            clicked: Boolean(doClick),
            score: bestScore,
            disabled: Boolean(best.disabled || best.getAttribute('aria-disabled') === 'true'),
            label: ((best.getAttribute('aria-label') || best.innerText || '') + '').trim().slice(0, 40),
            labels,
            marked: true,
        };
    }"""

    async def _flow_submit_dom(self, page, *, click: bool = False) -> dict[str, Any]:
        """Resolve (and optionally click) the best Flow submit control in-page."""
        try:
            result = await page.evaluate(self._FLOW_SUBMIT_DOM_SCRIPT, click)
        except Exception:
            return {"clicked": False, "score": 0, "disabled": False, "labels": [], "marked": False}
        return result if isinstance(result, dict) else {"clicked": False, "score": 0, "disabled": False, "labels": [], "marked": False}

    async def _close_flow_settings_panel(self, page, ui=None) -> None:
        """Dismiss quick-settings so the Create control receives the submit click."""
        for _ in range(4):
            if not await _flow_settings_panel_open(page, ui):
                return
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await asyncio.sleep(0.35)
            try:
                backdrop = page.locator(".cdk-overlay-backdrop")
                if await backdrop.count() > 0 and await backdrop.first.is_visible():
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.25)
            except Exception:
                pass

    async def _force_click_marked_flow_submit(self, page) -> bool:
        """Playwright force-click the button marked by ``_flow_submit_dom``."""
        try:
            marked = page.locator('[data-zm-flow-submit="1"]').first
            if await marked.count() == 0:
                ready = await self._flow_submit_dom(page, click=False)
                if not ready.get("marked"):
                    return False
            await page.locator('[data-zm-flow-submit="1"]').first.click(force=True, timeout=3_000)
            return True
        except Exception as exc:
            _log.warning("_force_click_marked_flow_submit: %s", exc)
            return False

    async def _submit_failure_diagnostics(
        self,
        page,
        rpc_requests: list[Any],
        rpc_responses: list[Any],
        other_rpc: list[Any] | None = None,
        ui=None,
    ) -> dict[str, Any]:
        """Redacted snapshot for FLOW_SUBMIT_NOT_CONFIRMED handoff."""
        diag: dict[str, Any] = {}
        try:
            state = await self._flow_submit_dom(page, click=False)
            diag["submit"] = {
                "score": state.get("score"),
                "label": state.get("label"),
                "disabled": state.get("disabled"),
                "labels": list(state.get("labels") or [])[:15],
            }
        except Exception:
            pass
        try:
            diag["settingsOpen"] = await _flow_settings_panel_open(page, ui)
        except Exception:
            diag["settingsOpen"] = None
        try:
            composer = await page.evaluate(
                """() => [...document.querySelectorAll(
                    '[role="textbox"], textarea, [contenteditable="true"]'
                )].map(node => node.value || node.innerText || node.textContent || '').join(' ').trim()"""
            )
            diag["composer"] = re.sub(r"\s+", " ", str(composer or "")).strip()[:200]
        except Exception:
            pass

        def _urls(items: list[Any], *, with_status: bool = False) -> list[Any]:
            out: list[Any] = []
            for item in list(items or [])[:8]:
                url = _redact_flow_diag_url(getattr(item, "url", "") or "")
                if with_status:
                    try:
                        out.append({"url": url, "status": getattr(item, "status", None)})
                    except Exception:
                        out.append({"url": url, "status": None})
                else:
                    out.append(url)
            return out

        diag["rpcRequestUrls"] = _urls(rpc_requests)
        diag["rpcResponses"] = _urls(rpc_responses, with_status=True)
        diag["otherRpc"] = _urls(other_rpc or [], with_status=True)
        try:
            shot = Path(tempfile.gettempdir()) / f"zm-flow-submit-fail-{uuid.uuid4().hex[:10]}.png"
            await page.screenshot(path=str(shot), full_page=False)
            diag["screenshot"] = str(shot)
        except Exception:
            pass
        return diag

    async def _click_flow_submit(
        self, page, *, ui=None, job_id: str | None = None, account_id: str | None = None,
    ) -> list[Any]:
        """Click the submit control used by the current Flow project page.

        Flow moved from an English ``Create`` button to a localized Material
        icon button (``Bắt đầu tạo``/``arrow_forward``).  The upstream
        flow-py client only checks the old label and then waits for a network
        endpoint that no longer exists.  Failing at the click boundary keeps
        a UI mismatch visible instead of leaving a job at 5% for five minutes.
        """
        if job_id and account_id:
            current = store.get_row("jobs", job_id) or {}
            self._assert_session_generation(account_id, int(current.get("sessionGeneration") or 0))
        # Submit stays disabled while an attached frame is still processing.
        deadline = time.monotonic() + 60
        escaped = False
        ready: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                body_text = await page.locator("body").inner_text()
                if _flow_credit_blocked(body_text):
                    raise RuntimeError(
                        "FLOW_CREDITS_INSUFFICIENT: Không có đủ tín dụng để thực hiện hành động này; "
                        "hãy chọn model/cấu hình ít tốn credits hơn."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
            ready = await self._flow_submit_dom(page, click=False)
            if int(ready.get("score") or 0) > 0:
                break
            if not escaped and time.monotonic() > deadline - 40:
                escaped = True
                try:
                    await self._close_flow_settings_panel(page, ui)
                except Exception:
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
            await asyncio.sleep(1)
        else:
            labels = list(ready.get("labels") or [])
            raise RuntimeError(
                "FLOW_UI_CHANGED: submit button was not found or is disabled; "
                f"refresh the Flow project and retry (visible buttons: {labels[:15]})"
            )
        try:
            baseline_tile_count = await page.locator("flow-grid-tile-container").count()
        except Exception:
            baseline_tile_count = 0
        try:
            baseline_composer_text = await page.evaluate("""() => [...document.querySelectorAll(
                '[role="textbox"], textarea, [contenteditable="true"]'
            )].map(node => node.value || node.innerText || node.textContent || '').join(' ').trim()""")
        except Exception:
            baseline_composer_text = ""
        baseline_errors = {str(item.get("text") or "") for item in await self._scan_visible_errors(page, 24)}
        # The generate RPC is the ground truth: DOM signals can lag or be
        # absent (virtualized grid, composer not cleared) while Flow already
        # accepted and billed the generation.
        rpc_requests: list[Any] = []
        rpc_responses: list[Any] = []
        other_rpc: list[Any] = []

        def _is_generate_rpc(url: str) -> bool:
            # flow.google.com sends image generation as batchexecute rpcids=YhhmEf.
            return "batchAsyncGenerateVideo" in url or "batchGenerateImages" in url or "rpcids=YhhmEf" in url

        def _on_request(request) -> None:
            if _is_generate_rpc(request.url):
                rpc_requests.append(request)

        def _on_response(response) -> None:
            url = response.url
            if _is_generate_rpc(url):
                rpc_responses.append(response)
            elif "batchexecute" in url:
                other_rpc.append(response)

        page.on("request", _on_request)
        page.on("response", _on_response)
        try:
            await self._click_flow_submit_and_confirm(
                page, baseline_tile_count, baseline_composer_text,
                baseline_errors, rpc_requests, rpc_responses, other_rpc,
                ui=ui, job_id=job_id, account_id=account_id,
            )
        finally:
            page.remove_listener("request", _on_request)
        # The response listener stays attached: each job owns its Chrome page,
        # so a late generate response still belongs to this job.
        return rpc_responses

    @staticmethod
    def _clear_unsent_submission(job_id: str) -> None:
        """Drop submission markers when no generate RPC left the browser."""
        store.patch_row("jobs", job_id, {
            "submissionStartedAt": None,
            "submissionProjectId": None,
            "baselineMediaIds": [],
            "baselineThumbs": [],
            "resumeOnly": False,
            "updatedAt": time.time(),
        })

    @staticmethod
    async def _rpc_media(responses: list[Any]) -> tuple[list[dict[str, str]], list[str]]:
        """Media created by this page's generate RPCs: (image items with src, all media names).

        Ownership comes from the RPC itself because Flow may store a rewritten
        prompt (e.g. a short English title), which defeats prompt matching.
        """
        items: list[dict[str, str]] = []
        names: list[str] = []
        for response in list(responses):
            try:
                if not response.ok:
                    continue
                body = await response.json()
            except Exception:
                continue
            if not isinstance(body, dict):
                continue
            items.extend(_captured_image_items(body))
            names.extend(_captured_video_ids(body))
        names.extend(item["id"] for item in items)
        return list({item["id"]: item for item in items}.values()), list(dict.fromkeys(names))

    async def _click_flow_submit_and_confirm(
        self, page, baseline_tile_count: int, baseline_composer_text: str,
        baseline_errors: set[str], rpc_requests: list[Any], rpc_responses: list[Any],
        other_rpc: list[Any] | None = None, *, ui=None, job_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        # Re-resolve + DOM click each attempt. Holding buttons.nth(N) across
        # awaits fails when Flow's Angular tree re-renders (nth(109) timeout).
        last_exc: Exception | None = None
        clicked = False
        force_clicks = 0
        for attempt in range(3):
            try:
                result = await self._flow_submit_dom(page, click=True)
                if int(result.get("score") or 0) <= 0 or not result.get("clicked"):
                    raise RuntimeError(
                        "submit button was not found or is disabled "
                        f"(attempt {attempt + 1}/3)"
                    )
                clicked = True
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.4)
        if not clicked:
            raise RuntimeError(f"FLOW_SUBMIT_CLICK_FAILED: {last_exc}") from last_exc
        # A successful click must produce an observable Flow acknowledgement.
        # Do not label the job as ``generating`` merely because Playwright's
        # click returned; otherwise a rejected/no-op submit sits at 15% until
        # the generic generation timeout hides the real cause.
        clicked_at = time.monotonic()
        ack_deadline = clicked_at + 8
        rpc_deadline = clicked_at + 30
        while time.monotonic() < (rpc_deadline if rpc_requests else ack_deadline):
            for response in list(other_rpc or []) + list(rpc_responses):
                try:
                    detail = re.sub(r"\s+", " ", await response.text())[:2000]
                except Exception:
                    detail = ""
                classified = _rpc_public_error(detail)
                if classified:
                    raise RuntimeError(classified)
            for response in rpc_responses:
                try:
                    detail = re.sub(r"\s+", " ", await response.text())[:2000]
                except Exception:
                    detail = ""
                # An unreadable HTTP 200 is not proof of acceptance; wait for
                # media names, a later PUBLIC_ERROR, or a visible Flow error.
                if response.ok and detail and not _rpc_public_error(detail):
                    return
            try:
                await self._raise_if_new_flow_error(page, baseline_errors)
                body = await page.locator("body").inner_text()
                if _flow_credit_blocked(body):
                    raise RuntimeError("FLOW_CREDITS_INSUFFICIENT: Flow rejected the submit because credits are insufficient")
                if rpc_requests:
                    # Sent but not answered yet: never fall back to a resubmit.
                    await asyncio.sleep(0.5)
                    continue
                # DOM click can be a no-op under an open settings overlay.
                # One Playwright force-click only — never after a generate RPC.
                if force_clicks == 0 and time.monotonic() >= clicked_at + 1.75:
                    force_clicks += 1
                    await self._force_click_marked_flow_submit(page)
                    await asyncio.sleep(0.4)
                    continue
                visible_tiles = await page.locator("flow-grid-tile-container").count()
                progressbars = await page.locator("flow-grid-tile-container [role='progressbar'], flow-grid-tile-container [aria-valuenow], flow-pending-tile").count()
                if visible_tiles > baseline_tile_count or progressbars:
                    await self._raise_if_new_flow_error(page, baseline_errors)
                    return
                submit_state = await self._flow_submit_dom(page, click=False)
                if submit_state.get("disabled") or int(submit_state.get("score") or 0) <= 0:
                    await self._raise_if_new_flow_error(page, baseline_errors)
                    return
                # Flow clears the composer as soon as the request is accepted;
                # this signal often arrives before the tile/progress DOM.
                composer_text = await page.evaluate("""() => [...document.querySelectorAll(
                    \"[role='textbox'], textarea, [contenteditable='true']\"
                )].map(node => node.value || node.innerText || node.textContent || '').join(' ').trim()""")
                if baseline_composer_text and not composer_text:
                    await self._raise_if_new_flow_error(page, baseline_errors)
                    return
            except RuntimeError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await self._raise_if_new_flow_error(page, baseline_errors)
        if rpc_requests:
            # The RPC left the browser; Flow's grid polling owns the result now.
            return
        diag = await self._submit_failure_diagnostics(
            page, rpc_requests, rpc_responses, other_rpc, ui=ui,
        )
        self._log(
            "warning", "submit_not_confirmed",
            job_id=job_id or "", account_id=account_id or "",
            details=diag,
        )
        raise RuntimeError(
            "FLOW_SUBMIT_NOT_CONFIRMED: Flow did not acknowledge the submit "
            "(no generate request was sent and no new generation tile appeared within 8 seconds)"
        )

    async def _ensure_flow_prompt_ready(self, page, ui, prompt: str) -> None:
        """Re-focus and verify the composer immediately before Submit."""
        wanted = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if not wanted:
            raise RuntimeError("FLOW_PROMPT_NOT_CONFIRMED: prompt is empty before submit")
        for _ in range(3):
            if await ui.fill_prompt(page, wanted):
                try:
                    actual = await page.evaluate("""() => {
                        const nodes = [
                            ...document.querySelectorAll('[role="textbox"], textarea, [contenteditable="true"]'),
                        ];
                        return nodes.map((node) => node.value || node.innerText || node.textContent || '').join(' ');
                    }""")
                    normalized = re.sub(r"\s+", " ", str(actual or "")).strip()
                    if wanted in normalized or normalized in wanted and len(normalized) >= min(24, len(wanted)):
                        return
                except Exception:
                    pass
            await asyncio.sleep(0.7)
        raise RuntimeError(
            "FLOW_PROMPT_NOT_CONFIRMED: prompt text was not present in the Flow composer before submit"
        )

    async def _set_flow_count(self, page, count: int) -> None:
        """Select the generation count in either the old tab or new radio UI."""
        desired = max(1, min(4, int(count or 1)))
        target = page.locator(_FLOW_CONTROL_SELECTOR).filter(
            has_text=re.compile(rf"^\s*x{desired}\s*$", re.IGNORECASE),
        )
        for index in range(await target.count()):
            candidate = target.nth(index)
            if not await candidate.is_visible():
                continue
            # Re-apply the count for every new prompt. A stale xN summary can
            # survive a tab reuse even when the active composer has not taken
            # the setting for this job.
            await candidate.click(force=True)
            await asyncio.sleep(0.25)
            if await _flow_control_is_selected(candidate):
                return
        if desired == 1 and await target.count() == 0:
            # Some Flow layouts omit the count control because x1 is the only
            # supported choice. Only then is proceeding without a click safe.
            _log.info("_set_flow_count: x1 is the only available generation count")
            return
        raise RuntimeError(f"FLOW_UI_CHANGED: generation count x{desired} was not found")
