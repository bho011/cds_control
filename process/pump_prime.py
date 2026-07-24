"""
"Prime" (Entlüften) mehrerer Peristaltikpumpen aus dem NiceGUI-Dashboard.

Erster Bridge zwischen der bisher komplett eigenständigen
Peristaltik-Servicearchitektur (services/peristaltic/*, bisher nur über
scripts/peristaltic_calibration_cli.py nutzbar) und dem Dashboard/
ProcessController. Baut auf BackgroundHardwareProcess auf (derselben
Basisklasse wie ManualDrainJog/TankCleaningController), damit Start/Stopp/
Doppelstart-Schutz/Emergency-Stop-Anbindung identisch zu den bestehenden
Wartungsfunktionen funktionieren.

Ausführungsmodell (bewusst vollständig sequenziell, siehe Plan/
Abschlussbericht): genau EIN Hintergrund-Thread arbeitet MCU für MCU (nur
MCUs mit mindestens einer ausgewählten Pumpe) und darin Pumpe für Pumpe
komplett nacheinander ab. Kein neues Parallelitäts-Muster - jede Pumpe
läuft für sich über services.peristaltic.prime.prime_single_pump(), exakt
der bereits getestete Ablauf aus dem CLI-Kommando 'prime'.

hardware_execution_enabled/Sensor-Snapshot (die Sicherheitsgates der
Basisklasse) sind für Peristaltikpumpen bewusst IRRELEVANT - eine
eigenständige Architekturentscheidung, kein versehentliches Umgehen einer
Sicherheitsprüfung: Prime dosiert nur 150 ml Wasser zu Wartungszwecken,
nicht den vollständigen automatischen CDS-Prozess, und soll auch während
Entwicklung/Kalibrierung nutzbar bleiben, ohne dafür die globale
Prozess-Hardwarefreigabe zu aktivieren. _check_start_preconditions_locked()
wird deshalb komplett überschrieben (kein super()-Aufruf) und prüft
stattdessen vier eigene, unabhängige Prime-spezifische Gates: Mapping,
Verbindungsverfügbarkeit, Firmware-/Protokollprofil, volumetrische
Kalibrierung. hardware_execution_enabled bleibt davon unberührt - andere
Prozesse (Fill-and-Measure/Manual Drain/Tank Cleaning) bleiben weiterhin
strikt davon abhängig.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.peristaltic.calibration import (
    DEFAULT_PRIMING_CHUNK_ML,
    CalibrationValidationError,
    has_volumetric_calibration,
    load_calibration_data,
)
from services.peristaltic.firmware_profiles import (
    FirmwareProfileError,
    load_firmware_profiles,
    resolve_firmware_ml_per_step,
)
from services.peristaltic.models import (
    UNASSIGNED,
    VALID_CONTROLLERS,
    VALID_PUMPS,
    ControllerMapping,
    MappingValidationError,
    load_mapping,
)
from services.peristaltic.prime import PrimePumpOutcome, classify_guarded_exception, prime_single_pump
from services.peristaltic.safety import best_effort_stop_all, ensure_all_idle_before_dose
from services.peristaltic.serial_client import (
    ClientConfig,
    PeristalticSerialClient,
    SerialFactory,
    default_serial_factory,
)
from services.peristaltic.session_log import PeristalticSessionLogger

from .background_process import BackgroundHardwareProcess

MAPPING_PATH = Path("config/peristaltic_mapping.json")
FIRMWARE_PROFILES_PATH = Path("config/peristaltic_firmware_profiles.json")
CALIBRATION_DATA_PATH = Path("calibration_data/peristaltic_calibration.json")

# Feste Prime-Menge je ausgewählter Pumpe - nicht durch den Benutzer
# veränderbar (siehe Aufgabenstellung). Gilt gleichermaßen für MCU_A und
# MCU_B; validate_priming_request() (services/peristaltic/calibration.py)
# erlaubt bis zu MAX_PRIMING_TOTAL_ML=200 ml, 150 ml braucht daher keine
# zusätzliche Mengenfreigabe je Pumpentyp.
PRIME_MAX_ML_PER_PUMP = 150.0
PRIME_CHUNK_ML = DEFAULT_PRIMING_CHUNK_ML

# Diese drei completion_reason-Werte gelten als "kein Fehler" (bewusster
# Stopp bzw. Erfolg) - alles andere (connection_error/safety_check_failed/
# timeout/error) führt zu PrimePhase.ERROR.
_NON_ERROR_COMPLETION_REASONS = frozenset({"completed", "user_abort", "emergency_stop"})


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PrimePhase:
    """Menschenlesbare Zustände, auch im Status-Dict verwendet."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


