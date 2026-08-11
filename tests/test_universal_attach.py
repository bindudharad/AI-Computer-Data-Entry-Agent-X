"""Unit tests for the attach-first manager decision engine (cases A-H)."""

from __future__ import annotations

from atlas.universal.attach import AttachFirstManager
from atlas.universal.detector import UniversalTargetDetector
from atlas.universal.models import CandidateTarget, TargetEnvironment
from atlas.universal.restart_policy import RestartMode, RestartPolicy
from tests.test_universal_detector import FakeProcesses, FakeWin32


def _browser_window(title: str, handle: int = 101, pid: int = 42, foreground: bool = False) -> dict:
    return {"handle": handle, "title": title, "class_name": "Chrome_WidgetWin_1",
            "pid": pid, "rect": (0, 0, 1280, 900)}


def _manager(win32=None, processes=None, mode: RestartMode = RestartMode.ON_CRASH_ONLY,
             auto: bool = True) -> AttachFirstManager:
    detector = UniversalTargetDetector(win32=win32 or FakeWin32([]),
                                       processes=processes or FakeProcesses())
    return AttachFirstManager(detector=detector, restart_policy=RestartPolicy(mode=mode, auto_launch_target=auto))


def test_case_a_attach_existing_browser_window() -> None:
    win32 = FakeWin32([_browser_window("Portal - Create Record", handle=101, pid=42)], foreground=101)
    manager = _manager(win32=win32)
    decision = manager.plan()
    assert decision.case == "A"
    assert decision.action == "ATTACH_EXISTING"
    assert decision.launch is False
    assert decision.candidate is not None
    assert decision.candidate.environment == TargetEnvironment.CHROME_BROWSER


def test_case_g_attach_existing_desktop_window() -> None:
    win32 = FakeWin32([
        {"handle": 55, "title": "Enterprise Data Entry", "class_name": "Win32WindowClass",
         "pid": 9, "rect": (0, 0, 800, 600)},
    ], foreground=55)
    manager = _manager(win32=win32)
    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    assert decision.candidate.environment == TargetEnvironment.DESKTOP_UIA


def test_case_f_wait_when_nothing_found_and_launch_disallowed() -> None:
    manager = _manager(win32=FakeWin32([]), processes=FakeProcesses([]), mode=RestartMode.ON_CRASH_ONLY, auto=False)
    decision = manager.plan()
    assert decision.case == "F"
    assert decision.action == "WAIT"
    assert decision.launch is False


def test_case_f_launch_when_policy_allows() -> None:
    manager = _manager(win32=FakeWin32([]), processes=FakeProcesses([]), mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.case == "F"
    assert decision.action == "LAUNCH"
    assert decision.launch is True


def test_case_e_browser_alive_but_no_cdp_never_launches() -> None:
    # A browser process exists with NO --remote-debugging-port and no window title.
    processes = FakeProcesses([{"pid": 77, "name": "chrome.exe",
                                "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "about:blank"]}])
    win32 = FakeWin32([], foreground=0)
    manager = _manager(win32=win32, processes=processes, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.case == "E"
    assert decision.action == "BROWSER_UIA"
    assert decision.launch is False


def test_case_b_browser_with_cdp_attach_tab() -> None:
    # Browser running WITH a debugging port => CDP is available => attach tab.
    processes = FakeProcesses([{"pid": 88, "name": "chrome.exe",
                                "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "--remote-debugging-port=9222"]}])
    manager = _manager(win32=FakeWin32([]), processes=processes, mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    assert decision.action == "ATTACH_EXISTING"
    assert decision.launch is False
    assert decision.case in {"A", "B"}


def test_execute_dispatches_to_injected_factories() -> None:
    win32 = FakeWin32([_browser_window("Portal", foreground=True)])
    manager = _manager(win32=win32)
    decision = manager.plan()
    calls: list[str] = []

    def attach_web(candidate) -> str:
        calls.append("web")
        return "web-adapter"

    def attach_desktop(candidate) -> str:
        calls.append("desktop")
        return "desktop-adapter"

    adapter = manager.execute(decision, attach_web=attach_web, attach_desktop=attach_desktop)
    assert adapter == "web-adapter"
    assert calls == ["web"]


def test_execute_launch_only_when_permitted() -> None:
    manager = _manager(win32=FakeWin32([]), processes=FakeProcesses([]), mode=RestartMode.AUTO, auto=True)
    decision = manager.plan()
    calls: list[str] = []

    def launch_web() -> str:
        calls.append("launch")
        return "launched"

    manager.execute(decision, launch_web=launch_web)
    assert calls == ["launch"]


def test_connection_loss_disconnected_never_launches() -> None:
    manager = _manager()
    decision = manager.decide_connection_loss(process_alive=True, cdp_available=False)
    assert decision.case == "E"
    assert decision.action == "BROWSER_UIA"
    assert decision.launch is False


def test_connection_loss_missing_launches_when_policy_allows() -> None:
    manager = _manager(mode=RestartMode.AUTO, auto=True)
    decision = manager.decide_connection_loss(process_alive=False)
    assert decision.case == "F"
    assert decision.action == "LAUNCH"
    assert decision.launch is True


def test_connection_loss_missing_blocks_when_policy_disallows() -> None:
    manager = _manager(mode=RestartMode.ON_CRASH_ONLY, auto=False)
    decision = manager.decide_connection_loss(process_alive=False)
    assert decision.action == "WAIT"
    assert decision.launch is False


def test_connection_loss_recovered_reattaches() -> None:
    manager = _manager()
    decision = manager.decide_connection_loss(process_alive=True, cdp_available=True)
    assert decision.case == "A"
    assert decision.action == "ATTACH_EXISTING"
