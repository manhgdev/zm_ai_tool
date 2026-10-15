"""Flow generation controls: mode, model, ratio, duration, and resolution."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .constants import _DURATION_UNITS, _FLOW_CONTROL_SELECTOR
from .models import _match_model_choice, _video_ui_resolution

_log = logging.getLogger("pipeline.flow.service")


def _mode_tab_icon(kind: str) -> str:
    """Material symbols used by Flow's language-independent mode tabs."""
    return "image" if kind == "image" else "videocam"


def _duration_pattern(duration: str) -> re.Pattern[str]:
    """Match Flow duration labels in English and Vietnamese."""
    return re.compile(
        rf"(?<!\d){re.escape(str(duration).strip())}\s*{_DURATION_UNITS}(?!\w)",
        re.IGNORECASE,
    )


def _flow_control_selected_from_attrs(
    *,
    aria_selected: str | None,
    aria_checked: str | None,
    data_state: str | None,
    aria_pressed: str | None = None,
) -> bool:
    """Return whether a Flow tab/radio reports the selected state."""
    state = str(data_state or "").strip().lower()
    return (
        str(aria_selected or "").strip().lower() == "true"
        or str(aria_checked or "").strip().lower() == "true"
        or str(aria_pressed or "").strip().lower() == "true"
        or state in {"checked", "on", "active", "selected"}
    )


async def _flow_control_is_selected(control) -> bool:
    """Read selection state across Flow's old and new control markup."""
    try:
        attrs_selected = _flow_control_selected_from_attrs(
            aria_selected=await control.get_attribute("aria-selected"),
            aria_checked=await control.get_attribute("aria-checked"),
            data_state=await control.get_attribute("data-state"),
            aria_pressed=await control.get_attribute("aria-pressed"),
        )
        if attrs_selected:
            return True
        aria_current = str(await control.get_attribute("aria-current") or "").lower()
        css_class = str(await control.get_attribute("class") or "").lower()
        if aria_current in {"true", "page", "step"} or bool(
            re.search(r"(?:^|[ _-])(selected|active|checked)(?:$|[ _-])", css_class)
        ):
            return True
        # Angular Material/Flow builds sometimes expose the state only on a
        # nested input or through the host's checked pseudo-state.
        # mat-button-toggle puts mat-button-toggle-checked on the host element
        # while visible_duration_tab resolves to the inner button child, so we
        # must also walk ancestors via closest().
        return bool(await control.evaluate("""(node) => Boolean(
          node.matches(':checked, .mat-button-toggle-checked, [aria-selected="true"], [aria-checked="true"], [data-state="on"]')
          || node.querySelector(':checked, .mat-button-toggle-checked, [aria-selected="true"], [aria-checked="true"], [data-state="on"]')
          || node.closest('.mat-button-toggle-checked, [aria-selected="true"], [aria-checked="true"], [data-state="on"]')
        )"""))
    except Exception:
        return False


def _is_settings_trigger(text: str, aria_label: str = "") -> bool:
    """Identify the generation settings pill without clicking grid settings."""
    text = str(text or "")
    aria_label = str(aria_label or "")
    combined = f"{aria_label} {text}"
    if re.search(r"\bx[1-4]\b", text, re.IGNORECASE):
        return True
    if re.search(r"\b(?:Nano Banana|Imagen|Veo|Video|Image|H\u00ecnh \u1ea3nh|16:9|9:16|1:1)\b", combined, re.IGNORECASE):
        return True
    return bool(re.search(
        r"(?:điều kiện kích hoạt|generation settings|trigger settings)",
        combined,
        re.IGNORECASE,
    ))


async def _click_settings_pill(pill, tabs) -> bool:
    """Open Flow settings, retrying with a forced click for Windows DPI."""
    for force in (False, True):
        try:
            await pill.click(force=force)
        except Exception:
            if force:
                return False
            continue
        await asyncio.sleep(.35)
        for index in range(await tabs.count()):
            if await tabs.nth(index).is_visible():
                return True
    return False


async def _dismiss_flow_whats_new(page) -> None:
    """Acknowledge an optional announcement, never the generation button."""
    get_by_role = getattr(page, "get_by_role", None)
    if get_by_role is None:
        return
    buttons = get_by_role("button", name=re.compile(r"^(?:Bắt đầu|Get started|Start)$", re.I))
    try:
        button_count = buttons.count()
        if not isinstance(button_count, int):
            button_count = await button_count
        button_count = int(button_count)
    except Exception:
        return
    for index in range(button_count):
        button = buttons.nth(index)
        if await button.is_visible() and await button.is_enabled():
            await button.click(timeout=5_000)
            await button.wait_for(state="hidden", timeout=10_000)
            return


