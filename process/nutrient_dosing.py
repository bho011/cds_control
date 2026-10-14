"""
MCU_B-Nährstoffdosierung (AP-05 Schritt 1, Prototyp): führt einen vorhandenen
``NutrientDosingPlan`` über den vorhandenen ``PeristalticSerialClient`` aus.

Bewusst einfach (Prototyp):

* Strikt sequenziell: P1, P2, P3, P4 nacheinander, je Pumpe alle Chunks, immer
  genau EIN DOSE-Befehl gleichzeitig. Die Mengenverteilung A/B und die
  Chunk-Planung kommen unverändert aus dem Plan.
* Gates: die Basisklassen-Gates (``hardware_execution_enabled`` strikt ``True``,
  aktueller Sensor-Snapshot) sind verpflichtend und werden nicht umgangen. Dazu
  Mapping, bestätigtes Firmwareprofil, Kalibrierung, Port vorhanden, Plan
  ausführbar (Hash, Controller, Rollen, Profil-ID, Chunk-Grenze, Laufzeit).
* Vorläufige Prototypgrenze: höchstens ``NUTRIENT_MAX_DOSE_ML`` (10 ml) je
  DOSE-Befehl, unabhängig von der Plan-Konfiguration (der Plan erlaubt bis 50 ml).
* Nur Befehle, die mit DONE (Menge passt) UND anschließendem Status IDLE
  abgeschlossen wurden, zählen als softwareseitig erledigt (``confirmed_ul``).
  Bei Timeout, Fehler, Desync, Abbruch oder Emergency Stop wird STOPALL gesendet
  und der Lauf beendet. Ein dabei unklarer Chunk wird NIE wiederholt und in
  ``uncertain_ul`` ausgewiesen (tatsächlich geförderte Menge unbekannt). Es gibt
  keine automatische Wiederaufnahme, und derselbe Plan (``plan_hash``) wird von ``start()``
  nie ein zweites Mal dosiert; eine neue Dosierung gehört zu einem neuen Mischvorgang/Plan.
* Unterbrechbarkeit: ``PeristalticSerialClient.dose()`` würde bis zu 35 s auf DONE
  warten und STOPALL erzeugt kein DONE. Deshalb wird hier ``start_dose()`` mit einer
  eigenen Warteschleife (``poll_pump_event``, 0,1 s Takt) verwendet; ein Stopp
  (Dashboard-Stop oder Emergency Stop über ``request_stop``) sendet nach dem Start-
  Handshake STOPALL. Wartet ``start_dose()`` noch auf ``OK START``, blockiert der
  Aufrufer bis zur Antwort oder bis ``command_timeout_s`` (5 s); bei ausbleibender Antwort
  markiert der Client sich als desynchronisiert und schreibt direkt STOPALL. Die Reaktion
  ist dort also durch den Client-Timeout begrenzt, nicht durch den 0,1-s-Takt.
* Identität: PING/STATUS beweisen NICHT, dass am Port wirklich MCU_B hängt. Der
  korrekte MCU_B-Port in ``config/peristaltic_mapping.json`` wird als durch eine
  manuelle Hardwareprüfung bestätigt vorausgesetzt (vor realem Betrieb).
* Kein Loader, keine Policy, keine Fingerprints, keine Release-Gates.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.recipe.nutrient_plan import NutrientDosingPlan, NutrientPlanError, build_nutrient_dosing_plan
from services.peristaltic.calibration import (
    CalibrationValidationError,
    has_volumetric_calibration,
    load_calibration_data,
)
from services.peristaltic.firmware_profiles import (
    ControllerFirmwareProfile,
    FirmwareProfileError,
    load_firmware_profiles,
    resolve_firmware_ml_per_step,
)
from services.peristaltic.models import (
    UNASSIGNED,
    VALID_PUMPS,
    ControllerMapping,
    LineKind,
    MappingValidationError,
    load_mapping,
)
from services.peristaltic.prime import classify_guarded_exception
from services.peristaltic.safety import SafetyCheckFailed, best_effort_stop_all, ensure_all_idle_before_dose
from services.peristaltic.serial_client import (
    ClientConfig,
    PeristalticCommandError,
    PeristalticDesyncError,
    PeristalticError,
    PeristalticProtocolError,
    PeristalticSerialClient,
    PeristalticTimeoutError,
    SerialFactory,
    default_serial_factory,
)
from services.peristaltic.session_log import PeristalticSessionLogger

from .background_process import BackgroundHardwareProcess

MAPPING_PATH = Path("config/peristaltic_mapping.json")
FIRMWARE_PROFILES_PATH = Path("config/peristaltic_firmware_profiles.json")
CALIBRATION_DATA_PATH = Path("calibration_data/peristaltic_calibration.json")

CONTROLLER = "MCU_B"

# Vorläufige Prototypgrenze je DOSE-Befehl (Plan und Firmware erlauben mehr).
NUTRIENT_MAX_DOSE_ML = 10.0

# Vorläufiges RO-Vorfüllziel vor der Dosierung (Konzept: 30 l, konfigurierbar,
# real zu bestätigen); bei kleinen Rezepten höchstens die Hälfte der RO-Sollmenge,
# damit nach der Dosierung noch RO-Wasser zum Verdünnen folgt.
NUTRIENT_PREFILL_LITERS = 30.0
NUTRIENT_PREFILL_MAX_FRACTION = 0.5

# Laufzeitabschätzung je Chunk: Schritte = ml / firmware_ml_per_step, Schrittrate =
# speed_rpm * STEPS_PER_REV * microsteps / 60 (konstante Rate, Beschleunigungsrampe
# ignoriert). ANNAHME: 200 Vollschritte je Umdrehung - vor realem Betrieb gegen die
# Firmware bestätigen. Die Schätzung muss unter diesem Anteil der Firmware-
# Maximallaufzeit (Watchdog) bleiben, sonst wird der Start blockiert.
STEPS_PER_REV = 200
RUNTIME_LIMIT_FRACTION = 0.8
# Eigener Client-Wartezeit-Puffer über die Firmware-Maximallaufzeit hinaus.
WAIT_MARGIN_S = 5.0
POLL_INTERVAL_S = 0.1
# DONE meldet die Menge mit 3 Nachkommastellen; größere Abweichung = Fehler.
DONE_ML_TOLERANCE = 0.001

_PUMP_ORDER = {pump: index for index, pump in enumerate(VALID_PUMPS)}
_GUARDED_EXCEPTIONS = (PeristalticError, SafetyCheckFailed)


class DosingPhase:
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FINISHED = "FINISHED"
    ABORTED = "ABORTED"
    ERROR = "ERROR"


class _StopRequested(Exception):
    """Interner Abbruch durch request_stop (Dashboard-Stop/Emergency Stop)."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def estimate_chunk_runtime_ms(ml: float, firmware_ml_per_step: float, profile: ControllerFirmwareProfile) -> float:
    steps = ml / firmware_ml_per_step
    steps_per_s = profile.speed_rpm * STEPS_PER_REV * profile.microsteps / 60.0
    return steps / steps_per_s * 1000.0


