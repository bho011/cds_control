"""Messpunkte erzeugen sowie als CSV schreiben/lesen (für --analyze)."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .cli_input import ask_yes_no
from .config import (
    BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS,
    BRIDGE_MIXER_SENSOR_LITER_FACTOR,
    BRIDGE_MIXER_SENSOR_LITER_OFFSET,
    BRIDGE_MIXER_VOLUME_LITERS,
)
from .models import Measurement, SensorStats


def create_measurement(
    phase: str,
    step: int,
    manual_added_l: float,
    manual_drained_l: float,
    reference_volume_l: float,
    raw_stats: SensorStats,
    system_liters_stats: Optional[SensorStats],
    note: str,
) -> Measurement:
    bridge_calculated_liters = (
        raw_stats.avg * BRIDGE_MIXER_SENSOR_LITER_FACTOR
        + BRIDGE_MIXER_SENSOR_LITER_OFFSET
    )

    bridge_error_liters = bridge_calculated_liters - reference_volume_l

    return Measurement(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        phase=phase,
        step=step,
        manual_added_l=manual_added_l,
        manual_drained_l=manual_drained_l,
        reference_volume_l=reference_volume_l,

        sensor_raw_avg=raw_stats.avg,
        sensor_raw_min=raw_stats.min,
        sensor_raw_max=raw_stats.max,
        sensor_raw_std=raw_stats.std,
        sensor_raw_first=raw_stats.first,
        sensor_raw_last=raw_stats.last,
        sensor_raw_drift=raw_stats.drift,
        sensor_raw_samples=raw_stats.samples,
        sensor_raw_series=raw_stats.raw_series,

        system_liters_avg=system_liters_stats.avg if system_liters_stats else None,
        system_liters_min=system_liters_stats.min if system_liters_stats else None,
        system_liters_max=system_liters_stats.max if system_liters_stats else None,
        system_liters_std=system_liters_stats.std if system_liters_stats else None,
        system_liters_first=system_liters_stats.first if system_liters_stats else None,
        system_liters_last=system_liters_stats.last if system_liters_stats else None,
        system_liters_drift=system_liters_stats.drift if system_liters_stats else None,
        system_liters_samples=system_liters_stats.samples if system_liters_stats else None,
        system_liters_series=system_liters_stats.raw_series if system_liters_stats else None,

        bridge_mixer_volume_liters=BRIDGE_MIXER_VOLUME_LITERS,
        bridge_sensor_liter_factor=BRIDGE_MIXER_SENSOR_LITER_FACTOR,
        bridge_sensor_liter_offset=BRIDGE_MIXER_SENSOR_LITER_OFFSET,
        bridge_sensor_calibration_status=BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS,
        bridge_calculated_liters=bridge_calculated_liters,
        bridge_error_liters=bridge_error_liters,

        valid_for_fit=True,
        invalid_reason="",

        note=note,
    )


def print_measurement_summary(measurement: Measurement) -> None:
    print("\nMesspunkt gespeichert:")
    print(f"  Phase:              {measurement.phase}")
    print(f"  Referenzvolumen:    {measurement.reference_volume_l:.3f} L")
    print(f"  Sensor raw avg:     {measurement.sensor_raw_avg:.6f}")
    print(f"  Sensor raw min/max: {measurement.sensor_raw_min:.6f} / {measurement.sensor_raw_max:.6f}")
    print(f"  Sensor raw std:     {measurement.sensor_raw_std:.6f}")
    print(f"  Sensor raw drift:   {measurement.sensor_raw_drift:.6f}")
    print(f"  Bridge gerechnet:   {measurement.bridge_calculated_liters:.3f} L")
    print(f"  Bridge Fehler:      {measurement.bridge_error_liters:+.3f} L")

    if measurement.system_liters_avg is not None:
        print(f"  System Liter avg:   {measurement.system_liters_avg:.6f} L")

    print()


def ask_measurement_validity(measurement: Measurement) -> None:
    valid = ask_yes_no("Messpunkt für spätere Formel verwenden?", default=True)
    measurement.valid_for_fit = valid

    if not valid:
        reason = input("Grund für ungültigen Messpunkt: ").strip()
        measurement.invalid_reason = reason or "not specified"

        if measurement.note:
            measurement.note = f"{measurement.note} | invalid: {measurement.invalid_reason}"
        else:
            measurement.note = f"invalid: {measurement.invalid_reason}"


def save_csv(path: Path, measurements: list[Measurement]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not measurements:
        return

    fieldnames = list(asdict(measurements[0]).keys())

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for measurement in measurements:
            writer.writerow(asdict(measurement))


def _row_float(row: dict[str, str], key: str, default: float) -> float:
    value = (row.get(key) or "").strip()
    return float(value) if value else default


def _row_optional_float(row: dict[str, str], key: str) -> Optional[float]:
    value = (row.get(key) or "").strip()
    return float(value) if value else None


def _row_int(row: dict[str, str], key: str, default: int) -> int:
    value = (row.get(key) or "").strip()
    return int(float(value)) if value else default


def _row_optional_int(row: dict[str, str], key: str) -> Optional[int]:
    value = (row.get(key) or "").strip()
    return int(float(value)) if value else None


def _row_bool(row: dict[str, str], key: str, default: bool) -> bool:
    value = row.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() == "true"


def load_csv(path: Path) -> list[Measurement]:
    """
    Loads previously saved calibration measurements back from CSV.

    Inverse of save_csv(). Used by --analyze to re-run the fit on
    already-collected data without a live hardware session. Older CSVs
    (from before the bridge_*/valid_for_fit/invalid_reason columns existed)
    are still readable: missing columns fall back to sensible defaults and
    never block loading.
    """
    with path.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file, delimiter=";")
        rows = list(reader)

    measurements = []

    for row in rows:
        measurements.append(
            Measurement(
                timestamp=row.get("timestamp", ""),
                phase=row.get("phase", ""),
                step=_row_int(row, "step", 0),
                manual_added_l=_row_float(row, "manual_added_l", 0.0),
                manual_drained_l=_row_float(row, "manual_drained_l", 0.0),
                reference_volume_l=_row_float(row, "reference_volume_l", 0.0),

                sensor_raw_avg=_row_float(row, "sensor_raw_avg", 0.0),
                sensor_raw_min=_row_float(row, "sensor_raw_min", 0.0),
                sensor_raw_max=_row_float(row, "sensor_raw_max", 0.0),
                sensor_raw_std=_row_float(row, "sensor_raw_std", 0.0),
                sensor_raw_first=_row_float(row, "sensor_raw_first", 0.0),
                sensor_raw_last=_row_float(row, "sensor_raw_last", 0.0),
                sensor_raw_drift=_row_float(row, "sensor_raw_drift", 0.0),
                sensor_raw_samples=_row_int(row, "sensor_raw_samples", 0),
                sensor_raw_series=row.get("sensor_raw_series", ""),

                system_liters_avg=_row_optional_float(row, "system_liters_avg"),
                system_liters_min=_row_optional_float(row, "system_liters_min"),
                system_liters_max=_row_optional_float(row, "system_liters_max"),
                system_liters_std=_row_optional_float(row, "system_liters_std"),
                system_liters_first=_row_optional_float(row, "system_liters_first"),
                system_liters_last=_row_optional_float(row, "system_liters_last"),
                system_liters_drift=_row_optional_float(row, "system_liters_drift"),
                system_liters_samples=_row_optional_int(row, "system_liters_samples"),
                system_liters_series=row.get("system_liters_series") or None,

                bridge_mixer_volume_liters=_row_float(
                    row, "bridge_mixer_volume_liters", BRIDGE_MIXER_VOLUME_LITERS
                ),
                bridge_sensor_liter_factor=_row_float(
                    row, "bridge_sensor_liter_factor", BRIDGE_MIXER_SENSOR_LITER_FACTOR
                ),
                bridge_sensor_liter_offset=_row_float(
                    row, "bridge_sensor_liter_offset", BRIDGE_MIXER_SENSOR_LITER_OFFSET
                ),
                bridge_sensor_calibration_status=row.get(
                    "bridge_sensor_calibration_status", BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS
                ),
                bridge_calculated_liters=_row_float(row, "bridge_calculated_liters", 0.0),
                bridge_error_liters=_row_float(row, "bridge_error_liters", 0.0),

                valid_for_fit=_row_bool(row, "valid_for_fit", True),
                invalid_reason=row.get("invalid_reason", "") or "",

                note=row.get("note", "") or "",
            )
        )

    return measurements
