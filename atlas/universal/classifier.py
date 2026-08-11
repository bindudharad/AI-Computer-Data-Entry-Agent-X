"""Application classifier.

Maps raw discovery facts (process name, window class, title, URL, DOM/UIA
availability) onto a :class:`~atlas.universal.models.TargetEnvironment` plus
the set of automation capabilities available on that target. Never assumes a
specific application (e.g. MPF) - everything is inferred from generic signals.
"""

from __future__ import annotations

from atlas.universal.models import Capability, TargetEnvironment

#: Executables that are full web browsers.
_CHROMIUM_BROWSERS = {"chrome", "chrome.exe", "msedge", "msedge.exe", "brave", "brave.exe", "opera", "opera.exe"}
_FIREFOX = {"firefox", "firefox.exe"}

#: Chromium's desktop chrome window class (browser UI + Electron/CEF hosts).
#: Normalised to lowercase to match the classifier's ``_norm``.
_CHROME_WIDGET_CLASS = "chrome_widgetwin_1"

#: Electron/CEF-flavoured executables and class names.
_ELECTRON_HINTS = ("electron", "cef", "webview")
_ELECTRON_CLASSES = {
    _CHROME_WIDGET_CLASS,
    "chrome_renderwidgethosthwnd",
    "cefbrowserwindow",
}


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _exe_leaf(exe: str) -> str:
    return (exe or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


class ApplicationClassifier:
    """Deterministic classifier for a single discovered candidate."""

    def classify(
        self,
        *,
        executable: str = "",
        class_name: str = "",
        title: str = "",
        url: str | None = None,
        process_name: str = "",
        dom_available: bool = False,
        uia_available: bool = False,
        cdp_available: bool = False,
    ) -> tuple[TargetEnvironment, str, set[Capability]]:
        """Return ``(environment, framework, capabilities)`` for the candidate.

        Order of checks matters: a real browser executable with a URL is a
        WEB_*_BROWSER; a Chromium-chrome window WITHOUT a URL is an Electron /
        Chromium desktop app; everything else falls to generic desktop or
        unknown.
        """
        exe = _exe_leaf(executable or process_name)
        cls = _norm(class_name)
        t = _norm(title)
        # Vision is always available as a last-resort channel; everything else
        # is added only when the discovery layer can prove it.
        caps: set[Capability] = {Capability.KEYBOARD, Capability.MOUSE, Capability.VISION}
        if uia_available:
            caps.add(Capability.UIA)
        if dom_available:
            caps.add(Capability.DOM)
        if cdp_available:
            caps.add(Capability.CDP)

        # 1) Full web browsers.
        if exe in _FIREFOX:
            return TargetEnvironment.FIREFOX_BROWSER, "firefox", caps
        if exe in {"chrome", "chrome.exe"}:
            return TargetEnvironment.CHROME_BROWSER, "chromium", caps
        if exe in {"msedge", "msedge.exe"}:
            return TargetEnvironment.EDGE_BROWSER, "chromium", caps
        if exe in {"brave", "brave.exe", "opera", "opera.exe"}:
            return TargetEnvironment.WEB_BROWSER, "chromium", caps
        if exe in _CHROMIUM_BROWSERS:
            return TargetEnvironment.WEB_BROWSER, "chromium", caps

        # 2) Chromium chrome-widget windows. The same window class is shared by
        #    the browser chrome and by Electron/CEF hosts, so the executable is
        #    the discriminator:
        #      * a known browser executable was already returned above;
        #      * a non-empty non-browser executable => Electron / CEF desktop app;
        #      * an empty/unknown executable     => treat as a browser window
        #        (we cannot attribute it, and a browser is the common case).
        if cls in _ELECTRON_CLASSES:
            if exe:
                framework = "electron" if any(h in exe for h in _ELECTRON_HINTS) else "electron"
                return TargetEnvironment.ELECTRON, framework, caps
            if url:
                return TargetEnvironment.CHROME_BROWSER, "chromium", caps
            return TargetEnvironment.CHROME_BROWSER, "chromium", caps
        if any(h in exe for h in _ELECTRON_HINTS):
            return TargetEnvironment.ELECTRON, "electron", caps

        # 3) A URL alone implies a web page.
        if url:
            return TargetEnvironment.WEB_BROWSER, "unknown", caps

        # 4) Generic desktop with UIA controls.
        if uia_available or cls in {
            "WindowsForms10.Window.8.app", "HwndWrapper", "Qt5QWindowIcon", "SWT_Window0",
        }:
            return TargetEnvironment.DESKTOP_UIA, "desktop", caps

        # 5) Unknown window.
        if not t:
            return TargetEnvironment.UNKNOWN, "", caps
        return TargetEnvironment.GENERIC_DESKTOP, "desktop", caps

    def environment_name(self, environment: TargetEnvironment) -> str:
        """Human label used in logs / overlay."""
        return environment.value.replace("_", " ").title()

    def expected_form_score(self, environment: TargetEnvironment) -> int:
        """Extra ranking points for environments that host data-entry forms."""
        if environment in {
            TargetEnvironment.WEB_BROWSER,
            TargetEnvironment.CHROME_BROWSER,
            TargetEnvironment.EDGE_BROWSER,
            TargetEnvironment.FIREFOX_BROWSER,
            TargetEnvironment.ELECTRON,
            TargetEnvironment.DESKTOP_UIA,
        }:
            return 15
        if environment in {TargetEnvironment.CHROMIUM_DESKTOP, TargetEnvironment.GENERIC_DESKTOP}:
            return 5
        return 0


__all__ = ["ApplicationClassifier"]
