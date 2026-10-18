"""Startfreigabe fuer einen Fill-and-Measure-Rezeptlauf (reine Funktion).

Keine Hardware, kein I/O, keine Mengenberechnung: Rezeptmengen kommen
ausschliesslich aus dem unveraenderlichen RunConfigSnapshot
(domain/recipe/models.py), Sensorwerte aus den gehaerteten Lesefunktionen
(statemachine/sensor_reading.py). Jede Pruefung ist fail-closed: fehlende,
ungueltige oder widerspruechliche Werte lehnen den Start ab, nichts wird
korrigiert oder geraten.

Die Betreiberbestaetigung "Mischtank entleert" ist kein Messnachweis. Der
Mixerwert dient nur als Veto gegen einen Widerspruch zur Bestaetigung
(MIXER_EMPTY_VETO_L); er ersetzt die Bestaetigung nie, weil die Kalibrierung
unterhalb von ca. 10 L nicht belastbar ist.

Nicht enthalten (AP-02C): erneute Sensorpruefung unmittelbar vor dem
Einschalten der Pumpe und laufende RO-Ueberwachung.
"""

from __future__ import annotations

import math
from typing import Any

from domain.recipe.limits import (
    MAX_PROCESS_VOLUME_L,
    MAX_RECIPE_RO_WATER_L,
    MIXER_EMPTY_VETO_L,
    RO_TANK_CAPACITY_L,
)
from statemachine.measurement_evaluation import ec_tolerance_problem
from statemachine.sensor_reading import (
    _section,
    finite_number,
    mixer_liters_from_snapshot,
    ro_liters_from_snapshot,
)

_TOLERANCE_L = 1e-6

# Anteil der bestaetigten Gesamtdosierung, ab dem die RO-Befuellung parallel zur Dosierung wieder startet.
# Steht ausschliesslich in config/process_settings.json (kein Wert im Code); 1.0 = keine Ueberlappung.
RESUME_PROGRESS_KEY = "nutrient_ro_resume_progress"


def nutrient_ro_resume_progress(settings: Any) -> float | None:
    """Gueltiger Wert (endliche Zahl, 0 < x <= 1; bool/String/None/NaN sind ungueltig) oder None."""
    value = finite_number(settings.get(RESUME_PROGRESS_KEY)) if isinstance(settings, dict) else None
    return value if value is not None and 0.0 < value <= 1.0 else None


def validate_dosing_run_settings(settings: Any) -> tuple[str, str] | None:
    """Einstellungen, die nur ein Lauf MIT Naehrstoffdosierung braucht, VOR jeder Hardwareausgabe pruefen.
    None = ok, sonst (Grund-Code, Meldung). Dieselben Regeln wie die Endbewertung bzw. die Laufzeitpruefung
    (valid_tolerance, nutrient_ro_resume_progress): ein Fehler soll vor der Dosierung auffallen, nicht danach.
    Beide Einstellungen sind fuer einen solchen Lauf Pflicht: ein fehlender Schluessel blockiert genauso wie ein
    ungueltiger Wert (Wasserlaeufe rufen diese Pruefung nicht auf)."""
    if nutrient_ro_resume_progress(settings) is None:
        raw = settings.get(RESUME_PROGRESS_KEY) if isinstance(settings, dict) else None
        return (
            "resume_progress_invalid",
            f"{RESUME_PROGRESS_KEY} fehlt oder ist ungueltig (erlaubt: Zahl groesser 0 bis 1, war: {raw!r}).",
        )
    problem = ec_tolerance_problem(settings)
    if problem is not None:
        return "tolerance_invalid", problem
    return None


def required_ro_liters(target_total_liters: float, mixer_start_liters: float) -> float:
    """RO-Bedarf im absoluten Fuellmodus: max(0, Ziel - Startstand)."""
    return max(0.0, target_total_liters - mixer_start_liters)


def validate_fill_start(
    settings: Any, sensor_snapshot: Any
) -> tuple[bool, str | None]:
    """Prueft die Startfreigabe. Rueckgabe: (True, None) oder (False, Meldung)."""
    ok, message, _reason = validate_fill_start_with_reason(settings, sensor_snapshot)
    return ok, message


def validate_fill_start_with_reason(
    settings: Any, sensor_snapshot: Any
) -> tuple[bool, str | None, str | None]:
    """Wie validate_fill_start, zusaetzlich mit maschinenlesbarem Grund-Code."""
    try:
        return _validate(settings, sensor_snapshot, runtime=False, mixer_filtered_l=None)
    except Exception as exc:  # fail-closed: nie unkontrolliert durchreichen
        return (
            False,
            f"Start blockiert: Startpruefung fehlgeschlagen ({type(exc).__name__}: {exc}).",
            "validation_exception",
        )


