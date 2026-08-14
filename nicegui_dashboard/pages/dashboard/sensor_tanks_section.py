"""Sensor-Werte- und Tank-Panel: pH/EC/Temperatur/DO-Metrikboxen + RO-/Mixing-Tank-Gauges."""

from __future__ import annotations

from typing import Any

from nicegui import ui

from nicegui_dashboard.components.formatting import fmt, percent_to_display
from nicegui_dashboard.components.status_widgets import create_metric_box, create_tank_gauge


def build_sensor_tanks() -> dict[str, Any]:
    with ui.card().classes("panel sensor-panel"):
        ui.label("Sensor Values").classes("panel-title")
        #ui.label("pH, EC, temperature and dissolved oxygen").classes(
        #    "panel-subtitle"
        #)

        with ui.row().classes("w-full gap-3"):
            ph_metric = create_metric_box("pH")
            ec_metric = create_metric_box("EC", "mS/cm")

        with ui.row().classes("w-full gap-3"):
            temperature_metric = create_metric_box("Temperature", "°C")
            do_metric = create_metric_box("DO", "mg/L")

    with ui.card().classes("panel tank-panel"):
        ui.label("Tanks / Levels").classes("panel-title")
        #ui.label("Live values from the sensor MQTT bridge").classes(
        #    "panel-subtitle"
        #)

        with ui.column().classes("w-full gap-3 tank-gauge-stack"):
            ro_tank_gauge = create_tank_gauge("RO Tank", max_percent=120)
            mixer_tank_gauge = create_tank_gauge("Mixing Tank", max_percent=100)

    return {
        "ph_metric": ph_metric,
        "ec_metric": ec_metric,
        "temperature_metric": temperature_metric,
        "do_metric": do_metric,
        "ro_tank_gauge": ro_tank_gauge,
        "mixer_tank_gauge": mixer_tank_gauge,
    }


def _update_tank_gauge(
    gauge: dict[str, Any],
    percent: Any,
    liters: Any,
    max_liters: Any,
) -> None:
    value = percent_to_display(percent)

    if value > gauge["max_percent"]:
        new_max = ((int(value) + 9) // 10) * 10
        gauge["chart"].options["series"][0]["max"] = new_max
        gauge["max_percent"] = new_max

    gauge["chart"].options["series"][0]["data"][0]["value"] = value
    gauge["chart"].update()

    if liters is not None and max_liters is not None:
        gauge["liters"].set_text(f"{liters} L / {max_liters} L")
    else:
        gauge["liters"].set_text("-")

    gauge["percent"].set_text(f"{fmt(percent, '%')}")


def refresh_sensor_tanks(widgets: dict[str, Any], sensor_data: dict[str, Any]) -> None:
    _update_tank_gauge(
        widgets["ro_tank_gauge"],
        sensor_data["ro_level_percent"],
        sensor_data["ro_liters"],
        sensor_data["ro_max_liters"],
    )

    _update_tank_gauge(
        widgets["mixer_tank_gauge"],
        sensor_data["mixer_level_percent"],
        sensor_data["mixer_liters"],
        sensor_data["mixer_max_liters"],
    )

    widgets["ph_metric"]["value"].set_text(fmt(sensor_data["ph"], decimals=2))
    widgets["ec_metric"]["value"].set_text(fmt(sensor_data["ec_ms_cm"], decimals=3))
    widgets["temperature_metric"]["value"].set_text(
        fmt(sensor_data["water_temperature"], decimals=2)
    )
    widgets["do_metric"]["value"].set_text(fmt(sensor_data["dissolved_oxygen"], decimals=2))
