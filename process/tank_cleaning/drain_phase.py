"""Phase 3: Mixing Tank über Transferpumpe + Drain-Ventil entleeren."""

from __future__ import annotations

import time
from typing import Any

from ..common import make_level_history
from ..drain_safety import (
    DrainSafetyLimits,
    DrainSafetySettingsError,
    DrainSensorWatchdog,
    filtered_after_observation,
    safe_stop_drain,
)
from ..watchdog import EmptyConfirmCounter
from .phases import TankCleaningPhase
from .status_publisher import publish_status


def run_drain_phase(controller, settings: dict[str, Any], actuators, transfer_pump, drain_valve) -> None:
    controller._change_phase(TankCleaningPhase.DRAINING)
    controller._last_message = "Draining Mixing Tank."

    level_history = make_level_history(settings)
    start_liters = controller._read_mixer_liters(settings, level_history)

    if start_liters is None:
        controller._fail("missing_mixer_level_before_drain", "No Mixing Tank level available before drain.")
        return

    # AP-04A: every setting that feeds the timeout, settle time, empty detection
    # and plausibility checks is validated BEFORE any actuator is touched
    # (fail closed).
    try:
        safety_limits = DrainSafetyLimits.from_settings(settings, start_liters)
    except DrainSafetySettingsError as exc:
        controller._fail("invalid_drain_safety_settings", f"Drain blocked, unusable safety settings: {exc}")
        return

    empty_threshold = safety_limits.empty_threshold_liters
    empty_confirm_samples = safety_limits.empty_confirm_samples

    if start_liters <= empty_threshold:
        controller._last_message = "Tank already empty, nothing to drain."
        return

    max_drain_seconds = safety_limits.max_drain_seconds
    start_stamp = controller._last_snapshot_stamp  # snapshot the start level came from
    valve_settle_seconds = safety_limits.valve_settle_seconds

    try:
        # Start: valve open -> settle -> re-check stop -> pump on.
        drain_valve.on()

        if controller._stop_event.wait(valve_settle_seconds):
            return

        if controller._stop_event.is_set():
            return

        transfer_pump.on()
        sensor_watchdog = DrainSensorWatchdog(safety_limits, time.monotonic(), start_stamp)
        publish_status(controller, actuators, TankCleaningPhase.DRAINING)

        drain_start = time.monotonic()
        empty_confirm = EmptyConfirmCounter(empty_threshold, empty_confirm_samples)

        while True:
            if controller._stop_event.wait(0.5):
                return

            elapsed = time.monotonic() - drain_start
            history_before = list(level_history)
            liters = controller._read_mixer_liters(settings, level_history)
            # `now` is taken AFTER the read, so a slow read can not hide an expired deadline.
            observation = sensor_watchdog.observe(
                controller._last_raw_liters, controller._last_snapshot_stamp, time.monotonic()
            )
            # A re-read of the same snapshot must not weight the moving average twice.
            liters = filtered_after_observation(
                level_history,
                history_before,
                liters,
                controller._last_raw_liters is not None,
                observation.is_new_sample,
            )
            controller._set_current_liters(liters)
            controller._auto_circulation.update(liters)

            if observation.violation is not None:
                reason, message = observation.violation
                controller._fail(reason, message)
                return

            # A repeated snapshot is not an independent measurement and must
            # not advance the empty confirmation.
            if observation.is_new_sample:
                is_empty_confirmed = empty_confirm.update(liters)
            else:
                is_empty_confirmed = False

            controller._last_message = (
                f"Draining: {liters if liters is not None else '-'} L "
                f"(elapsed {elapsed:.0f}/{max_drain_seconds:.0f}s)"
            )
            publish_status(controller, actuators, TankCleaningPhase.DRAINING)

            if is_empty_confirmed:
                controller._last_message = f"Tank emptied ({liters:.2f} L remaining)."
                return

            if elapsed >= max_drain_seconds:
                controller._fail(
                    "calculated_drain_timeout",
                    "Drain timeout reached before empty confirmation.",
                )
                return
    finally:
        # Stop: transfer pump OFF, then drain valve OFF (both always attempted).
        # An exception from the body keeps propagating to the controller after
        # this runs; cleanup failures are recorded, never swallowed (O1: the
        # behaviour after a failing pump switch-off is intentionally unchanged).
        cleanup = safe_stop_drain(transfer_pump, drain_valve)
        if not cleanup.ok:
            controller._record_cleanup_failure(cleanup.errors)
