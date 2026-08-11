"""Tests for structured verification states (MATCH/MISMATCH/UNKNOWN).

These lock in the core fix for the MPF regression: an unreadable read-back
must never be treated as a mismatch that re-runs the action. UNKNOWN is a
first-class status; the executor accepts a written-but-unconfirmed field
instead of burning retries on a value it cannot read.
"""

from __future__ import annotations

from atlas.act.controls import ControlInterface, ControlOutcome
from atlas.act.executor import ActionExecutor
from atlas.act.models import Action, ActionType
from atlas.act.verification import (
    VerificationEngine,
    VerificationStatus,
    classify_evidence,
)
from atlas.act.verify import (
    CompositeVerifier,
    FieldVerifier,
    UiaValueVerifier,
    VisionVerifier,
    normalize_ocr_text,
)
from atlas.core.events import EventType, get_event_bus
from atlas.reason.recovery import RecoveryPlanner
from atlas.vision.models import BBox, OcrText


class _OutcomeVerifier(FieldVerifier):
    """Verifier that returns a fixed (ok, evidence) pair."""

    name = "fixed"

    def __init__(self, ok: bool, evidence: str) -> None:
        self._ok = ok
        self._evidence = evidence
        self.calls = 0

    def verify(self, bbox, expected, field_id=None):
        self.calls += 1
        return self._ok, self._evidence


class _FakeControls(ControlInterface):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def _record(self, name, field_id):
        self.calls.append((name, field_id))
        return ControlOutcome(ok=True, evidence=f"fake {name}")

    def focus(self, bbox, field_id=None): return self._record("focus", field_id)
    def click_field(self, bbox, field_id=None): return self._record("click", field_id)
    def type_value(self, bbox, value, field_id=None): return self._record("type", field_id)
    def clear(self, bbox, field_id=None): return self._record("clear", field_id)
    def select_option(self, bbox, value, options=None, field_id=None): return self._record("select", field_id)
    def toggle(self, bbox, value, field_id=None): return self._record("toggle", field_id)
    def choose_date(self, bbox, value, date_format=None, field_id=None): return self._record("date", field_id)
    def press_tab(self): return ControlOutcome(ok=True)
    def press_enter(self): return ControlOutcome(ok=True)
    def press_escape(self): return ControlOutcome(ok=True)
    def scroll(self, direction, amount=3):
        self.calls.append(("scroll", None))
        return ControlOutcome(ok=True)
    def scroll_by_keys(self, direction, amount=3):
        self.calls.append(("scroll_by_keys", None))
        return ControlOutcome(ok=True)
    def scroll_bar(self, direction, amount=3):
        self.calls.append(("scroll_bar", None))
        return ControlOutcome(ok=True)
    def paste(self, value, field_id=None): return self._record("paste", field_id)
    def upload_file(self, bbox, path, field_id=None): return self._record("upload", field_id)


class _StubMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def move_to(self, x, y): pass
    def click(self, x, y): self.clicks.append((x, y))
    def double_click(self, x, y): pass
    def right_click(self, x, y): pass
    def hover(self, x, y): pass
    def scroll(self, direction, amount=3): pass


class _StubKeyboard:
    driver = None


# -- classify_evidence ---------------------------------------------------------


def test_classify_unknown_evidence_phrases() -> None:
    for evidence in (
        "uia read empty",
        "vision read empty",
        "uia read failed: boom",
        "no bbox for uia verification",
        "no value available",
        "clipboard read failed: clipboard blocked",
        "clipboard read-back is whole-window (not field-scoped): 812 chars",
        "no verifier configured",
    ):
        assert classify_evidence(evidence) is VerificationStatus.UNKNOWN, evidence


def test_classify_mismatch_evidence_phrases() -> None:
    for evidence in (
        "uia mismatch: got 'Ravi K', expected 'Ravi'",
        "vision mismatch: got 'Select -', expected 'Male'",
        "clipboard mismatch: got 'x', expected 'y'",
        "target read placeholder ('Select -')",
        "deliberate failure",
    ):
        assert classify_evidence(evidence) is VerificationStatus.MISMATCH, evidence


def test_classify_empty_evidence_is_unknown() -> None:
    assert classify_evidence("") is VerificationStatus.UNKNOWN


def test_classify_match_not_called_for_ok() -> None:
    """classify only sees failures; a MATCH is decided before it runs."""
    assert classify_evidence("uia matched ('Ravi')") is VerificationStatus.MISMATCH


# -- VerificationEngine --------------------------------------------------------


