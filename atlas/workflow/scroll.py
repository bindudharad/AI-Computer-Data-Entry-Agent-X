"""Dual-panel scroll reasoning for a split form.

A split (MPF-style) form is two independent scrollable panels that belong to the
same logical document:

* LEFT panel  - the source / reference data list.
* RIGHT panel - the entry form with the input controls.

A human operator reads the left list while the right form advances, and keeps
both sides scrolling in lockstep until the whole form (down to the Upload
Details section and the submit button) has been processed. This module is the
agent's mental model of that: it reasons about WHERE to scroll, HOW MUCH, when a
panel has genuinely reached its bottom, and when the two panels have fallen out
of sync. It performs NO input itself - the workflow loop turns its decisions
into clicks and wheel events.

The controller never assumes the form is complete just because the current
viewport has no visible controls. A panel is only declared at its bottom after
its visible content stops changing across ``stall_limit`` consecutive scrolls,
and the scan is complete only when BOTH panels have reached their bottom
(optionally confirmed by the Upload Details section becoming visible).
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas.observe.uia import ScrollContainer
from atlas.reason.sections import find_upload_sections
from atlas.vision.models import BBox, SceneDescription

PANEL_LEFT = "left"
PANEL_RIGHT = "right"

#: A wheel notch moves roughly this many pixels of form content.
_NOTCH_PIXELS = 50

#: Hard bounds on the incremental scroll amount (never a page jump).
_MIN_NOTCHES = 3
_MAX_NOTCHES = 8


@dataclass
class PanelScrollState:
    """Per-panel scroll progress and bottom detection."""

    name: str
    rect: BBox | None = None
    scroll_position: int = 0
    last_signature: str | None = None
    stall: int = 0
    at_bottom: bool = False
    moved: bool = False
    ever_moved: bool = False
    last_delta: int = 0
    container: ScrollContainer | None = None
    more_content: bool | None = None

    def reset(self) -> None:
        self.scroll_position = 0
        self.last_signature = None
        self.stall = 0
        self.at_bottom = False
        self.moved = False
        self.ever_moved = False
        self.last_delta = 0
        self.container = None
        self.more_content = None

    @property
    def known(self) -> bool:
        return self.rect is not None


class DualPanelScroll:
    """Tracks the LEFT (source) and RIGHT (entry) panels of a split form.

    Responsibilities:

    * ``update_panels``  - refresh each panel's visible region (clamped to the
      client area so the cursor never lands below the fold).
    * ``record_observation`` - after every fresh observation, decide whether
      each panel moved, stalled, or reached its bottom, and whether the Upload
      Details section is now visible.
    * ``scroll_anchor``  - a safe point INSIDE a panel to click-focus it before
      the wheel scroll (never a random screen position, never the whole window).
    * ``scroll_notches`` - the small incremental amount (targeting 250-350 px)
      with self-correction from the last measured movement.
    * ``lagging_panel`` - which side is behind so the loop can re-synchronize.
    """

    def __init__(
        self,
        stall_limit: int = 3,
        min_pixels: int = 250,
        max_pixels: int = 350,
        settle_range: tuple[float, float] = (0.3, 0.5),
    ) -> None:
        self.left = PanelScrollState(PANEL_LEFT)
        self.right = PanelScrollState(PANEL_RIGHT)
        self._stall_limit = max(1, stall_limit)
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels
        self.settle_range = settle_range
        self.upload_visible = False
        self._expects_upload = False
        self._prev_measure: dict[str, dict[str, int]] = {}
        self._last_scene: SceneDescription | None = None
        #: The reveal scan is DOWN-only until the Upload Details section is seen.
        #: A failed scroll is retried with a bigger distance or the next method,
        #: never by scrolling back up (``NEVER REVERSE SCROLL``).
        self.direction = "down"

    # -- panel access --------------------------------------------------------

    @property
    def panels(self) -> list[PanelScrollState]:
        return [self.left, self.right]

    def panel(self, name: str) -> PanelScrollState:
        return self.left if name == PANEL_LEFT else self.right

    def known_panels(self) -> list[PanelScrollState]:
        """Panels with a located region (the ones the loop can scroll)."""
        return [p for p in self.panels if p.known]

    def reset(self) -> None:
        for panel in self.panels:
            panel.reset()
        self.upload_visible = False
        self._expects_upload = False
        self.direction = "down"
        self._prev_measure.clear()
        self._last_scene = None

    # -- geometry ------------------------------------------------------------

    def update_panels(
        self,
        rects: dict[str, BBox | None],
        client: tuple[int, int, int, int] | None,
        containers: dict[str, ScrollContainer | None] | None = None,
    ) -> None:
        """Refresh panel regions from the field map / provider, clipped to the
        visible client area (so every scroll anchor stays on-screen).

        When a real UIA scroll container is provided for a panel its own rect
        is used as the scroll target and its ``more_content`` flag gates bottom
        detection (a container that still reports content below is never
        declared finished just because the visible signature stopped moving).
        """
        for name, panel in ((PANEL_LEFT, self.left), (PANEL_RIGHT, self.right)):
            container = (containers or {}).get(name)
            panel.container = container
            if container is not None:
                panel.rect = self._clamp_to_client(container.rect, client)
                # Once a container proves it is at its maximum (percent ~100,
                # e.g. after a DOM scroll reached the bottom) that is sticky:
                # the per-round re-discovery rebuilds containers with an
                # "unknown" percent and must not un-prove the bottom.
                if panel.more_content is not False:
                    panel.more_content = container.more_content
            else:
                panel.rect = self._clamp_to_client(rects.get(name), client)
                panel.more_content = None
            if panel.rect is None:
                panel.container = None

    @staticmethod
    def _clamp_to_client(
        rect: BBox | None,
        client: tuple[int, int, int, int] | None,
    ) -> BBox | None:
        if rect is None or rect.width <= 0 or rect.height <= 0:
            return None
        if client is None:
            return rect
        left, top, right, bottom = client
        ix = max(rect.left, left)
        iy = max(rect.top, top)
        ix2 = min(rect.right, right)
        iy2 = min(rect.bottom, bottom)
        if ix2 <= ix or iy2 <= iy:
            return None
        return BBox(ix, iy, ix2 - ix, iy2 - iy)

    # -- observation / bottom detection --------------------------------------

    @staticmethod
    def _in_panel_x(bbox: BBox, rect: BBox) -> bool:
        return bbox.right > rect.left and bbox.left < rect.right

    def panel_signature(
        self, scene: SceneDescription, panel: PanelScrollState, include_labels: bool = False
    ) -> str:
        """Order-independent snapshot of the content visible inside a panel.

        When a panel's region is unknown (no field map), the whole scene is
        used so the scan still detects content changes. With ``include_labels``
        the visible labels are part of the signature too - this is the
        "screenshot + OCR labels" check used to verify that a scroll actually
        moved the panel (the same labels still visible == the scroll failed).
        """
        parts = []
        for element in scene.elements:
            if element.bbox is None:
                continue
            if panel.rect is not None and not self._in_panel_x(element.bbox, panel.rect):
                continue
            b = element.bbox
            label = ""
            if include_labels:
                label = f":{element.label or element.name or ''}"
            parts.append(
                f"{element.element_id}:{element.type.value}{label}:"
                f"{b.left},{b.top},{b.width},{b.height}"
            )
        return "|".join(sorted(parts))

    def _measure_movement(self, scene: SceneDescription) -> None:
        """Measure how far each panel's matched content moved (px, downward+).

        Elements are matched by stable element_id across consecutive
        observations; the delta is the average top-edge change. Used to keep
        the two panels synchronized and to self-correct the scroll amount.
        """
        for panel in self.panels:
            current: dict[str, int] = {}
            for element in scene.elements:
                if element.bbox is None:
                    continue
                if panel.rect is not None and not self._in_panel_x(element.bbox, panel.rect):
                    continue
                current[element.element_id] = element.bbox.top
            previous = self._prev_measure.get(panel.name, {})
            if previous:
                common = set(current) & set(previous)
                deltas = [previous[eid] - current[eid] for eid in common]
                if deltas:
                    panel.last_delta = int(sum(deltas) / len(deltas))
            self._prev_measure[panel.name] = current

    def record_observation(self, scene: SceneDescription) -> None:
        """Update per-panel progress after a fresh observation.

        A panel whose visible content did not change since the last
        observation has stalled; after ``stall_limit`` consecutive stalls it is
        declared at its bottom - but ONLY when there is proof it can actually
        move: either it moved at least once (``ever_moved``) or its scroll
        container reports it is definitively at its maximum. A panel whose
        scrolls never moved anything (the MPF panels expose no ScrollPattern)
        is NEVER declared done: declaring it done would let the loop stop and
        submit a half-scrolled form. ``upload_visible`` is sticky: once the
        Upload Details section is seen it stays true (it may later scroll
        off-screen).
        """
        for panel in self.panels:
            signature = self.panel_signature(scene, panel)
            moved = panel.last_signature is not None and signature != panel.last_signature
            panel.moved = moved
            if moved:
                panel.ever_moved = True
                panel.stall = 0
            elif panel.last_signature is not None:
                panel.stall += 1
            panel.last_signature = signature
            # A real scroll container that still reports content below the fold
            # (vertical scroll percent < 100) must never be declared at its
            # bottom just because the visible signature stopped changing - the
            # scroll may simply have failed (the MPF panels expose no
            # ScrollPattern, so a failing scroll stalls forever). Only when it
            # is truly at its max, or proven to move, is the stall rule allowed
            # to finish the panel. A panel with NO container (single-viewport
            # web forms) keeps the plain stall heuristic: there is no evidence
            # of content below, so a static viewport really is the bottom.
            content_done = panel.more_content is not True
            proven_movable = (
                panel.container is None
                or panel.more_content is False
                or panel.ever_moved
            )
            if panel.stall >= self._stall_limit and content_done and proven_movable:
                panel.at_bottom = True
        self._measure_movement(scene)
        if find_upload_sections(scene):
            self._expects_upload = True
            self.upload_visible = True
        self._last_scene = scene

    # -- completion / synchronization ----------------------------------------

    def needs_scroll(self, name: str) -> bool:
        """A panel that can still move: not yet at its bottom."""
        return not self.panel(name).at_bottom

    def both_at_bottom(self) -> bool:
        """The scan is over: BOTH panels have reached their bottom.

        A viewport being complete is NOT enough - the loop must keep scrolling
        until neither side has any content left to reveal.
        """
        return self.left.at_bottom and self.right.at_bottom

    def form_complete(self) -> bool:
        """The whole form may be submitted: both panels are at their bottom.

        When the form is known to contain an Upload Details section, it must
        have been reached (visible) before the scan is allowed to finish - the
        spec's stop condition is "Upload Details visible AND both panels reached
        their bottom AND no further scrolling is possible". A form whose scroll
        never moved any content is NOT complete - submitting it would save a
        half-filled record.
        """
        if not self.both_at_bottom():
            return False
        if self._expects_upload and not self.upload_visible:
            return False
        return True

    def any_panel_at_bottom(self) -> bool:
        return self.left.at_bottom or self.right.at_bottom

    def lagging_panel(self) -> str | None:
        """The panel that moved this round while its partner did not.

        Returns ``None`` when both moved, neither moved, or the stalled panel is
        already at its bottom. The loop re-scrolls the returned panel alone so
        the left source list and right entry form stay in lockstep.
        """
        if self.left.moved and not self.right.moved and not self.right.at_bottom:
            return PANEL_RIGHT
        if self.right.moved and not self.left.moved and not self.left.at_bottom:
            return PANEL_LEFT
        return None

    def completion_reason(self) -> str:
        """Human-readable confirmation used when both panels are at their bottom."""
        if self._expects_upload and not self.upload_visible:
            return "both panels at their bottom but Upload Details never became visible"
        if self.upload_visible:
            return "Upload Details visible and both panels at their bottom"
        return "both panels at their bottom (no upload section found)"

    # -- scroll geometry -----------------------------------------------------

    def scroll_anchor(self, name: str, scene: SceneDescription) -> tuple[int, int] | None:
        """A safe point INSIDE the panel to click-focus before scrolling.

        Prefers the center of a non-editable element (a label / section header)
        so the focus click never toggles a checkbox, radio or other control.
        Falls back to the panel's top strip (header / margin area). Never a
        random position and never the whole window.
        """
        panel = self.panel(name)
        if panel.rect is None:
            return None
        for element in sorted(
            scene.elements,
            key=lambda e: (
                e.bbox.top if e.bbox is not None else 10**9,
                e.bbox.left if e.bbox is not None else 10**9,
            ),
        ):
            if element.bbox is None or element.editable:
                continue
            if not self._in_panel_x(element.bbox, panel.rect):
                continue
            if element.bbox.top >= panel.rect.bottom or element.bbox.bottom <= panel.rect.top:
                continue
            x, y = element.bbox.center
            return int(x), int(y)
        cx = panel.rect.left + panel.rect.width // 2
        cy = panel.rect.top + min(12, max(4, panel.rect.height // 6))
        return int(cx), int(cy)

    def scroll_notches(self, scene: SceneDescription) -> int:
        """Wheel notches for one small incremental scroll (target 250-350 px).

        Density-adaptive like the previous heuristic, then self-corrected from
        the last measured movement so the next scroll lands in the 250-350 px
        band instead of jumping or crawling. Never a page-height jump.
        """
        base = self._density_notches(scene)
        target = (self.min_pixels + self.max_pixels) // 2
        measured = self.right.last_delta or self.left.last_delta
        if measured > 0:
            px_per_notch = measured / max(1, base)
            if px_per_notch > 0:
                base = round(target / px_per_notch)
        return max(_MIN_NOTCHES, min(_MAX_NOTCHES, base))

    @staticmethod
    def _density_notches(scene: SceneDescription) -> int:
        """Baseline notches from the field density of the current viewport."""
        tops = sorted(
            e.bbox.top for e in scene.elements if e.bbox is not None and e.editable
        )
        gaps = [b - a for a, b in zip(tops, tops[1:], strict=False) if b - a > 0]
        avg_gap = (sum(gaps) / len(gaps)) if gaps else 40.0
        if avg_gap < 30:
            return 5  # ~250px - dense grid
        if avg_gap < 50:
            return 6  # ~300px
        return 7      # ~350px - sparse form (never more)

    # -- debug ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "upload_visible": self.upload_visible,
            "expects_upload": self._expects_upload,
            "direction": self.direction,
            "min_pixels": self.min_pixels,
            "max_pixels": self.max_pixels,
            "stall_limit": self._stall_limit,
            "panels": {
                p.name: {
                    "rect": list(p.rect.to_dict().values()) if p.rect is not None else None,
                    "scroll_position": p.scroll_position,
                    "stall": p.stall,
                    "at_bottom": p.at_bottom,
                    "moved": p.moved,
                    "last_delta": p.last_delta,
                    "more_content": p.more_content,
                    "container": p.container.to_dict() if p.container is not None else None,
                }
                for p in self.panels
            },
        }


__all__ = [
    "DualPanelScroll",
    "PanelScrollState",
    "PANEL_LEFT",
    "PANEL_RIGHT",
]
