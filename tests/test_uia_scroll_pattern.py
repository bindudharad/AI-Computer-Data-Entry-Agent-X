"""Tests for the UIA ScrollPattern QueryInterface fix.

Regression coverage for a real bug found via a screen recording:
``GetCurrentPattern`` hands back an untyped COM pointer (raw ``IUnknown``),
not the typed ``IUIAutomationScrollPattern`` interface. Calling
``.Scroll(...)`` / ``.CurrentVerticalViewSize`` directly on it failed with
``'POINTER(IUnknown)' object has no attribute 'Scroll'`` on every single
attempt - Method 1 (UIA ScrollPattern) of the Scroll Manager was silently
guaranteed to fail for every container that reported having a pattern at
all. The fix explicitly ``QueryInterface``s the raw pointer into the typed
interface before any member is touched.

The real ``comtypes`` package is Windows-only and unavailable on this test
runner, so ``comtypes.gen.UIAutomationClient`` is faked via ``sys.modules``.
"""

from __future__ import annotations

import sys
import types

import pytest

from atlas.observe.uia import ScrollContainer, UiaBackend
from atlas.vision.models import BBox


class _RawIUnknown:
    """Models the real, untyped pointer ``GetCurrentPattern`` returns: it
    has NO scroll members until explicitly ``QueryInterface``'d."""

    def __init__(self, typed: "_TypedScrollPattern | None") -> None:
        self._typed = typed

    def QueryInterface(self, interface):
        if self._typed is None:
            raise OSError("Interface not supported (E_NOINTERFACE)")
        return self._typed

    def __bool__(self) -> bool:
        return True

    # Deliberately NO .Scroll / .CurrentVerticalViewSize / .SetScrollPercent -
    # accessing any of those on this object must raise AttributeError, exactly
    # reproducing the bug the recording showed.


class _TypedScrollPattern:
    """The real, typed interface after a successful QueryInterface."""

    def __init__(self, view_size: float = 25.0, percent: float = 0.0) -> None:
        self.CurrentVerticalViewSize = view_size
        self.CurrentVerticalScrollPercent = percent
        self.scroll_calls: list[tuple] = []
        self.set_percent_calls: list[tuple[float, float]] = []

    def Scroll(self, horizontal, vertical) -> None:
        self.scroll_calls.append((horizontal, vertical))

    def SetScrollPercent(self, horizontal: float, vertical: float) -> None:
        self.set_percent_calls.append((horizontal, vertical))
        self.CurrentVerticalScrollPercent = vertical


class _FakeElement:
    def __init__(self, pattern) -> None:
        self._pattern = pattern
        self.name = "right-panel"

    def GetCurrentPattern(self, pattern_id):
        return self._pattern


class _FakeInfo:
    """Stands in for the pywinauto ``UIAElementInfo`` node UiaBackend reads."""

    def __init__(self, element) -> None:
        self.element = element
        self.name = "right-panel"  # `_container_info` probes this to confirm liveness


@pytest.fixture(autouse=True)
def fake_comtypes(monkeypatch):
    """Injects a minimal fake ``comtypes.gen.UIAutomationClient`` so the code
    under test can run without the real (Windows-only) comtypes package."""
    fake_module = types.ModuleType("UIAutomationClient")
    fake_module.UIA_ScrollPatternId = 1
    fake_module.IUIAutomationScrollPattern = "IUIAutomationScrollPattern-iid"
    fake_module.ScrollAmount_NoAmount = 0
    fake_module.ScrollAmount_LargeIncrement = 3
    fake_comtypes = types.ModuleType("comtypes")
    fake_gen = types.ModuleType("comtypes.gen")
    fake_gen.UIAutomationClient = fake_module
    fake_comtypes.gen = fake_gen
    monkeypatch.setitem(sys.modules, "comtypes", fake_comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.gen", fake_gen)
    monkeypatch.setitem(sys.modules, "comtypes.gen.UIAutomationClient", fake_module)
    yield


def _container(ref) -> ScrollContainer:
    return ScrollContainer(
        name="right",
        control_type="Pane",
        automation_id="",
        class_name="",
        framework_id="",
        handle=None,
        rect=BBox(200, 0, 300, 600),
        has_scroll_pattern=True,
        vertical_scroll_percent=0.0,
        vertical_view_size=25.0,
        runtime_id=(),
        parent=None,
        _ref=ref,
    )


def test_scroll_container_pattern_queries_the_typed_interface() -> None:
    """The raw pointer must be QueryInterface'd - calling members on the raw
    pointer directly (the old behaviour) would raise AttributeError."""
    typed = _TypedScrollPattern(view_size=25.0, percent=0.0)
    raw = _RawIUnknown(typed)
    info = _FakeInfo(_FakeElement(raw))
    container = _container(info)
    backend = UiaBackend.__new__(UiaBackend)  # skip pywinauto init (Windows-only)

    ok = backend.scroll_container_pattern(container, 300)

    assert ok is True
    # It moved via SetScrollPercent (real geometry available), on the TYPED
    # pattern, never on the untyped raw pointer (which has no such method).
    assert typed.set_percent_calls, "expected SetScrollPercent to be called on the typed pattern"


def test_scroll_container_pattern_fails_cleanly_when_interface_unsupported() -> None:
    """When the element genuinely doesn't support the scroll interface,
    QueryInterface raises - this must be caught and reported as a clean
    failure (return False), never propagate or crash the caller."""
    raw = _RawIUnknown(typed=None)  # QueryInterface always raises
    info = _FakeInfo(_FakeElement(raw))
    container = _container(info)
    backend = UiaBackend.__new__(UiaBackend)

    ok = backend.scroll_container_pattern(container, 300)

    assert ok is False


def test_scroll_pattern_helper_returns_none_for_untyped_raw_pointer() -> None:
    """`_scroll_pattern` must return the TYPED pattern (or None), never the
    raw untyped pointer - callers must never see a pointer that crashes on
    first real use."""
    raw = _RawIUnknown(typed=None)
    info = _FakeInfo(_FakeElement(raw))
    assert UiaBackend._scroll_pattern(info) is None

    typed = _TypedScrollPattern()
    raw_ok = _RawIUnknown(typed)
    info_ok = _FakeInfo(_FakeElement(raw_ok))
    assert UiaBackend._scroll_pattern(info_ok) is typed


def test_scroll_percent_reads_from_the_typed_pattern() -> None:
    """`_scroll_percent` must read real numbers back, not silently degrade to
    (None, None) because it touched the untyped raw pointer."""
    typed = _TypedScrollPattern(view_size=40.0, percent=12.5)
    raw = _RawIUnknown(typed)
    info = _FakeInfo(_FakeElement(raw))

    percent, view = UiaBackend._scroll_percent(info)

    assert percent == 12.5
    assert view == 40.0
