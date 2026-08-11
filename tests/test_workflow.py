"""Tests for the workflow loop against a fake target."""

from __future__ import annotations

from atlas.act.controls import ControlInterface, ControlOutcome
from atlas.act.executor import ActionExecutor
from atlas.act.models import ActionType
from atlas.act.verify import FieldVerifier
from atlas.mapping.mapper import SemanticMapper
from atlas.observe.uia import ScrollContainer
from atlas.reason.planner import ActionPlanner
from atlas.reason.recovery import RecoveryPlanner
from atlas.target.base import TargetAdapter, TargetInfo
from atlas.understanding.source import SourceReader
from atlas.vision.models import BBox, ElementType, SceneDescription, ScreenElement
from atlas.vision.scene import SceneAnalysis
from atlas.workflow.loop import AgentLoop
from atlas.workflow.scroller import PanelScroller, ScrollSession


class RecordingControls(ControlInterface):
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.clicked: list[str] = []

    def focus(self, bbox, field_id=None): return ControlOutcome(ok=True)
    def click_field(self, bbox, field_id=None):
        if field_id:
            self.clicked.append(field_id)
        return ControlOutcome(ok=True)
    def type_value(self, bbox, value, field_id=None):
        self.typed.append(value)
        return ControlOutcome(ok=True)
    def clear(self, bbox, field_id=None): return ControlOutcome(ok=True)
    def select_option(self, bbox, value, options=None, field_id=None): return ControlOutcome(ok=True)
    def toggle(self, bbox, value, field_id=None): return ControlOutcome(ok=True)
    def choose_date(self, bbox, value, date_format=None, field_id=None): return ControlOutcome(ok=True)
    def press_tab(self): return ControlOutcome(ok=True)
    def press_enter(self): return ControlOutcome(ok=True)
    def press_escape(self): return ControlOutcome(ok=True)
    def scroll(self, direction, amount=3): return ControlOutcome(ok=True)
    def paste(self, value, field_id=None): return ControlOutcome(ok=True)
    def upload_file(self, bbox, path, field_id=None): return ControlOutcome(ok=True)


class StubMouse:
    def move_to(self, x, y): pass
    def click(self, x, y): pass
    def double_click(self, x, y): pass
    def right_click(self, x, y): pass
    def hover(self, x, y): pass
    def scroll(self, direction, amount=3): pass


class StubKeyboard:
    driver = None


class PassVerifier(FieldVerifier):
    name = "pass"

    def verify(self, bbox, expected, field_id=None):
        return True, "ok"


class FakeTarget(TargetAdapter):
    name = "fake"

    def __init__(self, scenes: list[SceneDescription]) -> None:
        self._scenes = list(scenes)
        self._idx = 0
        self._info = TargetInfo(name="fake", title="Fake Window")

    def attach(self, hint: str | None = None) -> TargetInfo:
        return self._info

    def detach(self) -> None:
        pass

    def observe(self) -> SceneAnalysis | None:
        if self._idx < len(self._scenes):
            scene = self._scenes[self._idx]
            self._idx += 1
            return SceneAnalysis(scene=scene)
        return None

    def is_alive(self) -> bool:
        return True

    def read_field_value(self, field_id: str) -> str | None:
        return None


def make_scene(record_no: str, name: str, agree: str, pan_required: bool = False) -> SceneDescription:
    elements = [
        ScreenElement(element_id="s0", type=ElementType.LABEL, label="Application No", value=record_no, bbox=BBox(10, 10, 120, 16)),
        ScreenElement(element_id="s1", type=ElementType.LABEL, label="Applicant Name", value=name, bbox=BBox(10, 30, 120, 16)),
        ScreenElement(element_id="s2", type=ElementType.LABEL, label="Agree", value=agree, bbox=BBox(10, 50, 120, 16)),
        ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name", bbox=BBox(200, 40, 120, 20)),
        ScreenElement(element_id="f1", type=ElementType.CHECKBOX, label="Agree", bbox=BBox(200, 80, 20, 20)),
        ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save", bbox=BBox(200, 120, 60, 24)),
    ]
    if pan_required:
        elements.append(ScreenElement(
            element_id="f2", type=ElementType.TEXTBOX, label="PAN Number", required=True, bbox=BBox(200, 100, 120, 20),
        ))
    return SceneDescription(window_title="Fake Window", elements=elements, screen_offset=(0, 0))


