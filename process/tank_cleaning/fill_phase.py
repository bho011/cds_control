"""Phase 1: Mixing Tank mit RO-Wasser befüllen."""

from __future__ import annotations

import time
from typing import Any

from ..common import make_level_history, ro_liters_from_snapshot
from ..watchdog import FillWatchdog
from .phases import TankCleaningPhase
from .status_publisher import publish_status


def run_fill_phase(controller, settings: dict[str, Any], actuators, refill_pump) -> bool:
    """Returns True if the target level was reached, False otherwise."""

    controller._change_phase(TankCleaningPhase.FILLING)
    controller._last_message = "Filling Mixing Tank with RO water."

    level_history = make_level_history(settings)
    target_total = float(settings.get("target_fill_total_liters", 200.0))
    max_mixer_liters = float(settings.get("max_mixer_liters", 200.0))
    max_fill_seconds = float(settings.get("max_fill_seconds", 900.0))
    no_progress_timeout = float(settings.get("no_fill_progress_timeout_seconds", 30.0))
    min_progress = float(settings.get("min_fill_progress_liters", 0.5))
    max_negative_drift = float(settings.get("max_negative_level_drift_liters", 3.0))
    required_confirm = int(settings.get("target_reached_confirm_samples", 3))
    min_ro_liters = float(settings.get("min_ro_liters_required", 20.0))

    snapshot = controller.get_sensor_snapshot()
    start_liters = controller._read_mixer_liters(settings, level_history) if snapshot else None

    if start_liters is None:
        controller._fail("missing_mixer_level", "No Mixing Tank level available before fill.")
        return False

    ro_liters = ro_liters_from_snapshot(snapshot)
    if ro_liters is None:
        controller._fail("missing_ro_level", "No RO Tank level available before fill.")
        return False

    if ro_liters < min_ro_liters:
        controller._fail(
            "not_enough_ro_water",
            f"Not enough RO water: {ro_liters:.1f} L available, "
            f"{min_ro_liters:.1f} L required.",
        )
        return False

    if start_liters >= target_total:
        controller._last_message = f"Mixing Tank already at or above target ({start_liters:.1f} L)."
        return True

    refill_pump.on()
    publish_status(controller, actuators, TankCleaningPhase.FILLING)

    watchdog = FillWatchdog(
        start_liters=start_liters,
        max_liters=max_mixer_liters,
        max_seconds=max_fill_seconds,
        no_progress_timeout_seconds=no_progress_timeout,
        min_progress_liters=min_progress,
        max_negative_drift_liters=max_negative_drift,
    )

    fill_start = time.monotonic()
    target_confirm_count = 0

    try:
        while True:
            if controller._stop_event.wait(0.5):
                return False

            elapsed = time.monotonic() - fill_start
            liters = controller._read_mixer_liters(settings, level_history)
            controller._auto_circulation.update(liters)

            watchdog_result = watchdog.check(liters, elapsed)
            if watchdog_result is not None:
                reason, message = watchdog_result
                controller._fail(reason, message)
                return False

            target_confirm_count = target_confirm_count + 1 if liters >= target_total else 0

            controller._last_message = (
                f"Filling: {liters:.1f}/{target_total:.1f} L "
                f"(elapsed {elapsed:.0f}s)"
            )
            publish_status(controller, actuators, TankCleaningPhase.FILLING)

            if target_confirm_count >= required_confirm:
                controller._last_message = f"Fill complete: {liters:.1f} L."
                return True
    finally:
        refill_pump.off()
