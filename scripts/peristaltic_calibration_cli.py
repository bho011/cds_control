#!/usr/bin/env python3
"""
Eigenständiges, terminalbasiertes Test-, Mapping- und Kalibrierprogramm für
die beiden Peristaltik-Controller (MCU_A/MCU_B).

Keine Integration in Dashboard, ProcessController oder State Machine. Keine
automatische Chemikaliendosierung - dieses Programm wird ausschließlich mit
Wasser verwendet. Siehe docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md für den
vollständigen Ablauf und die Sicherheitsregeln.

Unterbefehle: discover, map, check, stop-all, test, calibrate, prime,
pair-test, all-four-test.

Modularisierungs-Hinweis (Phase 2b): nur 'discover' (scripts/peristaltic/
discover.py) ist ausgelagert. Alle anderen Unterbefehle - inkl. der
gemeinsamen Helfer _load_mapping_or_exit/_resolve_controller/_make_client/
_resolve_firmware_value_or_exit - bleiben bewusst HIER, in dieser Datei,
zusammen mit den Modul-Konstanten MAPPING_PATH/CALIBRATION_DATA_PATH/
FIRMWARE_PROFILES_PATH und dem Namen PeristalticSerialClient:
tests/test_peristaltic_calibration_cli.py lädt dieses Skript per
importlib direkt (kein normaler Package-Import) und patcht genau diese
Modul-Attribute auf dem geladenen Modul-Objekt (z.B. "cli.MAPPING_PATH =
tmp_path / ..."), um cmd_calibrate ohne echte Dateien/Hardware fail-closed
zu testen. Ein Python-Funktionskörper liest ein globales Attribut immer aus
dem Modul, in dem er DEFINIERT wurde - nicht aus dem Modul, aus dem er
später importiert wird. Würde man cmd_calibrate/cmd_map/die genannten
Helfer in eine Unterdatei verschieben, würde das Patchen dieser Tests
still und leise wirkungslos werden (sie würden echte Pfade/echte Hardware-
Objekte sehen statt der Fake-Werte). Ein Umbau auf explizite Parameter
statt Modul-globaler Werte wäre möglich, ist aber kein reines Verschieben
mehr und deshalb nicht Teil dieses Schritts - siehe Modularisierungs-Plan,
Entscheidungsprotokoll Phase 2b.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Erlaubt den Direktaufruf "python scripts/peristaltic_calibration_cli.py ..."
# aus dem Projekt-Root heraus - das Skript liegt in scripts/, die Pakete
# (services/, scripts.peristaltic/) aber eine Ebene höher bzw. daneben.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.peristaltic.calibration import (
    DEFAULT_PRIMING_CHUNK_ML,
    MAX_INITIAL_TEST_DOSE_ML,
    MAX_PRIMING_TOTAL_ML,
    CalibrationValidationError,
    add_trial,
    compute_priming_chunks,
    default_calibration_data,
    load_calibration_data,
    save_calibration_data,
    validate_dose_ml,
    validate_parallel_pump_selection,
    validate_priming_request,
)
from services.peristaltic.firmware_profiles import (
    FirmwareProfileError,
    load_firmware_profiles,
    resolve_firmware_ml_per_step,
)
from services.peristaltic.models import (
    VALID_CONTROLLERS,
    VALID_PUMPS,
    ControllerMapping,
    LineKind,
    MappingValidationError,
    PeristalticMapping,
    default_mapping,
    load_mapping,
    save_mapping,
)
from services.peristaltic.prime import PrimeChunkResult, prime_single_pump
from services.peristaltic.safety import (
    SafetyCheckFailed,
    best_effort_stop_all as _best_effort_stop_all,
    ensure_all_idle_before_dose,
)
from services.peristaltic.serial_client import (
    ClientConfig,
    ConnectionState,
    PeristalticError,
    PeristalticSerialClient,
)
from services.peristaltic.session_log import PeristalticSessionLogger
from scripts.peristaltic.discover import DiscoveredPort, cmd_discover, discover_ports  # noqa: F401 (DiscoveredPort re-exported for callers/tests)

MAPPING_PATH = Path("config/peristaltic_mapping.json")
CALIBRATION_DATA_PATH = Path("calibration_data/peristaltic_calibration.json")
FIRMWARE_PROFILES_PATH = Path("config/peristaltic_firmware_profiles.json")

REQUIRED_WATER_TEST_CONFIRMATION = "conf"

# Overall watchdog for the CLI's own parallel-dose monitoring loop -
# independent of the client's internal per-command timeouts. Generous
# enough to cover the firmware's own 30s runtime watchdog plus margin.
PARALLEL_MONITOR_TIMEOUT_S = 40.0

# SafetyCheckFailed, ensure_all_idle_before_dose und best_effort_stop_all
# (als _best_effort_stop_all) leben jetzt in services/peristaltic/safety.py -
# gemeinsam mit process/pump_prime.py genutzt, siehe dortige Docstrings.

_GUARDED_EXCEPTIONS = (PeristalticError, SafetyCheckFailed)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prompt(prompt: str, input_func: Callable[[str], str] = input) -> str:
    return input_func(prompt).strip()


# --- map ------------------------------------------------------------------------


def cmd_map(args: argparse.Namespace, input_func: Callable[[str], str] = input) -> int:
    ports = discover_ports()
    if ports:
        print("Gefundene serielle Ports:")
        for index, port in enumerate(ports, start=1):
            stable = port.by_id_path or "(kein by-id-Pfad)"
            print(f"  [{index}] {port.device}  stabiler Pfad: {stable}  ({port.description})")
    else:
        print("Keine seriellen Ports gefunden - eine Zuordnung ist trotzdem möglich (Pfad manuell eingeben).")

    if MAPPING_PATH.exists():
        try:
            mapping = load_mapping(MAPPING_PATH)
        except MappingValidationError as exc:
            print(f"[FEHLER] Bestehendes Mapping ist ungültig, wird nicht geladen: {exc}")
            return 1
    else:
        mapping = default_mapping()

    for controller_id in VALID_CONTROLLERS:
        current_entry = mapping.controllers[controller_id]
        print()
        print(f"Controller {controller_id} (Rolle: {current_entry.role}), aktueller Port: {current_entry.port}")
        choice = _prompt(
            f"Port für {controller_id} eingeben (Nummer aus der Liste, vollständiger Pfad, "
            "oder leer lassen zum Überspringen): ",
            input_func,
        )
        if not choice:
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(ports):
            selected = ports[int(choice) - 1]
            port_path = selected.by_id_path or selected.device
            if not selected.by_id_path:
                print(
                    f"[WARNUNG] Kein stabiler /dev/serial/by-id-Pfad für {selected.device} gefunden - "
                    "dieser Pfad kann sich nach einem Neustart/Umstecken ändern."
                )
        else:
            port_path = choice

        print(f"Teste Verbindung zu {controller_id} über {port_path} (PING + STATUS, keine Pumpenbewegung) ...")
        client = PeristalticSerialClient(port_path)
        try:
            client.open()
            status = client.status()
            print(f"  OK - PING erfolgreich. STATUS: {status}")
            print(
                "  Hinweis: Dies bestätigt nur eine funktionierende serielle Verbindung. Es kann NICHT "
                f"automatisch bestätigt werden, ob dies wirklich {controller_id} ist - die Firmware hat "
                "noch keine MCU-Identität im Protokoll (siehe docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md). "
                "Bitte physisch verifizieren (z.B. Kabel/USB-Port einzeln abstecken)."
            )
        except PeristalticError as exc:
            print(f"  [FEHLER] Verbindung zu {port_path} fehlgeschlagen: {exc}")
            client.close()
            continue
        finally:
            client.close()

        mapping.controllers[controller_id].port = port_path

    print()
    print("Neues Mapping:")
    for controller_id in VALID_CONTROLLERS:
        entry = mapping.controllers[controller_id]
        print(f"  {controller_id}: role={entry.role} port={entry.port} baudrate={entry.baudrate} pumps={entry.pumps}")

    if not confirm_simple("Mapping speichern? (y/n): ", input_func):
        print("Abgebrochen - nichts gespeichert.")
        return 1

    try:
        save_mapping(MAPPING_PATH, mapping)
    except MappingValidationError as exc:
        print(f"[FEHLER] Mapping ungültig, nicht gespeichert: {exc}")
        return 1

    print(f"Mapping gespeichert: {MAPPING_PATH}")
    return 0


def confirm_simple(prompt: str, input_func: Callable[[str], str] = input) -> bool:
    """y/n-Bestätigung - NUR für Aktionen ohne Pumpenbewegung (z.B. das
    Speichern eines Mappings)."""
    return _prompt(prompt, input_func).lower() in ("y", "yes", "j", "ja")


# --- gemeinsame Hilfsfunktionen für hardwareberührende Befehle ---------------


@dataclass
class PumpDoseRequest:
    controller_id: str
    port: str
    pump: str
    role: str
    ml: float


def print_pre_dose_summary_and_confirm(
    requests: list[PumpDoseRequest], input_func: Callable[[str], str] = input
) -> bool:
    """Zeigt Controller/Port/lokale Pumpe/fachliche Rolle/Menge und den
    WASSER-TEST-Hinweis für jede angeforderte Pumpenbewegung, verlangt
    danach die exakte Bestätigungsphrase - ein einfaches y/n reicht hier
    NICHT."""
    print()
    print("=== WASSER-TEST: Pumpenbewegung angefordert ===")
    for request in requests:
        print(f"  Controller: {request.controller_id}   Port: {request.port}")
        print(f"  Pumpe:      {request.pump}   Fachliche Rolle: {request.role}")
        print(f"  Menge:      {request.ml} ml")
        print("  Hinweis:    WASSER-TEST - keine Chemikalien")
        print()
    print(f"Zum Fortfahren exakt eingeben: {REQUIRED_WATER_TEST_CONFIRMATION}")
    answer = input_func("> ")
    return answer.strip() == REQUIRED_WATER_TEST_CONFIRMATION


def _load_mapping_or_exit() -> PeristalticMapping:
    if not MAPPING_PATH.exists():
        print(f"[FEHLER] Mapping-Datei fehlt: {MAPPING_PATH}. Zuerst 'map' ausführen.")
        raise SystemExit(1)
    try:
        return load_mapping(MAPPING_PATH)
    except MappingValidationError as exc:
        print(f"[FEHLER] Mapping ungültig: {exc}")
        raise SystemExit(1)


def _resolve_controller(mapping: PeristalticMapping, controller_id: str) -> ControllerMapping:
    entry = mapping.controllers[controller_id]
    if not entry.port:
        print(f"[FEHLER] Für {controller_id} ist noch kein Port zugeordnet. Zuerst 'map' ausführen.")
        raise SystemExit(1)
    return entry


def _make_client(entry: ControllerMapping, session_logger: PeristalticSessionLogger) -> PeristalticSerialClient:
    return PeristalticSerialClient(
        entry.port,
        config=ClientConfig(baudrate=entry.baudrate),
        on_raw_line=session_logger.log_raw,
    )


def run_guarded(
    client: PeristalticSerialClient,
    session_logger: PeristalticSessionLogger,
    description: str,
    fn: Callable[[], Any],
) -> Any | None:
    """Einheitlicher Fehlerpfad für jede hardwareberührende Aktion: bei
    ERR/Timeout/Verbindungsabbruch/Sicherheitsverletzung sofort
    best-effort STOPALL, den Versuch als fehlgeschlagen loggen, KEINE
    automatische Wiederholung. Gibt None zurück, wenn fn() eine der
    erwarteten Fehlerklassen wirft (Aufrufer prüft auf None); bei
    KeyboardInterrupt oder einer unerwarteten Exception wird nach dem
    Cleanup weitergereicht."""
    try:
        return fn()
    except KeyboardInterrupt:
        print(f"[ABBRUCH] {description}: durch Nutzer unterbrochen (Ctrl+C).")
        _best_effort_stop_all(client)
        session_logger.log_row(command=description, result="ABORTED_KEYBOARD_INTERRUPT")
        raise
    except _GUARDED_EXCEPTIONS as exc:
        print(f"[FEHLER] {description}: {exc}")
        _best_effort_stop_all(client)
        session_logger.log_row(command=description, result="FAILED", error_code=type(exc).__name__, notes=str(exc))
        return None
    except Exception as exc:
        print(f"[FEHLER] {description}: unerwarteter Fehler: {exc}")
        _best_effort_stop_all(client)
        session_logger.log_row(command=description, result="FAILED", error_code=type(exc).__name__, notes=str(exc))
        raise


# --- check / stop-all -----------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    mapping = _load_mapping_or_exit()
    entry = _resolve_controller(mapping, args.controller)

    session_logger = PeristalticSessionLogger("check")
    client = _make_client(entry, session_logger)
    try:
        client.open()  # führt bereits STOPALL+PING+STATUS als Handshake aus
        status = client.status()
        print("PING: OK")
        print(f"STATUS: {status}")
        if client.diagnostics:
            print("Boot-/Diagnosezeilen:")
            for line in client.diagnostics:
                print(f"  {line}")
        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            command="check", result="OK", notes=str(status),
        )
        return 0
    except PeristalticError as exc:
        print(f"[FEHLER] check fehlgeschlagen: {exc}")
        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            command="check", result="FAILED", error_code=type(exc).__name__, notes=str(exc),
        )
        return 1
    finally:
        client.close()
        session_logger.close({"command": "check", "controller": args.controller})


def cmd_stop_all(args: argparse.Namespace) -> int:
    mapping = _load_mapping_or_exit()
    entry = _resolve_controller(mapping, args.controller)

    session_logger = PeristalticSessionLogger("stop-all")
    client = _make_client(entry, session_logger)
    try:
        client.open()
        client.stop_all()  # explizit angefordert, nicht nur inzidentiell aus open()
        status = client.status()
        print(f"STOPALL gesendet. STATUS: {status}")
        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            command="stop-all", result="OK", notes=str(status),
        )
        return 0
    except PeristalticError as exc:
        print(f"[FEHLER] stop-all fehlgeschlagen: {exc}")
        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            command="stop-all", result="FAILED", error_code=type(exc).__name__, notes=str(exc),
        )
        return 1
    finally:
        client.close()
        session_logger.close({"command": "stop-all", "controller": args.controller})


# --- test -------------------------------------------------------------------


def cmd_test(args: argparse.Namespace, input_func: Callable[[str], str] = input) -> int:
    dose_errors = validate_dose_ml(args.ml)
    if dose_errors:
        for message in dose_errors:
            print(f"[FEHLER] {message}")
        return 1

    mapping = _load_mapping_or_exit()
    entry = _resolve_controller(mapping, args.controller)
    role = entry.pumps[args.pump]

    request = PumpDoseRequest(args.controller, entry.port, args.pump, role, args.ml)
    if not print_pre_dose_summary_and_confirm([request], input_func):
        print("Abgebrochen - Bestätigung nicht exakt eingegeben.")
        return 1

    session_logger = PeristalticSessionLogger("test")
    client = _make_client(entry, session_logger)
    try:
        client.open()

        def _do() -> Any:
            ensure_all_idle_before_dose(client)
            return client.dose(args.pump, args.ml)

        started_monotonic = time.monotonic()
        started_at = _iso_now()
        result = run_guarded(client, session_logger, "test", _do)
        finished_at = _iso_now()
        elapsed_seconds = time.monotonic() - started_monotonic

        if result is None:
            return 1

        print(f"DONE: {result.raw}")
        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            pump=args.pump, pump_role=role, command="test",
            requested_ml=args.ml, started_at=started_at, finished_at=finished_at,
            elapsed_seconds=elapsed_seconds, result="DONE", firmware_reported_ml=result.ml,
        )
        return 0
    finally:
        client.close()
        session_logger.close({"command": "test", "controller": args.controller, "pump": args.pump})


# --- calibrate ----------------------------------------------------------------


def _resolve_firmware_value_or_exit(controller: str, pump: str) -> float:
    """Löst firmware_ml_per_step ausschließlich aus
    config/peristaltic_firmware_profiles.json auf - kein stiller Fallback
    auf einen globalen Default. Bricht (vor jeder Pumpenbewegung, wie
    _load_mapping_or_exit()/_resolve_controller()) mit SystemExit(1) ab,
    wenn die Profildatei fehlt/ungültig ist, der Controller nicht
    'confirmed' ist, oder der Wert für genau diese Pumpe null ist."""
    try:
        profiles = load_firmware_profiles(FIRMWARE_PROFILES_PATH)
    except FirmwareProfileError as exc:
        print(f"[FEHLER] Firmwareprofil ungültig oder nicht lesbar - Kalibrierung wird abgebrochen: {exc}")
        raise SystemExit(1)

    try:
        firmware_value = resolve_firmware_ml_per_step(profiles, controller, pump)
    except FirmwareProfileError as exc:
        print(
            f"[FEHLER] Firmwarewert für {controller}/{pump} nicht verfügbar - "
            f"Kalibrierung wird abgebrochen, bevor eine Pumpe bewegt wird: {exc}"
        )
        raise SystemExit(1)

    print(f"Firmware-Ist-Wert aus bestätigtem Profil {controller}/{pump}: {firmware_value}")
    return firmware_value


def cmd_calibrate(args: argparse.Namespace, input_func: Callable[[str], str] = input) -> int:
    dose_errors = validate_dose_ml(args.requested_ml)
    if dose_errors:
        for message in dose_errors:
            print(f"[FEHLER] {message}")
        return 1

    firmware_value = _resolve_firmware_value_or_exit(args.controller, args.pump)

    mapping = _load_mapping_or_exit()
    entry = _resolve_controller(mapping, args.controller)
    role = entry.pumps[args.pump]

    request = PumpDoseRequest(args.controller, entry.port, args.pump, role, args.requested_ml)
    if not print_pre_dose_summary_and_confirm([request], input_func):
        print("Abgebrochen - Bestätigung nicht exakt eingegeben.")
        return 1

    session_logger = PeristalticSessionLogger("calibrate")
    client = _make_client(entry, session_logger)
    try:
        client.open()

        def _do() -> Any:
            ensure_all_idle_before_dose(client)
            return client.dose(args.pump, args.requested_ml)

        started_monotonic = time.monotonic()
        started_at = _iso_now()
        result = run_guarded(client, session_logger, "calibrate", _do)
        finished_at = _iso_now()
        elapsed_seconds = time.monotonic() - started_monotonic

        if result is None:
            return 1

        print(f"DONE: {result.raw}")

        measured_str = _prompt("Tatsächlich gemessene Wassermenge in ml eingeben: ", input_func)
        try:
            measured_ml = float(measured_str)
        except ValueError:
            print("[FEHLER] Ungültige Zahl - Versuch wird NICHT gespeichert.")
            session_logger.log_row(
                controller=args.controller, controller_role=entry.role, serial_port=entry.port,
                pump=args.pump, pump_role=role, command="calibrate",
                requested_ml=args.requested_ml, started_at=started_at, finished_at=finished_at,
                elapsed_seconds=elapsed_seconds, result="DONE_BUT_MEASUREMENT_INVALID",
                firmware_reported_ml=result.ml,
            )
            return 1

        method = _prompt("Messmethode (graduated_cylinder / mass_approx / leer lassen): ", input_func) or None
        if method not in (None, "graduated_cylinder", "mass_approx"):
            print(f"[WARNUNG] Unbekannte Messmethode '{method}', wird trotzdem gespeichert.")

        temp_str = _prompt("Wassertemperatur in °C (optional, leer lassen): ", input_func)
        water_temperature_c: float | None = None
        if temp_str:
            try:
                water_temperature_c = float(temp_str)
            except ValueError:
                print("[WARNUNG] Ungültige Temperatur, wird ignoriert.")

        if CALIBRATION_DATA_PATH.exists():
            try:
                data = load_calibration_data(CALIBRATION_DATA_PATH)
            except CalibrationValidationError as exc:
                print(f"[FEHLER] Kalibrierdatei ungültig, Trial wird NICHT gespeichert: {exc}")
                return 1
        else:
            data = default_calibration_data()

        trial = add_trial(
            data, args.controller, args.pump,
            requested_ml=args.requested_ml, measured_ml=measured_ml,
            measurement_method=method, water_temperature_c=water_temperature_c,
            firmware_ml_per_step_used=firmware_value,
        )
        save_calibration_data(CALIBRATION_DATA_PATH, data)

        print()
        print(f"Kandidat berechnet: candidate_ml_per_step = {trial.candidate_ml_per_step}")
        print("Hinweis: dies ist ein KANDIDAT, kein bestätigter Firmwarewert - Status = 'candidate'.")

        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            pump=args.pump, pump_role=role, command="calibrate",
            requested_ml=args.requested_ml, measured_ml=measured_ml,
            measurement_method=method, water_temperature_c=water_temperature_c,
            started_at=started_at, finished_at=finished_at, elapsed_seconds=elapsed_seconds,
            result="DONE", firmware_reported_ml=result.ml,
            firmware_ml_per_step_used=trial.firmware_ml_per_step_used,
            candidate_ml_per_step=trial.candidate_ml_per_step,
        )
        return 0
    finally:
        client.close()
        session_logger.close({"command": "calibrate", "controller": args.controller, "pump": args.pump})


# --- prime (Entlüften) ----------------------------------------------------------
#
# Bewusst KEINE Kalibrierung: kein Eintrag in calibration_data/
# peristaltic_calibration.json, kein firmware_ml_per_step_used, kein
# candidate_ml_per_step. Das Firmwareprofil wird trotzdem geprüft (fail-
# closed, wie bei calibrate) - nur als Gate vor jeder Pumpenbewegung, der
# aufgelöste Wert wird für Priming selbst nicht gebraucht/verwendet.


def _format_ml_for_confirmation(value: float) -> str:
    """180.0 -> "180", 155.5 -> "155.5" - die Bestätigungsphrase soll genau
    der Aufgaben-Beispielschreibweise entsprechen (PRIME MCU_B P2 180),
    nicht der Python-Float-Darstellung mit ".0"."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _required_prime_confirmation(controller_id: str, pump: str, max_ml: float) -> str:
    return f"PRIME {controller_id} {pump} {_format_ml_for_confirmation(max_ml)}"


