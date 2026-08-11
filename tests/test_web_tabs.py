"""Unit tests for CDP tab enumeration."""

from __future__ import annotations

from atlas.web.tabs import BrowserTab, discover_tabs, parse_tab_list

_PAYLOAD = """[
  {"id": "1", "type": "page", "title": "Portal - Create Record",
   "url": "https://portal.example.com/records/new", "webSocketDebuggerUrl": "ws://x"},
  {"id": "2", "type": "page", "title": "Settings",
   "url": "https://portal.example.com/settings", "webSocketDebuggerUrl": "ws://y"},
  {"id": "3", "type": "service_worker", "title": "", "url": "chrome-extension://x",
   "webSocketDebuggerUrl": "ws://z"}
]"""


def test_parse_tab_list_keeps_only_page_tabs() -> None:
    tabs = parse_tab_list(_PAYLOAD, browser="chrome", pid=42)
    assert len(tabs) == 2
    assert tabs[0].title == "Portal - Create Record"
    assert tabs[0].url == "https://portal.example.com/records/new"
    assert tabs[0].tab_index == 0
    assert tabs[1].tab_index == 1
    assert tabs[0].browser == "chrome"
    assert tabs[0].pid == 42


def test_parse_tab_list_invalid_payload_returns_empty() -> None:
    assert parse_tab_list("not json") == []
    assert parse_tab_list('{"not": "a list"}') == []
    assert parse_tab_list("[]") == []


def test_browser_tab_to_dict() -> None:
    tab = BrowserTab(title="T", url="https://x.example.com", tab_index=3, browser="msedge", pid=7)
    data = tab.to_dict()
    assert data["tab_index"] == 3
    assert data["type"] == "page"


def test_discover_tabs_uses_injected_http_and_discovery(monkeypatch) -> None:
    def _get(url: str, timeout: float = 1.5) -> str:
        return _PAYLOAD

    class _Discovery:
        def find_browsers(self):
            from atlas.web.browser_discovery import BrowserProcess

            return [BrowserProcess(pid=42, name="chrome.exe", exe_path="", cdp_port=9222)]

        def pid_to_browser(self, pid: int) -> str:
            return "chrome.exe"

    tabs = discover_tabs(ports=[9222], http_get=_get, discovery=_Discovery())
    assert len(tabs) == 2
    assert tabs[0]["browser"] == "chrome.exe"
    assert tabs[0]["pid"] == 42


def test_discover_tabs_no_ports_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr("atlas.web.tabs.load_config", lambda: _NoPortsConfig())
    assert discover_tabs(ports=[]) == []


class _NoPortsConfig:
    @property
    def cdp_ports(self) -> list[int]:
        return []
