"""Smart wait engine.

Replaces large fixed sleeps with adaptive polling for an observed state change.
Poll intervals back off (50ms -> 100ms -> 200ms -> 400ms -> 800ms) up to a
maximum duration; the caller supplies a predicate that observes the real state
(visibility, enabled, value, options, URL, DOM mutation, ...).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

#: Adaptive polling schedule (seconds).
DEFAULT_INTERVALS = (0.05, 0.1, 0.2, 0.4, 0.8)


class WaitTimeout(RuntimeError):
    """Raised when a smart-wait condition never became true."""


class SmartWait:
    """Poll a condition until it holds, backing off between polls."""

    def __init__(self, intervals: tuple[float, ...] = DEFAULT_INTERVALS, default_timeout: float = 5.0) -> None:
        self._intervals = intervals
        self.default_timeout = float(default_timeout)

    def wait_until(
        self,
        condition: Callable[[], Any],
        timeout: float | None = None,
        message: str = "condition",
        poll: float | None = None,
    ) -> bool:
        """Return True as soon as ``condition()`` is truthy; False on timeout."""
        deadline = time.time() + (float(timeout) if timeout is not None else self.default_timeout)
        idx = 0
        while time.time() < deadline:
            try:
                if condition():
                    return True
            except Exception:
                pass
            delay = poll if poll is not None else self._intervals[min(idx, len(self._intervals) - 1)]
            idx += 1
            time.sleep(delay)
        return False

    def wait_or_raise(
        self,
        condition: Callable[[], Any],
        timeout: float | None = None,
        message: str = "condition",
        poll: float | None = None,
    ) -> None:
        if not self.wait_until(condition, timeout=timeout, message=message, poll=poll):
            raise WaitTimeout(f"timed out waiting for {message}")

    # -- convenience predicates ---------------------------------------------

    @staticmethod
    def visible(locator: Any) -> Callable[[], bool]:
        """True once a Playwright locator is visible."""
        return lambda: _try(lambda: locator.is_visible(), False)

    @staticmethod
    def enabled(locator: Any) -> Callable[[], bool]:
        return lambda: _try(lambda: locator.is_enabled(), False)

    @staticmethod
    def value_equals(locator: Any, expected: str) -> Callable[[], bool]:
        return lambda: _try(lambda: (locator.input_value() or "") == expected, False)

    @staticmethod
    def page_url_contains(page: Any, fragment: str) -> Callable[[], bool]:
        return lambda: _try(lambda: fragment in (page.url or ""), False)

    @staticmethod
    def dom_change(page: Any, baseline: dict) -> Callable[[], bool]:
        """True once the DOM structure differs from ``baseline``.

        ``baseline`` is typically the return of ``page.evaluate(document_js)``
        that fingerprints the form (field count + labels + options).
        """

        def _changed() -> bool:
            try:
                from atlas.web.fields import form_fingerprint_js

                current = page.evaluate(form_fingerprint_js())
                return current != baseline
            except Exception:
                return False

        return _changed

    @staticmethod
    def count_at_least(locator: Any, n: int) -> Callable[[], bool]:
        return lambda: _try(lambda: locator.count() >= n, False)

    @staticmethod
    def value_changed(fn: Callable[[], str | None], previous: str | None) -> Callable[[], bool]:
        def _changed() -> bool:
            try:
                return fn() != previous
            except Exception:
                return False

        return _changed


def _try(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


__all__ = ["SmartWait", "WaitTimeout", "DEFAULT_INTERVALS"]
