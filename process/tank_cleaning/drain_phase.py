"""Phase 3: Mixing Tank über Transferpumpe + Drain-Ventil entleeren."""

from __future__ import annotations

import time
from typing import Any

from ..common import make_level_history
from ..watchdog import EmptyConfirmCounter, calculate_drain_timeout_seconds
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

    empty_threshold = float(settings.get("empty_threshold_liters", 0.3))
    empty_confirm_samples = int(settings.get("empty_confirm_samples", 5))

    if start_liters <= empty_threshold:
        controller._last_message = "Tank already empty, nothing to drain."
        return

    pump_lpm = float(settings.get("transfer_pump_liters_per_minute", 16.0))
    buffer_seconds = float(settings.get("drain_timeout_buffer_seconds", 180.0))
    max_drain_seconds = calculate_drain_timeout_seconds(start_liters, pump_lpm, buffer_seconds)
    valve_settle_seconds = float(settings.get("valve_settle_seconds", 1.0))

    drain_valve.on()

    if controller._stop_event.wait(valve_settle_seconds):
        return

    transfer_pump.on()
    publish_status(controller, actuators, TankCleaningPhase.DRAINING)

    drain_start = time.monotonic()
    empty_confirm = EmptyConfirmCounter(empty_threshold, empty_confirm_samples)

    try:
        while True:
            if controller._stop_event.wait(0.5):
                return

            elapsed = time.monotonic() - drain_start
            liters = controller._read_mixer_liters(settings, level_history)
            controller._auto_circulation.update(liters)

            is_empty_confirmed = empty_confirm.update(liters)

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
        transfer_pump.off()
        drain_valve.off()
