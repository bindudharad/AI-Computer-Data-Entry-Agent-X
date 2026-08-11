"""Tests for the two-level watchdog: level-2 state-budget overruns in
``AgentLoop._check_state_budget`` (level 1 is the ExecutionSandbox focus
watchdog, tested in ``test_sandbox.py``).

Covers:
* budget normalization defaults and per-state overrides,
* a state overrunning its budget publishes a RECOVERY event ONCE,
* a persistent overrun re-publishes after the repeat-log interval,
* ``_set`` resets the overrun tick counter for the entered state,
* the ``watchdog.json`` summary dump reflects level-1 + level-2 events,
* a zero/disabled budget never fires.
"""

from __future__ import annotations

import time

from atlas.core.events import EventType, get_event_bus
from atlas.core.states import AgentState, StateMachine
from atlas.workflow.loop import AgentLoop


def _bare_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._states = StateMachine()
    loop._state_entered = {}
    loop._state_warned = set()
    loop._state_overruns = {}
    loop._last_overrun_log = {}
    loop._overrun_repeat_log_seconds = 30.0
    loop._bus = get_event_bus()
    return loop


# ---------------------------------------------------------------------------
# budget normalization
# ---------------------------------------------------------------------------


def test_normalize_budget_defaults() -> None:
    budgets = AgentLoop._normalize_budget(None)
    assert budgets[AgentState.WATCHING.value] == 60.0
    assert budgets[AgentState.OBSERVING.value] == 45.0
    assert budgets[AgentState.THINKING.value] == 30.0
    assert budgets[AgentState.WAITING.value] == 60.0
    assert budgets[AgentState.WAITING_FOR_START_FIELD.value] == 0.0
    assert budgets[AgentState.TYPING.value] == 10.0


def test_normalize_budget_flat() -> None:
    budgets = AgentLoop._normalize_budget(25.0)
    assert budgets[AgentState.TYPING.value] == 25.0
    assert budgets[AgentState.CLICKING.value] == 25.0


def test_normalize_budget_dict() -> None:
    budgets = AgentLoop._normalize_budget({"typing": 5.0, "clicking": 8.0})
    assert budgets["typing"] == 5.0
    assert budgets["clicking"] == 8.0
    # Unlisted states fall back to the default at lookup time.
    assert budgets.get(AgentState.PLANNING.value, 10.0) == 10.0


# ---------------------------------------------------------------------------
# overrun detection
# ---------------------------------------------------------------------------


def test_no_overrun_below_budget() -> None:
    loop = _bare_loop()
    loop._state_budget = {AgentState.TYPING.value: 10.0}
    loop._states.force(AgentState.TYPING)
    loop._state_entered[AgentState.TYPING] = time.time()
    loop._bus.clear()
    loop._check_state_budget()
    assert loop._bus.history(EventType.RECOVERY) == []
    assert loop._state_overruns.get(AgentState.TYPING, 0) == 0


def test_zero_budget_never_fires() -> None:
    loop = _bare_loop()
    loop._state_budget = {AgentState.WAITING_FOR_START_FIELD.value: 0.0}
    loop._states.force(AgentState.WAITING_FOR_START_FIELD)
    loop._state_entered[AgentState.WAITING_FOR_START_FIELD] = time.time() - 999.0
    loop._bus.clear()
    loop._check_state_budget()
    assert loop._bus.history(EventType.RECOVERY) == []


def test_overrun_publishes_recovery_once(monkeypatch) -> None:
    loop = _bare_loop()
    loop._state_budget = {AgentState.TYPING.value: 10.0}
    loop._states.force(AgentState.TYPING)
    loop._state_entered[AgentState.TYPING] = time.time() - 20.0
    loop._bus.clear()

    # First tick: fires.
    loop._check_state_budget()
    assert len(loop._bus.history(EventType.RECOVERY)) == 1
    assert loop._state_overruns[AgentState.TYPING] == 1

    # Immediate second tick: suppressed (already warned, inside repeat window).
    loop._check_state_budget()
    assert len(loop._bus.history(EventType.RECOVERY)) == 1
    assert loop._state_overruns[AgentState.TYPING] == 2


def test_overrun_repeats_after_interval(monkeypatch) -> None:
    loop = _bare_loop()
    loop._overrun_repeat_log_seconds = 0.1
    loop._state_budget = {AgentState.TYPING.value: 10.0}
    loop._states.force(AgentState.TYPING)
    loop._state_entered[AgentState.TYPING] = time.time() - 20.0
    loop._bus.clear()

    loop._check_state_budget()
    assert len(loop._bus.history(EventType.RECOVERY)) == 1

    time.sleep(0.15)
    loop._check_state_budget()
    assert len(loop._bus.history(EventType.RECOVERY)) == 2
    last = loop._bus.history(EventType.RECOVERY)[-1]
    assert last.data["overruns"] == 2


def test_set_resets_overrun_counter() -> None:
    loop = _bare_loop()
    loop._state_budget = {AgentState.TYPING.value: 10.0}
    loop._states.force(AgentState.TYPING)
    loop._state_entered[AgentState.TYPING] = time.time() - 20.0
    loop._check_state_budget()
    assert loop._state_overruns[AgentState.TYPING] == 1

    # Re-enter the same state: the overrun counter resets and never resurfaces.
    loop._set(AgentState.TYPING)
    assert loop._state_overruns[AgentState.TYPING] == 0


# ---------------------------------------------------------------------------
# watchdog.json summary
# ---------------------------------------------------------------------------


def test_dump_watchdog(monkeypatch, tmp_path) -> None:
    from atlas.core.logging import logger

    loop = _bare_loop()
    loop._debug_dir = tmp_path
    loop._state_budget = {AgentState.TYPING.value: 10.0}
    loop._states.force(AgentState.TYPING)
    loop._state_entered[AgentState.TYPING] = time.time() - 20.0
    loop._bus.clear()
    loop._check_state_budget()

    # A level-1 style focus recovery event on the same bus.
    loop._bus.publish(EventType.RECOVERY, {"reason": "Focus lost. Waiting for MPF.", "state": "paused"})

    loop._dump_watchdog()
    payload = (tmp_path / "watchdog.json").read_text(encoding="utf-8")
    assert '"level2_state_overruns"' in payload
    assert '"typing": 1' in payload
    assert '"level1_focus_events"' in payload
    assert "Focus lost." in payload

    _ = logger  # (avoid unused import lint noise in bare-loop construction)