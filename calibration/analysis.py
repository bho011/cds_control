"""Offline-Auswertung (lineare Regression) der Kalibrier-Messpunkte, inkl. --analyze."""

from __future__ import annotations

import math
import statistics
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from .measurement_csv import load_csv
from .models import LinearFitResult, Measurement


def linear_regression(xs: Sequence[float], ys: Sequence[float], name: str) -> Optional[LinearFitResult]:
    if len(xs) < 2 or len(ys) < 2:
        return None

    if len(xs) != len(ys):
        raise ValueError("xs und ys müssen gleich lang sein.")

    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)

    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))

    if math.isclose(ss_xx, 0.0):
        return None

    slope = ss_xy / ss_xx
    offset = y_mean - slope * x_mean

    predictions = [slope * x + offset for x in xs]
    errors = [prediction - y for prediction, y in zip(predictions, ys)]

    ss_res = sum(error ** 2 for error in errors)
    ss_tot = sum((y - y_mean) ** 2 for y in ys)

    r2 = 1.0 - (ss_res / ss_tot) if not math.isclose(ss_tot, 0.0) else 1.0

    abs_errors = [abs(error) for error in errors]

    return LinearFitResult(
        name=name,
        slope=slope,
        offset=offset,
        r2=r2,
        max_abs_error_l=max(abs_errors),
        mean_abs_error_l=statistics.mean(abs_errors),
        points=len(xs),
    )


def build_fit(
    measurements: list[Measurement],
    phases: Optional[set[str]],
    name: str,
    min_reference_volume_l: float = 0.0,
) -> Optional[LinearFitResult]:
    selected = []

    for measurement in measurements:
        if phases is not None and measurement.phase not in phases:
            continue

        if not measurement.valid_for_fit:
            continue

        if measurement.reference_volume_l < min_reference_volume_l:
            continue

        if not math.isfinite(measurement.sensor_raw_avg):
            continue

        selected.append(measurement)

    xs = [measurement.sensor_raw_avg for measurement in selected]
    ys = [measurement.reference_volume_l for measurement in selected]

    return linear_regression(xs, ys, name)


def find_zero_raw(measurements: list[Measurement]) -> Optional[float]:
    """Average raw sensor value at this session's empty-tank zero point(s), if any."""
    zero_values = [
        measurement.sensor_raw_avg
        for measurement in measurements
        if measurement.phase in ("zero", "zero_after_drain") and measurement.valid_for_fit
    ]
    return statistics.mean(zero_values) if zero_values else None


def zero_normalize(measurements: list[Measurement], zero_raw: float) -> list[Measurement]:
    """
    Returns copies of the measurements with sensor_raw_avg shifted so this
    session's own zero point sits at 0. Lets fits pool raw values across
    calibration sessions whose absolute zero-point drifted (e.g. between
    two runs on different days), without touching the saved CSVs.
    """
    return [replace(measurement, sensor_raw_avg=measurement.sensor_raw_avg - zero_raw) for measurement in measurements]


def check_monotonic(measurements: list[Measurement], phase: str) -> list[str]:
    """
    Advisory warnings (not auto-exclusion) for points where the raw sensor
    value didn't increase even though reference_volume_l did, within one
    phase, in step order. A real hydrostatic sensor should be monotonic
    while filling or draining; a dip usually means a data entry error, a
    splash, or a sensor glitch worth reviewing by hand.
    """
    warnings: list[str] = []
    ordered = sorted(
        (measurement for measurement in measurements if measurement.phase == phase),
        key=lambda measurement: measurement.step,
    )

    for previous, current in zip(ordered, ordered[1:]):
        if current.reference_volume_l > previous.reference_volume_l and current.sensor_raw_avg <= previous.sensor_raw_avg:
            warnings.append(
                f"  [WARN] nicht-monoton in phase={phase}: step {previous.step}->{current.step}, "
                f"ref {previous.reference_volume_l:.1f}L->{current.reference_volume_l:.1f}L, "
                f"raw {previous.sensor_raw_avg:.3f}->{current.sensor_raw_avg:.3f}"
            )

    return warnings


