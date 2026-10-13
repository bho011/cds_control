"""
Shared safety helpers for the three drain paths (AP-04A):
process/drain.py, process/tank_cleaning/drain_phase.py and
process/manual_drain_jog.py.

Pure logic, no GPIO/MQTT/threading of its own - safe to unit-test without
hardware. Callers keep their own loop, sleep and stop-event code.

Two things live here:

* DrainSensorWatchdog - runtime plausibility/loss/progress checks on the
  UNFILTERED mixer level while the transfer pump drains the tank.
* safe_stop_drain - the one stop sequence (transfer pump OFF, then drain valve
  OFF) with isolated, recorded errors.

What this module does NOT do (see AP-04A report):

* A level of 0 L is NOT proof that the tank is physically empty. The mixer
  level calibration is only fitted for >= 10 L and the bridge clamps negative
  results to 0.0, so "0.0 L" means "below the calibrated range". Nothing here
  can prevent dry running of the transfer pump; it only bounds how long the
  pump keeps running when the sensor signal is lost or implausible.
* safe_stop_drain reports that the switch-off CALL returned without an
  exception. There is no physical feedback from the pump or valve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from statemachine.sensor_reading import finite_number

from .common import mixer_liters_from_snapshot
from .watchdog import calculate_drain_timeout_seconds

# Provisional upper bound for a sustained sensor loss. It is the freshness
# window already used for every sensor read (read_metrics / dashboard
# get_latest(max_age_seconds=5.0)). It is NOT hardware validated: no
# measurement exists that shows how long a transfer pump may run without a
# usable level reading.
SENSOR_LOSS_MAX_SECONDS = 5.0

# Existing literal from the former "no drain progress" warning in
# process/drain.py: less than this drained volume counts as "no progress".
MIN_DRAIN_PROGRESS_LITERS = 0.3

DEFAULT_NO_PROGRESS_SECONDS = 60.0
DEFAULT_MAX_RISE_LITERS = 3.0


class DrainSafetySettingsError(ValueError):
    """Raised before any actuator is touched if drain safety settings are unusable."""


@dataclass(frozen=True)
class DrainSafetyLimits:
    start_liters: float
    max_liters: float
    max_rise_liters: float
    no_progress_seconds: float
    transfer_pump_liters_per_minute: float = 16.0
    drain_timeout_buffer_seconds: float = 180.0
    valve_settle_seconds: float = 1.0
    empty_threshold_liters: float = 0.3
    empty_confirm_samples: int = 5
    expected_seconds: float = 0.0
    max_drain_seconds: float = 0.0
    sensor_loss_seconds: float = SENSOR_LOSS_MAX_SECONDS

    @classmethod
    def from_settings(cls, settings: dict[str, Any], start_liters: float) -> "DrainSafetyLimits":
        """Strict parsing; anything unusable raises (fail closed, no hardware touched yet)."""
        if not isinstance(settings, dict):
            raise DrainSafetySettingsError("settings is not a dict")

        start = finite_number(start_liters)
        if start is None or start < 0:
            raise DrainSafetySettingsError(f"start level unusable: {start_liters!r}")

        max_liters = finite_number(settings.get("max_mixer_liters"))
        if max_liters is None or max_liters <= 0:
            raise DrainSafetySettingsError(
                f"max_mixer_liters must be a finite number > 0, got {settings.get('max_mixer_liters')!r}"
            )

        max_rise = finite_number(settings.get("max_negative_level_drift_liters", DEFAULT_MAX_RISE_LITERS))
        if max_rise is None or max_rise < 0:
            raise DrainSafetySettingsError(
                "max_negative_level_drift_liters must be a finite number >= 0, "
                f"got {settings.get('max_negative_level_drift_liters')!r}"
            )

        no_progress = finite_number(
            settings.get("no_drain_progress_warning_seconds", DEFAULT_NO_PROGRESS_SECONDS)
        )
        if no_progress is None or no_progress <= 0:
            raise DrainSafetySettingsError(
                "no_drain_progress_warning_seconds must be a finite number > 0, "
                f"got {settings.get('no_drain_progress_warning_seconds')!r}"
            )

        # Timeout inputs: a NaN/Infinity/zero here would make the calculated
        # drain timeout ineffective, so they are validated like the rest.
        pump_lpm = finite_number(settings.get("transfer_pump_liters_per_minute", 16.0))
        if pump_lpm is None or pump_lpm <= 0:
            raise DrainSafetySettingsError(
                "transfer_pump_liters_per_minute must be a finite number > 0, "
                f"got {settings.get('transfer_pump_liters_per_minute')!r}"
            )

        buffer_seconds = finite_number(settings.get("drain_timeout_buffer_seconds", 180.0))
        if buffer_seconds is None or buffer_seconds < 0:
            raise DrainSafetySettingsError(
                "drain_timeout_buffer_seconds must be a finite number >= 0, "
                f"got {settings.get('drain_timeout_buffer_seconds')!r}"
            )

        settle = finite_number(settings.get("valve_settle_seconds", 1.0))
        if settle is None or settle < 0:
            raise DrainSafetySettingsError(
                f"valve_settle_seconds must be a finite number >= 0, got {settings.get('valve_settle_seconds')!r}"
            )

        empty_threshold = finite_number(settings.get("empty_threshold_liters", 0.3))
        if empty_threshold is None or empty_threshold < 0:
            raise DrainSafetySettingsError(
                f"empty_threshold_liters must be a finite number >= 0, got {settings.get('empty_threshold_liters')!r}"
            )

        confirm = settings.get("empty_confirm_samples", 5)
        if isinstance(confirm, bool) or not isinstance(confirm, int) or confirm < 1:
            raise DrainSafetySettingsError(f"empty_confirm_samples must be an integer >= 1, got {confirm!r}")

        if start > max_liters:
            raise DrainSafetySettingsError(
                f"start level {start:.2f} L exceeds max_mixer_liters {max_liters:.2f} L"
            )

        # The finite single values can still combine to inf/NaN (e.g. a pump
        # rate of 1e-320 L/min): check the RESULT that actually bounds the pump.
        expected_seconds = (start / pump_lpm) * 60.0
        max_drain_seconds = calculate_drain_timeout_seconds(start, pump_lpm, buffer_seconds)
        if not (math.isfinite(expected_seconds) and math.isfinite(max_drain_seconds)):
            raise DrainSafetySettingsError(
                "calculated drain timeout is not finite "
                f"(start {start} L, pump {pump_lpm} L/min, buffer {buffer_seconds} s)"
            )
        if start > empty_threshold and max_drain_seconds <= 0:
            raise DrainSafetySettingsError(
                f"calculated drain timeout must be > 0 s, got {max_drain_seconds}"
            )

        return cls(
            start_liters=start,
            max_liters=max_liters,
            max_rise_liters=max_rise,
            no_progress_seconds=no_progress,
            transfer_pump_liters_per_minute=pump_lpm,
            drain_timeout_buffer_seconds=buffer_seconds,
            valve_settle_seconds=settle,
            empty_threshold_liters=empty_threshold,
            empty_confirm_samples=confirm,
            expected_seconds=expected_seconds,
            max_drain_seconds=max_drain_seconds,
        )


def snapshot_stamp(snapshot: Any) -> str | None:
    """Sensor-bridge timestamp of a snapshot, or None if it has none."""
    if isinstance(snapshot, dict):
        stamp = snapshot.get("timestamp")
        if isinstance(stamp, str) and stamp.strip():
            return stamp
    return None


def parse_bridge_stamp(stamp: Any) -> datetime | None:
    """Parse a bridge timestamp (ISO 8601) or None if it is missing/invalid."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def raw_sample_from_snapshot(snapshot: Any, settings: dict[str, Any]) -> tuple[float | None, str | None]:
    """(unfiltered mixer liters or None, snapshot stamp) from one snapshot."""
    if not isinstance(snapshot, dict):
        return None, None
    return mixer_liters_from_snapshot(snapshot, settings), snapshot_stamp(snapshot)


