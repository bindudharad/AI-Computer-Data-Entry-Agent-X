"""CDP connection helpers.

Small HTTP helpers over the Chrome DevTools Protocol's HTTP endpoints
(``/json/version`` and ``/json/list``). All network access goes through an
injectable ``http_get`` so the parsing logic is unit-testable offline.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable

from atlas.core.logging import logger

HttpGet = Callable[[str, float], str]


def cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def _default_http_get(url: str, timeout: float = 1.5) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_json(url: str, http_get: HttpGet | None, timeout: float = 1.5) -> dict | None:
    fetcher = http_get or _default_http_get
    try:
        payload = fetcher(url, timeout)
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("[CDP] {} -> {}", url, exc)
        return None


def cdp_version(port: int, http_get: HttpGet | None = None) -> dict[str, Any]:
    """Return ``/json/version`` info for a debugging port ({} on failure)."""
    data = _fetch_json(f"{cdp_endpoint(port)}/json/version", http_get)
    return data or {}


def cdp_available(port: int, http_get: HttpGet | None = None) -> bool:
    """True when a real browser is listening on the debugging port."""
    version = cdp_version(port, http_get)
    return bool(version.get("Browser")) and bool(version.get("webSocketDebuggerUrl"))


__all__ = ["cdp_endpoint", "cdp_version", "cdp_available", "HttpGet"]
