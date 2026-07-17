"""
No-hardware regression test for the Phase 2 watchdog deduplication (see the
"Architecture-Hardening-Roadmap" plan, Phase 2).

FillWatchdog/calculate_drain_timeout_seconds/EmptyConfirmCounter are pure
logic - no threading, no GPIO, no I/O - so this is a plain assertion script,
safe to run anywhere at any time.

Usage:
    cd ~/cds_control && .venv/bin/python3 scripts/manual_tests/phase2_watchdog_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

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


def test_fill_watchdog_no_issue() -> None:
    print("[TEST] FillWatchdog: gesunder Fortschritt -> kein Abbruch ...")
    wd = make_watchdog()
    assert wd.check(current_liters=15.0, elapsed_seconds=5.0) is None
    print("  [OK]")


def test_fill_watchdog_missing_reading() -> None:
    print("[TEST] FillWatchdog: fehlender Messwert ...")
    wd = make_watchdog()
    result = wd.check(current_liters=None, elapsed_seconds=5.0)
    assert result is not None
    reason, _ = result
    assert reason == "missing_mixer_level_during_fill", reason
    print("  [OK]")


def test_fill_watchdog_over_max() -> None:
    print("[TEST] FillWatchdog: Überschreitung max_liters ...")
    wd = make_watchdog(max_liters=50.0)
    result = wd.check(current_liters=51.0, elapsed_seconds=5.0)
    assert result is not None
    reason, _ = result
    assert reason == "mixer_over_max_limit", reason
    print("  [OK]")


def test_fill_watchdog_negative_drift() -> None:
    print("[TEST] FillWatchdog: implausibler Rückgang (negative Drift) ...")
    wd = make_watchdog(start_liters=20.0, max_negative_drift_liters=3.0)
    result = wd.check(current_liters=16.0, elapsed_seconds=5.0)  # added = -4.0, allowed = -3.0
    assert result is not None
    reason, _ = result
    assert reason == "negative_level_drift", reason
    print("  [OK]")


def test_fill_watchdog_no_progress_timeout() -> None:
    print("[TEST] FillWatchdog: Kein-Fortschritt-Timeout ...")
    wd = make_watchdog(start_liters=10.0, no_progress_timeout_seconds=30.0, min_progress_liters=1.0)
    # elapsed past the no-progress window, but added liters below min_progress.
    result = wd.check(current_liters=10.5, elapsed_seconds=31.0)
    assert result is not None
    reason, _ = result
    assert reason == "no_fill_progress_timeout", reason

    # Same elapsed time, but WITH enough progress -> must not fire.
    assert wd.check(current_liters=12.0, elapsed_seconds=31.0) is None
    print("  [OK]")


def test_fill_watchdog_max_timeout() -> None:
    print("[TEST] FillWatchdog: Gesamt-Timeout ...")
    wd = make_watchdog(max_seconds=100.0)
    result = wd.check(current_liters=15.0, elapsed_seconds=101.0)
    assert result is not None
    reason, _ = result
    assert reason == "max_fill_timeout", reason
    print("  [OK]")


def test_fill_watchdog_separate_max_elapsed() -> None:
    print("[TEST] FillWatchdog: getrennter max_elapsed_seconds (State-Machine-Fall) ...")
    wd = make_watchdog(max_seconds=100.0, no_progress_timeout_seconds=1000.0)
    # fill_elapsed (used for no-progress) is small, process_elapsed (used for
    # max_seconds) is what should trigger here - mirrors
    # FillAndMeasureStateMachine's process_elapsed vs. fill_elapsed split.
    result = wd.check(current_liters=15.0, elapsed_seconds=5.0, max_elapsed_seconds=150.0)
    assert result is not None
    reason, _ = result
    assert reason == "max_fill_timeout", reason

    # Without exceeding max_elapsed_seconds, must not fire even if elapsed_seconds is small.
    assert wd.check(current_liters=15.0, elapsed_seconds=5.0, max_elapsed_seconds=50.0) is None
    print("  [OK]")


def test_calculate_drain_timeout_seconds() -> None:
    print("[TEST] calculate_drain_timeout_seconds() ...")
    # 32 L at 16 L/min = 120s expected, + 180s buffer = 300s.
    result = calculate_drain_timeout_seconds(start_liters=32.0, pump_liters_per_minute=16.0, buffer_seconds=180.0)
    assert result == 300.0, result
    print("  [OK]")


def test_empty_confirm_counter() -> None:
    print("[TEST] EmptyConfirmCounter Zustandsübergänge ...")
    counter = EmptyConfirmCounter(threshold_liters=0.3, required_samples=3)

    assert counter.update(5.0) is False  # well above threshold
    assert counter.count == 0

    assert counter.update(0.2) is False  # 1st qualifying sample
    assert counter.count == 1
    assert counter.update(0.1) is False  # 2nd qualifying sample
    assert counter.count == 2

    assert counter.update(5.0) is False  # interruption resets the streak
    assert counter.count == 0

    assert counter.update(0.2) is False
    assert counter.update(0.2) is False
    assert counter.update(0.2) is True  # 3rd consecutive qualifying sample -> confirmed
    assert counter.count == 3

    # None readings must reset too, same as an above-threshold reading.
    counter2 = EmptyConfirmCounter(threshold_liters=0.3, required_samples=2)
    assert counter2.update(0.1) is False
    assert counter2.update(None) is False
    assert counter2.count == 0
    print("  [OK]")


def main() -> None:
    print("=" * 60)
    print("Phase 2 Watchdog - Regressionstest (reine Logik, keine Hardware)")
    print("=" * 60)

    test_fill_watchdog_no_issue()
    test_fill_watchdog_missing_reading()
    test_fill_watchdog_over_max()
    test_fill_watchdog_negative_drift()
    test_fill_watchdog_no_progress_timeout()
    test_fill_watchdog_max_timeout()
    test_fill_watchdog_separate_max_elapsed()
    test_calculate_drain_timeout_seconds()
    test_empty_confirm_counter()

    print()
    print("[RESULT] Alle Phase-2-Watchdog-Tests erfolgreich.")


if __name__ == "__main__":
    main()
