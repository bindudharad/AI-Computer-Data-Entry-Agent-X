#!/usr/bin/env python3
"""Diagnose the window attachment strategy chain without running the loop.

Usage:
    python diagnose_mpf_attach.py [--title "MPF"] [--all] [--log-level DEBUG]
    python diagnose_mpf_attach.py --self-test

The script walks the exact strategy chain ``WindowAttacher.attach_by_title``
uses, emitting the same ``[ATTACH]`` / ``[WINDOW]`` / ``[UIA]`` / ``[TARGET]``
trace lines:

    A  HWND attachment           (the matching top-level window itself)
    C  raw UIA root from HWND    (ElementFromHandle)
    D  descendant UIA traversal  (probe every child window for editable fields)
    E  focused-element discovery (walk the focused element's ancestry)

``--self-test`` runs the Win32 pid-recovery helper against live windows and a
simulated pid=0 wrapper, so the chain can be validated even when the MPF
window is not open.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.core.logging import setup_logging
from atlas.observe.window import AttachError, WindowAttacher
from atlas.vision.capture import WindowCapture


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose the MPF window-attachment strategy chain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--title", default="MPF", help="window title substring (default MPF)")
    parser.add_argument("--all", action="store_true", help="also list every visible top-level window")
    parser.add_argument("--self-test", action="store_true", help="run the Win32/pid self-test only")
    parser.add_argument("--out", default="debug/mpf", help="directory for the debug snapshot")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    return parser


def _all_windows() -> list[dict]:
    """Every visible top-level window with pid/class/title (for --all)."""
    import win32gui

    found: list[dict] = []

    def _collect(handle: int, _: object = None) -> bool:
        try:
            if not win32gui.IsWindowVisible(handle):
                return True
            title = win32gui.GetWindowText(handle) or ""
            cls = win32gui.GetClassName(handle) or ""
            tid, pid = WindowAttacher._winapi_query_window(handle)
            found.append({"hwnd": handle, "title": title, "class": cls, "pid": pid, "tid": tid})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_collect, None)
    return found


def _self_test() -> int:
    """Validate pid recovery without needing the MPF window open."""
    import win32gui

    print("=" * 60)
    print("Win32 pid-recovery self-test")
    print("=" * 60)

    handle = win32gui.GetForegroundWindow()
    tid, pid = WindowAttacher._winapi_query_window(handle)
    resolver = WindowAttacher(WindowCapture())
    print(f"foreground hwnd={hex(handle)} -> tid={tid} pid={pid} "
          f"({resolver._resolve(handle).executable})")
    ok_foreground = pid > 0

    # Simulated pid=0 wrapper: force the native lookup to report (tid, 0) so
    # only the OpenThread -> GetProcessIdOfThread fallback can resolve it.
    import atlas.observe.window as window_mod

    real_lookup = window_mod._native_get_window_thread_process_id
    real_thread = window_mod._native_pid_from_thread

    window_mod._native_get_window_thread_process_id = lambda h: (tid, 0)
    window_mod._native_pid_from_thread = lambda t: pid
    try:
        tid2, pid2 = WindowAttacher._winapi_query_window(handle)
    finally:
        window_mod._native_get_window_thread_process_id = real_lookup
        window_mod._native_pid_from_thread = real_thread

    recovered = pid2 == pid and pid2 > 0
    print(f"simulated pid=0 wrapper  -> recovered pid={pid2} (expect {pid})")
    print(f"  OpenThread fallback used: {recovered}")

    # Control: a 0 handle must not crash the helper.
    tid3, pid3 = WindowAttacher._winapi_query_window(0)
    print(f"hwnd=0 safe path        -> tid={tid3} pid={pid3}")

    return 0 if (ok_foreground and recovered and pid3 == 0) else 1


def main() -> int:
    args = _build_parser().parse_args()
    setup_logging(args.log_level, Path(args.out) / "logs")
    Path(args.out).mkdir(parents=True, exist_ok=True)

    if args.self_test:
        return _self_test()

    print("=" * 60)
    print(f"Window-attachment strategy chain (title={args.title!r})")
    print("=" * 60)

    attacher = WindowAttacher(WindowCapture())

    if args.all:
        print("\nAll visible top-level windows:")
        for w in _all_windows():
            exe = WindowAttacher._executable_for(w["pid"])[0]
            print(f"  hwnd={hex(w['hwnd'])} pid={w['pid']} exe={exe or '?'} "
                  f"class={w['class']!r} title={w['title']!r}")

    matches = attacher._match_window(args.title)
    print(f"\nWindows matching {args.title!r}: {len(matches)}")
    for m in matches:
        exe = (m.get("exe_path") or m.get("executable") or "").split("\\")[-1]
        print(f"  [WINDOW] hwnd={hex(m['handle'])} pid={m['process_id']} "
              f"process={exe or '?'} class={m['class_name']!r} title={m['title']!r}")

    if not matches:
        print("\nNo matching window found. Open the MPF (Download and Upload Form)")
        print("window first and re-run, or use --all to see what IS visible.")
        return 2

    print("\nRunning the full attach chain (A -> C -> D -> E):")
    seen: set[int] = set()
    for candidate in matches:
        target = attacher._resolve(candidate["handle"])
        discovered = attacher._discover_ui_root(target)
        if discovered is None:
            print(f"  hwnd={hex(target.handle)} title={target.title!r} -> no UI root")
            continue
        if discovered.handle in seen:
            continue
        seen.add(discovered.handle)
        try:
            attacher._verify_and_attach(discovered)
        except AttachError as exc:
            print(f"  hwnd={hex(discovered.handle)} -> rejected: {exc}")
            continue
        editable = attacher._count_editable(discovered.handle)
        rect = WindowAttacher._canvas_rect(discovered.handle)
        print(f"  [ATTACHED] {editable} editable control(s) found")
        print(f"  [TARGET] hwnd={hex(discovered.handle)} pid={discovered.process_id} "
              f"class={discovered.class_name!r} title={discovered.title!r} rect={rect}")
        return 0

    print("\nEvery candidate was rejected (no editable controls anywhere).")
    print("Open the MPF window, click inside a field, then re-run -- the")
    print("focused-element strategy will locate the real form container.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
