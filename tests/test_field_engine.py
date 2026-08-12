"""Tests for the field-driven fill engine (performance path)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from atlas.act.models import ActionType
from atlas.observe.uia import ScrollContainer, UiaNode
from atlas.vision.models import BBox, ElementType
from atlas.workflow.field_engine import (
    DateGroupTarget,
    FieldStatus,
    FieldTarget,
    PendingFieldQueue,
    PerfTracker,
    ProgressGuard,
    ScrollCapabilityCache,
    TargetNavigator,
    _date_parts,
    _find_date_value,
    build_field_actions,
    build_field_queue,
    classify_fill_status,
    field_coverage_summary,
    split_date_parts,
)
from atlas.workflow.scroll import PANEL_LEFT, PANEL_RIGHT
from atlas.workflow.scroller import (
    SCROLL_METHOD_DOM,
    SCROLL_METHOD_PATTERN,
    SCROLL_METHOD_WHEEL,
    pick_left_right_containers,
)


def _node(name: str, ctype: str, x: int, y: int, w: int = 200, h: int = 24, handle: int | None = None) -> UiaNode:
    return UiaNode(
        name=name,
        control_type=ctype,
        automation_id=name.lower().replace(" ", "_"),
        handle=handle,
        rect=BBox(x, y, w, h),
        enabled=True,
    )


def _field_map(right_fields: list[UiaNode], mappings: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        right_fields=right_fields,
        mappings=mappings or [],
        left_rect=BBox(0, 0, 200, 600),
        right_rect=BBox(300, 0, 400, 600),
        upload_button=None,
        has_form=True,
    )


def _record(pairs: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(pairs=pairs, record_key="rec-1", title="test")


# -- build_field_queue -------------------------------------------------------


def test_queue_orders_fields_by_top_left_and_binds_values() -> None:
    fields = [
        _node("State", "ComboBox", 300, 300),
        _node("Full Name", "Edit", 300, 100),
        _node("Gender", "ComboBox", 300, 200),
    ]
    mappings = [
        {"source": "Full Name", "target": "Full Name", "confidence": 0.9},
        {"source": "Gender", "target": "Gender", "confidence": 0.9},
        {"source": "State", "target": "State", "confidence": 0.9},
    ]
    record = _record({"Full Name": "John", "Gender": "Male", "State": "Karnataka"})
    queue = build_field_queue(_field_map(fields, mappings), record)
    assert [q.label for q in queue.items] == ["Full Name", "Gender", "State"]
    assert queue.items[0].value == "John"
    assert queue.items[1].value == "Male"
    assert queue.items[2].value == "Karnataka"


def test_queue_leaves_unmapped_fields_value_none() -> None:
    fields = [_node("Full Name", "Edit", 300, 100), _node("Middle Name", "Edit", 300, 200)]
    mappings = [{"source": "Full Name", "target": "Full Name", "confidence": 0.9}]
    record = _record({"Full Name": "John"})
    queue = build_field_queue(_field_map(fields, mappings), record)
    assert queue.items[0].value == "John"
    assert queue.items[1].value is None


def test_queue_groups_date_combos_and_splits_value() -> None:
    fields = [
        _node("Day", "ComboBox", 300, 100),
        _node("Month", "ComboBox", 520, 100),
        _node("Year", "ComboBox", 740, 100),
    ]
    mappings = [{"source": "DOB", "target": "Day", "confidence": 1.0}]
    record = _record({"DOB": "15-08-1990"})
    queue = build_field_queue(_field_map(fields, mappings), record)
    assert len(queue.items) == 1
    group = queue.items[0]
    assert isinstance(group, DateGroupTarget)
    assert [t.value for t in group.targets] == ["15", "08", "1990"]


def test_queue_groups_date_combos_when_label_on_first() -> None:
    fields = [
        _node("Date of Birth", "ComboBox", 300, 100),
        _node("", "ComboBox", 520, 100, handle=2),
        _node("", "ComboBox", 740, 100, handle=3),
    ]
    mappings = [{"source": "DOB", "target": "Date of Birth", "confidence": 1.0}]
    record = _record({"DOB": "12/31/2000"})
    queue = build_field_queue(_field_map(fields, mappings), record)
    assert len(queue.items) == 1
    assert isinstance(queue.items[0], DateGroupTarget)
    assert [t.value for t in queue.items[0].targets] == ["12", "31", "2000"]


def test_queue_does_not_group_non_date_combos() -> None:
    fields = [
        _node("Caste", "ComboBox", 300, 100),
        _node("Sub Caste", "ComboBox", 520, 100),
        _node("State", "ComboBox", 300, 200),
    ]
    record = _record({})
    queue = build_field_queue(_field_map(fields), record)
    assert len(queue.items) == 3
    assert all(not isinstance(q, DateGroupTarget) for q in queue.items)


def test_queue_groups_unnamed_adjacent_combos_and_binds_date_value() -> None:
    fields = [
        _node("", "ComboBox", 972, 440, w=54),
        _node("", "ComboBox", 1027, 440, w=132),
        _node("", "ComboBox", 1161, 440, w=68),
    ]
    record = _record({"Name": "John", "Date of Birth": "02-02-1996"})
    queue = build_field_queue(_field_map(fields), record)
    assert len(queue.items) == 1
    group = queue.items[0]
    assert isinstance(group, DateGroupTarget)
    assert [t.value for t in group.targets] == ["02", "02", "1996"]
    assert group.date_value == "02-02-1996"


def test_queue_binds_iso_date_parts_to_unnamed_triplet() -> None:
    fields = [
        _node("", "ComboBox", 972, 440, w=54),
        _node("", "ComboBox", 1027, 440, w=132),
        _node("", "ComboBox", 1161, 440, w=68),
    ]
    record = _record({"Date of Birth": "1996-02-02"})
    queue = build_field_queue(_field_map(fields), record)
    group = queue.items[0]
    assert isinstance(group, DateGroupTarget)
    assert [t.value for t in group.targets] == ["02", "02", "1996"]
    assert group.date_value == "1996-02-02"


def test_queue_does_not_group_unnamed_combos_on_different_rows() -> None:
    fields = [
        _node("", "ComboBox", 300, 100, w=54),
        _node("", "ComboBox", 356, 100, w=132),
        _node("", "ComboBox", 490, 200, w=68),
    ]
    record = _record({"Date of Birth": "02-02-1996"})
    queue = build_field_queue(_field_map(fields), record)
    assert len(queue.items) == 3
    assert all(not isinstance(q, DateGroupTarget) for q in queue.items)


def test_queue_does_not_group_unnamed_combos_with_wide_gaps() -> None:
    fields = [
        _node("", "ComboBox", 300, 100, w=54),
        _node("", "ComboBox", 900, 100, w=132),
        _node("", "ComboBox", 1300, 100, w=68),
    ]
    record = _record({"Date of Birth": "02-02-1996"})
    queue = build_field_queue(_field_map(fields), record)
    assert len(queue.items) == 3


def test_date_group_bbox_is_union_of_parts() -> None:
    fields = [
        _node("Day", "ComboBox", 300, 100, w=40),
        _node("Month", "ComboBox", 342, 100, w=130),
        _node("Year", "ComboBox", 474, 100, w=70),
    ]
    queue = build_field_queue(_field_map(fields), _record({"DOB": "15-08-1990"}))
    group = queue.items[0]
    assert isinstance(group, DateGroupTarget)
    bbox = group.bbox
    assert bbox is not None
    assert bbox.left == 300 and bbox.top == 100
    assert bbox.right == 474 + 70
    assert bbox.height == 24


def test_bbox_for_id_resolves_sub_and_group() -> None:
    fields = [
        _node("Day", "ComboBox", 300, 100, w=40, handle=11),
        _node("Month", "ComboBox", 342, 100, w=130, handle=12),
        _node("Year", "ComboBox", 474, 100, w=70, handle=13),
    ]
    queue = build_field_queue(_field_map(fields), _record({"DOB": "15-08-1990"}))
    group = queue.items[0]
    assert isinstance(group, DateGroupTarget)
    assert queue.bbox_for_id(group.stable_id) is not None
    sub = group.targets[0]
    assert queue.bbox_for_id(sub.stable_id) == sub.bbox
    assert queue.bbox_for_id("h:999") is None
    assert queue.bbox_for_id(None) is None


def test_date_parts_reorders_iso() -> None:
    assert _date_parts("1996-02-02") == ["02", "02", "1996"]
    assert _date_parts("15-08-1990") == ["15", "08", "1990"]
    assert _date_parts("02 February 1996") == ["02", "February", "1996"]
    assert _date_parts("1996") == ["1996"]


def test_find_date_value_prefers_date_label() -> None:
    pairs = {"Name": "John", "Date of Birth": "1996-02-02", "Phone": "9001234567"}
    assert _find_date_value(pairs) == "1996-02-02"


def test_find_date_value_falls_back_to_any_date_value() -> None:
    pairs = {"Name": "John", "Phone": "900-123-4567", "Hired": "15-08-1990"}
    assert _find_date_value(pairs) == "15-08-1990"


def test_find_date_value_none_when_no_date() -> None:
    assert _find_date_value({"Name": "John"}) is None
    assert _find_date_value({}) is None


# -- PendingFieldQueue -------------------------------------------------------


def test_queue_remaining_all_ok_and_markers() -> None:
    queue = build_field_queue(_field_map([_node("A", "Edit", 300, 100)]), _record({"A": "x"}))
    assert queue.remaining == 1
    assert not queue.submit_ready()
    queue.mark_done(queue.items[0])
    assert queue.remaining == 0
    assert queue.all_ok()
    assert queue.submit_ready()

    queue2 = build_field_queue(_field_map([_node("B", "Edit", 300, 100)]), _record({"B": "y"}))
    queue2.mark_failed(queue2.items[0])
    assert queue2.remaining == 0
    assert not queue2.all_ok()
    assert queue2.submit_ready()


def test_refresh_positions_updates_bbox_by_stable_key() -> None:
    node = _node("Full Name", "Edit", 300, 100, handle=7)
    queue = build_field_queue(_field_map([node]), _record({"Full Name": "John"}))
    target = queue.items[0]
    moved = _node("Full Name", "Edit", 300, 640, handle=7)
    updated = queue.refresh_positions([moved])
    assert updated == 1
    assert target.bbox is not None and target.bbox.top == 640
    assert target.stable_id == "h:7"


def test_queue_keeps_value_less_fields_with_explicit_status() -> None:
    fields = [
        _node("Full Name", "Edit", 300, 100),
        _node("Middle Name", "Edit", 300, 200),
    ]
    mappings = [{"source": "Full Name", "target": "Full Name", "confidence": 0.9}]
    record = _record({"Full Name": "John"})
    queue = build_field_queue(_field_map(fields, mappings), record)
    assert len(queue.items) == 2
    assert queue.items[0].source_backed
    assert not queue.items[1].source_backed
    # value-less fields never block submit; only source-backed PENDING do.
    assert all(b.source_backed for b in queue.blockers())

    queue.mark_skipped(queue.items[1], FieldStatus.NO_SOURCE, "no source value for Middle Name")
    assert queue.items[1].status is FieldStatus.NO_SOURCE
    assert queue.items[1].status_reason == "no source value for Middle Name"
    assert queue.items[1] in queue.skipped_items


def test_merge_fields_appends_newly_discovered_targets() -> None:
    early = build_field_queue(_field_map([_node("Name", "Edit", 300, 100)]), _record({"Name": "John"}))
    assert len(early.items) == 1
    late = _node("District", "ComboBox", 300, 500)
    late.options = ["Karnataka"]
    added = early.merge_fields([late])
    assert added == 1
    assert len(early.items) == 2
    assert early.items[1].label == "District"
    assert early.merge_fields([late]) == 0  # no duplicates


def test_queue_and_refresh_exclude_transient_dropdown_options() -> None:
    fields = [
        _node("State", "ComboBox", 300, 100),
        _node("Karnataka", "ListItem", 300, 130),
    ]
    queue = build_field_queue(
        _field_map(fields, [{"source": "State", "target": "State"}]),
        _record({"State": "Karnataka"}),
    )
    assert [item.label for item in queue.items] == ["State"]

    option = _node("Tamil Nadu", "ListItem", 300, 130, handle=99)
    assert queue.merge_fields([fields[0], option]) == 0
    assert [item.label for item in queue.items] == ["State"]


def test_blockers_require_verified_or_already_correct() -> None:
    fields = [
        _node("Name", "Edit", 300, 100),
        _node("State", "ComboBox", 300, 200),
        _node("DOB", "Edit", 300, 300),
    ]
    mappings = [
        {"source": "Name", "target": "Name", "confidence": 0.9},
        {"source": "State", "target": "State", "confidence": 0.9},
    ]
    record = _record({"Name": "John", "State": "KA"})
    queue = build_field_queue(_field_map(fields, mappings), record)
    # DOB has no mapped source -> not source-backed.
    assert len(queue.items) == 3
    assert not queue.items[2].source_backed
    queue.mark_status(queue.items[0], FieldStatus.VERIFIED)
    queue.mark_status(queue.items[1], FieldStatus.FILLED, "written but UNKNOWN")
    assert len(queue.blockers()) == 1  # State is FILLED, not VERIFIED
    queue.mark_status(queue.items[1], FieldStatus.ALREADY_CORRECT)
    assert queue.blockers() == []
    queue.mark_status(queue.items[1], FieldStatus.FAILED, "boom")
    assert queue.blockers() == []  # failed is a distinct terminal state


def test_classify_fill_status() -> None:
    from atlas.act.models import ActionResult, Action

    def res(ok: bool, verified: bool, status: str | None) -> ActionResult:
        return ActionResult(
            action=Action(type=ActionType.TOGGLE, reason="x"),
            success=ok,
            verified=verified,
            verification_status=status,
        )

    assert classify_fill_status([res(True, True, "MATCH")]) is FieldStatus.VERIFIED
    assert classify_fill_status([res(True, False, "UNKNOWN")]) is FieldStatus.FILLED
    assert classify_fill_status([res(True, True, "ALREADY_CORRECT")]) is FieldStatus.ALREADY_CORRECT
    assert classify_fill_status([]) is FieldStatus.NOT_APPLICABLE


def test_mark_status_keeps_done_failed_in_sync() -> None:
    queue = build_field_queue(_field_map([_node("A", "Edit", 300, 100)]), _record({"A": "x"}))
    target = queue.items[0]
    queue.mark_status(target, FieldStatus.FAILED, "err")
    assert target.failed and not target.done
    assert queue.remaining == 0 and not queue.all_ok()
    queue.mark_status(target, FieldStatus.RETRY_PENDING, "retry")
    assert not target.failed and not target.done
    assert queue.remaining == 1
    queue.mark_done(target)
    assert target.done and target.status is FieldStatus.VERIFIED
    assert queue.all_ok()


# -- build_field_actions -----------------------------------------------------


def test_actions_for_textbox() -> None:
    fields = [_node("Name", "Edit", 300, 100)]
    mappings = [{"source": "Name", "target": "Name", "confidence": 0.9}]
    queue = build_field_queue(_field_map(fields, mappings), _record({"Name": "John"}))
    actions = build_field_actions(queue.items[0])
    assert [a.type for a in actions] == [ActionType.CLICK, ActionType.TYPE]
    assert actions[0].bbox is not None
    assert actions[1].value == "John"


def test_actions_for_combobox() -> None:
    node = _node("State", "ComboBox", 300, 100)
    node.options = ["Karnataka", "Tamil Nadu"]
    fields = [node]
    mappings = [{"source": "State", "target": "State", "confidence": 0.9}]
    queue = build_field_queue(_field_map(fields, mappings), _record({"State": "Karnataka"}))
    actions = build_field_actions(queue.items[0])
    assert [a.type for a in actions] == [ActionType.CLICK, ActionType.SELECT]
    assert actions[1].options == ["Karnataka", "Tamil Nadu"]


def test_actions_for_checkbox() -> None:
    fields = [_node("Agree", "CheckBox", 300, 100)]
    mappings = [{"source": "Agree", "target": "Agree", "confidence": 0.9}]
    queue = build_field_queue(_field_map(fields, mappings), _record({"Agree": "on"}))
    actions = build_field_actions(queue.items[0])
    assert [a.type for a in actions] == [ActionType.CLICK, ActionType.TOGGLE]


def test_actions_for_date_group() -> None:
    fields = [
        _node("Day", "ComboBox", 300, 100),
        _node("Month", "ComboBox", 520, 100),
        _node("Year", "ComboBox", 740, 100),
    ]
    mappings = [{"source": "DOB", "target": "Day", "confidence": 1.0}]
    queue = build_field_queue(_field_map(fields, mappings), _record({"DOB": "15-08-1990"}))
    actions = build_field_actions(queue.items[0])
    assert len(actions) == 6
    assert [a.type for a in actions] == [ActionType.CLICK, ActionType.SELECT] * 3
    assert actions[1].value == "15"
    assert actions[3].value == "08"
    assert actions[5].value == "1990"


def test_actions_empty_when_no_value() -> None:
    queue = build_field_queue(_field_map([_node("Name", "Edit", 300, 100)]), _record({}))
    assert build_field_actions(queue.items[0]) == []


# -- ScrollCapabilityCache ---------------------------------------------------


def _container(name: str, percent: float | None) -> ScrollContainer:
    return ScrollContainer(
        name=name,
        control_type="Pane",
        rect=BBox(300, 0, 400, 600),
        has_scroll_pattern=percent is not None,
        vertical_scroll_percent=percent,
        runtime_id=(1, 2, 3),
    )


def test_cache_prefers_pattern_then_dom_then_wheel() -> None:
    cache = ScrollCapabilityCache()
    pat = _container("right", 24.8)
    assert cache.method_for(pat) == SCROLL_METHOD_PATTERN
    plain = _container("right", None)
    assert cache.method_for(plain, dom_available=True) == SCROLL_METHOD_DOM
    assert cache.method_for(plain, dom_available=False) == SCROLL_METHOD_WHEEL


def test_cache_remembers_working_method() -> None:
    cache = ScrollCapabilityCache()
    container = _container("right", None)
    cache.remember(container, SCROLL_METHOD_WHEEL)
    assert cache.method_for(container) == SCROLL_METHOD_WHEEL


# -- TargetNavigator ---------------------------------------------------------


def test_navigator_clamps_scroll_amount() -> None:
    nav = TargetNavigator(min_px=120, max_px=700)
    client = (0, 0, 1000, 800)
    far = _node("X", "Edit", 300, 1500, h=30)
    assert nav.scroll_amount_for(SimpleNamespace(bbox=far.rect), client) == 700
    near = _node("Y", "Edit", 300, 100, h=30)
    assert nav.scroll_amount_for(SimpleNamespace(bbox=near.rect), client) == 120


def test_navigator_visibility() -> None:
    client = (0, 0, 1000, 800)
    inside = _node("A", "Edit", 300, 400)
    below = _node("B", "Edit", 300, 900)
    assert TargetNavigator.visible(SimpleNamespace(bbox=inside.rect), client)
    assert not TargetNavigator.visible(SimpleNamespace(bbox=below.rect), client)


def test_navigator_fillable_rejects_fold_hugging_field() -> None:
    # The MPF right panel's visible band ends at y=830; a fixed status bar
    # ("Record 114 of 114") sits at y=834-862. A field whose bottom just
    # crosses the band bottom must NOT be considered fillable.
    band = (826, 293, 1392, 830)
    clear = _node("A", "ComboBox", 972, 780, h=26)  # bottom 806 <= 822
    hugging = _node("B", "ComboBox", 972, 810, h=26)  # bottom 836 > 822
    under = _node("C", "ComboBox", 972, 848, h=26)  # bottom 874, in the footer zone
    assert TargetNavigator.fillable(SimpleNamespace(bbox=clear.rect), band)
    assert not TargetNavigator.fillable(SimpleNamespace(bbox=hugging.rect), band)
    assert not TargetNavigator.fillable(SimpleNamespace(bbox=under.rect), band)


def test_navigator_fillable_none_band_is_true() -> None:
    node = _node("A", "Edit", 300, 400)
    assert TargetNavigator.fillable(SimpleNamespace(bbox=node.rect), None)
    assert not TargetNavigator.fillable(SimpleNamespace(bbox=None), None)


# -- ProgressGuard / PerfTracker ---------------------------------------------


def test_progress_guard_expires() -> None:
    guard = ProgressGuard(timeout=0.05)
    guard.begin()
    assert not guard.expired
    time.sleep(0.08)
    assert guard.expired


def test_perf_tracker_totals_and_counts() -> None:
    perf = PerfTracker()
    perf.record("observe", 0.1)
    perf.record("observe", 0.2)
    perf.record("scroll", 0.3)
    totals = perf.totals()
    assert round(totals["observe"], 2) == 0.3
    assert round(totals["scroll"], 2) == 0.3
    assert perf.counts() == {"observe": 2, "scroll": 1}


def test_split_date_parts() -> None:
    assert split_date_parts("15-08-1990") == ["15", "08", "1990"]
    assert split_date_parts("12/31/2000") == ["12", "31", "2000"]
    assert split_date_parts(None) == []


# -- pick_left_right_containers regression -----------------------------------


def test_pick_containers_rejects_web_root_and_invalid_percent() -> None:
    web_root = ScrollContainer(
        name="RootWebArea",
        control_type="RootWebArea",
        rect=BBox(0, 0, 1024, 768),
        has_scroll_pattern=False,
        vertical_scroll_percent=-1.0,
    )
    left = ScrollContainer(
        name="left",
        control_type="Pane",
        rect=BBox(0, 0, 200, 600),
        has_scroll_pattern=True,
        vertical_scroll_percent=50.0,
    )
    right = ScrollContainer(
        name="right",
        control_type="Pane",
        rect=BBox(300, 0, 400, 600),
        has_scroll_pattern=True,
        vertical_scroll_percent=24.8,
    )
    chosen = pick_left_right_containers(
        [web_root, left, right],
        left_rect=BBox(0, 0, 200, 600),
        right_rect=BBox(300, 0, 400, 600),
        client_rect=(0, 0, 1024, 768),
    )
    assert web_root not in chosen.values()
    assert chosen[PANEL_LEFT].name == "left"
    assert chosen[PANEL_RIGHT].name == "right"


def test_pick_containers_rejects_full_client_wrapper() -> None:
    wrapper = ScrollContainer(
        name="wrapper",
        control_type="Pane",
        rect=BBox(0, 0, 1000, 800),
        has_scroll_pattern=True,
        vertical_scroll_percent=5.0,
    )
    chosen = pick_left_right_containers([wrapper], client_rect=(0, 0, 1024, 768))
    assert wrapper not in chosen.values()


# -- canonical TargetField model ------------------------------------------------


def test_fieldtarget_canonical_aliases_defaults() -> None:
    node = _node("Full Name", "Edit", 300, 100)
    target = FieldTarget(node=node, value="John Doe", ordinal=3)
    assert target.document_order == 3
    assert target.section == ""
    assert target.dependency_ids == ()
    assert target.current_value is None
    assert target.placeholder == ""


def test_fieldtarget_canonical_aliases_from_node() -> None:
    node = _node("State", "ComboBox", 300, 200)
    node.section = "Personal"
    node.dependencies = ["n:Edit:country_name:0"]
    node.value = "Karnataka"
    node.placeholder = "Select State"
    target = FieldTarget(node=node, value=None, ordinal=7)
    assert target.section == "Personal"
    assert target.dependency_ids == ("n:Edit:country_name:0",)
    assert target.current_value == "Karnataka"
    assert target.placeholder == "Select State"


# -- target inventory / anti-silent-skip ledger ---------------------------------


def test_field_coverage_summary_counts_all_targets() -> None:
    fields = [
        _node("Name", "Edit", 300, 100),
        _node("State", "ComboBox", 300, 200),
        _node("Not In Source", "Edit", 300, 300),
    ]
    queue = build_field_queue(_field_map(fields, mappings=[{"source": "Name", "target": "Name"}]), _record({"Name": "John"}))
    cov = field_coverage_summary(queue)
    assert cov["total_targets"] == 3
    assert cov["mapped_targets"] == 1
    assert cov["unmapped_targets"] == 2
    assert 0.0 < cov["mapped_pct"] < 1.0
    labels = {u["label"] for u in cov["unmapped"]}
    assert {"State", "Not In Source"} <= labels


def test_field_coverage_summary_keeps_status_on_unmapped() -> None:
    node = _node("State", "ComboBox", 300, 200)
    queue = build_field_queue(_field_map([node]), _record({"Name": "John"}))
    queue.items[0].status_reason = "no source pair for State"
    cov = field_coverage_summary(queue)
    entry = cov["unmapped"][0]
    assert entry["label"] == "State"
    assert entry["reason"] == "no source pair for State"
