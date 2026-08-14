"""Compatibility wrapper: calibration_mixing_tank.py wurde in das calibration/-Package aufgeteilt.

# Re-exports: hält "from calibration_mixing_tank import Y" funktionsfähig
# (u.a. tests/test_settings_validation.py::CALIBRATION_SETTINGS_SCHEMA, per
# echtem Import). Der eigentliche Inhalt lebt jetzt in calibration/ (siehe
# Modularisierungs-Plan, Phase 4a). Direkt ausführbar wie zuvor:
# python calibration_mixing_tank.py [--analyze CSV ...]
"""

from __future__ import annotations

from calibration.analysis import (
    analyze_csv_files,
    analyze_measurements,
    build_fit,
    check_monotonic,
    find_zero_raw,
    linear_regression,
    print_fit_result,
    print_invalid_measurements,
    zero_normalize,
)
from calibration.cli import main
from calibration.cli_input import ask_float, ask_yes_no, parse_float_input
from calibration.config import (
    BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS,
    BRIDGE_MIXER_SENSOR_LITER_FACTOR,
    BRIDGE_MIXER_SENSOR_LITER_OFFSET,
    BRIDGE_MIXER_VOLUME_LITERS,
    CALIBRATION_SETTINGS_SCHEMA,
    DATA_DIR,
    DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS,
    DEFAULT_CALIBRATION_FILL_MAX_SECONDS,
    DEFAULT_FILL_STEP_L,
    MANUAL_FILL_TARGET_L,
    MIXER_RAW_NODE_ID,
    MIXER_SYSTEM_LITERS_NODE_ID,
    OPCUA_ENDPOINT,
    PUMP_CONTROL_ENABLED,
    PUMP_DRAIN_STEP_L,
    PUMP_FILL_FIRST_STEP_L,
    PUMP_FILL_STEP_L,
    PUMP_FILL_TARGET_L,
    SAMPLES_PER_MEASUREMENT,
    SETTINGS_PATH,
    SETTLING_TIME_S,
    TRACE_LOG_INTERVAL_S,
    load_settings,
)
from calibration.measurement_csv import (
    ask_measurement_validity,
    create_measurement,
    load_csv,
    print_measurement_summary,
    save_csv,
)
from calibration.models import LinearFitResult, Measurement, SensorStats
from calibration.pump_segments import (
    append_trace_row,
    build_pump_drain_checkpoints,
    build_pump_fill_checkpoints,
    create_calibration_pump_actuators,
    open_trace_writer,
    run_drain_pump_segment,
    run_fill_pump_segment,
    shutdown_actuators,
    trace_csv_path,
)
from calibration.sensor_reading import calc_stats, capture_measurement, collect_sensor_stats, countdown, read_node_float, to_float
from calibration.session import run_calibration

__all__ = [
    "SensorStats",
    "Measurement",
    "LinearFitResult",
    "OPCUA_ENDPOINT",
    "MIXER_RAW_NODE_ID",
    "MIXER_SYSTEM_LITERS_NODE_ID",
    "MANUAL_FILL_TARGET_L",
    "DEFAULT_FILL_STEP_L",
    "PUMP_FILL_FIRST_STEP_L",
    "PUMP_FILL_STEP_L",
    "PUMP_FILL_TARGET_L",
    "PUMP_DRAIN_STEP_L",
    "SAMPLES_PER_MEASUREMENT",
    "SETTLING_TIME_S",
    "TRACE_LOG_INTERVAL_S",
    "DATA_DIR",
    "SETTINGS_PATH",
    "PUMP_CONTROL_ENABLED",
    "DEFAULT_CALIBRATION_FILL_MAX_SECONDS",
    "DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS",
    "BRIDGE_MIXER_VOLUME_LITERS",
    "BRIDGE_MIXER_SENSOR_LITER_FACTOR",
    "BRIDGE_MIXER_SENSOR_LITER_OFFSET",
    "BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS",
    "CALIBRATION_SETTINGS_SCHEMA",
    "load_settings",
    "parse_float_input",
    "ask_float",
    "ask_yes_no",
    "to_float",
    "read_node_float",
    "calc_stats",
    "countdown",
    "collect_sensor_stats",
    "capture_measurement",
    "create_calibration_pump_actuators",
    "shutdown_actuators",
    "trace_csv_path",
    "open_trace_writer",
    "append_trace_row",
    "build_pump_fill_checkpoints",
    "build_pump_drain_checkpoints",
    "run_fill_pump_segment",
    "run_drain_pump_segment",
    "create_measurement",
    "print_measurement_summary",
    "ask_measurement_validity",
    "save_csv",
    "load_csv",
    "linear_regression",
    "build_fit",
    "find_zero_raw",
    "zero_normalize",
    "check_monotonic",
    "analyze_csv_files",
    "print_fit_result",
    "print_invalid_measurements",
    "analyze_measurements",
    "run_calibration",
    "main",
]

if __name__ == "__main__":
    main()
