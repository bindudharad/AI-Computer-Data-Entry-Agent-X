"""Clipboard engine.

Provides safe clipboard read/write with restoration of the previous clipboard
content, and copy/paste into a focused control. Clipboard ops are used for
long text values (faster and more reliable than character typing) and for
verification (select-all + copy to read a field's current value).
"""

from __future__ import annotations

import time
import pyperclip

from atlas.act.mouse import InputDriver
from atlas.core.logging import logger


class ClipboardEngine:
    """Thread-safe-ish clipboard wrapper with restore semantics."""

    def __init__(self, driver: InputDriver | None = None) -> None:
        self._driver = driver

    def get_text(self) -> str:
        try:
            return pyperclip.paste() or ""
        except Exception as exc:
            logger.warning("clipboard read failed: {}", exc)
            return ""

    def set_text(self, text: str) -> None:
        try:
            pyperclip.copy(text or "")
        except Exception as exc:
            logger.warning("clipboard write failed: {}", exc)

    def paste_into_focused(self, text: str) -> None:
        """Copy text to the clipboard and paste into the focused control."""
        self.set_text(text)
        if self._driver is not None:
            self._driver.hotkey("ctrl", "v")
        time.sleep(0.1)

    #: Written to the clipboard before every read-back so a no-op Ctrl+A/Ctrl+C
    #: (nothing was actually focused/selected) is unambiguously detectable
    #: instead of silently returning whatever was left over from a previous,
    #: unrelated copy - see ``read_focused``.
    _SENTINEL = "\u0000atlas-clipboard-sentinel\u0000"

    def read_focused(self) -> str:
        """Select-all + copy the focused control and return its value.

        A stale or whole-page clipboard read is worse than no read at all: if
        Ctrl+A/Ctrl+C fires while the intended field does NOT actually have
        keyboard focus (common right after a scroll or a field that sits at
        the edge of a freshly revealed viewport), many apps either leave the
        clipboard untouched or select the entire page. Blindly trusting
        whatever ends up on the clipboard can then either (a) surface garbage
        like the window title as the "observed" value, or worse, (b) silently
        match a DIFFERENT field's leftover value by coincidence and report a
        false positive. To make a no-op detectable, the clipboard is primed
        with a private sentinel first; if it still reads back as the sentinel
        after the copy, nothing was actually selected and "" is returned
        rather than stale content.
        """
        self.set_text(self._SENTINEL)
        if self._driver is not None:
            self._driver.hotkey("ctrl", "a")
            time.sleep(0.05)
            self._driver.hotkey("ctrl", "c")
            time.sleep(0.05)
        text = self.get_text()
        if text == self._SENTINEL:
            logger.debug("clipboard read-back unchanged - nothing was focused/selected")
            return ""
        return text

    @staticmethod
    def normalize(value: str) -> str:
        return " ".join(value.strip().split())


__all__ = ["ClipboardEngine"]
