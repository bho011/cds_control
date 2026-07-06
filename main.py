#!/usr/bin/env python3
"""
Central entry point for the CDS control application.

Commands:
    python main.py preflight
    python main.py water-cycle
    python main.py safe-drain
    python main.py sensor-bridge-check
    python main.py gpio-check
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
PYTHON_EXECUTABLE = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)


def run_python_script(script_name: str) -> int:
    script_path = PROJECT_ROOT / script_name

    if not script_path.exists():
        print(f"[ERROR] Script not found: {script_path}")
        return 1

    print(f"[INFO] Python:  {PYTHON_EXECUTABLE}")
    print(f"[INFO] Running script: {script_name}")

    result = subprocess.run(
        [str(PYTHON_EXECUTABLE), str(script_path)],
        cwd=PROJECT_ROOT,
    )
    return result.returncode


def run_python_module(module_name: str) -> int:
    print(f"[INFO] Python:  {PYTHON_EXECUTABLE}")
    print(f"[INFO] Running module: {module_name}")

    result = subprocess.run(
        [str(PYTHON_EXECUTABLE), "-m", module_name],
        cwd=PROJECT_ROOT,
    )
    return result.returncode


def cmd_preflight() -> int:
    print("[INFO] Running combined CDS preflight.")
    print()

    preflight_result = run_python_script("preflight_check.py")
    if preflight_result != 0:
        print()
        print("[FAIL] Base preflight failed. GPIO conflict check skipped.")
        return preflight_result

    print()
    print("[INFO] Base preflight OK. Running GPIO conflict check.")
    gpio_result = run_python_script("scripts/check_gpio_conflicts.py")
    if gpio_result != 0:
        print()
        print("[FAIL] GPIO conflict check failed.")
        return gpio_result

    print()
    print("[OK] Combined CDS preflight successful.")
    return 0


def cmd_water_cycle() -> int:
    return run_python_module("process.water_cycle")


def cmd_safe_drain() -> int:
    return run_python_script("main_safe_drain.py")


def cmd_sensor_bridge_check() -> int:
    print("[INFO] Checking cds-sensor-bridge.service")
    result = subprocess.run(
        ["systemctl", "status", "cds-sensor-bridge.service", "--no-pager"],
        cwd=PROJECT_ROOT,
    )
    return result.returncode


def cmd_gpio_check() -> int:
    return run_python_script("scripts/check_gpio_conflicts.py")


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

    subparsers.add_parser(
        "gpio-check",
        help="Check whether Node-RED blocks Python-CDS GPIO pins",
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

    if args.command == "gpio-check":
        return cmd_gpio_check()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
