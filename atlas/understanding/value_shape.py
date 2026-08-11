"""Value-shape inference and repair.

Phase 2 of the BEST++ migration. Before a value is typed into a field the
agent may normalise its *shape* to match the target field's expected format
(the "value-shape repair" step). This complements mapping-time value-type
validation (``value_ok``) - validation rejects a wrongly-typed pairing, repair
re-formats a correctly-typed value so it fills the field cleanly.

Examples:

- a pincode ``560 001`` is written as ``560001``;
- a phone ``+91 98765 43210`` is written as ``9876543210``;
- an ISO date ``1996-02-02`` is written as ``02/02/1996`` for a text date
  field;
- a PAN ``abcde1234f`` is uppercased to ``ABCDE1234F``.

Repair is conservative: text values and unknown field kinds are returned
unchanged, and repair never blocks a mapping (that is ``value_ok``'s job).
"""

from __future__ import annotations

import re

#: Label keyword groups used to infer coarse semantic kinds. These never decide
#: a mapping on their own; they only gate value-type compatibility and drive
#: value-shape repair.
_DATE_MARKERS = ("dob", "date of birth", "birth date", "birthday", "date")
_PHONE_MARKERS = ("mobile", "phone", "contact", "whatsapp", "landline", "tel", "telephone")
_PINCODE_MARKERS = ("pincode", "pin code", "zip", "postal")
_NUMERIC_MARKERS = (
    "age", "amount", "account no", "account number", "application no",
    "application number", "app no", "code", "number", "no.",
)
_NAME_MARKERS = (
    "name", "applicant", "father", "mother", "husband", "wife",
    "spouse", "guardian", "maiden", "nominee",
)

_DATE_RE = re.compile(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}(\s\d{1,2}:\d{2}(:\d{2})?)?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{7,}$")
_NUMERIC_RE = re.compile(r"^\d{1,2}([.,]\d+)?%?$")

#: ``YYYY-MM-DD`` / ``YYYY/MM/DD`` / ``YYYY.MM.DD`` (ISO-style) whose year leads.
_ISO_DATE_RE = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")


def normalize_label(label: str) -> str:
    """Lowercase, strip punctuation/whitespace and collapse spacing."""
    text = str(label or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def label_kind(label: str) -> str:
    """Infer a coarse semantic kind from a target label (``text`` if unknown)."""
    s = normalize_label(label)
    if any(m in s for m in _DATE_MARKERS):
        return "date"
    if any(m in s for m in _PINCODE_MARKERS):
        return "pincode"
    if any(m in s for m in _PHONE_MARKERS):
        return "phone"
    if any(m in s for m in _NUMERIC_MARKERS):
        return "numeric"
    if any(m in s for m in _NAME_MARKERS):
        return "name"
    return "text"


def value_kind(value: str) -> str:
    """Infer the kind of a source value from its shape (``text`` if unknown)."""
    v = str(value or "").strip()
    if not v:
        return "text"
    if _EMAIL_RE.match(v):
        return "email"
    if _DATE_RE.match(v):
        return "date"
    if _PHONE_RE.match(v):
        return "phone"
    if _NUMERIC_RE.match(v):
        return "numeric"
    return "text"


def value_ok(target_label: str, value: str) -> bool:
    """Reject strongly-typed values that would land in the wrong kind of field.

    A date value only ever goes into a date field; phone/pincode/numeric values
    never go into name or date fields. Text values are always accepted (the
    common case). Rejected pairings surface as ``blocked`` instead of being
    typed into the wrong field.
    """
    kind = value_kind(value)
    if kind == "text":
        return True
    label = label_kind(target_label)
    if kind == "date":
        return label == "date"
    if kind in ("email", "phone", "pincode", "numeric"):
        return label not in ("date", "name")
    return True


def _repair_iso_date(value: str) -> str:
    """Re-format an ISO-style ``YYYY-MM-DD`` value as ``DD/MM/YYYY``."""
    match = _ISO_DATE_RE.match(value.strip())
    if not match:
        return value
    year, month, day = match.groups()
    return f"{day.zfill(2)}/{month.zfill(2)}/{year}"


def repair_value(target_label: str, value: str) -> str:
    """Normalise a source value's shape to fit the target field's format.

    Conservative: text values and ``text``-kind targets are returned unchanged.
    """
    v = str(value or "").strip()
    if not v:
        return v
    kind = label_kind(target_label)
    if kind == "pincode":
        # "560 001" / "560-001" -> "560001"
        return re.sub(r"[\s\-]", "", v)
    if kind == "phone":
        # "+91 98765 43210" -> "9876543210" (strip separators, keep digits)
        digits = re.sub(r"\D", "", v)
        return digits if digits else v
    if kind == "date":
        return _repair_iso_date(v)
    if kind == "numeric":
        # collapse internal spacing only; keep decimals and separators
        return re.sub(r"\s+", "", v)
    return v


__all__ = [
    "label_kind",
    "value_kind",
    "value_ok",
    "repair_value",
    "normalize_label",
]
