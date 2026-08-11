"""Field-driven fill engine.

A performance-oriented alternative to the viewport-round reveal pass
(``atlas.workflow.loop``): instead of observe -> fill visible -> gate -> scroll
BOTH panels -> full VLM re-observe, the engine builds ONE ordered queue of
targets from the full UIA field map (which already contains below-fold fields)
and walks it field by field:

* **Stable identity** - a target is keyed by ``handle``, then
  ``automation_id + control_type``, then ``name + visual order``. Its bbox is
  position only, so a scroll refreshes the position without losing the field.
* **UIA-only position refresh** - ``field_map_refresh`` re-queries UIA; no VLM
  re-observe per scroll. A full VLM observe is reserved for the post-submit
  success check.
* **RIGHT-panel-only scrolling** - the left source panel is never scrolled once
  the source is cached.
* **Scroll capability cache** - the method that first moves a container is
  remembered (pattern -> dom -> wheel -> keyboard priority), so later scrolls
  skip the full escalation ladder.
* **Adaptive scroll distance** - estimated gap to the next target clamped to
  [120, 700] px, never a fixed 250-350 px band.
* **ProgressGuard** - a hard per-field timeout so one stuck field can never
  stall the record forever; a failed field is retried once then skipped.

This module is pure logic (duck-typed UIA nodes) so it is fully unit-testable.
"""

from __future__ import annotations

import enum
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from atlas.act.models import Action, ActionType
from atlas.understanding.value_shape import repair_value
from atlas.vision.models import BBox, ElementType
from atlas.workflow.scroller import (
    SCROLL_METHOD_DOM,
    SCROLL_METHOD_KEYBOARD,
    SCROLL_METHOD_PATTERN,
    SCROLL_METHOD_SCROLLBAR_DRAG,
    SCROLL_METHOD_WHEEL,
)

#: Adaptive scroll band (px) used by :class:`TargetNavigator`.
MIN_SCROLL_PX = 120
MAX_SCROLL_PX = 700

#: Default per-field hard timeout (s) used by :class:`ProgressGuard`.
DEFAULT_FIELD_TIMEOUT = 20.0

#: Default scroll attempts per target before the field is marked failed.
DEFAULT_SCROLL_ATTEMPTS = 6

#: Default retries for a field whose fill action fails.
DEFAULT_FIELD_RETRIES = 1


class FieldStatus(str, enum.Enum):
    """Explicit lifecycle status for one queued field.

    Every target moves through exactly one terminal status - a field is never
    removed from the queue without a reason (no silent skips). ``VERIFIED`` and
    ``ALREADY_CORRECT`` are the only statuses that satisfy the submit gate for
    a source-backed field.
    """

    PENDING = "PENDING"                      # in the queue, not yet worked
    IN_PROGRESS = "IN_PROGRESS"              # fill actions being executed
    FILLED = "FILLED"                        # written, verification UNKNOWN
    VERIFIED = "VERIFIED"                    # written and read-back matched
    ALREADY_CORRECT = "ALREADY_CORRECT"      # no-op: already held the value
    FAILED = "FAILED"                        # could not be filled
    RETRY_PENDING = "RETRY_PENDING"          # failed once, queued for re-run
    UNMAPPED = "UNMAPPED"                    # no mapping produced a source
    NO_SOURCE = "NO_SOURCE"                  # mapped but source value is empty
    NOT_APPLICABLE = "NOT_APPLICABLE"        # control type not fillable


#: Statuses that count as "handled" for the remaining/pending counters.
#: ``RETRY_PENDING`` is deliberately NOT terminal - it re-enters the queue.
_TERMINAL = {
    FieldStatus.FILLED,
    FieldStatus.VERIFIED,
    FieldStatus.ALREADY_CORRECT,
    FieldStatus.FAILED,
    FieldStatus.UNMAPPED,
    FieldStatus.NO_SOURCE,
    FieldStatus.NOT_APPLICABLE,
}

#: Statuses that satisfy the submit gate for a source-backed field.
_SUBMIT_OK = {FieldStatus.VERIFIED, FieldStatus.ALREADY_CORRECT}


def classify_fill_status(results: list[Any]) -> FieldStatus:
    """Derive a target's terminal status from its fill action results.

    Any ``ALREADY_CORRECT`` result wins (the write was genuinely skipped);
    otherwise a fully-verified fill is ``VERIFIED`` and a write whose
    verification came back UNKNOWN is ``FILLED`` (written but unconfirmed).
    """
    if not results:
        return FieldStatus.NOT_APPLICABLE
    for r in results:
        if getattr(r, "verification_status", "") == "ALREADY_CORRECT":
            return FieldStatus.ALREADY_CORRECT
    verified = bool(results) and all(
        getattr(r, "ok", False) and getattr(r, "verified", False)
        for r in results
    )
    return FieldStatus.VERIFIED if verified else FieldStatus.FILLED


