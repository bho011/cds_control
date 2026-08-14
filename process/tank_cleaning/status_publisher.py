"""MQTT-Status-Payload für Tank Cleaning aufbauen und veröffentlichen."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def publish_status(controller, actuators, process_state: str) -> None:
    if controller._mqtt_publisher is None:
        return

    actuator_status: dict[str, Any] = {}
    if actuators is not None:
        try:
            actuator_status = actuators.status_payload()
        except Exception:
            actuator_status = {}

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": "python_nicegui",
        "process_state": process_state,
        "actuators": {
            "mixer_refill_pump": actuator_status.get("mixer_refill_pump", False),
            "transfer_pump": actuator_status.get("transfer_pump", False),
            "drain_valve_0": actuator_status.get("drain_valve_0", False),
            "mixing_circulation_pump": actuator_status.get("mixing_circulation_pump", False),
            "sensor_circulation_pump": actuator_status.get("sensor_circulation_pump", False),
        },
        "tank_cleaning": controller.get_status(),
        "error": controller._last_error,
    }

    try:
        controller._mqtt_publisher.publish_json(payload)
    except Exception:
        pass
