"""Unit tests for the universal target detector.

All tests inject fake win32 / process accessors; no live desktop is touched.
"""

from __future__ import annotations

from atlas.universal.detector import RankingPreferences, UniversalTargetDetector
from atlas.universal.models import TargetEnvironment


class FakeWin32:
    def __init__(self, windows, foreground: int = 0) -> None:
        self._windows = windows
        self._foreground = foreground

    def foreground(self) -> int:
        return self._foreground

    def visible_windows(self) -> list[dict]:
        return list(self._windows)

    def is_iconic(self, handle: int) -> bool:
        return False


class FakeProcesses:
    def __init__(self, browsers=None) -> None:
        self._browsers = browsers or []

    def browser_processes(self) -> list[dict]:
        return list(self._browsers)

    def process_name(self, pid: int) -> str:
        for b in self._browsers:
            if b["pid"] == pid:
                return b["name"]
        return ""

    def process_alive(self, pid: int) -> bool:
        return False


def _chrome_window(title: str = "Portal - Create Record", handle: int = 101, pid: int = 42,
                   cls: str = "Chrome_WidgetWin_1") -> dict:
    return {"handle": handle, "title": title, "class_name": cls, "pid": pid,
            "rect": (0, 0, 1280, 900)}


def _detector(win32=None, processes=None) -> UniversalTargetDetector:
    return UniversalTargetDetector(win32=win32 or FakeWin32([]),
                                   processes=processes or FakeProcesses())


def test_discovers_window_candidate() -> None:
    win32 = FakeWin32([_chrome_window()], foreground=101)
    detector = _detector(win32=win32)
    candidates = detector.discover()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.window_handle == 101
    assert c.process_id == 42
    assert c.environment == TargetEnvironment.CHROME_BROWSER


def test_skips_shell_and_empty_title_windows() -> None:
    win32 = FakeWin32([
        {"handle": 1, "title": "", "class_name": "Chrome_WidgetWin_1", "pid": 7, "rect": (0, 0, 1, 1)},
        {"handle": 2, "title": "Desktop", "class_name": "Progman", "pid": 0, "rect": (0, 0, 1, 1)},
        {"handle": 3, "title": "Taskbar", "class_name": "Shell_TrayWnd", "pid": 0, "rect": (0, 0, 1, 1)},
    ])
    candidates = _detector(win32=win32).discover()
    assert len(candidates) == 0


def test_browser_process_candidate() -> None:
    processes = FakeProcesses([{"pid": 99, "name": "chrome.exe", "exe": "C:\\Chrome\\chrome.exe",
                                "cmdline": ["chrome.exe", "--remote-debugging-port=9222"]}])
    detector = _detector(processes=processes)
    candidates = detector.discover()
    assert any(c.process_id == 99 and c.source == "process" for c in candidates)


def test_url_from_cmdline() -> None:
    assert UniversalTargetDetector._url_from_cmdline(["chrome.exe", "https://portal.example.com/records/new"]) \
        == "https://portal.example.com/records/new"


def test_origin_from_url() -> None:
    assert UniversalTargetDetector._origin("https://Portal.Example.com/x/y") == "portal.example.com"


def test_foreground_window_ranks_highest() -> None:
    win32 = FakeWin32([
        _chrome_window(title="Other page", handle=101, pid=42),
        _chrome_window(title="Portal - Create Record", handle=202, pid=43),
    ], foreground=202)
    prefs = RankingPreferences(title_hint="Portal")
    detector = _detector(win32=win32)
    candidates = detector.discover(prefs)
    detector._rank(candidates, prefs)
    best = max(candidates, key=lambda c: (c.score, c.confidence))
    assert best.window_handle == 202


def test_domain_hint_boosts_matching_candidate() -> None:
    from atlas.universal.detector import W_URL_DOMAIN

    prefs = RankingPreferences(url_hint="https://portal.example.com/")
    assert prefs.domain_hint == "portal.example.com"

    candidates = [
        _to_candidate("c1", origin="portal.example.com"),
        _to_candidate("c2", origin="other.example.com"),
    ]
    s1 = UniversalTargetDetector._score_candidate(candidates[0], prefs, prefs.domain_hint)
    s2 = UniversalTargetDetector._score_candidate(candidates[1], prefs, prefs.domain_hint)
    assert s1 - s2 == W_URL_DOMAIN


def test_hidden_and_minimized_penalised() -> None:
    from atlas.universal.detector import W_HIDDEN, W_MINIMIZED

    base = _to_candidate("base", title="Target App")
    base.is_hidden = True
    base.is_minimized = True
    prefs = RankingPreferences()
    score = UniversalTargetDetector._score_candidate(base, prefs, "")
    assert score <= -1
    assert W_HIDDEN < 0 and W_MINIMIZED < 0


def test_dedupe_merges_window_and_tab_for_same_pid() -> None:
    win = _to_candidate("win", title="Portal", process_id=42, source="window")
    win.window_handle = 101
    tab = _to_candidate("tab", title="Portal", process_id=42, source="tab")
    tab.dom_available = True
    detector = _detector()
    merged = detector._dedupe([win, tab])
    assert len(merged) == 1
    assert merged[0].dom_available is True
    assert merged[0].source == "window+tab"
    assert merged[0].window_handle == 101


def test_detect_returns_none_when_nothing_rankable() -> None:
    detector = _detector(win32=FakeWin32([]), processes=FakeProcesses([]))
    assert detector.detect() is None


def _to_candidate(cid: str, title: str = "", process_id: int = 0, origin: str = "",
                  source: str = "window") -> "object":
    from atlas.universal.models import CandidateTarget

    return CandidateTarget(title=title, origin=origin, process_id=process_id,
                           environment=TargetEnvironment.DESKTOP_UIA, source=source)
