#!/usr/bin/env python3
"""
Central entry point for the CDS control application.

This file is intentionally small.
It starts validated routines from one central place.

Current commands:
    python main.py preflight
    python main.py water-cycle
    python main.py safe-drain
    python main.py sensor-bridge-check
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def run_python_script(script_name: str) -> int:
    """
    Run an existing Python script with the current Python interpreter.
    This keeps the currently validated scripts usable while main.py becomes
    the central entry point.
    """
    script_path = PROJECT_ROOT / script_name

    if not script_path.exists():
        print(f"[ERROR] Script not found: {script_path}")
        return 1

    print(f"[INFO] Running: {script_name}")
    result = subprocess.run([sys.executable, str(script_path)], cwd=PROJECT_ROOT)
    return result.returncode


def cmd_preflight() -> int:
    """
    Run the CDS preflight check.
    """
    return run_python_script("preflight_check.py")


def cmd_water_cycle() -> int:
    """
    Run the currently validated refill -> sensorbox -> drain test process.

    This is still based on main_refill_and_drain_test.py.
    Later, this logic should be moved into proper process modules.
    """
    return run_python_script("main_refill_and_drain_test.py")


def cmd_safe_drain() -> int:
    """
    Run the manual safe drain tool.
    """
    return run_python_script("main_safe_drain.py")


def cmd_sensor_bridge_check() -> int:
    """
    Show current sensor bridge service status.
    """
    print("[INFO] Checking cds-sensor-bridge.service")
    result = subprocess.run(
        ["systemctl", "status", "cds-sensor-bridge.service", "--no-pager"],
        cwd=PROJECT_ROOT,
    )
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Central Dosing System control entry point"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "preflight",
        help="Run preflight checks",
    )

    subparsers.add_parser(
        "water-cycle",
        help="Run refill -> sensorbox -> drain process",
    )

    subparsers.add_parser(
        "safe-drain",
        help="Run manual safe drain tool",
    )

    subparsers.add_parser(
        "sensor-bridge-check",
        help="Show sensor bridge service status",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "preflight":
        return cmd_preflight()

    if args.command == "water-cycle":
        return cmd_water_cycle()

    if args.command == "safe-drain":
        return cmd_safe_drain()

    if args.command == "sensor-bridge-check":
        return cmd_sensor_bridge_check()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
