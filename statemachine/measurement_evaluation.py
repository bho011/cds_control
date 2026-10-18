"""Soll-Ist-Bewertung der EC-/pH-Endmessung (reine Funktion, keine Hardware).

Sollwerte kommen ausschliesslich aus dem unveraenderlichen
recipe_run_config_snapshot, die Toleranzen aus den Prozesseinstellungen
(ec_tolerance_ms_cm, ph_tolerance). Es gibt bewusst KEINE Standardtoleranz: die
Werte sind fachlich noch nicht festgelegt (docs/CDS_EC_MIXING_PROCESS_CONCEPT.md
Abschnitt 6). Ist eine Toleranz nicht gesetzt, wird die jeweilige Groesse nicht
bewertet (NOT_EVALUATED, keine bestaetigte Sollwerterfuellung); ist sie gesetzt, aber ungueltig, oder fehlt dann der Sollwert bzw. der
Istwert, ist das Ergebnis ein Fehler (fail-closed). Es wird nichts korrigiert
oder geraten.

Ausnahme: Bei Laeufen MIT Naehrstoffdosierung ist ein fehlender Schluessel
ec_tolerance_ms_cm ein Konfigurationsfehler und blockiert den Start vor jeder
Hardwareausgabe (ec_tolerance_problem, aufgerufen von
start_validation.validate_dosing_run_settings). Diese Endbewertung selbst bleibt
unveraendert.

Fuer pH gibt es alternativ ein Zulaessigkeitsband (ph_min/ph_max, Grenzen
eingeschlossen, beide Pflicht, nicht zusammen mit ph_tolerance): der Rezept-
Sollwert ist dann nur Anzeige. EC bleibt unveraendert Soll +/- Toleranz.

EC wird nur bei Rezeptlaeufen MIT Naehrstoffdosierung bewertet
(recipe_run_config_snapshot.nutrient_dosing_enabled): ein reiner Wasserlauf kann
den EC-Sollwert nicht erreichen und bekommt fuer EC NOT_EVALUATED (kein Fehler,
aber auch keine bestaetigte Sollwerterfuellung). pH ist davon nicht betroffen.

Der Toleranzvergleich ist ein exakter Dezimalvergleich (Decimal(repr(x))): so
gilt Soll 1.5, Ist 1.6, Toleranz 0.1 als innerhalb, obwohl abs(1.6 - 1.5) in
Gleitkommazahlen 0.10000000000000009 ergibt. Es gibt keine Zusatztoleranz.

Die einzigen festen Grenzen sind physikalisch: EC >= 0 und 0 <= pH <= 14.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from statemachine.sensor_reading import _section, finite_number

PH_PHYSICAL_MIN = 0.0
PH_PHYSICAL_MAX = 14.0

PH_MIN_KEY = "ph_min"
PH_MAX_KEY = "ph_max"
EC_TOLERANCE_KEY = "ec_tolerance_ms_cm"

STATUS_NOT_EVALUATED = "NOT_EVALUATED"
STATUS_OK = "OK"
STATUS_FAILED = "FAILED"

REASON_NO_DOSING = "no_nutrient_dosing"


def valid_tolerance(value: Any) -> float | None:
    """Konfigurierte Toleranz: echte endliche Zahl groesser 0 (bool, String, None, NaN/Inf und <= 0 sind
    ungueltig), sonst None. Einzige Pruefregel fuer die Endbewertung UND die Vorabpruefung beim Start."""
    tolerance = finite_number(value)
    return tolerance if tolerance is not None and tolerance > 0.0 else None


def ec_tolerance_problem(settings: Any) -> str | None:
    """Meldung, wenn die EC-Toleranz fehlt oder ungueltig ist, sonst None. Nur Laeufe MIT Naehrstoffdosierung rufen
    die Pruefung auf (start_validation.validate_dosing_run_settings): dort ist ein fehlender Schluessel ein
    Konfigurationsfehler, und der Lauf scheitert vor jeder Hardwareausgabe statt erst nach der Dosierung.
    Wasserlaeufe pruefen die Toleranz nie; fuer sie bleibt EC NOT_EVALUATED."""
    if not isinstance(settings, dict) or EC_TOLERANCE_KEY not in settings:
        return f"EC-Toleranz ({EC_TOLERANCE_KEY}) fehlt (Pflicht fuer Laeufe mit Naehrstoffdosierung)."
    if valid_tolerance(settings[EC_TOLERANCE_KEY]) is None:
        return (
            f"EC-Toleranz ({EC_TOLERANCE_KEY}) ist ungueltig "
            f"(erlaubt: Zahl groesser 0, war: {settings[EC_TOLERANCE_KEY]!r})."
        )
    return None


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


def _evaluate_ph_band(
    settings: dict[str, Any], run_snapshot: Any, water: Any, details: dict[str, Any]
) -> tuple[str, str] | None:
    """pH-Zulaessigkeitsband ph_min..ph_max (Grenzen eingeschlossen) statt
    symmetrischer Toleranz um den Rezept-Sollwert. Rueckgabe None = bewertet
    (Eintrag in details), sonst (Meldung, Grund-Code) eines Fehlers. Beide
    Grenzen sind Pflicht, ph_tolerance darf nicht zusaetzlich gesetzt sein
    (mehrdeutig). Der Rezept-Sollwert ist nur Anzeige und entscheidet nicht."""
    if "ph_tolerance" in settings:
        return (
            f"pH-Band ({PH_MIN_KEY}/{PH_MAX_KEY}) und ph_tolerance duerfen nicht gleichzeitig gesetzt sein.",
            "tolerance_invalid",
        )
    low = finite_number(settings.get(PH_MIN_KEY))
    high = finite_number(settings.get(PH_MAX_KEY))
    if (
        low is None
        or high is None
        or not (_plausible("pH", low) and _plausible("pH", high))
        or low >= high
    ):
        return (
            f"pH-Band ({PH_MIN_KEY}/{PH_MAX_KEY}) ist unvollstaendig oder ungueltig.",
            "tolerance_invalid",
        )

    actual = finite_number(water.get("ph"))
    if actual is None or not _plausible("pH", actual):
        return f"pH-Istwert fehlt oder ist unplausibel ({water.get('ph')!r}).", "measurement_implausible"

    entry: dict[str, Any] = {
        "ph_min": low,
        "ph_max": high,
        "actual": actual,
        "status": "ok" if low <= actual <= high else "out_of_tolerance",
    }
    target = finite_number(run_snapshot.get("target_ph"))
    if target is not None:
        entry["target"] = target
    details["pH"] = entry
    if entry["status"] != "ok":
        return f"pH ausserhalb des Bands: Ist {actual}, erlaubt {low} bis {high}.", "ph_out_of_tolerance"
    return None


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
        if name == "pH" and (PH_MIN_KEY in settings or PH_MAX_KEY in settings):
            failure = _evaluate_ph_band(settings, run_snapshot, water, details)
            if failure is not None:
                return (False, *failure, details)
            continue

        if tolerance_key not in settings:
            details[name] = {"status": STATUS_NOT_EVALUATED}
            continue

        if name == "EC" and run_snapshot.get("nutrient_dosing_enabled") is not True:
            # Reiner Wasserlauf: der EC-Sollwert ist ohne Naehrstoffe unerreichbar. Keine Bewertung und kein
            # Fehler, aber auch keine bestaetigte Sollwerterfuellung (Gesamtstatus NOT_EVALUATED).
            details[name] = {"status": STATUS_NOT_EVALUATED, "reason": REASON_NO_DOSING}
            continue

        tolerance = valid_tolerance(settings[tolerance_key])
        if tolerance is None:
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

        # Exakter Dezimalvergleich: repr(float) ist die kuerzeste Dezimaldarstellung, die den Wert eindeutig
        # bestimmt, also genau das, was geschrieben bzw. gemessen wurde (Grenzen sind eingeschlossen).
        difference = Decimal(repr(actual)) - Decimal(repr(target))
        deviation = float(difference)
        entry = {
            "target": target,
            "actual": actual,
            "tolerance": tolerance,
            "deviation": deviation,
            "status": "ok" if abs(difference) <= Decimal(repr(tolerance)) else "out_of_tolerance",
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
