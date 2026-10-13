"""Mixer-/RO-Sensorwerte aus einem Snapshot lesen, mit Fallback-Kette.

Reine Funktionen (keine Klassenmethoden) - brauchen nur die History/den
Settings-/Kalibrierungswert als Parameter, kein self.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from services.system_config import get_mixer_level_calibration

# Same calibration mqtt_sensor_bridge.py already applies to volume_liters_calc.
# Used below only as the fallback for the rare case where the bridge hasn't
# published a calibrated value yet.
_SYSTEM_MIXER_CALIBRATION = get_mixer_level_calibration()


def finite_number(value: Any) -> float | None:
    """Strict sensor/config number: a genuine int/float that is finite, as float.

    Returns None for everything else - None, bool (True would become 1.0),
    numeric strings ("12"), NaN and +/-Infinity (json.loads accepts NaN and
    Infinity, and every comparison against NaN is False, which would slip
    through min/max/watchdog checks). None means "not usable"; callers never
    guess a replacement.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # int too large for a float, e.g. 10**400
        return None
    return number if math.isfinite(number) else None


def _section(snapshot: Any, key: str) -> dict[str, Any]:
    """snapshot[key] if it is a dict, else {} - a malformed payload (string,
    list, ...) must read as "no value", not raise AttributeError."""
    if not isinstance(snapshot, dict):
        return {}
    section = snapshot.get(key)
    return section if isinstance(section, dict) else {}


def mixer_liters_from_snapshot(snapshot: dict[str, Any], settings: dict[str, Any]) -> float | None:
    mixer = _section(snapshot, "mixer")

    # Preferred value:
    # mqtt_sensor_bridge.py already publishes the calibrated liter value here.
    # A source that is present but invalid (NaN, string, bool, ...) is NOT
    # replaced by a less reliable fallback - the reading is simply unusable.
    value = mixer.get("volume_liters_calc")
    if value is not None:
        return finite_number(value)

    # Fallback: raw liter value, calibrated with the same factor/offset
    # the bridge uses (config/system_config.json), unless a process-
    # specific override is set in settings.
    raw_liters = mixer.get("volume_liters_raw")
    if raw_liters is not None:
        raw = finite_number(raw_liters)
        factor = finite_number(settings.get("mixer_sensor_liter_factor", _SYSTEM_MIXER_CALIBRATION["factor"]))
        offset = finite_number(settings.get("mixer_sensor_liter_offset", _SYSTEM_MIXER_CALIBRATION["offset"]))
        if raw is None or factor is None or offset is None:
            return None
        # Check the result is finite BEFORE clamping at zero: max(0.0, -inf)
        # would turn an overflow into a plausible-looking 0.0.
        calibrated = finite_number((raw * factor) + offset)
        if calibrated is None:
            return None
        return max(0.0, calibrated)

    # Last fallback: percent.
    # With the current bridge, level_percent is already calibrated.
    level_percent = mixer.get("level_percent")
    if level_percent is not None:
        percent = finite_number(level_percent)
        max_liters = finite_number(settings["max_mixer_liters"])
        if percent is None or max_liters is None:
            return None
        return finite_number((percent / 100.0) * max_liters)

    return None


def _mean(values: "deque[float]") -> float | None:
    """Arithmetic mean of finite values; None if it is not finite.

    The plain sum is used whenever it is finite (identical results to the
    previous implementation). Only if the sum overflows (two values of
    1e308) the mean is computed relative to the largest magnitude.
    """
    total = sum(values)
    if math.isfinite(total):
        return total / len(values)

    scale = max(abs(v) for v in values)
    if not math.isfinite(scale) or scale == 0.0:
        return None
    return finite_number(scale * (sum(v / scale for v in values) / len(values)))


def filtered_mixer_liters(
    history: "deque[float]", snapshot: dict[str, Any], settings: dict[str, Any]
) -> float | None:
    mixer_liters = mixer_liters_from_snapshot(snapshot, settings)

    if mixer_liters is None:
        return None

    history.append(mixer_liters)

    return _mean(history)


def ro_liters_from_snapshot(snapshot: dict[str, Any]) -> float | None:
    ro = _section(snapshot, "ro")

    # Same rule as for the mixer: an invalid preferred value is not masked
    # by the percent fallback.
    value = ro.get("volume_liters_calc")
    if value is not None:
        return finite_number(value)

    level_percent = ro.get("level_percent")
    configured_max_liters = ro.get("configured_max_liters")

    if level_percent is not None and configured_max_liters is not None:
        percent = finite_number(level_percent)
        max_liters = finite_number(configured_max_liters)
        if percent is None or max_liters is None:
            return None
        return finite_number((percent / 100.0) * max_liters)

    return None
