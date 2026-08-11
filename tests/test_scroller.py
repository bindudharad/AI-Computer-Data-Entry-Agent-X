"""Tests for UIA scroll-container selection and the PanelScroller retry chain."""

from __future__ import annotations

from atlas.observe.uia import ScrollContainer, UiaNode
from atlas.vision.models import BBox
from atlas.workflow.scroll import PANEL_LEFT, PANEL_RIGHT
from atlas.workflow.scroller import (
    PanelScroller,
    ScrollOutcome,
    pick_left_right_containers,
)


def _container(name: str, left: int, top: int, w: int, h: int, percent: float | None = 0.0) -> ScrollContainer:
    return ScrollContainer(
        name=name,
        control_type="Pane",
        rect=BBox(left, top, w, h),
        has_scroll_pattern=percent is not None,
        vertical_scroll_percent=percent,
        vertical_view_size=50.0 if percent is not None else None,
    )


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []
        self.scrolls: list[tuple[str, int]] = []
        self.clicks: list[tuple[int, int]] = []

    def move_to(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def scroll(self, direction: str, amount: int) -> None:
        self.scrolls.append((direction, amount))

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[tuple[str, int]] = []

    def press(self, key: str, count: int = 1) -> None:
        self.presses.append((key, count))


class _FakeBackend:
    def __init__(self, pattern_ok: bool = True) -> None:
        self.pattern_ok = pattern_ok
        self.calls: list[tuple] = []

    def scroll_container_pattern(self, container, pixels, handle=None):
        self.calls.append(("pattern", pixels))
        return self.pattern_ok

    def container_state(self, container, handle=None):
        self.calls.append(("state",))
        return container

    def container_scrollbar(self, container, handle=None):
        self.calls.append(("scrollbar",))
        return UiaNode(name="vscroll", control_type="ScrollBar", rect=BBox(490, 0, 16, 600))


# -- pick_left_right_containers ---------------------------------------------


def test_pick_containers_by_content_overlap() -> None:
    containers = [
        _container("left", 0, 0, 200, 600),
        _container("right", 300, 0, 400, 600),
        _container("tiny", 700, 0, 50, 30),
    ]
    chosen = pick_left_right_containers(
        containers,
        left_rect=BBox(10, 20, 180, 500),   # source content lives on the left
        right_rect=BBox(320, 20, 360, 500),  # form content lives on the right
        client_rect=(0, 0, 1024, 768),
    )
    assert chosen[PANEL_LEFT].name == "left"
    assert chosen[PANEL_RIGHT].name == "right"
    assert "tiny" not in chosen.values()


def test_pick_containers_falls_back_to_largest_split() -> None:
    containers = [
        _container("A", 0, 0, 180, 700),
        _container("B", 200, 0, 300, 700),
        _container("C", 600, 0, 200, 700),
    ]
    chosen = pick_left_right_containers(containers, client_rect=(0, 0, 1000, 800))
    assert chosen[PANEL_LEFT].name == "A"
    assert chosen[PANEL_RIGHT].name == "B"  # the largest right-of-centre panel


def test_pick_containers_empty() -> None:
    assert pick_left_right_containers([]) == {}


def test_pick_containers_filters_out_of_client() -> None:
    containers = [_container("far", 5000, 0, 200, 600)]
    assert pick_left_right_containers(containers, client_rect=(0, 0, 1024, 768)) == {}


# -- PanelScroller -----------------------------------------------------------


def test_scroller_succeeds_on_first_method() -> None:
    backend = _FakeBackend(pattern_ok=True)
    mouse = _FakeMouse()
    scroller = PanelScroller(backend=backend, mouse=mouse, keyboard=_FakeKeyboard(), settle=(0.0, 0.0))
    container = _container("right", 200, 0, 300, 600)
    outcome = scroller.scroll_down(container, 300, verify=lambda: True)
    assert outcome.ok is True
    assert outcome.changed is True
    assert outcome.method == "pattern"
    assert backend.calls[0][0] == "pattern"
    assert not mouse.scrolls  # no fallback needed


def test_scroller_prefers_dom_method_when_wired() -> None:
    """Chrome-hosted forms are scrolled by mutating DOM scrollTop first: when a
    DOM controller is wired it must win over the UIA pattern."""
    backend = _FakeBackend(pattern_ok=True)
    calls = {"dom": 0}

    def dom(container, pixels) -> bool:
        calls["dom"] += 1
        return True

    scroller = PanelScroller(backend=backend, dom=dom, settle=(0.0, 0.0))
    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=lambda: True)
    assert outcome.method == "dom"
    assert calls["dom"] == 1
    assert ("pattern", 300) not in backend.calls  # the UIA pattern was never needed