def _build_loop(target, controls, max_records=2, timeout=1.0, scan_reveal_fields=False):
    executor = ActionExecutor(
        mouse=StubMouse(),
        keyboard=StubKeyboard(),
        controls=controls,
        verifier=PassVerifier(),
        recovery=RecoveryPlanner(),
        verify_after_action=True,
        max_retries=2,
        retry_delay=0.0,
    )
    return AgentLoop(
        target=target,
        source_reader=SourceReader(),
        mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True),
        executor=executor,
        max_records=max_records,
        next_record_timeout=timeout,
        next_record_poll=0.05,
        scan_reveal_fields=scan_reveal_fields,
    )


def test_loop_processes_multiple_records() -> None:
    target = FakeTarget([
        make_scene("1001", "Ravi Kumar", "Yes"),
        make_scene("1002", "Sita Devi", "No"),
    ])
    controls = RecordingControls()
    loop = _build_loop(target, controls)
    summary = loop.run()
    assert summary.completed == 2
    assert summary.failed == 0
    assert controls.typed == ["Ravi Kumar", "Sita Devi"]
    assert loop.state.value == "stopped"


def test_loop_waits_when_no_record() -> None:
    """No records must NOT terminate the loop: it retries and waits for the
    next record; only an explicit stop ends the run."""
    import threading
    import time

    target = FakeTarget([])
    loop = _build_loop(target, controls=RecordingControls(), timeout=0.3)

    def _stop() -> None:
        time.sleep(0.2)
        loop.stop()

    stopper = threading.Thread(target=_stop, daemon=True)
    stopper.start()
    summary = loop.run()
    stopper.join()
    assert summary.records == []
    assert summary.stopped_reason == "stopped by user"


def test_loop_marks_unmapped_required_field() -> None:
    target = FakeTarget([
        make_scene("1003", "Ravi", "Yes", pan_required=True),
    ])
    loop = _build_loop(target, controls=RecordingControls(), max_records=1, timeout=0.3)
    summary = loop.run()
    assert summary.completed == 1
    record = summary.records[0]
    assert "PAN Number" in record.incomplete_fields
    assert record.success is True


def test_loop_surfaces_unverified_fields() -> None:
    """UNKNOWN-written fields are surfaced on the record (unverified_fields)
    and counted in the summary - never silently counted as a verified pass,
    but never re-filled or failed either."""

    class UnknownVerifier(FieldVerifier):
        name = "unknown"

        def verify(self, bbox, expected, field_id=None):
            return False, "read empty"

    target = FakeTarget([
        make_scene("1004", "Ravi", "Yes"),
    ])
    controls = RecordingControls()
    executor = ActionExecutor(
        mouse=StubMouse(), keyboard=StubKeyboard(), controls=controls,
        verifier=UnknownVerifier(), recovery=RecoveryPlanner(),
        verify_after_action=True, max_retries=2, retry_delay=0.0,
        read_recovery_attempts=1,
    )
    loop = AgentLoop(
        target=target,
        source_reader=SourceReader(),
        mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True),
        executor=executor,
        max_records=1,
        next_record_timeout=0.3,
        next_record_poll=0.05,
        scan_reveal_fields=False,
    )
    summary = loop.run()
    assert summary.completed == 1  # UNKNOWN ≠ FAIL: the record completes
    record = summary.records[0]
    assert record.success is True
    assert set(record.unverified_fields) == {"f0", "f1"}
    assert summary.unverified == 1
    assert summary.unverified_fields == 2
    # The field was written once (accepted, never re-filled) and surfaced.
    assert controls.typed == ["Ravi"]
    payload = record.to_dict()
    assert set(payload["unverified_fields"]) == {"f0", "f1"}
    actions = payload["actions"]
    unknown_actions = [a for a in actions if a["verification_status"] == "UNKNOWN"]
    assert unknown_actions
    assert all(a["verified"] is False for a in unknown_actions)


def test_loop_max_records() -> None:
    target = FakeTarget([
        make_scene("2001", "A", "Yes"),
        make_scene("2002", "B", "No"),
        make_scene("2003", "C", "Yes"),
    ])
    loop = _build_loop(target, controls=RecordingControls(), max_records=2, timeout=0.3)
    summary = loop.run()
    assert len(summary.records) == 2
    assert summary.stopped_reason == "max_records reached (2)"


