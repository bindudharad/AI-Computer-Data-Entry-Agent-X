"""Attach-first manager.

Implements the universal agent's core loop:

    DISCOVER -> CLASSIFY -> ATTACH -> VERIFY -> AUTOMATE

with the iron rule that an EXISTING target is never launched again. Launching
(``AUTO_LAUNCH_TARGET``) is only permitted when the restart policy says the
target genuinely does not exist and is not merely disconnected.

Decision cases (from the universal agent spec):

    CASE A  target application exists and is already open
    CASE B  browser exists, target tab exists
    CASE C  browser exists, target tab exists but is not active
    CASE D  browser exists, target application is on another tab
    CASE E  browser exists but automation connection (CDP) is unavailable
    CASE F  no suitable browser/application exists
    CASE G  target is a desktop application
    CASE H  target is an Electron/Chromium desktop app

Only CASE F may ever produce a launch decision, and only when policy allows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atlas.core.logging import logger
from atlas.universal.detector import RankingPreferences, UniversalTargetDetector
from atlas.universal.models import CandidateTarget, TargetEnvironment
from atlas.universal.restart_policy import RestartPolicy, RestartMode

#: Environment values that map to a browser-backed attach.
_BROWSER_ENVS = {
    TargetEnvironment.CHROME_BROWSER,
    TargetEnvironment.EDGE_BROWSER,
    TargetEnvironment.FIREFOX_BROWSER,
    TargetEnvironment.WEB_BROWSER,
    TargetEnvironment.ELECTRON,
}

#: Environment values we can attach to via an already-running window.
_DESKTOP_ENVS = {
    TargetEnvironment.DESKTOP_UIA,
    TargetEnvironment.GENERIC_DESKTOP,
    TargetEnvironment.UNKNOWN,
    TargetEnvironment.ELECTRON,
    TargetEnvironment.CHROMIUM_DESKTOP,
}

_HANDLED_ENVS = _BROWSER_ENVS | _DESKTOP_ENVS


class AttachFirstError(RuntimeError):
    """Raised when no target can be attached and launching is not permitted.

    The universal flow NEVER launches automatically; when nothing is found the
    caller should surface this error with a clear "wait for the user" message
    instead of relaunching the target application.
    """


@dataclass
class AttachDecision:
    case: str                     # A..H
    action: str                   # ATTACH_EXISTING / BROWSER_UIA / ATTACH_OTHER / LAUNCH / WAIT
    reason: str
    candidate: CandidateTarget | None = None
    launch: bool = False          # True only when policy explicitly allows a launch

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "action": self.action,
            "reason": self.reason,
            "launch": self.launch,
            "candidate": self.candidate.to_dict() if self.candidate else None,
        }


class AttachFirstManager:
    """Decides how to connect to the target without relaunching it."""

    def __init__(
        self,
        detector: UniversalTargetDetector | None = None,
        restart_policy: RestartPolicy | None = None,
        min_confidence: float = 0.55,
    ) -> None:
        self._detector = detector or UniversalTargetDetector()
        self._restart = restart_policy or RestartPolicy()
        self._min_confidence = min_confidence

    @property
    def detector(self) -> UniversalTargetDetector:
        return self._detector

    @property
    def restart_policy(self) -> RestartPolicy:
        return self._restart

    # ------------------------------------------------------------------
    # Decision logic (pure: takes prefs, returns an AttachDecision)
    # ------------------------------------------------------------------

    def plan(self, preferences: RankingPreferences | None = None) -> AttachDecision:
        """Decide what to do with the current desktop/browser state.

        Never launches anything; it only reports whether a launch is permitted.
        """
        prefs = preferences or RankingPreferences()
        candidates = self._detector.discover(prefs)
        if candidates:
            self._detector._rank(candidates, prefs)

        if not candidates:
            return self._decide_missing()

        browsers = [c for c in candidates if c.environment in _BROWSER_ENVS]
        desktop = [c for c in candidates if c.environment in _DESKTOP_ENVS and c.environment not in _BROWSER_ENVS]
        best = max(candidates, key=lambda c: (c.score, c.confidence))

        # A high-confidence target already exists -> NEVER relaunch it.
        if best is not None and best.score > 0 and best.confidence >= self._min_confidence:
            if best.environment in _BROWSER_ENVS:
                # A real window/tab can be attached directly; a bare process
                # with no automation channel (no CDP/DOM and no window) means
                # the browser is DISCONNECTED, not missing.
                if best.window_handle or best.has_cdp or best.dom_available:
                    return self._decision(
                        "A", "ATTACH_EXISTING",
                        f"browser target exists: {best.title or best.url} ({best.environment.value})",
                        best,
                    )
                return self._decision(
                    "E", "BROWSER_UIA",
                    "browser process alive but CDP unavailable - attach via UIA, do not relaunch",
                    best,
                )
            return self._decision(
                "G", "ATTACH_EXISTING",
                f"desktop target exists: {best.title or best.source} ({best.environment.value})",
                best,
            )

        # No high-confidence candidate, but browsers are present.
        if browsers:
            live_pid = next((b.process_id for b in browsers if b.process_id), 0)
            cdp_unavailable = not any(b.has_cdp or b.dom_available for b in browsers)
            if cdp_unavailable and live_pid:
                # A live browser with no CDP is NOT missing - never relaunch it.
                best_browser = max(browsers, key=lambda b: (b.score, b.confidence))
                return self._decision(
                    "E", "BROWSER_UIA",
                    "browser process alive but CDP unavailable - attach via UIA, do not relaunch",
                    best_browser,
                )
            if any(b.has_cdp or b.dom_available for b in browsers):
                best_browser = max(browsers, key=lambda b: (b.score, b.confidence))
                return self._decision(
                    "B", "ATTACH_EXISTING",
                    f"browser present, attach existing tab: {best_browser.url or best_browser.title}",
                    best_browser,
                )
            return self._decision(
                "F", "WAIT",
                "browser present but no matching target tab found; waiting for user input",
            )

        # A desktop window is present but scored below the confidence bar.
        if desktop:
            best_desktop = max(desktop, key=lambda c: (c.score, c.confidence))
            return self._decision(
                "G", "ATTACH_EXISTING",
                f"desktop window present: {best_desktop.title or best_desktop.source}",
                best_desktop,
            )

        return self._decide_missing()

    def _decide_missing(self) -> AttachDecision:
        """CASE F: nothing found at all - policy decides whether a launch is allowed."""
        allowed = self._restart.permit_launch(
            target_missing=True,
            reason="target_not_found",
            crash_detected=False,
        )
        if allowed:
            return self._decision(
                "F", "LAUNCH",
                "no target exists anywhere; AUTO_LAUNCH_TARGET permitted a fresh launch",
                launch=True,
            )
        return self._decision(
            "F", "WAIT",
            "no target found; launch not permitted by restart policy",
        )

    def decide_connection_loss(self, *, process_alive: bool, cdp_available: bool | None = None,
                               window_visible: bool | None = None) -> AttachDecision:
        """React when the automation channel drops mid-run.

        DISCONNECTED (alive but CDP down) => never a launch; keep UIA attach.
        MISSING (process gone)            => crash; relaunch only if policy allows.
        """
        health = self._restart.classify_health(
            process_alive=process_alive, cdp_available=cdp_available, window_visible=window_visible,
        )
        if health in {"HEALTHY", "DEGRADED"}:
            return self._decision(
                "A", "ATTACH_EXISTING",
                f"connection recovered (health={health})",
            )
        if health == "DISCONNECTED":
            return self._decision(
                "E", "BROWSER_UIA",
                "process alive but CDP disconnected - attach via UIA, do not relaunch",
            )
        # MISSING: real crash. Ask the policy.
        allowed = self._restart.permit_launch(
            target_missing=True,
            reason="crash_detected",
            crash_detected=True,
        )
        if allowed:
            return self._decision(
                "F", "LAUNCH",
                "target process terminated and AUTO_LAUNCH_TARGET=true; relaunch permitted",
                launch=True,
            )
        return self._decision(
            "F", "WAIT",
            "target process terminated but launch not permitted by restart policy",
        )

    # ------------------------------------------------------------------
    # Execution bridge (binds the decision to the adapters)
    # ------------------------------------------------------------------

    def execute(
        self,
        decision: AttachDecision,
        *,
        attach_web: Any = None,       # callable(candidate) -> web adapter
        attach_desktop: Any = None,   # callable(candidate) -> desktop adapter
        attach_browser_uia: Any = None,  # callable(candidate) -> uia adapter
        launch_web: Any = None,       # callable() -> launched adapter
    ) -> Any:
        """Run a decision through injected adapter factories.

        Pure callers/tests may pass ``None`` for all factories to only observe
        the decision. Returns the adapter from the chosen factory, or None.
        """
        logger.info(
            "[ATTACH] case={} action={} reason={}",
            decision.case, decision.action, decision.reason,
        )
        if decision.action == "ATTACH_EXISTING":
            candidate = decision.candidate
            if candidate and candidate.environment in _BROWSER_ENVS and attach_web:
                return attach_web(candidate)
            if attach_desktop:
                return attach_desktop(candidate)
            return None
        if decision.action == "BROWSER_UIA":
            return attach_browser_uia(decision.candidate) if attach_browser_uia else None
        if decision.action == "LAUNCH" and decision.launch and launch_web:
            return launch_web()
        return None

    # ------------------------------------------------------------------

    @staticmethod
    def _decision(case: str, action: str, reason: str,
                  candidate: CandidateTarget | None = None, launch: bool = False) -> AttachDecision:
        return AttachDecision(case=case, action=action, reason=reason,
                              candidate=candidate, launch=launch)


__all__ = ["AttachFirstManager", "AttachDecision", "AttachFirstError"]