def analyze_csv_files(paths: list[Path]) -> None:
    """
    Offline analysis of already-collected calibration CSVs. Never touches
    GPIO, OPC-UA, or any actuator - pure CSV parsing plus the same
    regression math used at the end of a live session.
    """
    print("============================================================")
    print("Kalibrier-Auswertung (offline, aus gespeicherten CSV-Dateien)")
    print("============================================================")

    raw_measurements: list[Measurement] = []
    normalized_measurements: list[Measurement] = []
    zero_points: dict[str, float] = {}

    for path in paths:
        measurements = load_csv(path)
        print(f"\n{path.name}: {len(measurements)} Messpunkte geladen.")

        for phase in ("fill", "drain"):
            for warning in check_monotonic(measurements, phase):
                print(warning)

        zero_raw = find_zero_raw(measurements)
        if zero_raw is not None:
            zero_points[path.name] = zero_raw
            normalized_measurements.extend(zero_normalize(measurements, zero_raw))
        else:
            print(f"  [WARN] Kein Nullpunkt in {path.name} gefunden, wird nicht zero-normalisiert.")
            normalized_measurements.extend(measurements)

        raw_measurements.extend(measurements)

    if len(zero_points) >= 2:
        print("\nNullpunkt-Vergleich zwischen Sessions (sollte bei stabilem Sensor nahe 0 liegen):")
        names = list(zero_points)
        for name in names:
            print(f"  {name}: raw={zero_points[name]:.3f}")
        for a, b in zip(names, names[1:]):
            print(f"  Delta {a} -> {b}: {zero_points[b] - zero_points[a]:+.3f}")

    print("\n--- Unveraenderte Rohdaten, wie am Ende einer Live-Session ---")
    analyze_measurements(raw_measurements)

    print("\n--- Zero-normalisiert und ueber alle Dateien gepoolt ---")
    print_fit_result(
        build_fit(normalized_measurements, phases={"zero", "fill"}, name="Zero-normalisiert, FILL, alle Dateien")
    )
    print_fit_result(
        build_fit(
            normalized_measurements,
            phases={"zero", "fill"},
            name="Zero-normalisiert, FILL, alle Dateien, ref >= 10L",
            min_reference_volume_l=10.0,
        )
    )
    print_fit_result(
        build_fit(
            normalized_measurements,
            phases=None,
            name="Zero-normalisiert, FILL+DRAIN, alle Dateien, ref >= 10L",
            min_reference_volume_l=10.0,
        )
    )


def print_fit_result(result: Optional[LinearFitResult]) -> None:
    if result is None:
        print("Keine ausreichenden Daten für diese Auswertung vorhanden.")
        return

    print(f"\n{result.name}")
    print("-" * len(result.name))
    print(f"Messpunkte:             {result.points}")
    print("Formel:")
    print(f"  Liter_real = {result.slope:.6f} * sensor_raw + {result.offset:.6f}")
    print(f"R² Linearität:          {result.r2:.6f}")
    print(f"Max. Fehler:            {result.max_abs_error_l:.3f} L")
    print(f"Mittlerer Fehler:       {result.mean_abs_error_l:.3f} L")

    if result.r2 >= 0.995 and result.max_abs_error_l <= 1.0:
        print("Bewertung:              sehr gut linear")
    elif result.r2 >= 0.985 and result.max_abs_error_l <= 2.0:
        print("Bewertung:              brauchbar linear")
    else:
        print("Bewertung:              auffällig - Interpolation/Kalibriertabelle prüfen")


def print_invalid_measurements(measurements: list[Measurement]) -> None:
    invalid = [measurement for measurement in measurements if not measurement.valid_for_fit]

    if not invalid:
        return

    print("\nAusgeschlossene Messpunkte:")
    for measurement in invalid:
        print(
            f"  phase={measurement.phase}, step={measurement.step}, "
            f"ref={measurement.reference_volume_l:.3f} L, "
            f"raw={measurement.sensor_raw_avg:.6f}, "
            f"reason={measurement.invalid_reason}"
        )


def analyze_measurements(measurements: list[Measurement]) -> None:
    print("\n\n============================================================")
    print("AUSWERTUNG")
    print("============================================================")

    if not measurements:
        print("Keine Messpunkte vorhanden.")
        return

    fill_fit = build_fit(
        measurements,
        phases={"zero", "fill"},
        name="Lineare Kalibrierung - Fill inklusive 0-L-Punkt",
    )

    drain_fit = build_fit(
        measurements,
        phases={"drain_start", "drain", "zero_after_drain"},
        name="Lineare Kalibrierung - Drain inklusive Drain-Start/0-L-Punkt",
    )

    all_fit = build_fit(
        measurements,
        phases=None,
        name="Lineare Kalibrierung - Gesamt",
    )

    print_fit_result(fill_fit)
    print_fit_result(drain_fit)
    print_fit_result(all_fit)

    print_invalid_measurements(measurements)

    print("\nHinweis:")
    print("  Die Formeln werden nur aus Messpunkten mit valid_for_fit=True berechnet.")
    print("  Die aktuelle MQTT-Bridge-Umrechnung wurde nur dokumentiert und nicht angewendet.")
    print("  Wenn Fill und Drain deutlich abweichen, besser Interpolation/Kalibriertabelle nutzen.")
