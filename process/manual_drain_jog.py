from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable


class ManualDrainJog:
    """
    Server-side dead-man / jog control for manual maintenance drain.

    The dashboard may start this action while the user presses and holds the
    button. This backend class enforces the hard maximum runtime even if the
    browser, websocket, or user input gets stuck.
    """

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
        max_seconds: float = 30.0,
        valve_settle_seconds: float = 0.5,
    ) -> None:
        self.get_sensor_snapshot = get_sensor_snapshot
        self.max_seconds = float(max_seconds)
        self.valve_settle_seconds = float(valve_settle_seconds)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._actuators = None
        self._mqtt_publisher = None
        self._started_at_monotonic: float | None = None
        self._started_at_iso: str | None = None
        self._stop_reason: str | None = None
        self._last_error: str | None = None
        self._last_message = "Manual Drain Jog ready."

    def is_active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Manual Drain Jog is already running.")

            if not bool(settings.get("hardware_execution_enabled", False)):
                self._last_error = "hardware_execution_enabled is false."
                self._last_message = "Manual Drain Jog locked by hardware safety setting."
                return self._result(False, self._last_message)

            snapshot = self.get_sensor_snapshot()
            if snapshot is None:
                self._last_error = "No current sensor snapshot available."
                self._last_message = "Manual Drain Jog locked: no current sensor snapshot."
                return self._result(False, self._last_message)

            self.max_seconds = float(settings.get("manual_drain_jog_max_seconds", 30.0))
            self.valve_settle_seconds = float(settings.get("manual_drain_jog_valve_settle_seconds", 0.5))

            if self.max_seconds <= 0:
                self.max_seconds = 30.0

            self._stop_event.clear()
            self._stop_reason = None
            self._last_error = None
            self._last_message = "Manual Drain Jog start requested."
            self._started_at_monotonic = time.monotonic()
            self._started_at_iso = datetime.now().isoformat(timespec="seconds")

            self._thread = threading.Thread(
                target=self._run,
                daemon=False,
                name="cds-manual-drain-jog",
            )
            self._thread.start()

        return self._result(True, "Manual Drain Jog started. Keep button pressed.")

    def stop(self, reason: str = "button_released") -> dict[str, Any]:
        thread_to_join: threading.Thread | None = None

        with self._lock:
            self._stop_reason = reason
            self._stop_event.set()
            if self._thread is not None and self._thread.is_alive():
                thread_to_join = self._thread

        if thread_to_join is not None and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=2.0)

        with self._lock:
            if self.is_active():
                return self._result(True, "Manual Drain Jog stop requested; cleanup still running.")

        return self._result(True, "Manual Drain Jog stopped.")

    def shutdown(self) -> None:
        self.stop(reason="shutdown")

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

    def _run(self) -> None:
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
            self._publish("MANUAL_DRAIN_JOG_VALVE_OPEN")

            if self._stop_event.wait(self.valve_settle_seconds):
                self._stop_reason = self._stop_reason or "button_released_before_pump_start"
                return

            transfer_pump.on()
            with self._lock:
                self._last_message = "Manual Drain Jog running."
            self._publish("MANUAL_DRAIN_JOG_RUNNING")

            started = time.monotonic()
            while not self._stop_event.is_set():
                elapsed = time.monotonic() - started
                if elapsed >= self.max_seconds:
                    with self._lock:
                        self._stop_reason = "max_runtime_reached"
                        self._last_message = "Manual Drain Jog stopped by 30 s watchdog."
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

            self._publish("MANUAL_DRAIN_JOG_STOPPED")

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
                if self._last_error is None and self._last_message == "Manual Drain Jog running.":
                    self._last_message = "Manual Drain Jog stopped."
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

    @staticmethod
    def _result(success: bool, message: str) -> dict[str, Any]:
        return {"success": success, "message": message}
