#!/usr/bin/env python3
"""
Einmaliges Migrationsskript: korrigiert firmware_ml_per_step_used und
candidate_ml_per_step für die MCU_B/P1-Kalibrierversuche, die noch mit dem
veralteten globalen Python-Client-Defaultwert (0.000095548) protokolliert
wurden, obwohl MCU_B nachweislich bereits mit dem realen Firmwareprofil
(8 Microsteps, ML_PER_STEP 0.000191096, siehe
config/peristaltic_firmware_profiles.json) geflasht war, als diese Trials
tatsächlich durchgeführt wurden.

Betrifft AUSSCHLIESSLICH Trials, die ALLE der folgenden Kriterien erfüllen:
  - controller == "MCU_B", pump == "P1"
  - firmware_ml_per_step_used == 0.000095548 (mit Toleranz)
  - timestamp_utc zwischen 2026-07-22T09:10:02Z und 2026-07-22T09:16:02Z
    (beide Grenzen inklusive)

Für jeden dieser Trials:
  - firmware_ml_per_step_used wird auf 0.000191096 gesetzt
  - candidate_ml_per_step wird NEU berechnet als
    firmware_ml_per_step_used * measured_ml / requested_ml
    (services.peristaltic.calibration.candidate_ml_per_step)

Unverändert bleiben in JEDEM betroffenen Trial: requested_ml, measured_ml,
timestamp_utc, measurement_method, water_temperature_c. Alle anderen
Controller/Pumpen/Trials in der Datei bleiben vollständig unberührt.

Nach der Migration wird ausschließlich candidate_ml_per_step des
Pumpenkopfs MCU_B/P1 aktualisiert (Median der zuletzt ausgewerteten Gruppe
- gleiche requested_ml UND gleicher firmware_ml_per_step_used wie der
zeitlich letzte Trial dieser Pumpe, identische Definition wie
services.peristaltic.calibration.add_trial()/compute_pump_stats()).
status, verified_ml_per_step, verified_at_ml und last_updated des
Pumpenkopfs werden NICHT verändert.

--dry-run zeigt die geplante Änderung, schreibt/verändert nichts.
--apply erzeugt zuerst ein eigenes, separates Backup
(<dateiname>_before_migration_<UTC-Zeitstempel>.json im selben Verzeichnis,
zusätzlich zum generischen .bak aus save_calibration_data()) und schreibt
danach atomar über services.peristaltic.calibration.save_calibration_data.

Idempotent: das Auswahlkriterium ist der ALTE Firmwarewert - nach einer
erfolgreichen Migration erfüllt kein Trial mehr dieses Kriterium, ein
zweiter --apply-Lauf findet daher keine passenden Trials mehr und
verändert/speichert nichts.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Erlaubt den Direktaufruf "python scripts/migrate_peristaltic_calibration_firmware_value.py ..."
# aus dem Projekt-Root heraus - das Skript liegt in scripts/, die Pakete
# (services/) aber eine Ebene höher (gleiches Muster wie
# scripts/peristaltic_calibration_cli.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.peristaltic.calibration import (
    CalibrationTrial,
    CalibrationValidationError,
    candidate_ml_per_step,
    compute_pump_stats,
    load_calibration_data,
    save_calibration_data,
)

CALIBRATION_DATA_PATH = Path("calibration_data/peristaltic_calibration.json")

TARGET_CONTROLLER = "MCU_B"
TARGET_PUMP = "P1"
OLD_FIRMWARE_ML_PER_STEP = 0.000095548
NEW_FIRMWARE_ML_PER_STEP = 0.000191096

_FIRMWARE_VALUE_COMPARISON_DECIMALS = 9  # gleiche Präzision wie services/peristaltic/calibration.py

WINDOW_START_UTC = datetime(2026, 7, 22, 9, 10, 2, tzinfo=timezone.utc)
WINDOW_END_UTC = datetime(2026, 7, 22, 9, 16, 2, tzinfo=timezone.utc)


def _same_firmware_value(a: float, b: float) -> bool:
    return round(a, _FIRMWARE_VALUE_COMPARISON_DECIMALS) == round(b, _FIRMWARE_VALUE_COMPARISON_DECIMALS)


def _in_migration_window(timestamp_utc: str) -> bool:
    try:
        parsed = datetime.fromisoformat(timestamp_utc)
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        return False
    return WINDOW_START_UTC <= parsed <= WINDOW_END_UTC


def _is_migration_candidate(trial: dict[str, Any]) -> bool:
    firmware_value = trial.get("firmware_ml_per_step_used")
    if type(firmware_value) is bool or not isinstance(firmware_value, (int, float)):
        return False
    if not _same_firmware_value(firmware_value, OLD_FIRMWARE_ML_PER_STEP):
        return False

    timestamp_utc = trial.get("timestamp_utc")
    if not isinstance(timestamp_utc, str):
        return False
    return _in_migration_window(timestamp_utc)


def find_migration_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Liefert die Liste der geplanten Änderungen (Trial-Index innerhalb
    von MCU_B/P1 plus alte/neue Werte). dry-run und apply verwenden
    dieselbe Funktion, damit die angezeigte Planung exakt dem entspricht,
    was --apply tatsächlich anwendet."""
    plan: list[dict[str, Any]] = []

    pump_entry = data.get("controllers", {}).get(TARGET_CONTROLLER, {}).get(TARGET_PUMP)
    if not isinstance(pump_entry, dict):
        return plan

    trials = pump_entry.get("trials")
    if not isinstance(trials, list):
        return plan

    for index, trial in enumerate(trials):
        if not isinstance(trial, dict) or not _is_migration_candidate(trial):
            continue

        new_candidate = candidate_ml_per_step(
            NEW_FIRMWARE_ML_PER_STEP, trial["measured_ml"], trial["requested_ml"]
        )
        plan.append(
            {
                "index": index,
                "timestamp_utc": trial["timestamp_utc"],
                "requested_ml": trial["requested_ml"],
                "measured_ml": trial["measured_ml"],
                "old_firmware_ml_per_step_used": trial["firmware_ml_per_step_used"],
                "new_firmware_ml_per_step_used": NEW_FIRMWARE_ML_PER_STEP,
                "old_candidate_ml_per_step": trial["candidate_ml_per_step"],
                "new_candidate_ml_per_step": new_candidate,
            }
        )

    return plan