def _clean_label(text: str) -> str:
    return re.sub(r"[:：\s]+$", "", (text or "")).strip()


def split_date_parts(value: str | None) -> list[str]:
    """Split a date value into up to 3 parts by ``- / . :`` or whitespace."""
    parts = [p for p in re.split(r"[-/.:\s]+", (value or "").strip()) if p]
    return parts


def _date_parts(value: str | None) -> list[str]:
    """Split a date into ``[day, month, year]`` order.

    ``split_date_parts`` keeps source order, which is day/month/year for most
    spellings but year/month/day for ISO (``1996-02-02``). The 4-digit token
    unambiguously pins the year, so ISO is reordered to ``[02, 02, 1996]`` so
    the parts can be assigned straight to the Day/Month/Year combos.
    """
    parts = split_date_parts(value)
    if len(parts) != 3:
        return parts
    p0, p1, p2 = parts
    if p0.isdigit() and len(p0) == 4:
        return [p2, p1, p0]
    if p2.isdigit() and len(p2) == 4:
        return parts
    return parts


def _looks_like_date(value: str) -> bool:
    """Heuristic: is this record value a calendar date?

    Requires exactly three tokens, a 4-digit 19xx/20xx year, and at least one
    other numeric or month-name token. A bare number, name or phone never
    matches.
    """
    parts = split_date_parts(value)
    if len(parts) != 3:
        return False
    if not any(p.isdigit() and 1900 <= int(p) <= 2100 for p in parts):
        return False
    return any(p.isdigit() or p.lower() in {
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept",
        "oct", "nov", "dec", "january", "february", "march", "april", "june",
        "july", "august", "september", "october", "november", "december",
    } for p in parts)


def _find_date_value(pairs: dict[str, str]) -> str | None:
    """The record pair that looks like a birth date (label or value).

    Prefers a pair whose LABEL signals a date (Date of Birth / DOB / ...),
    falling back to any value that parses as a date - so a form whose three
    date combos carry no label at all still gets its Day/Month/Year parts.
    """
    for label, value in pairs.items():
        if re.search(r"date|dob|birth", label, re.I) and _looks_like_date(value):
            return value
    for _label, value in pairs.items():
        if _looks_like_date(value):
            return value
    return None


def _node_match_key(node: Any, group_ordinal: int) -> tuple:
    """Stable key for a UIA node: handle -> automation_id -> name+visual order."""
    handle = getattr(node, "handle", None)
    if handle not in (None, 0):
        return ("h", int(handle))
    ctype = getattr(node, "control_type", "") or ""
    aid = (getattr(node, "automation_id", "") or "").strip()
    if aid:
        return ("aid", ctype, aid)
    name = (getattr(node, "name", "") or "").strip()
    return ("n", ctype, name, int(group_ordinal))


def _index_nodes(nodes: list[Any]) -> list[tuple[tuple, Any]]:
    """Sort nodes by (top, left) and assign stable match keys in that order."""
    valid = [n for n in nodes if getattr(n, "rect", None) is not None]
    valid.sort(key=lambda n: (n.rect.top, n.rect.left))
    out: list[tuple[tuple, Any]] = []
    counts: dict[tuple, int] = {}
    for node in valid:
        ctype = getattr(node, "control_type", "") or ""
        name = (getattr(node, "name", "") or "")
        group_key = (ctype, name)
        ordinal = counts.get(group_key, 0)
        counts[group_key] = ordinal + 1
        out.append((_node_match_key(node, ordinal), node))
    return out


def field_coverage_summary(queue: Any) -> dict:
    """Target-based inventory ledger: every form control is accounted for.

    Counted against the QUEUE (the right-panel targets), not the source fields,
    so ``mapped_pct`` answers "how much of the FORM is fillable" instead of
    "how much of the source was consumed". Unmapped targets stay visible with
    their exact status - the anti-silent-skip ledger used by the report.
    """
    items = list(getattr(queue, "items", None) or [])
    total = len(items)
    mapped = [it for it in items if it.source_backed]
    unmapped = [it for it in items if not it.source_backed]
    return {
        "total_targets": total,
        "mapped_targets": len(mapped),
        "unmapped_targets": len(unmapped),
        "mapped_pct": (len(mapped) / total) if total else 0.0,
        "unmapped": [
            {
                "label": it.label or it.stable_id,
                "stable_id": it.stable_id,
                "status": getattr(it.status, "value", ""),
                "reason": it.status_reason,
            }
            for it in unmapped
        ],
    }