async def _disable_flow_agent_mode(page) -> None:
    """Return from an Agent session to the normal generation composer."""
    try:
        # Wait for hydration before looking for the session X. The normal
        # settings pill remains mounted but hidden while a session is open.
        await page.locator(
            'button.header-action[aria-label="Đóng"]:visible, '
            'button.header-action[aria-label="Close"]:visible, '
            'button.agent-mode-chip:visible, .settings-trigger-button:visible'
        ).first.wait_for(state="visible", timeout=15_000)
    except Exception:
        return
    await _dismiss_flow_whats_new(page)
    close = page.locator('button.header-action')
    for index in range(await close.count()):
        candidate = close.nth(index)
        label = await candidate.get_attribute("aria-label") or ""
        if await candidate.is_visible() and re.fullmatch(r"Đóng|Close", label, re.I):
            await candidate.click(timeout=5_000)
            await page.locator('.settings-trigger-button:visible').first.wait_for(
                state="visible", timeout=10_000,
            )
            break
    chip = page.locator("button.agent-mode-chip[aria-pressed='true']:visible")
    if await chip.count():
        await chip.first.click()
        await asyncio.sleep(0.8)


async def _open_flow_settings_panel(page, pill, tabs, ui=None) -> bool:
    """Prefer Playwright clicks, then reuse flow-py's DOM-click fallback."""
    if pill is not None and await _click_settings_pill(pill, tabs):
        return True
    if ui is None:
        return False
    try:
        return bool(await ui.open_settings_panel(page))
    except Exception:
        return False


async def _flow_settings_panel_open(page, ui=None) -> bool:
    """True when Flow's mode/model/ratio quick-settings panel is on screen."""
    if ui is not None:
        try:
            return bool(await ui._settings_visible(page))
        except Exception:
            pass
    try:
        count = await page.evaluate(
            r"""() => {
                const tabs = [...document.querySelectorAll('[role="tab"], [role="radio"]')];
                return tabs.filter((node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    const label = `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''} ${node.getAttribute('data-value') || ''} ${node.getAttribute('data-mode') || ''} ${node.textContent || ''}`;
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && /(?:image|hình ảnh|video|frames|x[1-4])\b/i.test(label);
                }).length;
            }"""
        )
        return int(count or 0) > 0
    except Exception:
        return False


