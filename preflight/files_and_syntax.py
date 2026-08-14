"""Prüft, dass die projektkritischen Dateien existieren und syntaktisch gültig sind."""

from __future__ import annotations

import py_compile
from pathlib import Path

from .report import PreflightReport

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CRITICAL_FILES = [
    "mqtt_sensor_bridge.py",
    "config/water_cycle_settings.json",
    "process/common.py",
    "process/refill.py",
    "process/sensor_circulation.py",
    "process/drain.py",
    "process/water_cycle.py",
    "gpio_config.py",
    "services/mqtt_publisher.py",
    "hardware/digital_output.py",
    "hardware/actuator_manager.py",
    "services/mqtt_topic_reader.py",
    "statemachine/fill_and_measure_state_machine.py",
    "config/process_settings.json",
    "services/process_run_logger.py",
    "main_refill_and_drain_test.py"
]


def check_project_files(report: PreflightReport):
    for relative_path in CRITICAL_FILES:
        path = PROJECT_ROOT / relative_path

        if path.exists():
            report.ok(f"File exists: {relative_path}")
        else:
            report.fail(f"File missing: {relative_path}", str(path))


def check_python_syntax(report: PreflightReport):
    for relative_path in CRITICAL_FILES:
        path = PROJECT_ROOT / relative_path

        if not path.exists():
            continue

        try:
            py_compile.compile(str(path), doraise=True)
            report.ok(f"Python syntax: {relative_path}")
        except py_compile.PyCompileError as exc:
            report.fail(f"Python syntax: {relative_path}", str(exc))
