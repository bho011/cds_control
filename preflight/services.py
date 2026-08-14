"""Prüft, ob die erwarteten systemd-Dienste laufen."""

from __future__ import annotations

import subprocess

from .report import PreflightReport

SYSTEMD_SERVICES = [
    ("cds-sensor-bridge.service", True),
    ("nodered.service", False),
]


def run_command(command: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"Command not found: {command[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out: {' '.join(command)}"


def check_systemd_service(report: PreflightReport, service_name: str, required: bool):
    return_code, stdout, stderr = run_command(
        ["systemctl", "is-active", service_name],
        timeout=5,
    )

    if return_code == 0 and stdout == "active":
        report.ok(f"Systemd service: {service_name}", "active")
        return

    detail = stdout or stderr or f"systemctl returned {return_code}"

    if required:
        report.fail(f"Systemd service: {service_name}", detail)
    else:
        report.warn(f"Systemd service: {service_name}", detail)


def check_systemd_services(report: PreflightReport):
    for service_name, required in SYSTEMD_SERVICES:
        check_systemd_service(report, service_name, required)