def print_pre_prime_summary_and_confirm(
    controller_id: str, port: str, pump: str, role: str, max_ml: float, chunk_ml: float,
    input_func: Callable[[str], str] = input,
) -> bool:
    """Wie print_pre_dose_summary_and_confirm(), aber mit einer eigenen,
    dynamischen Bestätigungsphrase (enthält Controller/Pumpe/Maximalmenge)
    statt der festen WASSER-TEST-Phrase - ein einfaches 'conf' reicht für
    Priming NICHT."""
    print()
    print("=== WASSER-TEST: Priming (Entlüften) angefordert ===")
    print(f"  Controller: {controller_id}   Port: {port}")
    print(f"  Pumpe:      {pump}   Fachliche Rolle: {role}")
    print(f"  Maximale Priming-Menge: {max_ml} ml")
    print(f"  Teilmenge pro Auftrag:  {chunk_ml} ml")
    print("  Hinweis:    ausschließlich WASSER - keine Chemikalien")
    print("  Hinweis:    Ctrl+C stoppt den Vorgang (best-effort STOPALL)")
    print()
    required = _required_prime_confirmation(controller_id, pump, max_ml)
    print(f"Zum Fortfahren exakt eingeben: {required}")
    answer = input_func("> ")
    return answer.strip() == required


