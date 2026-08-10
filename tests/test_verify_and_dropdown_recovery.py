"""Tests for two fixes traced from a real 14-minute run log:

1. ``VisionVerifier`` recapturing once before trusting a blank OCR read.
   Evidence: many field verifications read "dt_boxes num: 0" (a genuinely
   blank crop) immediately after an action that visibly succeeded, forcing a
   full, expensive action retry (screenshot + OCR + click + type again) for
   a field that likely just got caught mid-repaint.

2. ``ActionExecutor`` pressing Escape before retrying a failed SELECT
   (dropdown) action. Evidence: once a dropdown selection stopped verifying
   ("MP\\r\\nMPF (Download and Upload Form...)" - a whole-window clipboard
   grab), every subsequent field in that batch failed identically, exactly
   the signature of a popup that failed to close and is now floating over
   the rest of the form.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from atlas.act.executor import ActionExecutor
from atlas.act.models import Action, ActionType
from atlas.act.verify import VisionVerifier
from atlas.vision.models import BBox, OcrText


class _FakeSandbox:
    def __init__(self) -> None:
        self.is_paused = False

    def validate_target(self):
        return SimpleNamespace(client_rect=(0, 0, 2000, 2000))

    def validate_keyboard(self):
        return True, ""

    def validate_click(self, x, y):
        return True, ""


class _FakeControls:
    def click_field(self, bbox, field_id=None):
        return SimpleNamespace(ok=True, evidence="clicked")

    def select_option(self, bbox, value, options=None, field_id=None):
        return SimpleNamespace(ok=True, evidence="selected")

    def scroll(self, direction: str, amount: int = 3):
        return SimpleNamespace(ok=True, evidence="scrolled")


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    def press(self, key: str, presses: int = 1) -> None:
        self.presses.append(key)


class _AlwaysFailingVerifier:
    name = "fake"

    def verify(self, bbox, expected, field_id=None):
        return False, "always fails"


def _select_action() -> Action:
    return Action(
        type=ActionType.SELECT,
        reason="select value in dropdown",
        field_id="uia-subCaste",
        value="Telugu",
        expected="Telugu",
        bbox=BBox(100, 100, 50, 20),
    )


def test_vision_verifier_recaptures_once_on_blank_read() -> None:
    """A blank first read gets one cheap recapture before giving up - if the
    SECOND read has content, verification proceeds normally instead of
    forcing a full, expensive action retry for a field that was just caught
    mid-repaint."""
    calls = {"n": 0}

    def read_region(bbox):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # blank - simulates the mid-repaint capture
        return [OcrText(text="Telugu", bbox=bbox)]

    verifier = VisionVerifier(read_region)
    verifier._EMPTY_RECAPTURE_DELAY = 0.0  # keep the test fast
    ok, evidence = verifier.verify(BBox(0, 0, 50, 20), "Telugu")

    assert ok is True
    assert calls["n"] == 2
    assert "matched" in evidence


def test_vision_verifier_reports_genuinely_empty_field_correctly() -> None:
    """A field that is ACTUALLY empty must still report 'vision read empty'
    after the recapture - the fix must not mask a real empty field."""
    def read_region(bbox):
        return []  # blank on every call - genuinely empty

    verifier = VisionVerifier(read_region)
    verifier._EMPTY_RECAPTURE_DELAY = 0.0
    ok, evidence = verifier.verify(BBox(0, 0, 50, 20), "Telugu")

    assert ok is False
    assert evidence == "vision read empty"


def test_select_retry_presses_escape_to_dismiss_stray_popup() -> None:
    """The exact regression: after a failed SELECT verification, Escape must
    be sent before the retry to dismiss any dropdown left open by a
    cascading list that was still repopulating when Enter fired."""
    keyboard = _FakeKeyboard()
    executor = ActionExecutor(
        mouse=SimpleNamespace(click=lambda *a, **k: None, center=lambda: (0, 0)),
        keyboard=keyboard,
        controls=_FakeControls(),
        verifier=_AlwaysFailingVerifier(),
        recovery=SimpleNamespace(),
        sandbox=_FakeSandbox(),
        max_retries=1,
        retry_delay=0.0,
    )
    action = _select_action()

    from atlas.reason.recovery import RecoveryDecision

    executor._recovery = SimpleNamespace(
        decide=lambda *a, **k: RecoveryDecision(
            action=ActionType.WAIT, reason="retry action on field", field_id="uia-subCaste", retry=True
        ),
        on_success=lambda *a, **k: None,
    )

    executor.execute(action)

    assert "escape" in keyboard.presses


def test_non_select_retry_never_presses_escape() -> None:
    """Escape is scoped to dropdown recovery only - a plain text field retry
    must not send stray Escape presses that could dismiss unrelated UI."""
    keyboard = _FakeKeyboard()
    executor = ActionExecutor(
        mouse=SimpleNamespace(click=lambda *a, **k: None),
        keyboard=keyboard,
        controls=_FakeControls(),
        verifier=_AlwaysFailingVerifier(),
        recovery=SimpleNamespace(),
        sandbox=_FakeSandbox(),
        max_retries=1,
        retry_delay=0.0,
    )
    action = Action(
        type=ActionType.TYPE,
        reason="fill textbox",
        field_id="uia-fatherName",
        value="X",
        expected="X",
        bbox=BBox(100, 100, 50, 20),
    )

    from atlas.reason.recovery import RecoveryDecision

    executor._recovery = SimpleNamespace(
        decide=lambda *a, **k: RecoveryDecision(
            action=ActionType.WAIT, reason="retry action on field", field_id="uia-fatherName", retry=True
        ),
        on_success=lambda *a, **k: None,
    )

    executor.execute(action)

    assert keyboard.presses == []
