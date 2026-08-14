"""Orchestriert alle Preflight-Prüfungen und stellt den CLI-Einstiegspunkt bereit."""

from __future__ import annotations

import sys

from .disk_space import check_disk_space
from .files_and_syntax import check_project_files, check_python_syntax
from .gpio import check_gpio_config
from .mqtt import check_latest_mqtt_sensor_payload, check_mqtt_tcp
from .opcua import check_opcua_endpoint
from .report import PreflightReport
from .services import check_systemd_services


def run_preflight_report() -> PreflightReport:
    """
    Runs the full check sequence and returns the report without printing or
    exiting the process - used by main() below for the CLI entry point, and
    by the dashboard's "Run Preflight Check" button (which runs inside the
    long-lived NiceGUI process and must never call sys.exit()).
    """
    report = PreflightReport()

    check_project_files(report)
    check_python_syntax(report)
    check_disk_space(report)
    check_gpio_config(report)
    check_systemd_services(report)
    check_mqtt_tcp(report)
    check_opcua_endpoint(report)
    check_latest_mqtt_sensor_payload(report)

    return report


def main():
    print("Running CDS preflight checks...")
    print("No GPIO output will be initialized or switched.")
    print()

    report = run_preflight_report()
    report.print_report()

    if report.has_failures:
        sys.exit(1)

    sys.exit(0)
