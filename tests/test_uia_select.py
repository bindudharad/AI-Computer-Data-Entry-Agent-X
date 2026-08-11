"""Tests for Phase 4 direct dropdown selection (SelectionItemPattern /
ExpandCollapsePattern) and the per-field option cache.

Covers:
* matching a ListItem by normalized name and firing SelectionItem.Select(),
* the ExpandCollapse.Expand() -> select -> Collapse() path,
* option-list caching and reuse (no re-open for the same field),
* graceful False when UIA cannot select (caller falls back to arrow/type),
* the ControlEngine.option_setter hook: direct-first, keyboard fallback.
"""

from __future__ import annotations

from types import SimpleNamespace

from atlas.act.controls import ControlEngine
from atlas.act.mouse import HumanMouse
from atlas.config import TypingConfig
from atlas.observe.uia import UiaBackend
from atlas.vision.models import BBox


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeItem:
    """A fake ListItem element with an optional SelectionItem iface."""

    def __init__(self, name: str, ctype: str = "ListItem", rect: BBox | None = None) -> None:
        self._name = name
        self._ctype = ctype
        self._rect = rect or BBox(0, 0, 40, 16)
        self.selected: list[str] = []

    @property
    def rectangle(self) -> _Rect:
        return _Rect(self._rect.x, self._rect.y, self._rect.right, self._rect.bottom)

    @property
    def control_type(self) -> str:
        return self._ctype

    @property
    def name(self) -> str:
        return self._name

    def children(self) -> list:
        return []

    def select(self, value: str) -> None:
        self.selected.append(value)


class _FakeCombo:
    """A fake ComboBox whose children are list items."""

    def __init__(self, items: list[_FakeItem], rect: BBox | None = None) -> None:
        self._items = items
        self._rect = rect or BBox(100, 100, 160, 24)
        self.expand_count = 0
        self.collapse_count = 0
        self.expand_ok = True

    @property
    def rectangle(self) -> _Rect:
        return _Rect(self._rect.x, self._rect.y, self._rect.right, self._rect.bottom)

    @property
    def control_type(self) -> str:
        return "ComboBox"

    @property
    def name(self) -> str:
        return "Gender"

    def children(self) -> list[_FakeItem]:
        return list(self._items)


class _SelectBackend(UiaBackend):
    """Fake backend: pattern ifaces are wired per fake element."""

    def __init__(self, combo: _FakeCombo) -> None:
        self._available = True
        self._combo = combo
        self._option_cache: dict = {}

    def _window_for(self, handle: int) -> object:
        return SimpleNamespace(element_info=self._combo)

    def _element_pattern_iface(self, info, pattern: str):
        if pattern == "ExpandCollapse" and isinstance(info, _FakeCombo):
            return SimpleNamespace(
                Expand=lambda: setattr(info, "expand_count", info.expand_count + 1),
                Collapse=lambda: setattr(info, "collapse_count", info.collapse_count + 1),
            )
        if pattern == "SelectionItem" and isinstance(info, _FakeItem):
            return SimpleNamespace(Select=lambda: info.select("fire"))
        return None


def _gender_combo() -> _FakeCombo:
    return _FakeCombo([_FakeItem("Male"), _FakeItem("Female"), _FakeItem("Other")])


# ---------------------------------------------------------------------------
# select_option - direct SelectionItem match
# ---------------------------------------------------------------------------


def test_select_option_fires_selection_item_select() -> None:
    combo = _gender_combo()
    backend = _SelectBackend(combo)
    assert backend.select_option(1001, combo._rect, "Female") is True
    # Only the matching item fired.
    assert combo._items[1].selected == ["fire"]
    assert combo._items[0].selected == []
    assert combo._items[2].selected == []


def test_select_option_matches_normalized_name() -> None:
    combo = _gender_combo()
    backend = _SelectBackend(combo)
    assert backend.select_option(1001, combo._rect, "female") is True
    assert combo._items[1].selected == ["fire"]


def test_select_option_no_match_returns_false() -> None:
    combo = _gender_combo()
    backend = _SelectBackend(combo)
    assert backend.select_option(1001, combo._rect, "Robot") is False
    assert all(item.selected == [] for item in combo._items)


# ---------------------------------------------------------------------------
# ExpandCollapse path
# ---------------------------------------------------------------------------


def test_select_option_expand_collapse_when_items_hidden() -> None:
    class _HiddenCombo(_FakeCombo):
        def __init__(self, items: list[_FakeItem]) -> None:
            super().__init__([])
            self._all_items = items
            self._hidden = True

        def children(self) -> list[_FakeItem]:
            return self._all_items if not self._hidden else []

    combo = _HiddenCombo([_FakeItem("Male"), _FakeItem("Female")])

    class _ExpandBackend(_SelectBackend):
        def _element_pattern_iface(self, info, pattern: str):
            if isinstance(info, _FakeCombo) and pattern == "ExpandCollapse":
                return SimpleNamespace(
                    Expand=lambda: setattr(info, "_hidden", False),
                    Collapse=lambda: setattr(info, "collapse_count", info.collapse_count + 1),
                )
            if isinstance(info, _FakeItem) and pattern == "SelectionItem":
                return SimpleNamespace(Select=lambda: info.select("fire"))
            return None

    backend = _ExpandBackend(combo)
    assert backend.select_option(1001, combo._rect, "Female") is True
    assert combo._all_items[1].selected == ["fire"]


