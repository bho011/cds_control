"""System-Status- und Aktoren/Outputs-Panel: Verbindungsstatus-Zeilen + 11 Aktor-Badges."""

from __future__ import annotations

from typing import Any

from nicegui import ui

from nicegui_dashboard.components.formatting import bool_dot
from nicegui_dashboard.components.status_widgets import create_actuator_row, create_status_row


def build_status_actuators() -> dict[str, Any]:
    with ui.card().classes("panel"):
        ui.label("System Status").classes("panel-title")
        ui.label("Communication and process modules").classes(
            "panel-subtitle"
        )

        mqtt_bridge_row = create_status_row(
            "MQTT Sensor Bridge",
            "OPC-UA → MQTT → MqttTopicReader",
        )
        process_reader_row = create_status_row(
            "Process MQTT Reader",
            "reads cds/status/process",
        )
        snapshot_row = create_status_row(
            "Sensor Snapshot",
            "max. 5 seconds old",
        )
        process_payload_row = create_status_row(
            "Process Payload",
            "max. 30 seconds old",
        )

    with ui.card().classes("panel"):
        ui.label("Actuators / Outputs").classes("panel-title")

        # Dict key == the process_data key holding that actuator's state -
        # lets refresh_status_actuators() update all of them in one loop
        # instead of 11 near-identical lines.
        actuator_rows = {
            "mixer_refill_pump": create_actuator_row("Mixer Refill Pump"),
            "supply_valve_6": create_actuator_row("Supply Valve 6"),
            "drain_valve_0": create_actuator_row("Drain Valve 0"),
            "transfer_pump": create_actuator_row("Transfer Pump"),
            "mixing_circulation_pump": create_actuator_row("Mixing Circulation Pump"),
            "sensor_circulation_pump": create_actuator_row("Sensor Circulation Pump"),
            "valve_1": create_actuator_row("Valve 1"),
            "valve_2": create_actuator_row("Valve 2"),
            "valve_3": create_actuator_row("Valve 3"),
            "valve_4": create_actuator_row("Valve 4"),
            "valve_5": create_actuator_row("Valve 5"),
        }

    return {
        "mqtt_bridge_row": mqtt_bridge_row,
        "process_reader_row": process_reader_row,
        "snapshot_row": snapshot_row,
        "process_payload_row": process_payload_row,
        "actuator_rows": actuator_rows,
    }


def _set_dot(row: dict[str, Any], value: Any) -> None:
    row["dot"].classes(remove="dot-on dot-off dot-unknown")
    row["dot"].classes(bool_dot(value))


def _set_actuator(row: dict[str, Any], value: Any) -> None:
    badge = row["badge"]
    badge.classes(remove="badge-on badge-off badge-unknown")

    if value is True:
        badge.set_text("ON")
        badge.classes("badge-on")
    elif value is False:
        badge.set_text("OFF")
        badge.classes("badge-off")
    else:
        badge.set_text("-")
        badge.classes("badge-unknown")


def refresh_status_actuators(
    widgets: dict[str, Any], sensor_data: dict[str, Any], process_data: dict[str, Any]
) -> None:
    sensor_available = sensor_data["snapshot_available"]
    process_available = process_data["payload_available"]

    mqtt_bridge_ok = (
        sensor_data["sensor_started"]
        and sensor_available
        and not sensor_data["bridge_error"]
    )

    _set_dot(widgets["mqtt_bridge_row"], mqtt_bridge_ok)
    widgets["mqtt_bridge_row"]["value"].set_text("connected" if mqtt_bridge_ok else "check")

    _set_dot(widgets["process_reader_row"], process_data["reader_started"])
    widgets["process_reader_row"]["value"].set_text(
        "running" if process_data["reader_started"] else "stopped"
    )

    _set_dot(widgets["snapshot_row"], sensor_available)
    widgets["snapshot_row"]["value"].set_text("current" if sensor_available else "stale")

    _set_dot(widgets["process_payload_row"], process_available)
    widgets["process_payload_row"]["value"].set_text(
        "current" if process_available else "no payload"
    )

    for key, row in widgets["actuator_rows"].items():
        _set_actuator(row, process_data[key])
