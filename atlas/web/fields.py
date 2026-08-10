"""Form field helpers for DOM automation.

Pure helpers for the universal web flow: a DOM fingerprint snippet used with
``SmartWait.dom_change``, label normalisation, locator construction and the
default interaction-method ordering for a field type.
"""

from __future__ import annotations

from atlas.vision.models import ElementType


def normalize_label(label: str) -> str:
    """Lowercase, collapse whitespace and strip common punctuation."""
    if not label:
        return ""
    normalized = " ".join(label.lower().split())
    for ch in "():*?-.":
        normalized = normalized.replace(ch, "")
    return normalized.strip()


def field_fingerprint_js() -> str:
    """JavaScript that fingerprints the current form.

    The result is stable for an unchanged form and changes when fields are
    added/removed/reloaded, making it a good SmartWait baseline:
    ``{"fields": n, "labels": [...], "options": [...]}``.
    """
    return """
    () => {
      const fields = Array.from(document.querySelectorAll(
        'input:not([type="hidden"]):not([type="button"]):not([type="submit"])'
        + ', select, textarea, [contenteditable="true"]'
      ));
      const labelFor = (el) => {
        if (el.labels && el.labels[0]) return el.labels[0].innerText.trim();
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
        if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
        const lbl = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lbl) return lbl.innerText.trim();
        return (el.getAttribute('name') || el.getAttribute('autocomplete') || '').replace(/[_-]+/g, ' ');
      };
      return {
        fields: fields.length,
        labels: fields.map(labelFor).filter(Boolean).slice(0, 20),
        options: fields
          .filter((el) => el.tagName === 'SELECT')
          .slice(0, 5)
          .map((el) => Array.from(el.options).map((o) => o.text.trim())),
      };
    }
    """


def build_locator(entry: dict[str, Any]) -> str:
    """Build a best-effort CSS locator for a DOM entry dict.

    Entries come from the page's own DOM inspection (the ``atlas.target.web``
    index) or from tab CDP evaluation; each carries ``id`` / ``name`` /
    ``aria`` / ``placeholder`` attributes.
    """
    ident = ""
    if entry.get("id"):
        ident = f"#{_css_escape(entry['id'])}"
    elif entry.get("name"):
        ident = f"[name={_css_string(entry['name'])}]"
    elif entry.get("aria"):
        ident = f"[aria-label={_css_string(entry['aria'])}]"
    elif entry.get("placeholder"):
        ident = f"[placeholder={_css_string(entry['placeholder'])}]"
    if not ident:
        return ""
    type_attr = entry.get("type") or entry.get("typeAttr") or ""
    if type_attr:
        return f"{ident}[type={_css_string(type_attr)}]"
    return ident


def rank_methods_for(element_type: ElementType) -> list[str]:
    """Default interaction-method order for a field type (before learning)."""
    if element_type in {ElementType.COMBOBOX, ElementType.LISTBOX}:
        return ["select_option", "dom", "keyboard", "uia", "vision"]
    if element_type == ElementType.CHECKBOX:
        return ["click", "dom", "uia", "vision"]
    if element_type == ElementType.DATE_PICKER:
        return ["dom", "uia", "keyboard", "vision"]
    if element_type in {ElementType.TEXTBOX, ElementType.TEXTAREA}:
        return ["dom", "uia", "keyboard", "vision", "ocr"]
    if element_type == ElementType.FILE_UPLOAD:
        return ["upload", "uia", "vision"]
    return ["uia", "vision", "ocr"]


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = ["normalize_label", "field_fingerprint_js", "build_locator", "rank_methods_for"]