@dataclass
class FieldTarget:
    """One fillable form control plus the source value bound to it."""

    node: Any
    value: str | None
    ordinal: int
    retries: int = 0
    done: bool = False
    failed: bool = False
    _match_key: tuple = field(default=(), repr=False)
    status: FieldStatus = FieldStatus.PENDING
    status_reason: str = ""

    @property
    def stable_id(self) -> str:
        return ":".join(str(p) for p in self._match_key)

    @property
    def label(self) -> str:
        return _clean_label(getattr(self.node, "name", "") or "")

    @property
    def control_type(self) -> str:
        return getattr(self.node, "control_type", "") or ""

    @property
    def element_type(self) -> ElementType | None:
        return getattr(self.node, "element_type", None)

    @property
    def options(self) -> list[str]:
        return list(getattr(self.node, "options", None) or [])

    @property
    def bbox(self) -> BBox | None:
        return getattr(self.node, "rect", None)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.node, "enabled", True))

    @property
    def source_backed(self) -> bool:
        """True when a source value is bound to this field."""
        return self.value is not None and str(self.value).strip() != ""

    @property
    def document_order(self) -> int:
        """Deterministic reading order (the builder's sort position)."""
        return self.ordinal

    @property
    def section(self) -> str:
        """Grouping hint from the UIA parent, when present."""
        return (getattr(self.node, "section", None) or "").strip()

    @property
    def dependency_ids(self) -> tuple:
        """Stable ids this field depends on (cascading parents), if declared."""
        deps = getattr(self.node, "dependencies", None) or ()
        return tuple(deps)

    @property
    def current_value(self) -> str | None:
        """Live value already in the control, if the UIA node exposes one."""
        return getattr(self.node, "value", None)

    @property
    def placeholder(self) -> str:
        return (getattr(self.node, "placeholder", None) or "").strip()


@dataclass
class DateGroupTarget:
    """Three adjacent date-part controls (day/month/year) treated as one fill."""

    targets: list[FieldTarget]
    retries: int = 0
    done: bool = False
    failed: bool = False
    #: The source value the whole group was filled from (e.g. ``1996-02-02``),
    #: used by the whole-group post-fill verification.
    date_value: str = ""
    status: FieldStatus = FieldStatus.PENDING
    status_reason: str = ""

    @property
    def stable_id(self) -> str:
        return "date+" + "+".join(t.stable_id for t in self.targets)

    @property
    def label(self) -> str:
        return "DOB"

    @property
    def value(self) -> str:
        for t in self.targets:
            if t.value:
                return t.value
        return ""

    @property
    def source_backed(self) -> bool:
        return any(t.source_backed for t in self.targets)

    @property
    def bbox(self) -> BBox | None:
        """The union box spanning all three part controls.

        Used by whole-group verification (one OCR/UIA read over Day+Month+Year)
        and by visibility/scroll checks, which should treat the triplet as one
        control rather than only its leftmost part.
        """
        rects = [t.bbox for t in self.targets if t.bbox is not None]
        if not rects:
            return None
        left = min(r.left for r in rects)
        top = min(r.top for r in rects)
        right = max(r.right for r in rects)
        bottom = max(r.bottom for r in rects)
        return BBox(left, top, right - left, bottom - top)

    @property
    def enabled(self) -> bool:
        return all(t.enabled for t in self.targets)


def _date_token_score(target: FieldTarget) -> int:
    label = _clean_label(target.label).lower()
    if not label:
        return 0
    if any(tok in label for tok in ("birth", "dob", "date")):
        return 2
    words = set(re.findall(r"[a-z]+", label))
    if words & {"day", "days", "month", "year", "dd", "mm", "yy", "yyyy"}:
        return 1
    return 0


def _is_combo(target: FieldTarget) -> bool:
    if target.control_type == "ComboBox":
        return True
    etype = target.element_type
    return etype in (ElementType.COMBOBOX, ElementType.CALENDAR, ElementType.DATE_PICKER)


def _same_row(a: FieldTarget, b: FieldTarget, tol: int = 12) -> bool:
    """Do the two controls sit on the same visual line (within ``tol`` px)?"""
    ra, rb = a.bbox, b.bbox
    return ra is not None and rb is not None and abs(ra.top - rb.top) <= tol


