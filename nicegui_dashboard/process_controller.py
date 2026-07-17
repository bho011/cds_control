import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from process.manual_drain_jog import ManualDrainJog
from process.tank_cleaning import TankCleaningController


SETTINGS_PATH = Path("config/process_settings.json")
TANK_CLEANING_SETTINGS_PATH = Path("config/tank_cleaning_settings.json")


class ProcessController:
    """
    Steuert den Fill-and-Measure-Prozess aus NiceGUI heraus.

    Sicherheitsprinzip:
    - Beim Laden des Dashboards werden keine GPIOs initialisiert.
    - GPIOs werden erst nach gültiger Sicherheitsprüfung erzeugt.
    - hardware_execution_enabled muss true sein.
    - required_confirmation_text muss exakt passen.
    - Ein Emergency Stop darf nicht durch einen parallelen Update-Tick
      wieder überschrieben werden.
    """

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.get_sensor_snapshot = get_sensor_snapshot

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._teardown_in_progress = False

        self.state_machine = None
        self.actuators = None
        self.mqtt_publisher = None
        self.process_logger = None

        self.is_running = False
        self.last_error: str | None = None
        self.last_message: str = "ProcessController initialized."
        self.last_start_request: str | None = None
        self.display_state: str | None = "IDLE"

        self.manual_drain_jog = ManualDrainJog(
            get_sensor_snapshot=self.get_sensor_snapshot,
        )
        self.tank_cleaning = TankCleaningController(
            get_sensor_snapshot=self.get_sensor_snapshot,
        )

    def load_settings(self) -> dict[str, Any]:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

    def load_tank_cleaning_settings(self) -> dict[str, Any]:
        with TANK_CLEANING_SETTINGS_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _thread_is_alive_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_status(self) -> dict[str, Any]:
        try:
            settings = self.load_settings()
        except Exception as exc:
            settings = {}
            settings_error = str(exc)
        else:
            settings_error = None

        try:
            tank_cleaning_settings = self.load_tank_cleaning_settings()
            tank_cleaning_settings_error = None
        except Exception as exc:
            tank_cleaning_settings = {}
            tank_cleaning_settings_error = str(exc)

        with self._lock:
            state_name = self.display_state
            error_message = self.last_error
            start_mixer_liters = None
            added_liters = None

            if self.state_machine is not None:
                state_name = self.state_machine.state.name
                error_message = self.state_machine.error_message or self.last_error
                start_mixer_liters = self.state_machine.start_mixer_liters
                added_liters = self.state_machine.last_added_liters

            return {
                "is_running": self.is_running,
                "thread_alive": self._thread_is_alive_locked(),
                "teardown_in_progress": self._teardown_in_progress,
                "state_name": state_name,
                "error": error_message,
                "last_message": self.last_message,
                "last_start_request": self.last_start_request,
                "settings_error": settings_error,

                "hardware_execution_enabled": settings.get(
                    "hardware_execution_enabled", False
                ),
                "required_confirmation_text": settings.get(
                    "required_confirmation_text", ""
                ),
                "fill_mode": settings.get("fill_mode"),
                "target_add_liters": settings.get("target_add_liters"),
                "target_total_liters": settings.get("target_total_liters"),
                "max_fill_seconds": settings.get("max_fill_seconds"),
                "enable_mixing_circulation": settings.get(
                    "enable_mixing_circulation", False
                ),
                "enable_sensor_circulation": settings.get(
                    "enable_sensor_circulation", False
                ),

                "start_mixer_liters": start_mixer_liters,
                "added_liters": added_liters,
                "manual_drain_jog": self.manual_drain_jog.get_status(),

                "tank_cleaning": self.tank_cleaning.get_status(),
                "tank_cleaning_hardware_execution_enabled": tank_cleaning_settings.get(
                    "hardware_execution_enabled", False
                ),
                "tank_cleaning_required_confirmation_text": tank_cleaning_settings.get(
                    "required_confirmation_text", ""
                ),
                "tank_cleaning_target_liters": tank_cleaning_settings.get(
                    "target_fill_total_liters"
                ),
                "tank_cleaning_hold_seconds": tank_cleaning_settings.get(
                    "cleaning_hold_seconds"
                ),
                "tank_cleaning_settings_error": tank_cleaning_settings_error,
            }

    def start_fill_and_measure(self, confirmation_text: str) -> dict[str, Any]:
        with self._lock:
            if (
                self.is_running
                or self._thread_is_alive_locked()
                or self._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Start blocked: process, cleanup, background thread, Manual Drain Jog, "
                    "or Tank Cleaning is still active.",
                )

            self.last_start_request = datetime.now().isoformat(timespec="seconds")
            self.last_error = None

        try:
            settings = self.load_settings()
        except Exception as exc:
            message = f"Settings konnten nicht geladen werden: {exc}"
            self._set_error(message)
            return self._result(False, message)

        hardware_enabled = settings.get("hardware_execution_enabled", False)

        if not hardware_enabled:
            message = (
                "Start blockiert: hardware_execution_enabled ist false. "
                "Es wurden keine GPIOs initialisiert."
            )
            self._set_error(message)
            return self._result(False, message)

        required_text = settings.get("required_confirmation_text", "confirmed")

        if confirmation_text.strip() != required_text:
            message = "Start blockiert: Bestätigungstext ist falsch."
            self._set_error(message)
            return self._result(False, message)

        snapshot = self.get_sensor_snapshot()

        if snapshot is None:
            message = "Start blockiert: Kein aktueller SensorSnapshot vorhanden."
            self._set_error(message)
            return self._result(False, message)

        with self._lock:
            if (
                self.is_running
                or self._thread_is_alive_locked()
                or self._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Start blocked: process, cleanup, background thread, Manual Drain Jog, "
                    "or Tank Cleaning is still active.",
                )

            self.is_running = True
            self._stop_requested = False
            self._teardown_in_progress = False
            self.last_error = None
            self.last_message = "Fill-and-Measure-Prozess wird gestartet."
            self.display_state = "START_REQUESTED"

            self._thread = threading.Thread(
                target=self._run_fill_and_measure,
                args=(settings,),
                daemon=False,
                name="cds-fill-and-measure",
            )
            self._thread.start()

        return self._result(True, "Fill-and-Measure-Prozess gestartet.")

    async def emergency_stop(self) -> dict[str, Any]:
        """
        Signals every hardware process to stop, then waits for them to
        actually exit. The signalling half runs under self._lock but never
        blocks; the waiting half can take several seconds (thread joins) and
        deliberately runs in a worker thread (asyncio.to_thread) without
        holding self._lock, so get_status()/other dashboard calls are never
        stalled for the duration of an emergency stop - see Phase 1 plan.
        """
        thread_to_join: threading.Thread | None = None

        with self._lock:
            self._stop_requested = True
            self.last_message = "Emergency Stop requested from NiceGUI."
            self.display_state = "EMERGENCY_STOP_REQUESTED"

            try:
                if self.state_machine is not None:
                    self.state_machine.error("Emergency stop requested from NiceGUI.")
            except Exception as exc:
                self.last_error = f"State machine emergency stop failed: {exc}"

            try:
                self.manual_drain_jog.request_stop(reason="emergency_stop")
            except Exception as exc:
                self.last_error = f"Manual Drain Jog emergency stop failed: {exc}"

            try:
                self.tank_cleaning.request_stop(reason="emergency_stop")
            except Exception as exc:
                self.last_error = f"Tank Cleaning emergency stop failed: {exc}"

            try:
                if self.actuators is not None:
                    self.actuators.safe_shutdown_all()
            except Exception as exc:
                self.last_error = f"Actuator emergency stop failed: {exc}"

            if self._thread_is_alive_locked():
                thread_to_join = self._thread

        def _wait_for_everything() -> None:
            if thread_to_join is not None and thread_to_join is not threading.current_thread():
                thread_to_join.join(timeout=2.0)

            try:
                self.manual_drain_jog.wait_stopped()
            except Exception as exc:
                self.last_error = f"Manual Drain Jog emergency stop failed: {exc}"

            try:
                self.tank_cleaning.wait_stopped()
            except Exception as exc:
                self.last_error = f"Tank Cleaning emergency stop failed: {exc}"

        await asyncio.to_thread(_wait_for_everything)

        with self._lock:
            if (
                self._thread_is_alive_locked()
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    True,
                    "Emergency Stop wurde ausgelöst. Cleanup läuft noch.",
                )

        return self._result(True, "Emergency Stop wurde ausgelöst und bestätigt.")

    def start_manual_drain_jog(self) -> dict[str, Any]:
        with self._lock:
            if (
                self.is_running
                or self._thread_is_alive_locked()
                or self._teardown_in_progress
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Manual Drain Jog blocked: main process, cleanup, or Tank Cleaning is active.",
                )

        try:
            settings = self.load_settings()
        except Exception as exc:
            message = f"Settings could not be loaded: {exc}"
            self._set_error(message)
            return self._result(False, message)

        # Re-check and start atomically under the same lock: the precondition
        # check above only fails fast, it does not prevent a concurrent
        # start_tank_cleaning() call from slipping in between it and
        # manual_drain_jog.start() actually flipping is_active(). This second
        # check-and-start is the one that actually has to be race-free.
        with self._lock:
            if (
                self.is_running
                or self._thread_is_alive_locked()
                or self._teardown_in_progress
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Manual Drain Jog blocked: main process, cleanup, or Tank Cleaning is active.",
                )

            result = self.manual_drain_jog.start(settings)

            self.last_message = result.get("message", "Manual Drain Jog start requested.")
            self.display_state = "MANUAL_DRAIN_JOG" if result.get("success") else self.display_state
            if not result.get("success"):
                self.last_error = result.get("message")

        return result

    def stop_manual_drain_jog(self) -> dict[str, Any]:
        result = self.manual_drain_jog.stop(reason="button_released")

        with self._lock:
            self.last_message = result.get("message", "Manual Drain Jog stop requested.")
            if not self.is_running and not self.manual_drain_jog.is_active():
                self.display_state = "IDLE"

        return result

    def shutdown(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """
        Für späteren NiceGUI/App-Shutdown:
        Stop anfordern, Aktoren abschalten, Thread kurz joinen.
        """
        thread_to_join: threading.Thread | None = None

        with self._lock:
            self._stop_requested = True
            self.last_message = "Controller shutdown requested."
            self.display_state = "SHUTDOWN_REQUESTED"

            try:
                self.manual_drain_jog.request_stop(reason="controller_shutdown")
            except Exception as exc:
                self.last_error = f"Manual Drain Jog shutdown failed: {exc}"

            try:
                self.tank_cleaning.request_stop(reason="controller_shutdown")
            except Exception as exc:
                self.last_error = f"Tank Cleaning shutdown failed: {exc}"

            try:
                if self.actuators is not None:
                    self.actuators.safe_shutdown_all()
            except Exception as exc:
                self.last_error = f"Actuator shutdown failed: {exc}"

            if self._thread_is_alive_locked():
                thread_to_join = self._thread

        if thread_to_join is not None and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=timeout_seconds)

        try:
            self.manual_drain_jog.wait_stopped()
        except Exception as exc:
            self.last_error = f"Manual Drain Jog shutdown failed: {exc}"

        try:
            self.tank_cleaning.wait_stopped()
        except Exception as exc:
            self.last_error = f"Tank Cleaning shutdown failed: {exc}"

        with self._lock:
            if (
                self._thread_is_alive_locked()
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return self._result(False, "Shutdown timeout: Hintergrundthread läuft noch.")

        return self._result(True, "Controller shutdown abgeschlossen.")

    def start_tank_cleaning(self, confirmation_text: str) -> dict[str, Any]:
        with self._lock:
            if (
                self.is_running
                or self._thread_is_alive_locked()
                or self._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Tank Cleaning blocked: another process, cleanup, or Manual Drain Jog is active.",
                )

        try:
            settings = self.load_tank_cleaning_settings()
        except Exception as exc:
            message = f"Tank Cleaning settings could not be loaded: {exc}"
            self._set_error(message)
            return self._result(False, message)

        required_text = settings.get("required_confirmation_text", "confirmed")

        if confirmation_text.strip() != required_text:
            message = "Tank Cleaning blocked: confirmation text is wrong."
            self._set_error(message)
            return self._result(False, message)

        # Re-check and start atomically under the same lock: the precondition
        # check above only fails fast, it does not prevent a concurrent
        # start_manual_drain_jog() call from slipping in between it and
        # tank_cleaning.start() actually flipping is_active(). This second
        # check-and-start is the one that actually has to be race-free.
        with self._lock:
            if (
                self.is_running
                or self._thread_is_alive_locked()
                or self._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Tank Cleaning blocked: another process, cleanup, or Manual Drain Jog is active.",
                )

            result = self.tank_cleaning.start(settings)

            self.last_message = result.get("message", "Tank Cleaning start requested.")
            self.display_state = "TANK_CLEANING" if result.get("success") else self.display_state
            if not result.get("success"):
                self.last_error = result.get("message")

        return result

    def stop_tank_cleaning(self) -> dict[str, Any]:
        result = self.tank_cleaning.stop(reason="stopped_by_user")

        with self._lock:
            self.last_message = result.get("message", "Tank Cleaning stop requested.")
            if (
                not self.is_running
                and not self.manual_drain_jog.is_active()
                and not self.tank_cleaning.is_active()
            ):
                self.display_state = "IDLE"

        return result

    def acknowledge_error(self) -> dict[str, Any]:
        with self._lock:
            thread_alive = self._thread_is_alive_locked()

            if (
                self.is_running
                or thread_alive
                or self._teardown_in_progress
                or self.tank_cleaning.is_active()
            ):
                return self._result(
                    False,
                    "Reset blockiert: Prozess, Hintergrundthread, Cleanup oder Tank Cleaning läuft noch."
                )

            self._stop_requested = False
            self._teardown_in_progress = False
            self._thread = None
            self.state_machine = None
            self.actuators = None
            self.mqtt_publisher = None
            self.process_logger = None
            self.is_running = False
            self.last_error = None
            self.display_state = "IDLE"
            self.last_message = "Reset acknowledged. Controller ready."

        return self._result(True, "Fehler wurde quittiert. Controller ist wieder bereit.")

    def _run_fill_and_measure(self, settings: dict[str, Any]) -> None:
        from gpio_config import ACTIVE_LOW, OUTPUTS
        from hardware.actuator_manager import ActuatorManager
        from services.mqtt_publisher import MqttPublisher
        from services.process_run_logger import ProcessRunLogger
        from statemachine.fill_and_measure_state_machine import (
            FillAndMeasureStateMachine,
        )

        try:
            actuators = ActuatorManager(active_low=ACTIVE_LOW)

            with self._lock:
                self.actuators = actuators

            mixer_refill_pump = actuators.add(
                name="mixer_refill_pump",
                gpio_pin=OUTPUTS["mixer_refill_pump"],
            )

            supply_valve_6 = actuators.add(
                name="supply_valve_6",
                gpio_pin=OUTPUTS["test_supply_valve_6"],
            )

            mixing_circulation_pump = None
            sensor_circulation_pump = None

            if settings.get("enable_mixing_circulation", False):
                mixing_circulation_pump = actuators.add(
                    name="mixing_circulation_pump",
                    gpio_pin=OUTPUTS["mixing_circulation_pump"],
                )

            if settings.get("enable_sensor_circulation", False):
                sensor_circulation_pump = actuators.add(
                    name="sensor_circulation_pump",
                    gpio_pin=OUTPUTS["sensor_circulation_pump"],
                )

            mqtt_publisher = MqttPublisher()
            process_logger = ProcessRunLogger(process_name="fill_and_measure")

            state_machine = FillAndMeasureStateMachine(
                mixer_refill_pump=mixer_refill_pump,
                ro_inlet_valve=supply_valve_6,
                mixing_circulation_pump=mixing_circulation_pump,
                sensor_circulation_pump=sensor_circulation_pump,
                get_sensor_snapshot=self.get_sensor_snapshot,
                settings=settings,
            )

            with self._lock:
                self.mqtt_publisher = mqtt_publisher
                self.process_logger = process_logger
                self.state_machine = state_machine
                self.last_message = "State Machine initialized."
                self.display_state = "STATE_MACHINE_INITIALIZED"

                if self._stop_requested:
                    state_machine.error("Stop requested before process start.")
                    return

                state_machine.start()
                self._publish_status()
                self._log_step()

            while True:
                with self._lock:
                    if self._stop_requested:
                        if state_machine.error_message is None:
                            state_machine.error("Stop requested from NiceGUI.")
                        else:
                            state_machine.safe_shutdown()
                        break

                    if state_machine.is_done:
                        break

                    state_machine.update()
                    self._publish_status()
                    self._log_step()

                time.sleep(0.5)

        except Exception as exc:
            self._set_error(f"Process failed: {exc}")

            with self._lock:
                try:
                    if self.state_machine is not None:
                        self.state_machine.safe_shutdown()
                except Exception:
                    pass

                try:
                    if self.actuators is not None:
                        self.actuators.safe_shutdown_all()
                except Exception:
                    pass

        finally:
            with self._lock:
                self._teardown_in_progress = True
                self.display_state = "TEARDOWN"
                self.last_message = "Fill-and-Measure cleanup läuft."

                try:
                    if self.state_machine is not None:
                        self.state_machine.safe_shutdown()
                except Exception:
                    pass

                try:
                    if self.actuators is not None:
                        self.actuators.safe_shutdown_all()
                except Exception:
                    pass

                try:
                    self._publish_status()
                except Exception:
                    pass

                try:
                    self._log_step()
                except Exception:
                    pass

                try:
                    if self.mqtt_publisher is not None:
                        self.mqtt_publisher.close()
                except Exception:
                    pass

                try:
                    if self.actuators is not None:
                        self.actuators.close_all()
                except Exception:
                    pass

                try:
                    if self.process_logger is not None:
                        self.process_logger.close()
                except Exception:
                    pass

                self.is_running = False
                self._stop_requested = False
                self._teardown_in_progress = False
                self.last_message = "Fill-and-Measure-Prozess beendet."

                if self.last_error is None and self.state_machine is not None:
                    if self.state_machine.error_message:
                        self.last_error = self.state_machine.error_message

    def _publish_status(self) -> None:
        if self.mqtt_publisher is None or self.state_machine is None:
            return

        actuator_status = {}

        if self.actuators is not None:
            actuator_status = self.actuators.status_payload()

        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "python_nicegui",
            "process_state": self.state_machine.state.name,
            "actuators": {
                "mixer_refill_pump": actuator_status.get("mixer_refill_pump", False),
                "supply_valve_6": actuator_status.get("supply_valve_6", False),
                "drain_valve_0": actuator_status.get("drain_valve_0", False),
                "transfer_pump": actuator_status.get("transfer_pump"),
                "mixing_circulation_pump": actuator_status.get(
                    "mixing_circulation_pump"
                ),
                "sensor_circulation_pump": actuator_status.get(
                    "sensor_circulation_pump"
                ),
            },
            "error": self.state_machine.error_message,
        }

        self.mqtt_publisher.publish_json(payload)

    def _log_step(self) -> None:
        if (
            self.process_logger is None
            or self.state_machine is None
            or self.actuators is None
        ):
            return

        snapshot = self.get_sensor_snapshot()
        actuator_status = self.actuators.status_payload()

        mixer_liters_filtered = None

        if snapshot is not None:
            try:
                mixer_liters_filtered = self.state_machine._filtered_mixer_liters(
                    snapshot
                )
            except Exception:
                mixer_liters_filtered = None

        self.process_logger.write_step(
            state=self.state_machine.state.name,
            error=self.state_machine.error_message,
            snapshot=snapshot,
            actuator_status=actuator_status,
            mixer_liters_filtered=mixer_liters_filtered,
            start_mixer_liters=self.state_machine.start_mixer_liters,
            added_liters=self.state_machine.last_added_liters,
        )

    def _set_error(self, message: str) -> None:
        with self._lock:
            self.last_error = message
            self.last_message = message

    @staticmethod
    def _result(success: bool, message: str) -> dict[str, Any]:
        return {
            "success": success,
            "message": message,
        }
