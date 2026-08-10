"""Tests for the ``_scroll_into_view`` scroll-lock fix.

Root-cause regression coverage: while the round-level reveal pass held its
scroll lock (`set_scroll_allowed(lambda: False)`), `_scroll_into_view`
returned immediately for EVERY action, even one whose bbox was confirmed to
be outside the visible client rect. A field's bbox can go stale mid-batch
(an earlier action in the same batch reflows the page, a dropdown shifts
layout, ...); with the rescue disabled, the click for that field silently
missed, focus stayed on the PREVIOUS field, and `clear_field()`'s Ctrl+A then
wiped that field's content instead - the "keeps focusing the previous
textbox, performs Ctrl+A, never scrolls" failure mode.

The fix ports the reference implementation's unconditional
"confirm-visible-before-touch" guarantee: the visibility check itself now
always runs; only the SIZE of the recovery (a single corrective nudge vs. the
full multi-strategy ladder) is still gated by the lock, so the round-level
pass's own bulk/synchronized scrolling is never fought or duplicated.
"""

from __future__ import annotations

from types import SimpleNamespace

from atlas.act.executor import ActionExecutor
from atlas.act.models import Action, ActionType
from atlas.vision.models import BBox


class _FakeControls:
    def __init__(self) -> None:
        self.scroll_calls: list[tuple[str, str, int]] = []

    def scroll(self, direction: str, amount: int = 3):
        self.scroll_calls.append(("wheel", direction, amount))
        return SimpleNamespace(ok=True, evidence="scrolled")

    def scroll_by_keys(self, direction: str, amount: int = 3):
        self.scroll_calls.append(("keys", direction, amount))
        return SimpleNamespace(ok=True, evidence="key-scrolled")

    def scroll_bar(self, direction: str, amount: int = 3):
        self.scroll_calls.append(("scrollbar", direction, amount))
        return SimpleNamespace(ok=True, evidence="scrollbar jump")


class _FakeSandbox:
    """Reports a fixed client rect and never actually blocks anything -
    only `_scroll_into_view`'s use of `validate_target()` is exercised."""

    def __init__(self, client_rect: tuple[int, int, int, int]) -> None:
        self._target = SimpleNamespace(client_rect=client_rect)
        self.is_paused = False

    def validate_target(self):
        return self._target


def _executor(controls: _FakeControls, sandbox: _FakeSandbox, reobserve=None) -> ActionExecutor:
    return ActionExecutor(
        mouse=SimpleNamespace(),
        keyboard=SimpleNamespace(),
        controls=controls,
        verifier=SimpleNamespace(),
        recovery=SimpleNamespace(),
        sandbox=sandbox,
        reobserve=reobserve,
        max_scroll_attempts=6,
    )


def _action(bbox: BBox) -> Action:
    return Action(type=ActionType.TYPE, reason="fill field", field_id="f1", value="x", bbox=bbox)


def test_bbox_is_refreshed_unconditionally_on_retry_even_if_technically_in_bounds() -> None:
    """The exact new regression: a field whose bbox merely SHIFTED (a
    cascading dropdown reflowed the layout below it) still reads as
    "inside the client rect" - the plain visibility check alone can't catch
    it. Once a first attempt has already failed, the field's true current
    bbox must be looked up fresh regardless, closing that gap."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 1000))
    moved_bbox = BBox(100, 500, 50, 20)  # the field's TRUE current position

    class _Scene:
        def element(self, field_id):
            return SimpleNamespace(bbox=moved_bbox)

        screen_offset = (0, 0)

    executor = _executor(controls, sandbox, reobserve=lambda: _Scene())
    # Stale bbox: technically still "inside" the 1000x1000 client rect, so
    # the plain out-of-viewport check alone would never trigger a rescue.
    action = _action(BBox(100, 100, 50, 20))

    executor._scroll_into_view(action, attempt=0)
    assert action.bbox.top == 100  # attempt 0: fast path, bbox left as-is
    assert controls.scroll_calls == []

    executor._scroll_into_view(action, attempt=1)
    assert action.bbox.top == moved_bbox.top  # attempt 1: refreshed to truth
    assert controls.scroll_calls == []  # no scroll needed - just a stale bbox


def test_bbox_refresh_is_skipped_on_the_first_attempt() -> None:
    """The common, successful case (no prior failure) must not pay the
    reobserve cost - only a field that already failed once does."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 1000))
    calls = {"n": 0}

    def reobserve():
        calls["n"] += 1
        return None

    executor = _executor(controls, sandbox, reobserve=reobserve)
    action = _action(BBox(100, 100, 50, 20))

    executor._scroll_into_view(action, attempt=0)

    assert calls["n"] == 0


