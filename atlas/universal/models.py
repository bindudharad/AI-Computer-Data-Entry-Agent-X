"""Universal target data models.

Describes what ATLAS is attached to and how it got there. These models are
target-agnostic: a web tab, an Electron window, a Win32 form and an OCR-only
screen all collapse into one :class:`TargetSession`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TargetEnvironment(str, Enum):
    """Classification of the automation target's hosting environment."""

    WEB_BROWSER = "WEB_BROWSER"
    DESKTOP_UIA = "DESKTOP_UIA"
    ELECTRON = "ELECTRON"
    CHROMIUM_DESKTOP = "CHROMIUM_DESKTOP"
    FIREFOX_BROWSER = "FIREFOX_BROWSER"
    EDGE_BROWSER = "EDGE_BROWSER"
    CHROME_BROWSER = "CHROME_BROWSER"
    GENERIC_DESKTOP = "GENERIC_DESKTOP"
    UNKNOWN = "UNKNOWN"


class AttachmentMode(str, Enum):
    """How the target was reached."""

    EXISTING_WINDOW = "EXISTING_WINDOW"
    EXISTING_BROWSER_TAB = "EXISTING_BROWSER_TAB"
    EXISTING_CDP = "EXISTING_CDP"
    USER_ATTACH = "USER_ATTACH"
    NEW_LAUNCH = "NEW_LAUNCH"


class BrowserHealthState(str, Enum):
    """Distinguishes a missing browser from a disconnected one."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class Capability(str, Enum):
    """Automation channels available on a target."""

    DOM = "DOM"
    CDP = "CDP"
    UIA = "UIA"
    WIN32 = "WIN32"
    KEYBOARD = "KEYBOARD"
    MOUSE = "MOUSE"
    OCR = "OCR"
    VISION = "VISION"


@dataclass
class CandidateTarget:
    """One discovered window/process that *may* be the automation target."""

    title: str = ""
    url: str | None = None
    origin: str = ""
    process_id: int = 0
    window_handle: int | None = None
    executable: str = ""
    exe_path: str = ""
    class_name: str = ""
    browser_type: str = ""
    environment: TargetEnvironment = TargetEnvironment.UNKNOWN
    framework: str = ""
    is_foreground: bool = False
    is_hidden: bool = False
    is_minimized: bool = False
    tab_index: int = -1
    has_cdp: bool = False
    cdp_port: int | None = None
    dom_available: bool = False
    uia_available: bool = False
    expected_form: bool = False
    known_field_labels: int = 0
    recent_interaction: bool = False
    confidence: float = 0.0
    score: int = 0
    source: str = ""  # where this candidate came from (window / process / tab)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "origin": self.origin,
            "process_id": self.process_id,
            "window_handle": self.window_handle,
            "executable": self.executable,
            "exe_path": self.exe_path,
            "class_name": self.class_name,
            "browser_type": self.browser_type,
            "environment": self.environment.value,
            "framework": self.framework,
            "is_foreground": self.is_foreground,
            "is_hidden": self.is_hidden,
            "is_minimized": self.is_minimized,
            "tab_index": self.tab_index,
            "has_cdp": self.has_cdp,
            "cdp_port": self.cdp_port,
            "dom_available": self.dom_available,
            "uia_available": self.uia_available,
            "expected_form": self.expected_form,
            "known_field_labels": self.known_field_labels,
            "recent_interaction": self.recent_interaction,
            "confidence": self.confidence,
            "score": self.score,
            "source": self.source,
        }


@dataclass
class TargetSession:
    """The fully-resolved, attached automation target."""

    environment: TargetEnvironment = TargetEnvironment.UNKNOWN
    process_id: int = 0
    window_handle: int | None = None
    browser_type: str = ""
    browser_context: Any = None
    page: Any = None
    url: str | None = None
    title: str = ""
    origin: str = ""
    application_name: str = ""
    framework: str = ""
    capabilities: set[Capability] = field(default_factory=set)
    attached: bool = False
    healthy: bool = False
    last_seen: float = field(default_factory=time.time)
    confidence: float = 0.0
    attachment_mode: AttachmentMode = AttachmentMode.EXISTING_WINDOW
    adapter: Any = None  # the TargetAdapter in use (DesktopTarget / WebTarget / ...)

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment.value,
            "process_id": self.process_id,
            "window_handle": self.window_handle,
            "browser_type": self.browser_type,
            "url": self.url,
            "title": self.title,
            "origin": self.origin,
            "application_name": self.application_name,
            "framework": self.framework,
            "capabilities": sorted(c.value for c in self.capabilities),
            "attached": self.attached,
            "healthy": self.healthy,
            "last_seen": self.last_seen,
            "confidence": self.confidence,
            "attachment_mode": self.attachment_mode.value,
        }


@dataclass
class TargetLock:
    """Immutable identity of the user-chosen target.

    Once the target is locked, every major operation re-verifies the foreground
    window / PID / HWND / origin against this record so the agent never types
    into a different application.
    """

    hwnd: int | None = None
    pid: int = 0
    process_name: str = ""
    window_title: str = ""
    browser: str = ""
    origin: str = ""
    url: str | None = None
    runtime_id: str = ""
    environment: TargetEnvironment = TargetEnvironment.UNKNOWN
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "process_name": self.process_name,
            "window_title": self.window_title,
            "browser": self.browser,
            "origin": self.origin,
            "url": self.url,
            "runtime_id": self.runtime_id,
            "environment": self.environment.value,
            "timestamp": self.timestamp,
        }
