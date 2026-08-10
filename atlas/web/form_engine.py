"""WEB_DOM form engine.

The universal agent's fast web path. Given a Playwright page it:

1. discovers every DOM form control into :class:`WebFieldDescriptor` objects,
2. maps a source record onto them via the semantic :class:`SemanticMapper`,
3. fills each field with the FASTEST reliable method (native select_option for
   ``<select>``, check()/uncheck() for checkboxes, fill() for text, date and
   file inputs - no clicks, no OCR, no human delays),
4. verifies with the authoritative DOM read-back,
5. times every phase and learns the best method per (application, field),
6. caches the form fingerprint so later records never re-discover everything.

All waits are state-observed (:class:`SmartWait`), never large fixed sleeps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from atlas.core.logging import logger
from atlas.mapping.mapper import SemanticMapper
from atlas.universal.learning import MethodLearner
from atlas.universal.smart_wait import SmartWait
from atlas.vision.models import ElementType, ScreenElement
from atlas.web.fields import build_locator, field_fingerprint_js

#: Maximum number of DOM fields captured (guards pathological pages).
_MAX_FIELDS = 120

#: JS that inspects the whole form and returns rich descriptors per control.
_DISCOVER_JS = """
() => {
  const result = [];
  const seen = new Set();
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const labelFor = (el) => {
    if (el.labels && el.labels[0]) return (el.labels[0].innerText || '').trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
    if (el.id) {
      const lb = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lb) return (lb.innerText || '').trim();
    }
    const parent = el.closest('label');
    if (parent) return (parent.innerText || '').trim();
    if (el.getAttribute('name')) return el.getAttribute('name').replace(/[_-]+/g, ' ');
    if (el.getAttribute('autocomplete')) return el.getAttribute('autocomplete').replace(/[_-]+/g, ' ');
    return '';
  };
  const optionText = (el) => {
    if (el.tagName !== 'SELECT') return [];
    return Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean);
  };
  const inputs = document.querySelectorAll(
    'input, select, textarea, [contenteditable="true"]'
  );
  for (const el of inputs) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (type === 'hidden') continue;
    const label = labelFor(el);
    const id = el.id || '';
    const name = el.name || '';
    const aria = el.getAttribute('aria-label') || '';
    const placeholder = el.getAttribute('placeholder') || '';
    const testid = el.getAttribute('data-testid') || '';
    const sel = buildSelector(el, id, name, aria, placeholder, testid);
    if (seen.has(sel)) continue;
    seen.add(sel);
    result.push({
      tag: el.tagName.toLowerCase(),
      type: type,
      name: name,
      id: id,
      aria: aria,
      placeholder: placeholder,
      role: el.getAttribute('role') || '',
      label: label,
      selector: sel,
      required: el.required === true || el.getAttribute('aria-required') === 'true',
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
      value: (el.value !== undefined ? el.value : el.innerText || ''),
      checked: el.type === 'checkbox' || el.type === 'radio' ? !!el.checked : null,
      bbox: rect(el),
      options: optionText(el),
    });
  }
  function buildSelector(el, id, name, aria, placeholder, testid) {
    if (id) return '#' + CSS.escape(id);
    if (name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(name) + '"]';
    if (aria) return '[aria-label="' + CSS.escape(aria) + '"]';
    if (placeholder) return '[placeholder="' + CSS.escape(placeholder) + '"]';
    if (testid) return '[data-testid="' + CSS.escape(testid) + '"]';
    let tag = el.tagName.toLowerCase();
    let idx = 1;
    let sib = el;
    while ((sib = sib.previousElementSibling)) { if (sib.tagName === el.tagName) idx++; }
    return tag + ':nth-of-type(' + idx + ')';
  }
  return result;
}
"""


def _infer_element_type(tag: str, input_type: str) -> ElementType:
    if tag == "select":
        return ElementType.COMBOBOX
    if tag == "textarea":
        return ElementType.TEXTAREA
    if tag == "button":
        return ElementType.BUTTON
    if input_type == "checkbox":
        return ElementType.CHECKBOX
    if input_type == "radio":
        return ElementType.RADIO
    if input_type in {"date", "datetime-local", "time", "month", "week"}:
        return ElementType.DATE_PICKER
    if input_type == "password":
        return ElementType.PASSWORD
    if input_type == "file":
        return ElementType.FILE_UPLOAD
    if input_type in {"text", "email", "number", "tel", "search", "url"}:
        return ElementType.TEXTBOX
    return ElementType.UNKNOWN


@dataclass
class WebFieldDescriptor:
    """One DOM form control, with selector candidates and full attributes."""

    element_id: str = ""
    selector: str = ""
    selectors: list[str] = field(default_factory=list)
    label: str = ""
    placeholder: str = ""
    role: str = ""
    tag: str = ""
    input_type: str = ""
    name: str = ""
    id: str = ""
    aria: str = ""
    bbox: dict | None = None
    visible: bool = True
    enabled: bool = True
    required: bool = False
    current_value: str = ""
    options: list[str] = field(default_factory=list)
    element_type: ElementType = ElementType.UNKNOWN
    confidence: float = 1.0

    def to_screen_element(self) -> ScreenElement:
        """Adapt this web field into the scene element used by SemanticMapper."""
        return ScreenElement(
            element_id=self.element_id,
            type=self.element_type,
            label=self.label or self.name or self.placeholder,
            name=self.selector,
            confidence=self.confidence,
            value=self.current_value,
            required=self.required or None,
            disabled=not self.enabled,
            options=list(self.options),
        )

    def to_dict(self) -> dict:
        return {
            "element_id": self.element_id,
            "selector": self.selector,
            "selectors": list(self.selectors),
            "label": self.label,
            "placeholder": self.placeholder,
            "role": self.role,
            "tag": self.tag,
            "input_type": self.input_type,
            "name": self.name,
            "id": self.id,
            "aria": self.aria,
            "bbox": self.bbox,
            "visible": self.visible,
            "enabled": self.enabled,
            "required": self.required,
            "current_value": self.current_value,
            "options": list(self.options),
            "element_type": self.element_type.value,
            "confidence": self.confidence,
        }


@dataclass
class WebForm:
    """A discovered web form plus its structural fingerprint."""

    fields: list[WebFieldDescriptor] = field(default_factory=list)
    url: str = ""
    title: str = ""
    submit_selector: str | None = None
    fingerprint: dict | None = None

    @property
    def field_count(self) -> int:
        return len(self.fields)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "submit_selector": self.submit_selector,
            "field_count": self.field_count,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass
class WebFieldAction:
    """A mapped source pair and the DOM field it targets."""

    source_label: str
    source_value: str
    field: WebFieldDescriptor
    confidence: float
    method: str = ""

    @property
    def selector(self) -> str:
        return self.field.selector


@dataclass
class FieldTiming:
    """Per-field performance breakdown (the speed dashboard's raw input)."""

    field: str = ""
    source_label: str = ""
    method: str = ""
    discover_ms: float = 0.0
    map_ms: float = 0.0
    scroll_ms: float = 0.0
    focus_ms: float = 0.0
    action_ms: float = 0.0
    wait_ms: float = 0.0
    verify_ms: float = 0.0
    recovery_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "source_label": self.source_label,
            "method": self.method,
            "discover_ms": self.discover_ms,
            "map_ms": self.map_ms,
            "scroll_ms": self.scroll_ms,
            "focus_ms": self.focus_ms,
            "action_ms": self.action_ms,
            "wait_ms": self.wait_ms,
            "verify_ms": self.verify_ms,
            "recovery_ms": self.recovery_ms,
            "total_ms": self.total_ms,
        }


