"""Mixer-/RO-Sensorwerte aus einem Snapshot lesen, mit Fallback-Kette.

Reine Funktionen (keine Klassenmethoden) - brauchen nur die History/den
Settings-/Kalibrierungswert als Parameter, kein self.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from services.system_config import get_mixer_level_calibration

# Same calibration mqtt_sensor_bridge.py already applies to volume_liters_calc.
# Used below only as the fallback for the rare case where the bridge hasn't
# published a calibrated value yet.
_SYSTEM_MIXER_CALIBRATION = get_mixer_level_calibration()


def mixer_liters_from_snapshot(snapshot: dict[str, Any], settings: dict[str, Any]) -> float | None:
    mixer = snapshot.get("mixer") or {}

    # Preferred value:
    # mqtt_sensor_bridge.py already publishes the calibrated liter value here.
    value = mixer.get("volume_liters_calc")
    if value is not None:
        return float(value)

    # Fallback: raw liter value, calibrated with the same factor/offset
    # the bridge uses (config/system_config.json), unless a process-
    # specific override is set in settings.
    raw_liters = mixer.get("volume_liters_raw")
    if raw_liters is not None:
        factor = float(settings.get("mixer_sensor_liter_factor", _SYSTEM_MIXER_CALIBRATION["factor"]))
        offset = float(settings.get("mixer_sensor_liter_offset", _SYSTEM_MIXER_CALIBRATION["offset"]))
        return max(0.0, (float(raw_liters) * factor) + offset)

    # Last fallback: percent.
    # With the current bridge, level_percent is already calibrated.
    level_percent = mixer.get("level_percent")
    if level_percent is not None:
        max_liters = float(settings["max_mixer_liters"])
        return (float(level_percent) / 100.0) * max_liters

    return None


def filtered_mixer_liters(
    history: "deque[float]", snapshot: dict[str, Any], settings: dict[str, Any]
) -> float | None:
    mixer_liters = mixer_liters_from_snapshot(snapshot, settings)

    if mixer_liters is None:
        return None

    history.append(mixer_liters)

    return sum(history) / len(history)


def ro_liters_from_snapshot(snapshot: dict[str, Any]) -> float | None:
    ro = snapshot.get("ro") or {}

    value = ro.get("volume_liters_calc")
    if value is not None:
        return float(value)

    level_percent = ro.get("level_percent")
    configured_max_liters = ro.get("configured_max_liters")

    if level_percent is not None and configured_max_liters is not None:
        return (float(level_percent) / 100.0) * float(configured_max_liters)

    return None
