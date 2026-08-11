"""UI Automation (UIA) helpers.

Thin wrapper over pywinauto/comtypes UIAutomationCore used to (a) resolve the
control the user clicks as the ``StartControl`` anchor, and (b) enumerate the
form's editable controls and the source panel's labels so the agent does not
depend on the VLM for exact field geometry.

Every entry point is defensive: if the UIA provider is unavailable or a call
fails, the helpers degrade to ``None``/``[]`` so the rest of the pipeline keeps
working on vision-only data.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas.core.logging import logger, uia_logger
from atlas.vision.models import BBox, ElementType

#: UIA control types that represent editable form widgets.
EDITABLE_CONTROL_TYPES = {
    "Edit",
    "ComboBox",
    "CheckBox",
    "RadioButton",
    "Calendar",
    "List",
    "ListItem",
    "DataGrid",
    "DataItem",
    "Spinner",
    "Slider",
    "Tree",
    "TreeItem",
}

#: UIA control types that can host a scrollable region (a Pane, List, Document,
#: Custom control, Grid/Table, Tree or ScrollViewer). Only these are inspected
#: for a ScrollPattern / vertical scrollbar when discovering scroll containers;
#: leaf widgets (Edit, Button, Text) are never containers.
SCROLL_CONTAINER_TYPES = {
    "Pane",
    "List",
    "Document",
    "Custom",
    "Group",
    "Table",
    "DataGrid",
    "Tree",
    "Tab",
    "Window",
    "ScrollViewer",
    "ScrollBar",
}

#: WinForms / WPF / Win32 class-name fragments that mark a scrollable region.
_SCROLL_CLASS_HINTS = ("scroll", "vscroll", "scrollviewer", "listview", "datagrid")

#: A container smaller than this cannot be a meaningful scroll panel.
_MIN_SCROLL_CONTAINER_W = 40
_MIN_SCROLL_CONTAINER_H = 60

#: A container is "at the bottom" once this close to 100% vertical scroll.
_MAX_SCROLL_PERCENT = 99.5

#: Control types worth inspecting even when no editable widget is found; these
#: tell us the window is a real application form (vs a desktop shell).
INSPECTABLE_CONTROL_TYPES = EDITABLE_CONTROL_TYPES | {
    "Button",
    "SplitButton",
    "ScrollBar",
    "Pane",
    "Custom",
    "Table",
    "Document",
    "Group",
    "List",
    "ListItem",
    "Text",
    "Static",
}

#: UIA control types that carry static text (potential source-panel labels).
TEXT_CONTROL_TYPES = {"Text", "Static", "Document"}

#: UIA control types that carry a displayable value for read-back verification.
#: Deliberately narrower than EDITABLE_CONTROL_TYPES (which also includes list /
#: tree / grid containers) so a read-back never grabs popup list items.
_VALUE_CONTROL_TYPES = {"Edit", "ComboBox", "Calendar", "Spinner"}

#: UIA control types that are the drop-down popup of a combobox / listbox.
#: These host the ``SelectionItemPattern`` that lets the agent select an option
#: DIRECTLY - no focus click, no arrow keys, no Enter, no dropdown animation
#: wait (the phase-4 replacement for the 1.7-2.0 s per-field dropdown open
#: cost).
_SELECTION_ITEM_TYPES = {
    "ListItem",
    "ListBoxOption",
    "ComboBox",
    "List",
    "SelectionItem",
}

#: How long a freshly-read option list stays cached for a field before it is
#: re-read from the live tree.
_OPTION_CACHE_TTL = 3.0

#: UIA pattern names probed when resolving a direct selection target.
_SELECTION_PATTERNS = ("SelectionItem", "Selection", "ExpandCollapse")

#: UIA framework ids whose ComboBox ``name`` is the ADJACENT FIELD LABEL
#: ("Rashi", "Gender", ...) rather than the selected item, so the name can
#: never be trusted as a selected-item fallback for read-back verification.
_WEB_FRAMEWORK_HINTS = ("chrome", "chromium", "edge", "electron", "webview", "cef", "firefox", "safari")


def _is_web_framework(info) -> bool:
    """True when ``info`` comes from a browser/Electron-hosed runtime.

    Chromium/Electron combos expose an empty ValuePattern and put the field's
    label in ``name``; trusting that name as the selected value makes UIA read-
    back falsely return the label (verifying nothing and polluting evidence).
    """
    try:
        fw = (info.framework_id or "").lower()
    except Exception:
        fw = ""
    return any(hint in fw for hint in _WEB_FRAMEWORK_HINTS)


def _intersects(rect: BBox, box: BBox, margin: int = 0) -> bool:
    """True when two boxes overlap (optionally grown by ``margin`` px)."""
    return (
        rect.left <= box.right + margin
        and rect.right >= box.left - margin
        and rect.top <= box.bottom + margin
        and rect.bottom >= box.top - margin
    )


#: Mapping of UIA control type -> agent element type.
_CONTROL_TYPE_MAP: dict[str, ElementType] = {
    "Edit": ElementType.TEXTBOX,
    "ComboBox": ElementType.COMBOBOX,
    "CheckBox": ElementType.CHECKBOX,
    "RadioButton": ElementType.RADIO,
    "Calendar": ElementType.CALENDAR,
    "List": ElementType.LISTBOX,
    "ListItem": ElementType.LISTBOX,
    "DataGrid": ElementType.GRID,
    "DataItem": ElementType.GRID,
    "Spinner": ElementType.TEXTBOX,
    "Slider": ElementType.TEXTBOX,
    "Button": ElementType.BUTTON,
    "SplitButton": ElementType.BUTTON,
    "Text": ElementType.LABEL,
    "Static": ElementType.LABEL,
    "StatusBar": ElementType.STATUS_BAR,
    "ToolBar": ElementType.TOOLBAR,
    "Tab": ElementType.TAB,
    "TabItem": ElementType.TAB,
    "Tree": ElementType.TREE_VIEW,
    "TreeItem": ElementType.TREE_VIEW,
    "Menu": ElementType.MENU,
    "MenuItem": ElementType.MENU,
}


@dataclass
class UiaNode:
    """A UIA element flattened into plain data."""

    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    handle: int | None = None
    rect: BBox | None = None  # absolute screen coordinates
    value: str | None = None
    enabled: bool = True
    visible: bool = True
    password: bool = False
    options: list[str] = field(default_factory=list)
    type_override: ElementType | None = None
    framework_id: str = ""
    is_keyboard_focusable: bool = False
    patterns: list[str] = field(default_factory=list)
    parent: dict[str, Any] | None = None
    children: list[dict[str, Any]] = field(default_factory=list)

    @property
    def editable(self) -> bool:
        return self.control_type in EDITABLE_CONTROL_TYPES and self.enabled

    @property
    def element_type(self) -> ElementType:
        if self.type_override is not None:
            return self.type_override
        if self.control_type == "Edit" and self.password:
            return ElementType.PASSWORD
        return _CONTROL_TYPE_MAP.get(self.control_type, ElementType.UNKNOWN)

    @property
    def center(self) -> tuple[int, int] | None:
        return self.rect.center if self.rect is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "handle": self.handle,
            "rect": self.rect.to_dict() if self.rect else None,
            "value": self.value,
            "enabled": self.enabled,
            "visible": self.visible,
            "password": self.password,
            "options": list(self.options),
            "type_override": self.type_override.value if self.type_override else None,
            "editable": self.editable,
            "element_type": self.element_type.value,
            "framework_id": self.framework_id,
            "is_keyboard_focusable": self.is_keyboard_focusable,
            "patterns": list(self.patterns),
            "parent": self.parent,
            "children": list(self.children),
        }


@dataclass
class ScrollContainer:
    """A vertically scrollable container discovered from the UIA tree.

    A split form has TWO of these (left source-data list, right entry form).
    Each is a real control that owns its own scrollbar / ScrollPattern and is
    scrolled *directly* - never by simulating wheel events over the window or
    the desktop. ``vertical_scroll_percent`` (0..100) is read from the live
    ScrollPattern and answers "is more content available?". The private
    ``_ref`` holds the live pywinauto element for runtime scrolling only.
    """

    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    framework_id: str = ""
    handle: int | None = None
    rect: BBox | None = None
    has_scroll_pattern: bool = False
    vertical_scroll_percent: float | None = None
    vertical_view_size: float | None = None
    runtime_id: tuple[int, ...] = ()
    parent: dict[str, Any] | None = None
    dom_ref: str = ""
    _ref: Any = field(default=None, compare=False, repr=False)

    @property
    def at_max(self) -> bool:
        """True when the container is definitively scrolled to its bottom."""
        return self.vertical_scroll_percent is not None and self.vertical_scroll_percent >= _MAX_SCROLL_PERCENT

    @property
    def more_content(self) -> bool:
        """Whether more content likely exists below the fold.

        ``None`` percent (no ScrollPattern) means "unknown" and is treated as
        True so the scan keeps trying; ``False`` only when the container is
        definitively at its maximum scroll position.
        """
        return not self.at_max

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "framework_id": self.framework_id,
            "handle": self.handle,
            "rect": self.rect.to_dict() if self.rect else None,
            "has_scroll_pattern": self.has_scroll_pattern,
            "vertical_scroll_percent": self.vertical_scroll_percent,
            "vertical_view_size": self.vertical_view_size,
            "runtime_id": list(self.runtime_id),
            "parent": self.parent,
            "dom_ref": self.dom_ref,
            "at_max": self.at_max,
            "more_content": self.more_content,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScrollContainer:
        rect = data.get("rect")
        box = None
        if isinstance(rect, dict):
            box = BBox.from_dict(rect)
        elif isinstance(rect, (list, tuple)) and len(rect) >= 4:
            box = BBox(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        return cls(
            name=data.get("name", ""),
            control_type=data.get("control_type", ""),
            automation_id=data.get("automation_id", ""),
            class_name=data.get("class_name", ""),
            framework_id=data.get("framework_id", ""),
            handle=data.get("handle"),
            rect=box,
            has_scroll_pattern=bool(data.get("has_scroll_pattern", False)),
            vertical_scroll_percent=data.get("vertical_scroll_percent"),
            vertical_view_size=data.get("vertical_view_size"),
            runtime_id=tuple(data.get("runtime_id") or ()),
            parent=data.get("parent"),
            dom_ref=data.get("dom_ref", "") or "",
        )


def _tree_rect(node: dict[str, Any]) -> BBox | None:
    """A node's rect from the recursive tree dict ``[left, top, right, bottom]``."""
    raw = node.get("rect")
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        left, top, right, bottom = (int(v) for v in raw[:4])
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return BBox(left, top, right - left, bottom - top)


