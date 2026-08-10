"""Unit tests for CDP HTTP helpers with a fake transport."""

from __future__ import annotations

from atlas.web import cdp


def _fake_get(payload: str, raise_on: str = "") -> cdp.HttpGet:
    def _get(url: str, timeout: float = 1.5) -> str:
        if raise_on and raise_on in url:
            raise RuntimeError(f"unreachable: {url}")
        return payload

    return _get


def test_cdp_endpoint_format() -> None:
    assert cdp.cdp_endpoint(9222) == "http://127.0.0.1:9222"


def test_cdp_version_parses() -> None:
    payload = '{"Browser": "Chrome/120.0", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}'
    info = cdp.cdp_version(9222, http_get=_fake_get(payload))
    assert info["Browser"] == "Chrome/120.0"
    assert "webSocketDebuggerUrl" in info


def test_cdp_version_returns_empty_on_failure() -> None:
    assert cdp.cdp_version(9999, http_get=_fake_get("", raise_on="/json/version")) == {}


def test_cdp_available_true_with_valid_version() -> None:
    payload = '{"Browser": "Chrome/120.0", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}'
    assert cdp.cdp_available(9222, http_get=_fake_get(payload)) is True


def test_cdp_available_false_without_websocket() -> None:
    payload = '{"Browser": "Chrome/120.0"}'
    assert cdp.cdp_available(9222, http_get=_fake_get(payload)) is False


def test_cdp_available_false_on_unreachable() -> None:
    assert cdp.cdp_available(9999, http_get=_fake_get("", raise_on="/json/version")) is False