def cmd_prime(args: argparse.Namespace, input_func: Callable[[str], str] = input) -> int:
    priming_errors = validate_priming_request(args.max_ml, args.chunk_ml)
    if priming_errors:
        for message in priming_errors:
            print(f"[FEHLER] {message}")
        return 1

    _resolve_firmware_value_or_exit(args.controller, args.pump)  # nur Gate, Wert wird nicht gebraucht

    mapping = _load_mapping_or_exit()
    entry = _resolve_controller(mapping, args.controller)
    role = entry.pumps[args.pump]

    chunks = compute_priming_chunks(args.max_ml, args.chunk_ml)

    if not print_pre_prime_summary_and_confirm(
        args.controller, entry.port, args.pump, role, args.max_ml, args.chunk_ml, input_func
    ):
        print("Abgebrochen - Bestätigung nicht exakt eingegeben.")
        return 1

    session_logger = PeristalticSessionLogger("prime")
    client = _make_client(entry, session_logger)
    # Läuft während des Chunk-Loops (in services.peristaltic.prime.prime_single_pump)
    # mit - so bleibt der zuletzt bekannte Fortschritt hier verfügbar, falls
    # KeyboardInterrupt/eine unerwartete Exception mitten im Vorgang durchschlägt.
    progress = {"completed_ml": 0.0}
    completion_reason = "error"

    def _on_chunk_complete(chunk: PrimeChunkResult) -> None:
        progress["completed_ml"] = chunk.completed_total_ml
        print(f"Priming-Fortschritt: {chunk.completed_total_ml} / {args.max_ml} ml")

    def _log_abort_row(*, result: str, error_code: str | None, notes: str | None) -> None:
        session_logger.log_row(
            controller=args.controller, controller_role=entry.role, serial_port=entry.port,
            pump=args.pump, pump_role=role, command="prime",
            operation="prime", requested_max_ml=args.max_ml, chunk_ml=args.chunk_ml,
            requested_ml=None, started_at=None, finished_at=_iso_now(), elapsed_seconds=None,
            result=result, error_code=error_code, notes=notes,
            completed_ml=progress["completed_ml"], completion_reason="user_abort" if result == "ABORTED_KEYBOARD_INTERRUPT" else "error",
        )

    try:
        client.open()
        ensure_all_idle_before_dose(client)  # STATUS -> STOPALL -> STATUS -> alle Pumpen IDLE

        print(f"Bereit - alle Pumpen IDLE. Starte Priming über {len(chunks)} Teilauftrag(e) (max. {args.max_ml} ml).")

        outcome = prime_single_pump(
            client, session_logger,
            controller_id=args.controller, controller_role=entry.role, port=entry.port,
            pump=args.pump, role=role, max_ml=args.max_ml, chunk_ml=args.chunk_ml,
            stop_event=None, on_chunk_complete=_on_chunk_complete,
        )
        completion_reason = outcome.completion_reason

        if outcome.completion_reason == "completed":
            print()
            print(f"Priming abgeschlossen: {outcome.completed_ml} / {args.max_ml} ml.")
            return 0

        # completion_reason == "error" - CSV-Fehlerzeile und best-effort
        # STOPALL wurden bereits innerhalb von prime_single_pump geschrieben/
        # ausgelöst (stop_event=None, "user_abort" kann hier nie auftreten).
        print(f"[FEHLER] prime: {outcome.error_message}")
        return 1

    except KeyboardInterrupt:
        completion_reason = "user_abort"
        print(f"\n[ABBRUCH] prime: durch Nutzer unterbrochen (Ctrl+C) nach {progress['completed_ml']} ml.")
        _best_effort_stop_all(client)
        try:
            final_status = client.status()
            print(f"STATUS nach Abbruch: {final_status}")
        except PeristalticError:
            pass
        _log_abort_row(result="ABORTED_KEYBOARD_INTERRUPT", error_code=None, notes=None)
        return 130
    except _GUARDED_EXCEPTIONS as exc:
        completion_reason = "error"
        print(f"[FEHLER] prime: {exc}")
        _best_effort_stop_all(client)
        _log_abort_row(result="FAILED", error_code=type(exc).__name__, notes=str(exc))
        return 1
    except Exception as exc:
        completion_reason = "error"
        print(f"[FEHLER] prime: unerwarteter Fehler: {exc}")
        _best_effort_stop_all(client)
        _log_abort_row(result="FAILED", error_code=type(exc).__name__, notes=str(exc))
        raise
    finally:
        client.close()
        session_logger.close({
            "command": "prime", "controller": args.controller, "pump": args.pump,
            "operation": "prime", "requested_max_ml": args.max_ml, "chunk_ml": args.chunk_ml,
            "completed_ml": progress["completed_ml"], "completion_reason": completion_reason,
        })


