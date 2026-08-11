"""Application restart policy.

Formalises the rule: ATLAS NEVER restarts a target application. A missing CDP
connection is not a crash, a wrong tab is not a crash, a verification failure is
not a crash. Only a genuine process termination may be considered for restart,
and even then only when the user has explicitly enabled ``AUTO_LAUNCH_TARGET``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class RestartMode(str, Enum):
    NEVER = "NEVER"
    ON_USER_REQUEST = "ON_USER_REQUEST"
    ON_CRASH_ONLY = "ON_CRASH_ONLY"
    AUTO = "AUTO"


#: States that are NOT a crash and must never trigger a restart.
_NON_CRASH_REASONS = {
    "cdp_unavailable",
    "cdp_connection_failed",
    "tab_not_found",
    "verification_failed",
    "uia_read_failed",
    "focus_lost",
    "window_hidden",
    "browser_health_disconnected",
    "health_degraded",
    "recovery_exhausted",
    "timeout",
}


class RestartPolicy:
    """Decides whether a relaunch is ever permitted."""

    def __init__(self, mode: RestartMode | str = RestartMode.ON_CRASH_ONLY, auto_launch_target: bool = False) -> None:
        self._mode = RestartMode(mode)
        self.auto_launch_target = bool(auto_launch_target)

    @property
    def mode(self) -> RestartMode:
        return self._mode

    def permit_launch(self, *, target_missing: bool, reason: str = "", crash_detected: bool = False) -> bool:
        """Whether launching a NEW target instance is allowed.

        ``target_missing`` must be true (an existing target is never replaced).
        ``crash_detected`` is only true when the target's process genuinely
        terminated. A non-crash reason (CDP unavailable, tab not found, ...)
        never permits a launch.
        """
        if not target_missing:
            return False
        if not self.auto_launch_target:
            return False
        if self._mode == RestartMode.NEVER:
            return False
        if self._mode == RestartMode.ON_CRASH_ONLY:
            return crash_detected
        # AUTO and ON_USER_REQUEST: the target genuinely does not exist, so a
        # fresh launch is permitted (still gated by AUTO_LAUNCH_TARGET above).
        return True

    def classify_health(self, *, process_alive: bool, cdp_available: bool | None = None,
                        window_visible: bool | None = None) -> str:
        """Map raw facts onto a browser-health state.

        Returns one of ``HEALTHY / DEGRADED / DISCONNECTED / MISSING / UNKNOWN``.
        Critically, a DISCONNECTED browser (alive, CDP down) is NOT MISSING and
        never yields a launch decision.
        """
        if not process_alive:
            return "MISSING"
        if cdp_available is False:
            return "DISCONNECTED"
        if window_visible is False or cdp_available is None:
            return "DEGRADED"
        if cdp_available:
            return "HEALTHY"
        return "UNKNOWN"

    def classify_missing(self, reason: str) -> bool:
        """True when ``reason`` describes a genuine crash / missing process."""
        norm = (reason or "").lower()
        if norm in _NON_CRASH_REASONS or any(token in norm for token in (
            "cdp", "tab not", "verification", "uia read", "focus", "timeout",
        )):
            return False
        return "crash" in norm or "terminated" in norm or "process" in norm and "missing" in norm

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "auto_launch_target": self.auto_launch_target,
        }


__all__ = ["RestartPolicy", "RestartMode"]
