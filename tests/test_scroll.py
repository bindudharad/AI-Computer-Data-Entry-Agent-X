"""Tests for the dual-panel scroll reasoning (DualPanelScroll)."""

from __future__ import annotations

from atlas.vision.models import BBox, ElementType, SceneDescription, ScreenElement
from atlas.workflow.scroll import PANEL_LEFT, PANEL_RIGHT, DualPanelScroll


def _el(
    element_id: str,
    type: ElementType,
    left: int,
    top: int,
    w: int = 80,
    h: int = 22,
    label: str | None = None,
) -> ScreenElement:
    return ScreenElement(
        element_id=element_id,
        type=type,
        label=label or element_id,
        name=label or element_id,
        bbox=BBox(left, top, w, h),
    )


def _scene(*elements: ScreenElement) -> SceneDescription:
    return SceneDescription(elements=list(elements))


def _editable(element_id: str, left: int, top: int) -> ScreenElement:
    return _el(element_id, ElementType.TEXTBOX, left, top)


def test_update_panels_clamps_rects_to_client_area() -> None:
    ctrl = DualPanelScroll()
    left = BBox(0, 0, 400, 800)
    right = BBox(500, 0, 400, 800)
    ctrl.update_panels(
        {PANEL_LEFT: left, PANEL_RIGHT: right},
        client=(0, 0, 900, 500),
    )
    assert ctrl.left.rect == BBox(0, 0, 400, 500)
    assert ctrl.right.rect == BBox(500, 0, 400, 500)


def test_update_panels_drops_fully_occluded_rect() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 600, 400, 300), PANEL_RIGHT: None},
        client=(0, 0, 900, 500),
    )
    assert ctrl.left.rect is None
    assert ctrl.right.rect is None
    assert ctrl.known_panels() == []


def test_bottom_detection_after_stall_limit() -> None:
    """A panel is declared at its bottom after the stall limit once its content
    has demonstrably moved at least once, and then stopped changing."""
    ctrl = DualPanelScroll(stall_limit=3)
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    assert ctrl.both_at_bottom() is False
    # Seed + one scroll that moves BOTH panels' content (they stay in lockstep).
    ctrl.record_observation(_scene(_editable("f0", 520, 40), _el("s0", ElementType.LABEL, 10, 40)))
    ctrl.record_observation(_scene(_editable("f0", 520, 80), _el("s0", ElementType.LABEL, 10, 80)))
    assert ctrl.both_at_bottom() is False
    # First static observation only seeds the new signature; the stall count
    # increments on each following identical observation.
    for _ in range(4):
        ctrl.record_observation(_scene(_editable("f0", 520, 80), _el("s0", ElementType.LABEL, 10, 80)))
    assert ctrl.right.at_bottom is True
    assert ctrl.left.at_bottom is True
    assert ctrl.both_at_bottom() is True


def test_never_moved_panel_is_not_at_bottom() -> None:
    """Regression: a panel WITH a scroll container that still reports content
    below the fold must NOT be declared at its bottom even when its scrolls
    never moved anything. Declaring it done would let the reveal pass stop and
    submit a half-scrolled form (the MPF panels expose no ScrollPattern, so a
    failing scroll stalls forever without ever reaching the upload section)."""
    from atlas.observe.uia import ScrollContainer

    ctrl = DualPanelScroll(stall_limit=3)
    left = ScrollContainer(name="Record summary", control_type="Group",
                           rect=BBox(0, 0, 294, 537), has_scroll_pattern=False,
                           vertical_scroll_percent=None)
    right = ScrollContainer(name="", control_type="Group",
                            rect=BBox(500, 0, 565, 537), has_scroll_pattern=False,
                            vertical_scroll_percent=None)
    ctrl.update_panels(
        {PANEL_LEFT: None, PANEL_RIGHT: None},
        None,
        containers={PANEL_LEFT: left, PANEL_RIGHT: right},
    )
    static = _scene(
        _editable("f0", 520, 40),
        _el("s0", ElementType.LABEL, 10, 40),
    )
    # Many identical observations: content never moved and the containers keep
    # reporting more content below, so neither panel may be declared done.
    for _ in range(12):
        ctrl.record_observation(static)
    assert ctrl.left.at_bottom is False
    assert ctrl.right.at_bottom is False
    assert ctrl.both_at_bottom() is False
    assert ctrl.form_complete() is False