# --- pair-test / all-four-test -------------------------------------------------


def _run_parallel_test(
    args: argparse.Namespace, pumps: list[str], command_name: str, input_func: Callable[[str], str] = input
) -> int:
    dose_errors = validate_dose_ml(args.ml_each)
    if dose_errors:
        for message in dose_errors:
            print(f"[FEHLER] {message}")
        return 1

    parallel_errors = validate_parallel_pump_selection(args.controller, pumps)
    if parallel_errors:
        for message in parallel_errors:
            print(f"[FEHLER] {message}")
        return 1

    mapping = _load_mapping_or_exit()
    entry = _resolve_controller(mapping, args.controller)

    requests = [PumpDoseRequest(args.controller, entry.port, pump, entry.pumps[pump], args.ml_each) for pump in pumps]
    if not print_pre_dose_summary_and_confirm(requests, input_func):
        print("Abgebrochen - Bestätigung nicht exakt eingegeben.")
        return 1

    session_logger = PeristalticSessionLogger(command_name)
    client = _make_client(entry, session_logger)
    try:
        client.open()

        def _do() -> dict[str, str]:
            ensure_all_idle_before_dose(client)
            outcomes: dict[str, str] = {}
            pending: list[str] = []

            for pump in pumps:
                try:
                    client.start_dose(pump, args.ml_each)
                    pending.append(pump)
                    print(f"Gestartet: {pump} {args.ml_each} ml")
                except PeristalticError as exc:
                    print(f"[FEHLER] Start von {pump} fehlgeschlagen: {exc}")
                    _best_effort_stop_all(client)
                    for already_started in pending:
                        outcomes[already_started] = "ABORTED_SIBLING_FAILURE"
                    outcomes[pump] = "FAILED_TO_START"
                    return outcomes

            deadline = time.monotonic() + PARALLEL_MONITOR_TIMEOUT_S
            failure = False
            while pending and time.monotonic() < deadline and not failure:
                for pump in list(pending):
                    event = client.poll_pump_event(pump, timeout=0.1)
                    if event is None:
                        continue
                    if event.kind is LineKind.RUNNING:
                        continue
                    if event.kind is LineKind.DONE:
                        print(f"DONE: {event.raw}")
                        outcomes[pump] = "DONE"
                        pending.remove(pump)
                    elif event.kind is LineKind.ERR_TIMEOUT:
                        print(f"[FEHLER] {pump}: Firmware-Timeout ({event.raw})")
                        outcomes[pump] = "FIRMWARE_TIMEOUT"
                        pending.remove(pump)
                        failure = True
                        break

            if failure or pending:
                _best_effort_stop_all(client)
                for pump in pending:
                    outcomes[pump] = "ABORTED"
                print("[FEHLER] Paralleltest abgebrochen - STOPALL gesendet, keine automatische Wiederholung.")

            return outcomes

        outcomes = run_guarded(client, session_logger, command_name, _do)
        if outcomes is None:
            for pump in pumps:
                session_logger.log_row(
                    controller=args.controller, controller_role=entry.role, serial_port=entry.port,
                    pump=pump, pump_role=entry.pumps[pump], command=command_name,
                    requested_ml=args.ml_each, result="FAILED",
                )
            return 1

        for pump in pumps:
            session_logger.log_row(
                controller=args.controller, controller_role=entry.role, serial_port=entry.port,
                pump=pump, pump_role=entry.pumps[pump], command=command_name,
                requested_ml=args.ml_each, result=outcomes.get(pump, "UNKNOWN"),
            )

        success = len(outcomes) == len(pumps) and all(outcome == "DONE" for outcome in outcomes.values())
        return 0 if success else 1
    finally:
        client.close()
        session_logger.close({"command": command_name, "controller": args.controller, "pumps": pumps})