def test_record_summary_contains_actions() -> None:
    target = FakeTarget([make_scene("3001", "Ravi", "Yes")])
    loop = _build_loop(target, controls=RecordingControls(), max_records=1, timeout=0.3)
    summary = loop.run()
    record = summary.records[0]
    types = [a.action.type for a in record.actions]
    assert ActionType.TYPE in types
    assert ActionType.TOGGLE in types
    assert types[-1] == ActionType.CLICK  # submit


def test_loop_captures_before_and_after_fill_screenshots(tmp_path) -> None:
    target = FakeTarget([make_scene("4001", "Ravi", "Yes")])
    saved: list[str] = []

    def capture(path) -> bool:
        saved.append(str(path))
        return True

    executor = ActionExecutor(
        mouse=StubMouse(), keyboard=StubKeyboard(), controls=RecordingControls(),
        verifier=PassVerifier(), recovery=RecoveryPlanner(),
        verify_after_action=True, max_retries=2, retry_delay=0.0,
    )
    loop = AgentLoop(
        target=target, source_reader=SourceReader(), mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True), executor=executor,
        max_records=1, next_record_timeout=0.3, next_record_poll=0.05,
        debug_dir=tmp_path, capture_callback=capture,
    )
    summary = loop.run()
    assert summary.completed == 1
    assert len(saved) >= 2, saved
    assert any(p.endswith("-before-fill.png") for p in saved)
    assert any(p.endswith("-after-fill.png") for p in saved)
    assert any(p.endswith("-after-upload.png") for p in saved)


class _SequenceTarget(FakeTarget):
    """Fake target whose observe() returns a fresh scene each call."""

    def observe(self) -> SceneAnalysis | None:
        if self._idx < len(self._scenes):
            scene = self._scenes[self._idx]
            self._idx += 1
            return SceneAnalysis(scene=scene)
        return None


def test_reobserve_scene_refreshes_after_ui_change() -> None:
    """Self-healing: reobserve_scene must return the NEW scene (window moved,
    layout changed, fields re-added) rather than the cached one."""
    from atlas.understanding.fields import EditableField
    from atlas.vision.models import ScreenElement

    initial = make_scene("5001", "Ravi", "Yes")
    # The UI changes: the form field moves (as if the window/layout changed).
    moved = SceneDescription(
        window_title="Fake Window",
        elements=[
            ScreenElement(element_id="s0", type=ElementType.LABEL, label="Application No",
                          value="5001", bbox=BBox(10, 10, 120, 16)),
            ScreenElement(element_id="s1", type=ElementType.LABEL, label="Applicant Name",
                          value="Ravi", bbox=BBox(10, 30, 120, 16)),
            ScreenElement(element_id="s2", type=ElementType.LABEL, label="Agree",
                          value="Yes", bbox=BBox(10, 50, 120, 16)),
            ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name",
                          bbox=BBox(200, 40, 120, 20)),
            ScreenElement(element_id="f1", type=ElementType.CHECKBOX, label="Agree",
                          bbox=BBox(200, 80, 20, 20)),
            ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                          bbox=BBox(200, 120, 60, 24)),
        ],
        screen_offset=(500, 300),  # window moved on screen
    )

    target = _SequenceTarget([initial, moved])
    executor = ActionExecutor(
        mouse=StubMouse(), keyboard=StubKeyboard(), controls=RecordingControls(),
        verifier=PassVerifier(), recovery=RecoveryPlanner(),
        verify_after_action=True, max_retries=2, retry_delay=0.0,
    )
    loop = AgentLoop(
        target=target, source_reader=SourceReader(), mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True), executor=executor,
        max_records=0, next_record_timeout=0.3, next_record_poll=0.05,
    )
    # First observation caches the initial scene.
    first = loop.reobserve_scene()
    assert first is not None and first.screen_offset == (0, 0)
    # A later re-observe (after a UI change) must see the moved window.
    second = loop.reobserve_scene()
    assert second is not None and second.screen_offset == (500, 300)
    fields = [e for e in second.elements if e.element_id == "f0"]
    assert fields and fields[0].bbox.x == 200


