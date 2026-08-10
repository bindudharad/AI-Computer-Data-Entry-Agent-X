"""Unit tests for the universal agent data models."""

from __future__ import annotations

from atlas.universal.models import (
    AttachmentMode,
    BrowserHealthState,
    Capability,
    CandidateTarget,
    TargetEnvironment,
    TargetLock,
    TargetSession,
)


def test_environment_enum_values() -> None:
    assert TargetEnvironment.CHROME_BROWSER.value == "CHROME_BROWSER"
    assert TargetEnvironment.ELECTRON.value == "ELECTRON"
    assert TargetEnvironment.UNKNOWN.value == "UNKNOWN"


def test_attachment_mode_distinguishes_existing_from_new() -> None:
    existing = {AttachmentMode.EXISTING_WINDOW, AttachmentMode.EXISTING_BROWSER_TAB, AttachmentMode.EXISTING_CDP}
    assert AttachmentMode.NEW_LAUNCH not in existing
    assert AttachmentMode.USER_ATTACH not in existing


def test_browser_health_disconnected_is_not_missing() -> None:
    assert BrowserHealthState.DISCONNECTED != BrowserHealthState.MISSING


def test_candidate_target_to_dict_roundtrip() -> None:
    c = CandidateTarget(
        title="Portal - Create Record",
        url="https://portal.example.com/records/new",
        origin="portal.example.com",
        process_id=1234,
        window_handle=999,
        executable="chrome.exe",
        environment=TargetEnvironment.CHROME_BROWSER,
        has_cdp=True,
        dom_available=True,
        source="tab",
    )
    data = c.to_dict()
    assert data["environment"] == "CHROME_BROWSER"
    assert data["url"] == "https://portal.example.com/records/new"
    assert data["process_id"] == 1234
    assert data["has_cdp"] is True


def test_target_session_capabilities_serialise() -> None:
    session = TargetSession(
        environment=TargetEnvironment.ELECTRON,
        capabilities={Capability.DOM, Capability.CDP, Capability.UIA},
        attachment_mode=AttachmentMode.EXISTING_WINDOW,
        attached=True,
        healthy=True,
    )
    data = session.to_dict()
    assert sorted(data["capabilities"]) == ["CDP", "DOM", "UIA"]
    assert data["attachment_mode"] == "EXISTING_WINDOW"
    assert data["attached"] is True


def test_target_lock_roundtrip() -> None:
    lock = TargetLock(hwnd=77, pid=500, process_name="msedge.exe", window_title="App", browser="msedge",
                      origin="app.example.com", environment=TargetEnvironment.EDGE_BROWSER)
    data = lock.to_dict()
    assert data["hwnd"] == 77
    assert data["pid"] == 500
    assert data["environment"] == "EDGE_BROWSER"
    assert data["browser"] == "msedge"
