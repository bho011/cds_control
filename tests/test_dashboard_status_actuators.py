"""
nicegui_dashboard/pages/dashboard/status_actuators_section.py: das Panel
"Actuators / Outputs" zeigt Valve 1-4, aber keine Zeile fuer Valve 5.

Nur die Anzeige ist betroffen: Valve 5 wird vom Transfer weiterhin vor dem
Oeffnen des Zielventils geschlossen (PRE_TRANSFER_CLOSE_VALVES, abgesichert in
test_target_transfer.py). Kein GPIO/OPC-UA, kein Browser - die NiceGUI-Elemente
werden direkt aufgebaut, wie in test_dashboard_page_recipe_editor.py.
"""

from __future__ import annotations

import pytest
from nicegui import context, ui

from nicegui_dashboard.pages.dashboard.status_actuators_section import (
    build_status_actuators,
    refresh_status_actuators,
)

EXPECTED_ACTUATOR_ROWS = {
    "mixer_refill_pump": "Mixer Refill",
    "supply_valve_6": "Supply V6",
    "drain_valve_0": "Drain V0",
    "transfer_pump": "Transfer Pump",
    "mixing_circulation_pump": "Mixing Circ.",
    "sensor_circulation_pump": "Sensor Circ.",
    "valve_1": "Valve 1",
    "valve_2": "Valve 2",
    "valve_3": "Valve 3",
    "valve_4": "Valve 4",
}


def _build() -> tuple[dict, list[str]]:
    before_ids = set(context.client.elements)
    with ui.column():
        widgets = build_status_actuators()
    labels = [
        element.text
        for element_id, element in context.client.elements.items()
        if element_id not in before_ids and isinstance(element, ui.label)
    ]
    return widgets, labels


def test_actuator_panel_has_no_valve_5_row():
    widgets, labels = _build()

    assert list(widgets["actuator_rows"]) == list(EXPECTED_ACTUATOR_ROWS)
    assert "valve_5" not in widgets["actuator_rows"]
    assert "Valve 5" not in labels
    for title in EXPECTED_ACTUATOR_ROWS.values():
        assert title in labels


@pytest.mark.parametrize("with_valve_5", [True, False])
def test_refresh_updates_the_shown_rows_and_tolerates_valve_5_data(with_valve_5):
    widgets, _labels = _build()
    sensor_data = {"snapshot_available": True, "sensor_started": True, "bridge_error": None}
    process_data = {"payload_available": True, "reader_started": True}
    process_data.update({key: key == "transfer_pump" for key in EXPECTED_ACTUATOR_ROWS})
    if with_valve_5:
        process_data["valve_5"] = True  # die Steuerung liefert den Wert weiterhin, er wird nicht angezeigt

    refresh_status_actuators(widgets, sensor_data, process_data)

    badges = {key: row["badge"].text for key, row in widgets["actuator_rows"].items()}
    assert badges["transfer_pump"] == "ON"
    assert all(text == "OFF" for key, text in badges.items() if key != "transfer_pump")