class _RevealTarget(TargetAdapter):
    """Returns the current screen; after enough observes, a below-fold field
    becomes visible (simulating a controlled scroll revealing more of the form)."""

    def __init__(self) -> None:
        self._info = TargetInfo(name="fake", title="Fake Window")
        self._calls = 0

    def observe(self) -> SceneAnalysis | None:
        elements = [
            ScreenElement(element_id="s0", type=ElementType.LABEL, label="Application No",
                          value="6001", bbox=BBox(10, 10, 120, 16)),
            ScreenElement(element_id="s1", type=ElementType.LABEL, label="Applicant Name",
                          value="Ravi", bbox=BBox(10, 30, 120, 16)),
            ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name",
                          bbox=BBox(200, 40, 120, 20)),
            ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                          bbox=BBox(200, 120, 60, 24)),
        ]
        # A field that is "below the fold" until a scroll reveals it.
        if self._calls >= 1:
            elements.append(ScreenElement(
                element_id="f2", type=ElementType.TEXTBOX, label="PAN Number",
                bbox=BBox(200, 160, 120, 20),
            ))
        self._calls += 1
        return SceneAnalysis(scene=SceneDescription(
            window_title="Fake Window", elements=elements, screen_offset=(0, 0),
        ))

    def attach(self, hint=None): return self._info
    def detach(self): pass
    def is_alive(self): return True
    def read_field_value(self, field_id): return None


def test_scan_reveal_fills_below_the_fold_field() -> None:
    """With scan_reveal_fields enabled and an idempotent live target, a field
    only visible after a scroll is detected and filled."""
    target = _RevealTarget()
    controls = RecordingControls()
    loop = _build_loop(target, controls=controls, max_records=1, timeout=0.3,
                       scan_reveal_fields=True)
    summary = loop.run()
    assert summary.completed == 1
    assert controls.typed and any("Ravi" in t for t in controls.typed)


class _RevealWithValueTarget(_RevealTarget):
    """A below-fold field that also has a value to type after it is revealed."""

    def observe(self) -> SceneAnalysis | None:
        elements = [
            ScreenElement(element_id="s0", type=ElementType.LABEL, label="Application No",
                          value="7001", bbox=BBox(10, 10, 120, 16)),
            ScreenElement(element_id="s1", type=ElementType.LABEL, label="Applicant Name",
                          value="Ravi", bbox=BBox(10, 30, 120, 16)),
            ScreenElement(element_id="s2", type=ElementType.LABEL, label="PAN Number",
                          value="ABCPD1234F", bbox=BBox(10, 50, 120, 16)),
            ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name",
                          bbox=BBox(200, 40, 120, 20)),
            ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                          bbox=BBox(200, 120, 60, 24)),
        ]
        if self._calls >= 1:
            elements.append(ScreenElement(
                element_id="f2", type=ElementType.TEXTBOX, label="PAN Number",
                bbox=BBox(200, 160, 120, 20),
            ))
        self._calls += 1
        return SceneAnalysis(scene=SceneDescription(
            window_title="Fake Window", elements=elements, screen_offset=(0, 0),
        ))


def test_scan_reveal_fills_below_fold_then_submits_once() -> None:
    """The reveal pass fills below-fold fields and defers the submit click so
    it fires exactly once, after every field is filled (never before)."""
    target = _RevealWithValueTarget()
    controls = RecordingControls()
    loop = _build_loop(target, controls=controls, max_records=1, timeout=0.3,
                       scan_reveal_fields=True)
    summary = loop.run()
    assert summary.completed == 1
    # Main plan fills the visible field; the reveal pass fills the below-fold one.
    assert any("Ravi" in t for t in controls.typed)
    assert any("ABCPD1234F" in t for t in controls.typed)
    # Submit is clicked exactly once, and only after all values were typed.
    assert controls.clicked.count("b0") == 1
    assert controls.clicked[-1] == "b0"


