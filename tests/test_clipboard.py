"""Tests for ClipboardEngine.read_focused's stale/no-op detection.

Regression coverage for a real bug found via a screen recording: when the
intended field did not actually have keyboard focus at the moment Ctrl+A /
Ctrl+C fired (very common right at the edge of a freshly revealed viewport),
the old implementation blindly trusted whatever was already on the clipboard.
In a Chromium-hosted form this surfaced as verification reading back the
window title / whole-page text instead of the field's value, forcing endless
retries - and, more seriously, could let a stale value from a PREVIOUS field
falsely match the current one.
"""

from __future__ import annotations

from atlas.act.clipboard import ClipboardEngine


class _FakeDriver:
    """Records hotkey calls; does not touch the real clipboard."""

    def __init__(self) -> None:
        self.hotkeys: list[tuple[str, ...]] = []

    def hotkey(self, *keys: str) -> None:
        self.hotkeys.append(keys)


class _NoOpClipboardEngine(ClipboardEngine):
    """A ClipboardEngine whose underlying OS clipboard never actually changes,
    simulating Ctrl+A/Ctrl+C firing while nothing was focused/selected."""

    def __init__(self, driver) -> None:
        super().__init__(driver)
        self._store = ""

    def get_text(self) -> str:
        return self._store

    def set_text(self, text: str) -> None:
        self._store = text or ""


class _RealCopyClipboardEngine(ClipboardEngine):
    """Simulates a real OS clipboard: set_text stores, and a successful
    Ctrl+C is modeled by overwriting the store right before get_text is next
    called via the driver hook below."""

    def __init__(self, driver, copied_value: str) -> None:
        super().__init__(driver)
        self._store = ""
        self._copied_value = copied_value
        self._copy_fires = False

    def get_text(self) -> str:
        if self._copy_fires:
            self._store = self._copied_value
        return self._store

    def set_text(self, text: str) -> None:
        self._store = text or ""


def test_read_focused_returns_empty_when_nothing_was_selected() -> None:
    """A no-op Ctrl+A/Ctrl+C (nothing focused) must return "" - never stale
    clipboard content left over from an earlier, unrelated operation."""
    driver = _FakeDriver()
    engine = _NoOpClipboardEngine(driver)
    # Something unrelated (e.g. the window title) was on the clipboard before
    # this read - simulating exactly what the recording showed.
    engine._store = "MP\r\nMPF (Download and Upload Form (Working Page))"
    result = engine.read_focused()
    assert result == ""
    # The select-all + copy sequence was still attempted.
    assert driver.hotkeys == [("ctrl", "a"), ("ctrl", "c")]


def test_read_focused_returns_real_value_on_successful_copy() -> None:
    """A genuine copy (the field really was focused) must still come through
    untouched - the sentinel guard must not eat real reads."""
    driver = _FakeDriver()
    engine = _RealCopyClipboardEngine(driver, "RAJESH KUMAR")
    engine._copy_fires = True
    result = engine.read_focused()
    assert result == "RAJESH KUMAR"


def test_read_focused_primes_a_private_sentinel_first() -> None:
    """The clipboard is primed with a private sentinel before every copy
    attempt, so a no-op is unambiguously detectable rather than inferred."""
    driver = _FakeDriver()
    engine = _NoOpClipboardEngine(driver)
    engine.read_focused()
    # After a no-op copy the store still holds whatever set_text primed it
    # with - never anything visible/public-looking.
    assert engine._store == ClipboardEngine._SENTINEL
