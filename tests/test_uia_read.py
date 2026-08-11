"""Tests for the UIA tree-based value reader (control_text / _collect_texts).

Covers the fixes that make verification robust on Chromium forms: ValuePattern
text is authoritative, the noisy multi-token element ``name`` (which Chromium
fills with the ADJACENT FIELD LABELS) is never emitted, Edit/Spinner names are
never treated as values, the read is window-scoped (no desktop hit-test, so an
occluding window can't leak text), and adjacent controls can't contaminate each
other through the intersection margin.
"""

from __future__ import annotations

from types import SimpleNamespace

from atlas.observe.uia import UiaBackend
from atlas.vision.models import BBox


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeInfo:
    """A fake UIA element: rectangle, control_type, name, optional value."""

    def __init__(
        self,
        ctype: str,
        name: str = "",
        value: str | None = None,
        rect: BBox | None = None,
        children: list["FakeInfo"] | None = None,
        framework_id: str = "",
    ) -> None:
        self._ctype = ctype
        self._name = name
        self._value = value
        self._rect = rect
        self._children = children or []
        self._framework_id = framework_id

    @property
    def rectangle(self) -> _Rect:
        if self._rect is None:
            raise RuntimeError("no rect")
        return _Rect(self._rect.x, self._rect.y, self._rect.right, self._rect.bottom)

    @property
    def control_type(self) -> str:
        return self._ctype

    @property
    def name(self) -> str:
        return self._name

    @property
    def value(self) -> str:
        return self._value or ""

    @property
    def framework_id(self) -> str:
        return self._framework_id

    def value_pattern(self) -> object:
        if self._value is None:
            raise RuntimeError("no value pattern")
        return SimpleNamespace(current_value=lambda: self._value)

    def children(self) -> list["FakeInfo"]:
        return list(self._children)


class _FakeBackend(UiaBackend):
    def __init__(self, root: FakeInfo) -> None:
        self._available = True
        self._root = root

    def _window_for(self, handle: int) -> object:
        return SimpleNamespace(element_info=self._root)

    def _flatten(self, info) -> FakeInfo:
        return info


def _backend(*children: FakeInfo) -> _FakeBackend:
    root = FakeInfo("Window", name="MPF (Download and Upload Form)", rect=BBox(0, 0, 1200, 800), children=list(children))
    return _FakeBackend(root)


# ---------------------------------------------------------------------------
# _control_display_text
# ---------------------------------------------------------------------------


def test_display_text_uses_value_pattern_first() -> None:
    info = FakeInfo("Edit", name="App No MBI Code", value="2026-0001")
    assert UiaBackend._control_display_text(info) == "2026-0001"


def test_display_text_edit_never_uses_label_name() -> None:
    info = FakeInfo("Edit", name="App No MBI Code", value="")
    assert UiaBackend._control_display_text(info) == ""


def test_display_text_spinner_never_uses_label_name() -> None:
    info = FakeInfo("Spinner", name="Speed", value="")
    assert UiaBackend._control_display_text(info) == ""


def test_display_text_combo_rejects_multi_token_label_noise() -> None:
    info = FakeInfo("ComboBox", name="Gender Marital Status", value="")
    assert UiaBackend._control_display_text(info) == ""


def test_display_text_combo_accepts_single_token_selected_item() -> None:
    info = FakeInfo("ComboBox", name="Male", value="")
    assert UiaBackend._control_display_text(info) == "Male"


def test_display_text_combo_single_token_rejected_for_web_framework() -> None:
    # Chromium/Electron exposes the field LABEL as the ComboBox name ("Rashi",
    # "Gender", ...) with an empty ValuePattern - never the selected item, so
    # a web-hosed combo's single-token name must NOT be treated as a value.
    info = FakeInfo("ComboBox", name="Rashi", value="", framework_id="Chrome")
    assert UiaBackend._control_display_text(info) == ""


def test_display_text_combo_value_pattern_wins_over_web_label() -> None:
    # The actual selected value via ValuePattern is authoritative even when
    # the name is the adjacent field label.
    info = FakeInfo("ComboBox", name="Rashi", value="Kataka / Cancer", framework_id="Chrome")
    assert UiaBackend._control_display_text(info) == "Kataka / Cancer"


