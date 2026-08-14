"""Prüft die interne Konsistenz von gpio_config.py (keine Node-RED-Konfliktprüfung -
das übernimmt scripts/check_gpio_conflicts.py separat)."""

from __future__ import annotations

from gpio_config import ACTIVE_LOW, OUTPUTS

from .report import PreflightReport

REQUIRED_GPIO_KEYS = [
    "mixer_refill_pump",
    "sensor_circulation_pump",
    "mixing_circulation_pump",
    "transfer_pump",
    "valve_0_drain",
]


def check_gpio_config(report: PreflightReport):
    if not isinstance(OUTPUTS, dict):
        report.fail("GPIO config", "OUTPUTS is not a dictionary")
        return

    missing_keys = [key for key in REQUIRED_GPIO_KEYS if key not in OUTPUTS]

    if missing_keys:
        report.fail("GPIO required keys", f"Missing: {', '.join(missing_keys)}")
    else:
        report.ok("GPIO required keys", ", ".join(REQUIRED_GPIO_KEYS))

    invalid_pins = {
        key: value
        for key, value in OUTPUTS.items()
        if not isinstance(value, int) or value < 0 or value > 27
    }

    if invalid_pins:
        report.warn("GPIO pin range", f"Check unusual GPIO values: {invalid_pins}")
    else:
        report.ok("GPIO pin range", "All configured GPIO pins are in BCM range 0-27")

    duplicate_pins: dict[int, list[str]] = {}

    for key, value in OUTPUTS.items():
        duplicate_pins.setdefault(value, []).append(key)

    duplicates = {
        pin: names
        for pin, names in duplicate_pins.items()
        if len(names) > 1
    }

    if duplicates:
        report.fail("GPIO duplicate pins", str(duplicates))
    else:
        report.ok("GPIO duplicate pins", "No duplicate GPIO assignments found")

    report.ok("GPIO active_low", f"ACTIVE_LOW={ACTIVE_LOW}")

    report.ok(
        "GPIO hardware safety",
        "No GPIO output was initialized or switched by this preflight check."
    )
