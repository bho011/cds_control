"""Datenmodelle für die Mixing-Tank-Kalibrierung."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SensorStats:
    avg: float
    min: float
    max: float
    std: float
    first: float
    last: float
    drift: float
    samples: int
    raw_series: str


@dataclass
class Measurement:
    timestamp: str
    phase: str
    step: int
    manual_added_l: float
    manual_drained_l: float
    reference_volume_l: float

    sensor_raw_avg: float
    sensor_raw_min: float
    sensor_raw_max: float
    sensor_raw_std: float
    sensor_raw_first: float
    sensor_raw_last: float
    sensor_raw_drift: float
    sensor_raw_samples: int
    sensor_raw_series: str

    system_liters_avg: Optional[float]
    system_liters_min: Optional[float]
    system_liters_max: Optional[float]
    system_liters_std: Optional[float]
    system_liters_first: Optional[float]
    system_liters_last: Optional[float]
    system_liters_drift: Optional[float]
    system_liters_samples: Optional[int]
    system_liters_series: Optional[str]

    bridge_mixer_volume_liters: float
    bridge_sensor_liter_factor: float
    bridge_sensor_liter_offset: float
    bridge_sensor_calibration_status: str
    bridge_calculated_liters: float
    bridge_error_liters: float

    valid_for_fit: bool
    invalid_reason: str

    note: str


@dataclass
class LinearFitResult:
    name: str
    slope: float
    offset: float
    r2: float
    max_abs_error_l: float
    mean_abs_error_l: float
    points: int