def _pump_blocking_reasons(
    controller_id: str,
    pump: str,
    entry: ControllerMapping,
    profiles,
    profiles_error: str | None,
    calibration_data: dict[str, Any] | None,
    calibration_error: str | None,
) -> list[str]:
    """Prüft die drei restlichen Gates (Verbindung/Firmwareprofil/
    volumetrische Kalibrierung) unabhängig voneinander für eine bereits
    gemappte (nicht UNASSIGNED) Pumpe - sammelt ALLE zutreffenden Gründe,
    nicht nur den ersten."""
    reasons: list[str] = []

    if not entry.port:
        reasons.append(f"Verbindung zu {controller_id} ist nicht verfügbar (kein Port zugeordnet).")
    elif not Path(entry.port).exists():
        reasons.append(
            f"Verbindung zu {controller_id} ist derzeit nicht verfügbar (Port {entry.port} nicht gefunden)."
        )

    if profiles is None:
        reasons.append(f"Firmware-/Protokollprofil nicht verfügbar: {profiles_error}")
    else:
        try:
            resolve_firmware_ml_per_step(profiles, controller_id, pump)
        except FirmwareProfileError:
            reasons.append(f"Firmware-/Protokollprofil für {controller_id} / {pump} ist nicht sicher bestätigt.")

    if calibration_data is None:
        reasons.append(f"Volumetrische Kalibrierung nicht verfügbar: {calibration_error}")
    elif not has_volumetric_calibration(calibration_data, controller_id, pump):
        reasons.append(f"Für {controller_id} / {pump} liegt keine volumetrische Kalibrierung vor.")

    return reasons


def describe_pump_options() -> dict[tuple[str, str], dict[str, Any]]:
    """
    Liest Mapping, Firmwareprofile und Kalibrierdaten (read-only, keine
    Hardware - insbesondere KEIN PeristalticSerialClient, kein Port-Öffnen,
    kein PING/STATUS/STOPALL/DOSE) und beschreibt für jede der 8
    (controller, pump)-Kombinationen, ob sie im Prime-Dialog anzeigbar/
    auswählbar ist. Wirft NIE - eine kaputte/fehlende Datei führt zu einem
    controllerweiten Grund in blocked_reasons, nie zu einem stillen Default.

    Rückgabe je Kombination: {"role", "hidden", "blocked", "blocked_reasons"}
    - hidden=True: Pumpe hat keine fachliche Rolle (UNASSIGNED) - im
      Dialog ausblenden, nicht nur deaktivieren.
    - blocked_reasons: Liste ALLER zutreffenden Gründe (Mapping/Verbindung/
      Firmwareprofil/Kalibrierung sind vier unabhängige Gates - können auch
      gleichzeitig zutreffen, werden nie gegeneinander ersetzt).
    """
    result: dict[tuple[str, str], dict[str, Any]] = {}

    try:
        mapping = load_mapping(MAPPING_PATH)
        mapping_error: str | None = None
    except MappingValidationError as exc:
        mapping = None
        mapping_error = str(exc)

    try:
        profiles = load_firmware_profiles(FIRMWARE_PROFILES_PATH)
        profiles_error: str | None = None
    except FirmwareProfileError as exc:
        profiles = None
        profiles_error = str(exc)

    try:
        calibration_data = load_calibration_data(CALIBRATION_DATA_PATH)
        calibration_error: str | None = None
    except CalibrationValidationError as exc:
        calibration_data = None
        calibration_error = str(exc)

    for controller_id in VALID_CONTROLLERS:
        for pump in VALID_PUMPS:
            if mapping is None:
                result[(controller_id, pump)] = {
                    "role": None,
                    "hidden": False,
                    "blocked": True,
                    "blocked_reasons": [f"Mapping nicht verfügbar: {mapping_error}"],
                }
                continue

            entry = mapping.controllers[controller_id]
            role = entry.pumps[pump]

            if role == UNASSIGNED:
                result[(controller_id, pump)] = {
                    "role": role,
                    "hidden": True,
                    "blocked": True,
                    "blocked_reasons": [],
                }
                continue

            reasons = _pump_blocking_reasons(
                controller_id, pump, entry, profiles, profiles_error, calibration_data, calibration_error
            )
            result[(controller_id, pump)] = {
                "role": role,
                "hidden": False,
                "blocked": bool(reasons),
                "blocked_reasons": reasons,
            }

    return result


