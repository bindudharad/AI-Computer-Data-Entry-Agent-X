"""Universal target detector.

Enumerates everything on the user's computer that could be the automation
target - visible windows, browser processes, browser tabs - then ranks every
candidate by a weighted score and returns the most likely target. It NEVER
launches anything; launching is owned by the attach-first manager under a
strict policy.

All OS access goes through small accessor objects (``Win32Accessor``,
``ProcessAccessor``) so the ranking logic is fully unit-testable without a
live desktop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atlas.core.logging import logger
from atlas.universal.classifier import ApplicationClassifier
from atlas.universal.models import Capability, CandidateTarget, TargetEnvironment

#: Chromium/Electron window classes that host web content.
_CHROMIUM_CLASSES = {"Chrome_WidgetWin_1", "Chrome_RenderWidgetHostHWND"}

#: Ranking weights (from the universal agent spec).
W_ACTIVE_FOREGROUND = 30
W_EXACT_TITLE = 35
W_URL_DOMAIN = 20
W_APP_NAME = 20
W_EXPECTED_FORM = 15
W_FIELD_LABELS = 15
W_BROWSER_COMPAT = 10
W_UIA_CONTROLS = 10
W_DOM_FIELDS = 10
W_RECENT_INTERACTION = 5
W_HIDDEN = -50
W_MINIMIZED = -40
W_UNRELATED = -100


@dataclass
class RankingPreferences:
    """What the user / config says the target should look like."""

    title_hint: str = ""
    url_hint: str = ""
    application_name: str = ""
    browser_types: set[str] = field(default_factory=set)
    known_field_labels: list[str] = field(default_factory=list)
    prefer_foreground: bool = True

    @property
    def domain_hint(self) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(self.url_hint or "")
        return (parsed.netloc or "").lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title_hint": self.title_hint,
            "url_hint": self.url_hint,
            "application_name": self.application_name,
            "browser_types": sorted(self.browser_types),
            "known_field_labels": list(self.known_field_labels),
            "prefer_foreground": self.prefer_foreground,
        }


class Win32Accessor:
    """Thin, monkeypatchable wrapper over the Win32 window APIs."""

    @staticmethod
    def foreground() -> int:
        import win32gui

        return int(win32gui.GetForegroundWindow() or 0)

    @staticmethod
    def visible_windows() -> list[dict[str, Any]]:
        import win32gui

        out: list[dict[str, Any]] = []

        def _collect(handle: int, _: Any) -> None:
            try:
                if not win32gui.IsWindowVisible(handle):
                    return
                title = win32gui.GetWindowText(handle) or ""
                class_name = win32gui.GetClassName(handle) or ""
                try:
                    import win32process

                    _, pid = win32process.GetWindowThreadProcessId(handle)
                except Exception:
                    pid = 0
                rect = (0, 0, 0, 0)
                try:
                    rect = win32gui.GetWindowRect(handle)
                except Exception:
                    pass
                out.append({
                    "handle": int(handle),
                    "title": title,
                    "class_name": class_name,
                    "pid": int(pid or 0),
                    "rect": tuple(int(v) for v in rect),
                })
            except Exception:
                pass

        win32gui.EnumWindows(_collect, None)
        return out

    @staticmethod
    def is_iconic(handle: int) -> bool:
        import win32gui

        try:
            return bool(win32gui.IsIconic(int(handle)))
        except Exception:
            return False


class ProcessAccessor:
    """Thin, monkeypatchable wrapper over process enumeration (psutil)."""

    @staticmethod
    def browser_processes() -> list[dict[str, Any]]:
        import psutil

        names = {
            "chrome", "chrome.exe", "msedge", "msedge.exe", "firefox", "firefox.exe",
            "brave", "brave.exe", "opera", "opera.exe",
            "electron", "electron.exe",
        }
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name not in names:
                    continue
                key = (name, str(proc.info.get("pid", "")))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "pid": int(proc.info["pid"] or 0),
                    "name": name,
                    "exe": proc.info.get("exe") or "",
                    "cmdline": list(proc.info.get("cmdline") or []),
                })
            except Exception:
                continue
        return out

    @staticmethod
    def process_alive(pid: int) -> bool:
        import psutil

        if not pid:
            return False
        try:
            return psutil.pid_exists(int(pid))
        except Exception:
            return False

    @staticmethod
    def process_name(pid: int) -> str:
        import psutil

        if not pid:
            return ""
        try:
            return psutil.Process(int(pid)).name() or ""
        except Exception:
            return ""


class UniversalTargetDetector:
    """Discovers, classifies and ranks candidate automation targets."""

    def __init__(
        self,
        win32: Any = None,
        processes: Any = None,
        classifier: ApplicationClassifier | None = None,
    ) -> None:
        self._win32 = win32 or Win32Accessor
        self._processes = processes or ProcessAccessor
        self._classifier = classifier or ApplicationClassifier()

    # -- discovery ----------------------------------------------------------

    def discover(self, preferences: RankingPreferences | None = None) -> list[CandidateTarget]:
        """Gather every candidate from windows, browser processes and tabs."""
        prefs = preferences or RankingPreferences()
        foreground = self._foreground_handle()
        candidates: list[CandidateTarget] = []

        windows = self._win32.visible_windows()
        for window in windows:
            candidate = self._candidate_from_window(window, foreground)
            if candidate is not None:
                candidates.append(candidate)

        candidates.extend(self._candidates_from_browser_processes())

        # Tab-level discovery (CDP where available) may add or refine candidates.
        tab_candidates = self._candidates_from_tabs(prefs)
        for tab in tab_candidates:
            candidates.append(tab)

        return self._dedupe(candidates)

    def detect(self, preferences: RankingPreferences | None = None) -> CandidateTarget | None:
        """Discover + rank; return the best candidate or None when nothing matches."""
        candidates = self.discover(preferences)
        if not candidates:
            return None
        self._rank(candidates, preferences or RankingPreferences())
        best = max(candidates, key=lambda c: (c.score, c.confidence))
        if best.score <= 0:
            logger.debug("[DETECT] best candidate {} score={} - no confident target", best.title or best.source, best.score)
            return None
        logger.info(
            "[DETECT] target={!r} url={} env={} score={} conf={:.2f} source={}",
            best.title, best.url, best.environment.value, best.score, best.confidence, best.source,
        )
        return best

    # -- internal helpers ----------------------------------------------------

    def _foreground_handle(self) -> int:
        try:
            return int(self._win32.foreground() or 0)
        except Exception:
            return 0

    def _candidate_from_window(self, window: dict, foreground: int) -> CandidateTarget | None:
        title = window.get("title") or ""
        handle = int(window.get("handle") or 0)
        pid = int(window.get("pid") or 0)
        cls = window.get("class_name") or ""
        rect = window.get("rect") or (0, 0, 0, 0)
        # Ghost / untitled windows carry no user-facing content and must never
        # be treated as the automation target.
        if not title:
            return None
        if cls in {"Progman", "WorkerW", "Shell_TrayWnd", "DV2ControlHost"}:
            return None
        url = self._url_from_title(title)
        exe, exe_path = self._executable_for(pid)
        environment, framework, caps = self._classifier.classify(
            executable=exe or self._processes.process_name(pid),
            class_name=cls,
            title=title,
            url=url,
            uia_available=bool(pid) or cls in _CHROMIUM_CLASSES,
        )
        return CandidateTarget(
            title=title,
            url=url,
            origin=self._origin(url),
            process_id=pid,
            window_handle=handle,
            executable=exe,
            exe_path=exe_path,
            class_name=cls,
            browser_type=self._browser_type_for(exe),
            environment=environment,
            framework=framework,
            is_foreground=handle == foreground,
            is_hidden=not bool(title),
            is_minimized=bool(handle) and self._win32.is_iconic(handle),
            has_cdp=(Capability.CDP in caps),
            dom_available=(Capability.DOM in caps),
            uia_available=(Capability.UIA in caps),
            source="window",
        )

    def _candidates_from_browser_processes(self) -> list[CandidateTarget]:
        out: list[CandidateTarget] = []
        for proc in self._processes.browser_processes():
            name = proc.get("name") or ""
            exe = proc.get("exe") or ""
            pid = int(proc.get("pid") or 0)
            if not name and not exe:
                continue
            url = self._url_from_cmdline(proc.get("cmdline") or [])
            cdp_port = self._cdp_port_from_cmdline(proc.get("cmdline") or [])
            cdp_available = cdp_port is not None
            environment, framework, caps = self._classifier.classify(
                executable=name or exe,
                url=url,
                uia_available=True,
                cdp_available=cdp_available,
            )
            out.append(CandidateTarget(
                title=name,
                url=url,
                origin=self._origin(url),
                process_id=pid,
                executable=name,
                exe_path=exe,
                browser_type=self._browser_type_for(name),
                environment=environment,
                framework=framework,
                has_cdp=(Capability.CDP in caps),
                cdp_port=cdp_port,
                dom_available=False,
                uia_available=True,
                source="process",
            ))
        return out

    def _candidates_from_tabs(self, prefs: RankingPreferences) -> list[CandidateTarget]:
        """Best-effort tab discovery via CDP (import lazily; may be unavailable)."""
        try:
            from atlas.web.tabs import discover_tabs

            tabs = discover_tabs()
        except Exception as exc:
            logger.debug("[DETECT] tab discovery unavailable: {}", exc)
            return []
        out: list[CandidateTarget] = []
        for tab in tabs:
            title = tab.get("title") or ""
            url = tab.get("url") or ""
            pid = int(tab.get("pid") or 0)
            if not url:
                continue
            environment, framework, caps = self._classifier.classify(
                executable=tab.get("browser") or "",
                title=title,
                url=url,
                dom_available=True,
                cdp_available=True,
                uia_available=False,
            )
            out.append(CandidateTarget(
                title=title,
                url=url,
                origin=self._origin(url),
                process_id=pid,
                executable=tab.get("browser") or "",
                browser_type=tab.get("browser") or "",
                environment=environment,
                framework=framework,
                tab_index=int(tab.get("tab_index") or 0),
                has_cdp=True,
                dom_available=True,
                uia_available=False,
                source="tab",
            ))
        return out

    # -- ranking -------------------------------------------------------------

    def _rank(self, candidates: list[CandidateTarget], prefs: RankingPreferences) -> None:
        domain = prefs.domain_hint
        for c in candidates:
            c.score = self._score_candidate(c, prefs, domain)
            c.confidence = self._confidence(c)

    @staticmethod
    def _score_candidate(c: CandidateTarget, prefs: RankingPreferences, domain: str) -> int:
        score = 0
        title_lower = (c.title or "").lower()
        hint_lower = (prefs.title_hint or "").lower()
        app_lower = (prefs.application_name or "").lower()

        if c.is_foreground and prefs.prefer_foreground:
            score += W_ACTIVE_FOREGROUND
        if hint_lower and hint_lower in title_lower:
            score += W_EXACT_TITLE
        if domain and domain == (c.origin or ""):
            score += W_URL_DOMAIN
        if app_lower and app_lower in title_lower:
            score += W_APP_NAME
        if c.environment in {
            TargetEnvironment.WEB_BROWSER,
            TargetEnvironment.CHROME_BROWSER,
            TargetEnvironment.EDGE_BROWSER,
            TargetEnvironment.FIREFOX_BROWSER,
            TargetEnvironment.ELECTRON,
            TargetEnvironment.DESKTOP_UIA,
        }:
            score += W_EXPECTED_FORM
        if prefs.known_field_labels and any(
            (label or "").lower() in title_lower for label in prefs.known_field_labels
        ):
            score += W_FIELD_LABELS
        if prefs.browser_types and (c.browser_type or "") in prefs.browser_types:
            score += W_BROWSER_COMPAT
        if c.uia_available:
            score += W_UIA_CONTROLS
        if c.dom_available:
            score += W_DOM_FIELDS
        if c.recent_interaction:
            score += W_RECENT_INTERACTION
        if c.is_hidden:
            score += W_HIDDEN
        if c.is_minimized:
            score += W_MINIMIZED
        if c.environment == TargetEnvironment.UNKNOWN and not c.url and not c.window_handle:
            score += W_UNRELATED
        return score

    @staticmethod
    def _confidence(c: CandidateTarget) -> float:
        base = 0.5
        if c.dom_available:
            base += 0.15
        if c.has_cdp:
            base += 0.10
        if c.uia_available:
            base += 0.05
        if c.url:
            base += 0.10
        if c.is_foreground:
            base += 0.05
        if c.is_hidden or c.is_minimized:
            base -= 0.25
        if c.environment == TargetEnvironment.UNKNOWN:
            base -= 0.2
        return round(max(0.0, min(1.0, base)), 3)

    # -- misc ----------------------------------------------------------------

    @staticmethod
    def _executable_for(pid: int) -> tuple[str, str]:
        if not pid:
            return "", ""
        try:
            import psutil

            proc = psutil.Process(int(pid))
            try:
                exe = proc.exe() or ""
            except Exception:
                exe = ""
            try:
                name = proc.name() or ""
            except Exception:
                name = ""
            return name, exe
        except Exception:
            return "", ""

    @staticmethod
    def _browser_type_for(exe: str) -> str:
        leaf = (exe or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if leaf in {"msedge", "msedge.exe"}:
            return "msedge"
        if leaf in {"firefox", "firefox.exe"}:
            return "firefox"
        if leaf in {"webkit"}:
            return "webkit"
        if leaf in {"brave", "brave.exe", "opera", "opera.exe"}:
            return "chromium"
        if leaf in {"chrome", "chrome.exe"}:
            return "chromium"
        if leaf in {"electron", "electron.exe"}:
            return "chromium"
        return ""

    @staticmethod
    def _url_from_title(title: str) -> str | None:
        """A URL embedded in a window title (e.g. Chrome tab titles rarely have it)."""
        import re

        match = re.search(r"https?://\S+", title)
        return match.group(0) if match else None

    @staticmethod
    def _url_from_cmdline(cmdline: list[str]) -> str | None:
        for token in cmdline:
            if token.startswith("http://") or token.startswith("https://"):
                return token
            if token.startswith("--") and "=" in token and "://" in token:
                value = token.split("=", 1)[1]
                if value.startswith("http"):
                    return value
        return None

    @staticmethod
    def _cdp_port_from_cmdline(cmdline: list[str]) -> int | None:
        """Extract a ``--remote-debugging-port`` value, if present."""
        for token in cmdline:
            if token == "--remote-debugging-port":
                return 9222
            if token.startswith("--remote-debugging-port="):
                value = token.split("=", 1)[1].strip()
                if value.isdigit():
                    return int(value)
        return None

    @staticmethod
    def _origin(url: str | None) -> str:
        if not url:
            return ""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            return (parsed.netloc or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _dedupe(candidates: list[CandidateTarget]) -> list[CandidateTarget]:
        """Merge window/process/tab candidates that describe the same PID+URL."""
        merged: dict[tuple, CandidateTarget] = {}
        for c in candidates:
            key = (c.process_id, c.origin or c.title or "")
            if key in merged:
                existing = merged[key]
                existing.dom_available = existing.dom_available or c.dom_available
                existing.has_cdp = existing.has_cdp or c.has_cdp
                existing.cdp_port = existing.cdp_port or c.cdp_port
                existing.uia_available = existing.uia_available or c.uia_available
                if c.tab_index >= 0:
                    existing.tab_index = c.tab_index
                existing.source = existing.source + "+" + c.source
                if c.window_handle:
                    existing.window_handle = c.window_handle
                continue
            merged[key] = c
        return list(merged.values())


__all__ = [
    "UniversalTargetDetector",
    "Win32Accessor",
    "ProcessAccessor",
    "RankingPreferences",
]