def _adjacent(a: FieldTarget, b: FieldTarget, max_gap: int = 48) -> bool:
    """Is ``b`` immediately to the right of ``a`` with at most ``max_gap`` px?"""
    ra, rb = a.bbox, b.bbox
    return ra is not None and rb is not None and 0 <= rb.left - ra.right <= max_gap


def _parent_key(node: Any) -> tuple:
    parent = getattr(node, "parent", None)
    if isinstance(parent, dict):
        return (parent.get("control_type") or "", parent.get("name") or "")
    return ("", "")


def _same_parent(run: list[FieldTarget]) -> bool:
    """All three controls must share one UIA parent (a field-row/group)."""
    return len({_parent_key(t.node) for t in run}) == 1


def _looks_like_date_triplet(run: list[FieldTarget]) -> bool:
    """Structural date detection for three UNNAMED adjacent combo/edits.

    MPF's Date of Birth is three ComboBoxes whose UIA names are all empty - so
    no label-based grouping can fire. The structural fingerprint of a date
    triplet: three combo-ish controls on one row, tightly adjacent, in one
    parent, with a narrow first part (day) and non-identical widths. Detecting
    this BEFORE binding values lets the engine fill DOB even when neither the
    label nor the mapping names the control.
    """
    if not all(not _clean_label(t.label) for t in run):
        return False
    if not all(t.bbox is not None for t in run):
        return False
    if not (_same_row(run[0], run[1]) and _same_row(run[1], run[2])):
        return False
    if not (_adjacent(run[0], run[1]) and _adjacent(run[1], run[2])):
        return False
    if not _same_parent(run):
        return False
    widths = [t.bbox.width for t in run]  # type: ignore[misc]
    # day (first) is the narrowest; month/year widths differ.
    return widths[0] <= widths[1] and widths[0] <= widths[2] and widths[1] != widths[2]


def _group_candidates(run: list[FieldTarget]) -> bool:
    """Is a window of 3 consecutive targets a genuine date (DOB) group?"""
    if len(run) != 3:
        return False
    if not all(_is_combo(t) for t in run):
        return False
    # A strong date signal (birth/date/dob label, or a declared date-picker/
    # calendar control) on ANY member marks the whole adjacent run as a date
    # group - the MPF DOB is three adjacent combos where only the first may
    # carry the "Date of Birth" label.
    if any(
        _date_token_score(t) >= 2
        or t.element_type in (ElementType.DATE_PICKER, ElementType.CALENDAR)
        for t in run
    ):
        return True
    if all(_date_token_score(t) >= 1 for t in run):
        return True
    if all(not _clean_label(t.label) for t in run):
        # Unnamed triplet: trust the structural fingerprint, or a value that
        # already parsed as a date (older labelling path).
        if _looks_like_date_triplet(run):
            return True
        value = next((t.value for t in run if t.value), "")
        return len(split_date_parts(value)) >= 3
    return False


def _assign_date_values(group: DateGroupTarget, date_value: str | None = None) -> None:
    """Split a single mapped DOB value across the three sub-controls.

    ``date_value`` (the whole-date record pair) is used when the parts were not
    already bound - the unlabelled-MPF path, where the record's Date of Birth
    value is re-split generically into Day/Month/Year.
    """
    valued = [t for t in group.targets if t.value]
    source = ""
    parts: list[str] = []
    if len(valued) == 1:
        source = valued[0].value or ""
        parts = _date_parts(source)
    if len(parts) < 3 and date_value:
        source = date_value
        parts = _date_parts(date_value)
    if len(parts) >= 3:
        for target, part in zip(group.targets, parts[:3]):
            target.value = part
        group.date_value = source


def _merge_date_groups(targets: list[FieldTarget], date_value: str | None = None) -> list[FieldTarget | DateGroupTarget]:
    """Collapse 3 consecutive date combos into one :class:`DateGroupTarget`."""
    merged: list[FieldTarget | DateGroupTarget] = []
    i = 0
    n = len(targets)
    while i < n:
        if i + 2 < n and _group_candidates(targets[i:i + 3]):
            group = DateGroupTarget(targets=list(targets[i:i + 3]))
            _assign_date_values(group, date_value)
            merged.append(group)
            i += 3
            continue
        merged.append(targets[i])
        i += 1
    return merged