def test_can_scroll_blocks_until_viewport_complete() -> None:
    """can_scroll() is the scroll permission gate: it returns False while any
    visible field is unhandled or a verification failed, and True once the
    current viewport is fully handled. Scrolling is forbidden before that."""
    from atlas.act.models import Action, ActionResult

    scene = SceneDescription(elements=[
        ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name",
                      bbox=BBox(200, 40, 120, 20)),
        ScreenElement(element_id="f1", type=ElementType.TEXTBOX, label="PAN Number",
                      bbox=BBox(200, 160, 120, 20)),
        ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                      bbox=BBox(200, 120, 60, 24)),
    ])
    loop = _build_loop(FakeTarget([]), RecordingControls(), max_records=0, timeout=0.2)
    viewport = (1920, 991)

    # An unhandled visible field blocks scrolling.
    assert loop.can_scroll(scene, set(), viewport, []) is False

    # All fields handled, but a failed verification still blocks scrolling.
    handled = {"f0", "f1"}
    failed = [ActionResult(
        action=Action(type=ActionType.TYPE, field_id="f0", value="x"), success=False,
    )]
    assert loop.can_scroll(scene, handled, viewport, failed) is False

    # Viewport complete and every action verified -> scroll is permitted.
    ok = [ActionResult(
        action=Action(type=ActionType.TYPE, field_id="f0", value="x"),
        success=True, verified=True,
    )]
    assert loop.can_scroll(scene, handled, viewport, ok) is True


class _ScrollState:
    """Shared state between the target and the controls of a scroll-driven test."""

    def __init__(self, n_fields: int) -> None:
        self.n_fields = n_fields
        self.visible = 1
        self.scrolls = 0


class _ScrollRevealTarget(TargetAdapter):
    """Reveals the i-th field only after ``i-1`` wheel scrolls.

    Models a real long form: a scroll is REQUIRED to bring the next band of
    fields into view, so the reveal pass must keep scrolling to reach the end.
    """

    def __init__(self, state: _ScrollState) -> None:
        self._state = state
        self._info = TargetInfo(name="fake", title="Fake Window")

    def observe(self) -> SceneAnalysis | None:
        elements = [
            ScreenElement(element_id="s0", type=ElementType.LABEL, label="Application No",
                          value="1001", bbox=BBox(10, 10, 120, 16)),
        ]
        # The source (left) panel is fixed and always shows the full record;
        # only the form fields below the fold are revealed by scrolling.
        for i in range(self._state.n_fields):
            label = f"Field {i}"
            elements.append(ScreenElement(
                element_id=f"s{i}", type=ElementType.LABEL, label=label,
                value=f"V{i}", bbox=BBox(10, 30 + i * 60, 120, 16),
            ))
        for i in range(self._state.visible):
            label = f"Field {i}"
            elements.append(ScreenElement(
                element_id=f"f{i}", type=ElementType.TEXTBOX, label=label,
                bbox=BBox(200, 30 + i * 60, 120, 20),
            ))
        elements.append(ScreenElement(
            element_id="b0", type=ElementType.BUTTON, label="Save",
            bbox=BBox(200, 30 + self._state.n_fields * 60, 60, 24),
        ))
        return SceneAnalysis(scene=SceneDescription(
            window_title="Fake Window", elements=elements, screen_offset=(0, 0),
        ))

    def attach(self, hint: str | None = None) -> TargetInfo:
        return self._info

    def detach(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def read_field_value(self, field_id: str) -> str | None:
        return None


class _ScrollRecordingControls(RecordingControls):
    """Records (field_id, value) pairs and advances the target on scroll."""

    def __init__(self, state: _ScrollState) -> None:
        super().__init__()
        self._state = state
        self.typed: list[tuple[str, str]] = []

    def type_value(self, bbox, value, field_id=None):
        self.typed.append((field_id, value))
        return ControlOutcome(ok=True)

    def scroll(self, direction, amount=3):
        self._state.scrolls += 1
        self._state.visible = min(self._state.n_fields, self._state.visible + 1)
        return ControlOutcome(ok=True)


class _FailRevealVerifier(FieldVerifier):
    """Verifies the first (in-view) field but fails every scrolled-in field."""

    name = "fail-reveal"

    def verify(self, bbox, expected, field_id=None):
        if field_id and field_id.startswith("f") and field_id != "f0":
            return False, "mismatch"
        return True, "ok"


def _build_reveal_loop(target, controls, verifier, max_records=1):
    executor = ActionExecutor(
        mouse=StubMouse(),
        keyboard=StubKeyboard(),
        controls=controls,
        verifier=verifier,
        recovery=RecoveryPlanner(),
        verify_after_action=True,
        max_retries=0,
        retry_delay=0.0,
    )
    return AgentLoop(
        target=target,
        source_reader=SourceReader(),
        mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True),
        executor=executor,
        max_records=max_records,
        next_record_timeout=0.3,
        next_record_poll=0.05,
        scan_reveal_fields=True,
        max_scan_rounds=20,
    )