@dataclass(frozen=True)
class DrainObservation:
    is_new_sample: bool
    violation: tuple[str, str] | None = None


class DrainSensorWatchdog:
    """
    Call observe() once per loop iteration after the transfer pump started.

    All times are monotonic seconds passed in by the caller (no clock access
    here, so tests need no sleeping). The caller must take `now` AFTER the
    sensor read returned, so a slow read cannot hide an expired deadline.
    `started_at` is the pump start.

    Order of checks (fail closed):
      1. Sensor-loss deadline - evaluated BEFORE any measurement of this call
         is accepted: a value arriving after the deadline cannot extend it.
      2. Plausibility of the unfiltered value (max level, rise over start).
      3. Rolling progress window.

    What counts as a NEW measurement: a valid, non-negative, finite value whose
    snapshot timestamp parses as ISO 8601 (datetime.fromisoformat) and is
    STRICTLY LATER than the timestamp of the last accepted measurement. The
    same or an older timestamp is not new even if the level differs, and a
    missing, unparsable or not comparable (naive vs. timezone-aware) timestamp
    can not be told apart from a re-read and is NOT new: it does not refresh the loss timer, is no progress evidence and
    must not advance the empty confirmation (callers check is_new_sample).
    The bridge publishes about once per second while the drain loops run at
    0.5 s, so the same snapshot is normally read twice.

    Limit: a fresh bridge timestamp only proves that the bridge published
    again. It does not prove that the physical sensor produced a new
    measurement (e.g. a value frozen upstream keeps getting new timestamps).
    That case is only caught by the progress window below.

    Progress window: progress means the level dropped by at least
    MIN_DRAIN_PROGRESS_LITERS below the last reference level (initially the
    start level). Each such accepted new measurement becomes the new reference
    and restarts the window. If no_progress_seconds pass without it, the drain
    aborts ("no_drain_progress"). The calculated total timeout is independent
    of this and stays with the caller.
    """

    def __init__(self, limits: DrainSafetyLimits, started_at: float, start_stamp: str | None = None) -> None:
        """`start_stamp`: timestamp of the snapshot the start level came from, so a
        re-read of that very snapshot is not taken for a new measurement."""
        self.limits = limits
        self.started_at = started_at
        self._last_new_valid_at = started_at
        self._last_stamp: datetime | None = parse_bridge_stamp(start_stamp)
        self._progress_ref_liters = limits.start_liters
        self._progress_ref_at = started_at
        self.latest_raw_liters: float = limits.start_liters

    def observe(self, raw_liters: Any, stamp: str | None, now: float) -> DrainObservation:
        limits = self.limits

        # 1. Deadline first, before accepting anything from this call.
        silent = now - self._last_new_valid_at
        if silent > limits.sensor_loss_seconds:
            return DrainObservation(
                False,
                (
                    "sensor_lost_during_drain",
                    f"No new valid mixer level for {silent:.1f}s "
                    f"(limit {limits.sensor_loss_seconds:.1f}s, provisional, not hardware validated).",
                ),
            )

        raw = finite_number(raw_liters)
        # A negative level is not physical (the calibrated path clamps at 0).
        if raw is not None and raw < 0:
            raw = None

        is_new = False
        if raw is not None:
            parsed = parse_bridge_stamp(stamp)
            try:
                is_new = parsed is not None and (self._last_stamp is None or parsed > self._last_stamp)
            except TypeError:  # naive vs. timezone-aware: not comparable
                is_new = False

            # Plausibility applies to every valid reading, new or repeated.
            if raw > limits.max_liters:
                return DrainObservation(
                    is_new,
                    (
                        "mixer_over_max_limit",
                        f"Mixer level {raw:.2f} L exceeded max {limits.max_liters:.2f} L during drain.",
                    ),
                )
            if raw > limits.start_liters + limits.max_rise_liters:
                return DrainObservation(
                    is_new,
                    (
                        "level_rise_during_drain",
                        f"Mixer level rose to {raw:.2f} L (start {limits.start_liters:.2f} L, "
                        f"allowed drift {limits.max_rise_liters:.2f} L) while draining.",
                    ),
                )

            if is_new:
                self._last_stamp = parsed
                self._last_new_valid_at = now
                self.latest_raw_liters = raw
                if self._progress_ref_liters - raw >= MIN_DRAIN_PROGRESS_LITERS:
                    self._progress_ref_liters = raw
                    self._progress_ref_at = now

        # 3. Rolling progress window.
        stalled = now - self._progress_ref_at
        if stalled >= limits.no_progress_seconds:
            return DrainObservation(
                is_new,
                (
                    "no_drain_progress",
                    f"Level did not drop by {MIN_DRAIN_PROGRESS_LITERS:.1f} L for {stalled:.1f}s "
                    f"(reference {self._progress_ref_liters:.2f} L, window {limits.no_progress_seconds:.1f}s).",
                ),
            )

        return DrainObservation(is_new)


