"""Web browser target (Chrome / Edge / Firefox / Electron via Playwright).

Perception is still vision-first: ``observe`` screenshots the page viewport and
passes it through the same VLM scene analyser. On top of that, the DOM is used
as a reliable *execution and verification* channel: a DOM index maps field
labels to selectors so the agent can click, fill and read-back with zero
coordinate fragility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from atlas.act.controls import ControlInterface, ControlOutcome
from atlas.core.logging import logger
from atlas.target.base import TargetAdapter, TargetInfo
from atlas.vision.capture import ClientArea
from atlas.vision.models import BBox, ElementType, SceneDescription
from atlas.vision.scene import SceneAnalysis, SceneAnalyzer


@dataclass
class DomEntry:
    """One DOM form control captured for the index."""

    selector: str
    tag: str
    input_type: str = ""
    label: str = ""
    normalized_label: str = ""
    element_type: ElementType = ElementType.UNKNOWN
    value: str = ""
    disabled: bool = False
    required: bool = False


_INSPECT_JS = """
() => {
  const result = [];
  const seen = new Set();
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const labelFor = (el) => {
    if (el.labels && el.labels[0]) return el.labels[0].innerText.trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
    if (el.tagName.toLowerCase() === 'button') return (el.innerText || '').trim();
    if (el.id) {
      const lb = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lb) return lb.innerText.trim();
    }
    const parent = el.closest('label');
    if (parent) return parent.innerText.trim();
    return '';
  };
  const cssSelector = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
    if (el.getAttribute('data-testid')) return '[data-testid="' + CSS.escape(el.getAttribute('data-testid')) + '"]';
    let tag = el.tagName.toLowerCase();
    let idx = 1;
    let sib = el;
    while ((sib = sib.previousElementSibling)) { if (sib.tagName === el.tagName) idx++; }
    return tag + ':nth-of-type(' + idx + ')';
  };
  const inputs = document.querySelectorAll('input, select, textarea, [contenteditable="true"], button');
  for (const el of inputs) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    const label = labelFor(el);
    const sel = cssSelector(el);
    if (seen.has(sel)) continue;
    seen.add(sel);
    result.push({
      selector: sel,
      tag: el.tagName.toLowerCase(),
      input_type: type,
      label: label,
      value: (el.value !== undefined ? el.value : el.innerText || ''),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      required: el.required === true || el.getAttribute('aria-required') === 'true',
      bbox: rect(el),
    });
  }
  return result;
}
"""


class ExistingTabNotFound(RuntimeError):
    """The existing browser is reachable but has no page matching the target URL.

    Raised by :meth:`WebTarget.attach_existing` so callers fall back to
    browser-UIA interaction or wait for the user - never relaunch a browser.
    """

    def __init__(self, endpoint_url: str, url: str | None) -> None:
        self.endpoint_url = endpoint_url
        self.url = url
        super().__init__(f"existing browser {endpoint_url} has no page matching {url!r}")


def _normalize_label(label: str) -> str:
    text = str(label).lower()
    text = re.sub(r"[:\s]+$", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _infer_type(dom_entry: dict) -> ElementType:
    input_type = (dom_entry.get("input_type") or "").lower()
    tag = dom_entry.get("tag") or ""
    if tag == "select":
        return ElementType.COMBOBOX
    if tag == "textarea":
        return ElementType.TEXTAREA
    if tag == "button":
        return ElementType.BUTTON
    if input_type in {"checkbox"}:
        return ElementType.CHECKBOX
    if input_type in {"radio"}:
        return ElementType.RADIO
    if input_type in {"date", "datetime-local", "time"}:
        return ElementType.DATE_PICKER
    if input_type in {"password"}:
        return ElementType.PASSWORD
    if input_type in {"text", "email", "number", "tel", "search", "url"}:
        return ElementType.TEXTBOX
    return ElementType.UNKNOWN


class DomControlEngine(ControlInterface):
    """Playwright-backed control engine (DOM execution + read-back)."""

    def __init__(self, page: Any) -> None:
        self._page: Any = page

    def _locator(self, field_id: str | None) -> Any:
        if field_id is None:
            return None
        target = getattr(self._page, "_atlas_dom_target", None)
        if target is not None:
            selector = target.locator_for(field_id)
            if selector:
                return self._page.locator(selector)
        return None

    def focus(self, bbox: BBox | None, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is not None:
            loc.click()
            return ControlOutcome(ok=True, evidence="dom focus")
        return ControlOutcome(ok=False, evidence="no dom locator")

    def click_field(self, bbox: BBox | None, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is not None:
            loc.click()
            return ControlOutcome(ok=True, evidence="dom click")
        return ControlOutcome(ok=False, evidence="no dom locator")

    def type_value(self, bbox: BBox | None, value: str, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        loc.fill("")
        loc.press_sequentially(value, delay=0.02)
        return ControlOutcome(ok=True, evidence="dom typed")

    def clear(self, bbox: BBox | None, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        loc.fill("")
        return ControlOutcome(ok=True, evidence="dom cleared")

    def select_option(
        self, bbox: BBox | None, value: str, options: list[str] | None = None, field_id: str | None = None
    ) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        try:
            loc.select_option(label=value)
            return ControlOutcome(ok=True, evidence=f"dom select {value!r}")
        except Exception:
            pass
        try:
            loc.select_option(value=value)
            return ControlOutcome(ok=True, evidence=f"dom select {value!r}")
        except Exception:
            pass
        loc.click()
        loc.fill(value)
        self._page.keyboard.press("Enter")
        return ControlOutcome(ok=True, evidence="dom type-select")

    def toggle(self, bbox: BBox | None, value: str, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        desired = _parse_boolean(value)
        try:
            if desired:
                loc.check()
            else:
                loc.uncheck()
            return ControlOutcome(ok=True, evidence=f"dom checkbox -> {desired}")
        except Exception:
            loc.click()
            return ControlOutcome(ok=True, evidence="dom toggle clicked")

    def choose_date(
        self, bbox: BBox | None, value: str, date_format: str | None = None, field_id: str | None = None
    ) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        loc.fill(value)
        return ControlOutcome(ok=True, evidence="dom date filled")

    def press_tab(self) -> ControlOutcome:
        self._page.keyboard.press("Tab")
        return ControlOutcome(ok=True, evidence="dom tab")

    def press_enter(self) -> ControlOutcome:
        self._page.keyboard.press("Enter")
        return ControlOutcome(ok=True, evidence="dom enter")

    def press_escape(self) -> ControlOutcome:
        self._page.keyboard.press("Escape")
        return ControlOutcome(ok=True, evidence="dom escape")

    def scroll(self, direction: str, amount: int = 3) -> ControlOutcome:
        delta = amount * 100 if direction == "down" else -amount * 100
        self._page.mouse.wheel(0, delta)
        return ControlOutcome(ok=True, evidence=f"dom scroll {direction}")

    def scroll_by_keys(self, direction: str, amount: int = 3) -> ControlOutcome:
        # PageDown/PageUp scroll whatever region currently has focus - this is
        # how nested scroll panes and iframes are reached in the browser.
        key = "PageDown" if direction == "down" else "PageUp"
        for _ in range(max(1, abs(amount))):
            self._page.keyboard.press(key)
        return ControlOutcome(ok=True, evidence=f"dom keys {direction}")

    def scroll_bar(self, direction: str, amount: int = 3) -> ControlOutcome:
        # End/Home jump to the end of the active scroll container.
        key = "End" if direction == "down" else "Home"
        self._page.keyboard.press(key)
        return ControlOutcome(ok=True, evidence=f"dom scrollbar {direction}")

    def paste(self, value: str, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        loc.fill(value)
        return ControlOutcome(ok=True, evidence="dom paste")

    def upload_file(self, bbox: BBox | None, path: str, field_id: str | None = None) -> ControlOutcome:
        loc = self._locator(field_id)
        if loc is None:
            return ControlOutcome(ok=False, evidence="no dom locator")
        try:
            loc.set_input_files(path)
            return ControlOutcome(ok=True, evidence=f"dom file upload {path!r}")
        except Exception as exc:
            return ControlOutcome(ok=False, evidence=f"dom upload failed: {exc}")


def _parse_boolean(value: str) -> bool:
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on", "checked"}


class WebTarget(TargetAdapter):
    """Vision-first web target with DOM execution channel."""

    name = "web"

    def __init__(
        self,
        analyzer: SceneAnalyzer,
        browser_type: str = "chromium",
        headless: bool = False,
        viewport: tuple[int, int] | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._browser_type = browser_type
        self._headless = headless
        self._viewport = viewport or (1280, 900)
        self._sync: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._dom_index: list[DomEntry] = []
        self._field_id_to_label: dict[str, str] = {}
        self._info: TargetInfo | None = None
        self._controls: DomControlEngine | None = None
        #: True when attached to an already-running browser via CDP. detach()
        #: must then only disconnect - never close the user's browser.
        self._connected_existing: bool = False

    @property
    def page(self):
        return self._page

    @property
    def info(self) -> TargetInfo | None:
        return self._info

    @property
    def controls(self) -> DomControlEngine:
        if self._controls is None:
            raise RuntimeError("web target not attached")
        return self._controls

    def attach(self, hint: str | None = None) -> TargetInfo:
        """Attach to a web page by launching a fresh Playwright browser.

        This is the LAST-RESORT path. Attach-first callers use
        :meth:`attach_existing` to connect to a browser the user already has
        open; launching is only permitted when the attach policy allows it.
        """
        from playwright.sync_api import sync_playwright

        url = hint or "http://localhost:5173"
        self._sync = sync_playwright().start()
        if self._browser_type == "firefox":
            self._browser = self._sync.firefox.launch(headless=self._headless)
        elif self._browser_type == "webkit":
            self._browser = self._sync.webkit.launch(headless=self._headless)
        else:
            self._browser = self._sync.chromium.launch(headless=self._headless)
        context = self._browser.new_context(viewport={"width": self._viewport[0], "height": self._viewport[1]})
        self._page = context.new_page()
        self._page._atlas_dom_target = self  # type: ignore[attr-defined]
        self._page.goto(url, wait_until="load", timeout=30000)
        self._page.wait_for_load_state("networkidle", timeout=15000)
        self._info = TargetInfo(name=self.name, title=self._page.title() or url, url=self._page.url)
        self._controls = DomControlEngine(self._page)
        logger.info("[ATTACH] web target launched (NEW browser): {}", self._page.url)
        return self._info

    def attach_existing(self, endpoint_url: str, url: str | None = None) -> TargetInfo:
        """Attach to an ALREADY-RUNNING Chromium/Edge browser over CDP.

        This never launches a browser, never opens a new window and never opens
        a new tab. A page whose URL matches ``url`` is reused in place; if no
        matching page is open a :class:`ExistingTabNotFound` is raised so the
        caller can fall back to UIA/desktop interaction instead of relaunching.
        """
        from playwright.sync_api import sync_playwright

        self._sync = sync_playwright().start()
        try:
            self._browser = self._sync.chromium.connect_over_cdp(endpoint_url)
        except Exception as exc:
            try:
                self._sync.stop()
            except Exception:
                pass
            self._sync = None
            raise RuntimeError(f"cannot connect to existing browser at {endpoint_url}: {exc}") from exc
        self._connected_existing = True
        page = self._pick_existing_page(url)
        if page is None:
            self.detach()
            raise ExistingTabNotFound(endpoint_url, url)
        self._page = page
        self._page._atlas_dom_target = self  # type: ignore[attr-defined]
        self._info = TargetInfo(name=self.name, title=page.title() or "", url=page.url)
        self._controls = DomControlEngine(page)
        logger.info("[ATTACH] attached to EXISTING browser {} tab: {}", endpoint_url, page.url)
        return self._info

    def _pick_existing_page(self, url: str | None) -> Any | None:
        """Pick a page from the connected browser, preferring a URL match."""
        pages: list[Any] = []
        for context in self._browser.contexts or []:
            pages.extend(context.pages)
        if not pages:
            return None
        if url:
            needle = url.strip().rstrip("/")
            if needle:
                for page in pages:
                    current = (page.url or "").strip().rstrip("/")
                    if current and (needle in current or current in needle):
                        return page
        return None

    def detach(self) -> None:
        if self._browser is not None and not self._connected_existing:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._sync is not None:
            try:
                self._sync.stop()
            except Exception:
                pass
        self._browser = self._sync = self._page = self._controls = None
        self._connected_existing = False
        self._info = None

    def is_alive(self) -> bool:
        """True when the attached page is still usable.

        Handles the async-vs-sync Playwright trap: ``is_closed()`` on the sync
        API returns a plain ``bool``, but when the underlying driver state is
        inconsistent (sync object created on another thread / driver being torn
        down) it can return a coroutine object or raise ``TypeError`` from
        ``asyncio.run_coroutine_threadsafe``. In every ambiguous case we report
        ALIVE - a page we cannot inspect must never be declared dead, because a
        false "dead" detection makes the browser watchdog tear down and restart
        a perfectly good connection (the restart loop from the production logs).
        A page that is truly gone fails the next real operation instead.
        """
        if self._page is None:
            return False
        try:
            closed = self._page.is_closed()
        except (TypeError, RuntimeError):
            # Coroutine/threading mismatch: the page is still attached - do not
            # declare it dead based on a broken aliveness probe.
            return True
        except Exception:
            return False
        if hasattr(closed, "__await__"):
            # The probe returned a coroutine instead of a bool (async driver
            # leak). Never treat an in-flight connection as closed.
            return True
        return not bool(closed)

    def observe(self) -> SceneAnalysis | None:
        if self._page is None:
            return None
        try:
            raw = self._page.screenshot(type="png")
            import io

            from PIL import Image

            pil = Image.open(io.BytesIO(raw)).convert("RGB")
            arr = np.array(pil)
        except Exception as exc:
            logger.warning("web observe screenshot failed: {}", exc)
            return None
        height, width = arr.shape[:2]
        area = ClientArea(image=arr, left=0, top=0, width=width, height=height)
        title = self._page.title() or ""
        url = self._page.url
        analysis = self._analyzer.analyze(area, window_title=title, url=url)
        self._sync_scene_to_dom(analysis.scene)
        return analysis

    def read_field_value(self, field_id: str) -> str | None:
        selector = self.locator_for(field_id)
        if selector is None or self._page is None:
            return None
        try:
            loc = self._page.locator(selector)
            if loc.count() == 0:
                return None
            tag = loc.first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                return loc.first.evaluate("el => el.value")
            if tag == "input":
                input_type = loc.first.evaluate("el => (el.type || '').toLowerCase()")
                if input_type in {"checkbox", "radio"}:
                    return loc.first.evaluate("el => el.checked ? 'checked' : 'unchecked'")
                return loc.first.evaluate("el => el.value")
            if tag == "textarea":
                return loc.first.evaluate("el => el.value")
            return loc.first.evaluate("el => el.innerText")
        except Exception:
            return None

    def locator_for(self, field_id: str) -> str | None:
        label = self._field_id_to_label.get(field_id)
        if not label:
            return None
        return self._selector_for_label(label)

    # -- DOM index -----------------------------------------------------------

    def _refresh_dom_index(self) -> None:
        if self._page is None:
            return
        try:
            raw = self._page.evaluate(_INSPECT_JS)
        except Exception as exc:
            logger.debug("dom inspect failed: {}", exc)
            return
        entries: list[DomEntry] = []
        for item in raw:
            label = item.get("label") or ""
            selector = item.get("selector") or ""
            if not selector:
                continue
            entries.append(DomEntry(
                selector=selector,
                tag=item.get("tag", ""),
                input_type=item.get("input_type", ""),
                label=label,
                normalized_label=_normalize_label(label),
                element_type=_infer_type(item),
                value=item.get("value", ""),
                disabled=bool(item.get("disabled")),
                required=bool(item.get("required")),
            ))
        self._dom_index = entries
        logger.debug("dom index: {} controls", len(entries))

    def _sync_scene_to_dom(self, scene: SceneDescription) -> None:
        """Map scene element ids to DOM selectors by normalized label."""
        self._refresh_dom_index()
        self._field_id_to_label.clear()
        for element in scene.elements:
            if not element.label:
                continue
            norm = _normalize_label(element.label)
            self._field_id_to_label[element.element_id] = element.label
            entry = self._find_dom_entry(norm)
            if entry is not None:
                if element.editable:
                    element.value = entry.value or element.value
                    element.required = entry.required if element.required is None else element.required
                    element.disabled = entry.disabled or element.disabled
                element.name = entry.selector

    def _find_dom_entry(self, normalized_label: str) -> DomEntry | None:
        if not normalized_label:
            return None
        for entry in self._dom_index:
            if entry.normalized_label and entry.normalized_label == normalized_label:
                return entry
        for entry in self._dom_index:
            if entry.normalized_label and (
                entry.normalized_label in normalized_label or normalized_label in entry.normalized_label
            ):
                return entry
        return None

    def _selector_for_label(self, label: str) -> str | None:
        norm = _normalize_label(label)
        entry = self._find_dom_entry(norm)
        return entry.selector if entry else None

    def close(self) -> None:
        self.detach()


__all__ = ["WebTarget", "DomControlEngine", "DomEntry", "ExistingTabNotFound"]
