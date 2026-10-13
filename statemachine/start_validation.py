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
from statemachine.sensor_reading import (
    _section,
    finite_number,
    mixer_liters_from_snapshot,
    ro_liters_from_snapshot,
)

_TOLERANCE_L = 1e-6


def required_ro_liters(target_total_liters: float, mixer_start_liters: float) -> float:
    """RO-Bedarf im absoluten Fuellmodus: max(0, Ziel - Startstand)."""
    return max(0.0, target_total_liters - mixer_start_liters)


def validate_fill_start(
    settings: Any, sensor_snapshot: Any
) -> tuple[bool, str | None]:
    """Prueft die Startfreigabe. Rueckgabe: (True, None) oder (False, Meldung)."""
    try:
        return _validate(settings, sensor_snapshot)
    except Exception as exc:  # fail-closed: nie unkontrolliert durchreichen
        return False, f"Start blockiert: Startpruefung fehlgeschlagen ({type(exc).__name__}: {exc})."


def _block(message: str) -> tuple[bool, str]:
    return False, f"Start blockiert: {message}"


def _validate(settings: Any, sensor_snapshot: Any) -> tuple[bool, str | None]:
    if not isinstance(settings, dict):
        return _block("Prozesseinstellungen fehlen oder sind ungueltig.")

    run_snapshot = settings.get("recipe_run_config_snapshot")
    if not isinstance(run_snapshot, dict):
        return _block("Rezept-RunConfig (recipe_run_config_snapshot) fehlt oder ist ungueltig.")

    # 1. Betreiberbestaetigung (strikt: nur echtes True)
    if run_snapshot.get("tank_emptied_confirmed") is not True:
        return _block(
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
        return _block("Fuellmodus ist nicht 'absolute'; der Rezeptlauf erwartet einen absoluten Fuellstand.")
    if None in (target_total, target_ro, estimated_volume, max_mixer):
        return _block("Zielmengen der RunConfig fehlen oder sind keine endlichen Zahlen.")
    if reserve is None or reserve < 0.0:
        return _block("min_ro_liters_required fehlt oder ist ungueltig.")
    if target_total <= 0.0:
        return _block("Rezept-Fuellziel muss groesser als 0 L sein.")
    if abs(target_total - target_ro) > _TOLERANCE_L:
        return _block(
            f"Widerspruechliche Zielmengen: Fuellziel {target_total:.2f} L "
            f"entspricht nicht der Rezept-RO-Menge {target_ro:.2f} L."
        )
    if target_ro > MAX_RECIPE_RO_WATER_L + _TOLERANCE_L:
        return _block(
            f"Rezept-RO-Menge {target_ro:.2f} L ueberschreitet das Maximum von {MAX_RECIPE_RO_WATER_L:.0f} L."
        )
    if estimated_volume + _TOLERANCE_L < target_ro or estimated_volume > MAX_PROCESS_VOLUME_L + _TOLERANCE_L:
        return _block(
            f"Geschaetztes Prozessvolumen {estimated_volume:.2f} L ist widerspruechlich "
            f"oder ueberschreitet {MAX_PROCESS_VOLUME_L:.0f} L."
        )
    if operative_max_mixer is None or operative_max_mixer <= 0.0 or operative_max_mixer > MAX_PROCESS_VOLUME_L + _TOLERANCE_L:
        return _block(
            f"max_mixer_liters der Einstellungen fehlt oder ist ungueltig (erlaubt: 0 bis {MAX_PROCESS_VOLUME_L:.0f} L)."
        )
    if max_mixer is not None and abs(operative_max_mixer - max_mixer) > _TOLERANCE_L:
        return _block(
            f"Widerspruechliche Mixergrenze: Einstellungen {operative_max_mixer:.2f} L, "
            f"Rezept-RunConfig {max_mixer:.2f} L."
        )
    if max_mixer > MAX_PROCESS_VOLUME_L + _TOLERANCE_L or max_mixer + _TOLERANCE_L < estimated_volume:
        return _block(
            f"max_mixer_liters ({max_mixer:.2f} L) ist ungueltig oder kleiner als das Prozessvolumen."
        )

    # 2. Sensor-Snapshot
    if not isinstance(sensor_snapshot, dict) or not sensor_snapshot:
        return _block("Kein gueltiger Sensor-Snapshot vorhanden.")

    # 3./4./5./6. Mixerfuellstand
    mixer_l = mixer_liters_from_snapshot(sensor_snapshot, settings)
    if mixer_l is None:
        return _block("Mixerfuellstand fehlt oder ist kein endlicher Zahlenwert.")
    if mixer_l < 0.0:
        return _block(f"Mixerfuellstand {mixer_l:.2f} L ist negativ und damit unplausibel.")
    if mixer_l > MAX_PROCESS_VOLUME_L:
        return _block(
            f"Mixerfuellstand {mixer_l:.2f} L liegt ueber der Prozessgrenze von {MAX_PROCESS_VOLUME_L:.0f} L."
        )
    if mixer_l > MIXER_EMPTY_VETO_L:
        return _block(
            f"Mixerfuellstand {mixer_l:.2f} L liegt ueber {MIXER_EMPTY_VETO_L:.1f} L; der Mischtank "
            "ist offenbar nicht entleert. Hinweis: Die Sensorik ist unterhalb von ca. 10 L nicht belastbar."
        )
    if mixer_l >= target_total:
        return _block(
            f"Mixerfuellstand {mixer_l:.2f} L liegt bereits auf oder ueber dem Rezeptziel {target_total:.2f} L."
        )

    # 7./8. RO-Fuellstand und gemeldete Kapazitaet
    ro_l = ro_liters_from_snapshot(sensor_snapshot)
    if ro_l is None:
        return _block("RO-Fuellstand fehlt oder ist kein endlicher Zahlenwert.")
    if ro_l < 0.0:
        return _block(f"RO-Fuellstand {ro_l:.2f} L ist negativ und damit unplausibel.")

    capacity = finite_number(_section(sensor_snapshot, "ro").get("configured_max_liters"))
    if capacity is None or capacity <= 0.0:
        return _block("RO-Kapazitaet (configured_max_liters) fehlt oder ist ungueltig.")
    if abs(capacity - RO_TANK_CAPACITY_L) > _TOLERANCE_L:
        return _block(
            f"Gemeldete RO-Kapazitaet {capacity:.1f} L widerspricht der erwarteten "
            f"CDS-Kapazitaet von {RO_TANK_CAPACITY_L:.0f} L."
        )
    if ro_l > capacity:
        return _block(f"RO-Fuellstand {ro_l:.2f} L uebersteigt die RO-Kapazitaet {capacity:.0f} L.")

    # 9. RO-Bedarf inklusive Reserve
    needed = required_ro_liters(target_total, mixer_l)
    if not math.isfinite(needed) or ro_l < needed + reserve:
        return _block(
            f"Unzureichender RO-Vorrat: verfuegbar {ro_l:.2f} L, benoetigt {needed:.2f} L "
            f"plus Reserve {reserve:.2f} L = {needed + reserve:.2f} L."
        )

    return True, None