class PendingFieldQueue:
    """Ordered queue of fields to fill for one record.

    Every target carries an explicit :class:`FieldStatus`. A target is never
    removed silently: it either reaches a terminal status with a reason, or it
    stays pending so the completeness pass / report can see it.
    """

    def __init__(
        self,
        items: list[Any],
        record: Any = None,
        mappings: list[dict[str, str]] | None = None,
    ) -> None:
        self.items: list[Any] = list(items)
        #: Targets skipped with a reason (NO_SOURCE / UNMAPPED / NOT_APPLICABLE).
        self.skipped_items: list[Any] = []
        #: Source record + mappings used to bind values to late-joining fields.
        self.record = record
        self.mappings = list(mappings or [])

    @property
    def remaining(self) -> int:
        return sum(1 for it in self.items if not it.done and not it.failed)

    @property
    def done(self) -> int:
        return sum(1 for it in self.items if it.done)

    @property
    def failed(self) -> int:
        return sum(1 for it in self.items if it.failed)

    @property
    def pending(self) -> int:
        return sum(1 for it in self.items if not it.done and not it.failed)

    def next_pending(self) -> Any | None:
        for it in self.items:
            if not it.done and not it.failed:
                return it
        return None

    def mark_status(self, target: Any, status: FieldStatus, reason: str = "") -> None:
        """Set a target's explicit status, keeping ``done``/``failed`` in sync."""
        target.status = FieldStatus(status)
        target.status_reason = reason or target.status_reason
        if target.status is FieldStatus.FAILED:
            target.failed = True
            target.done = False
        elif target.status is FieldStatus.RETRY_PENDING:
            # Re-enters the queue: neither done nor failed.
            target.failed = False
            target.done = False
        elif target.status in _TERMINAL:
            target.done = True
            target.failed = False

    def mark_done(self, target: Any) -> None:
        self.mark_status(target, FieldStatus.VERIFIED)

    def mark_failed(self, target: Any, reason: str = "") -> None:
        self.mark_status(target, FieldStatus.FAILED, reason)

    def mark_skipped(self, target: Any, status: FieldStatus, reason: str = "") -> None:
        """Record a deliberate skip with an explicit status + reason."""
        self.mark_status(target, status, reason)
        if target not in self.skipped_items:
            self.skipped_items.append(target)

    def submit_ready(self) -> bool:
        return self.remaining == 0

    def all_ok(self) -> bool:
        return self.remaining == 0 and self.failed == 0

    def blockers(self) -> list[Any]:
        """Source-backed targets that are NOT safely filled (never submit partial).

        Only ``VERIFIED`` and ``ALREADY_CORRECT`` satisfy the gate. A field
        written but verified UNKNOWN (``FILLED``) still blocks the submit.
        """
        return [
            it for it in self.items
            if it.source_backed and not it.failed
            and it.status not in _SUBMIT_OK
        ]

    def status_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for it in self.items:
            key = it.status.value if isinstance(it.status, FieldStatus) else str(it.status or "PENDING")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def validate_order(self) -> tuple[bool, int]:
        """Check the queue's initial reading order is monotonic in (top, left).

        Returns ``(ok, bad_index)``; the field-driven path logs a warning when
        the first snapshot had a field out of reading order (rare, but it is
        the kind of stale-geometry issue that makes fields be "processed out of
        order").
        """
        tops: list[tuple[float, float]] = []
        for it in self.items:
            sub = it.targets if isinstance(it, DateGroupTarget) else [it]
            first = sub[0]
            bbox = first.bbox
            if bbox is None:
                continue
            tops.append((bbox.top, bbox.left))
        for i in range(1, len(tops)):
            if tops[i][0] < tops[i - 1][0] or (tops[i][0] == tops[i - 1][0] and tops[i][1] < tops[i - 1][1]):
                return False, i
        return True, -1

    def refresh_positions(self, fresh_nodes: list[Any]) -> int:
        """Re-match pending targets against a fresh UIA snapshot by stable key.

        Returns the number of targets whose bbox was updated.
        """
        keyed: dict[tuple, Any] = dict(_index_nodes(fresh_nodes))
        updated = 0
        for item in self.items:
            sub = item.targets if isinstance(item, DateGroupTarget) else [item]
            for target in sub:
                node = keyed.get(target._match_key)
                if node is not None:
                    target.node = node
                    updated += 1
        return updated

    def merge_fields(
        self,
        fresh_nodes: list[Any],
        mappings: list[dict[str, str]] | None = None,
        record: Any = None,
    ) -> int:
        """Add right-form fields missing from the queue (complete inventory).

        The queue is built from the FIRST UIA snapshot; controls that only
        appear later (lazy render, dynamic sections) would otherwise be lost.
        Every snapshot refresh calls this so newly discovered controls join the
        queue in reading order - appended after the existing items so the
        original deterministic order is preserved. Returns the number added.
        """
        existing: set[tuple] = set()
        for item in self.items:
            sub = item.targets if isinstance(item, DateGroupTarget) else [item]
            for target in sub:
                existing.add(target._match_key)
        fresh = _index_nodes(fresh_nodes)
        if mappings is None:
            mappings = self.mappings
        if record is None:
            record = self.record
        pairs = dict(getattr(record, "pairs", {}) or {})
        targets: dict[str, str] = {}
        for m in (mappings or []):
            source = m.get("source", "")
            target = m.get("target", "")
            if target and source in pairs:
                targets[target] = pairs[source]
        ordinal = len(self.items)
        added = 0
        for match_key, node in fresh:
            if match_key in existing:
                continue
            key = _clean_label(getattr(node, "name", "") or "") or (getattr(node, "automation_id", "") or "").strip()
            self.items.append(FieldTarget(
                node=node,
                value=targets.get(key),
                ordinal=ordinal,
                _match_key=match_key,
            ))
            existing.add(match_key)
            ordinal += 1
            added += 1
        return added

    def bbox_for_id(self, field_id: str | None) -> BBox | None:
        """Live bbox for an action's ``field_id`` (stable id) from the queue.

        Used to refresh an action's geometry immediately before verification so
        a read never uses a bbox made stale by a scroll or window resize since
        the write. Date-group ids return the triplet's union box.
        """
        if not field_id:
            return None
        for item in self.items:
            if isinstance(item, DateGroupTarget):
                if item.stable_id == field_id:
                    return item.bbox
                for sub in item.targets:
                    if sub.stable_id == field_id:
                        return sub.bbox
            elif item.stable_id == field_id:
                return item.bbox
        return None


