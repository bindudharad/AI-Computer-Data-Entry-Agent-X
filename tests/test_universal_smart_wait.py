"""Unit tests for the smart wait engine (no real sleeps beyond ~200ms)."""

from __future__ import annotations

import time

import pytest

from atlas.universal.smart_wait import SmartWait, WaitTimeout


def test_wait_until_true_immediately() -> None:
    wait = SmartWait(default_timeout=1.0)
    assert wait.wait_until(lambda: True, poll=0.01) is True


def test_wait_until_eventually_true() -> None:
    state = {"n": 0}
    wait = SmartWait(default_timeout=1.0)

    def _check() -> bool:
        state["n"] += 1
        return state["n"] >= 3

    assert wait.wait_until(_check, poll=0.01) is True
    assert state["n"] >= 3


def test_wait_until_times_out_false() -> None:
    wait = SmartWait(default_timeout=0.2)
    assert wait.wait_until(lambda: False, poll=0.01) is False


def test_wait_or_raise_times_out() -> None:
    wait = SmartWait(default_timeout=0.2)
    with pytest.raises(WaitTimeout, match="never"):
        wait.wait_or_raise(lambda: False, poll=0.01, message="never")


def test_visible_predicate_handles_exception() -> None:
    class _Broken:
        def is_visible(self):
            raise RuntimeError("boom")

    wait = SmartWait(default_timeout=0.2)
    assert wait.wait_until(SmartWait.visible(_Broken()), poll=0.01) is False


def test_visible_predicate_true() -> None:
    class _Ok:
        def is_visible(self):
            return True

    wait = SmartWait(default_timeout=1.0)
    assert wait.wait_until(SmartWait.visible(_Ok()), poll=0.01) is True


def test_value_equals_predicate() -> None:
    class _Field:
        def __init__(self) -> None:
            self._value = ""

        def input_value(self) -> str:
            return self._value

        def set(self, value: str) -> None:
            self._value = value

    field = _Field()
    wait = SmartWait(default_timeout=1.0)

    def _set_later() -> None:
        time.sleep(0.05)
        field.set("filled")

    import threading

    threading.Thread(target=_set_later, daemon=True).start()

    result = wait.wait_until(SmartWait.value_equals(field, "filled"), poll=0.01)
    # The field is filled externally while we poll; wait_until must observe it.
    assert result is True


def test_dom_change_predicate() -> None:
    class _Page:
        def __init__(self) -> None:
            self._baseline = {"fields": 3, "labels": ["a", "b", "c"], "options": []}

        def evaluate(self, js: str):
            return self._baseline

    wait = SmartWait(default_timeout=1.0)
    page = _Page()
    baseline = {"fields": 3, "labels": ["a", "b", "c"], "options": []}
    predicate = SmartWait.dom_change(page, baseline)
    assert predicate() is False  # identical baseline -> unchanged
