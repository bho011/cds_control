"""Compatibility shim: services/peristaltic/calibration.py wurde aufgeteilt.

# Re-exports: hält "from services.peristaltic.calibration import Y" nach
# dem Datei-Split funktionsfähig. Der eigentliche Inhalt lebt jetzt in
# dose_limits.py, calibration_stats.py, calibration_storage.py und
# parallel_pump_safety.py (siehe Modularisierungs-Plan, Phase 2a).
"""

from __future__ import annotations

from .calibration_stats import (
    CalibrationTrial,
    PumpCalibrationStats,
    candidate_ml_per_step,
    compute_pump_stats,
)
from .calibration_storage import (
    add_trial,
    default_calibration_data,
    has_volumetric_calibration,
    load_calibration_data,
    save_calibration_data,
)
from .dose_limits import (
    DEFAULT_PRIMING_CHUNK_ML,
    FIRMWARE_DEFAULT_ML_PER_STEP,
    MAX_INITIAL_TEST_DOSE_ML,
    MAX_PRIMING_TOTAL_ML,
    CalibrationValidationError,
    compute_priming_chunks,
    validate_dose_ml,
    validate_priming_request,
)
from .parallel_pump_safety import ALLOWED_PARALLEL_PUMP_GROUPS, validate_parallel_pump_selection

__all__ = [
    "FIRMWARE_DEFAULT_ML_PER_STEP",
    "MAX_INITIAL_TEST_DOSE_ML",
    "MAX_PRIMING_TOTAL_ML",
    "DEFAULT_PRIMING_CHUNK_ML",
    "CalibrationValidationError",
    "validate_dose_ml",
    "validate_priming_request",
    "compute_priming_chunks",
    "candidate_ml_per_step",
    "CalibrationTrial",
    "PumpCalibrationStats",
    "compute_pump_stats",
    "default_calibration_data",
    "load_calibration_data",
    "has_volumetric_calibration",
    "save_calibration_data",
    "add_trial",
    "ALLOWED_PARALLEL_PUMP_GROUPS",
    "validate_parallel_pump_selection",
]