def plan_blocking_reasons(
    plan: Any,
    entry: ControllerMapping,
    profile: ControllerFirmwareProfile,
    firmware_values: dict[str, float],
) -> list[str]:
    """Sammelt ALLE Gründe, warum der Plan so nicht ausgeführt werden darf. Rein,
    ohne I/O. ``firmware_values``: bestätigte firmware_ml_per_step je Pumpe."""
    if not isinstance(plan, NutrientDosingPlan):
        return ["Kein NutrientDosingPlan übergeben."]

    reasons: list[str] = []
    if not plan.verify_hash():
        reasons.append("Plan-Hash stimmt nicht mit dem Planinhalt überein.")
    if not plan.has_dosing_work:
        reasons.append("Plan enthält keine Dosierarbeit.")
        return reasons
    if plan.controller != CONTROLLER:
        reasons.append(f"Plan ist nicht für {CONTROLLER} (war: {plan.controller!r}).")
    if plan.profile_id is None or plan.profile_id != profile.profile_id:
        reasons.append("Profil-ID des Plans passt nicht zum bestätigten MCU_B-Firmwareprofil.")

    seen: set[str] = set()
    for target in plan.pump_targets:
        pump = target.pump
        if pump not in VALID_PUMPS or pump in seen:
            reasons.append(f"Ungültige oder doppelte Pumpe im Plan: {pump!r}.")
            continue
        seen.add(pump)

        role = entry.pumps.get(pump)
        if role is None or role == UNASSIGNED or role != target.role:
            reasons.append(f"{pump}: Rolle {target.role!r} im Plan passt nicht zum Mapping ({role!r}).")

        firmware_value = firmware_values.get(pump)
        for amount_ul in target.chunks_ul:
            if type(amount_ul) is not int or amount_ul <= 0:
                reasons.append(f"{pump}: ungültige Chunk-Menge {amount_ul!r}.")
                break
            if amount_ul / 1000.0 > NUTRIENT_MAX_DOSE_ML:
                reasons.append(
                    f"{pump}: Chunk {amount_ul / 1000.0} ml überschreitet die Prototypgrenze "
                    f"von {NUTRIENT_MAX_DOSE_ML} ml je Dosierbefehl."
                )
                break
        else:
            if target.chunks_ul and firmware_value is not None:
                largest_ml = max(target.chunks_ul) / 1000.0
                estimate_ms = estimate_chunk_runtime_ms(largest_ml, firmware_value, profile)
                limit_ms = profile.max_runtime_ms * RUNTIME_LIMIT_FRACTION
                if estimate_ms > limit_ms:
                    reasons.append(
                        f"{pump}: geschätzte Laufzeit {estimate_ms:.0f} ms je Chunk überschreitet "
                        f"{limit_ms:.0f} ms ({RUNTIME_LIMIT_FRACTION:.0%} der Firmware-Maximallaufzeit)."
                    )
    return reasons


