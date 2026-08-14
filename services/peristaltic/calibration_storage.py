"""Persistenz: calibration_data/peristaltic_calibration.json (atomic write)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .calibration_stats import CalibrationTrial, candidate_ml_per_step, compute_pump_stats
from .dose_limits import CalibrationValidationError, FIRMWARE_DEFAULT_ML_PER_STEP

_SAVE_LOCK = threading.RLock()


def default_calibration_data() -> dict[str, Any]:
    """Frische, unabhängige Struktur pro Aufruf (kein Modul-Singleton -
    dasselbe Prinzip wie models.default_mapping())."""
    def _pump_entry(role: str) -> dict[str, Any]:
        return {
            "role": role,
            "status": "not_calibrated",
            "candidate_ml_per_step": None,
            "verified_ml_per_step": None,
            "verified_at_ml": [],
            "last_updated": None,
            "trials": [],
        }

    return {
        "schema_version": 1,
        "controllers": {
            "MCU_A": {
                "P1": _pump_entry("ph_acid"),
                "P2": _pump_entry("ph_base"),
                "P3": _pump_entry("unassigned"),
                "P4": _pump_entry("unassigned"),
            },
            "MCU_B": {
                "P1": _pump_entry("nutrient_a_1"),
                "P2": _pump_entry("nutrient_b_1"),
                "P3": _pump_entry("nutrient_a_2"),
                "P4": _pump_entry("nutrient_b_2"),
            },
        },
    }


def load_calibration_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CalibrationValidationError(f"Kalibrierdatei nicht gefunden: {path}")

    with path.open("r", encoding="utf-8") as file:
        try:
            return json.load(file)
        except json.JSONDecodeError as exc:
            raise CalibrationValidationError(f"{path}: ungültiges JSON ({exc}).") from exc


# Statuswerte, die als "volumetrisch kalibriert" gelten - bewusst eine
# Allow-Liste, keine Verneinung von "not_calibrated": ein unbekannter oder
# beschädigter Statuswert soll fail-closed als NICHT kalibriert gelten,
# nie versehentlich durchgelassen werden.
_CALIBRATED_STATUSES = frozenset({"candidate", "verified"})


def has_volumetric_calibration(data: dict[str, Any], controller: str, pump: str) -> bool:
    """True nur für explizit bekannte, tatsächlich kalibrierte Statuswerte
    ("candidate"/"verified"). load_calibration_data() validiert das Schema
    nicht (anders als load_mapping()/load_firmware_profiles()) - fehlende
    Schlüssel oder falsche Typen gelten hier deshalb ebenfalls als NICHT
    kalibriert, nie als Absturz."""
    try:
        status = data["controllers"][controller][pump]["status"]
    except (KeyError, TypeError):
        return False
    return status in _CALIBRATED_STATUSES


def save_calibration_data(path: Path, data: dict[str, Any]) -> None:
    with _SAVE_LOCK:
        _save_calibration_data_unlocked(path, data)


def _save_calibration_data_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: tempfile.mkstemp im selben Verzeichnis, flush+fsync,
    os.chmod(0o644), .bak-Backup, Path.replace() - identisches Muster zu
    models.py::_save_mapping_unlocked / recipe_store.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())

        os.chmod(tmp_path, 0o644)

        if path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            try:
                shutil.copy2(path, backup_path)
            except OSError:
                pass

        tmp_path.replace(path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def add_trial(
    data: dict[str, Any],
    controller: str,
    pump: str,
    *,
    requested_ml: float,
    measured_ml: float,
    measurement_method: str | None,
    water_temperature_c: float | None,
    firmware_ml_per_step_used: float | None = None,
) -> CalibrationTrial:
    """firmware_ml_per_step_used: None -> FIRMWARE_DEFAULT_ML_PER_STEP. NIE
    automatisch aus dem vorherigen candidate_ml_per_step der Pumpe
    übernommen - nur eine vom Aufrufer EXPLIZIT übergebene
    Firmwareänderung darf diesen Wert für einen neuen Trial verändern.
    Aktualisiert im Pumpen-Eintrag: status (not_calibrated -> candidate,
    nie automatisch verified), candidate_ml_per_step, last_updated.
    verified_ml_per_step und verified_at_ml bleiben unberührt. Verändert
    `data` in-place und gibt zusätzlich den neuen Trial zurück."""
    effective_firmware_value = (
        FIRMWARE_DEFAULT_ML_PER_STEP if firmware_ml_per_step_used is None else firmware_ml_per_step_used
    )

    pump_entry = data["controllers"][controller][pump]

    trial = CalibrationTrial(
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        requested_ml=requested_ml,
        measured_ml=measured_ml,
        measurement_method=measurement_method,
        water_temperature_c=water_temperature_c,
        firmware_ml_per_step_used=effective_firmware_value,
        candidate_ml_per_step=candidate_ml_per_step(effective_firmware_value, measured_ml, requested_ml),
    )

    pump_entry["trials"].append(trial.to_dict())

    all_trials = [CalibrationTrial.from_dict(t) for t in pump_entry["trials"]]
    stats = compute_pump_stats(all_trials, requested_ml=requested_ml, firmware_ml_per_step_used=effective_firmware_value)

    pump_entry["status"] = "candidate"
    pump_entry["candidate_ml_per_step"] = stats.suggested_candidate_ml_per_step
    pump_entry["last_updated"] = trial.timestamp_utc

    return trial
