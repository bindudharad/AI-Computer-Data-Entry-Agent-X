"""Unit tests for the universal performance report."""

from __future__ import annotations

from atlas.universal.performance import UniversalPerformanceReport


def test_ok_when_existing_target_not_relaunched() -> None:
    report = UniversalPerformanceReport(attach_mode="EXISTING_WINDOW", launch_count=0)
    assert report.ok is True


def test_not_ok_when_existing_target_relaunched() -> None:
    report = UniversalPerformanceReport(attach_mode="EXISTING_BROWSER_TAB", launch_count=1)
    assert report.ok is False


def test_new_launch_is_ok_when_launch_count_reflects_new_instance() -> None:
    report = UniversalPerformanceReport(attach_mode="NEW_LAUNCH", launch_count=1)
    assert report.ok is True


def test_avg_field_ms() -> None:
    report = UniversalPerformanceReport(total_ms=1000.0, field_count=4)
    assert report.avg_field_ms == 250.0


def test_avg_field_ms_zero_when_no_fields() -> None:
    report = UniversalPerformanceReport(total_ms=1000.0, field_count=0)
    assert report.avg_field_ms == 0.0


def test_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "perf.json"
    report = UniversalPerformanceReport(
        target="Portal",
        environment="WEB_BROWSER",
        attach_mode="EXISTING_BROWSER_TAB",
        field_count=3,
        verified_count=3,
        total_ms=600.0,
        method_usage={"dom": 2, "uia": 1},
    )
    report.save(path)
    loaded = UniversalPerformanceReport.from_file(path)
    assert loaded.target == "Portal"
    assert loaded.environment == "WEB_BROWSER"
    assert loaded.attach_mode == "EXISTING_BROWSER_TAB"
    assert loaded.field_count == 3
    assert loaded.method_usage == {"dom": 2, "uia": 1}
    assert loaded.ok is True