def test_reveal_keeps_scrolling_when_verification_fails() -> None:
    """A field that fails verification must NOT freeze the reveal pass: the scan
    keeps scrolling until the final below-the-fold field is reached (Issue 1).

    Regression: the reveal pass used to gate scrolling on cumulative
    verification success, so one missed verify froze the scan after a single
    scroll and every lower field was silently skipped.
    """
    state = _ScrollState(n_fields=6)
    controls = _ScrollRecordingControls(state)
    target = _ScrollRevealTarget(state)
    loop = _build_reveal_loop(target, controls, _FailRevealVerifier())
    summary = loop.run()
    typed = [value for _, value in controls.typed]
    assert "V5" in typed  # the final field was still reached
    assert state.scrolls > 1  # scanning continued instead of freezing
    assert summary.failed == 1  # the record still reports the verification miss


def test_reveal_fills_scroll_revealed_fields_in_sequence() -> None:
    """Continuous smart scroll fills every field top-to-bottom in one-by-one
    visual order, only stopping when the end of the form is reached."""
    state = _ScrollState(n_fields=6)
    controls = _ScrollRecordingControls(state)
    target = _ScrollRevealTarget(state)
    loop = _build_reveal_loop(target, controls, PassVerifier())
    summary = loop.run()
    assert summary.completed == 1
    # Strict visual order across the whole form, one field at a time.
    assert [field_id for field_id, _ in controls.typed] == ["f0", "f1", "f2", "f3", "f4", "f5"]
    # Each band of fields required its own scroll - never a single jump.
    assert state.scrolls >= 5


def test_can_reveal_scroll_ignores_verification_failures() -> None:
    """can_reveal_scroll() (the reveal-pass gate) is NOT blocked by a prior
    failed verification, while can_scroll() still is: discovery of the fields
    below the fold must never be stopped by a single unverified value."""
    from atlas.act.models import Action, ActionResult

    scene = SceneDescription(elements=[
        ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name",
                      bbox=BBox(200, 40, 120, 20)),
        ScreenElement(element_id="f1", type=ElementType.TEXTBOX, label="PAN Number",
                      bbox=BBox(200, 160, 120, 20)),
    ])
    loop = _build_loop(FakeTarget([]), RecordingControls(), max_records=0, timeout=0.2)
    viewport = (1920, 991)
    handled = {"f0", "f1"}
    failed = [ActionResult(
        action=Action(type=ActionType.TYPE, field_id="f0", value="x"), success=False,
    )]

    # The strict NO SCROLL RULE is still enforced for a failed verification...
    assert loop.can_scroll(scene, handled, viewport, failed) is False
    # ...but the reveal pass may scroll on: the fields below must be discovered.
    assert loop.can_reveal_scroll(scene, handled, viewport, failed) is True

    # Unhandled visible fields still block the reveal pass.
    assert loop.can_reveal_scroll(scene, {"f0"}, viewport, failed) is False


class _DualPanelRevealState:
    """Shared state for a split (left source + right entry) form target."""

    def __init__(self, n_fields: int) -> None:
        self.n_fields = n_fields
        self.visible = 1
        self.scrolls = 0


