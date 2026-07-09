from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from .background_process import BackgroundHardwareProcess


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

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Manual Drain Jog is already running.")

            block_message = self._check_start_preconditions_locked(settings)
            if block_message is not None:
                return self._result(False, block_message)

            self.max_seconds = float(settings.get("manual_drain_jog_max_seconds", 30.0))
            self.valve_settle_seconds = float(settings.get("manual_drain_jog_valve_settle_seconds", 0.5))

            if self.max_seconds <= 0:
                self.max_seconds = 30.0

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
                        self._last_message = "Manual Drain stopped by 30 s watchdog."
                    break

                time.sleep(0.1)

        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._last_message = f"Manual Drain Jog failed: {exc}"
                self._stop_reason = "error"

        finally:
            try:
                if transfer_pump is not None:
                    transfer_pump.off()
            except Exception:
                pass

            try:
                if drain_valve is not None:
                    drain_valve.off()
            except Exception:
                pass

            self._publish("Manual Drain: Stopped")

            try:
                if self._actuators is not None:
                    self._actuators.safe_shutdown_all()
            except Exception:
                pass

            try:
                if self._actuators is not None:
                    self._actuators.close_all()
            except Exception:
                pass

            try:
                if self._mqtt_publisher is not None:
                    self._mqtt_publisher.close()
            except Exception:
                pass

            with self._lock:
                if self._stop_reason is None:
                    self._stop_reason = "button_released"
                if self._last_error is None and self._last_message == "Manual Drain running.":
                    self._last_message = "Manual Drain stopped."
                self._actuators = None
                self._mqtt_publisher = None

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
