from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from statemachine.sensor_reading import finite_number

from .background_process import BackgroundHardwareProcess
from .drain_safety import safe_stop_drain, shutdown_errors

# Binding hard maximum runtime of the dead-man jog. Configured values above it
# (or non-finite, non-positive, non-numeric ones) block the start.
MANUAL_DRAIN_JOG_MAX_SECONDS = 30.0
DEFAULT_VALVE_SETTLE_SECONDS = 0.5


def parse_jog_timing(settings: dict[str, Any]) -> tuple[float, float] | str:
    """
    (max_seconds, valve_settle_seconds) from the settings, or an error string.

    Absent keys use the documented defaults; keys that are present but unusable
    (NaN/Infinity, bool, string, None, <= 0, > 30 s, negative settle time) are
    rejected - never silently replaced by a default.
    """
    max_seconds = MANUAL_DRAIN_JOG_MAX_SECONDS
    if "manual_drain_jog_max_seconds" in settings:
        raw = settings["manual_drain_jog_max_seconds"]
        max_seconds = finite_number(raw)
        if max_seconds is None or max_seconds <= 0 or max_seconds > MANUAL_DRAIN_JOG_MAX_SECONDS:
            return (
                "manual_drain_jog_max_seconds must be a finite number > 0 and <= "
                f"{MANUAL_DRAIN_JOG_MAX_SECONDS:.0f}, got {raw!r}."
            )

    settle = DEFAULT_VALVE_SETTLE_SECONDS
    if "manual_drain_jog_valve_settle_seconds" in settings:
        raw = settings["manual_drain_jog_valve_settle_seconds"]
        settle = finite_number(raw)
        if settle is None or settle < 0:
            return f"manual_drain_jog_valve_settle_seconds must be a finite number >= 0, got {raw!r}."

    return max_seconds, settle


