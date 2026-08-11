"""Tests for overflow-based scroll discovery and DOM scrolling."""

from __future__ import annotations

from atlas.observe.uia import ScrollContainer, discover_overflow_containers
from atlas.vision.models import BBox
from atlas.workflow.scroller import ChromeDomScroller


def _node(name: str, control_type: str, rect: tuple[int, int, int, int], children: list | None = None) -> dict:
    return {
        "name": name,
        "control_type": control_type,
        "automation_id": "",
        "class_name": "",
        "framework_id": "",
        "handle": None,
        "rect": list(rect),
        "children": list(children or []),
    }


def _mpf_tree() -> dict:
    """Mirror of the real MPF UIA tree: a Chrome window whose content root has
    two scrollable side panels plus a full-window wrapper chain."""
    left_panel_children = []
    # The source panel shows rows down to y=1056 inside a clip of y<=831.
    for i in range(20):
        top = 301 + i * 40
        left_panel_children.append(_node("label", "Text", (542, top, 700, top + 15)))
    left_panel_children.append(_node("overflowing", "Text", (640, 1041, 779, 1056)))

    right_sections = []
    for i in range(5):
        top = 301 + i * 260
        right_sections.append(_node(f"section{i}", "Group", (836, top, 1250, top + 240)))
    right_sections.append(_node("upload", "Button", (836, 1566, 1250, 1606)))
    content_wrapper = _node("", "Group", (836, 301, 1250, 1609), right_sections)
    right_panel = _node("", "Group", (829, 294, 1394, 831), [content_wrapper])

    return _node(
        "MPF (Download and Upload Form)", "Window", (273, 23, 1649, 999),
        [
            _node(
                "", "Pane", (283, 61, 1643, 992),
                [
                    _node(
                        "", "Group", (283, 61, 1642, 991),
                        [
                            _node("Record summary", "Group", (531, 294, 825, 831), left_panel_children),
                            right_panel,
                        ],
                    )
                ],
            )
        ],
    )


def test_overflow_discovery_finds_both_mpf_panels() -> None:
    containers = discover_overflow_containers(_mpf_tree())
    names = sorted(c.name for c in containers)
    assert names == ["", "Record summary"]
    by_name = {c.name: c for c in containers}
    left = by_name["Record summary"]
    right = by_name[""]
    assert left.rect == BBox(531, 294, 294, 537)
    assert right.rect == BBox(829, 294, 565, 537)
    # Overflow containers carry no ScrollPattern: unknown percent -> more content.
    assert left.has_scroll_pattern is False
    assert left.more_content is True


def test_overflow_discovery_filters_window_wrappers() -> None:
    containers = discover_overflow_containers(_mpf_tree())
    # The full-window Pane/Group wrappers must never be reported as panels.
    assert not any(c.control_type == "Pane" for c in containers)
    assert all(c.rect.width < 1000 for c in containers)


def test_overflow_discovery_ignores_non_overflowing_containers() -> None:
    tree = _node(
        "w", "Window", (0, 0, 800, 600),
        [
            _node("small", "Group", (10, 10, 400, 300), [
                _node("inner", "Text", (20, 20, 100, 50)),
            ]),
        ],
    )
    assert discover_overflow_containers(tree) == []


def test_overflow_discovery_drops_tall_content_wrapper() -> None:
    """The tall content wrapper inside a visible panel is not a separate panel:
    only the clip (the 565x537 group) is reported."""
    containers = discover_overflow_containers(_mpf_tree())
    rects = [(c.rect.width, c.rect.height) for c in containers]
    assert (565, 537) in rects
    assert (414, 1308) not in rects


class _FakeDom:
    def __init__(self, result) -> None:
        self._result = result
        self.scripts: list[str] = []

    def evaluate(self, script: str):
        self.scripts.append(script)
        return self._result


def test_dom_scroller_mutates_scroll_top_and_refreshes_percent() -> None:
    dom = _FakeDom([True, 25.0])
    scroller = ChromeDomScroller(dom.evaluate)
    container = ScrollContainer(name="", control_type="Group", rect=BBox(829, 294, 565, 537))
    moved = scroller.scroll_down(container, 300)
    assert moved is True
    assert container.vertical_scroll_percent == 25.0
    assert any("scrollTop" in s and "300" in s for s in dom.scripts)


def test_dom_scroller_reports_failure_when_element_absent() -> None:
    dom = _FakeDom(None)
    scroller = ChromeDomScroller(dom.evaluate)
    moved = scroller.scroll_down(
        ScrollContainer(name="", control_type="Group", rect=BBox(829, 294, 565, 537)), 300
    )
    assert moved is False
