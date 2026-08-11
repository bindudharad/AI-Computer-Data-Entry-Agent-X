"""Scroll-container discovery and UIA-based panel scrolling for a split form.

The outer application window never scrolls - only internal panels do. A split
(MPF-style) form has two such panels:

* LEFT panel  - the source / reference data list.
* RIGHT panel - the entry form with the input controls.

This module turns raw UIA discovery (``atlas.observe.uia``) into a human-like
scrolling engine. It:

* picks the two real scroll containers out of every discovered one
  (``pick_left_right_containers``), never trusting hardcoded coordinates;
* scrolls a container *directly* through its own ScrollPattern
  (``PanelScroller``), escalating through fallback methods when a scroll
  produces no change; and
* carries a per-record ``ScrollSession`` holding the discovered containers and
  the engine used to move them.

Nothing here emits wheel events over the window/desktop at random positions.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from atlas.observe.uia import ScrollContainer, UiaBackend
from atlas.vision.models import BBox
from atlas.workflow.scroll import PANEL_LEFT, PANEL_RIGHT

#: Scroll methods, in the order ``PanelScroller`` escalates through when a scroll
#: produces no change. DOM scrollTop mutation is primary for Chrome-hosted forms
#: (the MPF app exposes no UIA ScrollPattern); UIA ScrollPattern is the spec's
#: Method 1 for native controls, the click-focus + mouse-wheel is Method 2, and
#: the scrollbar-thumb drag is Method 3. A method is abandoned only when it
#: produced no change at the largest attempt size - the engine never stops after
#: one failed scroll and never reverses direction.
SCROLL_METHOD_DOM = "dom"
SCROLL_METHOD_PATTERN = "pattern"
SCROLL_METHOD_WHEEL = "wheel"
SCROLL_METHOD_SCROLLBAR_DRAG = "scrollbar-drag"
SCROLL_METHOD_KEYBOARD = "keyboard"
SCROLL_METHOD_OVERRIDE = "plugin-override"

#: Effective escalation order used by ``PanelScroller.scroll_down``. DOM comes
#: first because Chrome-hosted forms (e.g. MPF) expose no ScrollPattern at
#: all, so trying it first would waste a whole retry ladder on every single
#: scroll of the whole run. Keyboard (PageDown / ArrowDown) and the plugin
#: override are the last two resorts: keyboard because most native controls
#: already respond to the pattern/wheel/drag methods, and the plugin override
#: last of all because it is application-specific and only reached when every
#: generic method has failed.
SCROLL_METHOD_ORDER: tuple[str, ...] = (
    SCROLL_METHOD_DOM,
    SCROLL_METHOD_PATTERN,
    SCROLL_METHOD_WHEEL,
    SCROLL_METHOD_SCROLLBAR_DRAG,
    SCROLL_METHOD_KEYBOARD,
    SCROLL_METHOD_OVERRIDE,
)

#: One mouse-wheel notch approximates this many pixels of scroll.
_NOTCH_PIXELS = 50

#: A container whose UIA scroll percent reads -1.0 never reported a real value
#: (Chrome-hosted web roots do this); treating it as a panel makes bottom
#: detection impossible, so such containers are never picked as LEFT/RIGHT.
_INVALID_SCROLL_PERCENT = -1.0

#: A candidate covering this fraction of the client area is the web root /
#: window wrapper, never a panel.
_WRAPPER_AREA_FRACTION = 0.9

#: Adaptive scroll distances per method attempt (300 -> 600 -> 900 px).
#: Escalated from the caller's base ``pixels`` (the 250-350 px band), never a
#: page jump.
_MAX_SCROLL_ESCALATION = 900


@dataclass
class ScrollOutcome:
    """Result of a single panel scroll attempt."""

    ok: bool
    changed: bool
    method: str
    percent_before: float | None = None
    percent_after: float | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "method": self.method,
            "percent_before": self.percent_before,
            "percent_after": self.percent_after,
            "message": self.message,
        }


def _rect_overlap(a: BBox, b: BBox) -> int:
    x = max(0, min(a.right, b.right) - max(a.left, b.left))
    y = max(0, min(a.bottom, b.bottom) - max(a.top, b.top))
    return x * y


def pick_left_right_containers(
    containers: list[ScrollContainer],
    left_rect: BBox | None = None,
    right_rect: BBox | None = None,
    client_rect: tuple[int, int, int, int] | None = None,
) -> dict[str, ScrollContainer]:
    """Choose the LEFT (source) and RIGHT (entry) scroll containers.

    Uses content geometry when known (the container whose rect best overlaps the
    known source/form content is that panel); otherwise picks the two largest
    panels and splits them around the window centre. Never hardcodes coordinates.
    Either key may be absent when no suitable container exists.
    """
    if not containers:
        return {}

    candidates: list[ScrollContainer] = []
    for c in containers:
        if c.rect is None:
            continue
        # A -1.0 percent means the container never reported a real scroll
        # position (Chrome web roots do this). Picking it as a panel would make
        # scroll verification never see "bottom", so it is excluded outright.
        if c.vertical_scroll_percent == _INVALID_SCROLL_PERCENT:
            continue
        if client_rect is not None:
            left, top, right, bottom = client_rect
            box = c.rect
            if box.right <= left or box.left >= right or box.bottom <= top or box.top >= bottom:
                continue
            client_area = max(0, right - left) * max(0, bottom - top)
            # A container that covers ~the whole client area is the web root /
            # window wrapper, never one of the split panels.
            if client_area > 0 and box.area >= _WRAPPER_AREA_FRACTION * client_area:
                continue
        candidates.append(c)
    if not candidates:
        return {}

    chosen: dict[str, ScrollContainer] = {}
    used: set[int] = set()

    def _overflow_score(side: str) -> Callable[[BBox], int]:
        ref = left_rect if side == PANEL_LEFT else right_rect
        return (lambda b: _rect_overlap(b, ref)) if ref is not None else (lambda b: -1)

    if left_rect is not None and right_rect is not None:
        for side in (PANEL_LEFT, PANEL_RIGHT):
            score = _overflow_score(side)
            best: ScrollContainer | None = None
            best_score = 0  # require strictly positive content overlap
            for c in candidates:
                if id(c) in used or c.rect is None:
                    continue
                s = score(c.rect)
                if s > best_score:
                    best_score, best = s, c
            if best is not None:
                chosen[side] = best
                used.add(id(best))
        if chosen:
            return chosen

    # Fallback (no content geometry): the LEFT panel is the leftmost container
    # and the RIGHT panel is the largest remaining one (the entry form is the
    # biggest region of a split layout). Never hardcodes coordinates.
    candidates = [c for c in candidates if c.rect is not None]
    if len(candidates) == 1:
        chosen[PANEL_RIGHT] = candidates[0]
        return chosen
    left_pick = min(candidates, key=lambda c: c.rect.left if c.rect else 10**9)
    chosen[PANEL_LEFT] = left_pick
    rest = [c for c in candidates if c is not left_pick]
    right_pick = max(
        rest,
        key=lambda c: (c.rect.width * c.rect.height, c.rect.left) if c.rect is not None else (-1, 0),
    )
    chosen[PANEL_RIGHT] = right_pick
    return chosen


class PanelScroller:
    """Scrolls one UIA scroll container, verifying movement and escalating.

    ``scroll_down`` implements the fallback ladder exactly like a human
    operator, and NEVER stops after a single failed attempt:

    1. **UIA ScrollPattern** (preferred) - the container's own ScrollPattern
       (``Scroll`` / ``SetScrollPercent``); only touched when the container
       actually exposes one.
    2. **Mouse wheel** - move the cursor inside the panel, LEFT-CLICK to focus
       it (a wheel event scrolls whatever pane sits under the cursor, so the
       panel must be focused first - never the whole window), then wheel down.
    3. **Scrollbar thumb drag** - grab the container's vertical scrollbar thumb
       and drag it downward.
    4. **Keyboard** - click-focus the panel, then send PageDown (falling back
       to ArrowDown for a finer step). Some legacy / accessibility-only
       controls respond to keyboard navigation but expose neither a
       ScrollPattern nor a wheel-reactive surface.
    5. **JavaScript / DOM scroll** - handled separately by
       :class:`ChromeDomScroller` for browser-hosted panels; wired in through
       the ``dom`` callback and tried FIRST (see below) because Chrome-hosted
       forms such as MPF expose no UIA ScrollPattern at all.
    6. **Application-specific plugin override** - the very last resort. A
       plugin (e.g. the MPF plugin) may supply an ``override`` callback that
       performs a bespoke scroll gesture the generic engine cannot express.

    Each method is retried with an escalating distance (300 -> 600 -> 900 px)
    before being abandoned, and every attempt is verified: the caller re-
    observes the screen (fresh screenshot / scene) and returns True only when
    the visible labels actually changed. The DOM scrollTop method runs first for
    Chrome-hosted forms (the MPF app exposes no ScrollPattern); when no DOM
    controller is wired it is skipped, never credited with success.

    Direction is locked DOWN for the whole scan - a failed scroll is retried
    with the next method or a larger distance, never by scrolling back up.
    """

    def __init__(
        self,
        backend: UiaBackend | None = None,
        mouse: Any | None = None,
        keyboard: Any | None = None,
        handle: int | None = None,
        drag: Callable[[int, int, int, int], bool] | None = None,
        dom: Callable[[ScrollContainer, int], bool] | None = None,
        override: Callable[[ScrollContainer, int], bool | None] | None = None,
        settle: tuple[float, float] = (0.3, 0.5),
    ) -> None:
        self._backend = backend
        self._mouse = mouse
        self._keyboard = keyboard
        self._handle = handle
        self._drag = drag
        self._dom = dom
        self._override = override
        self._settle = settle

    def scroll_down(
        self,
        container: ScrollContainer,
        pixels: int,
        verify: Callable[[], bool],
    ) -> ScrollOutcome:
        """Scroll ``container`` down by ``~pixels``, escalating until it moves.

        ``verify`` is a zero-arg callable that re-observes the screen and
        returns True when the visible scene changed from the pre-scroll
        snapshot. The direction is always DOWN; a failed attempt never triggers
        an upward scroll.
        """
        last_percent = container.vertical_scroll_percent
        attempts = self._escalations(pixels)

        # (extra, Chrome-hosted forms) DOM scrollTop mutation via CDP. Only
        # credited when a controller is actually wired - otherwise verify()
        # alone would report movement that never happened.
        if self._dom is not None:
            for amount in attempts:
                try:
                    self._dom(container, amount)
                except Exception:
                    pass
                time.sleep(random.uniform(*self._settle))
                self._refresh_percent(container)
                if self._moved(container, verify, last_percent):
                    return self._ok(SCROLL_METHOD_DOM, last_percent, container)
                last_percent = container.vertical_scroll_percent

        # Method 1 (preferred): the container's own UIA ScrollPattern.
        if self._backend is not None and container.has_scroll_pattern:
            for amount in attempts:
                try:
                    self._backend.scroll_container_pattern(container, amount, self._handle)
                except Exception:
                    pass
                time.sleep(random.uniform(*self._settle))
                self._refresh_percent(container)
                if self._moved(container, verify, last_percent):
                    return self._ok(SCROLL_METHOD_PATTERN, last_percent, container)
                last_percent = container.vertical_scroll_percent

        # Method 2: click-focus the panel, then mouse wheel (300/600/900).
        if self._mouse is not None and container.rect is not None:
            for amount in attempts:
                self._focus_wheel(container, amount)
                time.sleep(random.uniform(*self._settle))
                self._refresh_percent(container)
                if self._moved(container, verify, last_percent):
                    return self._ok(SCROLL_METHOD_WHEEL, last_percent, container)
                last_percent = container.vertical_scroll_percent

        # Method 3: grab the vertical scrollbar thumb and drag it down.
        if self._drag is not None:
            for amount in attempts:
                self._drag_scroll(container, amount)
                time.sleep(random.uniform(*self._settle))
                self._refresh_percent(container)
                if self._moved(container, verify, last_percent):
                    return self._ok(SCROLL_METHOD_SCROLLBAR_DRAG, last_percent, container)
                last_percent = container.vertical_scroll_percent

        # Method 4: click-focus the panel, then PageDown (keyboard navigation).
        # Reached only when the pattern, wheel and scrollbar drag all failed -
        # some legacy / accessibility-only controls respond to keys alone.
        if self._keyboard is not None and container.rect is not None:
            for amount in attempts:
                self._keyboard_scroll(container, amount)
                time.sleep(random.uniform(*self._settle))
                self._refresh_percent(container)
                if self._moved(container, verify, last_percent):
                    return self._ok(SCROLL_METHOD_KEYBOARD, last_percent, container)
                last_percent = container.vertical_scroll_percent

        # Method 6 (absolute last resort): an application-specific override
        # supplied by a plugin. Only consulted after every generic method has
        # failed to move the panel - most plugins never implement this.
        if self._override is not None:
            for amount in attempts:
                try:
                    override_moved = self._override(container, amount)
                except Exception:
                    override_moved = None
                time.sleep(random.uniform(*self._settle))
                self._refresh_percent(container)
                if bool(override_moved) or self._moved(container, verify, last_percent):
                    return self._ok(SCROLL_METHOD_OVERRIDE, last_percent, container)
                last_percent = container.vertical_scroll_percent

        return ScrollOutcome(
            ok=False,
            changed=False,
            method="none",
            percent_before=last_percent,
            message="no scroll method produced a change",
        )

    # -- internals ------------------------------------------------------------

    def _escalations(self, pixels: int) -> list[int]:
        """Adaptive attempt sizes: base, 2x, 3x (300 -> 600 -> 900), capped."""
        base = max(1, int(pixels))
        seen: list[int] = []
        for i in range(1, 4):
            amount = min(base * i, _MAX_SCROLL_ESCALATION)
            if amount not in seen:
                seen.append(amount)
        return seen or [base]

    def _refresh_percent(self, container: ScrollContainer) -> None:
        """Refresh the UIA scroll percent for pattern-backed containers only.

        Overflow-discovered Chrome containers have no ScrollPattern: their
        percent is maintained by the DOM controller and must not be overwritten
        back to "unknown".
        """
        if self._backend is not None and container.has_scroll_pattern:
            try:
                self._backend.container_state(container, self._handle)
            except Exception:
                pass

    def _moved(
        self,
        container: ScrollContainer,
        verify: Callable[[], bool],
        last_percent: float | None,
    ) -> bool:
        """Did the scroll move the container (percent) or the visible scene?"""
        percent = container.vertical_scroll_percent
        if (
            percent is not None
            and last_percent is not None
            and percent > last_percent + 0.01
        ):
            return True
        try:
            return bool(verify())
        except Exception:
            return False

    def _ok(
        self, method: str, last_percent: float | None, container: ScrollContainer
    ) -> ScrollOutcome:
        return ScrollOutcome(
            ok=True,
            changed=True,
            method=method,
            percent_before=last_percent,
            percent_after=container.vertical_scroll_percent,
        )

    def _focus_wheel(self, container: ScrollContainer, pixels: int) -> bool:
        """Move the cursor inside the panel, CLICK to focus it, then wheel down.

        A wheel event scrolls whichever pane sits under the cursor, so each
        panel of a split form must be click-focused first - never the whole
        window. Never sends a wheel without focusing.
        """
        if self._mouse is None or container.rect is None:
            return False
        try:
            x, y = container.rect.center
            self._mouse.move_to(int(x), int(y))
            self._mouse.click(int(x), int(y))
            notches = max(3, min(18, max(1, int(pixels)) // _NOTCH_PIXELS))
            self._mouse.scroll("down", notches)
            return True
        except Exception:
            return False

    def _keyboard_scroll(self, container: ScrollContainer, pixels: int) -> bool:
        """Click-focus the panel, then send PageDown to scroll it.

        Mirrors ``_focus_wheel``: a key event is captured by whichever control
        has keyboard focus, so the panel is click-focused first (when a mouse
        is available) exactly like the wheel path - never a bare key press
        that might land on the wrong control. One PageDown is roughly a
        viewport height, so the press count is derived from the target pixel
        distance against a conservative ~120 px/press estimate (never zero).
        """
        if self._keyboard is None or container.rect is None:
            return False
        try:
            if self._mouse is not None:
                x, y = container.rect.center
                self._mouse.move_to(int(x), int(y))
                self._mouse.click(int(x), int(y))
            presses = max(1, min(10, int(pixels) // 120 or 1))
            self._keyboard.press("pagedown", presses)
            return True
        except Exception:
            return False

    def _drag_scroll(self, container: ScrollContainer, pixels: int) -> bool:
        """Drag the container's vertical scrollbar thumb downward by ``pixels``."""
        if self._drag is None or container.rect is None:
            return False
        try:
            bar = None
            if self._backend is not None:
                try:
                    bar = self._backend.container_scrollbar(container, self._handle)
                except Exception:
                    bar = None
            if bar is None or bar.rect is None:
                return False
            cx = container.rect.center[0]
            start_y = bar.rect.top + max(4, bar.rect.height // 10)
            end_y = min(bar.rect.bottom - 2, start_y + max(1, int(pixels)))
            return bool(self._drag(int(cx), int(start_y), int(cx), int(end_y)))
        except Exception:
            return False


class ChromeDomScroller:
    """Scrolls an embedded Chrome form's DOM containers via CDP evaluation.

    The MPF app is a Chromium-hosted form whose panels expose no UIA
    ScrollPattern, so the only reliable way to move them is to mutate the DOM
    scroll position directly (``element.scrollTop += N``) through the page's
    CDP connection. ``evaluate`` mirrors Playwright's synchronous
    ``page.evaluate`` and is injected so this class stays unit-testable without
    a live browser; ``locate`` maps a discovered container to a JS expression
    that resolves to its scrollable element (the container's ``dom_ref`` when
    set, otherwise the first element under the page that scrolls in that
    region).

    Each scroll refreshes the container's ``vertical_scroll_percent`` from real
    DOM geometry (``scrollTop / (scrollHeight - clientHeight)``) so bottom
    detection works for containers that have no ScrollPattern to report it.
    """

    def __init__(
        self,
        evaluate: Callable[[str], Any],
        locate: Callable[[ScrollContainer], str] | None = None,
    ) -> None:
        self._evaluate = evaluate
        self._locate = locate

    def _element_expr(self, container: ScrollContainer) -> str:
        if self._locate is not None:
            try:
                return self._locate(container)
            except Exception:
                pass
        ref = getattr(container, "dom_ref", "") or ""
        if ref:
            return ref
        # Fall back to the deepest scrollable element at the container's centre.
        rect = container.rect
        if rect is None:
            return "null"
        x, y = rect.center
        return (
            f"document.elementFromPoint({x}, {y})"
            f"?.closest('*')"
            f"|| document.elementsFromPoint({x}, {y})"
            f".find(e => e.scrollHeight > e.clientHeight) || null"
        )

    def scroll_down(self, container: ScrollContainer, pixels: int) -> bool:
        """Scroll ``container``'s DOM element down by ``~pixels``.

        Returns True when the element existed and its ``scrollTop`` moved (or
        was already at its maximum). The scroll percent is written back onto
        the container so the reveal pass knows when a panel truly hit its
        bottom - even though UIA never reports it.
        """
        expr = self._element_expr(container)
        if not expr or expr == "null":
            return False
        pixels = max(1, int(pixels))
        script = (
            f"(() => {{ const el = {expr};"
            f" if (!el || typeof el.scrollTop !== 'number') return null;"
            f" const maxTop = el.scrollHeight - el.clientHeight;"
            f" const before = el.scrollTop;"
            f" el.scrollTop = Math.min(maxTop, before + {pixels});"
            f" const moved = el.scrollTop !== before;"
            f" const percent = maxTop > 0 ? Math.round((el.scrollTop / maxTop) * 1000) / 10 : 100;"
            f" return [moved, percent]; }})()"
        )
        try:
            result = self._evaluate(script)
        except Exception:
            return False
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            return False
        try:
            container.vertical_scroll_percent = float(result[1])
        except (TypeError, ValueError):
            pass
        return bool(result[0])


@dataclass
class ScrollSession:
    """Discoverable state for one record's reveal pass.

    Bundles the freshly-discovered scroll containers with the engine that moves
    them so the workflow loop depends on a single object. ``containers`` is
    refreshed per observation (the loop calls the provider each round) so scroll
    positions stay current.
    """

    containers: list[ScrollContainer] = field(default_factory=list)
    scroller: PanelScroller | None = None

    @property
    def available(self) -> bool:
        return bool(self.containers) and self.scroller is not None


__all__ = [
    "PanelScroller",
    "ScrollOutcome",
    "ScrollSession",
    "ChromeDomScroller",
    "pick_left_right_containers",
    "SCROLL_METHOD_ORDER",
    "SCROLL_METHOD_PATTERN",
    "SCROLL_METHOD_WHEEL",
    "SCROLL_METHOD_SCROLLBAR_DRAG",
    "SCROLL_METHOD_DOM",
    "SCROLL_METHOD_KEYBOARD",
    "SCROLL_METHOD_OVERRIDE",
]