def _refresh_pump_head_candidate(pump_entry: dict[str, Any]) -> None:
    """candidate_ml_per_step des Pumpenkopfs = Median der zuletzt
    ausgewerteten Gruppe (gleiche requested_ml UND gleicher
    firmware_ml_per_step_used wie der zeitlich letzte Trial) - identische
    Definition wie services.peristaltic.calibration.add_trial(). status,
    verified_ml_per_step, verified_at_ml und last_updated werden NICHT
    angefasst."""
    all_trials = [CalibrationTrial.from_dict(t) for t in pump_entry["trials"]]
    if not all_trials:
        return

    last_trial = max(all_trials, key=lambda t: t.timestamp_utc)
    stats = compute_pump_stats(
        all_trials,
        requested_ml=last_trial.requested_ml,
        firmware_ml_per_step_used=last_trial.firmware_ml_per_step_used,
    )
    pump_entry["candidate_ml_per_step"] = stats.suggested_candidate_ml_per_step


def apply_migration_plan(data: dict[str, Any], plan: list[dict[str, Any]]) -> None:
    """Verändert `data` in-place NUR an den in `plan` referenzierten
    Trial-Indizes von MCU_B/P1 (firmware_ml_per_step_used und
    candidate_ml_per_step) und aktualisiert danach den Kandidaten des
    Pumpenkopfs. Alle anderen Controller/Pumpen/Trials/Felder bleiben
    unberührt."""
    pump_entry = data["controllers"][TARGET_CONTROLLER][TARGET_PUMP]
    trials = pump_entry["trials"]

    for change in plan:
        trial = trials[change["index"]]
        trial["firmware_ml_per_step_used"] = change["new_firmware_ml_per_step_used"]
        trial["candidate_ml_per_step"] = change["new_candidate_ml_per_step"]

    _refresh_pump_head_candidate(pump_entry)


def _print_plan(plan: list[dict[str, Any]]) -> None:
    if not plan:
        print(
            f"Keine passenden Trials gefunden ({TARGET_CONTROLLER}/{TARGET_PUMP}, "
            f"firmware_ml_per_step_used={OLD_FIRMWARE_ML_PER_STEP}, "
            f"Zeitfenster {WINDOW_START_UTC.isoformat()}..{WINDOW_END_UTC.isoformat()}) - "
            "keine Änderungen notwendig."
        )
        return

    print(f"{len(plan)} Trial(s) betroffen ({TARGET_CONTROLLER}/{TARGET_PUMP}):")
    for change in plan:
        print(
            f"  [Index {change['index']}] {change['timestamp_utc']}  "
            f"requested_ml={change['requested_ml']}  measured_ml={change['measured_ml']}"
        )
        print(
            f"    firmware_ml_per_step_used: {change['old_firmware_ml_per_step_used']!r} "
            f"-> {change['new_firmware_ml_per_step_used']!r}"
        )
        print(
            f"    candidate_ml_per_step:     {change['old_candidate_ml_per_step']!r} "
            f"-> {change['new_candidate_ml_per_step']!r}"
        )


def _create_backup(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.stem}_before_migration_{timestamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def run(path: Path, *, apply: bool) -> int:
    try:
        data = load_calibration_data(path)
    except CalibrationValidationError as exc:
        print(f"[FEHLER] Kalibrierdatei ungültig, keine Migration möglich: {exc}")
        return 1

    plan = find_migration_plan(data)
    _print_plan(plan)

    if not apply:
        print()
        print("Dry-Run - es wurde NICHTS verändert oder gespeichert.")
        return 0

    if not plan:
        print()
        print("Apply: keine Änderungen anzuwenden (bereits migriert oder nichts betroffen) - Datei unverändert.")
        return 0

    backup_path = _create_backup(path)
    print()
    print(f"Backup erstellt: {backup_path}")

    apply_migration_plan(data, plan)
    save_calibration_data(path, data)
    print(f"Migration angewendet und gespeichert: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_peristaltic_calibration_firmware_value.py",
        description=(
            "Korrigiert firmware_ml_per_step_used/candidate_ml_per_step für die betroffenen "
            "MCU_B/P1-Kalibrierversuche nach dem Wechsel auf das reale, bestätigte Firmwareprofil "
            "(config/peristaltic_firmware_profiles.json). Bewegt keine Pumpe, öffnet keinen seriellen Port."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Geplante Änderungen anzeigen, nichts speichern.")
    mode.add_argument("--apply", action="store_true", help="Backup erstellen und Änderungen atomar speichern.")
    parser.add_argument(
        "--path",
        type=Path,
        default=CALIBRATION_DATA_PATH,
        help=f"Pfad zur Kalibrierdatei (Default: {CALIBRATION_DATA_PATH}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.path, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
