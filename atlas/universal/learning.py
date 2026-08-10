"""Method learner.

Remembers which interaction method (DOM fill, select_option, UIA ValuePattern,
keyboard, OCR, vision) succeeded fastest for each application + field, so later
runs try the learned method first. All state is in-memory and opt-in via the
caller; no files or global caches are written implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MethodStats:
    """Accumulated performance of a single interaction method for a field."""

    method: str
    attempts: int = 0
    successes: int = 0
    avg_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


@dataclass
class MethodProfile:
    application: str
    field: str
    method: str = ""
    ok: bool = False
    avg_ms: float = 0.0
    attempts: int = 0
    successes: int = 0
    last_seen: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)
    _methods: dict[str, MethodStats] = field(default_factory=dict, repr=False, compare=False)

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    def record(self, method: str, ok: bool, elapsed_ms: float) -> None:
        self.attempts += 1
        self.successes += 1 if ok else 0
        self.ok = ok
        self.last_seen = elapsed_ms
        stats = self._methods.setdefault(method, MethodStats(method=method))
        stats.attempts += 1
        stats.successes += 1 if ok else 0
        if ok:
            n = max(stats.successes, 1)
            stats.avg_ms = ((stats.avg_ms * (n - 1)) + elapsed_ms) / n
        self.avg_ms = stats.avg_ms
        self.history.append({"method": method, "ok": ok, "elapsed_ms": elapsed_ms})
        self.history = self.history[-50:]

    def best_method(self) -> str | None:
        """The best-learned method: >=2 attempts and >=60% success, fastest first."""
        eligible = [s for s in self._methods.values() if s.attempts >= 2 and s.success_rate >= 0.6]
        if not eligible:
            return None
        return min(eligible, key=lambda s: (-s.success_rate, s.avg_ms, -s.attempts)).method

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "field": self.field,
            "method": self.method,
            "ok": self.ok,
            "avg_ms": self.avg_ms,
            "attempts": self.attempts,
            "successes": self.successes,
            "last_seen": self.last_seen,
            "success_rate": self.success_rate,
        }


def _key(application: str, field: str) -> str:
    return f"{application.strip().lower()}::{field.strip().lower()}"


class MethodLearner:
    """Tracks per-(application, field) performance of each interaction method."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = bool(enabled)
        self._profiles: dict[str, MethodProfile] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(
        self,
        *,
        application: str,
        field: str,
        method: str,
        ok: bool,
        elapsed_ms: float,
    ) -> MethodProfile:
        """Record one interaction attempt and return the updated profile."""
        if not self._enabled:
            return None
        key = _key(application, field)
        profile = self._profiles.get(key)
        if profile is None:
            profile = MethodProfile(application=application, field=field)
            self._profiles[key] = profile
        profile.record(method, ok, elapsed_ms)
        profile.method = profile.best_method() or profile.method or method
        return profile

    def preferred(self, application: str, field: str) -> str | None:
        """Best method for (application, field), or None when nothing learned yet.

        Only a method with >= 2 attempts and >= 60% success counts as learned;
        otherwise the caller falls back to its own default ordering.
        """
        if not self._enabled:
            return None
        profile = self._profiles.get(_key(application, field))
        if profile is None:
            return None
        return profile.best_method()

    def best(self, application: str, field: str) -> MethodProfile | None:
        return self._profiles.get(_key(application, field))

    def profile(self, application: str, field: str) -> MethodProfile:
        key = _key(application, field)
        if key not in self._profiles:
            self._profiles[key] = MethodProfile(application=application, field=field)
        return self._profiles[key]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {k: p.to_dict() for k, p in self._profiles.items()}

    def count(self) -> int:
        return len(self._profiles)

    def clear(self) -> None:
        self._profiles.clear()

    # -- convenience for callers ---------------------------------------------

    def ranked_methods(self, application: str, field: str, defaults: list[str]) -> list[str]:
        """Ordered method list for one field: learned first, then defaults."""
        preferred = self.preferred(application, field)
        if not preferred:
            return list(defaults)
        rest = [m for m in defaults if m != preferred]
        return [preferred, *rest]


__all__ = ["MethodLearner", "MethodProfile"]
