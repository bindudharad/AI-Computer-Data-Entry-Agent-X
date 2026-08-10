"""Tests for upload/attachment section detection."""

from __future__ import annotations

from atlas.reason.sections import (
    find_upload_sections,
    is_expandable_section,
    section_match_score,
)
from atlas.vision.models import BBox, ElementType, SceneDescription, ScreenElement


def _element(element_id: str, type: ElementType, label: str, box: BBox, section: str | None = None) -> ScreenElement:
    return ScreenElement(
        element_id=element_id, type=type, label=label, bbox=box, section=section,
    )


def test_section_match_scores() -> None:
    assert section_match_score("Upload Details") >= 0.5
    assert section_match_score("Upload Documents") >= 0.8
    assert section_match_score("Certificates") == 1.0
    assert section_match_score("") == 0.0
    assert section_match_score("Save") == 0.0
    assert section_match_score("Next") == 0.0
    assert section_match_score("Contact Info") == 0.0


def test_upload_sections_exclude_terminal_buttons() -> None:
    scene = SceneDescription(elements=[
        _element("sec", ElementType.BUTTON, "Upload Details", BBox(10, 10, 200, 30)),
        _element("save", ElementType.BUTTON, "Save", BBox(10, 120, 60, 24)),
        _element("tab", ElementType.TAB, "Documents", BBox(10, 60, 100, 24)),
        _element("lbl", ElementType.LABEL, "Notes", BBox(10, 90, 100, 20)),
    ])
    found = {e.element_id for e in find_upload_sections(scene)}
    assert found == {"sec", "tab"}


def test_upload_sections_respect_exclusions() -> None:
    scene = SceneDescription(elements=[
        _element("sec", ElementType.BUTTON, "Upload Details", BBox(10, 10, 200, 30)),
    ])
    found = find_upload_sections(scene, exclude_ids={"sec"})
    assert found == []


def test_small_regions_not_expandable() -> None:
    small = _element("tiny", ElementType.BUTTON, "Upload", BBox(10, 10, 5, 5))
    assert is_expandable_section(small) is False
