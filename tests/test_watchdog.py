"""
Phase 2 regression tests: process/watchdog.py (FillWatchdog,
calculate_drain_timeout_seconds, EmptyConfirmCounter). Pure logic, no
threading/GPIO/I-O of any kind.
"""

from __future__ import annotations

import pytest

from process.watchdog import EmptyConfirmCounter, FillWatchdog, calculate_drain_timeout_seconds


def make_watchdog(**overrides) -> FillWatchdog:
    defaults = dict(
        start_liters=10.0,
        max_liters=200.0,
        max_seconds=600.0,
        no_progress_timeout_seconds=30.0,
        min_progress_liters=0.5,
        max_negative_drift_liters=3.0,
    )
    defaults.update(overrides)
    return FillWatchdog(**defaults)


def test_healthy_progress_does_not_abort():
    wd = make_watchdog()
    assert wd.check(current_liters=15.0, elapsed_seconds=5.0) is None


def test_missing_reading_aborts():
    wd = make_watchdog()
    reason, _ = wd.check(current_liters=None, elapsed_seconds=5.0)
    assert reason == "missing_mixer_level_during_fill"


def test_over_max_liters_aborts():
    wd = make_watchdog(max_liters=50.0)
    reason, _ = wd.check(current_liters=51.0, elapsed_seconds=5.0)
    assert reason == "mixer_over_max_limit"


def test_negative_drift_aborts():
    wd = make_watchdog(start_liters=20.0, max_negative_drift_liters=3.0)
    reason, _ = wd.check(current_liters=16.0, elapsed_seconds=5.0)  # added = -4.0, allowed = -3.0
    assert reason == "negative_level_drift"


def test_no_progress_timeout_aborts_only_without_enough_progress():
    wd = make_watchdog(start_liters=10.0, no_progress_timeout_seconds=30.0, min_progress_liters=1.0)

    reason, _ = wd.check(current_liters=10.5, elapsed_seconds=31.0)
    assert reason == "no_fill_progress_timeout"

    assert wd.check(current_liters=12.0, elapsed_seconds=31.0) is None


def test_max_seconds_timeout_aborts():
    wd = make_watchdog(max_seconds=100.0)
    reason, _ = wd.check(current_liters=15.0, elapsed_seconds=101.0)
    assert reason == "max_fill_timeout"


def test_separate_max_elapsed_seconds_argument():
    """
    Mirrors FillAndMeasureStateMachine, the one call site that measures
    max_fill_seconds against the whole process's elapsed time rather than
    just the fill phase's - see FillWatchdog.check()'s docstring.
    """
    wd = make_watchdog(max_seconds=100.0, no_progress_timeout_seconds=1000.0)

    reason, _ = wd.check(current_liters=15.0, elapsed_seconds=5.0, max_elapsed_seconds=150.0)
    assert reason == "max_fill_timeout"

    assert wd.check(current_liters=15.0, elapsed_seconds=5.0, max_elapsed_seconds=50.0) is None


def test_calculate_drain_timeout_seconds():
    # 32 L at 16 L/min = 120s expected, + 180s buffer = 300s.
    result = calculate_drain_timeout_seconds(start_liters=32.0, pump_liters_per_minute=16.0, buffer_seconds=180.0)
    assert result == 300.0


class TestEmptyConfirmCounter:
    def test_above_threshold_never_confirms(self):
        counter = EmptyConfirmCounter(threshold_liters=0.3, required_samples=3)
        assert counter.update(5.0) is False
        assert counter.count == 0

    def test_confirms_after_required_consecutive_samples(self):
        counter = EmptyConfirmCounter(threshold_liters=0.3, required_samples=3)
        assert counter.update(0.2) is False
        assert counter.update(0.1) is False
        assert counter.update(0.2) is True
        assert counter.count == 3

    def test_interruption_resets_the_streak(self):
        counter = EmptyConfirmCounter(threshold_liters=0.3, required_samples=3)
        counter.update(0.2)
        counter.update(0.2)
        assert counter.update(5.0) is False  # interruption
        assert counter.count == 0

    def test_none_reading_resets_like_an_above_threshold_reading(self):
        counter = EmptyConfirmCounter(threshold_liters=0.3, required_samples=2)
        assert counter.update(0.1) is False
        assert counter.update(None) is False
        assert counter.count == 0