def test_container_at_max_is_at_bottom_without_movement() -> None:
    """A container that reports it is scrolled to its maximum is done even if
    the visible content never changed (it is already at the bottom)."""
    from atlas.observe.uia import ScrollContainer

    ctrl = DualPanelScroll(stall_limit=3)
    at_max = ScrollContainer(name="", control_type="Group", rect=BBox(500, 0, 565, 537),
                             has_scroll_pattern=False, vertical_scroll_percent=100.0)
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: None},
        None,
        containers={PANEL_RIGHT: at_max},
    )
    static = _scene(_editable("f0", 520, 40), _el("s0", ElementType.LABEL, 10, 40))
    for _ in range(5):
        ctrl.record_observation(static)
    assert ctrl.right.at_bottom is True
    assert ctrl.left.at_bottom is True  # no container -> plain stall heuristic


def test_no_container_static_viewport_reaches_bottom() -> None:
    """A single-viewport form with NO scroll container (e.g. a small web form)
    is complete after the stall limit even though nothing ever moved: there is
    no evidence of content below the fold, so a static viewport is the bottom."""
    ctrl = DualPanelScroll(stall_limit=3)
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    static = _scene(_editable("f0", 520, 40), _el("s0", ElementType.LABEL, 10, 40))
    for _ in range(5):
        ctrl.record_observation(static)
    assert ctrl.both_at_bottom() is True
    assert ctrl.form_complete() is True


def test_panel_at_bottom_only_after_content_moved_once() -> None:
    """A panel that moved exactly once, then stalled, is done after the limit."""
    ctrl = DualPanelScroll(stall_limit=3)
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    # Right panel moves once (scroll 1: top 40 -> 100), then goes static.
    ctrl.record_observation(_scene(_editable("f0", 520, 40), _el("s0", ElementType.LABEL, 10, 40)))
    ctrl.record_observation(_scene(_editable("f0", 520, 100), _el("s0", ElementType.LABEL, 10, 40)))
    for _ in range(6):
        ctrl.record_observation(_scene(_editable("f0", 520, 100), _el("s0", ElementType.LABEL, 10, 40)))
    assert ctrl.right.at_bottom is True   # moved once, then stalled past limit
    assert ctrl.left.at_bottom is True    # no container -> stall heuristic


def test_panel_not_at_bottom_while_content_moves() -> None:
    ctrl = DualPanelScroll(stall_limit=3)
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    for i in range(6):
        ctrl.record_observation(_scene(_editable("f0", 520, 40 + i)))
        assert ctrl.right.at_bottom is False  # content keeps moving


def test_signature_ignores_off_panel_content() -> None:
    """An element in the other panel must not appear in this panel's signature."""
    ctrl = DualPanelScroll()
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    scene = _scene(
        _editable("left_field", 50, 40),
        _editable("right_field", 550, 40),
    )
    left_sig = ctrl.panel_signature(scene, ctrl.left)
    right_sig = ctrl.panel_signature(scene, ctrl.right)
    assert "left_field" in left_sig and "right_field" not in left_sig
    assert "right_field" in right_sig and "left_field" not in right_sig


def test_lagging_panel_detects_asymmetric_movement() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    ctrl.record_observation(_scene(_editable("f0", 520, 40), _el("s0", ElementType.LABEL, 10, 40)))
    # Left moves down, right unchanged -> the RIGHT panel lagged.
    ctrl.record_observation(_scene(_editable("f0", 520, 40), _el("s0", ElementType.LABEL, 10, 80)))
    assert ctrl.lagging_panel() == PANEL_RIGHT


