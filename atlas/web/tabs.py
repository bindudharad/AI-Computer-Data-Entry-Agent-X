"""CDP tab enumeration.

Lists the open page tabs of running Chromium/Electron browsers via the CDP HTTP
endpoint ``/json/list``. The pure parser (``parse_tab_list``) is fully
unit-testable; ``discover_tabs`` adds the real network + process lookups.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atlas.config import load_config
from atlas.core.logging import logger
from atlas.web.browser_discovery import BrowserDiscovery
from atlas.web.cdp import HttpGet, cdp_endpoint, _default_http_get


@dataclass
class BrowserTab:
    title: str
    url: str
    tab_index: int
    browser: str = ""
    pid: int = 0
    type: str = "page"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "tab_index": self.tab_index,
            "browser": self.browser,
            "pid": self.pid,
            "type": self.type,
        }


def parse_tab_list(payload: str, *, browser: str = "", pid: int = 0) -> list[BrowserTab]:
    """Parse a ``/json/list`` payload into page tabs (pure)."""
    try:
        entries = json.loads(payload)
    except Exception as exc:
        logger.debug("[TABS] invalid /json/list payload: {}", exc)
        return []
    if not isinstance(entries, list):
        return []
    tabs: list[BrowserTab] = []
    index = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "page":
            continue
        tabs.append(
            BrowserTab(
                title=entry.get("title") or "",
                url=entry.get("url") or "",
                tab_index=index,
                browser=browser,
                pid=pid,
                type="page",
            )
        )
        index += 1
    return tabs


def _query_port(port: int, http_get: HttpGet | None, browser: str, pid: int) -> list[BrowserTab]:
    fetcher = http_get or _default_http_get
    try:
        payload = fetcher(f"{cdp_endpoint(port)}/json/list", 1.5)
        return parse_tab_list(payload, browser=browser, pid=pid)
    except Exception as exc:
        logger.debug("[TABS] port {} not reachable: {}", port, exc)
        return []


def discover_tabs(
    ports: list[int] | None = None,
    http_get: HttpGet | None = None,
    discovery: BrowserDiscovery | None = None,
) -> list[dict[str, Any]]:
    """List page tabs across the given CDP ports.

    ``ports`` defaults to the configured ``CDP_PORTS``; each port is mapped to
    its owning browser process (for ``browser`` / ``pid`` attribution) via
    ``BrowserDiscovery``. Returns a list of tab dicts for the detector.
    """
    if ports is None:
        try:
            ports = load_config().universal.cdp_ports
        except Exception:
            ports = []
    if not ports:
        return []

    discoverer = discovery or BrowserDiscovery()
    browsers = discoverer.find_browsers()
    port_to_pid = {b.cdp_port: b.pid for b in browsers if b.cdp_port}
    port_to_name = {b.cdp_port: b.name for b in browsers if b.cdp_port}

    tabs: list[dict[str, Any]] = []
    for port in ports:
        pid = port_to_pid.get(int(port), 0)
        name = port_to_name.get(int(port), "")
        for tab in _query_port(int(port), http_get, name, pid):
            tabs.append(tab.to_dict())
    return tabs


__all__ = ["discover_tabs", "parse_tab_list", "BrowserTab"]
