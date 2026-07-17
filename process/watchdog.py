"""
Shared progress/timeout watchdog logic for fill and drain phases.

Extracted from process/refill.py, process/tank_cleaning.py (fill and drain
phases), and statemachine/fill_and_measure_state_machine.py, which each
independently re-implemented the same "is this fill/drain making progress,
has it run too long, did the level drop implausibly" checks - see the
Architecture-Hardening-Roadmap plan, Phase 2.

This module owns only the CHECKING logic. Callers still read their own
settings values (this deliberately does not hide or default config values -
that is a separate, later concern) and still own their own loop/sleep/
actuator code, since that genuinely differs per phase. Pure logic, no
threading/GPIO/I-O of any kind - safe to unit-test without any hardware.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillWatchdog:
    """
    Call check() once per loop iteration of a fill phase. Returns
    (reason, message) if the fill should abort, or None if still fine to
    continue.
    """

    start_liters: float
    max_liters: float
    max_seconds: float
    no_progress_timeout_seconds: float
    min_progress_liters: float
    max_negative_drift_liters: float

    def check(
        self,
        current_liters: float | None,
        elapsed_seconds: float,
        max_elapsed_seconds: float | None = None,
    ) -> tuple[str, str] | None:
        """
        `elapsed_seconds` is the time since the fill itself started, used
        for the no-progress check. `max_elapsed_seconds` only needs to be
        passed separately if the caller measures its overall timeout
        against a different clock - the one real difference found between
        the three fill sites this was extracted from:
        FillAndMeasureStateMachine measures max_fill_seconds against the
        whole process's elapsed time, not just the fill phase's. Defaults
        to `elapsed_seconds` for callers where both are the same clock.
        """
        if max_elapsed_seconds is None:
            max_elapsed_seconds = elapsed_seconds

        if current_liters is None:
            return (
                "missing_mixer_level_during_fill",
                "Mixer level lost during fill.",
            )

        if current_liters > self.max_liters:
            return (
                "mixer_over_max_limit",
                f"Mixer level {current_liters:.2f} L exceeded max {self.max_liters:.2f} L.",
            )

        added = current_liters - self.start_liters

        if added < -self.max_negative_drift_liters:
            return (
                "negative_level_drift",
                f"Mixer level dropped implausibly during fill ({added:.2f} L).",
            )

        if elapsed_seconds >= self.no_progress_timeout_seconds and added < self.min_progress_liters:
            return (
                "no_fill_progress_timeout",
                f"No plausible fill progress. Added={added:.2f} L after {elapsed_seconds:.1f}s.",
            )

        if max_elapsed_seconds >= self.max_seconds:
            return (
                "max_fill_timeout",
                f"Max fill timeout reached after {max_elapsed_seconds:.1f}s.",
            )

        return None


def calculate_drain_timeout_seconds(
    start_liters: float,
    pump_liters_per_minute: float,
    buffer_seconds: float,
) -> float:
    """Worst-case runtime to drain start_liters at the given pump rate, plus a safety buffer."""
    return (start_liters / pump_liters_per_minute) * 60.0 + buffer_seconds


class EmptyConfirmCounter:
    """
    Tracks consecutive readings at/under an empty threshold. update()
    returns True once `required_samples` consecutive readings have
    qualified - a single low reading (sensor noise) is not enough.
    """

    def __init__(self, threshold_liters: float, required_samples: int):
        self.threshold_liters = threshold_liters
        self.required_samples = required_samples
        self.count = 0

    def update(self, current_liters: float | None) -> bool:
        if current_liters is not None and current_liters <= self.threshold_liters:
            self.count += 1
        else:
            self.count = 0

        return self.count >= self.required_samples
