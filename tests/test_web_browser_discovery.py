"""Unit tests for browser process discovery."""

from __future__ import annotations

from atlas.web.browser_discovery import BrowserDiscovery, BrowserProcess, parse_browsers, parse_cdp_port


def _proc(pid: int, name: str, cmdline: list[str] | None = None) -> dict:
    return {"pid": pid, "name": name, "exe": f"C:\\Browsers\\{name}", "cmdline": cmdline or []}


def test_parse_cdp_port_with_flag_value() -> None:
    assert parse_cdp_port(["chrome.exe", "--remote-debugging-port=9229"]) == 9229


def test_parse_cdp_port_bare_flag_defaults() -> None:
    assert parse_cdp_port(["chrome.exe", "--remote-debugging-port"]) == 9222


def test_parse_cdp_port_none_when_absent() -> None:
    assert parse_cdp_port(["chrome.exe", "about:blank"]) is None


def test_parse_browsers_filters_non_browsers() -> None:
    result = parse_browsers([
        _proc(1, "chrome.exe", ["chrome.exe"]),
        _proc(2, "notepad.exe"),
        _proc(3, "MSEDGE.EXE", ["msedge.exe", "--remote-debugging-port=9333"]),
    ])
    assert [r.pid for r in result] == [1, 3]
    assert result[1].cdp_port == 9333


def test_parse_browsers_skips_zero_pid_and_duplicates() -> None:
    result = parse_browsers([
        _proc(0, "chrome.exe"),
        _proc(5, "chrome.exe"),
        _proc(5, "chrome.exe"),
    ])
    assert [r.pid for r in result] == [5]


def test_browser_process_has_cdp_property() -> None:
    assert BrowserProcess(pid=1, name="chrome", exe_path="", cdp_port=9222).has_cdp is True
    assert BrowserProcess(pid=1, name="chrome", exe_path="").has_cdp is False


def test_discovery_with_injected_accessor() -> None:
    discovery = BrowserDiscovery(process_accessor=lambda: [
        _proc(11, "firefox.exe", ["firefox.exe"]),
        _proc(12, "chrome.exe", ["chrome.exe", "--remote-debugging-port=9222"]),
    ])
    browsers = discovery.find_browsers()
    assert [b.pid for b in browsers] == [11, 12]
    assert discovery.pid_to_browser(11) == "firefox.exe"


def test_discovery_accessor_failure_returns_empty() -> None:
    discovery = BrowserDiscovery(process_accessor=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert discovery.find_browsers() == []