def validate_fill_runtime(
    settings: Any, sensor_snapshot: Any, mixer_filtered_l: float | None
) -> tuple[bool, str | None, str | None]:
    """Laufende Pruefung waehrend/nach der Befuellung (Pumpe an oder gerade aus).

    Gleiche Regeln wie die Startfreigabe (Bestaetigung, Zielkonsistenz,
    Sensor-/RO-Plausibilitaet, Kapazitaet, RO-Bedarf plus Reserve), aber ohne
    Leere-Veto und ohne "Ziel schon erreicht" - beides gilt nur vor dem
    Pumpenstart. Sicherheitsgrenzen nutzen den ungefilterten Mixerwert; fuer
    den RO-Bedarf gilt min(ungefiltert, gefiltert), sofern der gefilterte Wert
    gueltig ist.
    """
    try:
        ok, message, reason = _validate(
            settings, sensor_snapshot, runtime=True, mixer_filtered_l=mixer_filtered_l
        )
        if message:
            message = message.replace("Start blockiert:", "Lauf abgebrochen:", 1)
        return ok, message, reason
    except Exception as exc:
        return (
            False,
            f"Lauf blockiert: Laufpruefung fehlgeschlagen ({type(exc).__name__}: {exc}).",
            "validation_exception",
        )


def _block(reason: str, message: str) -> tuple[bool, str, str]:
    return False, f"Start blockiert: {message}", reason