def filtered_after_observation(
    level_history, history_before: list[float], filtered: float | None, raw_valid: bool, is_new: bool
) -> float | None:
    """
    Keep the moving-average history free of repeated snapshots.

    read_metrics()/_read_mixer_liters() append every valid read to the history.
    A re-read of the same snapshot is not an independent sample, so after the
    watchdog classified it, the history is restored to its state before the
    read and the unchanged average is returned. Invalid readings keep their
    existing semantics (filtered value None, history untouched by the reader).
    """
    if is_new or not raw_valid:
        return filtered
    level_history.clear()
    level_history.extend(history_before)
    if not history_before:
        return filtered
    return sum(history_before) / len(history_before)


@dataclass
class DrainCleanupResult:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only if both switch-off calls returned without an exception.

        This is a software result, not a physical confirmation.
        """
        return not self.errors


def safe_stop_drain(transfer_pump, drain_valve) -> DrainCleanupResult:
    """
    Stop sequence: transfer pump OFF, then drain valve OFF.

    Both calls are always attempted, each isolated, so a failing pump switch-off
    does not skip the valve (existing behaviour of the manual jog and, via
    safe_shutdown_all, of tank cleaning). Whether the valve SHOULD close while
    the pump state is unknown is an open hardware decision (O1) and is
    deliberately not encoded here. Errors are collected, never swallowed.
    """
    result = DrainCleanupResult()
    try:
        try:
            if transfer_pump is not None:
                transfer_pump.off()
        except Exception as exc:
            result.errors.append(f"transfer_pump.off() failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            if drain_valve is not None:
                drain_valve.off()
        except Exception as exc:
            result.errors.append(f"drain_valve.off() failed: {type(exc).__name__}: {exc}")
    return result


def shutdown_errors(result: Any) -> list[str]:
    """Normalize the list returned by ActuatorManager.safe_shutdown_all()/close_all()."""
    if isinstance(result, list):
        return [str(item) for item in result]
    return []