@dataclass
class FillResult:
    """Outcome of filling one record (per-field timing + verification)."""

    ok: bool = True
    filled: int = 0
    verified: int = 0
    failed: list[str] = field(default_factory=list)
    timings: list[FieldTiming] = field(default_factory=list)

    @property
    def avg_field_ms(self) -> float:
        if not self.timings:
            return 0.0
        return round(sum(t.total_ms for t in self.timings) / len(self.timings), 1)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "filled": self.filled,
            "verified": self.verified,
            "failed": list(self.failed),
            "timings": [t.to_dict() for t in self.timings],
            "avg_field_ms": self.avg_field_ms,
        }


class WebFormEngine:
    """Fastest-reliable DOM automation for a single Playwright page."""

    def __init__(
        self,
        page: Any,
        mapper: SemanticMapper | None = None,
        learner: MethodLearner | None = None,
        wait: SmartWait | None = None,
        field_timeout: float = 5.0,
    ) -> None:
        self._page = page
        self._mapper = mapper or SemanticMapper()
        self._learner = learner or MethodLearner(enabled=True)
        self._wait = wait or SmartWait(default_timeout=field_timeout)
        self._field_timeout = field_timeout
        self._form: WebForm | None = None

    # -- discovery -----------------------------------------------------------

    def discover(self) -> WebForm:
        """Discover the current form. Caches it; call :meth:`refresh` on change."""
        url = ""
        title = ""
        try:
            url = self._page.url or ""
            title = self._page.title() or ""
        except Exception:
            pass
        raw = self._page.evaluate(_DISCOVER_JS)
        fields: list[WebFieldDescriptor] = []
        for index, item in enumerate(raw or []):
            if index >= _MAX_FIELDS:
                break
            selector = item.get("selector") or ""
            if not selector:
                continue
            tag = item.get("tag") or ""
            input_type = item.get("type") or ""
            element_type = _infer_element_type(tag, input_type)
            selectors = [
                sel for sel in (
                    f"#{item['id']}" if item.get("id") else "",
                    f"[name={_q(item.get('name'))}]" if item.get("name") else "",
                    f"[aria-label={_q(item.get('aria'))}]" if item.get("aria") else "",
                    f"[placeholder={_q(item.get('placeholder'))}]" if item.get("placeholder") else "",
                ) if sel
            ]
            fields.append(WebFieldDescriptor(
                element_id=f"w{index}",
                selector=selector,
                selectors=selectors or [selector],
                label=item.get("label") or "",
                placeholder=item.get("placeholder") or "",
                role=item.get("role") or "",
                tag=tag,
                input_type=input_type,
                name=item.get("name") or "",
                id=item.get("id") or "",
                aria=item.get("aria") or "",
                bbox=item.get("bbox"),
                visible=bool(item.get("visible", True)),
                enabled=not bool(item.get("disabled")),
                required=bool(item.get("required")),
                current_value=item.get("value") or "",
                options=[str(o) for o in (item.get("options") or [])],
                element_type=element_type,
            ))
        fingerprint = self.fingerprint()
        submit = self._find_submit_selector()
        self._form = WebForm(
            fields=fields,
            url=url,
            title=title,
            submit_selector=submit,
            fingerprint=fingerprint,
        )
        logger.info("[MAP] {} DOM fields on {}", len(fields), url or "page")
        return self._form

    def refresh(self) -> WebForm:
        """Re-discover the form (used when the fingerprint changed)."""
        return self.discover()

    def fingerprint(self) -> dict | None:
        """Structural fingerprint of the current form for change detection."""
        try:
            baseline = self._page.evaluate(field_fingerprint_js())
            if isinstance(baseline, dict):
                return baseline
        except Exception:
            pass
        return None

    def form_unchanged(self) -> bool:
        """True when the form structure is identical to the last discovery."""
        if self._form is None:
            return False
        current = self.fingerprint()
        return current == self._form.fingerprint

    # -- mapping -------------------------------------------------------------

    def map_record(self, record) -> list[WebFieldAction]:
        """Map a source record onto the discovered form (semantic, not CSS)."""
        if self._form is None:
            self.discover()
        from atlas.understanding.fields import EditableField

        form = self._form  # type: ignore[assignment]
        wrappers = [
            EditableField(element=f.to_screen_element(), offset=(0, 0)) for f in form.fields
        ]
        mapping = self._mapper.map(record, wrappers)
        actions: list[WebFieldAction] = []
        by_id = {f.element_id: f for f in form.fields}
        for m in mapping.mappings:
            field = by_id.get(m.target_id)
            if field is None:
                continue
            actions.append(WebFieldAction(
                source_label=m.source_label,
                source_value=m.source_value,
                field=field,
                confidence=m.confidence,
                method=m.method,
            ))
        if mapping.unmapped_source:
            logger.info("[MAP] {} unmapped source labels: {}", len(mapping.unmapped_source), mapping.unmapped_source)
        return actions

    # -- filling -------------------------------------------------------------

    def fill_record(
        self,
        record,
        upload_dir: str | None = None,
        source_label: str = "web",
    ) -> FillResult:
        """Discover (if needed), map and fill one record with DOM-first actions."""
        started = time.perf_counter()
        if self._form is None:
            self.discover()
        actions = self.map_record(record)
        result = FillResult()
        for action in actions:
            timing = FieldTiming(field=action.selector, source_label=action.source_label)
            t0 = time.perf_counter()
            try:
                method = self._fill_field(action, upload_dir=upload_dir, source_label=source_label)
                timing.method = method
                timing.action_ms = (time.perf_counter() - t0) * 1000
                v0 = time.perf_counter()
                read = self._read_value(action.field)
                timing.verify_ms = (time.perf_counter() - v0) * 1000
                verified = self._value_matches(action, read)
                if verified:
                    result.filled += 1
                    result.verified += 1
                    self._learner.record(
                        application=source_label, field=action.source_label,
                        method=method, ok=True, elapsed_ms=timing.action_ms,
                    )
                else:
                    result.failed.append(f"{action.source_label} (read {read!r} != {action.source_value!r})")
                    self._learner.record(
                        application=source_label, field=action.source_label,
                        method=method, ok=False, elapsed_ms=timing.action_ms,
                    )
            except Exception as exc:
                result.failed.append(f"{action.source_label}: {exc}")
            timing.total_ms = round((time.perf_counter() - t0) * 1000, 1)
            result.timings.append(timing)
        result.ok = not result.failed
        logger.info(
            "[FILL] {} fields, {} verified, {} failed in {:.0f}ms",
            len(actions), result.verified, len(result.failed), (time.perf_counter() - started) * 1000,
        )
        return result

    def _fill_field(self, action: WebFieldAction, upload_dir: str | None, source_label: str) -> str:
        field = action.field
        value = action.source_value
        locator = self._page.locator(field.selector)
        if not self._wait.visible(locator)():
            try:
                locator.scroll_into_view_if_needed(timeout=self._field_timeout * 1000)
            except Exception:
                pass
        method = self._learner.preferred(source_label, action.source_label)
        if method is None:
            method = self._default_method(field.element_type)
        return self._dispatch(method, locator, field, value, action, upload_dir)

    def _dispatch(
        self, method: str, locator, field: WebFieldDescriptor, value: str,
        action: WebFieldAction, upload_dir: str | None,
    ) -> str:
        if method == "select_option":
            return self._select(locator, field, value)
        if method == "click":
            return self._click_check(locator, field, value, action)
        if method == "radio":
            self._check_radio(action, True)
            return "radio"
        if method == "upload":
            return self._upload(locator, value, upload_dir)
        if method == "keyboard":
            locator.click()
            locator.press_sequentially(value, delay=0.02)
            return "keyboard"
        # DOM fill (default for text/date/textarea/contenteditable).
        try:
            locator.fill(value, timeout=self._field_timeout * 1000)
            return "dom"
        except Exception:
            locator.click(timeout=self._field_timeout * 1000)
            locator.press_sequentially(value, delay=0.02)
            return "keyboard"

    def _select(self, locator, field: WebFieldDescriptor, value: str) -> str:
        if field.element_type == ElementType.COMBOBOX:
            try:
                locator.select_option(label=value, timeout=self._field_timeout * 1000)
                return "select_option"
            except Exception:
                try:
                    locator.select_option(value=value, timeout=self._field_timeout * 1000)
                    return "select_option"
                except Exception:
                    pass
        locator.click()
        locator.press_sequentially(value, delay=0.02)
        self._page.keyboard.press("Enter")
        return "keyboard"

    def _click_check(self, locator, field: WebFieldDescriptor, value: str, action: WebFieldAction) -> str:
        desired = _parse_boolean(value)
        if field.element_type == ElementType.RADIO:
            self._check_radio(action, True)
            return "radio"
        try:
            if desired:
                locator.check(timeout=self._field_timeout * 1000)
            else:
                locator.uncheck(timeout=self._field_timeout * 1000)
            return "click"
        except Exception:
            checked = locator.is_checked()
            if checked != desired:
                locator.click()
            return "click"

    def _check_radio(self, action: WebFieldAction, desired: bool) -> None:
        """Check the radio option whose label/value matches the source value."""
        field = action.field
        base = field.selector
        if field.name:
            base = f'input[name="{_css_escape(field.name)}"]'
        value = action.source_value
        candidates = [
            f"{base}[value=\"{_css_escape(value)}\"]",
            f"{base}[data-value=\"{_css_escape(value)}\"]",
        ]
        for selector in candidates:
            loc = self._page.locator(selector)
            if loc.count():
                loc.check(timeout=self._field_timeout * 1000)
                return
        # fall back to matching by associated label text
        radios = self._page.locator(base)
        count = radios.count()
        for i in range(count):
            radio = radios.nth(i)
            text = (radio.evaluate("el => {const l = el.labels && el.labels[0]; return l ? l.innerText : '';}") or "")
            if _norm(value) in _norm(text) or _norm(text) in _norm(value):
                radio.check(timeout=self._field_timeout * 1000)
                return
        raise RuntimeError(f"radio option {value!r} not found for {field.label!r}")

    def _upload(self, locator, value: str, upload_dir: str | None) -> str:
        path = _materialise_upload(value, upload_dir)
        locator.set_input_files(path, timeout=self._field_timeout * 1000)
        return "upload"

    # -- verification --------------------------------------------------------

    def _read_value(self, field: WebFieldDescriptor) -> str:
        try:
            locator = self._page.locator(field.selector)
            if locator.count() == 0:
                return ""
            if field.element_type == ElementType.CHECKBOX:
                return "checked" if locator.is_checked() else "unchecked"
            if field.element_type == ElementType.RADIO:
                return locator.first.evaluate(
                    """el => {
                        const name = el.getAttribute('name');
                        if (name) {
                            const checked = document.querySelector(
                                'input[type="radio"][name="' + name + '"]:checked');
                            if (checked) {
                                const l = checked.labels && checked.labels[0];
                                return l ? l.innerText.trim() : (checked.value || '');
                            }
                            return '';
                        }
                        if (!el.checked) return '';
                        const l = el.labels && el.labels[0];
                        return l ? l.innerText.trim() : (el.value || '');
                    }"""
                )
            if field.element_type == ElementType.COMBOBOX:
                return locator.evaluate("el => { const i = el.selectedIndex; return i >= 0 ? el.options[i].text.trim() : ''; }")
            if field.element_type == ElementType.FILE_UPLOAD:
                return locator.evaluate("el => el.files && el.files[0] ? el.files[0].name : ''")
            return locator.input_value()
        except Exception:
            return ""

    def _value_matches(self, action: WebFieldAction, read: str) -> bool:
        field = action.field
        if field.element_type == ElementType.CHECKBOX:
            return read == ("checked" if _parse_boolean(action.source_value) else "unchecked")
        if field.element_type == ElementType.RADIO:
            return read == "checked" or _norm(read) == _norm(action.source_value)
        if field.element_type == ElementType.DATE_PICKER:
            return _norm_date(read) == _norm_date(action.source_value) or _norm(read) == _norm(action.source_value)
        if field.element_type == ElementType.COMBOBOX:
            return _norm(read) == _norm(action.source_value)
        return _norm(read) == _norm(action.source_value)

    # -- submit --------------------------------------------------------------

    def submit(self) -> bool:
        """Click the form's submit/save button; return False when none found."""
        selector = self._find_submit_selector()
        if not selector:
            logger.warning("[SUBMIT] no submit button found")
            return False
        try:
            self._page.locator(selector).click(timeout=self._field_timeout * 1000)
            logger.info("[SUBMIT] clicked {}", selector)
            return True
        except Exception as exc:
            logger.warning("[SUBMIT] failed: {}", exc)
            return False

    def _find_submit_selector(self) -> str | None:
        js = """
        () => {
          const b = document.querySelector('button[type="submit"], input[type="submit"], button[type="button"]');
          const buttons = Array.from(document.querySelectorAll('button'));
          const byText = buttons.find(x => /^(submit|save|create|add record|next|finish)$/i.test((x.innerText||'').trim()));
          const pick = b || byText;
          if (!pick) return null;
          if (pick.id) return '#' + CSS.escape(pick.id);
          const txt = (pick.innerText || pick.value || '').trim();
          if (txt) return 'button:has-text("' + txt + '")';
          return null;
        }
        """
        try:
            return self._page.evaluate(js)
        except Exception:
            return None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _default_method(element_type: ElementType) -> str:
        if element_type in {ElementType.COMBOBOX, ElementType.LISTBOX}:
            return "select_option"
        if element_type == ElementType.CHECKBOX:
            return "click"
        if element_type == ElementType.RADIO:
            return "radio"
        if element_type == ElementType.FILE_UPLOAD:
            return "upload"
        if element_type == ElementType.DATE_PICKER:
            return "dom"
        return "dom"


def _q(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _css_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _norm(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _parse_boolean(value: str) -> bool:
    return _norm(value) in {"1", "true", "yes", "y", "on", "checked", "agree"}


def _norm_date(value: str) -> str:
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y"):
        try:
            import datetime

            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return text


def _materialise_upload(value: str, upload_dir: str | None) -> str:
    """Turn a source value into an on-disk file for set_input_files()."""
    import os
    import tempfile
    from pathlib import Path

    if upload_dir:
        base = Path(upload_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / Path(str(value)).name
    else:
        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(value)[1])
        os.close(fd)
        path = Path(tmp)
    if not path.exists():
        path.write_text(str(value), encoding="utf-8")
    return str(path)


__all__ = [
    "WebFormEngine",
    "WebForm",
    "WebFieldDescriptor",
    "WebFieldAction",
    "FieldTiming",
    "FillResult",
    "discover_form_js",
]

#: Alias so external callers can get the raw JS if they need it.
discover_form_js = _DISCOVER_JS
