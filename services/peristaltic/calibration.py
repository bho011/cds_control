"""
Kalibrierberechnung, Trial-/Statistik-Modell und CLI-Sicherheitsvalidierung
(Dosismengen-Grenze, Parallel-Pumpen-Beschränkung) für den
Peristaltik-Testclient.

Trennt bewusst drei Begriffe, die sonst leicht unter einem einzigen Feld
vermischt würden:

- firmware_ml_per_step_used: der Wert, der zum Zeitpunkt eines konkreten
  Trials tatsächlich als in der Firmware aktiv angenommen wurde - niemals
  automatisch ein vorheriger Kandidat. Für das `calibrate`-Kommando wird
  dieser Wert ausschließlich aus dem bestätigten Firmwareprofil des
  jeweiligen Controllers/der jeweiligen Pumpe aufgelöst (siehe
  services/peristaltic/firmware_profiles.py::resolve_firmware_ml_per_step) -
  FIRMWARE_DEFAULT_ML_PER_STEP unten ist nur noch ein interner
  Rückfallwert für add_trial()-Aufrufe ohne explizit übergebenen Wert
  (z.B. in Tests) und wird vom `calibrate`-Kommando nicht mehr verwendet.
- candidate_ml_per_step: aus Messungen berechneter Vorschlag, wird nie
  automatisch als neuer firmware_ml_per_step_used übernommen.
- verified_ml_per_step: bleibt in dieser Aufgabe null (kein
  verify-Workflow implementiert, nur vorbereitet).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIRMWARE_DEFAULT_ML_PER_STEP = 0.000095548   # historischer Rückfallwert fuer add_trial() ohne
                                              # expliziten Wert - NICHT mehr der reale MCU_B-
                                              # Firmwarewert, siehe config/peristaltic_firmware_profiles.json
MAX_INITIAL_TEST_DOSE_ML = 10.0              # bewusst unter Firmware-Limit 50.0 - gilt für test/calibrate,
                                              # bleibt unverändert (siehe MAX_PRIMING_TOTAL_ML unten für prime)

# Separate, bewusst höhere Grenze nur für 'prime' (Entlüften) - die Schläuche
# fassen ca. 150 ml, MAX_INITIAL_TEST_DOSE_ML wäre dafür unpraktisch klein.
# Ein einzelner Priming-Teilauftrag ist technisch trotzdem ein normaler
# DOSE-Befehl und darf MAX_INITIAL_TEST_DOSE_ML nie überschreiten (siehe
# validate_priming_request) - nur die GESAMTMENGE über mehrere Teilaufträge
# darf höher sein.
MAX_PRIMING_TOTAL_ML = 200.0
DEFAULT_PRIMING_CHUNK_ML = 10.0

_REQUESTED_ML_COMPARISON_DECIMALS = 3    # wie ML_ROUNDING_DECIMALS im übrigen Repo
_FIRMWARE_VALUE_COMPARISON_DECIMALS = 9  # genug Präzision für ml_per_step-Werte wie 0.000095548


class CalibrationValidationError(ValueError):
    """Sammelt/trägt alle gefundenen Probleme, nie nur das erste."""


def validate_dose_ml(value: Any) -> list[str]:
    """type(value) is bool -> Fehler; nicht endliche Zahl (NaN/Inf) ->
    Fehler; 0 < ml <= MAX_INITIAL_TEST_DOSE_ML, sonst Fehler. Sammelt alle
    Probleme, wie services/settings_validation.py::validate_settings."""
    errors: list[str] = []

    if type(value) is bool:
        return [f"Dosismenge darf kein Wahrheitswert (true/false) sein (war: {value!r})."]

    if not isinstance(value, (int, float)):
        return [f"Dosismenge muss eine Zahl sein (war: {value!r}, Typ {type(value).__name__})."]

    if not math.isfinite(value):
        return [f"Dosismenge muss eine endliche Zahl sein (war: {value!r})."]

    if value <= 0:
        errors.append(f"Dosismenge muss größer als 0 ml sein (war: {value} ml).")
    elif value > MAX_INITIAL_TEST_DOSE_ML:
        errors.append(
            f"Dosismenge darf maximal {MAX_INITIAL_TEST_DOSE_ML} ml betragen (war: {value} ml) - "
            "diese Grenze ist bewusst niedriger als das Firmwarelimit."
        )

    return errors


def _validate_finite_positive_number(value: Any, label: str, max_value: float) -> list[str]:
    if type(value) is bool:
        return [f"'{label}' darf kein Wahrheitswert (true/false) sein (war: {value!r})."]

    if not isinstance(value, (int, float)):
        return [f"'{label}' muss eine Zahl sein (war: {value!r}, Typ {type(value).__name__})."]

    if not math.isfinite(value):
        return [f"'{label}' muss eine endliche Zahl sein (war: {value!r})."]

    if value <= 0:
        return [f"'{label}' muss größer als 0 ml sein (war: {value} ml)."]

    if value > max_value:
        return [f"'{label}' darf maximal {max_value} ml betragen (war: {value} ml)."]

    return []


def validate_priming_request(max_ml: Any, chunk_ml: Any) -> list[str]:
    """Eigene Grenzen für 'prime' (Entlüften) - MAX_INITIAL_TEST_DOSE_ML
    für test/calibrate bleibt davon unberührt. max_ml: 0 < max_ml <=
    MAX_PRIMING_TOTAL_ML. chunk_ml: 0 < chunk_ml <= MAX_INITIAL_TEST_DOSE_ML
    (ein einzelner Teilauftrag ist technisch weiterhin ein normaler
    DOSE-Befehl und darf die normale Testgrenze nie überschreiten). Sammelt
    beide Fehler, falls beide Werte ungültig sind."""
    errors: list[str] = []
    errors.extend(_validate_finite_positive_number(max_ml, "max_ml", MAX_PRIMING_TOTAL_ML))
    errors.extend(_validate_finite_positive_number(chunk_ml, "chunk_ml", MAX_INITIAL_TEST_DOSE_ML))
    return errors


def compute_priming_chunks(max_ml: float, chunk_ml: float) -> list[float]:
    """Teilt max_ml in Teilaufträge von höchstens chunk_ml auf - der
    letzte Teilauftrag darf kleiner sein (nie größer). Beispiel: 150/10 ->
    15x 10 ml; 155/10 -> 15x 10 ml + 1x 5 ml. Erwartet bereits über
    validate_priming_request() geprüfte, positive Werte."""
    full_chunks, remainder = divmod(max_ml, chunk_ml)
    chunks = [chunk_ml] * int(full_chunks)
    if remainder > 1e-9:  # rundungstolerant statt strikt > 0 (Float-divmod)
        chunks.append(round(remainder, 6))
    return chunks


def candidate_ml_per_step(firmware_ml_per_step_used: float, measured_ml: float, requested_ml: float) -> float:
    return firmware_ml_per_step_used * measured_ml / requested_ml


def _same_requested_ml(a: float, b: float) -> bool:
    return round(a, _REQUESTED_ML_COMPARISON_DECIMALS) == round(b, _REQUESTED_ML_COMPARISON_DECIMALS)


def _same_firmware_value(a: float, b: float) -> bool:
    return round(a, _FIRMWARE_VALUE_COMPARISON_DECIMALS) == round(b, _FIRMWARE_VALUE_COMPARISON_DECIMALS)


@dataclass
class CalibrationTrial:
    timestamp_utc: str
    requested_ml: float
    measured_ml: float
    measurement_method: str | None
    water_temperature_c: float | None
    firmware_ml_per_step_used: float
    candidate_ml_per_step: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "requested_ml": self.requested_ml,
            "measured_ml": self.measured_ml,
            "measurement_method": self.measurement_method,
            "water_temperature_c": self.water_temperature_c,
            "firmware_ml_per_step_used": self.firmware_ml_per_step_used,
            "candidate_ml_per_step": self.candidate_ml_per_step,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CalibrationTrial":
        return CalibrationTrial(
            timestamp_utc=data["timestamp_utc"],
            requested_ml=data["requested_ml"],
            measured_ml=data["measured_ml"],
            measurement_method=data.get("measurement_method"),
            water_temperature_c=data.get("water_temperature_c"),
            firmware_ml_per_step_used=data["firmware_ml_per_step_used"],
            candidate_ml_per_step=data["candidate_ml_per_step"],
        )


@dataclass
class PumpCalibrationStats:
    count: int
    mean_measured_ml: float
    median_measured_ml: float
    stdev_measured_ml: float | None      # None bei count < 2
    mean_absolute_deviation_ml: float    # mean(|x - mean_measured_ml|)
    mean_absolute_relative_error_percent: float   # je Trial abs(measured-requested)/requested*100, dann Mittelwert
    suggested_candidate_ml_per_step: float


def compute_pump_stats(
    trials: list[CalibrationTrial],
    requested_ml: float,
    firmware_ml_per_step_used: float,
) -> PumpCalibrationStats:
    """Wertet NUR Trials aus, bei denen BEIDE Werte (mit kanonischer
    Rundung statt nackter Floatgleichheit) übereinstimmen:
    trial.requested_ml == requested_ml UND
    trial.firmware_ml_per_step_used == firmware_ml_per_step_used. Trials
    derselben Sollmenge, aber mit einem anderen (z.B. nach einer expliziten
    Firmwareänderung protokollierten) firmware_ml_per_step_used, werden NIE
    gemeinsam zu einem Kandidaten verrechnet. Wirft
    CalibrationValidationError, wenn keine Trials beide Kriterien
    gleichzeitig erfüllen."""
    relevant = [
        t
        for t in trials
        if _same_requested_ml(t.requested_ml, requested_ml)
        and _same_firmware_value(t.firmware_ml_per_step_used, firmware_ml_per_step_used)
    ]

    if not relevant:
        raise CalibrationValidationError(
            f"Keine Versuche mit requested_ml={requested_ml} und "
            f"firmware_ml_per_step_used={firmware_ml_per_step_used} gefunden."
        )

    measured = [t.measured_ml for t in relevant]
    n = len(measured)
    mean_measured = statistics.mean(measured)
    median_measured = statistics.median(measured)
    stdev_measured = statistics.stdev(measured) if n >= 2 else None
    mean_absolute_deviation = statistics.mean(abs(x - mean_measured) for x in measured)
    mean_absolute_relative_error_percent = statistics.mean(
        abs(x - requested_ml) / requested_ml * 100.0 for x in measured
    )
    suggested = candidate_ml_per_step(firmware_ml_per_step_used, median_measured, requested_ml)

    return PumpCalibrationStats(
        count=n,
        mean_measured_ml=mean_measured,
        median_measured_ml=median_measured,
        stdev_measured_ml=stdev_measured,
        mean_absolute_deviation_ml=mean_absolute_deviation,
        mean_absolute_relative_error_percent=mean_absolute_relative_error_percent,
        suggested_candidate_ml_per_step=suggested,
    )


# --- Persistenz: calibration_data/peristaltic_calibration.json ----------------

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
                "P2": _pump_entry("nutrient_a_2"),
                "P3": _pump_entry("nutrient_b_1"),
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


# --- Parallel-Pumpen-Beschränkung (pair-test / all-four-test) -----------------

ALLOWED_PARALLEL_PUMP_GROUPS: dict[str, list[frozenset[str]]] = {
    # Keine Paralleltests auf MCU_A - pH-Säure/Base dürfen nie gleichzeitig
    # dosieren (siehe docs/CDS_EC_MIXING_PROCESS_CONCEPT.md).
    "MCU_A": [],
    "MCU_B": [
        frozenset({"P1", "P2"}),
        frozenset({"P3", "P4"}),
        frozenset({"P1", "P2", "P3", "P4"}),
    ],
}


def validate_parallel_pump_selection(controller: str, pumps: list[str]) -> list[str]:
    """Sammelt Fehler: die übergebene Pumpenmenge muss exakt einer der für
    diesen Controller erlaubten Gruppen entsprechen. MCU_A erlaubt gar
    keine Paralleltests. MCU_B erlaubt nur {P1,P2} und {P3,P4} (pair-test)
    oder alle vier (all-four-test) - explizit NICHT {P1,P3}, {P2,P4} oder
    sonstige Kombinationen."""
    allowed_groups = ALLOWED_PARALLEL_PUMP_GROUPS.get(controller)
    if allowed_groups is None:
        return [f"Unbekannter Controller für Paralleltest: {controller!r}."]

    requested_group = frozenset(pumps)

    if requested_group in allowed_groups:
        return []

    if not allowed_groups:
        return [f"Paralleltests sind für {controller} nicht zulässig (nur Einzeltest/-kalibrierung)."]

    allowed_display = " oder ".join("{" + ",".join(sorted(g)) + "}" for g in allowed_groups)
    return [
        f"Pumpenkombination {{{','.join(sorted(requested_group))}}} ist für {controller} nicht zulässig "
        f"- erlaubt ist nur {allowed_display}."
    ]
