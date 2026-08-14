#!/usr/bin/env python3
"""Compatibility wrapper: preflight_check.py wurde in das preflight/-Package aufgeteilt.

# Re-exports: hält "from preflight_check import Y" funktionsfähig (u.a.
# nicegui_dashboard/cds_controller.py::run_preflight_report, per echtem
# In-Process-Import für den "Run Preflight Check"-Button im Dashboard).
# Der eigentliche Inhalt lebt jetzt in preflight/ (siehe Modularisierungs-
# Plan, Phase 4b). Direkt ausführbar wie zuvor: python preflight_check.py
"""

from __future__ import annotations

from preflight.disk_space import MAX_DISK_USAGE_PERCENT, MIN_FREE_DISK_MB, check_disk_space
from preflight.files_and_syntax import CRITICAL_FILES, PROJECT_ROOT, check_project_files, check_python_syntax
from preflight.gpio import REQUIRED_GPIO_KEYS, check_gpio_config
from preflight.mqtt import (
    MQTT_HOST,
    MQTT_PAYLOAD_TIMEOUT_SECONDS,
    MQTT_PORT,
    MQTT_QOS,
    MQTT_TOPIC,
    check_latest_mqtt_sensor_payload,
    check_mqtt_tcp,
    validate_sensor_payload,
)
from preflight.opcua import OPCUA_ENDPOINT, OPCUA_TIMEOUT_SECONDS, check_opcua_endpoint, check_opcua_endpoint_async
from preflight.report import CheckResult, PreflightReport
from preflight.run import main, run_preflight_report
from preflight.services import SYSTEMD_SERVICES, check_systemd_service, check_systemd_services, run_command

__all__ = [
    "CheckResult",
    "PreflightReport",
    "PROJECT_ROOT",
    "CRITICAL_FILES",
    "REQUIRED_GPIO_KEYS",
    "SYSTEMD_SERVICES",
    "MIN_FREE_DISK_MB",
    "MAX_DISK_USAGE_PERCENT",
    "MQTT_PAYLOAD_TIMEOUT_SECONDS",
    "OPCUA_TIMEOUT_SECONDS",
    "OPCUA_ENDPOINT",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_TOPIC",
    "MQTT_QOS",
    "run_command",
    "check_project_files",
    "check_python_syntax",
    "check_disk_space",
    "check_systemd_service",
    "check_systemd_services",
    "check_mqtt_tcp",
    "check_gpio_config",
    "check_opcua_endpoint_async",
    "check_opcua_endpoint",
    "validate_sensor_payload",
    "check_latest_mqtt_sensor_payload",
    "run_preflight_report",
    "main",
]

if __name__ == "__main__":
    main()
