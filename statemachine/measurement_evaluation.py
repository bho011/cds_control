"""Soll-Ist-Bewertung der EC-/pH-Endmessung (reine Funktion, keine Hardware).

Sollwerte kommen ausschliesslich aus dem unveraenderlichen
recipe_run_config_snapshot, die Toleranzen aus den Prozesseinstellungen
(ec_tolerance_ms_cm, ph_tolerance). Es gibt bewusst KEINE Standardtoleranz: die
Werte sind fachlich noch nicht festgelegt (docs/CDS_EC_MIXING_PROCESS_CONCEPT.md
Abschnitt 6). Ist eine Toleranz nicht gesetzt, wird die jeweilige Groesse nicht
bewertet (NOT_EVALUATED, keine bestaetigte Sollwerterfuellung); ist sie gesetzt, aber ungueltig, oder fehlt dann der Sollwert bzw. der
Istwert, ist das Ergebnis ein Fehler (fail-closed). Es wird nichts korrigiert
oder geraten.

Die einzigen festen Grenzen sind physikalisch: EC >= 0 und 0 <= pH <= 14.
"""

from __future__ import annotations

from typing import Any

from statemachine.sensor_reading import _section, finite_number

PH_PHYSICAL_MIN = 0.0
PH_PHYSICAL_MAX = 14.0

STATUS_NOT_EVALUATED = "NOT_EVALUATED"
STATUS_OK = "OK"
STATUS_FAILED = "FAILED"


def overall_status(ok: bool, details: dict[str, Any]) -> str:
    """Gesamtstatus. OK nur, wenn EC und pH bewertet wurden und beide in
    Toleranz liegen; sonst gilt die Sollwerterfuellung nicht als bestaetigt."""
    if not ok:
        return STATUS_FAILED
    entries = [details.get(name) or {} for name, *_ in _QUANTITIES]
    if all(entry.get("status") == "ok" for entry in entries):
        return STATUS_OK
    return STATUS_NOT_EVALUATED

# (Name, Toleranz-Setting, Sollwert-Feld im Snapshot, Messwert-Feld, Einheit)
_QUANTITIES = (
    ("EC", "ec_tolerance_ms_cm", "target_ec_ms_cm", "ec_ms_cm", "mS/cm"),
    ("pH", "ph_tolerance", "target_ph", "ph", ""),
)


def _plausible(name: str, value: float) -> bool:
    if name == "EC":
        return value >= 0.0
    return PH_PHYSICAL_MIN <= value <= PH_PHYSICAL_MAX


def evaluate_final_values(
    settings: Any, sensor_snapshot: Any
) -> tuple[bool, str | None, str | None, dict[str, Any]]:
    """Rueckgabe: (ok, Meldung, Grund-Code, Details je Groesse).

    Grund-Codes: measurement_implausible, setpoint_invalid, tolerance_invalid,
    ec_out_of_tolerance, ph_out_of_tolerance, evaluation_failed.
    Details: {"EC": {"status": "ok"|"not_evaluated"|..., ...}, "pH": {...}}.
    """
    try:
        return _evaluate(settings, sensor_snapshot)
    except Exception as exc:  # fail-closed
        return False, f"Bewertung nicht moeglich ({type(exc).__name__}: {exc}).", "evaluation_failed", {}


def _evaluate(settings: Any, sensor_snapshot: Any) -> tuple[bool, str | None, str | None, dict[str, Any]]:
    settings = settings if isinstance(settings, dict) else {}
    run_snapshot = _section(settings, "recipe_run_config_snapshot")
    water = _section(sensor_snapshot, "water_values")
    details: dict[str, Any] = {}

    for name, tolerance_key, target_key, value_key, unit in _QUANTITIES:
        if tolerance_key not in settings:
            details[name] = {"status": STATUS_NOT_EVALUATED}
            continue

        tolerance = finite_number(settings[tolerance_key])
        if tolerance is None or tolerance <= 0.0:
            return False, f"{name}-Toleranz ({tolerance_key}) ist ungueltig.", "tolerance_invalid", details

        target = finite_number(run_snapshot.get(target_key))
        if target is None or not _plausible(name, target):
            return False, f"{name}-Sollwert ({target_key}) fehlt oder ist ungueltig.", "setpoint_invalid", details

        actual = finite_number(water.get(value_key))
        if actual is None or not _plausible(name, actual):
            return (
                False,
                f"{name}-Istwert fehlt oder ist unplausibel ({water.get(value_key)!r}).",
                "measurement_implausible",
                details,
            )

        deviation = actual - target
        entry = {
            "target": target,
            "actual": actual,
            "tolerance": tolerance,
            "deviation": deviation,
            "status": "ok" if abs(deviation) <= tolerance else "out_of_tolerance",
        }
        details[name] = entry
        if entry["status"] != "ok":
            return (
                False,
                f"{name} ausserhalb der Toleranz: Ist {actual} {unit}".rstrip()
                + f", Soll {target} +/- {tolerance} (Abweichung {deviation:+.3f}).",
                f"{name.lower()}_out_of_tolerance",
                details,
            )

    return True, None, None, details