class _DualPanelRevealTarget(TargetAdapter):
    """A split form: LEFT source rows and RIGHT entry fields both reveal
    progressively with scrolling, with an Upload Details header and a Save
    submit button at the bottom.

    Models the MPF layout exactly: two independent scrollable panels that move
    in lockstep. The loop must scroll BOTH sides (never just one) to reach the
    end of the document.
    """

    def __init__(self, state: _DualPanelRevealState) -> None:
        self._state = state
        self._info = TargetInfo(name="fake", title="Fake Window")

    def observe(self) -> SceneAnalysis | None:
        elements = [
            ScreenElement(
                element_id="rtitle", type=ElementType.LABEL, label="Form",
                bbox=BBox(220, 8, 80, 16),
            ),
            ScreenElement(
                element_id="sapp", type=ElementType.LABEL, label="Application No",
                value="7001", bbox=BBox(10, 8, 120, 16),
            ),
        ]
        n = self._state.visible
        for i in range(n):
            elements.append(ScreenElement(
                element_id=f"s{i}", type=ElementType.LABEL, label=f"Field {i}",
                value=f"V{i}", bbox=BBox(10, 30 + i * 60, 120, 16),
            ))
        for i in range(n):
            elements.append(ScreenElement(
                element_id=f"f{i}", type=ElementType.TEXTBOX, label=f"Field {i}",
                bbox=BBox(220, 30 + i * 60, 120, 20),
            ))
        if n >= self._state.n_fields:
            elements.append(ScreenElement(
                element_id="sec", type=ElementType.BUTTON, label="Upload Details",
                bbox=BBox(220, 30 + n * 60, 160, 30),
            ))
        elements.append(ScreenElement(
            element_id="b0", type=ElementType.BUTTON, label="Save",
            bbox=BBox(220, 30 + (n + 1) * 60, 60, 24),
        ))
        return SceneAnalysis(scene=SceneDescription(
            window_title="Fake Window", elements=elements, screen_offset=(0, 0),
        ))

    def attach(self, hint: str | None = None) -> TargetInfo:
        return self._info

    def detach(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def read_field_value(self, field_id: str) -> str | None:
        return None


def _build_dual_panel_loop(target, controls, n_fields, max_records=1):
    """Reveal-pass loop with two explicit panel regions (left + right)."""
    executor = ActionExecutor(
        mouse=StubMouse(),
        keyboard=StubKeyboard(),
        controls=controls,
        verifier=PassVerifier(),
        recovery=RecoveryPlanner(),
        verify_after_action=True,
        max_retries=0,
        retry_delay=0.0,
    )
    return AgentLoop(
        target=target,
        source_reader=SourceReader(),
        mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True),
        executor=executor,
        max_records=max_records,
        next_record_timeout=0.3,
        next_record_poll=0.05,
        scan_reveal_fields=True,
        max_scan_rounds=20,
        scroll_regions=lambda scene: [BBox(0, 0, 180, 600), BBox(200, 0, 520, 600)],
    )


def test_dual_panel_scrolls_both_sides_and_fills_all_fields() -> None:
    """A split (left source + right entry) form is scrolled as ONE document:
    BOTH panels advance until the bottom, every field is filled, and the submit
    button is clicked exactly once (the Upload Details header is expanded, never
    picked as the submit)."""
    state = _DualPanelRevealState(n_fields=4)
    controls = _ScrollRecordingControls(state)
    target = _DualPanelRevealTarget(state)
    loop = _build_dual_panel_loop(target, controls, n_fields=4)
    summary = loop.run()
    assert summary.completed == 1
    # Every source value reached its field, in order.
    typed = [field_id for field_id, _ in controls.typed]
    assert typed == ["f0", "f1", "f2", "f3"]
    # The loop kept scrolling both sides until the end, never stopping at a
    # merely-full viewport (two panels per scroll round, several rounds).
    assert state.scrolls >= 4
    # Submit is clicked exactly once, and only after every field was filled.
    assert controls.clicked.count("b0") == 1
    assert controls.clicked[-1] == "b0"


def test_find_submit_never_picks_upload_details_header() -> None:
    """Regression: an expandable 'Upload Details' section header must never be
    selected as the submit button (token 'upload' ranks first). The real submit
    ('Save') must win even when the header is present and clickable."""
    scene = SceneDescription(elements=[
        ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="PAN Number",
                      bbox=BBox(200, 40, 120, 20)),
        ScreenElement(element_id="sec", type=ElementType.BUTTON, label="Upload Details",
                      bbox=BBox(200, 120, 160, 30)),
        ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                      bbox=BBox(200, 180, 60, 24)),
    ])
    loop = _build_loop(FakeTarget([]), RecordingControls(), max_records=0, timeout=0.2)
    submit_id = loop._find_submit(scene)
    assert submit_id == "b0"
    # A plain 'Save' still resolves normally even with no upload header around.
    plain = SceneDescription(elements=[
        ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                      bbox=BBox(200, 180, 60, 24)),
    ])
    assert loop._find_submit(plain) == "b0"


