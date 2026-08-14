"""Dose-limit constants and validation for the peristaltic test/prime CLI.

Kept separate from calibration_stats.py (Trial/statistics model) and
calibration_storage.py (JSON persistence): this file only answers "is this
dose amount allowed", never touches trial history or disk I/O.
"""

from __future__ import annotations

import math
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
