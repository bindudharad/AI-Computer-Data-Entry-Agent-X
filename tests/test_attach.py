"""Regression tests for the window-attachment strategy chain.

Covers the touch-free attach path added for Electron/Chromium-hosted forms:

- pid=0 wrapper recovery (``_winapi_query_window``)
- strategy chain A/C/D/E (HWND -> raw UIA root -> child windows ->
  focused-element discovery)
- accepting a window whose editable controls live in a child window
- [ATTACH] / [WINDOW] / [UIA] / [TARGET] trace logging
- rejecting windows with no editable controls

All tests fake the Win32 + UIA layers; no live window is required.
"""

from __future__ import annotations

import pytest

from atlas.core.logging import logger
from atlas.observe.window import AttachError, WindowAttacher, WindowTarget
from atlas.vision.capture import WindowCapture

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_attacher() -> WindowAttacher:
    return WindowAttacher(WindowCapture())


def _target(handle: int, title: str = "MPF (Download and Upload Form)", pid: int = 4242) -> WindowTarget:
    return WindowTarget(
        handle=handle,
        title=title,
        process_id=pid,
        executable="mpf.exe",
        exe_path="C:\\App\\mpf.exe",
        class_name="Chrome_WidgetWin_1",
        thread_id=7,
    )


class FakeUiaBackend:
    """Deterministic stand-in for ``UiaBackend``.

    ``best_editable_root`` simulates the real probe of handle + child windows:
    it returns the first window in ``[handle, *children[handle]]`` that has
    editable controls.
    """

    available = True

    def __init__(self, editable_by_handle: dict[int, int], focus: dict | None = None,
                 controls_by_handle: dict[int, int] | None = None,
                 children: dict[int, list[int]] | None = None) -> None:
        self._editable_by_handle = editable_by_handle
        self._focus = focus
        self._controls = controls_by_handle or {}
        self._children = children or {}

    def best_editable_root(self, handle: int) -> dict | None:
        for probe in [handle, *self._children.get(handle, [])]:
            count = self._editable_by_handle.get(probe, 0)
            if count > 0:
                return {"hwnd": probe, "count": count}
        return None

    def control_counts(self, handle: int) -> dict:
        return {
            "controls": self._controls.get(handle, 0),
            "editable_controls": self._editable_by_handle.get(handle, 0),
        }

    def focused_element_and_chain(self) -> dict:
        return dict(self._focus or {})


