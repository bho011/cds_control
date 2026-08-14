"""Phase 2: Zirkulieren/Halten für eine feste Zeit."""

from __future__ import annotations

import time
from typing import Any

from ..common import make_level_history
from .phases import TankCleaningPhase
from .status_publisher import publish_status


def run_hold_phase(controller, settings: dict[str, Any], actuators) -> bool:
    """
    Returns True if the hold time completed, False if cancelled.

    No explicit sensor-loss abort here by design, not by omission: this
    phase only runs circulation pumps, and AutoCirculationController.update()
    already fails safe on a None reading by turning them off
    (process/auto_circulation.py). Nothing here fills or drains, so a
    lost sensor can't cause an overflow or dry-run. If the sensor stays
    lost for the rest of the hold, run_drain_phase() re-reads and aborts on
    its own start-of-phase check (missing_mixer_level_before_drain)
    before any drain pump ever turns on - that is the real safety net
    for a sensor that never recovers.
    """

    controller._change_phase(TankCleaningPhase.HOLDING)
    hold_seconds = float(settings.get("cleaning_hold_seconds", 300.0))
    controller._hold_seconds = hold_seconds
    controller._last_message = f"Circulating for {hold_seconds:.0f}s to flush the tank."

    level_history = make_level_history(settings)
    hold_start = time.monotonic()

    publish_status(controller, actuators, TankCleaningPhase.HOLDING)

    while True:
        if controller._stop_event.wait(0.5):
            return False

        elapsed = time.monotonic() - hold_start
        liters = controller._read_mixer_liters(settings, level_history)
        controller._auto_circulation.update(liters)

        controller._last_message = (
            f"Circulating: {elapsed:.0f}/{hold_seconds:.0f}s"
        )
        publish_status(controller, actuators, TankCleaningPhase.HOLDING)

        if elapsed >= hold_seconds:
            controller._last_message = "Hold phase complete."
            return True