class PumpPrimeController(BackgroundHardwareProcess):
    """
    Server-side Controller für die "Prime"-Wartungsfunktion.

    settings["pumps"] ist KEIN aus einer JSON-Settings-Datei geladenes
    Dict wie bei ManualDrainJog/TankCleaningController, sondern direkt aus
    der Checkbox-Auswahl im Dashboard gebaut:
    {"MCU_A": ["P1"], "MCU_B": ["P1", "P2", "P3", "P4"]}.
    """

    label = "Prime"
    # Ein einzelner Chunk kann intern bis zu ~35s auf DONE warten
    # (PeristalticSerialClient.ClientConfig.dose_wait_timeout_s) - der
    # Stop-Check erfolgt nur zwischen Chunks/Pumpen/MCUs, daher ein
    # deutlich höherer Join-Timeout als bei den anderen Prozessen.
    stop_join_timeout_seconds = 40.0

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
        serial_factory: SerialFactory = default_serial_factory,
        session_log_dir: Path = Path("logs/peristaltic"),
    ) -> None:
        # get_sensor_snapshot wird nur wegen der Basisklassen-Signatur
        # angenommen - Peristaltik-Hardware hat nichts mit Sensor-Snapshots
        # zu tun und die geerbte _check_start_preconditions_locked() wird
        # unten komplett überschrieben, statt sie zu verwenden.
        super().__init__(get_sensor_snapshot)

        self._serial_factory = serial_factory
        # Injectable wie serial_factory - Tests leiten dies auf tmp_path um,
        # damit Testläufe nie echte Dateien unter logs/peristaltic/ hinterlassen.
        self._session_log_dir = session_log_dir

        self._pumps_plan: dict[str, list[str]] = {}
        self._max_ml_per_pump: float = PRIME_MAX_ML_PER_PUMP
        self._chunk_ml: float = PRIME_CHUNK_ML
        self._session_id: str | None = None

        self._phase: str = PrimePhase.IDLE
        self._current_controller: str | None = None
        self._current_pump: str | None = None
        self._current_pump_role: str | None = None
        self._pump_results: list[PrimePumpOutcome] = []

    # ---- public dashboard API -----------------------------------------

    def _check_start_preconditions_locked(self, settings: dict[str, Any]) -> str | None:
        """
        Prime-eigene Sicherheitsgates - bewusst OHNE hardware_execution_enabled
        (siehe Modul-Docstring). Vier unabhängige Prüfungen je ausgewählter
        Pumpe: Mapping-Gültigkeit, Verbindungsverfügbarkeit, Firmware-/
        Protokollprofil, volumetrische Kalibrierung. Gibt beim ersten
        Fehlschlag die passende Meldung zurück (die vollständige Liste
        aller zutreffenden Gründe sieht der Nutzer vorab über
        describe_pump_options() im Dialog).
        """
        pumps: dict[str, list[str]] = settings.get("pumps") or {}
        if not any(pump_list for pump_list in pumps.values()):
            return "Bitte mindestens eine Pumpe auswählen."

        try:
            mapping = load_mapping(MAPPING_PATH)
        except MappingValidationError as exc:
            return f"Prime blockiert: Mapping ungültig oder nicht gefunden: {exc}"

        try:
            profiles = load_firmware_profiles(FIRMWARE_PROFILES_PATH)
        except FirmwareProfileError as exc:
            return f"Prime blockiert: Firmware-/Protokollprofil ungültig oder nicht gefunden: {exc}"

        try:
            calibration_data = load_calibration_data(CALIBRATION_DATA_PATH)
        except CalibrationValidationError as exc:
            return f"Prime blockiert: Kalibrierdaten ungültig oder nicht gefunden: {exc}"

        for controller_id, pump_list in pumps.items():
            if controller_id not in VALID_CONTROLLERS:
                return f"Prime blockiert: unbekannter Controller {controller_id!r}."

            entry = mapping.controllers[controller_id]

            for pump in pump_list:
                if pump not in VALID_PUMPS:
                    return f"Prime blockiert: unbekannte Pumpe {pump!r} an {controller_id}."

                role = entry.pumps[pump]
                if role == UNASSIGNED:
                    return f"Für {controller_id} / {pump} liegt keine eindeutige Pumpenzuordnung vor."

                if not entry.port:
                    return f"Verbindung zu {controller_id} ist nicht verfügbar (kein Port zugeordnet)."
                if not Path(entry.port).exists():
                    return (
                        f"Verbindung zu {controller_id} ist derzeit nicht verfügbar "
                        f"(Port {entry.port} nicht gefunden)."
                    )

                try:
                    resolve_firmware_ml_per_step(profiles, controller_id, pump)
                except FirmwareProfileError:
                    return f"Firmware-/Protokollprofil für {controller_id} / {pump} ist nicht sicher bestätigt."

                if not has_volumetric_calibration(calibration_data, controller_id, pump):
                    return f"Für {controller_id} / {pump} liegt keine volumetrische Kalibrierung vor."

        return None

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.is_active():
                return self._result(True, "Prime läuft bereits.")

            block_message = self._check_start_preconditions_locked(settings)
            if block_message is not None:
                return self._result(False, block_message)

            self._pumps_plan = {
                controller_id: list(pump_list)
                for controller_id, pump_list in (settings.get("pumps") or {}).items()
                if pump_list
            }
            self._max_ml_per_pump = float(settings.get("max_ml_per_pump", PRIME_MAX_ML_PER_PUMP))
            self._chunk_ml = float(settings.get("chunk_ml", PRIME_CHUNK_ML))
            # Kollisionssicher (Mikrosekunden + kurzes UUID-Suffix, nicht nur
            # sekundengenauer Zeitstempel) - eine session_id pro Prime-Lauf,
            # gemeinsam für ALLE in diesem Lauf beteiligten MCU-Logger.
            self._session_id = (
                f"prime_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:8]}"
            )
            self._current_controller = None
            self._current_pump = None
            self._current_pump_role = None
            self._pump_results = []
            self._phase = PrimePhase.RUNNING
            self._last_message = "Priming gestartet."

            self._begin_run_locked("cds-pump-prime", settings)

        return self._result(True, "Priming gestartet.")

    def request_stop(self, reason: str = "stopped_by_user") -> None:
        with self._lock:
            if self.is_active():
                self._phase = PrimePhase.STOPPING
        super().request_stop(reason)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            is_active = self._thread is not None and self._thread.is_alive()
            return {
                "is_active": is_active,
                "phase": self._phase,
                "session_id": self._session_id,
                "current_controller": self._current_controller,
                "current_pump": self._current_pump,
                "current_pump_role": self._current_pump_role,
                "pumps_plan": {k: list(v) for k, v in self._pumps_plan.items()},
                "pump_results": [asdict(outcome) for outcome in self._pump_results],
                "max_ml_per_pump": self._max_ml_per_pump,
                "chunk_ml": self._chunk_ml,
                "started_at": self._started_at_iso if is_active else None,
                "stop_reason": self._stop_reason,
                "last_error": self._last_error,
                "last_message": self._last_message,
            }

    # ---- background thread ---------------------------------------------

    def _log_mcu_level_failure(
        self,
        session_logger: PeristalticSessionLogger,
        controller_id: str,
        entry: ControllerMapping | None,
        pumps: list[str],
        completion_reason: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """
        Garantiert einen terminalen Logeintrag + PrimePumpOutcome für jede
        an diesem Controller ausgewählte, aber "tatsächlich begonnene"
        Pumpe, wenn der Fehler VOR jeder einzelnen prime_single_pump()-
        Ausführung auftrat (client.open()/ensure_all_idle_before_dose()/
        Mapping-Reload fehlgeschlagen) - sonst gäbe es für diese Pumpen gar
        keinen Logeintrag (die Lücke, die Punkt 7 schließt).
        """
        for pump in pumps:
            role = entry.pumps.get(pump) if entry is not None else None
            session_logger.log_row(
                controller=controller_id,
                controller_role=(entry.role if entry is not None else None),
                serial_port=(entry.port if entry is not None else None),
                pump=pump,
                pump_role=role,
                command="prime",
                operation="prime",
                requested_max_ml=self._max_ml_per_pump,
                chunk_ml=self._chunk_ml,
                requested_ml=None,
                started_at=None,
                finished_at=_iso_now(),
                elapsed_seconds=None,
                result="FAILED",
                error_code=error_code,
                notes=error_message,
                completed_ml=0.0,
                completion_reason=completion_reason,
            )
            outcome = PrimePumpOutcome(
                controller=controller_id,
                pump=pump,
                role=role or "",
                requested_max_ml=self._max_ml_per_pump,
                chunk_ml=self._chunk_ml,
                chunks_total=0,
                chunks_completed=0,
                completed_ml=0.0,
                completion_reason=completion_reason,
                error_code=error_code,
                error_message=error_message,
            )
            with self._lock:
                self._pump_results.append(outcome)

    def _run(self, settings: dict[str, Any]) -> None:
        pumps_plan = self._pumps_plan
        max_ml = self._max_ml_per_pump
        chunk_ml = self._chunk_ml
        session_id = self._session_id
        had_error = False

        try:
            for controller_id in VALID_CONTROLLERS:
                pump_list = pumps_plan.get(controller_id) or []
                if not pump_list or self._stop_event.is_set():
                    continue

                with self._lock:
                    self._current_controller = controller_id
                    self._last_message = f"Verbinde mit {controller_id}."

                session_logger = PeristalticSessionLogger(
                    "prime", log_dir=self._session_log_dir, session_id=session_id
                )
                client: PeristalticSerialClient | None = None
                entry: ControllerMapping | None = None
                mcu_failure_exc: Exception | None = None

                try:
                    mapping = load_mapping(MAPPING_PATH)
                    entry = mapping.controllers[controller_id]

                    client = PeristalticSerialClient(
                        entry.port,
                        config=ClientConfig(baudrate=entry.baudrate),
                        serial_factory=self._serial_factory,
                        on_raw_line=session_logger.log_raw,
                    )

                    client.open()
                    ensure_all_idle_before_dose(client)

                    for pump in pump_list:
                        if self._stop_event.is_set():
                            break

                        role = entry.pumps[pump]
                        with self._lock:
                            self._current_pump = pump
                            self._current_pump_role = role
                            self._last_message = f"Prime läuft: {controller_id}/{pump} ({role})."

                        outcome = prime_single_pump(
                            client,
                            session_logger,
                            controller_id=controller_id,
                            controller_role=entry.role,
                            port=entry.port,
                            pump=pump,
                            role=role,
                            max_ml=max_ml,
                            chunk_ml=chunk_ml,
                            stop_event=self._stop_event,
                            # Callable statt fixem String: der Stop-Grund
                            # (self._stop_reason) kann sich erst NACH dem
                            # Start dieses Aufrufs ändern (z.B. Emergency
                            # Stop mitten in einem laufenden Chunk) - ein
                            # vorab ausgewerteter String würde das nicht
                            # mehr erfassen können.
                            abort_completion_reason=lambda: (
                                "emergency_stop" if self._stop_reason == "emergency_stop" else "user_abort"
                            ),
                        )

                        with self._lock:
                            self._pump_results.append(outcome)

                        if outcome.completion_reason not in _NON_ERROR_COMPLETION_REASONS:
                            had_error = True
                            with self._lock:
                                self._last_error = outcome.error_message
                            break
                        if outcome.completion_reason in ("user_abort", "emergency_stop"):
                            break

                except Exception as exc:
                    mcu_failure_exc = exc
                    had_error = True
                    with self._lock:
                        self._last_error = f"Prime auf {controller_id} fehlgeschlagen: {exc}"
                finally:
                    if mcu_failure_exc is not None:
                        already_logged = {
                            outcome.pump for outcome in self._pump_results if outcome.controller == controller_id
                        }
                        remaining_pumps = [pump for pump in pump_list if pump not in already_logged]
                        if remaining_pumps:
                            self._log_mcu_level_failure(
                                session_logger,
                                controller_id,
                                entry,
                                remaining_pumps,
                                classify_guarded_exception(mcu_failure_exc),
                                type(mcu_failure_exc).__name__,
                                str(mcu_failure_exc),
                            )

                    if client is not None:
                        best_effort_stop_all(client)
                        client.close()
                    session_logger.close(
                        {"command": "prime", "controller": controller_id, "pumps": pump_list}
                    )

                if had_error or self._stop_event.is_set():
                    break

        except Exception as exc:
            had_error = True
            with self._lock:
                self._last_error = str(exc)

        finally:
            with self._lock:
                self._current_controller = None
                self._current_pump = None
                self._current_pump_role = None

                if had_error:
                    self._phase = PrimePhase.ERROR
                    self._last_message = self._last_error or "Priming fehlgeschlagen."
                elif self._stop_event.is_set():
                    self._phase = PrimePhase.IDLE
                    self._stop_reason = self._stop_reason or "stopped_by_user"
                    self._last_message = f"Priming gestoppt: {self._stop_reason}."
                else:
                    self._stop_reason = "completed"
                    self._phase = PrimePhase.FINISHED
                    self._last_message = "Priming abgeschlossen."