def test_engine_returns_match_result() -> None:
    engine = VerificationEngine(CompositeVerifier([_OutcomeVerifier(True, "uia matched ('Ravi')")]))
    result = engine.verify(BBox(0, 0, 10, 10), "Ravi", "f0")
    assert result.status is VerificationStatus.MATCH
    assert result.is_match
    assert result.observed is not None


def test_engine_classifies_unknown() -> None:
    engine = VerificationEngine(CompositeVerifier([_OutcomeVerifier(False, "uia read empty")]))
    result = engine.verify(BBox(0, 0, 10, 10), "Ravi", "f0")
    assert result.status is VerificationStatus.UNKNOWN
    assert result.is_unknown
    assert not result.is_mismatch


def test_engine_no_expected_is_not_applicable() -> None:
    engine = VerificationEngine(CompositeVerifier([_OutcomeVerifier(True, "ok")]))
    result = engine.verify(BBox(0, 0, 10, 10), None, "f0")
    assert result.status is VerificationStatus.NOT_APPLICABLE


def test_read_recovery_rereads_until_match() -> None:
    v = _OutcomeVerifier(False, "vision read empty")
    engine = VerificationEngine(CompositeVerifier([v]))
    result = engine.verify_with_read_recovery(
        BBox(0, 0, 10, 10), "Ravi", "f0", max_attempts=2, refocus=None,
    )
    # Two re-reads happened but the verifier never succeeds -> stays UNKNOWN.
    assert v.calls == 3  # initial + 2 ladder attempts
    assert result.status is VerificationStatus.UNKNOWN


def test_read_recovery_rescues_a_transient_blank() -> None:
    class FlakyVerifier(FieldVerifier):
        name = "flaky"
        calls = 0

        def verify(self, bbox, expected, field_id=None):
            type(self).calls += 1
            if type(self).calls >= 2:
                return True, "vision matched ('Ravi')"
            return False, "vision read empty"

    engine = VerificationEngine(CompositeVerifier([FlakyVerifier()]))
    result = engine.verify_with_read_recovery(
        BBox(0, 0, 10, 10), "Ravi", "f0", max_attempts=2, refocus=None,
    )
    assert result.status is VerificationStatus.MATCH


# -- normalize_ocr_text --------------------------------------------------------


def test_normalize_ocr_text_strips_trailing_caret_marker() -> None:
    assert normalize_ocr_text("Telugu V") == "Telugu"
    assert normalize_ocr_text("1996 V") == "1996"
    assert normalize_ocr_text("Kataka / Cancer V") == "Kataka / Cancer"


def test_normalize_ocr_text_keeps_real_words() -> None:
    assert normalize_ocr_text("Value") == "Value"
    assert normalize_ocr_text("Ravi") == "Ravi"
    assert normalize_ocr_text("") == ""


# -- executor: UNKNOWN is accepted, never retried ------------------------------


def _executor_with(verifier, max_retries=3) -> ActionExecutor:
    return ActionExecutor(
        mouse=_StubMouse(), keyboard=_StubKeyboard(),
        controls=_FakeControls(), verifier=verifier,
        recovery=RecoveryPlanner(max_retries=max_retries),
        verify_after_action=True, max_retries=max_retries, retry_delay=0.0,
    )


def test_unknown_verification_accepts_field_without_retry() -> None:
    get_event_bus().clear()
    controls = _FakeControls()
    v = _OutcomeVerifier(False, "uia read empty")
    executor = ActionExecutor(
        mouse=_StubMouse(), keyboard=_StubKeyboard(), controls=controls,
        verifier=v, recovery=RecoveryPlanner(max_retries=3),
        verify_after_action=True, max_retries=3, retry_delay=0.0,
        read_recovery_attempts=1,
    )
    action = Action(type=ActionType.TYPE, field_id="f0", value="Ravi", bbox=BBox(0, 0, 10, 10))
    result = executor.execute(action)
    # The field is accepted as written (ok / never re-filled) but UNKNOWN is
    # NEVER a verified pass per spec: verified stays False and the UNKNOWN
    # state is surfaced/tracked separately.
    assert result.ok is True
    assert result.verified is False
    assert result.verification_status == "UNKNOWN"
    assert result.verification_state == "ACTION_SUCCESS_VERIFICATION_UNKNOWN"
    # The action ran exactly once (no retry, no recovery decision).
    assert sum(1 for c in controls.calls if c[0] == "type") == 1
    assert len(get_event_bus().history(EventType.RECOVERY)) == 0
    # Event recorded with the honest status.
    events = executor.verification_events()
    assert events and events[0]["status"] == "UNKNOWN"
    assert events[0]["ok"] is False