class ManualDrainJog(BackgroundHardwareProcess):
    """
    Server-side dead-man / jog control for manual maintenance drain.

    The dashboard may start this action while the user presses and holds the
    button. This backend class enforces the hard maximum runtime even if the
    browser, websocket, or user input gets stuck.
    """

    label = "Manual Drain Jog"
    stop_label = "Manual Drain"
    stop_join_timeout_seconds = 2.0

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
        max_seconds: float = 30.0,
        valve_settle_seconds: float = 0.5,
    ) -> None:
        super().__init__(get_sensor_snapshot)

        self.max_seconds = float(max_seconds)
        self.valve_settle_seconds = float(valve_settle_seconds)
        self._started_at_monotonic: float | None = None
        self._cleanup_errors: list[str] = []

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Manual Drain Jog is already running.")

            block_message = self._check_start_preconditions_locked(settings)
            if block_message is not None:
                return self._result(False, block_message)

            timing = parse_jog_timing(settings)
            if isinstance(timing, str):
                self._last_error = timing
                self._last_message = f"Manual Drain Jog blocked: {timing}"
                return self._result(False, self._last_message)

            self.max_seconds, self.valve_settle_seconds = timing
            self._cleanup_errors = []

            self._last_message = "Manual Drain Jog start requested."
            self._started_at_monotonic = time.monotonic()
            self._begin_run_locked("cds-manual-drain-jog", settings)

        return self._result(True, "Manual Drain started. Keep button pressed.")

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            is_active = self._thread is not None and self._thread.is_alive()
            elapsed = 0.0
            if self._started_at_monotonic is not None:
                elapsed = max(0.0, time.monotonic() - self._started_at_monotonic)

            progress = 0.0
            if self.max_seconds > 0:
                progress = min(1.0, elapsed / self.max_seconds)

            return {
                "is_active": is_active,
                "elapsed_seconds": round(elapsed, 1) if is_active else 0.0,
                "max_seconds": self.max_seconds,
                "progress": progress if is_active else 0.0,
                "started_at": self._started_at_iso if is_active else None,
                "stop_reason": self._stop_reason,
                "last_error": self._last_error,
                "last_message": self._last_message,
            }

    def _run(self, settings: dict[str, Any]) -> None:
        from gpio_config import ACTIVE_LOW, OUTPUTS
        from hardware.actuator_manager import ActuatorManager
        from services.mqtt_publisher import MqttPublisher

        drain_valve = None
        transfer_pump = None

        try:
            with self._lock:
                self._last_message = "Manual Drain Jog initializing actuators."

            self._actuators = ActuatorManager(active_low=ACTIVE_LOW)
            drain_valve = self._actuators.add(
                name="drain_valve_0",
                gpio_pin=OUTPUTS["valve_0_drain"],
            )
            transfer_pump = self._actuators.add(
                name="transfer_pump",
                gpio_pin=OUTPUTS["transfer_pump"],
            )
            self._mqtt_publisher = MqttPublisher()

            drain_valve.on()
            self._publish("Manual Drain: Valve Open")

            if self._stop_event.wait(self.valve_settle_seconds):
                self._stop_reason = self._stop_reason or "button_released_before_pump_start"
                return

            # Re-check immediately before the pump: a stop request must never be
            # followed by a pump start.
            if self._stop_event.is_set():
                self._stop_reason = self._stop_reason or "button_released_before_pump_start"
                return

            transfer_pump.on()
            with self._lock:
                self._last_message = "Manual Drain running."
            self._publish("Manual Drain: Running")

            started = time.monotonic()
            while not self._stop_event.is_set():
                elapsed = time.monotonic() - started
                if elapsed >= self.max_seconds:
                    with self._lock:
                        self._stop_reason = "max_runtime_reached"
                        self._last_message = f"Manual Drain stopped by {self.max_seconds:g} s watchdog."
                    break

                time.sleep(0.1)

        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._last_message = f"Manual Drain Jog failed: {exc}"
                self._stop_reason = "error"

        finally:
            shutdown_problems: list[str] = []

            # Stop: transfer pump OFF, then drain valve OFF (both always attempted).
            # O1: behaviour after a failing pump switch-off is intentionally unchanged.
            shutdown_problems.extend(safe_stop_drain(transfer_pump, drain_valve).errors)
            if shutdown_problems:
                self._record_cleanup_failure(shutdown_problems)  # before the status goes out

            self._publish("Manual Drain: Stopped")

            try:
                if self._actuators is not None:
                    shutdown_problems.extend(shutdown_errors(self._actuators.safe_shutdown_all()))
            except Exception as exc:
                shutdown_problems.append(f"safe_shutdown_all failed: {type(exc).__name__}: {exc}")

            try:
                if self._actuators is not None:
                    shutdown_problems.extend(shutdown_errors(self._actuators.close_all()))
            except Exception as exc:
                shutdown_problems.append(f"close_all failed: {type(exc).__name__}: {exc}")

            # Errors first known after the last publish must not stay invisible:
            # record them and send the final status again.
            if shutdown_problems:
                already_published = len(self._cleanup_errors)
                self._record_cleanup_failure(shutdown_problems)
                if len(self._cleanup_errors) != already_published:
                    self._publish("Manual Drain: Stopped")

            try:
                if self._mqtt_publisher is not None:
                    self._mqtt_publisher.close()
            except Exception:
                pass

            with self._lock:
                if self._stop_reason is None:
                    self._stop_reason = "button_released"
                if not shutdown_problems and self._last_error is None and self._last_message == "Manual Drain running.":
                    self._last_message = "Manual Drain stopped."
                self._actuators = None
                self._mqtt_publisher = None

    def _record_cleanup_failure(self, errors: list[str]) -> None:
        """Record actuator switch-off errors (accumulated); never reported as a successful stop."""
        with self._lock:
            for error in errors:
                if error not in self._cleanup_errors:
                    self._cleanup_errors.append(error)
            self._stop_reason = "cleanup_failed"
            self._last_error = (
                f"Actuator switch-off failed: {'; '.join(self._cleanup_errors)}. "
                "Verify transfer pump and drain valve physically."
            )
            self._last_message = self._last_error

    def _publish(self, process_state: str) -> None:
        if self._mqtt_publisher is None:
            return

        actuator_status = {}
        if self._actuators is not None:
            try:
                actuator_status = self._actuators.status_payload()
            except Exception:
                actuator_status = {}

        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "python_nicegui",
            "process_state": process_state,
            "actuators": {
                "mixer_refill_pump": actuator_status.get("mixer_refill_pump"),
                "supply_valve_6": actuator_status.get("supply_valve_6"),
                "drain_valve_0": actuator_status.get("drain_valve_0", False),
                "transfer_pump": actuator_status.get("transfer_pump", False),
                "mixing_circulation_pump": actuator_status.get("mixing_circulation_pump"),
                "sensor_circulation_pump": actuator_status.get("sensor_circulation_pump"),
            },
            "manual_drain_jog": self.get_status(),
            "error": self._last_error,
        }

        try:
            self._mqtt_publisher.publish_json(payload)
        except Exception:
            pass
