"""
Server-side controller for the "Tank Cleaning" maintenance cycle.

Intended use: rinse the Mixing Tank with clean RO water after a mixing run,
before the next recipe starts.

Sequence:
    1. FILL    Mixer Refill Pump fills the Mixing Tank with RO water up to
               a target level (default 200 L).
    2. HOLD    Mixing/Sensor circulation pumps flush the tank and sensor box
               for a fixed duration (default 5 minutes).
    3. DRAIN   Transfer Pump + Drain Valve empty the tank again, using the
               same sensor-based empty detection and calculated timeout as
               process/drain.py.

During FILL, HOLD and DRAIN, the circulation pumps are governed by
AutoCirculationController (process/auto_circulation.py): they switch on once
the tank holds enough water (auto_circulation_start_liters, e.g. 30 L) and
switch off again once the level drops below auto_circulation_stop_liters
(e.g. 25 L) - the same logic already used by the regular water cycle, reused
here instead of duplicated.

Safety:
    - Gated behind hardware_execution_enabled + required_confirmation_text
      in config/tank_cleaning_settings.json, exactly like the Fill-and-Measure
      process.
    - Every phase loop waits on a threading.Event instead of a plain
      time.sleep(), and checks it every 0.5s. This means an Emergency Stop
      (or the dashboard Stop button) interrupts the cycle within about half
      a second, instead of only after the current phase finishes on its own
      like the older standalone process/*.py scripts do.
    - safe_shutdown of all actuators happens on every exit path: normal
      completion, cancellation and unexpected errors alike.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..auto_circulation import AutoCirculationController, load_auto_circulation_config
from ..background_process import BackgroundHardwareProcess
from ..common import mixer_liters_from_snapshot
from .drain_phase import run_drain_phase
from .fill_phase import run_fill_phase
from .hold_phase import run_hold_phase
from .phases import TankCleaningPhase
from .status_publisher import publish_status


class TankCleaningController(BackgroundHardwareProcess):
    """
    Built on BackgroundHardwareProcess (process/background_process.py), the
    same thread/lock/status base class ManualDrainJog uses, so it can be
    started/stopped safely from NiceGUI button clicks without blocking the
    web server.
    """

    label = "Tank Cleaning"
    # A full fill/hold/drain cycle can take several minutes, but every phase
    # loop reacts to the stop event within ~0.5s, so this join timeout only
    # needs to cover that normal-case teardown, not the whole cycle.
    stop_join_timeout_seconds = 5.0

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
    ) -> None:
        super().__init__(get_sensor_snapshot)

        self._auto_circulation = None

        self._phase = TankCleaningPhase.IDLE
        self._phase_started_at: float | None = None

        self._current_liters: float | None = None
        self._target_liters: float | None = None
        self._hold_seconds: float | None = None

    # ---- public dashboard API -----------------------------------------

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Tank Cleaning is already running.")

            block_message = self._check_start_preconditions_locked(settings)
            if block_message is not None:
                return self._result(False, block_message)

            self._current_liters = None
            self._target_liters = float(settings.get("target_fill_total_liters", 200.0))
            self._hold_seconds = float(settings.get("cleaning_hold_seconds", 300.0))
            self._last_message = "Tank Cleaning start requested."
            self._change_phase_locked(TankCleaningPhase.FILLING)
            self._begin_run_locked("cds-tank-cleaning", settings)

        return self._result(True, "Tank Cleaning started: fill -> hold -> drain.")

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            is_active = self._thread is not None and self._thread.is_alive()
            elapsed = 0.0
            if self._phase_started_at is not None:
                elapsed = max(0.0, time.monotonic() - self._phase_started_at)

            return {
                "is_active": is_active,
                "phase": self._phase,
                "phase_elapsed_seconds": round(elapsed, 1) if is_active else 0.0,
                "target_liters": self._target_liters,
                "hold_seconds": self._hold_seconds,
                "current_liters": self._current_liters,
                "started_at": self._started_at_iso if is_active else None,
                "stop_reason": self._stop_reason,
                "last_error": self._last_error,
                "last_message": self._last_message,
            }

    # ---- internal helpers ----------------------------------------------

    def _change_phase_locked(self, phase: str) -> None:
        self._phase = phase
        self._phase_started_at = time.monotonic()

    def _change_phase(self, phase: str) -> None:
        with self._lock:
            self._change_phase_locked(phase)

    def _fail(self, stop_reason: str, message: str) -> None:
        with self._lock:
            self._stop_reason = stop_reason
            self._last_error = message
            self._last_message = message
            self._phase = TankCleaningPhase.ERROR

    def _read_mixer_liters(self, settings: dict[str, Any], level_history) -> float | None:
        snapshot = self.get_sensor_snapshot()
        if snapshot is None:
            return None

        liters = mixer_liters_from_snapshot(snapshot, settings)
        if liters is None:
            return None

        level_history.append(liters)
        filtered = sum(level_history) / len(level_history)

        with self._lock:
            self._current_liters = filtered

        return filtered

    # ---- background thread ---------------------------------------------

    def _run(self, settings: dict[str, Any]) -> None:
        from gpio_config import ACTIVE_LOW, OUTPUTS
        from hardware.actuator_manager import ActuatorManager
        from services.mqtt_publisher import MqttPublisher

        actuators = None

        try:
            actuators = ActuatorManager(active_low=ACTIVE_LOW)
            self._actuators = actuators

            refill_pump = actuators.add(
                name="mixer_refill_pump", gpio_pin=OUTPUTS["mixer_refill_pump"]
            )
            mixing_circulation_pump = actuators.add(
                name="mixing_circulation_pump", gpio_pin=OUTPUTS["mixing_circulation_pump"]
            )
            sensor_circulation_pump = actuators.add(
                name="sensor_circulation_pump", gpio_pin=OUTPUTS["sensor_circulation_pump"]
            )
            transfer_pump = actuators.add(
                name="transfer_pump", gpio_pin=OUTPUTS["transfer_pump"]
            )
            drain_valve = actuators.add(
                name="drain_valve_0", gpio_pin=OUTPUTS["valve_0_drain"]
            )

            self._mqtt_publisher = MqttPublisher()

            self._auto_circulation = AutoCirculationController(
                actuators=actuators,
                config=load_auto_circulation_config(settings),
            )

            if not run_fill_phase(self, settings, actuators, refill_pump):
                return

            if not run_hold_phase(self, settings, actuators):
                return

            run_drain_phase(self, settings, actuators, transfer_pump, drain_valve)

        except Exception as exc:
            self._fail("error", f"Tank Cleaning failed: {exc}")

        finally:
            self._safe_shutdown_and_publish(actuators)

    def _safe_shutdown_and_publish(self, actuators) -> None:
        try:
            if self._auto_circulation is not None:
                self._auto_circulation.stop()
        except Exception:
            pass

        try:
            if actuators is not None:
                actuators.safe_shutdown_all()
        except Exception:
            pass

        publish_status(self, actuators, "TANK_CLEANING_STOPPED")

        try:
            if actuators is not None:
                actuators.close_all()
        except Exception:
            pass

        try:
            if self._mqtt_publisher is not None:
                self._mqtt_publisher.close()
        except Exception:
            pass

        with self._lock:
            if self._phase == TankCleaningPhase.ERROR:
                self._last_message = self._last_error or "Tank Cleaning failed."
            elif self._stop_reason not in (None, "completed"):
                self._phase = TankCleaningPhase.IDLE
                self._last_message = f"Tank Cleaning stopped: {self._stop_reason}"
            else:
                self._stop_reason = "completed"
                self._phase = TankCleaningPhase.FINISHED
                self._last_message = "Tank Cleaning completed."

            self._actuators = None
            self._mqtt_publisher = None
            self._auto_circulation = None