def test_display_text_combo_single_token_kept_for_native_framework() -> None:
    # Native (non-browser) combos can still expose the selected item as the
    # single-token name fallback.
    info = FakeInfo("ComboBox", name="Male", value="", framework_id="Win32")
    assert UiaBackend._control_display_text(info) == "Male"


def test_display_text_empty_name() -> None:
    info = FakeInfo("ComboBox", name="", value="")
    assert UiaBackend._control_display_text(info) == ""


# ---------------------------------------------------------------------------
# control_text over a single field
# ---------------------------------------------------------------------------


def test_control_text_reads_value_pattern_of_edit() -> None:
    field = FakeInfo("Edit", name="Full Name", value="ANITA SHARMA", rect=BBox(972, 382, 257, 26))
    b = _backend(field)
    assert b.control_text(1234, field._rect) == "ANITA SHARMA"


def test_control_text_empty_edit_returns_none_not_label_noise() -> None:
    field = FakeInfo("Edit", name="App No MBI Code", value="", rect=BBox(972, 325, 257, 26))
    b = _backend(field)
    assert b.control_text(1234, field._rect) is None


def test_control_text_unnamed_combo_returns_none_not_surrounding_labels() -> None:
    field = FakeInfo("ComboBox", name="Gender Marital Status", value="", rect=BBox(972, 440, 54, 26))
    b = _backend(field)
    assert b.control_text(1234, field._rect) is None


# ---------------------------------------------------------------------------
# DOB triplet: union-box read must join only real values
# ---------------------------------------------------------------------------


def _dob_scene() -> _FakeBackend:
    day = FakeInfo("ComboBox", name="Gender Marital Status", value="", rect=BBox(972, 440, 54, 26))
    month = FakeInfo("ComboBox", name="Gender Marital Status", value="02", rect=BBox(1027, 440, 132, 26))
    year = FakeInfo("ComboBox", name="Gender Marital Status", value="1996", rect=BBox(1161, 440, 68, 26))
    return _backend(day, month, year)


def test_control_text_union_reads_only_real_values() -> None:
    b = _dob_scene()
    union = BBox(972, 440, 257, 26)
    assert b.control_text(1234, union) == "02 1996"


def test_control_text_union_empty_when_all_empty() -> None:
    b = _backend(
        FakeInfo("ComboBox", name="Gender Marital Status", value="", rect=BBox(972, 440, 54, 26)),
        FakeInfo("ComboBox", name="Gender Marital Status", value="", rect=BBox(1027, 440, 132, 26)),
        FakeInfo("ComboBox", name="Gender Marital Status", value="", rect=BBox(1161, 440, 68, 26)),
    )
    assert b.control_text(1234, BBox(972, 440, 257, 26)) is None


# ---------------------------------------------------------------------------
# Adjacent controls must not contaminate each other
# ---------------------------------------------------------------------------


def test_adjacent_control_not_leaked_through_margin() -> None:
    field_a = FakeInfo("Edit", value="A", rect=BBox(0, 0, 50, 26))
    field_b = FakeInfo("Edit", value="B", rect=BBox(0, 29, 50, 26))
    b = _backend(field_a, field_b)
    assert b.control_text(1234, BBox(0, 0, 50, 26)) == "A"


# ---------------------------------------------------------------------------
# Window-scoped fallback (never the desktop hit-test)
# ---------------------------------------------------------------------------


def test_fallback_is_window_scoped_and_returns_value_control() -> None:
    # The occluding "PowerShell" window is NOT part of this tree, so only the
    # value-bearing child can ever be returned.
    field = FakeInfo("Edit", value="Karnataka", rect=BBox(100, 100, 100, 26))
    b = _backend(field)
    assert b._deepest_node_at(b._root, 150, 113) is field


def test_fallback_never_returns_window_title_or_label() -> None:
    b = _backend(FakeInfo("Edit", name="App No MBI Code", value="", rect=BBox(100, 100, 100, 26)))
    assert b._deepest_node_at(b._root, 150, 113) is None
    assert b.control_text(1234, BBox(100, 100, 100, 26)) is None


