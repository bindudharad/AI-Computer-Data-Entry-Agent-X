"""Action model shared by the planner, executor and verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from atlas.vision.models import BBox


class ActionType(str, Enum):
    """All actions the agent can execute."""

    MOVE_MOUSE = "move_mouse"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    HOVER = "hover"
    SCROLL = "scroll"
    TYPE = "type"
    CLEAR = "clear"
    SELECT = "select"
    CHOOSE_DATE = "choose_date"
    OPEN_DROPDOWN = "open_dropdown"
    TOGGLE = "toggle"  # checkbox / radio
    TAB = "tab"
    PRESS_ENTER = "press_enter"
    PRESS_ESCAPE = "press_escape"
    PASTE = "paste"
    UPLOAD_FILE = "upload_file"
    CTRL_A = "ctrl_a"
    WAIT = "wait"
    VERIFY = "verify"
    SUBMIT = "submit"
    CAPTURE = "capture"
    ANALYZE = "analyze"
    STOP = "stop"


@dataclass
class Action:
    """A single planned action.

    ``bbox`` is in absolute screen coordinates (client area + screen offset)
    so the executor can act on it directly.
    """

    type: ActionType
    reason: str = ""
    field_id: str | None = None
    value: str | None = None
    bbox: BBox | None = None
    confidence: float = 1.0
    options: list[str] = field(default_factory=list)
    wait_seconds: float = 0.5
    scroll_amount: int = 3
    expected: str | None = None  # expected observable value after the action
    max_retries: int | None = None  # per-action retry budget (overrides default)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "reason": self.reason,
            "field_id": self.field_id,
            "value": self.value,
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "confidence": self.confidence,
            "options": list(self.options),
            "wait_seconds": self.wait_seconds,
            "scroll_amount": self.scroll_amount,
            "expected": self.expected,
            "max_retries": self.max_retries,
        }


@dataclass
class ActionResult:
    """Outcome of executing one action.

    ``success`` reflects whether the action itself executed; ``verified`` and
    ``verification_status`` reflect the read-back. An action may be successful
    yet ``UNKNOWN`` (no usable read) - that is NOT a failure and NOT a
    mismatch. Per the spec an UNKNOWN write is NEVER a verified pass: the
    field is accepted as written (so it is never re-filled) but
    ``verified`` stays ``False`` and the UNKNOWN status is surfaced and
    tracked (``RecordResult.unverified_fields``), not silently counted as a
    confirmed success.
    """

    action: Action
    success: bool
    verified: bool = False
    message: str = ""
    retries: int = 0
    duration_ms: float = 0.0
    verification_evidence: str = ""
    verification_status: str | None = None

    @property
    def ok(self) -> bool:
        # UNKNOWN is "ok" for the field engine (done, never re-filled, never
        # fails) WITHOUT being a verified pass: ``verified`` stays False and the
        # UNKNOWN state is surfaced separately.
        if not self.success:
            return False
        if not self._requires_verification:
            return True
        return self.verified or self.verification_status == "UNKNOWN"

    @property
    def _requires_verification(self) -> bool:
        return self.action.type in {
            ActionType.TYPE,
            ActionType.SELECT,
            ActionType.TOGGLE,
            ActionType.CHOOSE_DATE,
            ActionType.CLEAR,
        }

    @property
    def verification_state(self) -> str:
        """Combined action+verification state (the ``FieldResult`` label).

        Used by the field engine / audit to distinguish an honest success from
        ``ACTION_SUCCESS_VERIFICATION_UNKNOWN`` (field written, value not
        confirmed - never counted as a verified pass) and a genuine mismatch.
        """
        if not self.success:
            return "ACTION_FAILED"
        if self.action.type not in VERIFYABLE_ACTIONS:
            return "ACTION_SUCCESS"
        if self.verification_status == "UNKNOWN":
            return "ACTION_SUCCESS_VERIFICATION_UNKNOWN"
        if not self.verified:
            return "ACTION_SUCCESS_VERIFICATION_MISMATCH"
        status = self.verification_status or "MATCH"
        return f"ACTION_SUCCESS_VERIFICATION_{status}"

    def to_dict(self) -> dict:
        return {
            "action": self.action.to_dict(),
            "success": self.success,
            "verified": self.verified,
            "message": self.message,
            "retries": self.retries,
            "duration_ms": self.duration_ms,
            "verification_evidence": self.verification_evidence,
            "verification_status": self.verification_status,
            "verification_state": self.verification_state,
        }


#: Actions that require explicit verification after execution.
VERIFYABLE_ACTIONS = {
    ActionType.TYPE,
    ActionType.SELECT,
    ActionType.TOGGLE,
    ActionType.CHOOSE_DATE,
    ActionType.CLEAR,
    ActionType.PASTE,
    ActionType.UPLOAD_FILE,
}


__all__ = ["Action", "ActionType", "ActionResult", "VERIFYABLE_ACTIONS"]
