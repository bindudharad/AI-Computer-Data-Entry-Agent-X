"""Unit tests for the method learner."""

from __future__ import annotations

from atlas.universal.learning import MethodLearner


def test_preferred_returns_none_before_learning() -> None:
    learner = MethodLearner()
    assert learner.preferred("app", "name") is None


def test_preferred_requires_two_attempts_and_success() -> None:
    learner = MethodLearner()
    learner.record(application="app", field="name", method="dom", ok=False, elapsed_ms=10)
    assert learner.preferred("app", "name") is None


def test_learning_prefers_successful_method() -> None:
    learner = MethodLearner()
    for _ in range(3):
        learner.record(application="app", field="name", method="uia", ok=True, elapsed_ms=5)
    assert learner.preferred("app", "name") == "uia"


def test_failed_method_never_becomes_preferred() -> None:
    learner = MethodLearner()
    for _ in range(5):
        learner.record(application="app", field="email", method="vision", ok=False, elapsed_ms=20)
    assert learner.preferred("app", "email") is None


def test_ranked_methods_puts_learned_first() -> None:
    learner = MethodLearner()
    for _ in range(3):
        learner.record(application="app", field="dob", method="dom", ok=True, elapsed_ms=5)
    ranked = learner.ranked_methods("app", "dob", ["uia", "dom", "vision"])
    assert ranked == ["dom", "uia", "vision"]


def test_ranked_methods_defaults_without_learning() -> None:
    learner = MethodLearner()
    assert learner.ranked_methods("app", "dob", ["uia", "dom"]) == ["uia", "dom"]


def test_disabled_learner_never_learns() -> None:
    learner = MethodLearner(enabled=False)
    learner.record(application="app", field="name", method="dom", ok=True, elapsed_ms=5)
    learner.record(application="app", field="name", method="dom", ok=True, elapsed_ms=5)
    assert learner.preferred("app", "name") is None
    assert learner.count() == 0


def test_snapshot_and_clear() -> None:
    learner = MethodLearner()
    learner.record(application="app", field="x", method="dom", ok=True, elapsed_ms=5)
    snap = learner.snapshot()
    assert "app::x" in snap
    assert snap["app::x"]["successes"] == 1
    learner.clear()
    assert learner.count() == 0
