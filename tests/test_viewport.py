"""Tests for the human-like ViewportModel and NO SCROLL RULE."""

from __future__ import annotations

from atlas.act.models import Action, ActionResult, ActionType
from atlas.mapping.mapper import FieldMapping, MappingResult
from atlas.reason.planner import ActionPlanner
from atlas.understanding.fields import discover_fields
from atlas.vision.models import BBox, ElementType, SceneDescription, ScreenElement
from atlas.workflow.viewport import ViewportModel


def _el(element_id: str, type: ElementType, left: int, top: int, w: int = 80, h: int = 22) -> ScreenElement:
    return ScreenElement(
        element_id=element_id,
        type=type,
        label=element_id,
        name=element_id,
        bbox=BBox(left, top, w, h),
    )


def _scene(*elements: ScreenElement) -> SceneDescription:
    return SceneDescription(elements=list(elements))


def _action(field_id: str, ok: bool = True) -> ActionResult:
    return ActionResult(action=Action(type=ActionType.TYPE, field_id=field_id), success=ok, verified=ok)


VIEW = (800, 600)


def test_visible_fields_intersect_viewport_band() -> None:
    """A field scrolled above the top edge or fully below the fold is not visible."""
    from atlas.understanding.fields import discover_fields
    from atlas.workflow.viewport import visible_fields

    scene = _scene(
        _el("above", ElementType.TEXTBOX, 10, -40, 80, 22),   # scrolled off the top
        _el("t1", ElementType.TEXTBOX, 10, 10, 80, 22),        # visible
        _el("below", ElementType.TEXTBOX, 10, 5000, 80, 22),   # below the fold
    )
    fields = discover_fields(scene)
    visible = [f.element_id for f in visible_fields(fields, VIEW)]
    assert visible == ["t1"]


def test_no_scroll_rule_blocks_when_fields_unfilled() -> None:
    scene = _scene(_el("t1", ElementType.TEXTBOX, 10, 10))
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids=set())
    assert model.can_scroll is False
    assert model.has_unfilled_visible
    assert model.scroll_blocked_reason()


def test_no_scroll_rule_allows_when_viewport_complete() -> None:
    scene = _scene(_el("t1", ElementType.TEXTBOX, 10, 10))
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids={"t1"})
    assert not model.has_unfilled_visible
    assert model.dropdowns_done and model.dates_done and model.upload_checked
    assert model.verification_passed
    assert model.can_scroll is True


def test_verification_failure_blocks_scroll() -> None:
    scene = _scene(_el("t1", ElementType.TEXTBOX, 10, 10))
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids={"t1"}, results=[_action("t1", ok=False)])
    assert model.verification_passed is False
    assert model.can_scroll is False


def test_pending_dropdown_blocks_scroll() -> None:
    scene = _scene(_el("d1", ElementType.COMBOBOX, 10, 10))
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids=set())
    assert not model.dropdowns_done
    assert model.can_scroll is False


def test_upload_section_must_be_expanded_before_scroll() -> None:
    upload_collapsed = ScreenElement(
        element_id="sec",
        type=ElementType.BUTTON,
        label="Upload Details",
        name="Upload Details",
        bbox=BBox(10, 300, 200, 30),
    )
    upload_field = _el("u1", ElementType.FILE_UPLOAD, 10, 340, 120, 24)
    scene = _scene(upload_collapsed, upload_field)
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids=set())
    assert model.upload_checked is False
    assert model.can_scroll is False
    # Once the header is "expanded", the section no longer blocks scrolling
    # (the upload field itself is still pending and will be filled first).
    model.expanded_upload_ids.add("sec")
    assert model.pending_uploads
    assert model.can_scroll is False


def test_pending_uploads_block_scroll() -> None:
    scene = _scene(_el("u1", ElementType.FILE_UPLOAD, 10, 10))
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids=set())
    assert model.upload_checked is False
    assert model.can_scroll is False


def test_off_viewport_field_is_not_pending() -> None:
    # A below-the-fold field must not block scrolling: it is not visible yet.
    scene = _scene(_el("d1", ElementType.TEXTBOX, 10, 5000))
    model = ViewportModel(scene=scene, viewport=VIEW, handled_ids=set())
    assert model.unfilled_visible == []
    assert model.has_unfilled_visible is False


def test_planner_fills_in_visual_order() -> None:
    """Fields are processed one-by-one in strict visual order, NOT grouped by
    type. A textbox below a date picker is filled AFTER that date picker, even
    though textboxes 'rank' earlier by type."""
    planner = ActionPlanner(verify_after_action=False)
    scene = _scene(
        _el("date", ElementType.DATE_PICKER, 10, 100),
        _el("text2", ElementType.TEXTBOX, 10, 200),
        _el("drop", ElementType.COMBOBOX, 10, 300, w=120, h=24),
        _el("text1", ElementType.TEXTBOX, 10, 90),
    )
    fields = discover_fields(scene)
    mappings = [
        FieldMapping(
            source_label=f"src-{f.element_id}",
            source_value="V",
            target=f,
            method="exact",
            confidence=1.0,
        )
        for f in fields
    ]
    mapping = MappingResult(mappings=mappings, unmatched_fields=[])

    plan = planner.plan_fill(None, mapping, scene=scene, submit_element_id=None)
    ordered = [
        a.field_id
        for a in plan.actions
        if a.type in {ActionType.TYPE, ActionType.SELECT, ActionType.CHOOSE_DATE}
    ]
    # Strict visual (reading) order: text1(90), date(100), text2(200), drop(300).
    assert ordered == ["text1", "date", "text2", "drop"]


def test_planner_interleaves_types_in_visual_order() -> None:
    """Textbox -> dropdown -> textbox -> date: each control is completed in
    sequence, never all textboxes first then all dropdowns."""
    planner = ActionPlanner(verify_after_action=False)
    scene = _scene(
        _el("a", ElementType.TEXTBOX, 10, 10),
        _el("b", ElementType.COMBOBOX, 10, 50, w=120, h=24),
        _el("c", ElementType.TEXTBOX, 10, 90),
        _el("d", ElementType.DATE_PICKER, 10, 130),
        _el("e", ElementType.CHECKBOX, 10, 170),
    )
    fields = discover_fields(scene)
    mappings = [
        FieldMapping(
            source_label=f"src-{f.element_id}",
            source_value="V",
            target=f,
            method="exact",
            confidence=1.0,
        )
        for f in fields
    ]
    mapping = MappingResult(mappings=mappings, unmatched_fields=[])
    plan = planner.plan_fill(None, mapping, scene=scene, submit_element_id=None)
    ordered = [
        a.field_id
        for a in plan.actions
        if a.type in {ActionType.TYPE, ActionType.SELECT, ActionType.CHOOSE_DATE, ActionType.TOGGLE}
    ]
    assert ordered == ["a", "b", "c", "d", "e"]
    # And the value-producing actions are interleaved by position, not by type.
    assert [a.field_id for a in plan.actions if a.type == ActionType.TYPE] == ["a", "c"]
    assert [a.field_id for a in plan.actions if a.type == ActionType.SELECT] == ["b"]
