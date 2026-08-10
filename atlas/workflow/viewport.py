"""ViewportModel - the agent's human-like understanding of the current view.

A human never scrolls before reading the whole visible form. This module is the
single authority for that decision. It builds a complete model of the current
viewport from the discovered editable fields and answers one question:

    "Is the current viewport fully handled, so is scrolling now safe?"

The NO SCROLL RULE is:

    visible_unfilled_fields == 0
    AND visible_dropdowns_done == true
    AND visible_dates_done == true
    AND upload_checked == true
    AND verification_passed == true

If any gate is false, scrolling must never happen. The model also fixes the
filling order so the agent acts like a human data-entry operator: fields are
completed strictly in visual order (top-to-bottom, left-to-right), one by one,
regardless of control type - never batched by type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atlas.act.models import ActionResult
from atlas.reason.sections import find_upload_sections
from atlas.understanding.fields import (
    DATE_TYPES,
    DROPDOWN_TYPES,
    TEXT_TYPES,
    UPLOAD_TYPES,
    EditableField,
    discover_fields,
)
from atlas.vision.models import ElementType, SceneDescription

#: Fields are filled in strict visual order (see ``visible_controls``); the
#: type sets above only group fields for the pending/readiness gates.


def visible_fields(fields: list[EditableField], viewport: tuple[int, int] | None) -> list[EditableField]:
    """Fields whose band intersects the current viewport (no-bbox always kept).

    A field is visible when part of it overlaps the viewport band: fields
    scrolled above the top edge (``bbox.bottom <= 0``) or fully below the
    fold (``bbox.top >= height``) are NOT visible yet.
    """
    if viewport is None:
        return fields
    _, height = viewport
    return [
        f
        for f in fields
        if f.bbox is None
        or (f.bbox.top < height and f.bbox.bottom > 0)
    ]


@dataclass
class ViewportModel:
    """A structured snapshot of the current viewport and its readiness."""

    scene: SceneDescription
    viewport: tuple[int, int] | None
    handled_ids: set[str] = field(default_factory=set)
    expanded_upload_ids: set[str] = field(default_factory=set)
    results: list[ActionResult] = field(default_factory=list)
    scroll_position: int = 0
    _all_fields: list[EditableField] = field(default_factory=list)
    _last_block: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self._all_fields:
            self._all_fields = discover_fields(self.scene)
        self.fields = visible_fields(self._all_fields, self.viewport)

    # -- field categories (visible only) --------------------------------------
    @property
    def visible_controls(self) -> list[EditableField]:
        """Visible fields in strict visual order: top-to-bottom, left-to-right.

        A human completes the visible controls one by one in reading order,
        whatever each control's type is (textbox, dropdown, date, checkbox,
        radio, upload). Never grouped by type - that was the old, unnatural
        all-text-first behaviour.
        """
        return sorted(
            self.fields,
            key=lambda f: (
                f.bbox.top if f.bbox else 10**9,
                f.bbox.left if f.bbox else 10**9,
            ),
        )

    def _pending(self, types: set[ElementType]) -> list[EditableField]:
        return [
            f
            for f in self.fields
            if f.type in types and f.element_id not in self.handled_ids and f.bbox is not None
        ]

    @property
    def pending_textboxes(self) -> list[EditableField]:
        return self._pending(TEXT_TYPES)

    @property
    def pending_dropdowns(self) -> list[EditableField]:
        return self._pending(DROPDOWN_TYPES)

    @property
    def pending_dates(self) -> list[EditableField]:
        return self._pending(DATE_TYPES)

    @property
    def pending_checkboxes(self) -> list[EditableField]:
        return self._pending({ElementType.CHECKBOX})

    @property
    def pending_radios(self) -> list[EditableField]:
        return self._pending({ElementType.RADIO})

    @property
    def pending_uploads(self) -> list[EditableField]:
        return self._pending(UPLOAD_TYPES)

    @property
    def unfilled_visible(self) -> list[EditableField]:
        """Every visible, actionable field not yet handled."""
        return [
            f
            for f in self.fields
            if f.element_id not in self.handled_ids and f.bbox is not None
        ]

    # -- NO SCROLL RULE gates ------------------------------------------------
    @property
    def has_unfilled_visible(self) -> bool:
        return len(self.unfilled_visible) > 0

    @property
    def dropdowns_done(self) -> bool:
        return not self.pending_dropdowns

    @property
    def dates_done(self) -> bool:
        return not self.pending_dates

    @property
    def verification_passed(self) -> bool:
        return not (self.results and not all(r.ok for r in self.results))

    @property
    def pending_upload_sections(self) -> int:
        """Collapsible upload/attachment headers still to open in this viewport."""
        if not self.viewport:
            return 0
        count = 0
        for element in find_upload_sections(
            self.scene, exclude_ids=self.expanded_upload_ids
        ):
            if element.element_id in self.expanded_upload_ids:
                continue
            if element.section == "actions":
                continue
            bbox = element.bbox
            if bbox is None:
                continue
            if bbox.top >= self.viewport[1]:
                continue
            has_fields_below = any(
                f.bbox is not None and f.bbox.top >= bbox.bottom for f in self.fields
            )
            if has_fields_below:
                count += 1
        return count

    @property
    def upload_checked(self) -> bool:
        return self.pending_upload_sections == 0 and not self.pending_uploads

    @property
    def viewport_complete(self) -> bool:
        """Every visible control is handled (fields, dropdowns, dates, uploads).

        Unlike :attr:`can_scroll` this deliberately IGNORES the verification
        gate: a value that failed to verify on one field must never block the
        reveal pass from scrolling to DISCOVER more fields further down the
        form. The agent's job is to keep scanning until the bottom is reached
        (Issue 1), so a verification miss records the field but does not stop
        the search for the remaining fields.
        """
        if self.has_unfilled_visible:
            self._last_block = "visible unfilled fields remain"
            return False
        if not self.dropdowns_done:
            self._last_block = "visible dropdowns remain"
            return False
        if not self.dates_done:
            self._last_block = "visible date pickers remain"
            return False
        if not self.upload_checked:
            self._last_block = "uploads/attachments not checked"
            return False
        self._last_block = None
        return True

    @property
    def can_scroll(self) -> bool:
        """The full NO SCROLL RULE. False blocks all scrolling."""
        if not self.viewport_complete:
            return False
        if not self.verification_passed:
            self._last_block = "a prior value did not verify"
            return False
        self._last_block = None
        return True

    def scroll_blocked_reason(self) -> str | None:
        """Human-readable reason scanning is not permitted to scroll yet."""
        bool(self.can_scroll)  # evaluate to populate _last_block for the debug dump
        return self._last_block

    def reveal_blocked_reason(self) -> str | None:
        """Human-readable reason the reveal pass may not scroll yet.

        Reports the first NO SCROLL RULE gate that is still open, ignoring the
        verification gate entirely (see :attr:`viewport_complete`).
        """
        bool(self.viewport_complete)  # evaluate to populate _last_block
        return self._last_block

    # -- misc ----------------------------------------------------------------
    @property
    def required_unfilled(self) -> list[EditableField]:
        return [
            f
            for f in self.fields
            if f.element_id not in self.handled_ids and (f.element.required or False)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "viewport": list(self.viewport) if self.viewport else None,
            "scroll_position": self.scroll_position,
            "visible_controls": [f.to_dict() for f in self.visible_controls],
            "unfilled_visible": [f.to_dict() for f in self.unfilled_visible],
            "pending_textboxes": [f.element_id for f in self.pending_textboxes],
            "pending_dropdowns": [f.element_id for f in self.pending_dropdowns],
            "pending_dates": [f.element_id for f in self.pending_dates],
            "pending_checkboxes": [f.element_id for f in self.pending_checkboxes],
            "pending_radios": [f.element_id for f in self.pending_radios],
            "pending_uploads": [f.element_id for f in self.pending_uploads],
            "required_unfilled": [f.element_id for f in self.required_unfilled],
            "pending_upload_sections": self.pending_upload_sections,
            "dropdowns_done": self.dropdowns_done,
            "dates_done": self.dates_done,
            "upload_checked": self.upload_checked,
            "verification_passed": self.verification_passed,
            "has_unfilled_visible": self.has_unfilled_visible,
            "viewport_complete": self.viewport_complete,
            "can_scroll": self.can_scroll,
            "scroll_blocked_reason": self.scroll_blocked_reason(),
        }


__all__ = [
    "ViewportModel",
    "visible_fields",
]
