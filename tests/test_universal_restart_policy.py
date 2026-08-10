"""Unit tests for the restart policy - the 'never relaunch' guarantee."""

from __future__ import annotations

import pytest

from atlas.universal.restart_policy import RestartMode, RestartPolicy


def _policy(mode: RestartMode = RestartMode.ON_CRASH_ONLY, auto: bool = True) -> RestartPolicy:
    return RestartPolicy(mode=mode, auto_launch_target=auto)


def test_existing_target_never_launched() -> None:
    p = _policy(RestartMode.AUTO, auto=True)
    assert p.permit_launch(target_missing=False, reason="", crash_detected=True) is False


def test_no_auto_launch_means_never() -> None:
    p = _policy(RestartMode.AUTO, auto=False)
    assert p.permit_launch(target_missing=True, reason="crash_detected", crash_detected=True) is False


def test_never_mode_blocks_even_crash() -> None:
    p = _policy(RestartMode.NEVER, auto=True)
    assert p.permit_launch(target_missing=True, reason="crash_detected", crash_detected=True) is False


def test_crash_only_requires_crash_trigger() -> None:
    p = _policy(RestartMode.ON_CRASH_ONLY, auto=True)
    assert p.permit_launch(target_missing=True, reason="cdp_unavailable", crash_detected=False) is False
    assert p.permit_launch(target_missing=True, reason="crash_detected", crash_detected=True) is True


def test_on_user_request_allows_when_auto_enabled() -> None:
    p = _policy(RestartMode.ON_USER_REQUEST, auto=True)
    assert p.permit_launch(target_missing=True, reason="user_requested", crash_detected=False) is True


def test_health_disconnected_is_not_missing() -> None:
    p = _policy()
    assert p.classify_health(process_alive=True, cdp_available=False) == "DISCONNECTED"


def test_health_missing_only_when_process_dead() -> None:
    p = _policy()
    assert p.classify_health(process_alive=False) == "MISSING"
    assert p.classify_health(process_alive=True, cdp_available=True) == "HEALTHY"


def test_classify_missing_rejects_non_crash_reasons() -> None:
    p = _policy()
    assert p.classify_missing("cdp_unavailable") is False
    assert p.classify_missing("tab_not_found") is False
    assert p.classify_missing("verification_failed") is False
    assert p.classify_missing("crash_detected") is True
    assert p.classify_missing("process terminated") is True


def test_to_dict() -> None:
    p = _policy(RestartMode.ON_CRASH_ONLY, auto=True)
    assert p.to_dict() == {"mode": "ON_CRASH_ONLY", "auto_launch_target": True}


def test_invalid_mode_raises() -> None:
    with pytest.raises(ValueError):
        RestartPolicy(mode="BOGUS")  # type: ignore[arg-type]