def build_field_queue(field_map: Any, record: Any, mappings: list[dict[str, str]] | None = None) -> PendingFieldQueue:
    """Build the ordered fill queue for a record from the UIA field map.

    Values are resolved from the source record through the LEFT->RIGHT
    mappings. Right fields with no mapped source value are kept in the queue
    (they may still be visible) but produce no actions.
    """
    pairs = dict(getattr(record, "pairs", {}) or {})
    target_value: dict[str, str] = {}
    for m in (mappings if mappings is not None else getattr(field_map, "mappings", None) or []):
        source = m.get("source", "")
        target = m.get("target", "")
        if target and source in pairs:
            target_value[target] = pairs[source]

    targets: list[FieldTarget] = []
    for ordinal, (match_key, node) in enumerate(_index_nodes(getattr(field_map, "right_fields", None) or [])):
        key = _clean_label(getattr(node, "name", "") or "") or (getattr(node, "automation_id", "") or "").strip()
        targets.append(FieldTarget(node=node, value=target_value.get(key), ordinal=ordinal, _match_key=match_key))
    # Resolve the record's date-like value so unlabelled Day/Month/Year combos
    # can be detected and filled even without a label or mapping.
    date_value = _find_date_value(pairs)
    merged = _merge_date_groups(targets, date_value)
    queue_mappings = list(mappings) if mappings is not None else list(getattr(field_map, "mappings", None) or [])
    return PendingFieldQueue(merged, record=record, mappings=queue_mappings)


class ScrollCapabilityCache:
    """Remembers the first scroll method that moved each container.

    Priority when nothing is known: UIA ScrollPattern first (a container that
    exposes it), then DOM, then mouse wheel. A successfully used method is
    cached per container so later scrolls skip the escalation ladder.
    """

    _DEFAULT_ORDER = (
        SCROLL_METHOD_PATTERN,
        SCROLL_METHOD_DOM,
        SCROLL_METHOD_WHEEL,
        SCROLL_METHOD_SCROLLBAR_DRAG,
        SCROLL_METHOD_KEYBOARD,
    )

    def __init__(self) -> None:
        self._best: dict[str, str] = {}

    @staticmethod
    def _container_id(container: Any) -> str:
        runtime = getattr(container, "runtime_id", None)
        if runtime:
            return "r:" + ":".join(str(r) for r in runtime)
        handle = getattr(container, "handle", None)
        if handle not in (None, 0):
            return f"h:{int(handle)}"
        return f"n:{getattr(container, 'control_type', '') or ''}:{getattr(container, 'automation_id', '') or ''}:{getattr(container, 'name', '') or ''}"

    def method_for(self, container: Any, dom_available: bool = False) -> str:
        cid = self._container_id(container)
        if cid in self._best:
            return self._best[cid]
        if getattr(container, "has_scroll_pattern", False):
            return SCROLL_METHOD_PATTERN
        if dom_available:
            return SCROLL_METHOD_DOM
        return SCROLL_METHOD_WHEEL

    def remember(self, container: Any, method: str) -> None:
        if not method or method == "none":
            return
        self._best[self._container_id(container)] = method

    def forget(self, container: Any) -> None:
        self._best.pop(self._container_id(container), None)

    def reset(self) -> None:
        self._best.clear()

    def to_dict(self) -> dict[str, str]:
        return dict(self._best)