def test_bbox_refresh_failure_does_not_crash() -> None:
    """A reobserve that raises or returns None must not break execution -
    the action just proceeds with whatever bbox it already had."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 1000))

    def reobserve():
        raise RuntimeError("boom")

    executor = _executor(controls, sandbox, reobserve=reobserve)
    action = _action(BBox(100, 100, 50, 20))

    executor._scroll_into_view(action, attempt=1)  # must not raise

    assert action.bbox.top == 100


def test_visible_field_is_never_scrolled_even_when_unlocked() -> None:
    """The common case (field already on screen): no scroll call at all."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 1000))
    executor = _executor(controls, sandbox)
    action = _action(BBox(100, 100, 50, 20))  # well inside the client rect

    executor._scroll_into_view(action)

    assert controls.scroll_calls == []


def test_offscreen_field_gets_rescued_even_while_scroll_locked() -> None:
    """The exact regression: a field confirmed off-screen must still get at
    least one corrective scroll, even while the round-level lock is held."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 600))
    executor = _executor(controls, sandbox)
    executor.set_scroll_allowed(lambda: False)  # the reveal pass's batch lock
    action = _action(BBox(100, 700, 50, 20))  # below the visible client rect

    executor._scroll_into_view(action)

    assert controls.scroll_calls, "expected at least one corrective scroll while locked"
    assert controls.scroll_calls[0][:2] == ("wheel", "down")


def test_locked_rescue_is_capped_at_one_attempt() -> None:
    """While locked, the rescue must be a single nudge, never the full
    multi-strategy ladder - that remains the round-level pass's job."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 600))

    def reobserve():
        return None  # never resolves - if uncapped this would loop 6 times

    executor = _executor(controls, sandbox, reobserve=reobserve)
    executor.set_scroll_allowed(lambda: False)
    action = _action(BBox(100, 700, 50, 20))

    executor._scroll_into_view(action)

    assert len(controls.scroll_calls) == 1


def test_unlocked_offscreen_field_gets_the_full_ladder() -> None:
    """Outside a locked batch (e.g. the final submit-button scroll), the full
    multi-attempt, multi-strategy behaviour is unchanged."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 600))

    def reobserve():
        return None  # bbox is never refreshed - forces every attempt to fire

    executor = _executor(controls, sandbox, reobserve=reobserve)
    executor.set_scroll_allowed(lambda: True)
    action = _action(BBox(100, 700, 50, 20))

    executor._scroll_into_view(action)

    # Bbox never changes (reobserve returns None) so each strategy escalates
    # after a single no-op attempt: wheel, then keys, then scrollbar - all
    # three are exhausted (never capped to the locked case's single attempt).
    assert [call[0] for call in controls.scroll_calls] == ["wheel", "keys", "scrollbar"]


def test_field_that_becomes_visible_after_one_nudge_stops_immediately() -> None:
    """Once the rescued field is confirmed visible again, no further scroll
    calls are made - the nudge is corrective, not exploratory."""
    controls = _FakeControls()
    sandbox = _FakeSandbox(client_rect=(0, 0, 1000, 600))
    moved_bbox = BBox(100, 300, 50, 20)  # now inside the client rect

    class _Scene:
        def element(self, field_id):
            return SimpleNamespace(bbox=moved_bbox)

        screen_offset = (0, 0)

    executor = _executor(controls, sandbox, reobserve=lambda: _Scene())
    executor.set_scroll_allowed(lambda: False)
    action = _action(BBox(100, 700, 50, 20))

    executor._scroll_into_view(action)

    assert len(controls.scroll_calls) == 1
    assert action.bbox.top == moved_bbox.top