def build_run_plan(settings: dict[str, Any]) -> NutrientDosingPlan:
    """Dosierplan für einen Mischlauf aus dem Rezept-Snapshot der Einstellungen
    plus Mapping und bestätigtem Firmwareprofil. Liest nur Konfigurationsdateien.
    Wirft ValueError mit lesbarer Meldung bei jedem Problem (fail-closed)."""
    try:
        snapshot = settings["recipe_run_config_snapshot"]
        mapping = load_mapping(MAPPING_PATH)
        profiles = load_firmware_profiles(FIRMWARE_PROFILES_PATH)
        entry = mapping.controllers[CONTROLLER]
        profile = profiles.controllers[CONTROLLER]
        weights = {pump: resolve_firmware_ml_per_step(profiles, CONTROLLER, pump) for pump in VALID_PUMPS}
        roles = {pump: entry.pumps.get(pump) for pump in VALID_PUMPS}
        ro_l = snapshot["target_ro_water_l"]
        prefill = min(float(settings.get("nutrient_prefill_liters", NUTRIENT_PREFILL_LITERS)), ro_l * NUTRIENT_PREFILL_MAX_FRACTION)
        return build_nutrient_dosing_plan(
            snapshot,
            pump_roles=roles,
            pump_weights=weights,
            profile_id=profile.profile_id,
            max_chunk_ml=NUTRIENT_MAX_DOSE_ML,
            prefill_target_l=prefill,
        )
    except (MappingValidationError, FirmwareProfileError, NutrientPlanError, KeyError, TypeError) as exc:
        raise ValueError(f"Dosierplan nicht erstellbar: {type(exc).__name__}: {exc}") from exc


@dataclass
class PumpProgress:
    pump: str
    role: str
    target_ul: int
    chunks_total: int
    chunks_done: int = 0
    confirmed_ul: int = 0
    uncertain_ul: int = 0