def test_scroller_escalates_until_verify_passes() -> None:
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    keyboard = _FakeKeyboard()
    scroller = PanelScroller(backend=backend, mouse=mouse, keyboard=keyboard, settle=(0.0, 0.0))

    verify_calls = {"n": 0}

    def verify() -> bool:
        verify_calls["n"] += 1
        return verify_calls["n"] >= 4  # 3 pattern attempts, then the wheel at 300px

    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=verify)
    assert outcome.ok is True
    assert outcome.method == "wheel"
    assert ("down", 6) in mouse.scrolls  # 300px / 50px per notch = 6 notches
    assert mouse.clicks  # every wheel is preceded by a focus click inside the panel


def test_scroller_reports_failure_when_nothing_moves() -> None:
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    scroller = PanelScroller(backend=backend, mouse=mouse, settle=(0.0, 0.0))
    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=lambda: False)
    assert outcome.ok is False
    assert outcome.changed is False
    # Every fallback was attempted: pattern (3 escalating attempts), then the
    # click-focus wheel (3 escalating attempts). Nothing moved, so it failed.
    assert len(backend.calls) >= 3
    assert len(mouse.scrolls) == 3
    # Every wheel was preceded by a focus click inside the panel.
    assert len(mouse.clicks) == len(mouse.scrolls)


def test_scroller_wheel_escalates_distance_and_focuses_first() -> None:
    """Method 2 (wheel) is retried with escalating distances - 300 -> 600 -> 900
    px - and EVERY wheel is preceded by a focus click inside the panel (never a
    bare wheel over the window)."""
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    scroller = PanelScroller(backend=backend, mouse=mouse, settle=(0.0, 0.0))

    verify_calls = {"n": 0}

    def verify() -> bool:
        verify_calls["n"] += 1
        return verify_calls["n"] == 5  # 3 pattern attempts + wheel at 600px

    container = _container("right", 200, 0, 300, 600)
    outcome = scroller.scroll_down(container, 300, verify=verify)
    assert outcome.ok is True
    assert outcome.method == "wheel"
    # Adaptive distances: 300px (6 notches) failed, so 600px (12 notches) moved it.
    assert mouse.scrolls == [("down", 6), ("down", 12)]
    # The focus click landed inside the panel BEFORE every wheel.
    assert len(mouse.clicks) == len(mouse.scrolls)
    cx, cy = container.rect.center
    assert mouse.clicks == [(int(cx), int(cy)), (int(cx), int(cy))]


def test_scroller_never_scrolls_up() -> None:
    """NEVER REVERSE SCROLL: every wheel/drag attempt is DOWN, even when the
    scene never changes (a failed scroll is retried forward, never up)."""
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    drags: list[tuple] = []
    scroller = PanelScroller(
        backend=backend, mouse=mouse, drag=lambda x1, y1, x2, y2: drags.append((x1, y1, x2, y2)) or False,
        settle=(0.0, 0.0),
    )
    scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=lambda: False)
    assert mouse.scrolls and all(direction == "down" for direction, _ in mouse.scrolls)
    # Every drag goes downward (end_y below start_y), never upward.
    assert drags and all(y2 >= y1 for _, y1, _, y2 in drags)


def test_scroller_drag_is_last_resort_fallback() -> None:
    """When UIA and the wheel both fail, the scrollbar-thumb drag is tried and
    wins as soon as it moves the scene (Method 3)."""
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    drag_calls = {"n": 0}

    def drag(x1, y1, x2, y2) -> bool:
        drag_calls["n"] += 1
        return True

    scroller = PanelScroller(backend=backend, mouse=mouse, drag=drag, settle=(0.0, 0.0))

    verify_calls = {"n": 0}

    def verify() -> bool:
        verify_calls["n"] += 1
        return verify_calls["n"] == 9  # 3 pattern + 3 wheel + 3rd drag attempt

    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=verify)
    assert outcome.ok is True
    assert outcome.method == "scrollbar-drag"
    assert drag_calls["n"] == 3


