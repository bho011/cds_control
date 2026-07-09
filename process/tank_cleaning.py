"""
Automated Mixing Tank cleaning cycle.

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

import threading
import time
from datetime import datetime
from typing import Any, Callable

from .auto_circulation import AutoCirculationController, load_auto_circulation_config
from .common import make_level_history, mixer_liters_from_snapshot, ro_liters_from_snapshot


class TankCleaningPhase:
    """Human-readable phase names, also used as the published process_state."""

    IDLE = "IDLE"
    FILLING = "FILLING"
    HOLDING = "HOLDING"
    DRAINING = "DRAINING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class TankCleaningController:
    """
    Server-side controller for the "Tank Cleaning" maintenance cycle.

    Mirrors the ManualDrainJog pattern already used in this dashboard:
    a background thread plus a lock-protected status dict, so it can be
    started/stopped safely from NiceGUI button clicks without blocking the
    web server.
    """

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.get_sensor_snapshot = get_sensor_snapshot

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._actuators = None
        self._mqtt_publisher = None
        self._auto_circulation = None

        self._phase = TankCleaningPhase.IDLE
        self._phase_started_at: float | None = None
        self._started_at_iso: str | None = None
        self._stop_reason: str | None = None
        self._last_error: str | None = None
        self._last_message = "Tank Cleaning ready."

        self._current_liters: float | None = None
        self._target_liters: float | None = None
        self._hold_seconds: float | None = None

    # ---- public dashboard API -----------------------------------------

    def is_active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Tank Cleaning is already running.")

            if not bool(settings.get("hardware_execution_enabled", False)):
                self._last_error = "hardware_execution_enabled is false."
                self._last_message = "Tank Cleaning locked by hardware safety setting."
                return self._result(False, self._last_message)

            snapshot = self.get_sensor_snapshot()
            if snapshot is None:
                self._last_error = "No current sensor snapshot available."
                self._last_message = "Tank Cleaning locked: no current sensor snapshot."
                return self._result(False, self._last_message)

            self._stop_event.clear()
            self._stop_reason = None
            self._last_error = None
            self._current_liters = None
            self._target_liters = float(settings.get("target_fill_total_liters", 200.0))
            self._hold_seconds = float(settings.get("cleaning_hold_seconds", 300.0))
            self._started_at_iso = datetime.now().isoformat(timespec="seconds")
            self._last_message = "Tank Cleaning start requested."
            self._change_phase_locked(TankCleaningPhase.FILLING)

            self._thread = threading.Thread(
                target=self._run,
                args=(settings,),
                daemon=False,
                name="cds-tank-cleaning",
            )
            self._thread.start()

        return self._result(True, "Tank Cleaning started: fill -> hold -> drain.")

    def stop(self, reason: str = "stopped_by_user") -> dict[str, Any]:
        thread_to_join: threading.Thread | None = None

        with self._lock:
            self._stop_reason = reason
            self._stop_event.set()
            if self._thread is not None and self._thread.is_alive():
                thread_to_join = self._thread

        if thread_to_join is not None and thread_to_join is not threading.current_thread():
            # A full fill/hold/drain cycle can take several minutes, but every
            # phase loop below reacts to the stop event within ~0.5s, so a
            # short join timeout here is enough to catch the normal case.
            thread_to_join.join(timeout=5.0)

        with self._lock:
            if self.is_active():
                return self._result(True, "Tank Cleaning stop requested; cleanup still running.")

        return self._result(True, "Tank Cleaning stopped.")

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

            if not self._run_fill(settings, actuators, refill_pump):
                return

            if not self._run_hold(settings, actuators):
                return

            self._run_drain(settings, actuators, transfer_pump, drain_valve)

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

        self._publish(actuators, "TANK_CLEANING_STOPPED")

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

    # ---- phase 1: fill ---------------------------------------------------

    def _run_fill(self, settings: dict[str, Any], actuators, refill_pump) -> bool:
        """Returns True if the target level was reached, False otherwise."""

        self._change_phase(TankCleaningPhase.FILLING)
        self._last_message = "Filling Mixing Tank with RO water."

        level_history = make_level_history(settings)
        target_total = float(settings.get("target_fill_total_liters", 200.0))
        max_mixer_liters = float(settings.get("max_mixer_liters", 200.0))
        max_fill_seconds = float(settings.get("max_fill_seconds", 900.0))
        no_progress_timeout = float(settings.get("no_fill_progress_timeout_seconds", 30.0))
        min_progress = float(settings.get("min_fill_progress_liters", 0.5))
        max_negative_drift = float(settings.get("max_negative_level_drift_liters", 3.0))
        required_confirm = int(settings.get("target_reached_confirm_samples", 3))
        min_ro_liters = float(settings.get("min_ro_liters_required", 20.0))

        snapshot = self.get_sensor_snapshot()
        start_liters = self._read_mixer_liters(settings, level_history) if snapshot else None

        if start_liters is None:
            self._fail("missing_mixer_level", "No Mixing Tank level available before fill.")
            return False

        ro_liters = ro_liters_from_snapshot(snapshot)
        if ro_liters is None:
            self._fail("missing_ro_level", "No RO Tank level available before fill.")
            return False

        if ro_liters < min_ro_liters:
            self._fail(
                "not_enough_ro_water",
                f"Not enough RO water: {ro_liters:.1f} L available, "
                f"{min_ro_liters:.1f} L required.",
            )
            return False

        if start_liters >= target_total:
            self._last_message = f"Mixing Tank already at or above target ({start_liters:.1f} L)."
            return True

        refill_pump.on()
        self._publish(actuators, TankCleaningPhase.FILLING)

        fill_start = time.monotonic()
        target_confirm_count = 0

        try:
            while True:
                if self._stop_event.wait(0.5):
                    return False

                elapsed = time.monotonic() - fill_start
                liters = self._read_mixer_liters(settings, level_history)
                self._auto_circulation.update(liters)

                if liters is None:
                    self._fail("missing_mixer_level_during_fill", "Mixer level lost during fill.")
                    return False

                added = liters - start_liters

                if liters > max_mixer_liters:
                    self._fail(
                        "mixer_over_max_limit",
                        f"Mixer level {liters:.2f} L exceeded max {max_mixer_liters:.2f} L.",
                    )
                    return False

                if added < -max_negative_drift:
                    self._fail(
                        "negative_level_drift",
                        f"Mixer level dropped implausibly during fill ({added:.2f} L).",
                    )
                    return False

                if elapsed >= no_progress_timeout and added < min_progress:
                    self._fail(
                        "no_fill_progress_timeout",
                        f"No plausible fill progress after {elapsed:.1f}s.",
                    )
                    return False

                if elapsed >= max_fill_seconds:
                    self._fail(
                        "max_fill_timeout",
                        f"Max fill time of {max_fill_seconds:.0f}s exceeded.",
                    )
                    return False

                target_confirm_count = target_confirm_count + 1 if liters >= target_total else 0

                self._last_message = (
                    f"Filling: {liters:.1f}/{target_total:.1f} L "
                    f"(elapsed {elapsed:.0f}s)"
                )
                self._publish(actuators, TankCleaningPhase.FILLING)

                if target_confirm_count >= required_confirm:
                    self._last_message = f"Fill complete: {liters:.1f} L."
                    return True
        finally:
            refill_pump.off()

    # ---- phase 2: hold / circulate ---------------------------------------

    def _run_hold(self, settings: dict[str, Any], actuators) -> bool:
        """Returns True if the hold time completed, False if cancelled."""

        self._change_phase(TankCleaningPhase.HOLDING)
        hold_seconds = float(settings.get("cleaning_hold_seconds", 300.0))
        self._hold_seconds = hold_seconds
        self._last_message = f"Circulating for {hold_seconds:.0f}s to flush the tank."

        level_history = make_level_history(settings)
        hold_start = time.monotonic()

        self._publish(actuators, TankCleaningPhase.HOLDING)

        while True:
            if self._stop_event.wait(0.5):
                return False

            elapsed = time.monotonic() - hold_start
            liters = self._read_mixer_liters(settings, level_history)
            self._auto_circulation.update(liters)

            self._last_message = (
                f"Circulating: {elapsed:.0f}/{hold_seconds:.0f}s"
            )
            self._publish(actuators, TankCleaningPhase.HOLDING)

            if elapsed >= hold_seconds:
                self._last_message = "Hold phase complete."
                return True

    # ---- phase 3: drain ----------------------------------------------------

    def _run_drain(self, settings: dict[str, Any], actuators, transfer_pump, drain_valve) -> None:
        self._change_phase(TankCleaningPhase.DRAINING)
        self._last_message = "Draining Mixing Tank."

        level_history = make_level_history(settings)
        start_liters = self._read_mixer_liters(settings, level_history)

        if start_liters is None:
            self._fail("missing_mixer_level_before_drain", "No Mixing Tank level available before drain.")
            return

        empty_threshold = float(settings.get("empty_threshold_liters", 0.3))
        empty_confirm_samples = int(settings.get("empty_confirm_samples", 5))

        if start_liters <= empty_threshold:
            self._last_message = "Tank already empty, nothing to drain."
            return

        pump_lpm = float(settings.get("transfer_pump_liters_per_minute", 16.0))
        buffer_seconds = float(settings.get("drain_timeout_buffer_seconds", 180.0))
        max_drain_seconds = (start_liters / pump_lpm) * 60.0 + buffer_seconds
        valve_settle_seconds = float(settings.get("valve_settle_seconds", 1.0))

        drain_valve.on()

        if self._stop_event.wait(valve_settle_seconds):
            return

        transfer_pump.on()
        self._publish(actuators, TankCleaningPhase.DRAINING)

        drain_start = time.monotonic()
        empty_confirm_count = 0

        try:
            while True:
                if self._stop_event.wait(0.5):
                    return

                elapsed = time.monotonic() - drain_start
                liters = self._read_mixer_liters(settings, level_history)
                self._auto_circulation.update(liters)

                if liters is not None:
                    empty_confirm_count = (
                        empty_confirm_count + 1 if liters <= empty_threshold else 0
                    )

                self._last_message = (
                    f"Draining: {liters if liters is not None else '-'} L "
                    f"(elapsed {elapsed:.0f}/{max_drain_seconds:.0f}s)"
                )
                self._publish(actuators, TankCleaningPhase.DRAINING)

                if empty_confirm_count >= empty_confirm_samples:
                    self._last_message = f"Tank emptied ({liters:.2f} L remaining)."
                    return

                if elapsed >= max_drain_seconds:
                    self._fail(
                        "calculated_drain_timeout",
                        "Drain timeout reached before empty confirmation.",
                    )
                    return
        finally:
            transfer_pump.off()
            drain_valve.off()

    # ---- MQTT status -------------------------------------------------------

    def _publish(self, actuators, process_state: str) -> None:
        if self._mqtt_publisher is None:
            return

        actuator_status: dict[str, Any] = {}
        if actuators is not None:
            try:
                actuator_status = actuators.status_payload()
            except Exception:
                actuator_status = {}

        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "python_nicegui",
            "process_state": process_state,
            "actuators": {
                "mixer_refill_pump": actuator_status.get("mixer_refill_pump", False),
                "transfer_pump": actuator_status.get("transfer_pump", False),
                "drain_valve_0": actuator_status.get("drain_valve_0", False),
                "mixing_circulation_pump": actuator_status.get("mixing_circulation_pump", False),
                "sensor_circulation_pump": actuator_status.get("sensor_circulation_pump", False),
            },
            "tank_cleaning": self.get_status(),
            "error": self._last_error,
        }

        try:
            self._mqtt_publisher.publish_json(payload)
        except Exception:
            pass

    @staticmethod
    def _result(success: bool, message: str) -> dict[str, Any]:
        return {"success": success, "message": message}
