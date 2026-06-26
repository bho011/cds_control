from typing import Any

from nicegui import ui

from nicegui_dashboard.cds_controller import CdsController


def fmt(value: Any, unit: str = "", decimals: int = 2) -> str:
    if value is None:
        return "-"

    if isinstance(value, float):
        return f"{value:.{decimals}f} {unit}".strip()

    return f"{value} {unit}".strip()


def bool_dot(value: Any) -> str:
    if value is True:
        return "dot-on"

    if value is False:
        return "dot-off"

    return "dot-unknown"


def percent_to_display(value: Any) -> float:
    if value is None:
        return 0.0

    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


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


def create_actuator_row(title: str, subtitle: str) -> dict[str, Any]:
    with ui.row().classes("actuator-row"):
        with ui.column().classes("gap-0 flex-1"):
            ui.label(title).classes("actuator-title")
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
                                    [0.75, "#22c55e"],
                                    [1.0, "#f59e0b"],
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
                            "length": "62%",
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
        ).classes("w-full h-64")

        liters_label = ui.label("-").classes("tank-liters")
        percent_label = ui.label("-").classes("tank-percent")

    return {
        "chart": chart,
        "liters": liters_label,
        "percent": percent_label,
        "max_percent": max_percent,
    }


def create_dashboard_page(controller: CdsController) -> None:
    ui.add_head_html('<link rel="stylesheet" href="/static/dashboard.css">')

    with ui.column().classes("dashboard-root gap-5"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.row().classes("items-center gap-4"):
                ui.image("/static/logo.jpg").classes("w-16 h-16 rounded-xl")
                with ui.column().classes("gap-0"):
                    ui.label("Central Dosing System Dashboard").classes("headline")
                    ui.label(
                        "NiceGUI-HMI – Live-Sensorwerte über Python-Core-Anbindung auf dem Raspberry Pi."
                    ).classes("subtitle")

            with ui.row().classes("gap-2"):
                ui.label("RASPI LIVE").classes("top-badge badge-green")
                ui.label("PYTHON CORE").classes("top-badge badge-blue")
                last_update_badge = ui.label("Update: -").classes(
                    "top-badge badge-orange"
                )

        with ui.element("div").classes("layout-grid w-full"):
            with ui.column().classes("gap-5"):
                with ui.card().classes("panel"):
                    ui.label("Systemstatus").classes("panel-title")
                    ui.label("Kommunikations- und Prozessmodule").classes(
                        "panel-subtitle"
                    )

                    mqtt_bridge_row = create_status_row(
                        "MQTT Sensor Bridge",
                        "OPC-UA → MQTT → SensorSnapshotReader",
                    )
                    process_reader_row = create_status_row(
                        "Process MQTT Reader",
                        "liest cds/status/process",
                    )
                    snapshot_row = create_status_row(
                        "Sensor Snapshot",
                        "max. 5 Sekunden alt",
                    )
                    process_payload_row = create_status_row(
                        "Process Payload",
                        "max. 30 Sekunden alt",
                    )

                with ui.card().classes("panel"):
                    ui.label("Aktoren / Outputs").classes("panel-title")
                    ui.label("Read-only Anzeige aus MQTT, keine Direktsteuerung").classes(
                        "panel-subtitle"
                    )

                    mixer_refill_pump_row = create_actuator_row(
                        "Mixer Refill Pump",
                        "Contactor 1 / GPIO20 / Pin 38",
                    )
                    supply_valve_6_row = create_actuator_row(
                        "Supply Valve 6",
                        "Valve 6 / GPIO6 / Pin 31",
                    )
                    drain_valve_0_row = create_actuator_row(
                        "Drain Valve 0",
                        "validierter Drain / aus MQTT",
                    )
                    transfer_pump_row = create_actuator_row(
                        "Transfer Pump",
                        "späterer Erweiterungspunkt",
                    )
                    mixing_circulation_pump_row = create_actuator_row(
                        "Mixing Circulation Pump",
                        "optional aus Settings",
                    )
                    sensor_circulation_pump_row = create_actuator_row(
                        "Sensor Circulation Pump",
                        "optional aus Settings",
                    )

            with ui.column().classes("gap-5"):
                with ui.card().classes("panel"):
                    ui.label("Process State Machine").classes("panel-title")
                    ui.label("Live-Prozessstatus aus MQTT und lokalem Controller").classes(
                        "panel-subtitle"
                    )

                    with ui.row().classes("w-full gap-4 items-stretch"):
                        with ui.column().classes("process-display"):
                            ui.label("Aktueller Prozesszustand").classes(
                                "process-label"
                            )
                            process_state_label = ui.label("-").classes(
                                "process-state"
                            )
                            process_timestamp_label = ui.label(
                                "Process timestamp: -"
                            ).classes("process-meta")
                            process_source_label = ui.label("Source: -").classes(
                                "process-meta"
                            )

                        with ui.column().classes("gap-2 flex-1"):
                            control_state_label = ui.label(
                                "Controller state: -"
                            ).classes("text-sm text-slate-300")
                            control_message_label = ui.label("Message: -").classes(
                                "text-sm text-slate-300"
                            )
                            process_error_label = ui.label("").classes("error-text")
                            control_error_label = ui.label("").classes("error-text")

                    confirmation_input = ui.input(
                        label="Sicherheitsbestätigung",
                        placeholder="confirmed eingeben",
                        password=False,
                    ).classes("control-input w-full mt-4")

                    with ui.row().classes("w-full gap-3 mt-3"):
                        start_button = ui.button("Start Fill & Measure").classes(
                            "flex-1 font-bold"
                        ).props("color=positive")
                        reset_button = ui.button("Reset / Acknowledge").classes(
                            "flex-1 font-bold"
                        ).props("color=primary")
                        stop_button = ui.button("Emergency Stop").classes(
                            "flex-1 font-bold"
                        ).props("color=negative")

                    ui.label(
                        "GPIOs werden erst nach gültiger Bestätigung und hardware_execution_enabled=true initialisiert."
                    ).classes("warn-text mt-2")

                with ui.card().classes("panel recipe-panel"):
                    ui.label("Rezept / Sollwerte").classes("panel-title")
                    ui.label("Aktuelle Werte aus process_settings.json").classes(
                        "panel-subtitle"
                    )

                    hardware_enabled_label = ui.label(
                        "hardware_execution_enabled: -"
                    ).classes("warn-text")
                    fill_settings_label = ui.label("Settings: -").classes(
                        "text-sm text-slate-300"
                    )
                    required_text_label = ui.label(
                        "required_confirmation_text: -"
                    ).classes("text-sm text-slate-400")

            with ui.column().classes("gap-5"):
                with ui.card().classes("panel"):
                    ui.label("Prozesslog").classes("panel-title")
                    ui.label("Dashboard-Ereignisse und Live-Status").classes(
                        "panel-subtitle"
                    )
                    log_box = ui.label("").classes("log-box")

                with ui.card().classes("panel sensor-panel"):
                    ui.label("Sensorwerte").classes("panel-title")
                    ui.label("pH, EC, Temperatur und gelöster Sauerstoff").classes(
                        "panel-subtitle"
                    )

                    with ui.row().classes("w-full gap-3"):
                        ph_metric = create_metric_box("pH")
                        ec_metric = create_metric_box("EC", "mS/cm")

                    with ui.row().classes("w-full gap-3"):
                        temperature_metric = create_metric_box("Temperatur", "°C")
                        do_metric = create_metric_box("DO", "mg/L")

        with ui.element("div").classes("bottom-grid w-full"):
            with ui.element("div"):
                pass

            with ui.card().classes("panel tank-panel"):
                ui.label("Tanks / Füllstände").classes("panel-title")
                ui.label("Livewerte aus Sensor-MQTT-Bridge als Gauges").classes(
                    "panel-subtitle"
                )

                with ui.row().classes("w-full gap-5"):
                    ro_tank_gauge = create_tank_gauge("RO Tank", max_percent=120)
                    mixer_tank_gauge = create_tank_gauge("Mixing Tank", max_percent=100)

    event_log: list[str] = ["[OK] NiceGUI Dashboard geladen."]

    def add_log(message: str) -> None:
        if not event_log or event_log[-1] != message:
            event_log.append(message)

        if len(event_log) > 18:
            del event_log[:-18]

        log_box.set_text("\n".join(event_log))

    def set_dot(row: dict[str, Any], value: Any) -> None:
        row["dot"].classes(remove="dot-on dot-off dot-unknown")
        row["dot"].classes(bool_dot(value))

    def set_actuator(row: dict[str, Any], value: Any) -> None:
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

    def update_tank_gauge(
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

    def handle_start() -> None:
        control_data = controller.get_process_control_status()

        if not control_data["hardware_execution_enabled"]:
            message = (
                "Start blockiert: hardware_execution_enabled ist false. "
                "NiceGUI bleibt im Beobachtungsmodus."
            )
            ui.notify(message, color="negative")
            add_log(f"[SAFE] {message}")
            return

        entered_text = (confirmation_input.value or "").strip()
        required_text = str(control_data["required_confirmation_text"]).strip()

        add_log(
            f"[DEBUG] Confirmation erhalten: '{entered_text}' | erwartet: '{required_text}'"
        )

        if entered_text != required_text:
            message = (
                "Start blockiert: Bestätigungstext ist falsch. "
                f"Eingegeben='{entered_text}' Erwartet='{required_text}'"
            )
            ui.notify(message, color="negative")
            add_log(f"[BLOCKED] {message}")
            return

        result = controller.start_fill_and_measure(entered_text)

        if result["success"]:
            ui.notify(result["message"], color="positive")
            add_log(f"[OK] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[BLOCKED] {result['message']}")

    def handle_reset() -> None:
        result = controller.acknowledge_error()

        if result["success"]:
            ui.notify(result["message"], color="positive")
            add_log(f"[RESET] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[BLOCKED] {result['message']}")

    def handle_stop() -> None:
        result = controller.emergency_stop()

        if result["success"]:
            ui.notify(result["message"], color="warning")
            add_log(f"[STOP] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[ERROR] {result['message']}")

    start_button.on_click(handle_start)
    reset_button.on_click(handle_reset)
    stop_button.on_click(handle_stop)

    def refresh() -> None:
        sensor_data = controller.get_sensor_status()
        process_data = controller.get_process_status()
        control_data = controller.get_process_control_status()

        last_update_badge.set_text(f"Update: {fmt(sensor_data['timestamp'])}")

        sensor_available = sensor_data["snapshot_available"]
        process_available = process_data["payload_available"]

        mqtt_bridge_ok = (
            sensor_data["sensor_started"]
            and sensor_available
            and not sensor_data["bridge_error"]
        )

        set_dot(mqtt_bridge_row, mqtt_bridge_ok)
        mqtt_bridge_row["value"].set_text("verbunden" if mqtt_bridge_ok else "prüfen")

        set_dot(process_reader_row, process_data["reader_started"])
        process_reader_row["value"].set_text(
            "läuft" if process_data["reader_started"] else "gestoppt"
        )

        set_dot(snapshot_row, sensor_available)
        snapshot_row["value"].set_text("aktuell" if sensor_available else "veraltet")

        set_dot(process_payload_row, process_available)
        process_payload_row["value"].set_text(
            "aktuell" if process_available else "kein Payload"
        )

        update_tank_gauge(
            ro_tank_gauge,
            sensor_data["ro_level_percent"],
            sensor_data["ro_liters"],
            sensor_data["ro_max_liters"],
        )

        update_tank_gauge(
            mixer_tank_gauge,
            sensor_data["mixer_level_percent"],
            sensor_data["mixer_liters"],
            sensor_data["mixer_max_liters"],
        )

        ph_metric["value"].set_text(fmt(sensor_data["ph"], decimals=2))
        ec_metric["value"].set_text(fmt(sensor_data["ec_ms_cm"], decimals=3))
        temperature_metric["value"].set_text(
            fmt(sensor_data["water_temperature"], decimals=2)
        )
        do_metric["value"].set_text(fmt(sensor_data["dissolved_oxygen"], decimals=2))

        process_state = (
            process_data["process_state"]
            if process_data["payload_available"]
            else control_data["state_name"]
        ) or "-"

        process_state_label.set_text(fmt(process_state))
        process_timestamp_label.set_text(
            f"Process timestamp: {fmt(process_data['timestamp'])}"
        )
        process_source_label.set_text(f"Source: {fmt(process_data['source'])}")

        if process_data["error"]:
            process_error_label.set_text(f"Process error: {process_data['error']}")
        else:
            process_error_label.set_text("")

        set_actuator(mixer_refill_pump_row, process_data["mixer_refill_pump"])
        set_actuator(supply_valve_6_row, process_data["supply_valve_6"])
        set_actuator(drain_valve_0_row, process_data["drain_valve_0"])
        set_actuator(transfer_pump_row, process_data["transfer_pump"])
        set_actuator(mixing_circulation_pump_row, process_data["mixing_circulation_pump"])
        set_actuator(sensor_circulation_pump_row, process_data["sensor_circulation_pump"])

        hardware_enabled = control_data["hardware_execution_enabled"]

        hardware_enabled_label.set_text(
            f"hardware_execution_enabled: {hardware_enabled}"
        )

        required_text_label.set_text(
            f"required_confirmation_text: {control_data['required_confirmation_text']}"
        )

        fill_settings_label.set_text(
            f"mode={control_data['fill_mode']} | "
            f"target_add={control_data['target_add_liters']} L | "
            f"target_total={control_data['target_total_liters']} L | "
            f"max_fill={control_data['max_fill_seconds']} s | "
            f"mixing_circulation={control_data['enable_mixing_circulation']} | "
            f"sensor_circulation={control_data['enable_sensor_circulation']}"
        )

        control_state_label.set_text(
            "Controller: "
            f"running={control_data['is_running']} | "
            f"state={fmt(control_data['state_name'])} | "
            f"start_mixer={fmt(control_data['start_mixer_liters'], 'L')} | "
            f"added={fmt(control_data['added_liters'], 'L')}"
        )

        control_message_label.set_text(f"Message: {control_data['last_message']}")

        if control_data["error"]:
            control_error_label.set_text(f"Controller error: {control_data['error']}")
        else:
            control_error_label.set_text("")

        if control_data["is_running"] or not hardware_enabled:
            start_button.disable()
        else:
            start_button.enable()

        if control_data["is_running"]:
            reset_button.disable()
        else:
            reset_button.enable()

        if control_data["is_running"]:
            add_log(f"[STATE] {fmt(process_state)}")
        elif not hardware_enabled:
            add_log("[SAFE] Hardware execution disabled. Start button locked.")

    refresh()
    ui.timer(1.0, refresh)