class FakeCapture:
    def attach(self, handle: int, title: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Win32 pid recovery
# ---------------------------------------------------------------------------


def test_winapi_query_window_safe_zero_handle() -> None:
    assert WindowAttacher._winapi_query_window(0) == (0, 0)


def test_winapi_query_window_returns_tid_and_pid(monkeypatch) -> None:
    called: list[int] = []

    def _fake(handle: int) -> tuple[int, int]:
        called.append(handle)
        return (99, 4321)

    monkeypatch.setattr("atlas.observe.window._native_get_window_thread_process_id", _fake)
    tid, pid = WindowAttacher._winapi_query_window(1234)
    assert (tid, pid) == (99, 4321)
    assert called == [1234]


def test_winapi_query_window_recovers_real_pid_when_wrapper_is_zero(monkeypatch) -> None:
    """A wrapper that reports pid=0 must fall back to the thread -> process."""
    monkeypatch.setattr(
        "atlas.observe.window._native_get_window_thread_process_id",
        lambda handle: (55, 0),  # tid=55, pid=0 -> wrapper
    )
    monkeypatch.setattr(
        "atlas.observe.window._native_pid_from_thread",
        lambda tid: 7777,
    )
    tid, pid = WindowAttacher._winapi_query_window(999)
    assert tid == 55
    assert pid == 7777


def test_winapi_query_window_returns_zero_when_lookup_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "atlas.observe.window._native_get_window_thread_process_id",
        lambda handle: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert WindowAttacher._winapi_query_window(999) == (0, 0)


# ---------------------------------------------------------------------------
# Strategy E: focused-element discovery
# ---------------------------------------------------------------------------


def test_attach_resolves_root_from_focused_element(monkeypatch) -> None:
    """The user clicked a field already -> the focused element's ancestry wins."""
    focus = {
        "focused": {"name": "Full Name", "control_type": "Edit", "editable": True},
        "chain": [{"name": "Full Name", "control_type": "Edit", "editable": True}],
        "hwnd_roots": [2222],
    }
    backend = FakeUiaBackend(editable_by_handle={2222: 6}, focus=focus)
    _patch_ui_backend(monkeypatch, backend)

    verified: list[int] = []

    def _fake_verify(target: WindowTarget) -> None:
        verified.append(target.handle)

    attacher = _make_attacher()
    monkeypatch.setattr(attacher, "_verify_and_attach", _fake_verify)
    _patch_win32(monkeypatch, {
        1111: _target(1111, pid=0),
        2222: _target(2222, pid=7777),
    })
    _patch_query_window(monkeypatch)

    result = attacher.attach_by_title("MPF")
    assert result.handle == 2222
    assert verified == [2222]


def test_attach_focused_skips_when_focus_not_editable(monkeypatch) -> None:
    """A focused Button (not editable) must not hijack attachment."""
    focus = {
        "focused": {"name": "Upload", "control_type": "Button", "editable": False},
        "chain": [{"name": "Upload", "control_type": "Button", "editable": False}],
        "hwnd_roots": [2222],
    }
    backend = FakeUiaBackend(editable_by_handle={1111: 3}, focus=focus)
    _patch_ui_backend(monkeypatch, backend)

    attacher = _make_attacher()
    monkeypatch.setattr(attacher, "_verify_and_attach", lambda t: None)
    _patch_win32(monkeypatch, {1111: _target(1111, pid=5555)})
    _patch_query_window(monkeypatch)

    result = attacher.attach_by_title("MPF")
    assert result.handle == 1111  # fell through to the HWND/UIA strategies


# ---------------------------------------------------------------------------
# Strategies C + D: child-window editable resolution
# ---------------------------------------------------------------------------


def test_attach_accepts_window_whose_editables_live_in_child(monkeypatch) -> None:
    """Top-level hwnd=1111 has no editable controls but child hwnd=3333 does.

    The resolver must not reject 1111; it resolves to 3333 (the real form
    container, as in Chromium's Chrome_RenderWidgetHostHWND).
    """
    # best_editable_root probes handle + children; simulate the child finding.
    backend = FakeUiaBackend(
        editable_by_handle={1111: 0, 3333: 12},
        focus=None,
        children={1111: [3333]},
    )
    _patch_ui_backend(monkeypatch, backend)

    verified: list[int] = []
    attacher = _make_attacher()
    monkeypatch.setattr(attacher, "_verify_and_attach",
                        lambda t: verified.append(t.handle))
    _patch_win32(monkeypatch, {
        1111: _target(1111, pid=0),
        3333: _target(3333, title="", pid=8888),
    })
    _patch_query_window(monkeypatch)

    result = attacher.attach_by_title("MPF")
    assert result.handle == 3333
    assert verified == [3333]
    assert result.title == "MPF (Download and Upload Form)"  # parent title kept


def test_attach_skips_rejected_candidate_and_takes_next(monkeypatch) -> None:
    """The first candidate is rejected during verify; the second is attached."""
    backend = FakeUiaBackend(editable_by_handle={1111: 1, 5555: 4}, focus=None)
    _patch_ui_backend(monkeypatch, backend)

    attacher = _make_attacher()
    handles: list[int] = []

    def _verify(target: WindowTarget) -> None:
        if target.handle == 1111:
            raise AttachError("mock reject: no editable controls")
        handles.append(target.handle)

    monkeypatch.setattr(attacher, "_verify_and_attach", _verify)
    _patch_win32(monkeypatch, {
        1111: _target(1111, pid=0),
        5555: _target(5555, pid=9000),
    })
    _patch_query_window(monkeypatch)

    result = attacher.attach_by_title("MPF")
    assert result.handle == 5555
    assert handles == [5555]


def test_attach_rejects_all_and_raises_no_match(monkeypatch) -> None:
    backend = FakeUiaBackend(editable_by_handle={1111: 0}, focus=None,
                             controls_by_handle={1111: 0})
    _patch_ui_backend(monkeypatch, backend)

    attacher = _make_attacher()
    _patch_win32(monkeypatch, {1111: _target(1111, pid=0)})
    _patch_query_window(monkeypatch)

    with pytest.raises(AttachError, match="matched title"):
        attacher.attach_by_title("MPF")


def test_attach_emits_attach_target_uia_trace(monkeypatch) -> None:
    """The strategy chain logs [ATTACH] / [WINDOW] / [TARGET] / [ATTACHED]."""
    backend = FakeUiaBackend(editable_by_handle={1111: 3}, focus=None)
    _patch_ui_backend(monkeypatch, backend)

    attacher = _make_attacher()
    monkeypatch.setattr(attacher, "_verify_and_attach", lambda t: None)
    _patch_win32(monkeypatch, {1111: _target(1111, pid=4444)})
    _patch_query_window(monkeypatch)

    sink: list[str] = []
    record_id = logger.add(sink.append, format="{message}")
    try:
        attacher.attach_by_title("MPF")
    finally:
        logger.remove(record_id)

    joined = "\n".join(sink)
    assert "[ATTACH]" in joined
    assert "[WINDOW]" in joined
    assert "[TARGET]" in joined
    assert "[ATTACHED]" in joined
    assert "pid=" in joined
    assert "rect=(0, 0, 1024, 768)" in joined


# ---------------------------------------------------------------------------
# _verify_target_tree acceptance rules
# ---------------------------------------------------------------------------


def test_verify_target_tree_accepts_editable_root(monkeypatch) -> None:
    backend = FakeUiaBackend(editable_by_handle={1111: 4}, focus=None)
    _patch_ui_backend(monkeypatch, backend)
    attacher = _make_attacher()
    attacher._verify_target_tree(_target(1111))  # must not raise


def test_verify_target_tree_rejects_no_controls(monkeypatch) -> None:
    backend = FakeUiaBackend(editable_by_handle={1111: 0}, focus=None,
                             controls_by_handle={1111: 0})
    _patch_ui_backend(monkeypatch, backend)
    attacher = _make_attacher()
    with pytest.raises(AttachError, match="exposes no UIA"):
        attacher._verify_target_tree(_target(1111))


def test_verify_target_tree_rejects_controls_but_no_editables(monkeypatch) -> None:
    backend = FakeUiaBackend(editable_by_handle={1111: 0}, focus=None,
                             controls_by_handle={1111: 25})
    _patch_ui_backend(monkeypatch, backend)
    attacher = _make_attacher()
    with pytest.raises(AttachError, match="no editable fields"):
        attacher._verify_target_tree(_target(1111))


# ---------------------------------------------------------------------------
# Patching helpers
# ---------------------------------------------------------------------------


def _patch_ui_backend(monkeypatch, backend: FakeUiaBackend) -> None:
    from atlas.observe.uia import UiaBackend

    monkeypatch.setattr(UiaBackend, "_instance", backend)
    monkeypatch.setattr(UiaBackend, "instance", classmethod(lambda cls: backend))


def _patch_query_window(monkeypatch) -> None:
    pids = {1111: 0, 2222: 7777, 3333: 8888, 5555: 9000}

    def _fake(handle: int) -> tuple[int, int]:
        return 7, pids.get(int(handle or 0), 0)

    monkeypatch.setattr(WindowAttacher, "_winapi_query_window", staticmethod(_fake))
    monkeypatch.setattr(WindowAttacher, "_executable_for",
                        staticmethod(lambda pid: ("mpf.exe", "C:\\App\\mpf.exe") if pid else ("", "")))


def _patch_win32(monkeypatch, targets: dict[int, WindowTarget]) -> None:
    """Simulate a desktop with exactly the given windows visible."""
    import win32gui

    def _enum(callback, _: object) -> None:
        for t in targets.values():
            callback(t.handle, None)

    def _visible(handle: int) -> bool:
        return handle in targets

    def _title(handle: int) -> str:
        return targets.get(handle, _target(0)).title

    def _class(handle: int) -> str:
        return targets.get(handle, _target(0)).class_name

    def _is_window(handle: int) -> bool:
        return handle in targets or handle == 0

    def _rect(handle: int) -> tuple[int, int, int, int]:
        return (0, 0, 1024, 768)

    monkeypatch.setattr(win32gui, "EnumWindows", _enum)
    monkeypatch.setattr(win32gui, "IsWindowVisible", _visible)
    monkeypatch.setattr(win32gui, "GetWindowText", _title)
    monkeypatch.setattr(win32gui, "GetClassName", _class)
    monkeypatch.setattr(win32gui, "IsWindow", _is_window)
    monkeypatch.setattr(win32gui, "GetWindowRect", _rect)
    monkeypatch.setattr(WindowAttacher, "_get_ancestor", staticmethod(lambda h, f: None))
    monkeypatch.setattr(WindowAttacher, "_canvas_rect",
                        staticmethod(lambda h: (0, 0, 1024, 768)))