def test_scroller_keyboard_is_tried_after_drag_fails() -> None:
    """Method 4 (keyboard): PageDown is tried once UIA, wheel and the
    scrollbar drag have all failed, and the panel is click-focused first."""
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    keyboard = _FakeKeyboard()
    drag_calls = {"n": 0}

    def drag(x1, y1, x2, y2) -> bool:
        drag_calls["n"] += 1
        return False

    scroller = PanelScroller(
        backend=backend, mouse=mouse, keyboard=keyboard, drag=drag, settle=(0.0, 0.0)
    )

    verify_calls = {"n": 0}

    def verify() -> bool:
        verify_calls["n"] += 1
        # 3 pattern + 3 wheel + 3 drag = 9 attempts before keyboard's first try.
        return verify_calls["n"] == 10

    container = _container("right", 200, 0, 300, 600)
    outcome = scroller.scroll_down(container, 300, verify=verify)
    assert outcome.ok is True
    assert outcome.method == "keyboard"
    assert drag_calls["n"] == 3  # drag was exhausted before keyboard was tried
    assert keyboard.presses  # PageDown was actually sent
    assert keyboard.presses[0][0] == "pagedown"
    # The panel was click-focused before the key press, like the wheel path.
    assert mouse.clicks[-1] == (int(container.rect.center[0]), int(container.rect.center[1]))


def test_scroller_keyboard_skipped_when_not_wired() -> None:
    """No keyboard object means Method 4 is silently skipped, not crashed."""
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    scroller = PanelScroller(backend=backend, mouse=mouse, keyboard=None, settle=(0.0, 0.0))
    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=lambda: False)
    assert outcome.ok is False
    assert outcome.method == "none"


def test_scroller_plugin_override_is_absolute_last_resort() -> None:
    """Method 6: a plugin override is only consulted after every generic
    method (pattern, wheel, drag, keyboard) has failed to move the panel."""
    backend = _FakeBackend(pattern_ok=False)
    mouse = _FakeMouse()
    keyboard = _FakeKeyboard()
    override_calls: list[int] = []

    def override(container, pixels) -> bool:
        override_calls.append(pixels)
        return True

    scroller = PanelScroller(
        backend=backend, mouse=mouse, keyboard=keyboard, override=override, settle=(0.0, 0.0)
    )
    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=lambda: False)
    assert outcome.ok is True
    assert outcome.method == "plugin-override"
    assert override_calls == [300]  # won on the first override attempt


def test_scroller_plugin_override_deferring_none_still_fails() -> None:
    """An override that always returns None defers to the engine; with no
    other method left, the scroll is correctly reported as failed."""
    backend = _FakeBackend(pattern_ok=False)
    scroller = PanelScroller(backend=backend, override=lambda c, p: None, settle=(0.0, 0.0))
    outcome = scroller.scroll_down(_container("right", 200, 0, 300, 600), 300, verify=lambda: False)
    assert outcome.ok is False
    assert outcome.method == "none"


def test_scroller_percent_change_is_a_success() -> None:
    backend = _FakeBackend(pattern_ok=True)

    class _MovingBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__(pattern_ok=True)
            self._next_percent = 10.0

        def container_state(self, container, handle=None):
            container.vertical_scroll_percent = self._next_percent
            self._next_percent = min(100.0, self._next_percent + 10.0)
            return container

    scroller = PanelScroller(backend=_MovingBackend(), mouse=None, keyboard=None, settle=(0.0, 0.0))
    container = _container("right", 200, 0, 300, 600, percent=0.0)
    # verify() stays False (scene appears frozen) but the scroll percent moved.
    outcome = scroller.scroll_down(container, 300, verify=lambda: False)
    assert outcome.ok is True
    assert outcome.changed is True
    assert outcome.percent_after >= 10.0


def test_scroll_outcome_to_dict() -> None:
    data = ScrollOutcome(ok=True, changed=True, method="pattern", percent_before=0.0, percent_after=5.0).to_dict()
    assert data["method"] == "pattern"
    assert data["ok"] is True and data["changed"] is True


def test_container_more_content_properties() -> None:
    partial = _container("left", 0, 0, 200, 600, percent=45.0)
    at_max = _container("left", 0, 0, 200, 600, percent=100.0)
    unknown = _container("left", 0, 0, 200, 600, percent=None)
    assert partial.more_content is True and partial.at_max is False
    assert at_max.more_content is False and at_max.at_max is True
    # Unknown scroll position is treated as "more content may exist".
    assert unknown.more_content is True and unknown.at_max is False
