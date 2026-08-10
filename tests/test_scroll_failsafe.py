"""Tests for the raw-wheel scroll failsafe in ``AgentLoop._scroll_one_container``.

Regression coverage for the "it will not scroll and just sits there" failure
mode from the recording: when every structured scroll method (UIA
ScrollPattern, mouse wheel, scrollbar drag, keyboard, plugin override) fails
for a panel across consecutive cycles, the panel must never be left to idle
forever. After ``_RAW_SCROLL_FAILSAFE_THRESHOLD`` consecutive failures, a
raw click-and-wheel fallback is forced directly on the panel's rect,
bypassing the structured PanelScroller entirely.
"""

from __future__ import annotations

from types import SimpleNamespace

from atlas.core.events import get_event_bus
from atlas.core.states import StateMachine
from atlas.observe.uia import ScrollContainer
from atlas.vision.models import BBox
from atlas.workflow.loop import _RAW_SCROLL_FAILSAFE_THRESHOLD, AgentLoop
from atlas.workflow.scroller import ScrollOutcome, ScrollSession


def _bare_loop() -> AgentLoop:
    """A minimally-initialized AgentLoop: enough state for `_set` and the
    scroll bookkeeping to work, without running the real constructor (which
    needs a target adapter, executor, mapper, etc.)."""
    loop = AgentLoop.__new__(AgentLoop)
    loop._states = StateMachine()
    loop._state_entered = {}
    loop._state_warned = set()
    loop._bus = get_event_bus()
    loop._panel_scroll_failures = {}
    loop._scroll_position = 0
    loop._scroll_max_pixels = 350
    return loop


class _AlwaysFailScroller:
    """A PanelScroller stand-in whose scroll_down never moves anything."""

    def __init__(self) -> None:
        self.calls = 0

    def scroll_down(self, container, pixels, verify) -> ScrollOutcome:
        self.calls += 1
        return ScrollOutcome(ok=False, changed=False, method="none", percent_before=0.0)


def _panel_and_container() -> tuple[SimpleNamespace, ScrollContainer]:
    panel = SimpleNamespace(scroll_position=0, more_content=None)
    container = ScrollContainer(
        name="right",
        control_type="Pane",
        automation_id="",
        class_name="",
        framework_id="",
        handle=None,
        rect=BBox(200, 0, 300, 600),
        has_scroll_pattern=False,
        vertical_scroll_percent=0.0,
        vertical_view_size=None,
        runtime_id=(),
        parent=None,
    )
    return panel, container


def test_raw_failsafe_fires_after_threshold_consecutive_failures() -> None:
    loop = _bare_loop()
    scroller = _AlwaysFailScroller()
    session = ScrollSession(containers=[], scroller=scroller)
    panel, container = _panel_and_container()

    forced_calls: list[tuple] = []
    loop._scroll_region = lambda name, region, amount, anchor=None, reason="": forced_calls.append(
        (name, region, amount, anchor, reason)
    )

    for i in range(1, _RAW_SCROLL_FAILSAFE_THRESHOLD + 1):
        loop._scroll_one_container(session, panel, container, 300, lambda: False, "right")
        if i < _RAW_SCROLL_FAILSAFE_THRESHOLD:
            assert forced_calls == [], f"failsafe fired too early (attempt {i})"

    # Exactly at the threshold, the raw fallback must have fired once.
    assert len(forced_calls) == 1
    name, region, amount, anchor, reason = forced_calls[0]
    assert name == "right"
    assert region is container.rect
    assert amount > 0
    assert "stuck" in reason
    # The counter resets after firing, so it doesn't fire again immediately.
    assert loop._panel_scroll_failures["right"] == 0


def test_raw_failsafe_never_fires_when_a_structured_method_succeeds() -> None:
    loop = _bare_loop()
    panel, container = _panel_and_container()

    class _FailsThenSucceedsAlternating:
        """Fails, then succeeds, then fails, then succeeds... - a consecutive
        streak of failures never reaches the threshold because every other
        call resets the counter."""

        def __init__(self) -> None:
            self.calls = 0

        def scroll_down(self, container, pixels, verify) -> ScrollOutcome:
            self.calls += 1
            if self.calls % 2 == 0:
                return ScrollOutcome(ok=True, changed=True, method="wheel", percent_before=0.0)
            return ScrollOutcome(ok=False, changed=False, method="none", percent_before=0.0)

    scroller = _FailsThenSucceedsAlternating()
    session = ScrollSession(containers=[], scroller=scroller)

    forced_calls: list[tuple] = []
    loop._scroll_region = lambda *a, **k: forced_calls.append((a, k))

    for _ in range(6):
        loop._scroll_one_container(session, panel, container, 300, lambda: False, "right")

    # No two consecutive failures ever occur, so the raw failsafe never fires.
    assert forced_calls == []
    assert loop._panel_scroll_failures["right"] == 0


def test_panel_failure_counts_are_tracked_independently() -> None:
    """The left and right panels must not share a failure counter - a stuck
    right panel must not trigger a forced scroll on a healthy left panel."""
    loop = _bare_loop()
    left_panel, left_container = _panel_and_container()
    right_panel, right_container = _panel_and_container()
    always_fail = _AlwaysFailScroller()
    session = ScrollSession(containers=[], scroller=always_fail)

    forced_calls: list[str] = []
    loop._scroll_region = lambda name, *a, **k: forced_calls.append(name)

    # Right panel fails enough times to trip the failsafe...
    for _ in range(_RAW_SCROLL_FAILSAFE_THRESHOLD):
        loop._scroll_one_container(session, right_panel, right_container, 300, lambda: False, "right")
    # ...but the left panel, scrolled only once, must not be affected.
    loop._scroll_one_container(session, left_panel, left_container, 300, lambda: False, "left")

    assert forced_calls == ["right"]
    assert loop._panel_scroll_failures["left"] == 1
