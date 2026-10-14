"""
Fill-and-Measure-Prozess: befüllt den Mixing Tank und misst EC/pH/Temperatur/DO.

Wie ManualDrainJog/TankCleaningController/PumpPrimeController auf
BackgroundHardwareProcess aufgebaut (process/background_process.py). Die
Fassade (nicegui_dashboard/process_controller.py::ProcessController) lädt
Settings, merged das aktive Rezept, prüft den Bestätigungstext und die
Exklusivität gegen die anderen drei Prozesse, BEVOR sie
self.fill_and_measure.start(settings) aufruft - dieser Controller prüft
hier nur noch die zwei generischen Gates aus BackgroundHardwareProcess
(hardware_execution_enabled, aktueller Sensor-Snapshot vorhanden), exakt
wie bei den drei Geschwister-Controllern.

is_running/teardown_in_progress bleiben als eigene, explizite Felder
erhalten (nicht auf die einfachere is_active()-Logik der Geschwister
reduziert) - das bildet weiterhin denselben dreistufigen Zustand ab
(laufend / Thread noch am Leben / Cleanup läuft), den get_status() schon
immer unterschieden hat.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from statemachine.start_validation import validate_fill_start

from .background_process import BackgroundHardwareProcess


class FillAndMeasureController(BackgroundHardwareProcess):
    label = "Fill-and-Measure"

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
        dosing_controller: Any = None,
        plan_builder: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        super().__init__(get_sensor_snapshot)

        # AP-05 Schritt 2: Nährstoffdosierung ist Teil des Mischlaufs. Der
        # Dosier-Controller schaltet kein GPIO (nur MCU_B ueber seriell); die
        # RO-Pumpe und die Zirkulation steuert ausschliesslich die State Machine.
        self._dosing_controller = dosing_controller
        self._plan_builder = plan_builder
        self._nutrient_plan = None

        self.is_running = False
        self._teardown_in_progress = False
        self.state_machine = None
        self.process_logger = None
        self.display_state: str | None = "IDLE"
        self.last_start_request: str | None = None

    def is_active(self) -> bool:
        """Auch aktiv, solange der zugehoerige Dosier-Thread noch laeuft (z. B.
        wenn er die Wartefrist im Teardown ueberschreitet): kein neuer
        Hardwareprozess, kein Reset, bis MCU_B nicht mehr dosieren kann."""
        if super().is_active():
            return True
        dosing = self._dosing_controller
        return dosing is not None and bool(dosing.is_active())

    # ---- public dashboard API -----------------------------------------

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            # Strikte Vorpruefung (AP-02B): BackgroundHardwareProcess wertet das
            # Flag per bool(...) aus (bool("false") ist True) und liest den
            # Sensor ungeschuetzt. Beides wird hier VOR dem Basis-Gate und
            # ohne Aenderung der Basisklasse abgefangen.
            if not isinstance(settings, dict):
                return self._reject_start_locked(
                    "Start blockiert: Prozesseinstellungen fehlen oder sind ungueltig."
                )
            if self._dosing_controller is not None and self._dosing_controller.is_active():
                return self._reject_start_locked(
                    "Start blockiert: der Dosier-Thread des vorherigen Laufs ist noch aktiv."
                )
            if settings.get("hardware_execution_enabled") is not True:
                return self._reject_start_locked(
                    "Start blockiert: hardware_execution_enabled ist nicht strikt true."
                )

            try:
                block_message = self._check_start_preconditions_locked(settings)
            except Exception as exc:
                return self._reject_start_locked(
                    f"Start blockiert: Sicherheitspruefung fehlgeschlagen ({type(exc).__name__}: {exc})."
                )
            if block_message is not None:
                return self._result(False, block_message)

            # Verbindliche Startfreigabe: vor is_running, Thread und
            # GPIO-Initialisierung. Ein Fehler beim Sensorlesen blockiert ebenfalls.
            try:
                sensor_snapshot = self.get_sensor_snapshot()
            except Exception as exc:
                return self._reject_start_locked(
                    f"Start blockiert: Sensor-Snapshot nicht lesbar ({type(exc).__name__}: {exc})."
                )

            allowed, validation_message = validate_fill_start(settings, sensor_snapshot)
            if not allowed:
                return self._reject_start_locked(validation_message)

            plan_message = self._prepare_nutrient_plan_locked(settings)
            if plan_message is not None:
                return self._reject_start_locked(plan_message)

            self.last_start_request = datetime.now().isoformat(timespec="seconds")
            self.is_running = True
            self._teardown_in_progress = False
            self._last_error = None
            self.display_state = "START_REQUESTED"
            self._last_message = "Fill-and-Measure-Prozess wird gestartet."

            self._begin_run_locked("cds-fill-and-measure", settings)

        return self._result(True, "Fill-and-Measure-Prozess gestartet.")

    def _prepare_nutrient_plan_locked(self, settings: dict[str, Any]) -> str | None:
        """Plan und Dosiervoraussetzungen VOR Thread/GPIO pruefen. None = ok
        (auch ohne Dosierung). Jeder Fehler blockiert den ganzen Lauf."""
        self._nutrient_plan = None
        snapshot = settings.get("recipe_run_config_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("nutrient_dosing_enabled") is not True:
            return None
        try:
            if self._dosing_controller is None:
                from process.nutrient_dosing import NutrientDosingController

                self._dosing_controller = NutrientDosingController(self.get_sensor_snapshot)
            builder = self._plan_builder
            if builder is None:
                from process.nutrient_dosing import build_run_plan as builder
            plan = builder(settings)
        except Exception as exc:
            return f"Start blockiert: Dosierplan nicht erstellbar ({type(exc).__name__}: {exc})."
        if not plan.has_dosing_work:
            return None
        if settings.get("enable_mixing_circulation") is not True or settings.get("enable_sensor_circulation") is not True:
            return "Start blockiert: Mit Naehrstoffdosierung muessen Mixing- und Sensorzirkulation aktiviert sein."
        if self._dosing_controller.is_active():
            return "Start blockiert: Naehrstoffdosierung laeuft noch."
        reason = self._dosing_controller.check_plan(plan)
        if reason is not None:
            return f"Start blockiert: {reason}"
        self._nutrient_plan = plan
        return None

    def _reject_start_locked(self, message: str) -> dict[str, Any]:
        """Abgelehnter Start: nur Fehler-/Meldungstext, kein Laufstatus. Lock gehalten."""
        self._last_error = message
        self._last_message = message
        return self._result(False, message)

    def acknowledge_error(self) -> dict[str, Any]:
        """
        Reset nach einem fehlgeschlagenen/abgebrochenen Lauf - nur zulässig,
        wenn der Hintergrund-Thread tatsächlich fertig ist. Die Exklusivitäts-
        Prüfung gegen die anderen drei Prozesse bleibt Sache der Fassade
        (ProcessController.acknowledge_error()), hier nur der Reset des
        eigenen Zustands.
        """
        with self._lock:
            if self.is_active() or self._teardown_in_progress:
                return self._result(
                    False,
                    "Reset blockiert: Fill-and-Measure-Hintergrundthread oder Cleanup läuft noch.",
                )

            self.is_running = False
            self._teardown_in_progress = False
            self._thread = None
            self.state_machine = None
            self._actuators = None
            self._mqtt_publisher = None
            self.process_logger = None
            self._nutrient_plan = None  # Dosieranzeige des beendeten Laufs verschwindet mit dem Reset
            self._last_error = None
            self.display_state = "IDLE"
            self._last_message = "Reset acknowledged. Controller ready."

        return self._result(True, "Fehler wurde quittiert. Controller ist wieder bereit.")

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            state_name = self.display_state
            error_message = self._last_error
            start_mixer_liters = None
            added_liters = None

            if self.state_machine is not None:
                state_name = self.state_machine.state.name
                error_message = self.state_machine.error_message or self._last_error
                start_mixer_liters = self.state_machine.start_mixer_liters
                added_liters = self.state_machine.last_added_liters

            dosing_status = None
            if self._nutrient_plan is not None and self._dosing_controller is not None:
                # planned_ul: Gesamtmenge des Plans, auch bevor die Dosierung gestartet ist.
                dosing_status = dict(self._dosing_controller.get_status(), planned_ul=self._nutrient_plan.total_ul)

            return {
                "dosing": dosing_status,
                "is_running": self.is_running,
                "thread_alive": self.is_active(),
                "teardown_in_progress": self._teardown_in_progress,
                "state_name": state_name,
                "error": error_message,
                "last_message": self._last_message,
                "last_start_request": self.last_start_request,
                "start_mixer_liters": start_mixer_liters,
                "added_liters": added_liters,
            }

    # ---- background thread ---------------------------------------------

    def _run(self, settings: dict[str, Any]) -> None:
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
                self._actuators = actuators

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
                recipe_run=True,
                nutrient_plan=self._nutrient_plan,
                dosing=self._dosing_controller if self._nutrient_plan is not None else None,
            )

            with self._lock:
                self._mqtt_publisher = mqtt_publisher
                self.process_logger = process_logger
                self.state_machine = state_machine
                self._last_message = "State Machine initialized."
                self.display_state = "STATE_MACHINE_INITIALIZED"

                if self._stop_event.is_set():
                    state_machine.error("Stop requested before process start.")
                    return

                state_machine.start()
                self._publish_status()
                self._log_step()

            while True:
                with self._lock:
                    if self._stop_event.is_set():
                        if state_machine.error_message is None:
                            state_machine.error(f"Stop requested: {self._stop_reason or 'stopped_by_user'}.")
                        else:
                            state_machine.safe_shutdown()
                        break

                    if state_machine.is_done:
                        break

                    state_machine.update()
                    self._publish_status()
                    self._log_step()

                # Deliberately kept as the original plain sleep (not
                # self._stop_event.wait(0.5)): this preserves the exact
                # pre-split polling cadence instead of adopting the three
                # siblings' interruptible-wait timing as a side effect of
                # the move onto BackgroundHardwareProcess - a real behavior
                # change (faster stop reaction), not authorized for this
                # phase (see Modularisierungs-Plan Phase 7, "keine
                # Verhaltensänderung").
                time.sleep(0.5)

        except Exception as exc:
            self._last_error = f"Process failed: {exc}"

            with self._lock:
                try:
                    if self.state_machine is not None:
                        self.state_machine.safe_shutdown()
                except Exception:
                    pass

                try:
                    if self._actuators is not None:
                        self._actuators.safe_shutdown_all()
                except Exception:
                    pass

        finally:
            self._shutdown_and_wait_for_dosing()
            with self._lock:
                self._teardown_in_progress = True
                self.display_state = "TEARDOWN"
                self._last_message = "Fill-and-Measure cleanup läuft."

                try:
                    if self.state_machine is not None:
                        self.state_machine.safe_shutdown()
                except Exception:
                    pass

                try:
                    if self._actuators is not None:
                        self._actuators.safe_shutdown_all()
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
                    if self._mqtt_publisher is not None:
                        self._mqtt_publisher.close()
                except Exception:
                    pass

                try:
                    if self._actuators is not None:
                        self._actuators.close_all()
                except Exception:
                    pass

                try:
                    if self.process_logger is not None:
                        self.process_logger.close()
                except Exception:
                    pass

                self.is_running = False
                self._teardown_in_progress = False
                self._last_message = "Fill-and-Measure-Prozess beendet."

                if self._last_error is None and self.state_machine is not None:
                    if self.state_machine.error_message:
                        self._last_error = self.state_machine.error_message

    def _shutdown_and_wait_for_dosing(self) -> None:
        """Vor dem Cleanup: Aktoren aus (setzt auch den Dosier-Stopp ab) und auf das
        Ende des Dosier-Threads warten, damit kein Prozess als beendet gilt, solange
        MCU_B noch dosieren koennte. Ohne Lock (Warten bis zu stop_join_timeout)."""
        state_machine = self.state_machine
        if state_machine is not None:
            try:
                state_machine.safe_shutdown()
            except Exception:
                pass
        dosing = self._dosing_controller
        if dosing is None or self._nutrient_plan is None:
            return
        try:
            if dosing.is_active():
                dosing.request_stop("process_end")
                dosing.wait_stopped()
                if dosing.is_active():
                    self._last_error = self._last_error or "Dosier-Thread beendet sich nicht rechtzeitig."
        except Exception as exc:
            self._last_error = self._last_error or f"Dosierstopp fehlgeschlagen: {type(exc).__name__}: {exc}"

    def _publish_status(self) -> None:
        if self._mqtt_publisher is None or self.state_machine is None:
            return

        actuator_status = {}

        if self._actuators is not None:
            actuator_status = self._actuators.status_payload()

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

        self._mqtt_publisher.publish_json(payload)

    def _log_step(self) -> None:
        if (
            self.process_logger is None
            or self.state_machine is None
            or self._actuators is None
        ):
            return

        snapshot = self.get_sensor_snapshot()
        actuator_status = self._actuators.status_payload()

        # Cached value of the state machine: logging must never feed the
        # moving average a second time (AP-02C).
        mixer_liters_filtered = self.state_machine.last_filtered_mixer_liters

        self.process_logger.write_step(
            state=self.state_machine.state.name,
            error=self.state_machine.error_message,
            snapshot=snapshot,
            actuator_status=actuator_status,
            mixer_liters_filtered=mixer_liters_filtered,
            start_mixer_liters=self.state_machine.start_mixer_liters,
            added_liters=self.state_machine.last_added_liters,
        )