def test_mismatch_verification_still_recovers() -> None:
    get_event_bus().clear()
    controls = _FakeControls()
    v = _OutcomeVerifier(False, "uia mismatch: got 'X', expected 'Ravi'")
    executor = ActionExecutor(
        mouse=_StubMouse(), keyboard=_StubKeyboard(), controls=controls,
        verifier=v, recovery=RecoveryPlanner(max_retries=1, max_refocus=1, max_analyze=0),
        verify_after_action=True, max_retries=3, retry_delay=0.0,
    )
    action = Action(type=ActionType.TYPE, field_id="f0", value="Ravi", bbox=BBox(0, 0, 10, 10))
    result = executor.execute(action)
    # A genuine mismatch retries the action before giving up.
    assert result.ok is False
    assert sum(1 for c in controls.calls if c[0] == "type") > 1
    assert len(get_event_bus().history(EventType.RECOVERY)) >= 1


def test_not_applicable_verification_accepted() -> None:
    executor = _executor_with(_OutcomeVerifier(False, "nothing to verify"))
    action = Action(type=ActionType.CLICK, field_id="f0", bbox=BBox(0, 0, 10, 10))
    result = executor.execute(action)
    assert result.ok is True


# -- UIA + Vision integration --------------------------------------------------


def test_vision_verifier_matches_after_ocr_normalization() -> None:
    def read_region(bbox):
        return [OcrText(text="Kataka / Cancer V", bbox=bbox, confidence=0.9)]

    verifier = VisionVerifier(read_region)
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Kataka / Cancer", "rashi")
    assert ok is True


def test_uia_verifier_unknown_on_empty() -> None:
    def read_text(bbox):
        return None

    verifier = UiaValueVerifier(read_text)
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Ravi", "f0")
    assert ok is False
    assert classify_evidence(evidence) is VerificationStatus.UNKNOWN


# -- verification hierarchy: UIA -> Vision -> TargetField -> Clipboard ----------


class _CountingVerifier(_OutcomeVerifier):
    name = "counting"

    def __init__(self, ok: bool, evidence: str) -> None:
        super().__init__(ok, evidence)
        self.calls = 0

    def verify(self, bbox, expected, field_id=None):
        self.calls += 1
        return self._ok, self._evidence


def test_composite_verifier_runs_in_hierarchy_order() -> None:
    """UIA (cheapest, occluded-safe) runs first; a match short-circuits the
    chain so the expensive clipboard strategy is never reached."""
    uia = _CountingVerifier(True, "uia matched ('Ravi')")
    vision = _CountingVerifier(False, "vision read empty")
    clipboard = _CountingVerifier(False, "clipboard read empty")

    engine = VerificationEngine(CompositeVerifier([uia, vision, clipboard]))
    result = engine.verify(BBox(0, 0, 10, 10), "Ravi", "f0")

    assert result.status is VerificationStatus.MATCH
    assert uia.calls == 1
    assert vision.calls == 0
    assert clipboard.calls == 0


def test_composite_verifier_escalates_when_uia_cannot_read() -> None:
    """When UIA reads nothing the chain falls through to the next strategy;
    the clipboard is only consulted as the final fallback."""
    uia = _CountingVerifier(False, "uia read empty")
    vision = _CountingVerifier(True, "vision matched ('Ravi')")
    clipboard = _CountingVerifier(False, "clipboard read empty")

    engine = VerificationEngine(CompositeVerifier([uia, vision, clipboard]))
    result = engine.verify(BBox(0, 0, 10, 10), "Ravi", "f0")

    assert result.status is VerificationStatus.MATCH
    assert uia.calls == 1
    assert vision.calls == 1
    assert clipboard.calls == 0


def test_composite_verifier_clipboard_is_last_resort() -> None:
    """Clipboard is consulted only after both UIA and vision failed."""
    uia = _CountingVerifier(False, "uia read empty")
    vision = _CountingVerifier(False, "vision read empty")
    clipboard = _CountingVerifier(True, "clipboard matched ('Ravi')")

    engine = VerificationEngine(CompositeVerifier([uia, vision, clipboard]))
    result = engine.verify(BBox(0, 0, 10, 10), "Ravi", "f0")

    assert result.status is VerificationStatus.MATCH
    assert uia.calls == 1
    assert vision.calls == 1
    assert clipboard.calls == 1
