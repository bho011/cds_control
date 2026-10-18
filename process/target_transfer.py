"""
AP-08: kontrollierter Transfer des Mixing-Tank-Inhalts in genau einen
Solution Tank (1 bis 4).

Ablauf (ein Hintergrundthread, gleiche Basisklasse wie Tank Cleaning und
Manual Drain Jog):

    Alle dokumentierten Ventile (Valve 0 bis 6) schliessen -> Stop pruefen ->
    Zielventil auf -> Settle-Zeit -> Stop erneut pruefen -> Sensor erneut
    pruefen -> Transferpumpe an -> Ueberwachung -> Pumpe aus -> Ventil zu.

Verbindliche Zuordnung (bestaetigte Anlagenzuordnung, Python-GPIO-Mapping,
Node-RED wird ignoriert):

    Tank 1 -> Valve 1 -> GPIO27      Tank 3 -> Valve 3 -> GPIO24
    Tank 2 -> Valve 2 -> GPIO19      Tank 4 -> Valve 4 -> GPIO25
    Transferpumpe -> GPIO26
    Drain (Valve 0, GPIO21) und Valve 6 sind NIE Zieltankventile.

Vorab-Schliessen: Bevor das Zielventil oeffnet und die Pumpe foerdert, werden
ALLE dokumentierten Ventile (Drain Valve 0, Valve 1 bis 5, Supply Valve 6;
PRE_TRANSFER_CLOSE_VALVES) geschlossen. Jedes wird als Ausgang im inaktiven
Zustand (Relais aus) angelegt und der gemeldete Zustand geprueft. Gelingt das bei
einem Ventil nicht oder ist es nicht als inaktiv bestaetigt, bricht der Transfer
fail-closed ab, bevor Zielventil oder Pumpe geschaltet wurden. Valve 7 bis 9 sind
nicht dokumentiert und werden nicht beruehrt. Bekannte, vom Anlagenverantwortlichen
bewusst akzeptierte Eigenschaft des Hardware-Layers (Standardweg): gpiozero legt
einen neuen Ausgang ueber lgpio zuerst kurz auf LOW und dann auf den inaktiven
Pegel; bei active-low-Relaiskarten ist das ein sehr kurzer Impuls pro Ausgang
(Dauer nicht gemessen). Bisher betraf das nur Pumpe und Zielventil, jetzt alle
sieben Ventile.

Was diese Funktion NICHT ist:

* Keine Ueberlaufsicherung. Die Zieltanks haben keine Sensoren. Die Software
  prueft nur die vom Bediener abgelesene Fuellmenge gegen das Nennvolumen
  (300 L) und die beim Start erfasste Mixer-Menge. Die Sicherheitsreserve und
  die Messungenauigkeit des Mixer-Sensors liegen vollstaendig in der
  ausdruecklichen Bedienerbestaetigung; die Software legt keinen Reservewert
  fest.
* Kein Qualitaetsnachweis. Bei NOT_EVALUATED gilt die EC-/pH-Sollwerterfuellung
  als nicht bestaetigt; der Bediener muss das gesondert zur Kenntnis nehmen.
* Keine physische Rueckmeldung: "Pumpe aus"/"Ventil zu" bedeutet, dass der
  Schaltaufruf ohne Fehler zurueckkam; beim Vorab-Schliessen, dass der Ausgang
  inaktiv angelegt und als inaktiv gemeldet wurde. Ob ein Ventil physisch zu
  ist, weiss die Software nicht.

Sicherheitsueberwachung der Quellseite: dieselben Bausteine wie beim Drain
(process/drain_safety.py): Sensorverlust, Plausibilitaet, Fortschrittsfenster,
berechnetes Maximal-Timeout.

Transferende (bestaetigtes Sensorverhalten): beim Entleeren bedeutet die ERSTE
angezeigte 0 L, dass der Mixing Tank tatsaechlich leer ist; danach duerfen die
Werte wieder schwanken. Deshalb gilt ein einzelner frischer, gueltiger 0-L-Wert
als reguelaeres Ende (keine Mehrfachbestaetigung), sofern er zum bisherigen
Entleerverlauf passt: der letzte plausible Messwert davor muss schon im unteren
Sensorbereich (<= MIXER_SENSOR_RELIABLE_MIN_LITERS) gelegen haben. Eine Null mitten
im hohen Pegel wird nicht als Ende gewertet und auch nicht als Verlauf
fortgeschrieben. Bei der Null wird sofort die Pumpe gestoppt, danach das Ventil
geschlossen.

Mixer-Sensor unter ca. 10 L: der Fuellstandssensor ist dort nicht belastbar
(Kalibrierung nur ab 10 L, siehe statemachine/start_validation.py). Deshalb
blockiert ein Start bei einer Mixer-Menge unter 10 L (die zu transferierende
Menge waere nicht verlaesslich bestimmt).

Wiederholung: Ein abgebrochener oder fehlgeschlagener Transfer verbraucht die
Freigabe des Mischlaufs NICHT; nur ein erfolgreich abgeschlossener Transfer tut
das. Jeder neue Start braucht die volle Bedienerfreigabe mit neu abgelesenem
Zieltank-Fuellstand und neu erfasster Mixer-Menge. Nach einer Stoerung (Fehler,
Emergency Stop) ist erst nach Quittierung (Reset / Ack) ein neuer Start moeglich.

Pumpenabschaltung: Die Transferpumpe haengt an einem GPIO-Contactor ohne
Rueckmeldung. Schlaegt pump.off() fehl, wird das Zielventil bewusst NICHT
geschlossen (nicht gegen eine moeglicherweise laufende Pumpe), nichts wird
freigegeben, der Fehler ist kritisch und der Bediener muss den physischen
NOT-AUS betaetigen. Die Aktoren bleiben bis zur Quittierung gehalten; bis dahin
gibt es keinen weiteren Start. Software kann keinen Pumpenstillstand beweisen.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from domain.recipe.limits import MIXER_SENSOR_RELIABLE_MIN_LITERS
from gpio_config import OUTPUTS
from statemachine.measurement_evaluation import STATUS_NOT_EVALUATED, STATUS_OK
from statemachine.sensor_reading import finite_number

from .background_process import BackgroundHardwareProcess
from .common import make_level_history, mixer_liters_from_snapshot
from .drain_safety import (
    DrainSafetyLimits,
    DrainSafetySettingsError,
    DrainSensorWatchdog,
    filtered_after_observation,
    parse_bridge_stamp,
    shutdown_errors,
    snapshot_stamp,
)

TARGET_TANK_NOMINAL_LITERS = 300.0

# Bestaetigte Zuordnung: Zieltank -> (Aktorname = Schluessel in gpio_config.OUTPUTS, GPIO).
# Die GPIO-Nummern sind hier bewusst NOCHMAL festgeschrieben: weicht
# gpio_config.OUTPUTS davon ab, blockiert der Transfer (fail-closed), statt
# ein anderes Ventil zu schalten.
TARGET_TANK_ROUTING: dict[int, tuple[str, int]] = {
    1: ("valve_1", 27),
    2: ("valve_2", 19),
    3: ("valve_3", 24),
    4: ("valve_4", 25),
}
TRANSFER_PUMP_NAME = "transfer_pump"
TRANSFER_PUMP_GPIO = 26

# Alle dokumentierten Ventile (Valve 0 bis 6), die VOR dem Oeffnen des Zielventils geschlossen werden:
# (Aktorname, Schluessel in gpio_config.OUTPUTS). Die Aktornamen entsprechen denen der anderen Prozesse
# und des Dashboards (drain_valve_0, supply_valve_6, valve_1 ...), die GPIO-Nummern kommen aus
# gpio_config.OUTPUTS. Drain steht bewusst an erster Stelle. Valve 7 bis 9 sind nicht dokumentiert/genutzt
# und werden nicht beruehrt; Pumpen und Contactoren gehoeren nie in diese Liste.
PRE_TRANSFER_CLOSE_VALVES: tuple[tuple[str, str], ...] = (
    ("drain_valve_0", "valve_0_drain"),
    ("valve_1", "valve_1"),
    ("valve_2", "valve_2"),
    ("valve_3", "valve_3"),
    ("valve_4", "valve_4"),
    ("valve_5", "valve_5"),
    ("supply_valve_6", "test_supply_valve_6"),
)

CRITICAL_PUMP_OFF_MESSAGE = (
    "KRITISCH: Die Transferpumpe konnte nicht abgeschaltet werden. Ohne Contactor-Rueckmeldung ist "
    "NICHT bestaetigt, dass die Pumpe steht. Physischen NOT-AUS BETAETIGEN. Das Zielventil wurde "
    "bewusst NICHT geschlossen. Bis zur Quittierung ist kein weiterer Transfer moeglich."
)

# Unterhalb dieser Mixer-Menge ist der Fuellstandssensor nicht belastbar
# (Konstante in domain/recipe/limits.py, gemeinsam mit dem AP-05-Dosierstart).


class TransferPhase:
    IDLE = "IDLE"
    OPENING_VALVE = "OPENING_VALVE"
    TRANSFERRING = "TRANSFERRING"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class ValveCloseError(RuntimeError):
    """Vorab-Schliessen der dokumentierten Ventile fehlgeschlagen: Transfer faehrt nicht an."""


@dataclass(frozen=True)
class TransferRequest:
    """Bedienerfreigabe. Alle Felder werden strikt geprueft (kein Cast, kein bool als Zahl)."""

    run_id: Any
    tank: Any
    target_level_liters: Any
    capacity_confirmed: Any
    not_evaluated_acknowledged: Any = False


@dataclass(frozen=True)
class TransferPlan:
    run_id: str
    tank: int
    valve_name: str
    valve_pin: int
    pump_pin: int
    close_valves: tuple[tuple[str, int], ...]
    target_level_liters: float
    evaluation_status: str
    not_evaluated_acknowledged: bool
    start_liters: float
    start_stamp: str
    limits: DrainSafetyLimits
    released_at: str

    def release_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tank": self.tank,
            "valve": self.valve_name,
            "target_level_liters_operator_input": self.target_level_liters,
            "nominal_capacity_liters": TARGET_TANK_NOMINAL_LITERS,
            "mixer_start_liters": self.start_liters,
            "operator_confirmed_free_capacity_incl_reserve": True,
            "evaluation_status": self.evaluation_status,
            "not_evaluated_acknowledged": self.not_evaluated_acknowledged,
            "released_at": self.released_at,
        }


def resolve_routing(tank: Any) -> tuple[str, int, int] | str:
    """(Ventilname, Ventil-GPIO, Pumpen-GPIO) oder eine Fehlermeldung (fail-closed)."""
    if type(tank) is not int or tank not in TARGET_TANK_ROUTING:
        return f"Zieltank muss 1, 2, 3 oder 4 sein (war: {tank!r})."
    valve_name, valve_pin = TARGET_TANK_ROUTING[tank]
    if OUTPUTS.get(valve_name) != valve_pin or OUTPUTS.get(TRANSFER_PUMP_NAME) != TRANSFER_PUMP_GPIO:
        return (
            "GPIO-Konfiguration weicht von der bestaetigten Transferzuordnung ab "
            f"({valve_name}={OUTPUTS.get(valve_name)!r}, erwartet {valve_pin}; "
            f"{TRANSFER_PUMP_NAME}={OUTPUTS.get(TRANSFER_PUMP_NAME)!r}, erwartet {TRANSFER_PUMP_GPIO})."
        )
    # Drain und Valve 6 duerfen nie Zielventil sein.
    if valve_pin in (OUTPUTS.get("valve_0_drain"), OUTPUTS.get("test_supply_valve_6")):
        return "Zielventil kollidiert mit Drain-/Versorgungsventil."
    # Ohne eindeutige, gueltige Pins aller dokumentierten Ventile laesst sich nicht vorab schliessen.
    closing = resolve_valve_closing(valve_name, valve_pin, TRANSFER_PUMP_GPIO)
    if isinstance(closing, str):
        return closing
    return valve_name, valve_pin, TRANSFER_PUMP_GPIO


def resolve_valve_closing(valve_name: str, valve_pin: int, pump_pin: int) -> tuple[tuple[str, int], ...] | str:
    """(Aktorname, GPIO) aller vor dem Transfer zu schliessenden Ventile oder eine Fehlermeldung (fail-closed).

    Rein rechnerisch, beruehrt keine Hardware. Jeder GPIO muss eine einfache Ganzzahl im BCM-Bereich 0 bis 27
    sein (kein bool) und darf weder doppelt vorkommen noch der Transferpumpe gehoeren. Das Zielventil muss in
    der Liste stehen."""
    closing: list[tuple[str, int]] = []
    used_pins = {pump_pin}
    for actuator_name, config_key in PRE_TRANSFER_CLOSE_VALVES:
        pin = OUTPUTS.get(config_key)
        if type(pin) is not int or not 0 <= pin <= 27:
            return (
                f"Ventil {actuator_name} laesst sich nicht vorab schliessen: ungueltiger GPIO in gpio_config "
                f"({config_key}={pin!r})."
            )
        if pin in used_pins:
            return (
                f"Ventil {actuator_name} laesst sich nicht vorab schliessen: GPIO {pin} ist doppelt belegt "
                f"({config_key}, Pumpe oder anderes Ventil)."
            )
        used_pins.add(pin)
        closing.append((actuator_name, pin))
    if (valve_name, valve_pin) not in closing:
        return f"Zielventil {valve_name} (GPIO {valve_pin}) fehlt in der Liste der vorab zu schliessenden Ventile."
    return tuple(closing)


def validate_transfer_release(
    request: Any,
    evaluation_status: Any,
    mixer_start_liters: Any,
    max_mixer_liters: Any,
) -> str | None:
    """Reine Freigabepruefung. None = freigegeben, sonst Blockgrund (fail-closed)."""
    if not isinstance(request, TransferRequest):
        return "Transferfreigabe fehlt oder ist ungueltig."

    routing = resolve_routing(request.tank)
    if isinstance(routing, str):
        return routing

    if evaluation_status not in (STATUS_OK, STATUS_NOT_EVALUATED):
        return f"Endbewertung des Mischlaufs fehlt oder ist ungueltig ({evaluation_status!r})."
    if evaluation_status == STATUS_NOT_EVALUATED and request.not_evaluated_acknowledged is not True:
        return (
            "EC/pH-Endbewertung ist NOT_EVALUATED (Sollwerterfuellung nicht bestaetigt, kein Qualitaetsnachweis). "
            "Der Transfer braucht dafuer eine gesonderte ausdrueckliche Bestaetigung."
        )

    if request.capacity_confirmed is not True:
        return (
            "Bestaetigung fehlt: ausreichend freie Zieltank-Kapazitaet einschliesslich Sicherheitsreserve "
            "(die Software prueft keine Ueberlaufsicherheit)."
        )

    if isinstance(request.target_level_liters, bool):
        return "Zieltank-Fuellstand muss eine Zahl sein."
    level = finite_number(request.target_level_liters)
    if level is None or level < 0.0 or level > TARGET_TANK_NOMINAL_LITERS:
        return (
            f"Zieltank-Fuellstand muss eine endliche Zahl von 0 bis {TARGET_TANK_NOMINAL_LITERS:g} L sein "
            f"(war: {request.target_level_liters!r})."
        )

    mixer = finite_number(mixer_start_liters)
    if mixer is None or mixer < 0.0:
        return "Mixer-Menge beim Transferstart nicht verfuegbar."
    if mixer < MIXER_SENSOR_RELIABLE_MIN_LITERS:
        return (
            f"Mixer-Menge {mixer:.2f} L liegt unter {MIXER_SENSOR_RELIABLE_MIN_LITERS:g} L: "
            "die Menge ist mit dem Sensor nicht belastbar bestimmbar, kein Transfer."
        )
    max_mixer = finite_number(max_mixer_liters)
    if max_mixer is None or max_mixer <= 0.0 or mixer > max_mixer:
        return f"Mixer-Menge {mixer:.2f} L ist unplausibel (max_mixer_liters {max_mixer_liters!r})."

    if level + mixer > TARGET_TANK_NOMINAL_LITERS:
        return (
            f"Nennvolumen wuerde ueberschritten: Zieltank {level:.1f} L + Mixer {mixer:.1f} L = "
            f"{level + mixer:.1f} L > {TARGET_TANK_NOMINAL_LITERS:g} L."
        )

    return None


class TargetTransferController(BackgroundHardwareProcess):
    label = "Target Transfer"
    stop_label = "Target Transfer"
    stop_join_timeout_seconds = 5.0

    #: Takt der Ueberwachungsschleife; Tests setzen ihn auf 0.
    poll_interval_seconds = 0.5

    def __init__(self, get_sensor_snapshot: Callable[[], dict[str, Any] | None]) -> None:
        super().__init__(get_sensor_snapshot)

        self._plan: TransferPlan | None = None
        # Nur ERFOLGREICH abgeschlossene Transfers verbrauchen die Freigabe eines Mischlaufs.
        self._completed_run_ids: set[str] = set()
        # Stoerung (Fehler/Emergency Stop): neuer Start erst nach acknowledge().
        self._fault_latched = False
        # Pumpenabschaltung fehlgeschlagen: Aktoren bleiben gehalten (kein Ventil-Zu, kein Close).
        self._held_actuators = None
        self._held_pump = None
        self._held_valve = None
        self._pump_off_failed = False

        self._phase = TransferPhase.IDLE
        self._outcome: str | None = None  # completed | failed | stopped
        self._pump_started_at: float | None = None
        self._max_seconds: float | None = None
        self._expected_seconds: float | None = None
        self._start_liters: float | None = None
        self._current_liters: float | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_raw_liters: float | None = None
        self._last_stamp: str | None = None
        self._logger = None
        self._cleanup_errors: list[str] = []
        self._cleanup_previous_reason: str | None = None

    # ---- public dashboard API -----------------------------------------

    def is_run_used(self, run_id: Any) -> bool:
        """True, wenn mit dieser Mischlauf-Kennung bereits ein Transfer erfolgreich abgeschlossen wurde."""
        with self._lock:
            return run_id in self._completed_run_ids

    def fault_latched(self) -> bool:
        """Stoerung nicht quittiert: kein neuer Transferstart."""
        with self._lock:
            return self._fault_latched

    def hardware_fault_pending(self) -> bool:
        """Pumpenabschaltung fehlgeschlagen und noch nicht bereinigt: Aktoren sind noch gehalten."""
        with self._lock:
            return self._held_actuators is not None

    def start(self, settings: dict[str, Any], request: Any, evaluation_status: Any) -> dict[str, Any]:
        """Prueft die Freigabe und startet den Thread. Rueckgabe success=True nur, wenn
        der Thread wirklich gestartet wurde; ein blockierter Start beruehrt keine Hardware
        und verbraucht die Freigabe des Mischlaufs nicht."""
        with self._lock:
            if self.is_active():
                return self._result(False, "Target Transfer laeuft bereits.")

            if self._fault_latched:
                return self._result(
                    False,
                    "Transfer blockiert: Stoerung des letzten Transfers ist nicht quittiert (Reset / Ack).",
                )

            block_message = self._check_start_preconditions_locked(settings)
            if block_message is not None:
                return self._result(False, block_message)

            blocked = self._prepare_plan_locked(settings, request, evaluation_status)
            if isinstance(blocked, str):
                self._last_error = blocked
                self._last_message = f"Transfer blockiert: {blocked}"
                return self._result(False, self._last_message)
            plan = blocked

            self._plan = plan
            self._phase = TransferPhase.OPENING_VALVE
            self._outcome = None
            self._cleanup_errors = []
            self._cleanup_previous_reason = None
            self._pump_started_at = None
            self._start_liters = plan.start_liters
            self._current_liters = plan.start_liters
            self._expected_seconds = plan.limits.expected_seconds
            self._max_seconds = plan.limits.max_drain_seconds
            self._last_message = f"Transfer in Tank {plan.tank} angefordert."
            self._begin_run_locked("cds-target-transfer", settings)

        return self._result(True, f"Transfer in Zieltank {plan.tank} gestartet.")

    def acknowledge(self) -> dict[str, Any]:
        """Quittiert/resettet einen beendeten Transfer. Erfolgreich abgeschlossene Mischlaeufe
        bleiben verbraucht. Waren Aktoren nach fehlgeschlagener Pumpenabschaltung gehalten,
        wird die Abschaltung erneut versucht (Pumpe zuerst); schlaegt sie wieder fehl, bleibt
        die Stoerung bestehen."""
        with self._lock:
            if self.is_active():
                return self._result(False, "Reset blockiert: Target Transfer laeuft noch.")
            if self._held_actuators is not None:
                errors = self._retry_held_cleanup_locked()
                if errors:
                    return self._result(
                        False,
                        "Quittierung blockiert: Transferpumpe laesst sich weiterhin nicht abschalten "
                        f"({'; '.join(errors)}). Physischen NOT-AUS betaetigen und Anlage pruefen.",
                    )
            self._fault_latched = False
            self._pump_off_failed = False
            self._plan = None
            self._phase = TransferPhase.IDLE
            self._outcome = None
            self._stop_reason = None
            self._last_error = None
            self._pump_started_at = None
            self._max_seconds = None
            self._expected_seconds = None
            self._start_liters = None
            self._current_liters = None
            self._cleanup_errors = []
            self._last_message = "Target Transfer ready."
        return self._result(True, "Target Transfer zurueckgesetzt.")

    def retry_hardware_cleanup(self) -> dict[str, Any]:
        """Fuer Emergency Stop/Shutdown: Abschaltung gehaltener Aktoren erneut versuchen
        (Pumpe zuerst). Ohne gehaltene Aktoren ein No-op."""
        with self._lock:
            if self._held_actuators is None:
                return self._result(True, "Keine ausstehende Transfer-Abschaltung.")
            errors = self._retry_held_cleanup_locked()
            if errors:
                return self._result(False, f"Transferpumpe laesst sich weiterhin nicht abschalten: {'; '.join(errors)}")
            return self._result(True, "Transferpumpe abgeschaltet, Zielventil geschlossen.")

    def _retry_held_cleanup_locked(self) -> list[str]:
        """Pumpe aus; NUR wenn das gelingt: Ventil zu, dann Aktoren freigeben. Fehlerliste, leer = ok."""
        errors: list[str] = []
        try:
            if self._held_pump is not None:
                self._held_pump.off()
        except Exception as exc:
            errors.append(f"transfer_pump.off() failed: {type(exc).__name__}: {exc}")
            return errors  # Ventil bleibt offen, Aktoren bleiben gehalten
        try:
            if self._held_valve is not None:
                self._held_valve.off()
        except Exception as exc:
            errors.append(f"target valve off() failed: {type(exc).__name__}: {exc}")
        try:
            errors.extend(shutdown_errors(self._held_actuators.safe_shutdown_all()))
            errors.extend(shutdown_errors(self._held_actuators.close_all()))
        except Exception as exc:
            errors.append(f"cleanup failed: {type(exc).__name__}: {exc}")
        if not errors:
            self._held_actuators = self._held_pump = self._held_valve = None
        return errors

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            is_active = self._thread is not None and self._thread.is_alive()
            plan = self._plan
            elapsed = 0.0
            if is_active and self._pump_started_at is not None:
                elapsed = max(0.0, time.monotonic() - self._pump_started_at)

            transferred = None
            if self._start_liters is not None and self._current_liters is not None:
                transferred = max(0.0, self._start_liters - self._current_liters)

            result = {
                TransferPhase.FINISHED: "COMPLETED",
                TransferPhase.STOPPED: "STOPPED",
                TransferPhase.ERROR: "ERROR",
            }.get(self._phase)

            return {
                "is_active": is_active,
                "phase": self._phase,
                "result": result,
                "run_id": plan.run_id if plan else None,
                "target_tank": plan.tank if plan else None,
                "target_level_liters_operator_input": plan.target_level_liters if plan else None,
                "evaluation_status": plan.evaluation_status if plan else None,
                "not_evaluated_acknowledged": plan.not_evaluated_acknowledged if plan else None,
                "start_mixer_liters": self._start_liters,
                "current_mixer_liters": self._current_liters,
                # Sensorrechnung (Start minus aktueller Mixerstand), unter ca. 10 L nicht belastbar.
                "transferred_liters_sensor_calc": transferred,
                "elapsed_seconds": round(elapsed, 1),
                "expected_seconds": self._expected_seconds,
                "max_seconds": self._max_seconds,
                "started_at": self._started_at_iso,
                "stop_reason": self._stop_reason,
                "last_error": self._last_error,
                "last_message": self._last_message,
                "cleanup_errors": list(self._cleanup_errors),
                "fault_latched": self._fault_latched,
                # KRITISCH: Pumpenabschaltung fehlgeschlagen, Stillstand NICHT bestaetigt.
                "pump_off_failed": self._pump_off_failed,
            }

    # ---- start preparation --------------------------------------------

    def _prepare_plan_locked(self, settings: dict[str, Any], request: Any, evaluation_status: Any) -> TransferPlan | str:
        if not isinstance(request, TransferRequest) or not isinstance(request.run_id, str) or not request.run_id:
            return "Transferfreigabe ohne gueltige Mischlauf-Kennung."
        if request.run_id in self._completed_run_ids:
            return "Fuer diesen Mischlauf wurde der Transfer bereits erfolgreich abgeschlossen."

        try:
            snapshot = self.get_sensor_snapshot()
        except Exception as exc:
            return f"Sensor-Snapshot nicht lesbar ({type(exc).__name__}: {exc})."
        if not isinstance(snapshot, dict):
            return "Kein aktueller Sensor-Snapshot."

        stamp = snapshot_stamp(snapshot)
        if parse_bridge_stamp(stamp) is None:
            return "Sensor-Snapshot ohne gueltigen Zeitstempel."
        start_liters = mixer_liters_from_snapshot(snapshot, settings)

        error = validate_transfer_release(
            request, evaluation_status, start_liters, settings.get("max_mixer_liters")
        )
        if error is not None:
            return error

        routing = resolve_routing(request.tank)
        if isinstance(routing, str):  # bereits geprueft, doppelt haelt besser
            return routing
        valve_name, valve_pin, pump_pin = routing

        close_valves = resolve_valve_closing(valve_name, valve_pin, pump_pin)
        if isinstance(close_valves, str):  # bereits ueber resolve_routing geprueft, doppelt haelt besser
            return close_valves

        try:
            limits = DrainSafetyLimits.from_settings(settings, start_liters)
        except DrainSafetySettingsError as exc:
            return f"Sicherheitseinstellungen unbrauchbar: {exc}"

        return TransferPlan(
            run_id=request.run_id,
            tank=request.tank,
            valve_name=valve_name,
            valve_pin=valve_pin,
            pump_pin=pump_pin,
            close_valves=close_valves,
            target_level_liters=float(request.target_level_liters),
            evaluation_status=evaluation_status,
            not_evaluated_acknowledged=request.not_evaluated_acknowledged is True,
            start_liters=start_liters,
            start_stamp=stamp,
            limits=limits,
            released_at=datetime.now().isoformat(timespec="seconds"),
        )

    # ---- state helpers --------------------------------------------------

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def _fail(self, stop_reason: str, message: str) -> None:
        with self._lock:
            self._outcome = "failed"
            self._stop_reason = stop_reason
            self._last_error = message
            self._last_message = message

    def _mark_stopped(self, fallback_reason: str) -> None:
        with self._lock:
            self._outcome = "stopped"
            self._stop_reason = self._stop_reason or fallback_reason
            self._last_message = f"Transfer gestoppt: {self._stop_reason}"

    def _mark_completed(self) -> None:
        with self._lock:
            self._outcome = "completed"
            self._stop_reason = "empty_by_sensor_zero"
            self._current_liters = 0.0

    def _record_cleanup_failure(self, errors: list[str]) -> None:
        """Abschaltfehler (akkumuliert); wird nie als erfolgreicher Stopp gemeldet."""
        with self._lock:
            if not self._cleanup_errors:
                self._cleanup_previous_reason = self._stop_reason
            for error in errors:
                if error not in self._cleanup_errors:
                    self._cleanup_errors.append(error)
            previous = self._cleanup_previous_reason
            self._outcome = "failed"
            self._stop_reason = "cleanup_failed"
            self._last_error = (
                f"Abschalten fehlgeschlagen: {'; '.join(self._cleanup_errors)}. "
                "Transferpumpe und Zielventil PHYSISCH pruefen."
                + (f" (vorher: {previous})" if previous not in (None, "cleanup_failed") else "")
            )
            self._last_message = self._last_error

    def _read_mixer(self, settings: dict[str, Any], level_history) -> float | None:
        """Gefilterter Mixerstand; merkt ungefilterten Wert und Zeitstempel fuer den Watchdog."""
        self._last_raw_liters = None
        self._last_stamp = None

        snapshot = self.get_sensor_snapshot()
        self._last_snapshot = snapshot if isinstance(snapshot, dict) else None
        if not isinstance(snapshot, dict):
            return None

        self._last_stamp = snapshot_stamp(snapshot)
        liters = mixer_liters_from_snapshot(snapshot, settings)
        if liters is None:
            return None
        self._last_raw_liters = liters
        level_history.append(liters)
        return sum(level_history) / len(level_history)

    # ---- background thread ---------------------------------------------

    def _run(self, settings: dict[str, Any]) -> None:
        from gpio_config import ACTIVE_LOW
        from hardware.actuator_manager import ActuatorManager
        from services.mqtt_publisher import MqttPublisher
        from services.process_run_logger import ProcessRunLogger

        plan = self._plan
        actuators = None
        pump = None
        valve = None

        try:
            if plan is None:
                raise RuntimeError("kein Transferplan")

            if self._stop_event.is_set():
                self._mark_stopped("stopped_before_start")
                return

            actuators = ActuatorManager(active_low=ACTIVE_LOW)
            self._actuators = actuators
            # Pumpe zuerst registriert: auch ein safe_shutdown_all() schaltet sie vor dem Ventil ab.
            pump = actuators.add(name=TRANSFER_PUMP_NAME, gpio_pin=plan.pump_pin)

            # Danach ALLE dokumentierten Ventile (Valve 0 bis 6) geschlossen anlegen und pruefen, bevor
            # irgendetwas geoeffnet oder gefoerdert wird. Bei einem Fehler wurden Zielventil und Pumpe
            # nie eingeschaltet; der Cleanup im finally gibt alles frei.
            try:
                valves = self._close_all_valves(actuators, plan)
            except ValveCloseError as exc:
                self._fail(
                    "valve_close_failed",
                    f"Transfer abgebrochen: nicht alle Ventile konnten geschlossen werden ({exc}). "
                    "Weder Zielventil noch Pumpe wurden eingeschaltet.",
                )
                return
            valve = valves[plan.valve_name]

            self._mqtt_publisher = MqttPublisher()
            self._logger = ProcessRunLogger(process_name="target_transfer")
            self._emit("TARGET_TRANSFER_RELEASED", actuators)

            self._transfer(settings, plan, actuators, pump, valve)

        except Exception as exc:
            self._fail("error", f"Transfer fehlgeschlagen: {type(exc).__name__}: {exc}")

        finally:
            self._safe_shutdown_and_publish(actuators, pump, valve)

    def _close_all_valves(self, actuators, plan: TransferPlan) -> dict[str, Any]:
        """Legt alle dokumentierten Ventile als Ausgang im inaktiven (geschlossenen) Zustand an und prueft
        den gemeldeten Zustand. Rueckgabe: Aktorname -> Ausgang (enthaelt das Zielventil). Jeder Fehler,
        jedes nicht als inaktiv bestaetigte Ventil und jeder nicht lesbare Zustand wirft ValveCloseError.
        Keine physische Rueckmeldung: bestaetigt ist nur der Software-Zustand."""
        outputs: dict[str, Any] = {}
        for name, pin in plan.close_valves:
            try:
                outputs[name] = actuators.add(name=name, gpio_pin=pin)
            except Exception as exc:
                raise ValveCloseError(f"{name} (GPIO {pin}): {type(exc).__name__}: {exc}") from exc
            print(f"[CLOSE] {name} | GPIO {pin}")

        try:
            status = actuators.status_payload()
        except Exception as exc:
            raise ValveCloseError(f"Ventilzustand nicht lesbar ({type(exc).__name__}: {exc})") from exc
        not_closed = [name for name in outputs if status.get(name) is not False]
        if not_closed:
            raise ValveCloseError(f"nicht als geschlossen bestaetigt: {', '.join(not_closed)}")
        return outputs

    def _transfer(self, settings, plan: TransferPlan, actuators, pump, valve) -> None:
        limits = plan.limits

        # Alle Ventile sind bereits geschlossen (_close_all_valves). Ein Stop, der waehrenddessen kam,
        # verhindert das Oeffnen des Zielventils ganz.
        if self._stop_event.is_set():
            self._mark_stopped("stopped_before_valve_open")
            return

        # Start: Ventil auf -> Settle -> Stop erneut pruefen -> Sensor erneut pruefen -> Pumpe an.
        valve.on()
        self._emit("TARGET_TRANSFER_VALVE_OPEN", actuators)

        if self._stop_event.wait(limits.valve_settle_seconds):
            self._mark_stopped("stopped_during_valve_settle")
            return
        if self._stop_event.is_set():
            self._mark_stopped("stopped_before_pump_start")
            return

        level_history = make_level_history(settings)
        level_history.append(plan.start_liters)
        self._read_mixer(settings, level_history)
        if self._last_raw_liters is None:
            self._fail("missing_mixer_level_before_pump", "Kein gueltiger Mixerstand unmittelbar vor Pumpenstart.")
            return
        if self._stop_event.is_set():
            self._mark_stopped("stopped_before_pump_start")
            return

        pump.on()
        pump_started = time.monotonic()
        with self._lock:
            self._pump_started_at = pump_started
            self._phase = TransferPhase.TRANSFERRING
            self._last_message = f"Transfer in Tank {plan.tank} laeuft."
        watchdog = DrainSensorWatchdog(limits, pump_started, plan.start_stamp)
        # Letzter plausibler Messwert des Entleerverlaufs (Start = erfasste Mixer-Menge).
        last_plausible_liters = plan.start_liters
        self._emit("TARGET_TRANSFER_RUNNING", actuators)

        while True:
            if self._stop_event.wait(self.poll_interval_seconds):
                self._mark_stopped("stopped_by_user")
                return

            elapsed = time.monotonic() - pump_started
            history_before = list(level_history)
            liters = self._read_mixer(settings, level_history)
            # `now` NACH dem Lesen, damit ein langsamer Read keine abgelaufene Frist verdeckt.
            observation = watchdog.observe(self._last_raw_liters, self._last_stamp, time.monotonic())
            liters = filtered_after_observation(
                level_history,
                history_before,
                liters,
                self._last_raw_liters is not None,
                observation.is_new_sample,
            )
            with self._lock:
                self._current_liters = liters if liters is not None else self._current_liters

            if observation.violation is not None:
                reason, message = observation.violation
                self._fail(reason, f"{reason}: {message}")
                return

            # Nur eine frische Messung zaehlt (ein wiederholter Snapshot ist keine neue).
            raw = self._last_raw_liters
            if observation.is_new_sample and raw is not None:
                if raw == 0.0:
                    # Erste 0 L = Mixing Tank tatsaechlich leer (bestaetigtes Sensorverhalten),
                    # aber nur, wenn der Verlauf schon im unteren Bereich war. Sofort beenden:
                    # der Cleanup schaltet als Erstes die Pumpe ab.
                    if last_plausible_liters <= MIXER_SENSOR_RELIABLE_MIN_LITERS:
                        self._mark_completed()
                        return
                    # Unplausible Null mitten im hohen Pegel: kein Ende, kein Verlauf.
                else:
                    last_plausible_liters = raw

            self._emit("TARGET_TRANSFER_RUNNING", actuators, elapsed=elapsed)

            if elapsed >= limits.max_drain_seconds:
                self._fail(
                    "calculated_transfer_timeout",
                    "Maximale Transferlaufzeit erreicht, bevor der Mixer 0 L meldete.",
                )
                return

    def _safe_shutdown_and_publish(self, actuators, pump, valve) -> None:
        # Stop-Reihenfolge: Transferpumpe AUS, dann Zielventil ZU. Schlaegt pump.off() fehl,
        # wird das Ventil NICHT geschlossen (nicht gegen eine moeglicherweise laufende Pumpe).
        try:
            if pump is not None:
                pump.off()
        except Exception as exc:
            self._handle_pump_off_failure(actuators, pump, valve, f"transfer_pump.off() failed: {type(exc).__name__}: {exc}")
            return

        shutdown_problems: list[str] = []
        try:
            if valve is not None:
                valve.off()
        except Exception as exc:
            shutdown_problems.append(f"{getattr(valve, 'name', 'target_valve')}.off() failed: {type(exc).__name__}: {exc}")

        try:
            if actuators is not None:
                shutdown_problems.extend(shutdown_errors(actuators.safe_shutdown_all()))
        except Exception as exc:
            shutdown_problems.append(f"safe_shutdown_all failed: {type(exc).__name__}: {exc}")

        # Vor dem Statuspublish festhalten, damit der Status den Fehler traegt.
        if shutdown_problems:
            self._record_cleanup_failure(shutdown_problems)

        self._finalize_phase()
        self._emit("TARGET_TRANSFER_STOPPED", actuators)

        close_problems: list[str] = []
        try:
            if actuators is not None:
                close_problems.extend(shutdown_errors(actuators.close_all()))
        except Exception as exc:
            close_problems.append(f"close_all failed: {type(exc).__name__}: {exc}")

        if close_problems:
            shutdown_problems.extend(close_problems)
            self._record_cleanup_failure(shutdown_problems)
            self._finalize_phase()
            self._emit("TARGET_TRANSFER_STOPPED", actuators)

        self._close_telemetry()

        with self._lock:
            # Nur ein sauber abgeschlossener Transfer verbraucht die Freigabe des Mischlaufs.
            if self._outcome == "completed" and self._plan is not None:
                self._completed_run_ids.add(self._plan.run_id)
            self._actuators = None

    def _handle_pump_off_failure(self, actuators, pump, valve, pump_error: str) -> None:
        """pump.off() ist fehlgeschlagen: kritischer Fehler, KEIN Ventil-Zu, KEIN Freigeben der
        Aktoren (Hardware-Sperre bleibt), Stoerung bleibt bis zur Quittierung bestehen."""
        with self._lock:
            self._held_actuators = actuators
            self._held_pump = pump
            self._held_valve = valve
            self._pump_off_failed = True
            self._fault_latched = True
            self._outcome = "failed"
            self._cleanup_errors = [pump_error]
            self._stop_reason = "pump_off_failed"
            self._last_error = f"{CRITICAL_PUMP_OFF_MESSAGE} ({pump_error})"
            self._last_message = self._last_error
        self._finalize_phase()
        self._emit("TARGET_TRANSFER_PUMP_OFF_FAILED", actuators)
        self._close_telemetry()
        with self._lock:
            self._actuators = None

    def _close_telemetry(self) -> None:
        try:
            if self._mqtt_publisher is not None:
                self._mqtt_publisher.close()
        except Exception:
            pass
        try:
            if self._logger is not None:
                self._logger.close()
        except Exception:
            pass
        with self._lock:
            self._mqtt_publisher = None
            self._logger = None

    def _finalize_phase(self) -> None:
        with self._lock:
            if self._outcome == "completed":
                self._phase = TransferPhase.FINISHED
                self._last_message = "Transfer abgeschlossen (Mixer meldet 0 L)."
            elif self._outcome == "stopped":
                self._phase = TransferPhase.STOPPED
                if self._stop_reason == "emergency_stop":
                    self._fault_latched = True
            else:
                self._phase = TransferPhase.ERROR
                self._fault_latched = True  # Neustart erst nach Quittierung
                if self._last_error is None:
                    self._last_error = "Transfer ohne Abschluss beendet."
                self._last_message = self._last_error

    # ---- status + log ---------------------------------------------------

    def _emit(self, process_state: str, actuators, elapsed: float | None = None) -> None:
        """Dashboard-/MQTT-Status und Prozesslog. Nie ein Grund, den Ablauf abzubrechen."""
        try:
            actuator_status = actuators.status_payload() if actuators is not None else {}
        except Exception:
            actuator_status = {}

        status = self.get_status()
        plan = self._plan

        publisher = self._mqtt_publisher
        if publisher is not None:
            payload = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "source": "python_nicegui",
                "process_state": process_state,
                "actuators": {
                    "transfer_pump": actuator_status.get(TRANSFER_PUMP_NAME, False),
                    # Alle vorab geschlossenen Ventile (Drain, Valve 1 bis 5, Supply 6); None = nicht angelegt.
                    **{name: actuator_status.get(name) for name, _config_key in PRE_TRANSFER_CLOSE_VALVES},
                },
                "target_transfer": status,
                "error": status.get("last_error"),
            }
            try:
                publisher.publish_json(payload)
            except Exception:
                pass

        logger = self._logger
        if logger is not None and plan is not None:
            try:
                logger.write_step(
                    state=process_state,
                    error=status.get("last_error"),
                    snapshot=self._last_snapshot,
                    actuator_status=actuator_status,
                    mixer_liters_filtered=status.get("current_mixer_liters"),
                    start_mixer_liters=plan.start_liters,
                    extra={
                        "phase": status.get("phase"),
                        "stop_reason": status.get("stop_reason"),
                        "elapsed_seconds": elapsed if elapsed is not None else status.get("elapsed_seconds"),
                        "drained_liters": status.get("transferred_liters_sensor_calc"),
                        "evaluation_status": plan.evaluation_status,
                        "transfer_target_tank": plan.tank,
                        "transfer_run_id": plan.run_id,
                        "transfer_release": json.dumps(plan.release_record(), sort_keys=True),
                    },
                )
            except Exception:
                pass


__all__ = [
    "MIXER_SENSOR_RELIABLE_MIN_LITERS",
    "PRE_TRANSFER_CLOSE_VALVES",
    "TARGET_TANK_NOMINAL_LITERS",
    "TARGET_TANK_ROUTING",
    "TargetTransferController",
    "TransferPhase",
    "TransferPlan",
    "TransferRequest",
    "ValveCloseError",
    "resolve_routing",
    "resolve_valve_closing",
    "validate_transfer_release",
]
