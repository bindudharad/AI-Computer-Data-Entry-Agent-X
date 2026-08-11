"""Browser process discovery.

Finds running browser/Electron processes and the CDP debugging endpoints they
expose. A browser may be running even when no CDP port is open (started without
``--remote-debugging-port``) - that is ``DISCONNECTED``, never ``MISSING``.

The heavy lifting is in the pure ``parse_browsers`` so it is unit-testable
without psutil; ``BrowserDiscovery`` only feeds real process data into it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atlas.core.logging import logger

#: Executable names (case-insensitive, with and without extension) that are
#: automation targets via CDP.
_BROWSER_EXE_NAMES = {
    "chrome", "chrome.exe",
    "msedge", "msedge.exe",
    "brave", "brave.exe",
    "opera", "opera.exe",
    "chromium", "chromium.exe",
    "firefox", "firefox.exe",
    "electron", "electron.exe",
}

_DEBUG_FLAG = "--remote-debugging-port"
_DEFAULT_DEBUG_PORT = 9222


@dataclass
class BrowserProcess:
    pid: int
    name: str
    exe_path: str
    cdp_port: int | None = None

    @property
    def has_cdp(self) -> bool:
        return bool(self.cdp_port)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "exe_path": self.exe_path,
            "cdp_port": self.cdp_port,
            "has_cdp": self.has_cdp,
        }


def parse_cdp_port(cmdline: list[str]) -> int | None:
    """Extract the debugging port from a browser command line (or None)."""
    for token in cmdline or []:
        if token == _DEBUG_FLAG:
            return _DEFAULT_DEBUG_PORT
        if token.startswith(_DEBUG_FLAG + "="):
            value = token.split("=", 1)[1].strip()
            if value.isdigit():
                return int(value)
    return None


def parse_browsers(processes: list[dict[str, Any]]) -> list[BrowserProcess]:
    """Convert raw process dicts ``{pid, name, exe, cmdline}`` into targets.

    Pure function - the full test surface for browser discovery.
    """
    out: list[BrowserProcess] = []
    seen: set[int] = set()
    for info in processes:
        try:
            name = (info.get("name") or "").lower()
            if name not in _BROWSER_EXE_NAMES:
                continue
            pid = int(info.get("pid") or 0)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append(
                BrowserProcess(
                    pid=pid,
                    name=name,
                    exe_path=info.get("exe") or "",
                    cdp_port=parse_cdp_port(info.get("cmdline") or []),
                )
            )
        except Exception:
            continue
    return out


class BrowserDiscovery:
    """Enumerate running browsers via psutil (injectable accessor)."""

    def __init__(self, process_accessor: Any = None) -> None:
        #: ``process_accessor() -> iterable of {pid,name,exe,cmdline}``
        self._accessor = process_accessor

    def find_browsers(self) -> list[BrowserProcess]:
        if self._accessor is not None:
            try:
                return parse_browsers(list(self._accessor()))
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("[WEB] browser discovery accessor failed: {}", exc)
                return []
        try:
            import psutil

            raw = []
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
                raw.append({
                    "pid": proc.info.get("pid") or 0,
                    "name": proc.info.get("name") or "",
                    "exe": proc.info.get("exe") or "",
                    "cmdline": list(proc.info.get("cmdline") or []),
                })
            return parse_browsers(raw)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[WEB] psutil unavailable: {}", exc)
            return []

    def pid_to_browser(self, pid: int) -> str:
        for proc in self.find_browsers():
            if proc.pid == pid:
                return proc.name
        return ""


__all__ = ["BrowserDiscovery", "BrowserProcess", "parse_browsers", "parse_cdp_port"]
