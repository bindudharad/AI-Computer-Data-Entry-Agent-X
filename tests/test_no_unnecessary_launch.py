"""Mandatory regression: an EXISTING browser/target is NEVER launched again.

This is the core bug the universal agent fixed. A new process must never be
spawned when any browser or target already exists - even when:
  * CDP is unavailable (disconnected, not missing),
  * the attach call fails or returns nothing,
  * the target tab is not found,
  * the connection drops mid-run, or
  * AUTO_LAUNCH_TARGET is enabled.

Only genuine CASE F (nothing exists anywhere) may launch, and only when the
restart policy permits it.
"""

from __future__ import annotations

import pytest

from atlas.universal.attach import AttachFirstManager
from atlas.universal.detector import UniversalTargetDetector
from atlas.universal.models import CandidateTarget, TargetEnvironment
from atlas.universal.restart_policy import RestartMode, RestartPolicy
from tests.test_universal_detector import FakeProcesses, FakeWin32


def _browser_window(title: str, handle: int = 101, pid: int = 42) -> dict:
    return {"handle": handle, "title": title, "class_name": "Chrome_WidgetWin_1",
            "pid": pid, "rect": (0, 0, 1280, 900)}


def _manager(win32=None, processes=None, mode: RestartMode = RestartMode.AUTO,
             auto: bool = True) -> AttachFirstManager:
    detector = UniversalTargetDetector(win32=win32 or FakeWin32([]),
                                       processes=processes or FakeProcesses())
    # These tests target the attach decision engine, not tab discovery; skip the
    # slow live CDP port probes (would cost ~1.5s x configured ports each).
    detector._candidates_from_tabs = lambda prefs: []  # type: ignore[method-assign]
    return AttachFirstManager(detector=detector,
                              restart_policy=RestartPolicy(mode=mode, auto_launch_target=auto))


def _launch_spy():
    calls: list[str] = []

    def launch_web() -> str:
        calls.append("launch")
        return "launched"

    return calls, launch_web


def test_existing_browser_with_cdp_never_calls_launch() -> None:
    processes = FakeProcesses([{"pid": 88, "name": "chrome.exe", "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "--remote-debugging-port=9222"]}])
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([_browser_window("Portal")]), processes=processes)
    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    manager.execute(decision, attach_web=lambda c: "web-adapter", launch_web=launch_web)
    assert calls == []


def test_browser_window_without_cdp_never_calls_launch() -> None:
    # Chrome is running (process + window) but has NO debugging port. The window
    # handle means we can still attach (via UIA) -> ATTACH_EXISTING, not relaunch.
    processes = FakeProcesses([{"pid": 77, "name": "chrome.exe", "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "about:blank"]}])
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([_browser_window("Portal - Create Record")]),
                       processes=processes, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    assert decision.launch is False
    manager.execute(decision, attach_browser_uia=lambda c: "uia-adapter", launch_web=launch_web)
    assert calls == []


def test_browser_process_alive_without_cdp_never_calls_launch() -> None:
    # No window at all, but the browser process is alive with no CDP channel.
    # Disconnected, NOT missing -> BROWSER_UIA, never a fresh launch.
    processes = FakeProcesses([{"pid": 77, "name": "chrome.exe", "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "about:blank"]}])
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([]), processes=processes, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.case == "E"
    assert decision.action == "BROWSER_UIA"
    assert decision.launch is False
    manager.execute(decision, attach_browser_uia=lambda c: "uia-adapter", launch_web=launch_web)
    assert calls == []


def test_attach_failure_never_falls_through_to_launch() -> None:
    # Existing browser target, but the web attach adapter FAILS. The launch
    # factory must still never be called - fail loud, do not relaunch.
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([_browser_window("Portal")]))

    def failing_attach(candidate):
        raise RuntimeError("CDP connect refused")

    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    with pytest.raises(RuntimeError, match="CDP connect refused"):
        manager.execute(decision, attach_web=failing_attach, launch_web=launch_web)
    assert calls == []


def test_attach_returning_none_never_falls_through_to_launch() -> None:
    # Attach adapter returns None (no adapter created) - still no launch.
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([_browser_window("Portal")]))
    decision = manager.plan()
    manager.execute(decision, attach_web=lambda c: None, launch_web=launch_web)
    assert calls == []


def test_existing_browser_with_no_matching_tab_never_launches() -> None:
    # Browser process alive with CDP, but no candidate has a usable window/tab
    # for attach. plan() must degrade to WAIT, never LAUNCH.
    processes = FakeProcesses([{"pid": 66, "name": "chrome.exe", "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "--remote-debugging-port=9223"]}])
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([]), processes=processes, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.action in {"WAIT", "ATTACH_EXISTING"}
    assert decision.launch is False
    assert decision.case != "F" or decision.launch is False
    manager.execute(decision, launch_web=launch_web)
    assert calls == []


def test_connection_loss_disconnected_never_launches() -> None:
    # Mid-run disconnect with the process still alive must never relaunch.
    calls, launch_web = _launch_spy()
    manager = _manager(mode=RestartMode.AUTO, auto=True)
    decision = manager.decide_connection_loss(process_alive=True, cdp_available=False)
    assert decision.case == "E"
    assert decision.launch is False
    manager.execute(decision, attach_browser_uia=lambda c: None, launch_web=launch_web)
    assert calls == []


def test_desktop_target_never_launches() -> None:
    # A desktop window is present -> ATTACH_EXISTING via desktop, no launch.
    win32 = FakeWin32([
        {"handle": 55, "title": "Enterprise Data Entry", "class_name": "Win32WindowClass",
         "pid": 9, "rect": (0, 0, 800, 600)},
    ], foreground=55)
    calls, launch_web = _launch_spy()
    manager = _manager(win32=win32, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    assert decision.launch is False
    manager.execute(decision, attach_desktop=lambda c: "desktop-adapter", launch_web=launch_web)
    assert calls == []


def test_launch_only_possible_when_nothing_exists() -> None:
    # The ONLY scenario allowed to launch: CASE F, and only with policy backing.
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([]), processes=FakeProcesses([]),
                       mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.case == "F"
    assert decision.launch is True
    assert decision.action == "LAUNCH"
    assert decision.candidate is None
    manager.execute(decision, launch_web=launch_web)
    assert calls == ["launch"]


def test_launch_blocked_when_policy_disallows_even_if_nothing_exists() -> None:
    # Nothing exists AND launch disallowed -> WAIT, launch factory untouched.
    calls, launch_web = _launch_spy()
    manager = _manager(win32=FakeWin32([]), processes=FakeProcesses([]),
                       mode=RestartMode.ON_CRASH_ONLY, auto=False)
    decision = manager.plan()
    assert decision.case == "F"
    assert decision.action == "WAIT"
    manager.execute(decision, launch_web=launch_web)
    assert calls == []


def test_electron_target_never_launches() -> None:
    # Electron/Chromium desktop app running -> attach existing, never relaunch.
    win32 = FakeWin32([{
        "handle": 202, "title": "MyApp", "class_name": "Chrome_WidgetWin_1",
        "pid": 31, "rect": (0, 0, 900, 700),
    }], foreground=202)
    processes = FakeProcesses([{"pid": 31, "name": "myapp.exe", "exe": "C:\\Apps\\myapp.exe",
                                "cmdline": ["myapp.exe"]}])
    calls, launch_web = _launch_spy()
    manager = _manager(win32=win32, processes=processes, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    assert decision.launch is False
    manager.execute(decision, attach_desktop=lambda c: "desktop-adapter", launch_web=launch_web)
    assert calls == []