def test_element_at_is_not_used_by_control_text() -> None:
    # Guard: control_text must never reach the desktop-wide element_at fallback.
    field = FakeInfo("Edit", name="App No MBI Code", value="", rect=BBox(972, 325, 257, 26))
    b = _backend(field)

    def boom(*_a, **_k):
        raise AssertionError("element_at must not be called")

    b.element_at = boom  # type: ignore[method-assign]
    assert b.control_text(1234, field._rect) is None


# ---------------------------------------------------------------------------
# Real-backend ValuePattern read fix (pywinauto 0.6.9 has no value_pattern())
# ---------------------------------------------------------------------------


class _RealLikeInfo(FakeInfo):
    """A fake shaped like real pywinauto 0.6.9 UIAElementInfo: it has a raw
    ``element`` (with the Value pattern) but NO ``value_pattern()`` method,
    which is exactly the production shape the read fix targets."""

    def __init__(self, value: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_value = value
        iface = SimpleNamespace(CurrentValue=value, CurrentIsReadOnly=False, SetValue=lambda v: None)
        raw = SimpleNamespace()
        raw.GetCurrentPattern = lambda _pid: SimpleNamespace(QueryInterface=lambda _cls: iface)
        self.element = raw  # type: ignore[attr-defined]

    def __getattribute__(self, name: str):
        # Real pywinauto 0.6.9 UIAElementInfo has NO value_pattern(); making
        # attribute ACCESS raise AttributeError means hasattr() reports False
        # exactly like production, so the read fix's element branch is used.
        if name == "value_pattern":
            raise AttributeError("no value_pattern on real pywinauto 0.6.9")
        return super().__getattribute__(name)


def test_control_display_text_reads_real_element_value_iface() -> None:
    """When a real element has no ``value_pattern()`` (pywinauto 0.6.9) but does
    expose a raw ``element`` with a Value pattern, the read must use it instead
    of silently returning an empty string."""
    info = _RealLikeInfo(value="2026-0001", ctype="Edit", name="App No MBI Code", rect=BBox(0, 0, 100, 26))
    assert UiaBackend._control_display_text(info) == "2026-0001"


def test_set_control_value_writes_real_element_value_iface() -> None:
    """The ValuePattern writer reaches the raw element's SetValue and returns
    True when the control is writable."""
    written: list[str] = []
    iface = SimpleNamespace(CurrentValue="old", CurrentIsReadOnly=False)
    iface.SetValue = lambda v: written.append(str(v))
    raw = SimpleNamespace()
    raw.GetCurrentPattern = lambda _pid: SimpleNamespace(QueryInterface=lambda _cls: iface)
    info = _RealLikeInfo(value="old", ctype="Edit", name="Full Name", rect=BBox(200, 100, 150, 26))
    info.element = raw  # type: ignore[attr-defined]
    b = _backend(info)
    assert b.set_control_value(1234, info._rect, "NEW-VALUE") is True
    assert written == ["NEW-VALUE"]


def test_deepest_value_info_finds_edit_through_bad_ancestor_rect() -> None:
    """A container Group with a stale/oversized rect must not hide its
    value-bearing descendant from the writer's element lookup."""
    field = FakeInfo("Edit", name="Full Name", value="ANITA SHARMA", rect=BBox(200, 100, 150, 26))
    weird_group = FakeInfo("Group", name="inner", rect=BBox(150, -5000, 300, 300), children=[field])
    b = _backend(weird_group)
    found = b._deepest_value_info(b._root, 275, 113)
    assert found is field


def test_set_control_value_unavailable_without_value_pattern() -> None:
    """No Value pattern (fake has no element) -> the writer reports False so
    callers fall back to click + type. Never raises."""
    field = FakeInfo("Edit", name="Full Name", value="ANITA SHARMA", rect=BBox(200, 100, 150, 26))
    b = _backend(field)
    assert b.set_control_value(1234, field._rect, "NEW") is False
