"""Tests for value-shape repair (Phase 2), target-verifier date parity
(Phase 3) and blocked-field reporting (Phase 5) of the BEST++ migration."""

from __future__ import annotations

from atlas.act.models import ActionType
from atlas.act.verify import TargetFieldVerifier
from atlas.mapping.mapper import FieldMapping, MappingResult
from atlas.reason.planner import ActionPlanner
from atlas.understanding.fields import EditableField
from atlas.understanding.source import SourceRecord
from atlas.understanding.value_shape import (
    label_kind,
    repair_value,
    value_kind,
    value_ok,
)
from atlas.vision.models import BBox, ElementType, SceneDescription, ScreenElement
from atlas.workflow.loop import RecordResult, WorkflowSummary


def _field(element_id: str, label: str, type_: ElementType, bbox: BBox) -> EditableField:
    return EditableField(
        element=ScreenElement(element_id=element_id, type=type_, label=label, bbox=bbox),
        offset=(0, 0),
    )


def _scene() -> SceneDescription:
    return SceneDescription(elements=[
        ScreenElement(element_id="f0", type=ElementType.TEXTBOX, label="Name", bbox=BBox(0, 0, 100, 20)),
        ScreenElement(element_id="b0", type=ElementType.BUTTON, label="Save", bbox=BBox(0, 100, 60, 24)),
    ])


# -- kind inference -----------------------------------------------------------

def test_label_kind_inference() -> None:
    assert label_kind("Date of Birth") == "date"
    assert label_kind("Mobile No") == "phone"
    assert label_kind("Pincode") == "pincode"
    assert label_kind("Age") == "numeric"
    assert label_kind("Applicant Name") == "name"
    assert label_kind("District") == "text"


def test_value_kind_inference() -> None:
    assert value_kind("a@b.com") == "email"
    assert value_kind("02/02/1996") == "date"
    assert value_kind("+91 98765 43210") == "phone"
    assert value_kind("25") == "numeric"
    assert value_kind("Ravi Kumar") == "text"


# -- value-type gating (Phase 1, re-exported) ---------------------------------

def test_value_ok_blocks_wrong_kind() -> None:
    assert value_ok("District", "02/02/1996") is False
    assert value_ok("Applicant Name", "9876543210") is False
    assert value_ok("Father's Name", "Ravi") is True
    assert value_ok("Date of Birth", "02/02/1996") is True
    assert value_ok("Pincode", "560001") is True


# -- value-shape repair (Phase 2) ---------------------------------------------

def test_repair_pincode_spacing() -> None:
    assert repair_value("Pincode", "560 001") == "560001"
    assert repair_value("PIN Code", "560-001") == "560001"


def test_repair_phone() -> None:
    # Country code digits are preserved: only separators/spaces are stripped.
    assert repair_value("Mobile No", "+91 98765 43210") == "919876543210"
    assert repair_value("Mobile No", "98765-43210") == "9876543210"


def test_repair_iso_date() -> None:
    assert repair_value("Date of Birth", "1996-02-02") == "02/02/1996"
    assert repair_value("DOB", "1996/2/2") == "02/02/1996"


def test_repair_numeric_spacing() -> None:
    assert repair_value("Age", "2 5") == "25"


def test_repair_text_unchanged() -> None:
    assert repair_value("Applicant Name", "Ravi Kumar") == "Ravi Kumar"
    assert repair_value("District", "12345") == "12345"


def test_repair_empty_or_unknown_unchanged() -> None:
    assert repair_value("Mobile No", "") == ""
    assert repair_value("Remarks", "1996-02-02") == "1996-02-02"


def test_planner_types_repaired_value() -> None:
    planner = ActionPlanner(verify_after_action=True)
    record = SourceRecord(pairs={"Pincode": "560 001"}, ordered_labels=["Pincode"])
    field = _field("f0", "Pincode", ElementType.TEXTBOX, BBox(0, 0, 100, 20))
    mapping = MappingResult(mappings=[FieldMapping("Pincode", "560 001", field, 0.98, "exact")])
    plan = planner.plan_fill(record, mapping, _scene(), "b0")
    typed = [a for a in plan.actions if a.type == ActionType.TYPE]
    assert len(typed) == 1
    assert typed[0].value == "560001"
    assert typed[0].expected == "560001"


def test_planner_does_not_repair_dropdown() -> None:
    planner = ActionPlanner(verify_after_action=True)
    record = SourceRecord(pairs={"Preferred Contact": "Mobile"}, ordered_labels=["Preferred Contact"])
    field = _field("f0", "Preferred Contact", ElementType.COMBOBOX, BBox(0, 0, 100, 20))
    mapping = MappingResult(mappings=[FieldMapping("Preferred Contact", "Mobile", field, 0.98, "exact")])
    plan = planner.plan_fill(record, mapping, _scene(), "b0")
    select = [a for a in plan.actions if a.type == ActionType.SELECT]
    assert len(select) == 1
    assert select[0].value == "Mobile"


# -- deterministic verification (Phase 3) -------------------------------------

def test_target_verifier_date_parity() -> None:
    """Web DOM read-back must accept a date spelling different from the source's."""
    verifier = TargetFieldVerifier(get_value=lambda _: None)
    ok, evidence = verifier._compare("02/02/1996", "1996-02-02")
    assert ok, evidence
    ok, evidence = verifier._compare("02-02-1996", "1996/02/02")
    assert ok, evidence


def test_target_verifier_plain_value() -> None:
    verifier = TargetFieldVerifier(get_value=lambda _: None)
    ok, _ = verifier._compare("560001", "560001")
    assert ok
    ok, _ = verifier._compare("560002", "560001")
    assert not ok


# -- reporting (Phase 5) ------------------------------------------------------

def test_workflow_summary_blocked_fields() -> None:
    record = SourceRecord(pairs={"DOB": "02/02/1996"}, ordered_labels=["DOB"])
    m1 = MappingResult(blocked=[("DOB", "District", "value-type")])
    m2 = MappingResult(blocked=[("Mobile", "Name", "value-type")])
    summary = WorkflowSummary(records=[
        RecordResult(index=1, record=record, mapping=m1, success=False),
        RecordResult(index=2, record=record, mapping=m2, success=False),
    ])
    assert len(summary.blocked_fields) == 2
    d = summary.to_dict()
    assert d["blocked_fields"] == [["DOB", "District", "value-type"], ["Mobile", "Name", "value-type"]]
