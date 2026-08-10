"""Verification of executed actions.

Every value-producing action is verified before the agent continues. Multiple
strategies are composed:

1. target adapter verification (e.g. DOM ``value`` for web targets),
2. clipboard read-back (select-all + copy on desktop text fields),
3. vision read-back (OCR the field region after the action).

Verification failure never continues blindly - the executor retries and, if it
still fails, the recovery planner decides what to do.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from atlas.act.clipboard import ClipboardEngine
from atlas.act.keyboard import HumanKeyboard
from atlas.core.logging import logger
from atlas.vision.models import BBox, OcrText


class FieldVerifier(ABC):
    """A verification strategy."""

    name = "abstract"

    @abstractmethod
    def verify(self, bbox: BBox | None, expected: str, field_id: str | None = None) -> tuple[bool, str]:
        """Return (matched, evidence)."""


#: Boolean value synonyms collapsed to "1"/"0" before comparison, so checkbox
#: and radio verification is robust across sources ("Yes"/"checked"/"on"/"true").
_TRUE_VALUES = {"yes", "y", "true", "t", "1", "on", "checked", "selected", "x"}
_FALSE_VALUES = {"no", "n", "false", "f", "0", "off", "unchecked", "unselected", ""}

#: Trailing single-character markers OCR of a focused field often appends
#: (the caret/selection indicator). Stripped by :func:`normalize_ocr_text`.
_OCR_TRAILING_MARKERS = {"v", "|"}


def normalize_ocr_text(value: str) -> str:
    """Strip OCR cursor/selection artifacts from a read-back before comparison.

    OCR of a focused field frequently appends a stray standalone character -
    most commonly a trailing ``V`` (the caret/selection indicator), sometimes
    a pipe. ``Telugu V`` must compare equal to ``Telugu``, ``1996 V`` to
    ``1996`` and ``Kataka / Cancer V`` to ``Kataka / Cancer``. Only a
    *standalone trailing* marker is removed, so a genuine ``Value`` (no space)
    is never harmed and ``1`` (a real digit token) is never touched.
    """
    text = re.sub(r"[\s_\-]+", " ", str(value).strip())
    tokens = text.split()
    if len(tokens) >= 2 and tokens[-1].lower() in _OCR_TRAILING_MARKERS:
        return " ".join(tokens[:-1])
    return text


def normalize_for_compare(value: str) -> str:
    """Normalize values for comparison: strip, collapse spaces, lowercase.

    Handles currency symbols, thousand separators and bullet/no-break spaces so
    that ``$1,234`` compares equal to ``1234`` and ``50.00`` to ``50.00``. It
    deliberately does NOT strip the decimal point so ``1.5`` never equals ``15``.
    """
    text = " ".join(str(value).strip().split())
    lowered = text.lower()
    if lowered in _TRUE_VALUES:
        return "1"
    if lowered in _FALSE_VALUES:
        return "0"
    normalized = re.sub(r"[\s_\-]+", " ", lowered).strip()
    # Strip leading/trailing currency symbols only when part of a number.
    normalized = re.sub(r"[$\u20ac\u00a3\u00a5\u20b9\u00a2](?=\d)", "", normalized)
    normalized = re.sub(r"(?<=\d)[$\u20ac\u00a3\u00a5\u20b9\u00a2]", "", normalized)
    # Collapse thousand separators (digit,comma,digits) - preserves single comma decimals.
    normalized = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", normalized)
    return re.sub(r"[\s\-]+", " ", normalized).strip()


def _contains_token(a: str, e: str) -> bool:
    """True when ``e`` appears as a whole token within ``a`` (not a substring).

    Prevents expected ``"John"`` passing a field that really holds ``"Johnny"``.
    Empty expected never "contains". Numeric expected matches only when ``a``
    contains the exact normalized number as a token.

    Separator runs (``/``, ``.``, ``,``, ``;``, ``:``, backslash) are ignored
    when the plain whole-token search fails: OCR/vision reads often drop the
    spaces around punctuation (expected ``"Kataka / Cancer"`` read back as
    ``"Kataka /Cancer V"``), so ``"Kataka Cancer"`` must still match inside
    ``"Kataka Cancer V"``. Whole-token boundaries are still enforced, so
    ``"John Smith"`` never matches ``"John Smithson"`` and ``"1 / 2"`` never
    matches ``"12"``.
    """
    if not e:
        return False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", e):
        return e in a.split()
    if re.search(r"(^|[\s()\[\]{}.,;:-])" + re.escape(e) + r"($|[\s()\[\]{}.,;:-])", a) is not None:
        return True
    _sep = re.compile(r"\s*[/\\.,;:]\s*")
    e2 = re.sub(r"\s+", " ", _sep.sub(" ", e)).strip()
    a2 = re.sub(r"\s+", " ", _sep.sub(" ", a)).strip()
    if e2 and e2 != e:
        return re.search(r"(^|[\s()\[\]{}.,;:-])" + re.escape(e2) + r"($|[\s()\[\]{}.,;:-])", a2) is not None
    return False


def looks_like_whole_window(text: str) -> bool:
    """Heuristic: is this clipboard read-back the whole window, not one field?

    In Chromium/Electron targets Ctrl+A selects the entire page, so the
    clipboard read-back contains far more than a single field's value (labels,
    the source panel, the window title, ...). Field values are single, short
    lines; whole-window grabs are multi-line and long. When a read-back looks
    like a whole-window grab it must NOT be used to confirm a field, otherwise
    "contains expected" passes trivially because the expected value already
    exists somewhere in the page.
    """
    if not text:
        return False
    stripped = text.strip()
    if "\n" in stripped:
        return True
    return len(stripped) > 250


def _file_match(actual: str, expected: str) -> bool:
    """Match file-upload read-backs against an expected path.

    Browsers expose file inputs as ``C:\\fakepath\\<basename>`` (and the
    separator can vary), so compare the basenames case-insensitively and also
    accept the expected path appearing anywhere in the read-back.
    """
    if not actual or not expected:
        return False
    expected_lower = expected.lower().replace("\\", "/")
    actual_lower = actual.lower().replace("\\", "/")
    if expected_lower in actual_lower:
        return True
    expected_base = expected_lower.rsplit("/", 1)[-1]
    actual_base = actual_lower.rsplit("/", 1)[-1]
    return bool(expected_base) and expected_base == actual_base


#: Month names / abbreviations -> month number, used by date-aware comparison.
_MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_PLACEHOLDER_WORDS = {
    "select", "choose", "pick", "please", "none", "dd", "mm", "yy", "yyyy",
    "hh", "hhmm", "option", "options",
}


def is_placeholder(text: str) -> bool:
    """True when a read-back value looks like an unfilled select placeholder.

    Chromium/Electron combos display "Select" / "-- Select --" / "DD" until a
    real value is chosen. Treating those as a match would falsely confirm a
    field that was never filled, so every verifier treats a placeholder read-
    back as a mismatch.
    """
    if not text:
        return False
    t = re.sub(r"[\s_\-–—.]+", " ", str(text).strip().lower())
    if not t:
        return True
    if re.fullmatch(r"(?:-|--|…|\.\.\.|:|\|)+", t):
        return True
    if any(ch.isdigit() for ch in t):
        return False
    meaningful = [w for w in t.split() if w not in {"an", "a", "one", "the", "to", "of", "in", "here", "from", "value", "list", "item"}]
    if not meaningful:
        return False
    return all(w in _PLACEHOLDER_WORDS for w in meaningful)


def date_tokens(value: str) -> tuple[int, int, int] | None:
    """Parse a date-ish string into ``(day, month, year)``; None if not a date.

    Handles ``D-M-YYYY``, ``MM/DD/YYYY``, ``YYYY-MM-DD`` (ISO) and word months
    (``02 February 1996``). The 4-digit token unambiguously pins the year, so
    day/month ordering is decided by which end holds it.
    """
    if not value:
        return None
    text = re.sub(r"[^0-9a-z/.\-: ]+", " ", str(value).strip().lower())
    tokens = [t for t in re.split(r"[-/.:\s]+", text) if t]
    if len(tokens) != 3:
        return None

    def _to_month(tok: str) -> int | None:
        if tok in _MONTH_NAMES:
            return _MONTH_NAMES[tok]
        if tok.isdigit() and 1 <= int(tok) <= 12:
            return int(tok)
        return None

    def _day(tok: str) -> int | None:
        return int(tok) if tok.isdigit() and 1 <= int(tok) <= 31 else None

    t0, t1, t2 = tokens
    if t2.isdigit() and 1900 <= int(t2) <= 2100:
        day, month = _day(t0), _to_month(t1)
        if day is None or month is None:
            # Not D/M/Y - try M/D/Y (US "12/31/2000") before giving up.
            day, month = _day(t1), _to_month(t0)
        if day is None or month is None:
            return None
        return (day, month, int(t2))
    if t0.isdigit() and 1900 <= int(t0) <= 2100:
        day, month = _day(t2), _to_month(t1)
        if day is None or month is None:
            return None
        return (day, month, int(t0))
    return None


def dates_match(actual: str, expected: str) -> bool:
    """Date-aware comparison: any date spelling vs any other date spelling.

    Lets ``"1996-02-02"`` (ISO source record) verify against an OCR/UIA
    read-back of ``"02 02 1996"`` (day/month/year combo triplet) - the exact
    mismatch the field-driven DOB group used to fail on.
    """
    ta, te = date_tokens(actual), date_tokens(expected)
    return ta is not None and te is not None and ta == te



class TargetFieldVerifier(FieldVerifier):
    """Delegates verification to the target adapter (e.g. DOM value read)."""

    name = "target"

    def __init__(self, get_value: Callable[[Any], str | None]) -> None:
        self._get_value = get_value

    def verify(self, bbox: BBox | None, expected: str, field_id: str | None = None) -> tuple[bool, str]:
        try:
            actual = self._get_value(field_id)
        except Exception as exc:
            return False, f"target read failed: {exc}"
        if actual is None:
            return False, "no value available"
        return self._compare(actual, expected)

    @staticmethod
    def _compare(actual: str, expected: str) -> tuple[bool, str]:
        a, e = normalize_for_compare(actual), normalize_for_compare(expected)
        if is_placeholder(actual):
            return False, f"target read placeholder ({actual!r})"
        if a == e:
            return True, f"target read-back matched ({actual!r})"
        if _contains_token(a, e):
            return True, f"target read-back contains expected ({actual!r})"
        if dates_match(actual, expected):
            return True, f"target read-back date matched ({actual!r} ~ {expected!r})"
        if _file_match(actual, expected):
            return True, f"target read-back file matched ({actual!r})"
        return False, f"target read-back mismatch: got {actual!r}, expected {expected!r}"


class ClipboardVerifier(FieldVerifier):
    """Select-all + copy and compare with the expected value."""

    name = "clipboard"

    def __init__(self, keyboard: HumanKeyboard, clipboard: ClipboardEngine) -> None:
        self._keyboard = keyboard
        self._clipboard = clipboard

    def verify(self, bbox: BBox | None, expected: str, field_id: str | None = None) -> tuple[bool, str]:
        try:
            actual = self._clipboard.read_focused()
        except Exception as exc:
            return False, f"clipboard read failed: {exc}"
        if looks_like_whole_window(actual):
            # Deliberately do NOT embed the grabbed text: in Chromium/Electron
            # targets it is the entire page (labels + source panel + title) and
            # dumping it into evidence/observed leaks the whole window.
            return False, f"clipboard read-back is whole-window (not field-scoped): {len(actual)} chars"
        return self._compare(actual, expected)

    @staticmethod
    def _compare(actual: str, expected: str) -> tuple[bool, str]:
        a, e = normalize_for_compare(actual), normalize_for_compare(expected)
        if is_placeholder(actual):
            return False, f"clipboard read placeholder ({actual!r})"
        if a == e:
            return True, f"clipboard matched ({actual!r})"
        if _contains_token(a, e):
            return True, f"clipboard contains expected ({actual!r})"
        return False, f"clipboard mismatch: got {actual!r}, expected {expected!r}"


class VisionVerifier(FieldVerifier):
    """Re-captures the field region and OCR-reads it for comparison.

    OCR is used here deliberately - this is the explicit "read what's in the
    field" request, not part of scene understanding.
    """

    name = "vision"

    #: A capture landing mid-repaint (dropdown just closed, scroll just
    #: settled, a Chromium/Electron paint frame not yet flushed) reads as
    #: zero OCR boxes even though the field genuinely has content. A real,
    #: sustained blank costs nothing extra to detect correctly; a transient
    #: one is cheap to rule out with one short recapture before reporting a
    #: mismatch that triggers a full, much more expensive action retry.
    _EMPTY_RECAPTURE_DELAY = 0.35

    def __init__(self, read_region: Callable[[BBox], list[OcrText]]) -> None:
        """``read_region(image_source, bbox)`` returns OCR lines for the region."""
        self._read_region = read_region

    def verify(self, bbox: BBox | None, expected: str, field_id: str | None = None) -> tuple[bool, str]:
        if bbox is None:
            return False, "no bbox for vision verification"
        try:
            lines = self._read_region(bbox)
        except Exception as exc:
            return False, f"vision read failed: {exc}"
        actual = " ".join(line.text for line in lines)
        if not actual:
            # One short, cheap recapture before trusting a blank read - see
            # `_EMPTY_RECAPTURE_DELAY`. This does not change behaviour for a
            # field that is genuinely empty (it will still read empty on the
            # recapture and correctly report "vision read empty"); it only
            # rescues a field that has content but was caught mid-repaint.
            time.sleep(self._EMPTY_RECAPTURE_DELAY)
            try:
                lines = self._read_region(bbox)
            except Exception as exc:
                return False, f"vision read failed: {exc}"
            actual = " ".join(line.text for line in lines)
        if not actual:
            return False, "vision read empty"
        return self._compare(actual, expected)

    @staticmethod
    def _compare(actual: str, expected: str) -> tuple[bool, str]:
        a, e = normalize_for_compare(normalize_ocr_text(actual)), normalize_for_compare(expected)
        if is_placeholder(actual):
            return False, f"vision read placeholder ({actual!r})"
        if a == e:
            return True, f"vision matched ({actual!r})"
        if _contains_token(a, e):
            return True, f"vision contains expected ({actual!r})"
        if dates_match(actual, expected):
            return True, f"vision date matched ({actual!r} ~ {expected!r})"
        return False, f"vision mismatch: got {actual!r}, expected {expected!r}"


class UiaValueVerifier(FieldVerifier):
    """Reads the live control value straight from the UI Automation tree.

    Unlike OCR (which reads whatever pixels happen to be drawn at the bbox) and
    clipboard read-back (whose Ctrl+A grabs the whole page in Chromium apps),
    UIA reports the control's actual text/value no matter what occludes it, so
    this is the FIRST strategy for desktop targets: a match here is
    authoritative. Date-aware comparison is applied so a day/month/year triplet
    read-back matches the ISO spelling of the source record.
    """

    name = "uia"

    def __init__(self, read_text: Callable[[BBox], str | None]) -> None:
        """``read_text(bbox)`` returns the UIA text under the region or None."""
        self._read_text = read_text

    def verify(self, bbox: BBox | None, expected: str, field_id: str | None = None) -> tuple[bool, str]:
        if bbox is None:
            return False, "no bbox for uia verification"
        try:
            actual = self._read_text(bbox)
        except Exception as exc:
            return False, f"uia read failed: {exc}"
        if not actual or not actual.strip():
            return False, "uia read empty"
        if is_placeholder(actual):
            return False, f"uia read placeholder ({actual!r})"
        return self._compare(actual, expected)

    @classmethod
    def _compare(cls, actual: str, expected: str) -> tuple[bool, str]:
        a, e = normalize_for_compare(actual), normalize_for_compare(expected)
        if a == e:
            return True, f"uia matched ({actual!r})"
        if _contains_token(a, e):
            return True, f"uia contains expected ({actual!r})"
        if dates_match(actual, expected):
            return True, f"uia date matched ({actual!r} ~ {expected!r})"
        if _file_match(actual, expected):
            return True, f"uia file matched ({actual!r})"
        return False, f"uia mismatch: got {actual!r}, expected {expected!r}"


class CompositeVerifier:
    """Runs verification strategies in order until one matches."""

    def __init__(self, verifiers: list[FieldVerifier] | None = None) -> None:
        self._verifiers: list[FieldVerifier] = verifiers or []

    def add(self, verifier: FieldVerifier) -> None:
        self._verifiers.append(verifier)

    @property
    def strategies(self) -> list[str]:
        return [v.name for v in self._verifiers]

    def verify(self, bbox: BBox | None, expected: str, field_id: str | None = None) -> tuple[bool, str]:
        if not self._verifiers:
            logger.debug("no verifier strategies configured - verification skipped")
            return False, "no verifier configured"
        failures: list[str] = []
        for verifier in self._verifiers:
            ok, evidence = verifier.verify(bbox, expected, field_id)
            if ok:
                return True, evidence
            failures.append(evidence)
        return False, " | ".join(failures)


__all__ = [
    "FieldVerifier", "CompositeVerifier", "ClipboardVerifier", "VisionVerifier",
    "UiaValueVerifier", "TargetFieldVerifier", "normalize_for_compare",
    "normalize_ocr_text", "looks_like_whole_window", "_file_match", "_contains_token",
    "is_placeholder", "date_tokens", "dates_match",
]
