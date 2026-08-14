"""Wiederverwendbare Widget-Bausteine: Status-Zeile, Aktor-Zeile, Metrik-Box, Tank-Gauge."""

from __future__ import annotations

from typing import Any

from nicegui import ui


def create_status_row(title: str, subtitle: str) -> dict[str, Any]:
    with ui.row().classes("status-row"):
        dot = ui.element("div").classes("status-dot dot-unknown")

        with ui.column().classes("gap-0 flex-1"):
            ui.label(title).classes("status-title")
            ui.label(subtitle).classes("status-subtitle")

        value = ui.label("-").classes("status-value")

    return {
        "dot": dot,
        "value": value,
    }


def create_actuator_row(title: str, subtitle: str = "") -> dict[str, Any]:
    with ui.row().classes("actuator-row"):
        with ui.column().classes("gap-0 flex-1"):
            ui.label(title).classes("actuator-title")
            if subtitle:
                ui.label(subtitle).classes("actuator-subtitle")

        badge = ui.label("-").classes("actuator-badge badge-unknown")

    return {"badge": badge}


def create_metric_box(title: str, unit: str = "") -> dict[str, Any]:
    with ui.card().classes("metric-box"):
        ui.label(title).classes("metric-title")
        value = ui.label("-").classes("metric-value")

        if unit:
            ui.label(unit).classes("metric-unit")

    return {"value": value}


def create_tank_gauge(title: str, max_percent: int = 120) -> dict[str, Any]:
    with ui.card().classes("tank-gauge-card"):
        ui.label(title).classes("tank-title")

        chart = ui.echart(
            {
                "backgroundColor": "transparent",
                "series": [
                    {
                        "type": "gauge",
                        "min": 0,
                        "max": max_percent,
                        "startAngle": 210,
                        "endAngle": -30,
                        "progress": {
                            "show": True,
                            "width": 16,
                        },
                        "axisLine": {
                            "lineStyle": {
                                "width": 16,
                                "color": [
                                    [0.33, "#f20707"],
                                    [0.66, "#f59e0b"],
                                    [1.0, "#22c55e"],
                                ],
                            }
                        },
                        "axisTick": {
                            "show": False,
                        },
                        "splitLine": {
                            "show": False,
                        },
                        "axisLabel": {
                            "color": "#94a3b8",
                            "fontSize": 10,
                        },
                        "pointer": {
                            "show": True,
                            "length": "50%",
                            "width": 5,
                        },
                        "anchor": {
                            "show": True,
                            "size": 8,
                        },
                        "title": {
                            "show": False,
                        },
                        "detail": {
                            "valueAnimation": True,
                            "formatter": "{value}%",
                            "color": "#f8fafc",
                            "fontSize": 24,
                            "fontWeight": "bold",
                            "offsetCenter": [0, "55%"],
                        },
                        "data": [
                            {
                                "value": 0,
                                "name": title,
                            }
                        ],
                    }
                ],
            }
        ).classes("w-full h-52")

        liters_label = ui.label("-").classes("tank-liters")
        percent_label = ui.label("-").classes("tank-percent")

    return {
        "chart": chart,
        "liters": liters_label,
        "percent": percent_label,
        "max_percent": max_percent,
    }