class FlowControlsMixin:
    """Methods composed into FlowService; shared state is initialized by FlowService."""

    async def _prepare_video_mode(self, page, model: str) -> None:
        """Open Flow's live settings popover and verify Video mode.

        flow-py's JavaScript click can race the localized Agent UI and leave
        Image mode selected. Use trusted Playwright clicks against the controls
        observed in the current account instead.
        """
        model_pills = page.locator('button[aria-haspopup="menu"]')
        visible_pill = None
        for index in range(await model_pills.count() - 1, -1, -1):
            candidate = model_pills.nth(index)
            label = (await candidate.inner_text()).strip()
            # x[1-4]\b (no leading \b): matches both "x1" and "crop_16_9x1"
            if await candidate.is_visible() and re.search(r"x[1-4]\b", label):
                visible_pill = candidate
                break
        if visible_pill is None:
            raise RuntimeError("FLOW_UI_CHANGED: generation settings control was not found")
        await visible_pill.click()
        await asyncio.sleep(1.0)
        # Tab "Video" — dùng regex để catch cả localized variants; fallback force-click nếu is_visible() fail (Windows DPI)
        video_tab_loc = page.locator('[role="tab"]').filter(has=page.locator('text=/^Video/i'))
        if await video_tab_loc.count() == 0:
            # Thử regex rộng hơn: bất kỳ tab nào KHÔNG phải Image/Hình/S/M/L/Landscape/Portrait/Square
            all_tabs = page.locator('[role="tab"]')
            await all_tabs.first.wait_for(state="attached", timeout=10_000)
            video_tab_loc = all_tabs.filter(has=page.locator('text=/^Video/i'))
        if await video_tab_loc.count() == 0:
            tab_texts = []
            all_tabs2 = page.locator('[role="tab"]')
            for i in range(min(10, await all_tabs2.count())):
                try:
                    tab_texts.append((await all_tabs2.nth(i).inner_text()).strip())
                except Exception:
                    pass
            raise RuntimeError(f"FLOW_UI_CHANGED: Video mode tab was not found — tabs={tab_texts}")
        # Force click — is_visible() unreliable trên Windows DPI
        await video_tab_loc.first.click(force=True)
        await asyncio.sleep(0.6)

        setting_pills = page.locator('button[aria-haspopup="menu"]')
        video_summary = setting_pills.filter(has_text=re.compile(r"Video", re.I)).last
        if await video_summary.count() == 0 or not await video_summary.is_visible():
            raise RuntimeError("FLOW_MODE_MISMATCH: Flow did not switch from Image to Video")

        model_buttons = setting_pills.filter(has_text=re.compile(r"Omni|Veo", re.I))
        if await model_buttons.count() == 0:
            raise RuntimeError("FLOW_UI_CHANGED: video model selector was not found")
        model_button = model_buttons.last
        current_model = (await model_button.inner_text()).strip()
        if not _match_model_choice(model, current_model):
            await model_button.click()
            await asyncio.sleep(0.25)
            choices = page.locator('[role="menuitem"], [role="option"]')
            selected = False
            for index in range(await choices.count()):
                choice = choices.nth(index)
                text = (await choice.inner_text()).strip()
                if await choice.is_visible() and _match_model_choice(model, text):
                    await choice.click()
                    selected = True
                    break
            if not selected:
                raise RuntimeError(f"FLOW_MODEL_UNAVAILABLE: {model}")

    async def _prepare_ui_model(self, page, kind: str, model: str, *, ui=None) -> None:  # noqa: C901
        """Select a current Flow UI model for models without a stable REST key.

        Strategy:
        1. Ensure settings panel is open via pill click (x[1-4] or any visible pill).
        2. Switch to the correct mode tab (Image/Video).
        3. Click the model family selector and pick the right option.
        """
        current_url = str(page.url or "")
        if "accounts.google.com" in current_url or "/about" in current_url or "flow.google.com/about" in current_url:
            raise RuntimeError(
                f"FLOW_LOGIN_REQUIRED: Google session expired or redirected to {current_url}; please reconnect the account in Settings"
            )
        await _disable_flow_agent_mode(page)

        try:
            await page.wait_for_selector('button', timeout=30_000, state="attached")
        except Exception:
            _log.warning("_prepare_ui_model: settings pill not found within 30s — proceeding anyway")

        family = re.compile(r"Nano Banana|Imagen", re.I) if kind == "image" else re.compile(r"Omni|Veo", re.I)
        mode_pattern = (
            re.compile(r"Image|H\u00ecnh \u1ea3nh", re.I) if kind == "image"
            else re.compile(r"Video", re.I)
        )
        controls = page.locator(_FLOW_CONTROL_SELECTOR)

        async def _visible_mode_tab():
            # Current Flow exposes the mode controls as radios whose accessible
            # name is localized ("Hình ảnh"), while their inner text may only
            # contain an icon. Prefer the accessibility name before text.
            for role in ("radio", "tab"):
                named = page.get_by_role(role, name=mode_pattern).first
                if await named.count() > 0 and await named.is_visible():
                    return named

            loc = controls.filter(has_text=mode_pattern)
            # 1. Prefer truly visible tab
            for index in range(await loc.count()):
                candidate = loc.nth(index)
                if await candidate.is_visible():
                    return candidate
            # 2. Fallback: Windows DPI can make is_visible() unreliable — use first attached tab
            if await loc.count() > 0:
                _log.warning("_visible_mode_tab: %s tab not visible but attached — DPI fallback", kind)
                return loc.first
            icon_tabs = controls.filter(
                has=page.locator("i", has_text=re.compile(rf"^{_mode_tab_icon(kind)}$", re.I)),
            )
            for index in range(await icon_tabs.count()):
                candidate = icon_tabs.nth(index)
                if await candidate.is_visible():
                    return candidate
            if await icon_tabs.count() > 0:
                _log.warning("_visible_mode_tab: %s icon tab not visible but attached — DPI fallback", kind)
                return icon_tabs.first
            return None

        async def _model_controls():
            candidates = page.locator('button, [role="button"]')
            found = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                text = (await candidate.inner_text()).strip()
                aria_label = await candidate.get_attribute("aria-label") or ""
                combined = f"{text} {aria_label}"
                if family.search(combined) or re.search(
                    r"chọn nhóm mô hình|select model|model group|model selector",
                    combined,
                    re.I,
                ):
                    found.append(candidate)
            return found

        async def _model_pill_already_visible() -> bool:
            return bool(await _model_controls())

        async def _model_pill_matches(target_model: str) -> bool:
            """True when the visible model pill already shows the desired model."""
            for candidate in await _model_controls():
                text = (await candidate.inner_text()).strip()
                aria_label = await candidate.get_attribute("aria-label") or ""
                if _match_model_choice(target_model, f"{text} {aria_label}"):
                    return True
            return False

        # ------------------------------------------------------------------
        # Step 2: switch to the correct mode tab (Image / Video)
        # ------------------------------------------------------------------
        # Focus prompt input first so clicking the settings pill opens the
        # settings panel instead of the reference-image media picker.
        try:
            ed = page.locator('textarea, [contenteditable="true"][role="textbox"], div[contenteditable]').first
            if await ed.count() > 0:
                await ed.click()
                await asyncio.sleep(0.3)
        except Exception:
            pass

        switched = False
        if ui is not None:
            try:
                from ._flow._models import GenerationMode
                gen_mode = GenerationMode.IMAGE if kind == "image" else GenerationMode.VIDEO
                switched = await ui.switch_mode(page, gen_mode)
            except Exception:
                switched = False

        if not switched:
            # Fallback: find the tab by text and click it.
            mode_tab = await _visible_mode_tab()
            if mode_tab is None:
                for attempt in range(3):
                    try:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass

                    # Critical: focus the prompt textarea before clicking the settings
                    # pill.  In the current Flow UI the pill only opens the mode/model/
                    # aspect panel when the prompt input already has focus; otherwise it
                    # opens the reference-image media picker.
                    try:
                        ed = page.locator('textarea, [contenteditable="true"][role="textbox"], div[contenteditable]').first
                        if await ed.count() > 0:
                            await ed.click()
                            await asyncio.sleep(0.4)
                    except Exception:
                        pass

                    pills = page.locator("button, [role='button']")
                    trigger = None
                    trigger_deadline = time.monotonic() + 8
                    while trigger is None and time.monotonic() < trigger_deadline:
                        for index in range(await pills.count() - 1, -1, -1):
                            candidate = pills.nth(index)
                            try:
                                role = await candidate.get_attribute("role")
                                if role in {"tab", "radio"} or not await candidate.is_visible():
                                    continue
                                text = (await candidate.inner_text()).strip()
                                aria_label = await candidate.get_attribute("aria-label") or ""
                                if _is_settings_trigger(text, aria_label):
                                    trigger = candidate
                                    break
                            except Exception:
                                continue
                        if trigger is None:
                            await asyncio.sleep(0.25)
                    if trigger is None:
                        _log.warning("_prepare_ui_model attempt %d: no settings pill found", attempt + 1)
                    opened = await _open_flow_settings_panel(page, trigger, controls, ui)
                    if not opened:
                        _log.warning(
                            "_prepare_ui_model attempt %d: settings pill did not open panel",
                            attempt + 1,
                        )
                    await asyncio.sleep(1.2)
                    mode_tab = await _visible_mode_tab()
                    if mode_tab is not None:
                        break

                    tab_texts: list[str] = []
                    for idx in range(min(10, await controls.count())):
                        try:
                            tab_texts.append((await controls.nth(idx).inner_text()).strip())
                        except Exception:
                            pass
                    _log.warning(
                        "_prepare_ui_model attempt %d: no %s tab visible; tabs=%s",
                        attempt + 1, kind, tab_texts,
                    )

            if mode_tab is None:
                current_url = str(page.url or "")
                if "accounts.google.com" in current_url or "/about" in current_url or "flow.google.com/about" in current_url:
                    raise RuntimeError(
                        f"FLOW_LOGIN_REQUIRED: Google session expired or redirected to {current_url}; please reconnect the account in Settings"
                    )
                raise RuntimeError(f"FLOW_UI_CHANGED: {kind} mode tab was not found")
            elif not await _flow_control_is_selected(mode_tab):
                await mode_tab.click(force=True)
                await asyncio.sleep(0.6)



        # Verify the requested mode is really active (switch_mode may report
        # success while the page is still on the other mode).
        for _mode_check in range(3):
            try:
                verify_tab = await _visible_mode_tab()
                if verify_tab is None or await _flow_control_is_selected(verify_tab):
                    break
                await verify_tab.click(force=True)
                await asyncio.sleep(0.8)
            except Exception:
                break

        # ------------------------------------------------------------------
        # Step 3: find and click model family selector, pick the right option
        # ------------------------------------------------------------------
        selector = None
        model_controls = await _model_controls()
        if model_controls:
            selector = model_controls[-1]
        if selector is None:
            raise RuntimeError(f"FLOW_UI_CHANGED: {kind} model selector was not found")
        # Always reopen and choose the requested option. Flow can keep a stale
        # settings summary after a previous prompt, so a matching label alone
        # is not proof that this job's settings were applied to its composer.
        choices = page.locator('[role="menuitem"], [role="option"]')
        selected = False
        visible_texts: list[str] = []
        for menu_attempt in range(3):
            await selector.click()
            await asyncio.sleep(0.5 + menu_attempt * 0.7)
            for index in range(await choices.count()):
                choice = choices.nth(index)
                text = (await choice.inner_text()).strip()
                if await choice.is_visible() and _match_model_choice(model, text):
                    # Flow's nested CDK overlays can intercept a coordinate click.
                    # Dispatch on the visible option, then wait for its menu to close.
                    await choice.dispatch_event("click", timeout=5_000)
                    await choice.wait_for(state="hidden", timeout=10_000)
                    selected = True
                    break
            if selected:
                break
            visible_texts = []
            for index in range(await choices.count()):
                choice = choices.nth(index)
                if await choice.is_visible():
                    visible_texts.append((await choice.inner_text()).strip())
            if visible_texts:
                break  # menu is open and the model really is absent
            await page.keyboard.press("Escape")  # menu never opened: close and retry
        if not selected:
            raise RuntimeError(f"FLOW_MODEL_UNAVAILABLE: {model} — available: {visible_texts}")
        if not await _model_pill_matches(model):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: model {model} was not selected")

    async def _prepare_ui_format(
        self,
        page,
        ratio: str,
        duration: str | None = None,
        resolution: str | None = None,
        duration_model: str = "",
    ) -> None:
        """Select current numeric Flow tabs and verify the requested format.

        Recent Flow builds label aspect tabs ``16:9``/``9:16``/``1:1`` rather
        than the older Landscape/Portrait/Square labels used by flow-py.
        """
        await _dismiss_flow_whats_new(page)
        async def visible_tab(label: str | re.Pattern[str]):
            if getattr(page, "is_closed", lambda: False)():
                raise RuntimeError("Target page, context or browser has been closed")
            pattern = label if hasattr(label, "search") else re.compile(re.escape(label))
            matches = page.locator(_FLOW_CONTROL_SELECTOR).filter(
                has_text=pattern
            )
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                try:
                    try:
                        visible = await candidate.is_visible(timeout=0)
                    except TypeError:
                        # Keep lightweight test doubles and older Playwright
                        # wrappers compatible with the zero-wait probe.
                        visible = await candidate.is_visible()
                    if visible:
                        return candidate
                except Exception:
                    pass
            return None

        async def visible_ratio_tab(label: str):
            """Find a ratio control across Flow's text and icon-based UIs.

            Some Flow builds render the square/portrait controls with only a
            Material icon (for example ``crop_1_1``) and expose the numeric
            ratio through ``aria-label`` or ``data-value``.  A text-only
            locator then waits for 30 seconds even though the control exists.
            """
            candidate = await visible_tab(label)
            if candidate is not None:
                return candidate
            icon = f"crop_{label.replace(':', '_')}"
            controls = page.locator(_FLOW_CONTROL_SELECTOR)
            for index in range(await controls.count()):
                control = controls.nth(index)
                try:
                    try:
                        visible = await control.is_visible(timeout=0)
                    except TypeError:
                        visible = await control.is_visible()
                    if not visible:
                        continue
                    values = [
                        await control.inner_text(),
                        await control.get_attribute("aria-label"),
                        await control.get_attribute("data-value"),
                        await control.get_attribute("title"),
                    ]
                    haystack = " ".join(str(value or "") for value in values)
                    if re.search(rf"(?<!\\d){re.escape(label)}(?!\\d)", haystack) or icon in haystack:
                        return control
                except Exception:
                    continue
            return None

        async def visible_duration_tab(label: re.Pattern[str]):
            # Flow has shipped duration controls as tabs, radios, and plain
            # Material buttons. Keep the broad fallback scoped to the exact
            # duration text so model/ratio controls cannot be selected by it.
            selectors = (
                _FLOW_CONTROL_SELECTOR,
                'button, [role="option"], [role="menuitem"], [data-value]',
            )
            candidates = []
            for selector in selectors:
                matches = page.locator(selector).filter(has_text=label)
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        try:
                            text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                            css_class = str(await candidate.get_attribute("class") or "").lower()
                            aria_label = str(await candidate.get_attribute("aria-label") or "").lower()
                            trigger_ancestor = bool(await candidate.evaluate("""node => Boolean(
                                node.closest('.settings-trigger-button, .settings-summary')
                            )"""))
                        except Exception:
                            text = ""
                            css_class = ""
                            aria_label = ""
                            trigger_ancestor = False
                        # The bottom-bar summary also contains the requested
                        # duration (e.g. ``Video · 720p · 6 giây``), but it is
                        # only the settings-panel trigger, not the 6s option.
                        # Never treat that summary as a duration control.
                        if (
                            "settings-trigger" in css_class
                            or "settings-summary" in css_class
                            or trigger_ancestor
                            or "điều kiện kích hoạt" in aria_label
                            or "settings" in aria_label and "duration" not in aria_label
                        ):
                            continue
                        candidates.append((len(text) or 10_000, candidate))
            # has_text also matches a parent group containing all four pills.
            # Prefer the smallest matching node so the click lands on the
            # actual 6s/8s/10s control rather than the group wrapper.
            if candidates:
                candidates.sort(key=lambda item: item[0])
                return candidates[0][1]
            return None

        async def duration_debug(label: re.Pattern[str]) -> list[dict[str, str]]:
            result: list[dict[str, str]] = []
            matches = page.locator(
                'button, [role="tab"], [role="radio"], [role="option"], [role="menuitem"], [data-value]'
            ).filter(has_text=label)
            for index in range(min(12, await matches.count())):
                candidate = matches.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    parent_info = await candidate.evaluate(
                        "node => { const p = node.parentElement; return p ? p.tagName + '|' + (p.className||'') : ''; }"
                    )
                    result.append({
                        "text": re.sub(r"\s+", " ", (await candidate.inner_text()).strip()),
                        "ariaSelected": str(await candidate.get_attribute("aria-selected") or ""),
                        "ariaChecked": str(await candidate.get_attribute("aria-checked") or ""),
                        "ariaPressed": str(await candidate.get_attribute("aria-pressed") or ""),
                        "class": str(await candidate.get_attribute("class") or ""),
                        "parentTag": str(parent_info or ""),
                    })
                except Exception:
                    continue
            return result

        async def summary_confirms_duration(value: str) -> bool:
            """Accept Flow's bottom-bar summary as the selected state.

            Some builds render duration controls as a closed overlay and expose
            only the committed value on the settings trigger. In that state
            there is no aria-checked node to inspect, but the summary is the
            value that will be submitted and must not be replaced by 8s.
            """
            pattern = _duration_pattern(value)
            summaries = page.locator(".settings-trigger-button, .settings-summary")
            for index in range(await summaries.count()):
                summary = summaries.nth(index)
                try:
                    if await summary.is_visible() and pattern.search(
                        re.sub(r"\s+", " ", (await summary.inner_text()).strip())
                    ):
                        return True
                except Exception:
                    continue
            return False

        async def summary_confirms_resolution(value: str) -> bool:
            """Accept Flow's committed resolution summary when controls close after selection."""
            summaries = page.locator(".settings-trigger-button, .settings-summary")
            for index in range(await summaries.count()):
                summary = summaries.nth(index)
                try:
                    if not await summary.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await summary.inner_text()).strip())
                    if re.search(rf"(?<!\d){re.escape(value)}(?!\d)", text, re.I):
                        return True
                    if value == "720p" and re.search(r"original size|kích thước gốc", text, re.I):
                        return True
                except Exception:
                    continue
            return False

        ratio_label = str(ratio or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{1,2}", ratio_label):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid aspect ratio {ratio_label!r}")
        ratio_tab = None
        for attempt in range(4):
            if getattr(page, "is_closed", lambda: False)():
                raise RuntimeError("Target page, context or browser has been closed")
            ratio_tab = await visible_ratio_tab(ratio_label)
            if ratio_tab is not None:
                break
            # Model selection can leave a transient menu open or close the
            # settings popover. Focus textarea (so pill opens settings not media picker),
            # then reopen via pill click.
            if attempt:
                keyboard = getattr(page, "keyboard", None)
                if keyboard is not None:
                    await keyboard.press("Escape")
                await asyncio.sleep(0.2)
            # Focus textarea trước khi click pill
            try:
                ed = page.locator("div[contenteditable]").first
                if await ed.count() > 0:
                    await ed.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass
            trigger = None
            # 1. Pill x[1-4] (matches "x1" và "crop_16_9x1")
            buttons = page.locator("button")
            for index in range(await buttons.count() - 1, -1, -1):
                candidate = buttons.nth(index)
                text = (await candidate.inner_text()).strip()
                if (
                    await candidate.is_visible()
                    and re.search(r"x[1-4]\b", text)
                    and await candidate.get_attribute("role") != "tab"
                ):
                    trigger = candidate
                    break
            # 2. Fallback: aria-haspopup (menu / dialog / listbox)
            if trigger is None:
                for haspopup in ("menu", "dialog", "listbox", "true"):
                    pills = page.locator(f'button[aria-haspopup="{haspopup}"]')
                    for index in range(await pills.count() - 1, -1, -1):
                        candidate = pills.nth(index)
                        try:
                            if await candidate.is_visible(timeout=0):
                                trigger = candidate
                                break
                        except Exception:
                            pass
                    if trigger is not None:
                        break
            # 3. Fallback: settings-trigger-button class
            if trigger is None:
                stb = page.locator(".settings-trigger-button, .settings-summary")
                for index in range(await stb.count() - 1, -1, -1):
                    candidate = stb.nth(index)
                    try:
                        if await candidate.is_visible(timeout=0):
                            trigger = candidate
                            break
                    except Exception:
                        pass
            if trigger is not None:
                await trigger.click()
            await asyncio.sleep(1.2)
        if ratio_tab is None:
            raise RuntimeError(f"FLOW_UI_CHANGED: aspect ratio {ratio_label} was not found")
        scroll_into_view = getattr(ratio_tab, "scroll_into_view_if_needed", None)
        if scroll_into_view is not None:
            await scroll_into_view()
        click_ratio = getattr(ratio_tab, "click", None)
        if click_ratio is not None:
            await click_ratio(timeout=5_000)
            await asyncio.sleep(0.5)
        if not await _flow_control_is_selected(ratio_tab):
            # Second retry — UI may lag on slow machines
            await ratio_tab.click(timeout=5_000)
            await asyncio.sleep(0.8)
        if not await _flow_control_is_selected(ratio_tab):
            # mat-button-toggle ancestor fallback
            try:
                host = await ratio_tab.evaluate_handle(
                    "node => node.closest('mat-button-toggle, [role=\"radio\"], [role=\"tab\"]') || node.parentElement"
                )
                await host.as_element().click(force=True)  # type: ignore[union-attr]
                await asyncio.sleep(0.8)
            except Exception as _exc:
                _log.debug("ratio ancestor click failed: %s", _exc)
        if not await _flow_control_is_selected(ratio_tab):
            raise RuntimeError(f"FLOW_SETTING_MISMATCH: aspect ratio {ratio_label} was not selected")


        if resolution:
            resolution_value = _video_ui_resolution(resolution).lower()
            if not resolution_value:
                # Shared settings often keep image plan labels (1K/2K/4K). Veo has
                # no resolution tabs — ignore instead of FLOW_SETTING_MISMATCH.
                _log.info("_prepare_ui_format: ignoring non-video resolution %r", resolution)
            else:
                # Tab text may be "360pinfo" (trailing info icon) — only forbid digits.
                resolution_pattern = re.compile(rf"(?<!\d){re.escape(resolution_value)}(?!\d)", re.I)
                # Prefer the smallest visible control. A broad has_text match
                # can otherwise return the settings container containing both
                # 360p and 720p, leaving Flow on its 720p default.
                resolution_tab = None
                resolution_candidates = []
                # gflow-cli drives the committed Flow control: exact radios inside
                # the settings radiogroup, then verifies aria-checked. Prefer this
                # anchor over broad text matches that can hit the 720p summary.
                exact_radios = page.locator("[role='radiogroup'] [role='radio']").filter(has_text=resolution_pattern)
                for index in range(await exact_radios.count()):
                    candidate = exact_radios.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                        if re.fullmatch(rf"{re.escape(resolution_value)}(?:\s*info)?", text, re.I):
                            resolution_candidates.append((-1, len(text), candidate))
                    except Exception:
                        continue
                if not resolution_candidates:
                    for selector in ('mat-button-toggle', _FLOW_CONTROL_SELECTOR, 'button, [role="radio"], [role="tab"], [data-value]'):
                        matches = page.locator(selector).filter(has_text=resolution_pattern)
                        for index in range(await matches.count()):
                            candidate = matches.nth(index)
                            try:
                                if not await candidate.is_visible():
                                    continue
                                text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                                if selector == 'mat-button-toggle' and not re.fullmatch(rf"{re.escape(resolution_value)}(?:\s*info)?", text, re.I):
                                    continue
                                values = " ".join(filter(None, [
                                    text,
                                    await candidate.get_attribute("aria-label"),
                                    await candidate.get_attribute("data-value"),
                                    await candidate.get_attribute("title"),
                                ]))
                                if resolution_pattern.search(values):
                                    resolution_candidates.append((0 if selector == 'mat-button-toggle' else 1, len(text), candidate))
                            except Exception:
                                continue
                if resolution_candidates:
                    chosen = min(resolution_candidates, key=lambda item: item[:2])
                    resolution_tab = chosen[2]
                    if chosen[0] == 0:
                        # Material handles input on the button, not the host's
                        # ripple overlay. Use a real actionable button click.
                        resolution_tab = resolution_tab.locator('button[role="radio"]').first
                if resolution_tab is None:
                    if re.search(r"\bomni\b.*\bflash\b", str(duration_model or ""), re.I):
                        raise RuntimeError(
                            f"FLOW_SETTING_MISMATCH: Omni Flash resolution {resolution_value} was not found"
                        )
                    _log.info("_prepare_ui_format: resolution %s control is hidden; using Flow model default", resolution_value)
                    return
                await resolution_tab.scroll_into_view_if_needed()
                await resolution_tab.click()
                await asyncio.sleep(0.5)
                if not await _flow_control_is_selected(resolution_tab):
                    await resolution_tab.click(force=True)
                    await asyncio.sleep(0.8)
                if not await _flow_control_is_selected(resolution_tab):
                    # mat-button-toggle: visible_tab resolves to inner <button>,
                    # but the clickable host is the parent. Try closest ancestor.
                    try:
                        host = await resolution_tab.evaluate_handle(
                            "node => node.closest('mat-button-toggle, [role=\"radio\"], [role=\"tab\"]') || node.parentElement"
                        )
                        await host.as_element().click(force=True)  # type: ignore[union-attr]
                        await asyncio.sleep(0.8)
                    except Exception as _exc:
                        _log.debug("resolution ancestor click failed: %s", _exc)
                if not await _flow_control_is_selected(resolution_tab):
                    if await summary_confirms_resolution(resolution_value):
                        _log.info(
                            "_prepare_ui_format: resolution %s confirmed by settings summary",
                            resolution_value,
                        )
                    else:
                        raise RuntimeError(
                            f"FLOW_SETTING_MISMATCH: resolution {resolution_value} was not confirmed selected"
                        )

        if duration is not None:
            duration_value = str(duration).strip()
            if not re.fullmatch(r"\d{1,3}", duration_value):
                raise RuntimeError(f"FLOW_SETTING_MISMATCH: invalid duration {duration_value!r}")
            duration_label = f"{duration_value}s"
            duration_tab = await visible_duration_tab(_duration_pattern(duration_value))
            if duration_tab is None:
                # Selecting the aspect ratio can close the settings popover.
                # Reopen it before concluding that the duration control is
                # unavailable; the bottom-bar summary is not a control.
                trigger_candidates = page.locator(".settings-trigger-button, .settings-summary").filter(
                    has_text=re.compile(r"\b(?:Video|Omni|Veo)\b", re.I)
                )
                trigger = trigger_candidates.nth(await trigger_candidates.count() - 1)
                if await trigger.count() and await trigger.is_visible():
                    await trigger.click()
                    await asyncio.sleep(0.5)
                    duration_tab = await visible_duration_tab(_duration_pattern(duration_value))
            if duration_tab is None:
                if await summary_confirms_duration(duration_value):
                    _log.info("_prepare_ui_format: duration %ss confirmed by settings summary", duration_value)
                    return
                # An explicit duration is a contract. Silently accepting the
                # model default turns a requested 6s Omni job into 8s output.
                # Callers that omit the model are legacy/internal probes; they
                # cannot tell Veo's fixed default from a missing Omni control.
                if not duration_model:
                    _log.info("_prepare_ui_format: duration %s control is hidden; using Flow model default", duration_label)
                    return
                raise RuntimeError(
                    f"FLOW_SETTING_MISMATCH: duration {duration_label} was not confirmed selected (model={duration_model or 'unknown'})"
                )
            else:
                # The MAT-BUTTON-TOGGLE host has role="presentation" and Angular
                # sets aria-checked on the inner mat-button-toggle-button, not on
                # the host. force=True clicks bypass Angular's zone; use a standard
                # click on the inner button so Angular processes the event normally.
                # _flow_control_is_selected already checks closest() ancestors so
                # mat-button-toggle-checked on the host is also caught as a fallback.
                _host_tag = ""
                _inner_btn = duration_tab  # keep reference to inner button for click
                try:
                    host = await duration_tab.evaluate_handle(
                        "node => node.closest('mat-button-toggle') || node"
                    )
                    if host:
                        resolved = host.as_element()
                        if resolved:
                            _host_tag = await resolved.evaluate(
                                "node => node.tagName + '.' + (node.className||'').replace(/\\s+/g,'.')"
                            )
                            # keep duration_tab as the inner button for aria-checked checks
                except Exception as _e:
                    _host_tag = f"err:{_e}"

                try:
                    await _inner_btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                # Always click, even when aria-checked already says true. The
                # current Flow page can retain that state from the previous
                # prompt while the new composer has not committed it yet.
                # Standard click first lets Angular's NgZone process the event.
                _clicked = False
                try:
                    await _inner_btn.click(timeout=3_000)
                    await asyncio.sleep(0.4)
                    _clicked = True
                except Exception:
                    pass
                # Fall back to force click if the standard click was intercepted.
                if not _clicked or not await _flow_control_is_selected(_inner_btn):
                    try:
                        await _inner_btn.click(force=True, timeout=5_000)
                        await asyncio.sleep(0.4)
                    except Exception as exc:
                        raise RuntimeError(
                            f"FLOW_SETTING_MISMATCH: duration {duration_label} was not confirmed selected (model={duration_model or 'unknown'})"
                        ) from exc


                for _ in range(8):
                    if await _flow_control_is_selected(duration_tab):
                        break
                    await asyncio.sleep(0.2)
                if duration_tab is not None and not await _flow_control_is_selected(duration_tab):
                    if await summary_confirms_duration(duration_value):
                        _log.info("_prepare_ui_format: duration %ss confirmed by settings summary", duration_value)
                        return
                    if not duration_model:
                        _log.info("_prepare_ui_format: duration %s not confirmed; using Flow model default", duration_label)
                        return
                    try:
                        _host_attrs = await duration_tab.evaluate("""node => {
                            const attrs = {};
                            for (const a of node.attributes) attrs[a.name] = a.value;
                            attrs['_class'] = node.className;
                            // If this is the inner button, also report parent host state
                            const host = node.closest('mat-button-toggle');
                            if (host) {
                                attrs['_host_class'] = host.className;
                                attrs['_host_aria_checked'] = host.getAttribute('aria-checked');
                            }
                            return attrs;
                        }""")
                    except Exception:
                        _host_attrs = {}
                    raise RuntimeError(
                        f"FLOW_SETTING_MISMATCH: duration {duration_label} was not confirmed selected"
                        f" (model={duration_model or 'unknown'}; host={_host_tag}; hostAttrs={_host_attrs};"
                        f" controls={await duration_debug(_duration_pattern(duration_value))})"
                    )
