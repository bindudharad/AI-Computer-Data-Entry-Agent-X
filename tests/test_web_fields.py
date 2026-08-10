"""Unit tests for the web form field helpers."""

from __future__ import annotations

from atlas.vision.models import ElementType
from atlas.web.fields import build_locator, field_fingerprint_js, normalize_label, rank_methods_for


def test_normalize_label() -> None:
    assert normalize_label("  Applicant Name : ") == "applicant name"
    assert normalize_label("Date of Birth*") == "date of birth"
    assert normalize_label("") == ""


def test_field_fingerprint_js_is_callable_expression() -> None:
    js = field_fingerprint_js().strip()
    assert js.startswith("() =>")
    assert "fields" in js
    assert "labels" in js


def test_build_locator_by_id() -> None:
    assert build_locator({"id": "f-name", "tag": "input", "type": "text"}) == '#f-name[type="text"]'


def test_build_locator_by_name() -> None:
    assert build_locator({"name": "gender", "tag": "select"}) == '[name="gender"]'


def test_build_locator_by_aria_label() -> None:
    assert build_locator({"aria": "Applicant Name", "tag": "input"}) == '[aria-label="Applicant Name"]'


def test_build_locator_by_placeholder() -> None:
    assert build_locator({"placeholder": "Enter name", "tag": "input"}) == '[placeholder="Enter name"]'


def test_build_locator_empty_when_no_identifiers() -> None:
    assert build_locator({}) == ""


def test_build_locator_escapes_quotes() -> None:
    locator = build_locator({"aria": 'Say "hi"', "tag": "input"})
    assert '\\"' in locator


def test_rank_methods_for_textbox() -> None:
    assert rank_methods_for(ElementType.TEXTBOX)[0] == "dom"


def test_rank_methods_for_combobox() -> None:
    assert rank_methods_for(ElementType.COMBOBOX)[0] == "select_option"


def test_rank_methods_for_checkbox() -> None:
    assert rank_methods_for(ElementType.CHECKBOX)[0] == "click"


def test_rank_methods_for_unknown_falls_back_to_uia() -> None:
    assert rank_methods_for(ElementType.UNKNOWN)[0] == "uia"