def _validate(
    settings: Any, sensor_snapshot: Any, *, runtime: bool, mixer_filtered_l: float | None
) -> tuple[bool, str | None, str | None]:
    if not isinstance(settings, dict):
        return _block("run_config_invalid", "Prozesseinstellungen fehlen oder sind ungueltig.")

    run_snapshot = settings.get("recipe_run_config_snapshot")
    if not isinstance(run_snapshot, dict):
        return _block("run_config_invalid", "Rezept-RunConfig (recipe_run_config_snapshot) fehlt oder ist ungueltig.")

    # 1. Betreiberbestaetigung (strikt: nur echtes True)
    if run_snapshot.get("tank_emptied_confirmed") is not True:
        return _block("tank_not_confirmed",
            "Die Entleerung des Mischtanks wurde fuer diesen Lauf nicht bestaetigt."
        )

    # 10. Konsistenz der Zielwerte (Werte stammen aus der vorhandenen Rezeptberechnung)
    target_total = finite_number(settings.get("target_total_liters"))
    target_ro = finite_number(run_snapshot.get("target_ro_water_l"))
    estimated_volume = finite_number(run_snapshot.get("estimated_process_volume_l"))
    max_mixer = finite_number(run_snapshot.get("max_mixer_liters"))
    operative_max_mixer = finite_number(settings.get("max_mixer_liters"))
    reserve = finite_number(settings.get("min_ro_liters_required"))

    if settings.get("fill_mode") != "absolute":
        return _block("run_config_invalid", "Fuellmodus ist nicht 'absolute'; der Rezeptlauf erwartet einen absoluten Fuellstand.")
    if None in (target_total, target_ro, estimated_volume, max_mixer):
        return _block("run_config_invalid", "Zielmengen der RunConfig fehlen oder sind keine endlichen Zahlen.")
    if reserve is None or reserve < 0.0:
        return _block("run_config_invalid", "min_ro_liters_required fehlt oder ist ungueltig.")
    if target_total <= 0.0:
        return _block("run_config_invalid", "Rezept-Fuellziel muss groesser als 0 L sein.")
    if abs(target_total - target_ro) > _TOLERANCE_L:
        return _block("run_config_invalid",
            f"Widerspruechliche Zielmengen: Fuellziel {target_total:.2f} L "
            f"entspricht nicht der Rezept-RO-Menge {target_ro:.2f} L."
        )
    if target_ro > MAX_RECIPE_RO_WATER_L + _TOLERANCE_L:
        return _block("run_config_invalid",
            f"Rezept-RO-Menge {target_ro:.2f} L ueberschreitet das Maximum von {MAX_RECIPE_RO_WATER_L:.0f} L."
        )
    if estimated_volume + _TOLERANCE_L < target_ro or estimated_volume > MAX_PROCESS_VOLUME_L + _TOLERANCE_L:
        return _block("run_config_invalid",
            f"Geschaetztes Prozessvolumen {estimated_volume:.2f} L ist widerspruechlich "
            f"oder ueberschreitet {MAX_PROCESS_VOLUME_L:.0f} L."
        )
    if operative_max_mixer is None or operative_max_mixer <= 0.0 or operative_max_mixer > MAX_PROCESS_VOLUME_L + _TOLERANCE_L:
        return _block("run_config_invalid",
            f"max_mixer_liters der Einstellungen fehlt oder ist ungueltig (erlaubt: 0 bis {MAX_PROCESS_VOLUME_L:.0f} L)."
        )
    if max_mixer is not None and abs(operative_max_mixer - max_mixer) > _TOLERANCE_L:
        return _block("run_config_invalid",
            f"Widerspruechliche Mixergrenze: Einstellungen {operative_max_mixer:.2f} L, "
            f"Rezept-RunConfig {max_mixer:.2f} L."
        )
    if max_mixer > MAX_PROCESS_VOLUME_L + _TOLERANCE_L or max_mixer + _TOLERANCE_L < estimated_volume:
        return _block("run_config_invalid",
            f"max_mixer_liters ({max_mixer:.2f} L) ist ungueltig oder kleiner als das Prozessvolumen."
        )

    # 2. Sensor-Snapshot
    if not isinstance(sensor_snapshot, dict) or not sensor_snapshot:
        return _block("sensor_invalid", "Kein gueltiger Sensor-Snapshot vorhanden.")

    # 3./4./5./6. Mixerfuellstand
    mixer_l = mixer_liters_from_snapshot(sensor_snapshot, settings)
    if mixer_l is None:
        return _block("mixer_invalid", "Mixerfuellstand fehlt oder ist kein endlicher Zahlenwert.")
    if mixer_l < 0.0:
        return _block("mixer_implausible", f"Mixerfuellstand {mixer_l:.2f} L ist negativ und damit unplausibel.")
    if mixer_l > MAX_PROCESS_VOLUME_L:
        return _block("mixer_implausible",
            f"Mixerfuellstand {mixer_l:.2f} L liegt ueber der Prozessgrenze von {MAX_PROCESS_VOLUME_L:.0f} L."
        )
    if not runtime and mixer_l > MIXER_EMPTY_VETO_L:
        return _block("empty_veto",
            f"Mixerfuellstand {mixer_l:.2f} L liegt ueber {MIXER_EMPTY_VETO_L:.1f} L; der Mischtank "
            "ist offenbar nicht entleert. Hinweis: Die Sensorik ist unterhalb von ca. 10 L nicht belastbar."
        )
    if not runtime and mixer_l >= target_total:
        return _block("mixer_at_target",
            f"Mixerfuellstand {mixer_l:.2f} L liegt bereits auf oder ueber dem Rezeptziel {target_total:.2f} L."
        )

    # 7./8. RO-Fuellstand und gemeldete Kapazitaet
    ro_l = ro_liters_from_snapshot(sensor_snapshot)
    if ro_l is None:
        return _block("ro_invalid", "RO-Fuellstand fehlt oder ist kein endlicher Zahlenwert.")
    if ro_l < 0.0:
        return _block("ro_invalid", f"RO-Fuellstand {ro_l:.2f} L ist negativ und damit unplausibel.")

    capacity = finite_number(_section(sensor_snapshot, "ro").get("configured_max_liters"))
    if capacity is None or capacity <= 0.0:
        return _block("ro_capacity_invalid", "RO-Kapazitaet (configured_max_liters) fehlt oder ist ungueltig.")
    if abs(capacity - RO_TANK_CAPACITY_L) > _TOLERANCE_L:
        return _block("ro_capacity_invalid",
            f"Gemeldete RO-Kapazitaet {capacity:.1f} L widerspricht der erwarteten "
            f"CDS-Kapazitaet von {RO_TANK_CAPACITY_L:.0f} L."
        )
    if ro_l > capacity:
        return _block("ro_invalid", f"RO-Fuellstand {ro_l:.2f} L uebersteigt die RO-Kapazitaet {capacity:.0f} L.")

    # 9. RO-Bedarf inklusive Reserve
    mixer_for_need = mixer_l
    if runtime:
        filtered = finite_number(mixer_filtered_l)
        if filtered is not None:
            mixer_for_need = min(mixer_l, filtered)
    needed = required_ro_liters(target_total, mixer_for_need)
    if not math.isfinite(needed) or ro_l < needed + reserve:
        return _block("ro_insufficient",
            f"Unzureichender RO-Vorrat: verfuegbar {ro_l:.2f} L, benoetigt {needed:.2f} L "
            f"plus Reserve {reserve:.2f} L = {needed + reserve:.2f} L."
        )

    return True, None, None