def cmd_pair_test(args: argparse.Namespace, input_func: Callable[[str], str] = input) -> int:
    return _run_parallel_test(args, list(args.pumps), "pair-test", input_func)


def cmd_all_four_test(args: argparse.Namespace, input_func: Callable[[str], str] = input) -> int:
    return _run_parallel_test(args, list(VALID_PUMPS), "all-four-test", input_func)


# --- argparse / main ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="peristaltic_calibration_cli.py",
        description=(
            "Eigenständiges, terminalbasiertes Test-, Mapping- und Kalibrierprogramm für die "
            "Peristaltik-Controller MCU_A/MCU_B. Nur Wasser-Tests, keine Chemikaliendosierung, "
            "keine Dashboard-/State-Machine-Integration."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("discover", help="Verfügbare serielle Ports anzeigen (keine Pumpenbewegung).")

    subparsers.add_parser(
        "map", help="Ports interaktiv MCU_A/MCU_B zuordnen (PING+STATUS, keine Pumpenbewegung)."
    )

    check_parser = subparsers.add_parser(
        "check", help="PING/STATUS/Diagnose eines Controllers anzeigen (keine Pumpenbewegung)."
    )
    check_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)

    stop_all_parser = subparsers.add_parser("stop-all", help="STOPALL an einen Controller senden und STATUS prüfen.")
    stop_all_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)

    test_parser = subparsers.add_parser(
        "test", help=f"Sicherer Einzelpumpentest mit Wasser (max. {MAX_INITIAL_TEST_DOSE_ML} ml)."
    )
    test_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)
    test_parser.add_argument("--pump", choices=VALID_PUMPS, required=True)
    test_parser.add_argument("--ml", type=float, required=True)

    calibrate_parser = subparsers.add_parser("calibrate", help="Interaktive Wasser-Kalibrierung einer einzelnen Pumpe.")
    calibrate_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)
    calibrate_parser.add_argument("--pump", choices=VALID_PUMPS, required=True)
    calibrate_parser.add_argument("--requested-ml", type=float, required=True)

    prime_parser = subparsers.add_parser(
        "prime",
        help=(
            f"Schlauch mit Wasser entlüften (bis zu {MAX_PRIMING_TOTAL_ML} ml gesamt, in Teilaufträgen "
            f"von höchstens {MAX_INITIAL_TEST_DOSE_ML} ml) - KEINE Kalibrierung, kein Eintrag in "
            "peristaltic_calibration.json, immer nur eine Pumpe."
        ),
    )
    prime_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)
    prime_parser.add_argument("--pump", choices=VALID_PUMPS, required=True)
    prime_parser.add_argument("--max-ml", type=float, required=True)
    prime_parser.add_argument("--chunk-ml", type=float, default=DEFAULT_PRIMING_CHUNK_ML)

    pair_parser = subparsers.add_parser(
        "pair-test",
        help=(
            "Zwei Pumpen gleichzeitig testen - NUR MCU_B, NUR die Paare P1+P3 oder P2+P4. "
            "Nur nach erfolgreicher Einzelkalibrierung der beteiligten Pumpen empfohlen."
        ),
    )
    pair_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)
    pair_parser.add_argument("--pumps", nargs=2, choices=VALID_PUMPS, required=True)
    pair_parser.add_argument("--ml-each", type=float, required=True)

    all_four_parser = subparsers.add_parser(
        "all-four-test",
        help=(
            "Alle vier Pumpen eines Controllers gleichzeitig testen - NUR MCU_B. "
            "Nur nach erfolgreicher Einzelkalibrierung empfohlen."
        ),
    )
    all_four_parser.add_argument("--controller", choices=VALID_CONTROLLERS, required=True)
    all_four_parser.add_argument("--ml-each", type=float, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "discover": cmd_discover,
        "map": cmd_map,
        "check": cmd_check,
        "stop-all": cmd_stop_all,
        "test": cmd_test,
        "calibrate": cmd_calibrate,
        "prime": cmd_prime,
        "pair-test": cmd_pair_test,
        "all-four-test": cmd_all_four_test,
    }

    try:
        return handlers[args.subcommand](args)
    except KeyboardInterrupt:
        print("\nAbgebrochen (Ctrl+C).")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