class TargetNavigator:
    """Computes adaptive scroll distances and visibility checks."""

    def __init__(self, min_px: int = MIN_SCROLL_PX, max_px: int = MAX_SCROLL_PX) -> None:
        self.min_px = max(60, int(min_px))
        self.max_px = max(self.min_px, int(max_px))

    def scroll_amount_for(self, target: Any, viewport_rect: tuple[int, int, int, int] | None) -> int:
        bbox = target.bbox
        if bbox is None or viewport_rect is None:
            return (self.min_px + self.max_px) // 2
        left, top, right, bottom = viewport_rect
        gap = bbox.top - top
        below = bbox.top - bottom + max(0, bbox.height)
        amount = max(gap, below)
        return max(self.min_px, min(self.max_px, int(amount)))

    @staticmethod
    def visible(target: Any, client_rect: tuple[int, int, int, int] | None, margin: int = 0) -> bool:
        bbox = target.bbox
        if bbox is None:
            return False
        if client_rect is None:
            return True
        left, top, right, bottom = client_rect
        return (
            bbox.bottom > top - margin
            and bbox.top < bottom + margin
            and bbox.right > left - margin
            and bbox.left < right + margin
        )

    @staticmethod
    def fillable(
        target: Any,
        viewport_rect: tuple[int, int, int, int] | None,
        clearance: int = 8,
    ) -> bool:
        """True when the field is fully readable inside the visible band.

        ``visible`` only requires *any* overlap with the window; ``fillable``
        additionally demands the field's bottom clear the band's bottom edge by
        ``clearance`` px, so a fixed footer / status bar that sits just below
        the panel (e.g. an MPF "Record 114 of 114" line) can never be cropped
        into a read-back. A field hugging the fold is therefore still treated as
        below-fold until it is scrolled comfortably into view.
        """
        bbox = target.bbox
        if bbox is None:
            return False
        if viewport_rect is None:
            return True
        left, top, right, bottom = viewport_rect
        return (
            bbox.bottom > top
            and bbox.bottom <= bottom - clearance
            and bbox.right > left
            and bbox.left < right
        )


@dataclass
class ScrollProgress:
    """Pre-scroll snapshot used to verify a scroll without a full re-observe."""

    percent_before: float | None = None
    y_before: int | None = None

    @classmethod
    def capture(cls, container: Any, target: Any) -> ScrollProgress:
        return cls(
            percent_before=getattr(container, "vertical_scroll_percent", None),
            y_before=target.bbox.top if target.bbox is not None else None,
        )

    def moved(self, container: Any, target: Any) -> bool:
        percent_after = getattr(container, "vertical_scroll_percent", None)
        if (
            self.percent_before is not None
            and percent_after is not None
            and percent_after > self.percent_before + 0.01
        ):
            return True
        y_after = target.bbox.top if target.bbox is not None else None
        if self.y_before is not None and y_after is not None and y_after < self.y_before:
            return True
        return False


class ProgressGuard:
    """Hard deadline for a single field so one stuck field never stalls a record."""

    def __init__(self, timeout: float = DEFAULT_FIELD_TIMEOUT) -> None:
        self.timeout = float(timeout)
        self._deadline = 0.0

    def begin(self) -> None:
        self._deadline = time.time() + self.timeout

    @property
    def expired(self) -> bool:
        return time.time() > self._deadline