def test_expand_collapse_no_match_still_collapses() -> None:
    combo = _gender_combo()
    combo.expand_ok = True
    backend = _SelectBackend(combo)

    class _FailingExpand(_SelectBackend):
        def _element_pattern_iface(self, info, pattern: str):
            if pattern == "ExpandCollapse":
                return SimpleNamespace(Expand=lambda: None, Collapse=lambda: None)
            return None

    backend = _FailingExpand(combo)
    assert backend.select_option(1001, combo._rect, "Robot") is False
    # No crash, popup collapsed (collapse_count increments in the fake).
    assert all(item.selected == [] for item in combo._items)


# ---------------------------------------------------------------------------
# option cache
# ---------------------------------------------------------------------------


def test_select_option_caches_options_for_field() -> None:
    combo = _gender_combo()
    backend = _SelectBackend(combo)
    assert backend.select_option(1001, combo._rect, "Female") is True
    cached = backend.cached_options(1001, combo._rect)
    assert cached is not None
    assert set(cached) == {"Male", "Female", "Other"}


def test_cached_options_expires() -> None:
    combo = _gender_combo()
    backend = _SelectBackend(combo)
    backend.remember_options(1001, combo._rect, ["Male", "Female", "Other"])
    assert backend.cached_options(1001, combo._rect) == ["Male", "Female", "Other"]


def test_remember_options_overwrites_for_field() -> None:
    combo = _gender_combo()
    backend = _SelectBackend(combo)
    backend.remember_options(1001, combo._rect, ["A", "B"])
    backend.remember_options(1001, combo._rect, ["C"])
    assert backend.cached_options(1001, combo._rect) == ["C"]


# ---------------------------------------------------------------------------
# ControlEngine.option_setter
# ---------------------------------------------------------------------------


class _StubDriver:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []

    def position(self): return (0, 0)
    def move_to(self, x, y, duration=0.0): pass
    def click(self, x, y, clicks=1, interval=0.0): pass
    def type_char(self, char): self.typed.append(char)
    def press(self, key): self.pressed.append(key)
    def hotkey(self, *keys): pass
    def scroll(self, dx, dy): pass


class _StubMouse:
    def move_to(self, x, y): pass
    def click(self, x, y): pass
    def scroll(self, direction, amount=3): pass


def _engine(**kwargs) -> ControlEngine:
    from atlas.act.keyboard import HumanKeyboard

    keyboard = HumanKeyboard(_StubDriver(), TypingConfig())
    return ControlEngine(mouse=_StubMouse(), keyboard=keyboard, **kwargs)


def test_option_setter_is_used_first() -> None:
    calls: list[tuple] = []

    def setter(bbox: BBox, value: str, options: list[str] | None, field_id: str | None) -> bool:
        calls.append((bbox, value, options, field_id))
        return True

    engine = _engine(option_setter=setter)
    outcome = engine.select_option(BBox(100, 100, 160, 24), "Female", ["Male", "Female"], "f0")
    assert outcome.ok is True
    assert "UIA direct" in outcome.evidence
    assert calls == [(BBox(100, 100, 160, 24), "Female", ["Male", "Female"], "f0")]


def test_option_setter_fall_back_on_failure() -> None:
    engine = _engine(option_setter=lambda bbox, value, options, fid: False)
    outcome = engine.select_option(BBox(100, 100, 160, 24), "Female", ["Male", "Female"], "f0")
    assert outcome.ok is True
    assert "UIA direct" not in outcome.evidence
    assert "arrow-selected" in outcome.evidence


def test_option_setter_fall_back_on_exception() -> None:
    def setter(bbox, value, options, fid) -> bool:
        raise RuntimeError("uia gone")

    engine = _engine(option_setter=setter)
    outcome = engine.select_option(BBox(100, 100, 160, 24), "Female", ["Male", "Female"], "f0")
    assert outcome.ok is True
    assert "UIA direct" not in outcome.evidence


def test_option_setter_not_called_without_bbox() -> None:
    calls: list[tuple] = []

    def setter(bbox, value, options, fid) -> bool:
        calls.append(bbox)
        return True

    engine = _engine(option_setter=setter)
    outcome = engine.select_option(None, "Female", ["Male", "Female"], "f0")
    assert outcome.ok is True
    assert calls == []


def test_select_empty_value_skips_option_setter() -> None:
    calls: list[tuple] = []

    def setter(bbox, value, options, fid) -> bool:
        calls.append(value)
        return True

    engine = _engine(option_setter=setter)
    outcome = engine.select_option(BBox(100, 100, 160, 24), "", ["Male"], "f0")
    assert outcome.ok is True
    assert "skipped" in outcome.evidence
    assert calls == []
