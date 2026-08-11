"""Unit tests for the universal application classifier."""

from __future__ import annotations

from atlas.universal.classifier import ApplicationClassifier
from atlas.universal.models import Capability, TargetEnvironment


def _classify(**kwargs) -> tuple[TargetEnvironment, str, set[Capability]]:
    return ApplicationClassifier().classify(**kwargs)


def test_chrome_browser_by_executable() -> None:
    env, framework, caps = _classify(executable="chrome.exe", url="https://portal.example.com/")
    assert env == TargetEnvironment.CHROME_BROWSER
    assert framework == "chromium"
    assert Capability.KEYBOARD in caps and Capability.MOUSE in caps


def test_edge_browser() -> None:
    env, _, _ = _classify(executable="msedge.exe", url="https://outlook.office.com/")
    assert env == TargetEnvironment.EDGE_BROWSER


def test_firefox_browser() -> None:
    env, _, _ = _classify(executable="firefox.exe")
    assert env == TargetEnvironment.FIREFOX_BROWSER


def test_electron_desktop() -> None:
    env, framework, caps = _classify(executable="myapp.exe", class_name="Chrome_WidgetWin_1",
                                     uia_available=True)
    assert env == TargetEnvironment.ELECTRON
    assert framework == "electron"
    assert Capability.UIA in caps


def test_desktop_uia_application() -> None:
    env, _, caps = _classify(executable="enterprise.exe", class_name="Window Class 1", uia_available=True)
    assert env == TargetEnvironment.DESKTOP_UIA
    assert Capability.UIA in caps


def test_url_alone_infers_web_browser() -> None:
    env, _, _ = _classify(url="https://portal.example.com/records/new")
    assert env in {TargetEnvironment.WEB_BROWSER, TargetEnvironment.CHROME_BROWSER}


def test_dom_and_cdp_flags_surface_as_capabilities() -> None:
    _, _, caps = _classify(executable="chrome.exe", url="https://x.example.com",
                           dom_available=True, cdp_available=True)
    assert Capability.DOM in caps
    assert Capability.CDP in caps


def test_empty_input_is_unknown() -> None:
    env, _, caps = _classify()
    assert env == TargetEnvironment.UNKNOWN
    assert Capability.VISION in caps  # vision is always a last-resort channel
