"""Collapsed-section and upload-area detection.

Enterprise forms frequently hide uploads behind an accordion, tab or a group
header labelled e.g. "Upload Details". The agent expands these areas *before*
filling or scrolling so their fields are never skipped. Detection is purely
linguistic + geometric: a clickable element whose label fuzzy-matches upload
vocabulary is treated as an expandable section header unless it is the final
submit button (which the workflow clicks once at the end instead).
"""

from __future__ import annotations

from atlas.vision.models import ElementType, SceneDescription, ScreenElement

#: Vocabulary any of whose tokens marks a region as an upload / attachment area.
_UPLOAD_TOKENS = {
    "upload", "uploads", "uploaded", "attachment", "attachments", "attach",
    "document", "documents", "file", "files", "certificate", "certificates",
    "annexure", "annexures", "evidence", "proof", "supporting", "supporting_document",
}

#: Words that suggest a collapsible container rather than a plain action button.
_EXPANDABLE_HINTS = {
    "section", "details", "more", "show", "expand", "open", "view", "list",
}

#: Element types that can act as a clickable section header.
_CLICKABLE_TYPES = {ElementType.BUTTON, ElementType.TAB, ElementType.SECTION}

#: A region smaller than this is too small to be a meaningful section header.
_MIN_REGION_SIZE = 20


def normalize_label(text: str) -> str:
    """Lowercase and collapse whitespace, dropping punctuation/decoration."""
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text or "")
    return " ".join(cleaned.split()).lower()


def section_match_score(text: str) -> float:
    """0..1 how strongly ``text`` reads as an upload/attachment area.

    Whole-label membership is strongest, token matches second, single-word
    hits third. ``None``/empty always scores 0.
    """
    if not text:
        return 0.0
    normalized = normalize_label(text)
    if not normalized:
        return 0.0
    if normalized in _UPLOAD_TOKENS:
        return 1.0
    tokens = set(normalized.split())
    if not tokens:
        return 0.0
    hits = tokens & _UPLOAD_TOKENS
    if not hits:
        return 0.0
    # "upload details" -> strong; "submit my document" -> weaker but still a hit.
    if any(h in _EXPANDABLE_HINTS or len(h) >= 7 for h in hits):
        return 0.8
    return 0.5


def is_expandable_section(element: ScreenElement) -> bool:
    """True for a clickable element whose label reads as an upload area."""
    if element.bbox is None:
        return False
    if element.bbox.width < _MIN_REGION_SIZE or element.bbox.height < _MIN_REGION_SIZE:
        return False
    if element.type not in _CLICKABLE_TYPES and element.type != ElementType.UNKNOWN:
        return False
    return section_match_score(element.label or element.name) >= 0.5


def find_upload_sections(
    scene: SceneDescription,
    exclude_ids: frozenset[str] | set[str] = frozenset(),
) -> list[ScreenElement]:
    """Every expandable upload/attachment region in the current scene.

    Returns elements ordered by confidence, then top-to-bottom. Already-handled
    element ids in ``exclude_ids`` are skipped so a region is never expanded
    twice.
    """
    candidates = [
        e for e in scene.elements
        if is_expandable_section(e) and e.element_id not in exclude_ids
    ]
    candidates.sort(
        key=lambda e: (-section_match_score(e.label or e.name), e.bbox.top if e.bbox else 0)
    )
    return candidates


__all__ = [
    "find_upload_sections",
    "is_expandable_section",
    "normalize_label",
    "section_match_score",
]