class NutrientDosingController(BackgroundHardwareProcess):
    label = "Nutrient Dosing"
    # Stop wird innerhalb eines Takts (0,1 s) plus STOPALL-Antwort erkannt.
    stop_join_timeout_seconds = 10.0

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
        serial_factory: SerialFactory = default_serial_factory,
        session_log_dir: Path = Path("logs/peristaltic"),
    ) -> None:
        super().__init__(get_sensor_snapshot)
        self._serial_factory = serial_factory
        self._session_log_dir = session_log_dir

        self._plan: NutrientDosingPlan | None = None
        self._entry: ControllerMapping | None = None
        self._profile: ControllerFirmwareProfile | None = None
        self._session_id: str | None = None
        self._phase: str = DosingPhase.IDLE
        self._progress: dict[str, PumpProgress] = {}
        self._current_pump: str | None = None
        self._completion_reason: str | None = None
        self._chunk_started = False
        # Einmalnutzung je Plan: ein bereits gestarteter Plan (auch abgebrochen/fehlgeschlagen)
        # wird nie erneut dosiert. Nur im Speicher; ein neuer Mischvorgang erzeugt einen neuen Plan.
        self._started_plan_hashes: set[str] = set()

    # ---- Start ----------------------------------------------------------

    def _load_and_check(self, plan: Any) -> str | None:
        """Mapping/Profil/Kalibrierung/Port/Plan prüfen; setzt _entry/_profile."""
        try:
            mapping = load_mapping(MAPPING_PATH)
        except MappingValidationError as exc:
            return f"Dosierung blockiert: Mapping ungültig oder nicht gefunden: {exc}"
        try:
            profiles = load_firmware_profiles(FIRMWARE_PROFILES_PATH)
        except FirmwareProfileError as exc:
            return f"Dosierung blockiert: Firmwareprofil ungültig oder nicht gefunden: {exc}"
        try:
            calibration_data = load_calibration_data(CALIBRATION_DATA_PATH)
        except CalibrationValidationError as exc:
            return f"Dosierung blockiert: Kalibrierdaten ungültig oder nicht gefunden: {exc}"

        entry = mapping.controllers[CONTROLLER]
        profile = profiles.controllers[CONTROLLER]
        if not entry.port:
            return f"Dosierung blockiert: kein Port für {CONTROLLER} zugeordnet."
        if not Path(entry.port).exists():
            return f"Dosierung blockiert: Port {entry.port} nicht gefunden."

        # Plan ohne Dosierarbeit/falschen Typ vor weiteren Profilzugriffen abweisen.
        firmware_values: dict[str, float] = {}
        if isinstance(plan, NutrientDosingPlan):
            for target in plan.pump_targets:
                if target.pump not in VALID_PUMPS:
                    continue
                try:
                    firmware_values[target.pump] = resolve_firmware_ml_per_step(profiles, CONTROLLER, target.pump)
                except FirmwareProfileError:
                    return f"Dosierung blockiert: Firmwareprofil für {CONTROLLER}/{target.pump} nicht bestätigt."
                if not has_volumetric_calibration(calibration_data, CONTROLLER, target.pump):
                    return f"Dosierung blockiert: keine volumetrische Kalibrierung für {CONTROLLER}/{target.pump}."
            if profile.speed_rpm is None or profile.microsteps is None or profile.max_runtime_ms is None:
                return "Dosierung blockiert: Firmwareprofil ohne speed_rpm/microsteps/max_runtime_ms."

        reasons = plan_blocking_reasons(plan, entry, profile, firmware_values)
        if reasons:
            return "Dosierung blockiert: " + " ".join(reasons)

        self._entry = entry
        self._profile = profile
        return None

    def check_plan(self, plan: Any) -> str | None:
        """Vorabprüfung ohne Start und ohne seriellen Zugriff (Mapping, Profil,
        Kalibrierung, Port-Datei, Plan). None = ausführbar, sonst Blockgrund."""
        with self._lock:
            return self._load_and_check(plan)

    def start(self, settings: dict[str, Any], plan: NutrientDosingPlan) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Dosierung läuft bereits.")

            # Verpflichtende Prozess-Gates der Basisklasse (hardware_execution_enabled,
            # Sensor-Snapshot) - keine Umgehung.
            block_message = self._check_start_preconditions_locked(settings)
            if block_message is None:
                block_message = self._load_and_check(plan)
            if block_message is not None:
                self._last_message = block_message
                return self._result(False, block_message)

            if plan.plan_hash in self._started_plan_hashes:
                message = (
                    "Dosierung blockiert: dieser Plan wurde bereits gestartet. Eine neue Dosierung "
                    "erfordert einen neu gestarteten Mischvorgang mit neuem Plan."
                )
                self._last_message = message
                return self._result(False, message)
            self._started_plan_hashes.add(plan.plan_hash)

            self._plan = plan
            self._session_id = f"nutrient_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:8]}"
            self._progress = {
                target.pump: PumpProgress(target.pump, target.role, target.target_ul, target.chunk_count)
                for target in sorted(plan.pump_targets, key=lambda t: _PUMP_ORDER[t.pump])
            }
            self._current_pump = None
            self._completion_reason = None
            self._phase = DosingPhase.RUNNING
            self._last_message = "Nährstoffdosierung gestartet."
            self._begin_run_locked("cds-nutrient-dosing", settings)

        return self._result(True, "Nährstoffdosierung gestartet.")

    def request_stop(self, reason: str = "stopped_by_user") -> None:
        with self._lock:
            if self.is_active():
                self._phase = DosingPhase.STOPPING
        super().request_stop(reason)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            progress = [
                {
                    "pump": p.pump,
                    "role": p.role,
                    "target_ul": p.target_ul,
                    "chunks_total": p.chunks_total,
                    "chunks_done": p.chunks_done,
                    "confirmed_ul": p.confirmed_ul,
                    "uncertain_ul": p.uncertain_ul,
                }
                for p in self._progress.values()
            ]
            return {
                "is_active": self._thread is not None and self._thread.is_alive(),
                "phase": self._phase,
                "session_id": self._session_id,
                "current_pump": self._current_pump,
                "pumps": progress,
                "confirmed_ul": sum(p.confirmed_ul for p in self._progress.values()),
                "uncertain_ul": sum(p.uncertain_ul for p in self._progress.values()),
                "target_ul": sum(p.target_ul for p in self._progress.values()),
                "completion_reason": self._completion_reason,
                "plan_hash": self._plan.plan_hash if self._plan is not None else None,
                "started_at": self._started_at_iso if self._thread is not None and self._thread.is_alive() else None,
                "stop_reason": self._stop_reason,
                "last_error": self._last_error,
                "last_message": self._last_message,
            }

    # ---- Hintergrund-Thread --------------------------------------------

    def _stop_completion_reason(self) -> str:
        return "emergency_stop" if self._stop_reason == "emergency_stop" else "user_abort"

    def _dose_chunk(
        self,
        client: PeristalticSerialClient,
        pump: str,
        ml: float,
        wait_timeout_s: float,
    ) -> float:
        """Ein DOSE-Befehl bis DONE + IDLE; gibt die gemeldete Menge zurück.
        Wirft bei jeder Abweichung (nie ein stilles Weitermachen). Der Aufrufer
        wertet einen Wurf nach dem Start als unklaren Chunk."""
        client.start_dose(pump, ml)  # Wurf hier: siehe _run (Command/Desync = nichts gefördert)
        self._chunk_started = True

        deadline = time.monotonic() + wait_timeout_s
        reported_ml: float | None = None
        while reported_ml is None:
            if self._stop_event.is_set():
                best_effort_stop_all(client)
                raise _StopRequested()
            if time.monotonic() > deadline:
                best_effort_stop_all(client)
                raise PeristalticTimeoutError(f"Keine DONE/TIMEOUT-Antwort für {pump} innerhalb von {wait_timeout_s}s.")
            event = client.poll_pump_event(pump, POLL_INTERVAL_S)
            if event is None or event.kind is LineKind.RUNNING:
                continue
            if event.kind is LineKind.DONE:
                reported_ml = event.ml if event.ml is not None else -1.0
            elif event.kind is LineKind.ERR_TIMEOUT:
                raise PeristalticCommandError(
                    f"Firmware-Timeout während Dosis auf {pump}: {event.raw!r}",
                    pump=pump, kind=event.kind, raw=event.raw,
                )
            else:
                raise PeristalticProtocolError(f"Unerwartete Antwort während Dosis auf {pump}: {event.raw!r}")

        if abs(reported_ml - ml) > DONE_ML_TOLERANCE:
            raise SafetyCheckFailed(f"{pump}: DONE meldet {reported_ml} ml, angefordert {ml} ml.")
        status = client.status()
        if status.get(pump) != "IDLE":
            raise SafetyCheckFailed(f"{pump} ist nach DONE nicht IDLE (STATUS: {status}).")
        return reported_ml

    def _run(self, settings: dict[str, Any]) -> None:
        plan, entry, profile = self._plan, self._entry, self._profile
        assert plan is not None and entry is not None and profile is not None
        reason = "completed"
        error: str | None = None
        client: PeristalticSerialClient | None = None
        logger = PeristalticSessionLogger("nutrient_dose", log_dir=self._session_log_dir, session_id=self._session_id)
        wait_timeout_s = profile.max_runtime_ms / 1000.0 + WAIT_MARGIN_S

        def log(pump: str, **fields: Any) -> None:
            logger.log_row(
                controller=CONTROLLER, controller_role=entry.role, serial_port=entry.port,
                pump=pump, pump_role=entry.pumps.get(pump), command="nutrient_dose",
                operation="nutrient_dose", **fields,
            )

        try:
            client = PeristalticSerialClient(
                entry.port,
                config=ClientConfig(baudrate=entry.baudrate, dose_wait_timeout_s=wait_timeout_s),
                serial_factory=self._serial_factory,
                on_raw_line=logger.log_raw,
            )
            client.open()
            ensure_all_idle_before_dose(client)

            for target in sorted(plan.pump_targets, key=lambda t: _PUMP_ORDER[t.pump]):
                pump = target.pump
                with self._lock:
                    self._current_pump = pump
                    self._last_message = f"Dosierung läuft: {CONTROLLER}/{pump} ({target.role})."
                for chunk in target.chunks():
                    if self._stop_event.is_set():
                        raise _StopRequested()
                    started_at, t0 = _iso_now(), time.monotonic()
                    self._chunk_started = False
                    try:
                        reported = self._dose_chunk(client, pump, chunk.amount_ml, wait_timeout_s)
                    except (PeristalticCommandError, PeristalticDesyncError):
                        # Firmware hat abgelehnt bzw. nichts wurde geschrieben, solange der
                        # Befehl nicht gestartet war; danach (Timeout-ERR etc.) ist es unklar.
                        if self._chunk_started:
                            self._mark_uncertain(pump, chunk.amount_ul)
                        raise
                    except BaseException:
                        self._mark_uncertain(pump, chunk.amount_ul)
                        raise
                    with self._lock:
                        progress = self._progress[pump]
                        progress.chunks_done += 1
                        progress.confirmed_ul += chunk.amount_ul
                    log(pump, requested_ml=chunk.amount_ml, started_at=started_at, finished_at=_iso_now(),
                        elapsed_seconds=time.monotonic() - t0, result="DONE", firmware_reported_ml=reported,
                        completed_ml=self._progress[pump].confirmed_ul / 1000.0)
        except _StopRequested:
            reason = self._stop_completion_reason()
        except _GUARDED_EXCEPTIONS as exc:
            reason = classify_guarded_exception(exc)
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # unerwartet: fail-closed, nie weiterdosieren
            reason = "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if client is not None:
                best_effort_stop_all(client)
                client.close()
            with self._lock:
                pump_now = self._current_pump
            if reason != "completed" and pump_now is not None:
                log(pump_now, requested_ml=None, finished_at=_iso_now(), result="ABORTED" if error is None else "FAILED",
                    error_code=None if error is None else error.split(":", 1)[0], notes=error,
                    completion_reason=reason)
            logger.close({"command": "nutrient_dose", "controller": CONTROLLER, "plan_hash": plan.plan_hash,
                          "completion_reason": reason})
            with self._lock:
                self._current_pump = None
                self._completion_reason = reason
                if reason == "completed":
                    self._phase = DosingPhase.FINISHED
                    self._stop_reason = "completed"
                    self._last_message = "Nährstoffdosierung abgeschlossen."
                elif error is None:
                    self._phase = DosingPhase.ABORTED
                    self._stop_reason = self._stop_reason or "stopped_by_user"
                    self._last_message = f"Nährstoffdosierung abgebrochen: {reason}."
                else:
                    self._phase = DosingPhase.ERROR
                    self._last_error = error
                    self._last_message = f"Nährstoffdosierung fehlgeschlagen: {error}"

    def _mark_uncertain(self, pump: str, amount_ul: int) -> None:
        with self._lock:
            self._progress[pump].uncertain_ul += amount_ul