class _StaticBrokenScrollTarget(TargetAdapter):
    """A static form whose scroll containers report more content but whose
    scrolls never move anything (the MPF failure mode)."""

    def __init__(self) -> None:
        self._info = TargetInfo(name="fake", title="Fake Window")

    def observe(self) -> SceneAnalysis | None:
        elements = [
            ScreenElement(element_id="s0", type=ElementType.LABEL, label="Application No",
                          value="8001", bbox=BBox(10, 10, 120, 16)),
            ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Applicant Name",
                          bbox=BBox(200, 40, 120, 20)),
            ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save",
                          bbox=BBox(200, 120, 60, 24)),
        ]
        return SceneAnalysis(scene=SceneDescription(
            window_title="Fake Window", elements=elements, screen_offset=(0, 0),
        ))

    def attach(self, hint: str | None = None) -> TargetInfo:
        return self._info

    def detach(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def read_field_value(self, field_id: str) -> str | None:
        return None


def _broken_scroll_provider() -> ScrollSession:
    left = ScrollContainer(name="Record summary", control_type="Group",
                           rect=BBox(0, 0, 294, 537), has_scroll_pattern=False,
                           vertical_scroll_percent=None)
    right = ScrollContainer(name="", control_type="Group",
                            rect=BBox(500, 0, 565, 537), has_scroll_pattern=False,
                            vertical_scroll_percent=None)
    scroller = PanelScroller(settle=(0.0, 0.0))  # nothing wired -> no method moves
    return ScrollSession(containers=[left, right], scroller=scroller)


def test_broken_scroll_never_submits_and_marks_record_failed() -> None:
    """Regression (MPF): when the panels' scroll containers report more content
    below but no scroll method can move them, the reveal pass must NOT click
    submit and save a half-filled record - it marks the record failed instead."""
    controls = RecordingControls()
    executor = ActionExecutor(
        mouse=StubMouse(), keyboard=StubKeyboard(), controls=controls,
        verifier=PassVerifier(), recovery=RecoveryPlanner(),
        verify_after_action=True, max_retries=0, retry_delay=0.0,
    )
    loop = AgentLoop(
        target=_StaticBrokenScrollTarget(),
        source_reader=SourceReader(),
        mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True),
        executor=executor,
        max_records=1,
        next_record_timeout=0.3,
        next_record_poll=0.05,
        scan_reveal_fields=True,
        max_scan_rounds=3,
        scroll_settle=(0.0, 0.0),
        scroll_container_provider=_broken_scroll_provider,
    )
    summary = loop.run()
    assert summary.failed == 1
    assert summary.completed == 0
    # The submit button was NEVER clicked: the form was never complete.
    assert controls.clicked.count("b0") == 0


def test_dom_scroll_reaches_bottom_and_submits_once() -> None:
    """The positive MPF path: a DOM controller that actually moves the panels
    reveals every field, the container percent reaches 100%, and the submit
    button is clicked exactly once at the end."""
    state = _ScrollState(n_fields=4)
    controls = _ScrollRecordingControls(state)
    target = _ScrollRevealTarget(state)

    def dom(container, pixels) -> bool:
        controls.scroll("down", 3)  # reveal the next band of fields
        container.vertical_scroll_percent = 100.0 if state.visible >= state.n_fields else 50.0
        return True

    def provider() -> ScrollSession:
        container = ScrollContainer(
            name="", control_type="Group", rect=BBox(0, 0, 400, 600),
            has_scroll_pattern=False, vertical_scroll_percent=50.0,
        )
        return ScrollSession(containers=[container], scroller=PanelScroller(dom=dom, settle=(0.0, 0.0)))

    executor = ActionExecutor(
        mouse=StubMouse(), keyboard=StubKeyboard(), controls=controls,
        verifier=PassVerifier(), recovery=RecoveryPlanner(),
        verify_after_action=True, max_retries=0, retry_delay=0.0,
    )
    loop = AgentLoop(
        target=target,
        source_reader=SourceReader(),
        mapper=SemanticMapper(),
        planner=ActionPlanner(verify_after_action=True),
        executor=executor,
        max_records=1,
        next_record_timeout=0.3,
        next_record_poll=0.05,
        scan_reveal_fields=True,
        max_scan_rounds=20,
        scroll_settle=(0.0, 0.0),
        scroll_container_provider=provider,
    )
    summary = loop.run()
    assert summary.completed == 1
    assert summary.failed == 0
    typed = [field_id for field_id, _ in controls.typed]
    assert typed == ["f0", "f1", "f2", "f3"]
    assert state.scrolls >= 3
    # The DOM scroll kept the scan going until the very end, then submit once.
    assert controls.clicked.count("b0") == 1
    assert controls.clicked[-1] == "b0"
