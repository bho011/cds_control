"""Calibration trial model and per-pump statistics.

Trennt bewusst drei Begriffe, die sonst leicht unter einem einzigen Feld
vermischt würden:

- firmware_ml_per_step_used: der Wert, der zum Zeitpunkt eines konkreten
  Trials tatsächlich als in der Firmware aktiv angenommen wurde - niemals
  automatisch ein vorheriger Kandidat. Für das `calibrate`-Kommando wird
  dieser Wert ausschließlich aus dem bestätigten Firmwareprofil des
  jeweiligen Controllers/der jeweiligen Pumpe aufgelöst (siehe
  services/peristaltic/firmware_profiles.py::resolve_firmware_ml_per_step) -
  dose_limits.py::FIRMWARE_DEFAULT_ML_PER_STEP ist nur noch ein interner
  Rückfallwert für calibration_storage.py::add_trial()-Aufrufe ohne
  explizit übergebenen Wert (z.B. in Tests) und wird vom `calibrate`-Kommando
  nicht mehr verwendet.
- candidate_ml_per_step: aus Messungen berechneter Vorschlag, wird nie
  automatisch als neuer firmware_ml_per_step_used übernommen.
- verified_ml_per_step: bleibt in dieser Aufgabe null (kein
  verify-Workflow implementiert, nur vorbereitet).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from .dose_limits import CalibrationValidationError

_REQUESTED_ML_COMPARISON_DECIMALS = 3    # wie ML_ROUNDING_DECIMALS im übrigen Repo
_FIRMWARE_VALUE_COMPARISON_DECIMALS = 9  # genug Präzision für ml_per_step-Werte wie 0.000095548


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
