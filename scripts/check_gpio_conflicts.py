#!/usr/bin/env python3
"""
Check whether Node-RED GPIO helper processes block GPIO pins
that are required by the Python CDS process.

Allowed:
    GPIO12 -> RO-Machine / RO-Tank refill logic via Node-RED

Critical for Python CDS:
    GPIO20 -> mixer_refill_pump
    GPIO21 -> valve_0_drain
    GPIO22 -> contactor_2 / sensor circulation pump
    GPIO26 -> transfer_pump
"""

from __future__ import annotations

import re
import subprocess
import sys


CRITICAL_GPIO_PINS = {
    20: "mixer_refill_pump",
    21: "valve_0_drain",
    22: "contactor_2 / sensor_circulation_pump",
    26: "transfer_pump",
}

ALLOWED_NODE_RED_GPIO_PINS = {
    12: "RO-Machine / RO-Tank refill",
}


NRPIO_PATTERN = re.compile(
    r"(?P<pid>\d+)\s+.*node-red-node-pi-gpio/nrpio\.py\s+out\s+(?P<pin>\d+)\s+"
)


def get_process_lines() -> list[str]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        print("[FAIL] Could not read process list.")
        print(result.stderr.strip())
        return []

    return result.stdout.splitlines()


def main() -> int:
    print("CDS GPIO Conflict Check")
    print("=======================")
    print()

    process_lines = get_process_lines()

    node_red_gpio_processes = []

    for line in process_lines:
        if "node-red-node-pi-gpio/nrpio.py" not in line:
            continue

        match = NRPIO_PATTERN.search(line)
        if not match:
            node_red_gpio_processes.append(
                {
                    "pid": None,
                    "pin": None,
                    "line": line.strip(),
                    "parse_error": True,
                }
            )
            continue

        node_red_gpio_processes.append(
            {
                "pid": int(match.group("pid")),
                "pin": int(match.group("pin")),
                "line": line.strip(),
                "parse_error": False,
            }
        )

    if not node_red_gpio_processes:
        print("[OK] No Node-RED GPIO helper processes found.")
        return 0

    print("[INFO] Node-RED GPIO helper processes found:")
    for proc in node_red_gpio_processes:
        print(f"  {proc['line']}")

    print()

    blocking = []
    allowed = []
    unknown = []
    parse_errors = []

    for proc in node_red_gpio_processes:
        if proc["parse_error"]:
            parse_errors.append(proc)
            continue

        pin = proc["pin"]

        if pin in CRITICAL_GPIO_PINS:
            blocking.append(proc)
        elif pin in ALLOWED_NODE_RED_GPIO_PINS:
            allowed.append(proc)
        else:
            unknown.append(proc)

    for proc in allowed:
        pin = proc["pin"]
        print(f"[OK] GPIO{pin} used by Node-RED: {ALLOWED_NODE_RED_GPIO_PINS[pin]}")

    for proc in unknown:
        pin = proc["pin"]
        print(f"[WARN] GPIO{pin} used by Node-RED but not classified for CDS.")

    for proc in parse_errors:
        print(f"[WARN] Could not parse Node-RED GPIO process: {proc['line']}")

    if blocking:
        print()
        print("[FAIL] Node-RED blocks GPIO pins required by Python CDS:")

        for proc in blocking:
            pin = proc["pin"]
            print(
                f"  GPIO{pin} -> {CRITICAL_GPIO_PINS[pin]} "
                f"(PID {proc['pid']})"
            )

        print()
        print("Fix:")
        print("  Disable these GPIO nodes in Node-RED and deploy again.")
        print("  Or stop Node-RED before running Python CDS hardware processes.")
        return 2

    print()
    print("[OK] No critical Python-CDS GPIO pins are blocked by Node-RED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