def test_scroll_notches_bounds() -> None:
    ctrl = DualPanelScroll()
    sparse = _scene(*[_el(f"s{i}", ElementType.LABEL, 10, 30 + i * 200, 80, 16) for i in range(4)])
    dense = _scene(*[_el(f"s{i}", ElementType.LABEL, 10, 30 + i * 20, 80, 16) for i in range(6)])
    assert 3 <= ctrl.scroll_notches(sparse) <= 8
    assert 3 <= ctrl.scroll_notches(dense) <= 8
    # Sparse forms scroll more per wheel notch than dense grids.
    assert ctrl.scroll_notches(sparse) >= ctrl.scroll_notches(dense)


def test_scroll_anchor_prefers_non_editable_element() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels({PANEL_LEFT: BBox(0, 0, 400, 500)}, None)
    scene = _scene(
        _el("header", ElementType.BUTTON, 10, 20, 200, 30),
        _editable("f0", 50, 100),
    )
    anchor = ctrl.scroll_anchor(PANEL_LEFT, scene)
    assert anchor is not None
    x, y = anchor
    # The anchor sits on the non-editable header, not on the editable field.
    header = scene.element("header")
    assert header is not None and header.bbox is not None
    assert header.bbox.contains(x, y)


def test_scroll_anchor_falls_back_to_panel_top_strip() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels({PANEL_LEFT: BBox(0, 0, 400, 500)}, None)
    anchor = ctrl.scroll_anchor(PANEL_LEFT, _scene(_editable("f0", 50, 40)))
    assert anchor is not None
    x, y = anchor
    # Inside the panel, high up (the header/margin strip), not on the field.
    assert ctrl.left.rect.contains(x, y)
    assert y < ctrl.left.rect.top + ctrl.left.rect.height // 2


def test_upload_visible_is_sticky() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    with_upload = _scene(
        _el("sec", ElementType.BUTTON, 520, 40, 200, 30, label="Upload Details"),
        _editable("f0", 520, 120),
    )
    without_upload = _scene(_editable("f0", 520, 40))
    ctrl.record_observation(with_upload)
    assert ctrl.upload_visible is True
    ctrl.record_observation(without_upload)
    assert ctrl.upload_visible is True  # sticky once seen


def test_completion_reason() -> None:
    ctrl = DualPanelScroll()
    ctrl.left.at_bottom = True
    ctrl.right.at_bottom = True
    ctrl.upload_visible = True
    assert "Upload Details" in ctrl.completion_reason()
    ctrl.upload_visible = False
    assert "no upload" in ctrl.completion_reason()


def test_reset_clears_progress() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels(
        {PANEL_LEFT: BBox(0, 0, 400, 500), PANEL_RIGHT: BBox(500, 0, 400, 500)},
        None,
    )
    ctrl.record_observation(_scene(_el("sec", ElementType.BUTTON, 10, 10, 200, 30, label="Upload Details")))
    ctrl.left.at_bottom = True
    ctrl.right.at_bottom = True
    ctrl.reset()
    assert ctrl.both_at_bottom() is False
    assert ctrl.upload_visible is False
    assert ctrl.left.stall == 0 and ctrl.right.stall == 0
    assert ctrl.panel(PANEL_LEFT).rect == BBox(0, 0, 400, 500)


def test_measure_movement_updates_last_delta() -> None:
    ctrl = DualPanelScroll()
    ctrl.update_panels({PANEL_RIGHT: BBox(500, 0, 400, 500)}, None)
    ctrl.record_observation(_scene(_editable("f0", 520, 200)))
    ctrl.record_observation(_scene(_editable("f0", 520, 150)))
    # Content moved up by 50px on the right panel.
    assert ctrl.right.last_delta == 50