def discover_overflow_containers(
    tree: dict[str, Any] | list[dict[str, Any]],
    client_rect: tuple[int, int, int, int] | None = None,
    min_width: int = _MIN_SCROLL_CONTAINER_W,
    min_height: int = _MIN_SCROLL_CONTAINER_H,
    overflow_margin: int = 10,
) -> list[ScrollContainer]:
    """Discover scrollable panels from content overflow alone.

    Chrome-hosted forms (the MPF app) expose NO ScrollPattern and NO ScrollBar
    nodes: the only evidence that a panel scrolls is that its child content
    extends below its clipped visible rect. This walks the recursive UIA tree
    (``_tree_dict`` / ``dump_tree`` format) and reports every container-ish node
    whose descendant content overflows its rect by a meaningful amount.

    The returned containers carry ``has_scroll_pattern=False`` and a ``None``
    scroll percent (unknown), so ``more_content`` stays True and the reveal pass
    keeps scrolling until a DOM/pattern scroll proves the bottom.
    """
    roots = tree if isinstance(tree, list) else [tree]

    def _overflow_bottom(node: dict[str, Any], box: BBox) -> int | None:
        """Deepest bottom of descendant content that overflows ``box``, or None."""
        deepest: int | None = None
        stack = list(node.get("children") or [])
        while stack:
            child = stack.pop()
            rect = _tree_rect(child)
            if rect is None:
                stack.extend(child.get("children") or [])
                continue
            # Only content that shares this panel's column counts; elements in
            # the sibling panel never do.
            if rect.right <= box.left or rect.left >= box.right:
                stack.extend(child.get("children") or [])
                continue
            if rect.bottom > box.bottom + overflow_margin:
                deepest = rect.bottom if deepest is None else max(deepest, rect.bottom)
            stack.extend(child.get("children") or [])
        return deepest

    # Chrome wraps every page in a chain of full-window Panes/Groups. A
    # candidate that covers ~90% of the window is a wrapper, never a panel.
    wrapper_area: int = 0
    if client_rect is not None:
        left, top, right, bottom = client_rect
        wrapper_area = max(0, right - left) * max(0, bottom - top)
    elif roots:
        root_box = _tree_rect(roots[0])
        wrapper_area = root_box.area if root_box is not None else 0

    result: list[ScrollContainer] = []
    for root in roots:
        stack: list[dict[str, Any]] = [root]
        while stack:
            node = stack.pop()
            box = _tree_rect(node)
            if box is not None:
                ctype = node.get("control_type") or ""
                inside = True
                if client_rect is not None:
                    left, top, right, bottom = client_rect
                    inside = box.right > left and box.left < right and box.bottom > top and box.top < bottom
                if (
                    ctype in SCROLL_CONTAINER_TYPES
                    and ctype != "Window"
                    and box.width >= min_width
                    and box.height >= min_height
                    and inside
                    and (wrapper_area <= 0 or box.area < 0.9 * wrapper_area)
                ):
                    overflow = _overflow_bottom(node, box)
                    if overflow is not None:
                        result.append(ScrollContainer(
                            name=node.get("name") or "",
                            control_type=ctype,
                            automation_id=node.get("automation_id") or "",
                            class_name=node.get("class_name") or "",
                            framework_id=node.get("framework_id") or "",
                            handle=node.get("handle"),
                            rect=box,
                            has_scroll_pattern=False,
                            vertical_scroll_percent=None,
                        ))
            children = node.get("children") or []
            stack.extend(reversed(children))

    # De-duplicate nested candidates in the SAME column: the tall content
    # wrapper inside a visible panel (its clip) is dropped - the clip is the
    # scroll target the loop reasons about. Sibling panels (the MPF left source
    # list and right entry form) never contain each other and both stay.
    kept: list[ScrollContainer] = []
    for candidate in result:
        if candidate.rect is None:
            continue
        nested = any(
            other is not candidate
            and other.rect is not None
            and other.rect.height < candidate.rect.height
            and candidate.rect.left >= other.rect.left
            and candidate.rect.right <= other.rect.right
            and candidate.rect.top >= other.rect.top
            and candidate.rect.bottom <= other.rect.bottom
            and min(candidate.rect.right, other.rect.right) - max(candidate.rect.left, other.rect.left)
            >= 0.5 * candidate.rect.width
            for other in result
        )
        if not nested:
            kept.append(candidate)
    return kept


