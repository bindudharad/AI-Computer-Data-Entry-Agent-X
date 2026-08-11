"""Tests for semantic field mapping."""

from __future__ import annotations

from atlas.mapping.mapper import SemanticMapper, normalize_label
from atlas.understanding.fields import EditableField
from atlas.understanding.source import SourceRecord
from atlas.vision.models import BBox, ElementType, ScreenElement


def _field(element_id: str, label: str, type_: ElementType) -> EditableField:
    return EditableField(
        element=ScreenElement(
            element_id=element_id,
            type=type_,
            label=label,
            bbox=BBox(0, 0, 100, 20),
        ),
        offset=(0, 0),
    )


def _record(pairs: dict[str, str]) -> SourceRecord:
    return SourceRecord(pairs=pairs, ordered_labels=list(pairs))


def test_exact_match() -> None:
    mapper = SemanticMapper()
    record = _record({"Applicant Name": "Ravi Kumar"})
    fields = [_field("f0", "Applicant Name", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1
    mapping = result.mappings[0]
    assert mapping.target_id == "f0"
    assert mapping.source_value == "Ravi Kumar"
    assert mapping.method == "exact"
    assert mapping.confidence >= 0.9


def test_alias_match() -> None:
    mapper = SemanticMapper()
    record = _record({"DOB": "1990-05-15"})
    fields = [_field("f0", "Date of Birth", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1
    assert result.mappings[0].target_id == "f0"


def test_learned_alias_from_memory() -> None:
    mapper = SemanticMapper(aliases={"mob": "mobile number"})
    record = _record({"mob": "9876543210"})
    fields = [_field("f0", "Mobile Number", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1


def test_boolean_label_to_checkbox() -> None:
    mapper = SemanticMapper()
    record = _record({"Agree": "Yes"})
    fields = [_field("f0", "Agree", ElementType.CHECKBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1
    assert result.mappings[0].target.type == ElementType.CHECKBOX


def test_combobox_accepts_text_value() -> None:
    mapper = SemanticMapper()
    record = _record({"Gender": "Male"})
    fields = [_field("f0", "Gender", ElementType.COMBOBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1
    assert result.mappings[0].target_id == "f0"


def test_fuzzy_match() -> None:
    mapper = SemanticMapper(threshold=0.55)
    record = _record({"Employee Name": "Ravi"})
    fields = [_field("f0", "Name", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1
    assert result.mappings[0].method in {"token", "containment", "fuzzy"}


def test_cross_concept_fuzzy_rejected() -> None:
    """Two distinct known field concepts must never fuzzy-match."""
    mapper = SemanticMapper(threshold=0.3)
    record = _record({"Application No": "1001"})
    fields = [_field("f0", "PAN Number", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert result.mappings == []
    assert "Application No" in result.unmapped_source


def test_unmapped_source_reported() -> None:
    mapper = SemanticMapper()
    record = _record({"Application No": "1001", "Unrelated Thing": "x"})
    fields = [_field("f0", "Name", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert "Unrelated Thing" in result.unmapped_source


def test_unmatched_fields_reported() -> None:
    mapper = SemanticMapper()
    record = _record({"Name": "Ravi"})
    fields = [
        _field("f0", "Name", ElementType.TEXTBOX),
        _field("f1", "Pan Number", ElementType.TEXTBOX),
    ]
    result = mapper.map(record, fields)
    assert result.unmatched_fields[0].element_id == "f1"


def test_one_target_per_source() -> None:
    mapper = SemanticMapper()
    record = _record({"Name": "Ravi", "Full Name": "Ravi Kumar"})
    fields = [
        _field("f0", "Name", ElementType.TEXTBOX),
        _field("f1", "Full Name", ElementType.TEXTBOX),
    ]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 2
    used = {m.target_id for m in result.mappings}
    assert used == {"f0", "f1"}


def test_normalize_label() -> None:
    assert normalize_label("  Date  of-Birth! ") == "date of birth"
    assert normalize_label("Pincode:") == "pincode"


def test_date_value_never_maps_to_non_date_field() -> None:
    """The DOB->District class of data-integrity bugs must be blocked."""
    mapper = SemanticMapper()
    record = _record({"DOB": "02/02/1996"})
    fields = [_field("f0", "District", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert result.mappings == []
    assert "DOB" in result.unmapped_source
    assert any(b[2] == "value-type" for b in result.blocked)


def test_phone_value_never_maps_to_name_field() -> None:
    mapper = SemanticMapper()
    record = _record({"Mobile No": "9876543210"})
    fields = [_field("f0", "Name", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert result.mappings == []
    assert any(b[2] == "value-type" for b in result.blocked)


def test_date_value_maps_to_date_field() -> None:
    mapper = SemanticMapper()
    record = _record({"DOB": "02/02/1996"})
    fields = [_field("f0", "Date of Birth", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 1
    assert result.mappings[0].target_id == "f0"


def test_numeric_value_never_maps_to_name_field() -> None:
    mapper = SemanticMapper()
    record = _record({"Application No": "1001"})
    fields = [_field("f0", "Applicant Name", ElementType.TEXTBOX)]
    result = mapper.map(record, fields)
    assert result.mappings == []
    assert "Application No" in result.unmapped_source


def test_stronger_source_wins_when_two_sources_share_a_target() -> None:
    mapper = SemanticMapper()
    record = _record({"Name": "ANITA", "Full Name": "ANITA K"})
    fields = [
        _field("f0", "Name", ElementType.TEXTBOX),
        _field("f1", "Full Name", ElementType.TEXTBOX),
    ]
    result = mapper.map(record, fields)
    assert len(result.mappings) == 2
    binding = {m.source_label: m.target_id for m in result.mappings}
    assert binding == {"Name": "f0", "Full Name": "f1"}


def test_blocked_pairings_surface_in_result() -> None:
    mapper = SemanticMapper()
    record = _record({"DOB": "02/02/1996"})
    fields = [
        _field("f0", "District", ElementType.TEXTBOX),
        _field("f1", "Gender", ElementType.TEXTBOX),
    ]
    result = mapper.map(record, fields)
    assert result.mappings == []
    assert len(result.blocked) == 2
    assert all(b[2] == "value-type" for b in result.blocked)