class PerfTracker:
    """Records phase durations so the field-driven path is auditable."""

    def __init__(self) -> None:
        self._events: list[tuple[str, float]] = []
        self._started: dict[str, float] = {}

    def start(self, phase: str) -> None:
        self._started[phase] = time.time()

    def stop(self, phase: str) -> None:
        started = self._started.pop(phase, None)
        if started is not None:
            self._events.append((phase, time.time() - started))

    def record(self, phase: str, seconds: float) -> None:
        self._events.append((phase, float(seconds)))

    def totals(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for phase, seconds in self._events:
            out[phase] = out.get(phase, 0.0) + seconds
        return out

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for phase, _ in self._events:
            out[phase] = out.get(phase, 0) + 1
        return out

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {"totals": self.totals(), "counts": self.counts()}


def build_field_actions(target: Any, include_focus_click: bool = True) -> list[Action]:
    """Actions to fill one target (a date group expands to its sub-controls)."""
    if isinstance(target, DateGroupTarget):
        actions: list[Action] = []
        for sub in target.targets:
            actions.extend(_actions_for(sub, include_focus_click))
        return actions
    return _actions_for(target, include_focus_click)


def _actions_for(target: Any, include_focus_click: bool) -> list[Action]:
    bbox = target.bbox
    if bbox is None or not target.enabled or target.value is None:
        return []
    label = target.label or "field"
    field_id = target.stable_id
    actions: list[Action] = []
    if include_focus_click:
        actions.append(Action(
            type=ActionType.CLICK,
            field_id=field_id,
            bbox=bbox,
            reason=f"focus {label}",
        ))
    etype = target.element_type
    if etype in (ElementType.COMBOBOX, ElementType.DATE_PICKER, ElementType.CALENDAR) or target.control_type == "ComboBox":
        actions.append(Action(
            type=ActionType.SELECT,
            field_id=field_id,
            bbox=bbox,
            value=target.value,
            options=list(target.options),
            expected=target.value,
            reason=f"select {label}",
        ))
    elif etype in (ElementType.CHECKBOX, ElementType.RADIO):
        actions.append(Action(
            type=ActionType.TOGGLE,
            field_id=field_id,
            bbox=bbox,
            value=target.value,
            expected=target.value,
            reason=f"toggle {label}",
        ))
    else:
        # Value-shape repair (Phase 2): normalise to the field's expected
        # format before typing (e.g. pincode/phone spacing, ISO dates).
        typed = repair_value(label, target.value)
        actions.append(Action(
            type=ActionType.TYPE,
            field_id=field_id,
            bbox=bbox,
            value=typed,
            expected=typed,
            reason=f"type {label}",
        ))
    return actions


def make_scroll_fn(session: Any, method: str, handle: int | None = None, backend: Any = None) -> Callable[[Any, int], bool] | None:
    """Build a single-method scroll callable from the cached method.

    ``session`` is a :class:`atlas.workflow.scroller.ScrollSession`; the
    returned callable performs exactly one scroll gesture of ``pixels`` on a
    container and returns True when it was issued. Returns None when the
    method cannot be performed with the available wiring (caller should fall
    back to the full :class:`PanelScroller` escalation).
    """
    scroller = getattr(session, "scroller", None)

    def _pattern(container: Any, pixels: int) -> bool:
        if backend is None or not getattr(container, "has_scroll_pattern", False):
            return False
        try:
            backend.scroll_container_pattern(container, int(pixels), handle)
            return True
        except Exception:
            return False

    def _dom(container: Any, pixels: int) -> bool:
        dom = getattr(scroller, "_dom", None)
        if dom is None:
            return False
        try:
            dom(container, int(pixels))
            return True
        except Exception:
            return False

    def _wheel(container: Any, pixels: int) -> bool:
        if scroller is None:
            return False
        try:
            return bool(scroller._focus_wheel(container, int(pixels)))
        except Exception:
            return False

    def _drag(container: Any, pixels: int) -> bool:
        if scroller is None:
            return False
        try:
            return bool(scroller._drag_scroll(container, int(pixels)))
        except Exception:
            return False

    def _keyboard(container: Any, pixels: int) -> bool:
        if scroller is None:
            return False
        try:
            return bool(scroller._keyboard_scroll(container, int(pixels)))
        except Exception:
            return False

    fn = {
        SCROLL_METHOD_PATTERN: _pattern,
        SCROLL_METHOD_DOM: _dom,
        SCROLL_METHOD_WHEEL: _wheel,
        SCROLL_METHOD_SCROLLBAR_DRAG: _drag,
        SCROLL_METHOD_KEYBOARD: _keyboard,
    }.get(method)
    return fn


__all__ = [
    "FieldTarget",
    "DateGroupTarget",
    "PendingFieldQueue",
    "FieldStatus",
    "classify_fill_status",
    "ScrollCapabilityCache",
    "TargetNavigator",
    "ScrollProgress",
    "ProgressGuard",
    "PerfTracker",
    "build_field_queue",
    "build_field_actions",
    "make_scroll_fn",
    "split_date_parts",
    "_date_parts",
    "_find_date_value",
    "MIN_SCROLL_PX",
    "MAX_SCROLL_PX",
    "DEFAULT_FIELD_TIMEOUT",
    "DEFAULT_SCROLL_ATTEMPTS",
    "DEFAULT_FIELD_RETRIES",
]