class UiaBackend:
    """Lazily loaded, defensive facade over pywinauto's UIA bindings.

    A single process-wide instance is safe because all UIA work happens on the
    thread that constructs it (the main agent thread).
    """

    _instance: UiaBackend | None = None

    #: Scroll-container discovery results are stable across scrolls (a panel's
    #: clip rect does not change when content scrolls inside it), so the result
    #: is cached per ``(handle, client_rect)`` to stop every field-map rebuild
    #: from paying two full recursive walks. Window resizes change the client
    #: rect and therefore invalidate the cache via the key.
    _SCROLL_CONTAINER_CACHE_TTL = 3.0

    def __init__(self) -> None:
        self._available = False
        self._desktop = None
        self._window = None
        self._scroll_container_cache: dict[tuple[int, tuple[int, int, int, int] | None], tuple[float, list[ScrollContainer]]] = {}
        self._option_cache: dict[tuple[int, tuple[int, int, int, int]], tuple[float, list[str]]] = {}
        try:
            from pywinauto import Desktop
            from pywinauto.controls.uiawrapper import UIAElementInfo
            from pywinauto.uia_defines import IUIA

            self._desktop = Desktop(backend="uia")
            self._element_info = UIAElementInfo
            self._iuia = IUIA()
            self._available = True
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("UIA backend unavailable: {}", exc)

    @classmethod
    def instance(cls) -> UiaBackend:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def available(self) -> bool:
        return self._available

    # -- resolution ----------------------------------------------------------

    def element_at(self, x: int, y: int) -> UiaNode | None:
        """Resolve the UIA element under an absolute screen point."""
        if not self._available:
            return None
        try:
            info = self._element_info.from_point(x, y)
            return self._flatten(info)
        except Exception as exc:
            logger.debug("element_at({}, {}) failed: {}", x, y, exc)
            return None

    def focused(self) -> UiaNode | None:
        """Return the currently focused UIA element, if any."""
        if not self._available:
            return None
        try:
            return self._flatten(self._element_info(self._iuia.get_focused_element()))
        except Exception as exc:
            logger.debug("focused() failed: {}", exc)
            return None

    # -- enumeration ---------------------------------------------------------

    def descendants(self, handle: int) -> list[UiaNode]:
        """Return every UIA descendant of a window handle (read order)."""
        if not self._available:
            return []
        window = self._window_for(handle)
        if window is None:
            return []
        try:
            wrappers = window.descendants()
        except Exception as exc:
            logger.debug("descendants({}) failed: {}", handle, exc)
            return []
        nodes = []
        for wrapper in wrappers:
            try:
                node = self._flatten(wrapper.element_info)
            except Exception:
                continue
            if node is not None:
                nodes.append(node)
        uia_logger.debug("uia descendants({}) -> {} nodes", handle, len(nodes))
        return nodes

    def editable_fields(self, handle: int, nodes: list[UiaNode] | None = None) -> list[UiaNode]:
        """Editable form widgets under ``handle``, in reading order.

        Uses pywinauto's flat ``descendants()`` walk first. If that returns
        zero editable controls, falls back to an explicit recursive traversal
        (Step 3) so controls nested inside Panes/Customs/Tables are still
        found. Never silently returns zero without trying the full tree.
        """
        if nodes is None:
            nodes = self.descendants(handle)
        editable = [n for n in nodes if n.editable and not (n.rect is not None and (n.rect.width <= 0 or n.rect.height <= 0))]
        if editable:
            return editable
        # Step 3: recursive traversal fallback - inspect every child element.
        logger.info("flat UIA walk found 0 editable controls; trying recursive traversal")
        recursive = self.inspectable_nodes(handle)
        recursive_editable = [
            n for n in recursive
            if n.editable and not (n.rect is not None and (n.rect.width <= 0 or n.rect.height <= 0))
        ]
        if recursive_editable:
            logger.info("recursive traversal found {} editable controls", len(recursive_editable))
        return recursive_editable

    # -- UIA root / attachment discovery (Chrome_WidgetWin_1, CEF, Electron) ---

    def uia_root(self, handle: int):
        """Resolve the UIA root element of ``handle`` straight from the HWND.

        ``UIAElementInfo(hwnd)`` performs ``IUIAutomation.ElementFromHandle`` -
        the same resolution ``Desktop.window(handle=...)`` uses, but reached
        directly so a wrapper with pid=0 or an unusual backend never blocks it.
        Returns a ``UIAElementInfo`` (or None when UIA is unavailable).
        """
        if not self._available:
            return None
        try:
            return self._element_info(handle)
        except Exception as exc:
            logger.debug("uia_root({}) failed: {}", handle, exc)
            return None

    def walk_descendants(self, handle: int) -> list[UiaNode]:
        """Recursively walk the full tree from the RAW root element of ``handle``.

        Bypasses ``Desktop.window(handle=...)`` entirely - the raw
        ``ElementFromHandle`` root is the UIA root from HWND. Chromium /
        Electron expose their accessibility tree lazily; walking
        ``children()`` (instead of pywinauto's flat ``descendants()``) forces
        every Pane/Custom/Group level to materialize, which is what keeps
        nested WebView content discoverable.
        """
        if not self._available:
            return []
        root = self.uia_root(handle)
        if root is None:
            return []
        nodes: list[UiaNode] = []

        def _walk(info, depth: int = 0) -> None:
            if depth > 64:
                return
            try:
                node = self._flatten(info)
            except Exception:
                node = None
            if node is not None:
                nodes.append(node)
            try:
                children = info.children()
            except Exception:
                children = []
            for child in children:
                try:
                    _walk(child, depth + 1)
                except Exception:
                    continue

        try:
            _walk(root)
        except Exception as exc:
            logger.debug("walk_descendants({}) failed: {}", handle, exc)
        uia_logger.debug("uia walk_descendants({}) -> {} nodes", handle, len(nodes))
        return nodes

    def child_hwnds(self, handle: int, visible_only: bool = True) -> list[int]:
        """Every descendant HWND of ``handle`` via EnumChildWindows."""
        import win32gui

        found: list[int] = []

        def _enum(child: int, _: Any) -> bool:
            try:
                if not visible_only or win32gui.IsWindowVisible(child):
                    found.append(child)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(handle, _enum, None)
        except Exception as exc:
            logger.debug("child_hwnds({}) failed: {}", handle, exc)
        return found

    def _editable_with_rect(self, handle: int) -> list[UiaNode]:
        editable = self.editable_fields(handle)
        if not editable:
            editable = [n for n in self.walk_descendants(handle) if n.editable]
        return [
            n for n in editable
            if n.rect is not None and n.rect.width > 0 and n.rect.height > 0
        ]

    def probe_editable_roots(self, handle: int) -> list[dict[str, Any]]:
        """Probe ``handle`` first, then every descendant HWND, for editable
        controls (strategies C + D).

        Chromium apps (class ``Chrome_WidgetWin_1`` / ``Chrome_RenderWidgetHostHWND``)
        often keep the real form in a CHILD window; the title-matched top-level
        may itself expose nothing editable. Each probed HWND yields a dict with
        its editable controls (empty when none) so the caller can pick the real
        root without assuming the top-level window is the form container.
        """
        if not self._available:
            return []
        results: list[dict[str, Any]] = []

        def probe(h: int) -> dict[str, Any]:
            try:
                editable = self._editable_with_rect(h)
            except Exception as exc:
                logger.debug("probe hwnd {} failed: {}", h, exc)
                editable = []
            return {"hwnd": h, "editable": editable, "count": len(editable)}

        results.append(probe(handle))
        for child in self.child_hwnds(handle):
            results.append(probe(child))
        results.sort(key=lambda r: (0 if r["count"] else 1, r["hwnd"]))
        return results

    def best_editable_root(self, handle: int) -> dict[str, Any] | None:
        """Best (hwnd -> editable controls) among ``handle`` and its children."""
        for probe in self.probe_editable_roots(handle):
            if probe["count"] > 0:
                return probe
        return None

    def focused_element_and_chain(self) -> dict[str, Any]:
        """Focused UIA element + its ancestor chain (strategy E).

        Returns ``{focused, chain, hwnd_roots, pid, class_name, control_type,
        name, automation_id, framework_id}``. ``hwnd_roots`` lists every native
        window handle on the focused element's ancestry - the caller climbs
        these to find the real application/root container regardless of which
        window owns the title match.
        """
        if not self._available:
            return {}
        try:
            info = self._element_info(self._iuia.get_focused_element())
        except Exception as exc:
            logger.debug("focused_element_and_chain failed: {}", exc)
            return {}
        chain: list[UiaNode] = []
        hwnd_roots: list[int] = []
        cur = info
        for _ in range(48):
            try:
                node = self._flatten(cur)
            except Exception:
                node = None
            if node is not None:
                chain.append(node)
            try:
                hwnd = int(cur.handle or 0)
            except Exception:
                hwnd = 0
            if hwnd:
                hwnd_roots.append(hwnd)
            try:
                parent = cur.parent
            except Exception:
                parent = None
            if parent is None:
                break
            cur = parent
        focused = chain[0] if chain else None
        focus = focused.to_dict() if focused is not None else {}
        info = {
            "focused": focus,
            "chain": [n.to_dict() for n in chain],
            "hwnd_roots": hwnd_roots,
            "hwnd": hwnd_roots[0] if hwnd_roots else 0,
            "pid": 0,
            "class": focus.get("class_name", ""),
            "control_type": focus.get("control_type", ""),
            "name": focus.get("name", ""),
            "automation_id": focus.get("automation_id", ""),
            "framework_id": focus.get("framework_id", ""),
        }
        if hwnd_roots:
            # richest detail: walk up from focused's own hwnd to a top-level.
            info["pid"] = self._pid_for_hwnd(hwnd_roots[0])
        return info

    def _pid_for_hwnd(self, hwnd: int) -> int:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        pid = wintypes.DWORD(0)
        try:
            user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        except Exception:
            pass
        return int(pid.value)

    def control_counts(self, handle: int) -> dict[str, int]:
        """Editable / ComboBox / Button / Edit / Date control counts."""
        nodes = self.walk_descendants(handle)
        return {
            "controls": len(nodes),
            "editable_controls": len([n for n in nodes if n.editable]),
            "comboboxes": len([n for n in nodes if n.control_type == "ComboBox"]),
            "buttons": len([n for n in nodes if n.control_type in {"Button", "SplitButton"}]),
            "textboxes": len([n for n in nodes if n.control_type == "Edit"]),
            "calendars": len([n for n in nodes if n.control_type == "Calendar"]),
        }

    def text_nodes(self, handle: int, nodes: list[UiaNode] | None = None) -> list[UiaNode]:
        """Static text controls under ``handle`` (source-panel labels)."""
        if nodes is None:
            nodes = self.descendants(handle)
        result = []
        for n in nodes:
            if n.control_type not in TEXT_CONTROL_TYPES:
                continue
            if not n.name or not n.name.strip():
                continue
            if n.rect is None or n.rect.width <= 0 or n.rect.height <= 0:
                continue
            result.append(n)
        return result

    # -- read-back verification ------------------------------------------------

    def control_text(self, handle: int, bbox: BBox) -> str | None:
        """Read the current text/value of the control(s) under ``bbox`` from the
        UIA tree - no clicks, no focus change, works while occluded.

        Collects the display text of every value-bearing control whose rect
        intersects the box (a single field, or the whole day/month/year date
        triplet at once) and joins them. Falls back to the element resolved
        under the box's centre when the tree walk finds nothing. Returns None
        when nothing readable was found (empty field, unknown widget).
        """
        if not self._available or bbox is None:
            return None
        window = self._window_for(handle)
        if window is None:
            return None
        try:
            root = window.element_info
        except Exception as exc:
            logger.debug("control_text: no root info: {}", exc)
            return None
        texts: list[str] = []
        self._collect_texts(root, bbox, texts)
        joined = " ".join(t for t in texts if t).strip()
        if joined:
            return joined
        node = self._deepest_node_at(root, *bbox.center)
        if node is not None and (node.value or node.name):
            return (node.value or node.name).strip() or None
        return None

    def _collect_texts(self, info, bbox: BBox, out: list[str], seen: set[str] | None = None) -> None:
        """Depth-first: append the display text of editable controls in ``bbox``.

        ``seen`` de-duplicates identical text so a ComboBox and its inner Edit
        (which often report the same selection) appear only once.
        """
        if seen is None:
            seen = set()
        box: BBox | None = None
        try:
            rect = info.rectangle
            box = BBox(
                int(rect.left), int(rect.top),
                max(0, rect.right - rect.left), max(0, rect.bottom - rect.top),
            )
        except Exception:
            box = None
        if box is not None and box.width > 0 and box.height > 0 and _intersects(box, bbox, margin=2):
            try:
                ctype = info.control_type or ""
            except Exception:
                ctype = ""
            if ctype in _VALUE_CONTROL_TYPES:
                text = self._control_display_text(info)
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        if len(out) >= 12:
            return
        try:
            children = info.children()
        except Exception:
            return
        for child in children:
            if len(out) >= 12:
                return
            try:
                self._collect_texts(child, bbox, out, seen)
            except Exception:
                continue

    def _deepest_node_at(self, root, x: int, y: int) -> UiaNode | None:
        """Deepest value-bearing UIA element within ``root``'s subtree that
        contains the point.

        Window-scoped alternative to a desktop ``from_point`` hit test, so an
        occluding window on top of the target can never leak its text into a
        read-back. Only value-bearing controls with readable text qualify - a
        window title or label is never returned.
        """
        best: UiaNode | None = None
        try:
            children = root.children()
        except Exception:
            children = []
        for child in children:
            try:
                text = self._control_display_text(child).strip()
            except Exception:
                text = ""
            if not text:
                continue
            try:
                rect = child.rectangle
                bx = BBox(
                    int(rect.left), int(rect.top),
                    max(0, rect.right - rect.left), max(0, rect.bottom - rect.top),
                )
            except Exception:
                bx = None
            if bx is None or bx.width <= 0 or bx.height <= 0 or not bx.contains(x, y):
                continue
            try:
                ctype = child.control_type or ""
            except Exception:
                ctype = ""
            if ctype in _VALUE_CONTROL_TYPES:
                return self._flatten(child)
        return best

    @staticmethod
    def _control_display_text(info) -> str:
        """Display text of one UIA element.

        ValuePattern text is authoritative (it is the control's actual value).
        Chromium/Electron exposes the ADJACENT FIELD LABELS as the element
        name (e.g. ``"Gender Marital Status"``) instead of the selected item,
        so the name is only trusted as a selected-item fallback for combo-ish
        controls when it is a single short token AND the element is not
        browser-hosed - never for Edit/Spinner (whose name is always the
        label) and never for multi-token label noise.
        """
        value = ""
        try:
            if hasattr(info, "value_pattern"):  # test doubles / fakes
                value = info.value_pattern().current_value() or ""
            else:
                raw = getattr(info, "element", None)
                if raw is not None:
                    from pywinauto.uia_defines import get_elem_interface

                    value = get_elem_interface(raw, "Value").CurrentValue or ""
        except Exception:
            value = ""
        text = value.strip()
        if text:
            return text
        try:
            ctype = info.control_type or ""
        except Exception:
            ctype = ""
        try:
            name = (info.name or "").strip()
        except Exception:
            name = ""
        if not name:
            return ""
        if ctype in ("Edit", "Spinner"):
            return ""
        if ctype in ("ComboBox", "Calendar") and len(name.split()) == 1 and len(name) <= 32:
            if not _is_web_framework(info):
                return name
        return ""

    # -- write-back (ValuePattern.SetValue) ------------------------------------

    @staticmethod
    def _element_value_iface(info):
        """The live, typed ``IUIAutomationValuePattern`` of ``info``, or None.

        pywinauto 0.6.9 does NOT expose ``value_pattern()`` on
        ``UIAElementInfo`` (only ``UIAWrapper`` has it), so the old
        ``hasattr(info, "value_pattern")`` guards were dead code in production
        and UIA reads silently returned nothing. The real pattern is reached
        through ``get_elem_interface`` on the raw COM element - the same
        route ``UIAWrapper.iface_value`` uses.
        """
        try:
            raw = getattr(info, "element", None)
            if raw is None:
                return None
            from pywinauto.uia_defines import get_elem_interface

            return get_elem_interface(raw, "Value")
        except Exception:
            return None

    def _deepest_value_info(self, root, x: int, y: int, _depth: int = 0):
        """Deepest value-bearing raw ``element_info`` under ``root`` whose own
        rect contains the point, or None.

        Returns the live element_info (so its ValuePattern can be written),
        not a flattened UiaNode. Recurse into every child (like
        ``_collect_texts``) instead of pruning on ancestor rects: UIA
        container Groups can report stale/oversized rects that fail the
        contains test even though their descendant Edit genuinely holds the
        point, so pruning by the ancestors would miss the target.
        """
        if _depth > 64:
            return None
        best = None
        try:
            children = root.children()
        except Exception:
            return None
        for child in children:
            try:
                rect = child.rectangle
                bx = BBox(
                    int(rect.left), int(rect.top),
                    max(0, rect.right - rect.left), max(0, rect.bottom - rect.top),
                )
            except Exception:
                bx = None
            if bx is not None and bx.width > 0 and bx.height > 0 and bx.contains(x, y):
                try:
                    ctype = child.control_type or ""
                except Exception:
                    ctype = ""
                if ctype in _VALUE_CONTROL_TYPES:
                    best = child
            deeper = self._deepest_value_info(child, x, y, _depth + 1)
            if deeper is not None:
                best = deeper
        return best

    def set_control_value(self, handle: int, bbox: BBox, value: str) -> bool:
        """Write ``value`` straight into the control under ``bbox`` via UIA
        ``ValuePattern.SetValue`` - no focus change, no keystrokes, no
        clipboard (a few ms instead of per-character typing).

        Returns True when the write was applied, False when UIA is
        unavailable, no value-bearing element sits under the box, the control
        exposes no Value pattern, or it is read-only (callers then fall back
        to click + type).
        """
        if not self._available or bbox is None:
            return False
        window = self._window_for(handle)
        if window is None:
            return False
        try:
            root = window.element_info
        except Exception as exc:
            logger.debug("set_control_value: no root info: {}", exc)
            return False
        info = self._deepest_value_info(root, *bbox.center)
        if info is None:
            return False
        iface = self._element_value_iface(info)
        if iface is None:
            return False
        try:
            if iface.CurrentIsReadOnly:
                return False
            current = None
            try:
                current = iface.CurrentValue
            except Exception:
                current = None
            if current is not None and str(current) == str(value):
                # No-op: the control already holds the target value (e.g. a
                # Sub Caste / Nakshatra field that was NOT actually reset).
                # Skip the write entirely - avoids focus flicker and any
                # server-side reformat on an identical write.
                return True
            iface.SetValue(str(value))
            return True
        except Exception as exc:
            logger.debug("set_control_value failed: {}", exc)
            return False

    # -- direct selection (SelectionItem / ExpandCollapse) ---------------------

    @staticmethod
    def _element_pattern_iface(info, pattern: str):
        """The live, typed UIA pattern interface of ``info``, or None.

        Reached through ``get_elem_interface`` on the raw COM element, the
        same route ``_element_value_iface`` uses for the Value pattern.
        """
        try:
            raw = getattr(info, "element", None)
            if raw is None:
                return None
            from pywinauto.uia_defines import get_elem_interface

            return get_elem_interface(raw, pattern)
        except Exception:
            return None

    def _deepest_selection_info(self, root, x: int, y: int, _depth: int = 0):
        """Deepest selection-capable raw ``element_info`` under ``root`` whose
        rect contains the point, or None.

        Like ``_deepest_value_info`` but for selectable items: recurses past
        stale/oversized ancestor rects and prefers the DEEPEST element that
        supports a SelectionItem / Selection / ExpandCollapse pattern so a
        value written to a mid-level control still lands on the live item.
        """
        if _depth > 64:
            return None
        best = None
        try:
            children = root.children()
        except Exception:
            return None
        for child in children:
            try:
                rect = child.rectangle
                bx = BBox(
                    int(rect.left), int(rect.top),
                    max(0, rect.right - rect.left), max(0, rect.bottom - rect.top),
                )
            except Exception:
                bx = None
            if bx is not None and bx.width > 0 and bx.height > 0 and bx.contains(x, y):
                if self._supports_selection(child):
                    best = child
            deeper = self._deepest_selection_info(child, x, y, _depth + 1)
            if deeper is not None:
                best = deeper
        return best

    @classmethod
    def _supports_selection(cls, info) -> bool:
        """True when ``info`` can host a direct selection.

        Short-circuits on the control type first (the leaf is a ListItem /
        ComboBox / List without extra COM calls); only falls through to
        probing patterns when the type is ambiguous.
        """
        try:
            ctype = (info.control_type or "").strip()
        except Exception:
            ctype = ""
        if ctype in _SELECTION_ITEM_TYPES:
            return True
        if not ctype or ctype == "Pane":
            try:
                element = getattr(info, "element", None)
                if element is not None:
                    from pywinauto.uia_defines import get_elem_interface

                    for pattern in _SELECTION_PATTERNS:
                        try:
                            get_elem_interface(element, pattern)
                            return True
                        except Exception:
                            continue
            except Exception:
                return False
        return False

    def _select_item_by_name(self, info, value: str) -> bool:
        """Select the option matching ``value`` under ``info`` (or ``info``
        itself when it is the item). Returns True when a selection fired."""
        value_str = str(value or "").strip().lower()
        if not value_str:
            return False
        # ``info`` itself is the match (e.g. the popup already selected on the
        # combo and the deepest element IS the ListItem).
        if self._item_matches(info, value_str):
            iface = self._element_pattern_iface(info, "SelectionItem")
            if iface is not None:
                try:
                    iface.Select()
                    return True
                except Exception:
                    pass
        try:
            children = info.children()
        except Exception:
            return False
        for child in children:
            if self._select_item_by_name(child, value_str):
                return True
        return False

    @staticmethod
    def _item_matches(info, value: str) -> bool:
        if not value:
            return False
        try:
            name = (info.name or "").strip()
        except Exception:
            name = ""
        if not name:
            return False
        try:
            ctype = (info.control_type or "").strip()
        except Exception:
            ctype = ""
        if ctype not in _SELECTION_ITEM_TYPES:
            return False
        normalized = re.sub(r"[^a-z0-9]", "", name.lower())
        target = re.sub(r"[^a-z0-9]", "", value)
        return name.lower() == value or (normalized and normalized == target)

    def _option_list(self, info, out: list[str], seen: set[str] | None = None) -> None:
        """Collect the option names of a selection host into ``out``."""
        if seen is None:
            seen = set()
        try:
            ctype = (info.control_type or "").strip()
        except Exception:
            ctype = ""
        if ctype in _SELECTION_ITEM_TYPES or ctype in {"List", "ListBox"}:
            # Hosts (ComboBox / List / ListBox) carry their own label, not an
            # option; only leaf items contribute names to the option list.
            if ctype not in {"ComboBox", "List", "ListBox"}:
                try:
                    name = (info.name or "").strip()
                except Exception:
                    name = ""
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        try:
            children = info.children()
        except Exception:
            return
        for child in children:
            self._option_list(child, out, seen)

    def _option_key(self, handle: int, bbox: BBox) -> tuple[int, tuple[int, int, int, int]]:
        return (handle, (bbox.x, bbox.y, bbox.width, bbox.height))

    def cached_options(self, handle: int, bbox: BBox) -> list[str] | None:
        """Fresh cached option list for the field at ``bbox``, or None."""
        key = self._option_key(handle, bbox)
        entry = self._option_cache.get(key)
        if entry is None:
            return None
        ts, options = entry
        if time.monotonic() - ts > _OPTION_CACHE_TTL:
            return None
        return options

    def remember_options(self, handle: int, bbox: BBox, options: list[str]) -> None:
        """Store the option list read for the field so later selections on the
        same field (and the field map builder) reuse it without re-opening the
        dropdown (the 1.7-2.0 s per-field penalty being eliminated)."""
        self._option_cache[self._option_key(handle, bbox)] = (time.monotonic(), list(options))

    def select_option(self, handle: int, bbox: BBox, value: str, declared: list[str] | None = None) -> bool:
        """Select ``value`` in the dropdown / listbox under ``bbox`` directly.

        Order tries:
        1. a cached option list for the field (no dropdown open at all) with a
           direct ``SelectionItemPattern.Select()`` on the live item,
        2. a live ``SelectionItemPattern.Select()`` on the item under the box,
        3. ``ExpandCollapsePattern.Expand()`` then match-and-select the item,
           reading + caching the option list while the dropdown is open.

        Returns True when the selection fired without keyboard interaction;
        False when UIA cannot select the value (callers fall back to the
        arrow/type path). Never raises.
        """
        if not self._available or bbox is None:
            return False
        window = self._window_for(handle)
        if window is None:
            return False
        try:
            root = window.element_info
        except Exception as exc:
            logger.debug("select_option: no root info: {}", exc)
            return False
        value_str = str(value or "").strip()
        if not value_str:
            return False

        # 1) Direct select the live item under the box (already-open list,
        #    native combos whose list is a sibling). Prefer the deepest
        #    selection-capable element under the point; fall back to the root
        #    of the whole window subtree when nothing specific is found so
        #    items hanging off a popup still match.
        target = self._deepest_selection_info(root, *bbox.center) or root
        if self._select_item_by_name(target, value_str):
            self._cache_from_target(handle, bbox, target)
            return True

        # 2) ExpandCollapse path: open the dropdown, match + select, then
        #    collapse so the popup never covers the next field.
        expanded = self._expand_select(handle, bbox, target, value_str)
        if expanded:
            return True

        # 3) Everything failed - let the keyboard arrow/type path take over.
        logger.debug(
            "uia direct select failed value={!r} (no matching item/pattern)",
            value_str,
        )
        return False

    def _cache_from_target(self, handle: int, bbox: BBox, target) -> None:
        """Best-effort: cache the option list reachable from ``target``."""
        try:
            options: list[str] = []
            self._option_list(target, options)
            if options:
                self.remember_options(handle, bbox, options)
        except Exception:
            pass

    def _expand_select(self, handle: int, bbox: BBox, info, value: str) -> bool:
        """ExpandCollapsePattern.Expand() -> select -> Collapse()."""
        iface = self._element_pattern_iface(info, "ExpandCollapse")
        if iface is None:
            return False
        try:
            iface.Expand()
        except Exception as exc:
            logger.debug("uiExpand failed: {}", exc)
            return False
        try:
            # Let the popup materialize before matching (no-op when the tree
            # already had the items - matching is done once above).
            time.sleep(0.15)
            if self._select_item_by_name(info, value):
                self._cache_from_target(handle, bbox, info)
                return True
            return False
        finally:
            try:
                iface.Collapse()
            except Exception:
                pass

    def buttons(self, handle: int, nodes: list[UiaNode] | None = None) -> list[UiaNode]:
        """Button-like controls under ``handle``."""
        if nodes is None:
            nodes = self.descendants(handle)
        result = []
        for n in nodes:
            if n.control_type not in {"Button", "SplitButton", "MenuItem"}:
                continue
            if not n.name or not n.name.strip():
                continue
            if n.rect is None or n.rect.width <= 0 or n.rect.height <= 0:
                continue
            result.append(n)
        return result

    # -- geometry ------------------------------------------------------------

    @staticmethod
    def client_origin(handle: int) -> tuple[int, int]:
        """Absolute screen (x, y) of the window's client-area origin."""
        import win32gui

        try:
            return win32gui.ClientToScreen(handle, (0, 0))
        except Exception:
            return (0, 0)

    @staticmethod
    def client_size(handle: int) -> tuple[int, int]:
        """(width, height) of the window's client area."""
        import win32gui

        try:
            rect = win32gui.GetClientRect(handle)
            return (rect[2], rect[3])
        except Exception:
            return (0, 0)

    def scroll_into_view(self, node: UiaNode) -> UiaNode:
        """Best-effort ScrollItemPattern.ScrollIntoView + refreshed rect.
        
        Checks pattern availability before calling. Falls back to mouse wheel
        scrolling if UIA pattern is unavailable.
        """
        if not self._available:
            return node

        # Try UIA ScrollItemPattern first
        try:
            from comtypes.gen import UIAutomationClient

            info = self._element_info.from_point(*node.center)
            if info is not None:
                # Check if pattern is supported before calling
                pattern = info.element.GetCurrentPattern(UIAutomationClient.UIA_ScrollItemPatternId)
                if pattern is not None:
                    pattern.ScrollIntoView()
                    time.sleep(0.3)  # Wait for scroll to complete
                    refreshed = self.element_at(*node.center)
                    if refreshed is not None and refreshed.rect is not None:
                        return refreshed
        except Exception:
            pass  # Pattern not supported

        # Fallback: mouse wheel scroll using SendInput
        try:
            import win32api
            import win32con
            center_x, center_y = node.center if node.center else (0, 0)
            # Scroll down 3 times using win32api.mouse_event
            # Use MOUSEEVENTF_WHEEL (0x0800) with negative delta for scroll down
            for _ in range(3):
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -600)
                time.sleep(0.1)
            # Refresh position
            refreshed = self.element_at(center_x, center_y)
            if refreshed is not None:
                return refreshed
        except Exception as exc:
            logger.debug("mouse wheel scroll failed: {}", exc)

        return node

    # -- scroll containers -----------------------------------------------------
    # The outer window never scrolls; only internal panels do. These helpers
    # discover the real scrollable containers from the UIA tree and scroll each
    # one *directly* via its own ScrollPattern — never by emitting wheel events
    # over the window/desktop at random positions.

    def scroll_containers(
        self,
        handle: int,
        client_rect: tuple[int, int, int, int] | None = None,
    ) -> list[ScrollContainer]:
        """Recursively discover every vertically scrollable container.

        A control is registered as a scroll container when it is a container-ish
        type (Pane/List/Document/Custom/Grid/Tree/...) AND (a) it supports the
        UIA ScrollPattern, (b) it has a vertical ScrollBar child, or (c) its
        class name hints at a scroll region. Only controls big enough to be a
        panel, inside the client area, are kept. Returns them in tree order.
        """
        if not self._available:
            return []
        window = self._window_for(handle)
        if window is None:
            return []
        found: list[ScrollContainer] = []

        cache_key = (handle, client_rect)
        cached = self._scroll_container_cache.get(cache_key)
        if cached is not None:
            stamp, value = cached
            if time.time() - stamp < self._SCROLL_CONTAINER_CACHE_TTL:
                uia_logger.debug("scroll containers({}) -> {} cached", handle, len(value))
                return value

        def _walk(info) -> None:
            try:
                container = self._scroll_container_for(info, client_rect)
            except Exception as exc:
                logger.debug("scroll container inspect failed: {}", exc)
                container = None
            if container is not None:
                found.append(container)
            try:
                children = info.children()
            except Exception:
                children = []
            for child in children:
                try:
                    _walk(child)
                except Exception:
                    continue

        try:
            _walk(window.element_info)
        except Exception as exc:
            logger.debug("scroll container discovery failed: {}", exc)

        # Chrome-hosted content (the MPF app) exposes no ScrollPattern and no
        # ScrollBar, so the walk above finds nothing. Fall back to content
        # overflow: any container-ish node whose descendants extend below its
        # clip rect is a scrollable panel. Merge with anything already found.
        try:
            tree = self.dump_tree(handle)
            if isinstance(tree, dict) and "error" not in tree:
                overflow = discover_overflow_containers(tree, client_rect)
                found = self._merge_containers(found, overflow)
        except Exception as exc:
            logger.debug("overflow container discovery failed: {}", exc)
        self._scroll_container_cache[cache_key] = (time.time(), found)
        if len(self._scroll_container_cache) > 16:
            oldest = min(self._scroll_container_cache, key=lambda k: self._scroll_container_cache[k][0])
            self._scroll_container_cache.pop(oldest, None)
        uia_logger.debug("scroll containers({}) -> {} found", handle, len(found))
        return found

    @staticmethod
    def _merge_containers(
        existing: list[ScrollContainer], extra: list[ScrollContainer]
    ) -> list[ScrollContainer]:
        """Merge overflow-discovered containers without duplicating known ones.

        Two containers describe the same region when their rects coincide (the
        pattern/scrollbar-backed discovery is more authoritative, so it wins).
        """
        merged = list(existing)

        def _same_region(a: ScrollContainer, b: ScrollContainer) -> bool:
            if a.rect is None or b.rect is None:
                return False
            return (
                a.rect.left == b.rect.left
                and a.rect.top == b.rect.top
                and a.rect.right == b.rect.right
                and a.rect.bottom == b.rect.bottom
            )

        for candidate in extra:
            if candidate.rect is None:
                continue
            if any(_same_region(candidate, known) for known in merged):
                continue
            merged.append(candidate)
        return merged

    def _scroll_container_for(
        self,
        info,
        client_rect: tuple[int, int, int, int] | None,
    ) -> ScrollContainer | None:
        """Build a ScrollContainer for a UIA element if it can scroll vertically."""
        node = self._flatten(info)
        if node is None or node.rect is None:
            return None
        box = node.rect
        if box.width < _MIN_SCROLL_CONTAINER_W or box.height < _MIN_SCROLL_CONTAINER_H:
            return None
        if client_rect is not None:
            left, top, right, bottom = client_rect
            if box.right <= left or box.left >= right or box.bottom <= top or box.top >= bottom:
                return None
        pattern = self._scroll_pattern(info)
        has_scrollbar = self._has_vertical_scrollbar(info)
        class_hint = any(h in (node.class_name or "").lower() for h in _SCROLL_CLASS_HINTS)
        if pattern is None and not has_scrollbar and not class_hint:
            return None
        percent, view = self._scroll_percent(info)
        runtime_id = ()
        try:
            runtime_id = tuple(info.element.CurrentRuntimeId or ())
        except Exception:
            runtime_id = ()
        return ScrollContainer(
            name=node.name,
            control_type=node.control_type,
            automation_id=node.automation_id,
            class_name=node.class_name,
            framework_id=node.framework_id,
            handle=node.handle,
            rect=box,
            has_scroll_pattern=pattern is not None,
            vertical_scroll_percent=percent,
            vertical_view_size=view,
            runtime_id=runtime_id,
            parent=node.parent,
            _ref=info,
        )

    def container_state(self, container: ScrollContainer, handle: int | None = None) -> ScrollContainer:
        """Refresh a container's scroll position (percent / view size) from the
        live element, returning the same (mutated) container."""
        info = self._container_info(container, handle)
        if info is None:
            return container
        percent, view = self._scroll_percent(info)
        container.vertical_scroll_percent = percent
        container.vertical_view_size = view
        return container

    def scroll_container_pattern(self, container: ScrollContainer, pixels: int, handle: int | None = None) -> bool:
        """Scroll a container by ``pixels`` down via its UIA ScrollPattern.

        Only the given container's own scrollbar is touched - never the window,
        never the desktop. ``pixels`` is translated into an absolute scroll
        percent using the container's view size and on-screen height so it lands
        in the requested band instead of jumping to the end. Returns True if the
        pattern was invoked successfully.
        """
        info = self._container_info(container, handle)
        if info is None:
            return False
        pattern = self._scroll_pattern(info)
        if pattern is None:
            return False
        try:
            from comtypes.gen import UIAutomationClient

            pixels = max(1, int(pixels))
            view_size = float(getattr(pattern, "CurrentVerticalViewSize", 0.0) or 0.0)
            percent = float(getattr(pattern, "CurrentVerticalScrollPercent", 0.0) or 0.0)
            view_size = float(getattr(pattern, "CurrentVerticalViewSize", 0.0) or 0.0)
            percent = float(getattr(pattern, "CurrentVerticalScrollPercent", 0.0) or 0.0)
            try:
                box = container.rect
                rect_h = box.height if box is not None else 0
            except Exception:
                rect_h = 0
            if view_size > 0 and rect_h > 0:
                content_h = rect_h / (view_size / 100.0)  # full scrollable height, px
                delta = (pixels / content_h) * 100.0
                target = min(100.0, max(0.0, percent + delta))
                pattern.SetScrollPercent(-1.0, float(target))
            else:
                # No geometry to translate pixels: one viewport increment.
                pattern.Scroll(
                    UIAutomationClient.ScrollAmount_NoAmount,
                    UIAutomationClient.ScrollAmount_LargeIncrement,
                )
            time.sleep(0.2)
            return True
        except Exception as exc:
            logger.debug("scroll_container_pattern failed: {}", exc)
            return False

    def container_scrollbar(self, container: ScrollContainer, handle: int | None = None) -> UiaNode | None:
        """The vertical ScrollBar child of a container (for click/drag fallbacks),
        or None when the container exposes no separate scrollbar control."""
        info = self._container_info(container, handle)
        if info is None:
            return None
        try:
            children = info.children()
        except Exception:
            return None
        for child in children:
            try:
                if (child.control_type or "") != "ScrollBar":
                    continue
                if self._scrollbar_is_vertical(child):
                    return self._flatten(child)
            except Exception:
                continue
        return None

    # -- scroll helpers (internal) -------------------------------------------

    @staticmethod
    def _scroll_pattern(info):
        """The live, typed ``IUIAutomationScrollPattern`` of an element, or None.

        ``GetCurrentPattern`` hands back an untyped COM pointer even when the
        element genuinely supports scrolling - it must be QueryInterface'd
        into the typed interface before any member access works. Returning
        the raw pointer here (as before) made every caller believe the
        element "has" a scroll pattern when in fact every real use of it
        (``.Scroll``, ``.CurrentVerticalViewSize``, ...) would silently fail.
        """
        try:
            from comtypes.gen import UIAutomationClient

            raw = info.element.GetCurrentPattern(UIAutomationClient.UIA_ScrollPatternId)
            if not raw:
                return None
            return raw.QueryInterface(UIAutomationClient.IUIAutomationScrollPattern)
        except Exception:
            return None

    @staticmethod
    def _scroll_percent(info) -> tuple[float | None, float | None]:
        """(vertical_scroll_percent, vertical_view_size) of a scrollable, or (None, None)."""
        pattern = UiaBackend._scroll_pattern(info)
        if pattern is None:
            return None, None
        try:
            return float(pattern.CurrentVerticalScrollPercent), float(pattern.CurrentVerticalViewSize)
        except Exception:
            return None, None

    def _has_vertical_scrollbar(self, info) -> bool:
        try:
            children = info.children()
        except Exception:
            children = []
        for child in children:
            try:
                if (child.control_type or "") == "ScrollBar" and self._scrollbar_is_vertical(child):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _scrollbar_is_vertical(info) -> bool:
        try:
            orientation = getattr(info, "orientation", None)
            if orientation is not None:
                return str(orientation).lower() in {"vertical", "small"}
        except Exception:
            pass
        try:
            r = info.rectangle
            if r is not None:
                w = max(0, r.right - r.left)
                h = max(0, r.bottom - r.top)
                if w > 0 and h > w:  # tall & narrow -> vertical scrollbar
                    return True
        except Exception:
            pass
        return True  # a lone ScrollBar child is assumed vertical

    def _container_info(self, container: ScrollContainer, handle: int | None):
        """Resolve the live element for a container: prefer its stored ref,
        else re-find it by runtime id in the tree (handles UIA refresh)."""
        if container._ref is not None:
            try:
                _ = container._ref.name  # is the reference still valid?
                return container._ref
            except Exception:
                container._ref = None
        if handle is None or not self._available:
            return None
        window = self._window_for(handle)
        if window is None:
            return None
        if not container.runtime_id:
            return None
        target = tuple(container.runtime_id)

        def _find(info):
            try:
                rid = tuple(info.element.CurrentRuntimeId or ())
                if rid == target:
                    return info
            except Exception:
                pass
            try:
                for child in info.children():
                    found = _find(child)
                    if found is not None:
                        return found
            except Exception:
                pass
            return None

        found = _find(window.element_info)
        if found is not None:
            container._ref = found
        return found

    # -- diagnostics ---------------------------------------------------------

    def dump_tree(self, handle: int) -> dict[str, Any]:
        """Serialisable UIA tree for ``debug/mpf/uia_tree.json``."""
        if not self._available:
            return {"available": False}
        window = self._window_for(handle)
        if window is None:
            return {"available": True, "error": "window not found"}
        try:
            return self._tree_dict(window.element_info)
        except Exception as exc:
            return {"available": True, "error": str(exc)}

    def inspectable_nodes(self, handle: int) -> list[UiaNode]:
        """Every node whose control type we can act on, recursing the full tree.

        Unlike :meth:`descendants` (pywinauto's flat walk), this recurses
        explicitly through ``children()`` so controls nested inside Panes,
        Customs, Groups and Tables are still found.
        """
        if not self._available:
            return []
        window = self._window_for(handle)
        if window is None:
            return []
        nodes: list[UiaNode] = []

        def _walk(info) -> None:
            node = self._flatten(info)
            if node is not None and node.control_type in INSPECTABLE_CONTROL_TYPES:
                nodes.append(node)
            try:
                children = info.children()
            except Exception:
                children = []
            for child in children:
                try:
                    _walk(child)
                except Exception:
                    continue

        try:
            _walk(window.element_info)
        except Exception as exc:
            logger.debug("inspectable_nodes({}) failed: {}", handle, exc)
        nodes.sort(key=lambda n: (n.rect.top, n.rect.left) if n.rect else (10**9, 10**9))
        return nodes

    def dump_diagnostics(self, handle: int, out_dir: str | Path) -> dict[str, Any]:
        """Write the full UIA diagnostic set to ``debug/uia/``.

        Files:
          window.json            - handle / title / pid / exe / class / thread / client area
          tree.json              - full recursive UIA tree
          controls.json          - every inspectable control, flattened
          editable_controls.json - the editable form widgets
          labels.json            - static text nodes (source-panel labels)
          focus.json             - the currently focused element
          bounding_boxes.json    - name/type/bbox for every boxed node

        Returns a summary dict (control counts) for the caller.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        summary: dict[str, Any] = {"handle": handle}

        nodes = self.descendants(handle)
        editable = self.editable_fields(handle)
        text = self.text_nodes(handle)
        boxes = [n for n in nodes if n.rect is not None]

        window_info = self._window_json(handle)
        self._write_json(out / "window.json", window_info)
        self._write_json(out / "tree.json", self.dump_tree(handle))
        self._write_json(out / "controls.json", {"controls": [n.to_dict() for n in nodes], "count": len(nodes)})
        self._write_json(
            out / "editable_controls.json",
            {"editable_controls": [n.to_dict() for n in editable], "count": len(editable)},
        )
        self._write_json(out / "labels.json", {"labels": [n.to_dict() for n in text], "count": len(text)})
        focus = self.focused()
        self._write_json(out / "focus.json", focus.to_dict() if focus else None)
        self._write_json(
            out / "bounding_boxes.json",
            {"bounding_boxes": [_bbox_dict(n) for n in boxes], "count": len(boxes)},
        )

        summary.update(
            {
                "window": window_info,
                "controls": len(nodes),
                "editable_controls": len(editable),
                "labels": len(text),
                "bounding_boxes": len(boxes),
            }
        )
        logger.info(
            "UIA diagnostics written to {} ({} controls, {} editable)",
            out,
            len(nodes),
            len(editable),
        )
        return summary

    def _window_json(self, handle: int) -> dict[str, Any]:
        import win32gui

        def _safe(fn: Any) -> Any:
            try:
                return fn()
            except Exception:
                return None

        title = _safe(lambda: win32gui.GetWindowText(handle)) or ""
        class_name = _safe(lambda: win32gui.GetClassName(handle)) or ""
        thread_id, pid = 0, 0
        try:
            thread_id, pid = win32gui.GetWindowThreadProcessId(handle)
        except Exception:
            pass
        left, top, width, height = self.client_origin(handle)[0], self.client_origin(handle)[1], *self.client_size(handle)
        return {
            "handle": handle,
            "title": title,
            "class_name": class_name,
            "process_id": pid,
            "thread_id": thread_id,
            "executable": self._executable_for_pid(pid),
            "client_area": {"left": left, "top": top, "width": width, "height": height},
            "generated_at": time.strftime("%Y%m%d-%H%M%S"),
        }

    @staticmethod
    def _executable_for_pid(pid: int) -> str:
        if pid <= 0:
            return ""
        try:
            import psutil

            return psutil.Process(pid).name() or ""
        except Exception:
            return ""

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # -- internals -----------------------------------------------------------

    def _window_for(self, handle: int):
        try:
            return self._desktop.window(handle=handle)
        except Exception as exc:
            logger.debug("desktop.window({}) failed: {}", handle, exc)
            return None

    def _flatten(self, info) -> UiaNode | None:
        try:
            rect = info.rectangle
        except Exception:
            rect = None
        box = None
        if rect is not None:
            try:
                width = max(0, rect.right - rect.left)
                height = max(0, rect.bottom - rect.top)
                box = BBox(int(rect.left), int(rect.top), int(width), int(height))
            except Exception:
                box = None
        try:
            name = info.name or ""
        except Exception:
            name = ""
        try:
            control_type = info.control_type or ""
        except Exception:
            control_type = ""
        try:
            automation_id = info.automation_id or ""
        except Exception:
            automation_id = ""
        try:
            handle = info.handle
        except Exception:
            handle = None
        try:
            class_name = info.class_name or ""
        except Exception:
            class_name = ""
        try:
            enabled = bool(info.enabled)
        except Exception:
            enabled = True
        try:
            visible = bool(info.visible)
        except Exception:
            visible = True
        value: str | None = None
        if control_type in _VALUE_CONTROL_TYPES:
            try:
                if hasattr(info, "value_pattern"):  # test doubles / fakes
                    value = info.value_pattern().current_value()
                else:
                    raw = getattr(info, "element", None)
                    if raw is not None:
                        from pywinauto.uia_defines import get_elem_interface

                        value = get_elem_interface(raw, "Value").CurrentValue
            except Exception:
                value = None
        # Additional UIA properties (Step 2).
        framework_id = ""
        try:
            framework_id = info.framework_id or ""
        except Exception:
            framework_id = ""
        is_keyboard_focusable = False
        try:
            is_keyboard_focusable = bool(info.is_keyboard_focusable)
        except Exception:
            is_keyboard_focusable = False
        patterns: list[str] = []
        try:
            patterns = self._extract_patterns(info)
        except Exception:
            patterns = []
        parent: dict[str, Any] | None = None
        try:
            parent_info = info.parent
            if parent_info is not None:
                parent = {
                    "name": getattr(parent_info, "name", "") or "",
                    "control_type": getattr(parent_info, "control_type", "") or "",
                    "automation_id": getattr(parent_info, "automation_id", "") or "",
                }
        except Exception:
            parent = None
        children: list[dict[str, Any]] = []
        try:
            for child in info.children():
                children.append({
                    "name": getattr(child, "name", "") or "",
                    "control_type": getattr(child, "control_type", "") or "",
                    "automation_id": getattr(child, "automation_id", "") or "",
                })
        except Exception:
            children = []
        return UiaNode(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            class_name=class_name,
            handle=handle,
            rect=box,
            value=value,
            enabled=enabled,
            visible=visible,
            framework_id=framework_id,
            is_keyboard_focusable=is_keyboard_focusable,
            patterns=patterns,
            parent=parent,
            children=children,
        )

    @staticmethod
    def _extract_patterns(info) -> list[str]:
        """Best-effort extraction of the UIA patterns supported by an element."""
        patterns: list[str] = []
        # pywinauto exposes patterns via element_info.element.GetSupportedPatterns().
        try:
            element = info.element
            supported = element.GetSupportedPatterns()
            for pattern in supported:
                try:
                    name = str(pattern)
                    # comtypes GUIDs -> readable names.
                    if "Pattern" in name:
                        patterns.append(name.split(".")[-1].split(" ")[0])
                except Exception:
                    continue
        except Exception:
            pass
        return patterns

    def _tree_dict(self, info) -> dict[str, Any]:
        try:
            name = info.name or ""
        except Exception:
            name = ""
        try:
            control_type = info.control_type or ""
        except Exception:
            control_type = ""
        try:
            auto_id = info.automation_id or ""
        except Exception:
            auto_id = ""
        try:
            handle = info.handle
        except Exception:
            handle = None
        try:
            class_name = info.class_name or ""
        except Exception:
            class_name = ""
        try:
            framework_id = info.framework_id or ""
        except Exception:
            framework_id = ""
        try:
            enabled = bool(info.enabled)
        except Exception:
            enabled = True
        try:
            r = info.rectangle
            rect = [int(r.left), int(r.top), int(r.right), int(r.bottom)]
        except Exception:
            rect = None
        try:
            patterns = self._extract_patterns(info)
        except Exception:
            patterns = []
        node: dict[str, Any] = {
            "name": name,
            "control_type": control_type,
            "automation_id": auto_id,
            "class_name": class_name,
            "framework_id": framework_id,
            "handle": handle,
            "enabled": enabled,
            "rect": rect,
            "patterns": patterns,
        }
        try:
            children = info.children()
        except Exception:
            children = []
        kids = []
        for child in children:
            try:
                kids.append(self._tree_dict(child))
            except Exception:
                continue
        if kids:
            node["children"] = kids
        return node


def _bbox_dict(node: UiaNode) -> dict[str, Any]:
    """Compact bbox entry for a UIA node."""
    return {
        "name": node.name,
        "control_type": node.control_type,
        "automation_id": node.automation_id,
        "bbox": node.rect.to_dict() if node.rect else None,
        "editable": node.editable,
    }


__all__ = [
    "UiaBackend",
    "UiaNode",
    "ScrollContainer",
    "discover_overflow_containers",
    "EDITABLE_CONTROL_TYPES",
    "TEXT_CONTROL_TYPES",
    "INSPECTABLE_CONTROL_TYPES",
    "SCROLL_CONTAINER_TYPES",
]
