from typing import Any

from nicegui import ui

from nicegui_dashboard.cds_controller import CdsController


PROCESS_STEPS = [
    "IDLE",
    "OPEN_RO_INLET",
    "START_REFILL_PUMP",
    "FILL_UNTIL_TARGET",
    "STOP_REFILL_PUMP",
    "SETTLE_LEVEL",
    "START_CIRCULATION",
    "SENSOR_STABILIZE",
    "MEASURE_VALUES",
    "FINISHED",
    "ERROR",
]


def fmt(value: Any, unit: str = "", decimals: int = 2) -> str:
    if value is None:
        return "-"

    if isinstance(value, float):
        return f"{value:.{decimals}f} {unit}".strip()

    return f"{value} {unit}".strip()


def on_off(value: Any) -> str:
    if value is True:
        return "ON"

    if value is False:
        return "OFF"

    return "-"


def bool_dot(value: Any) -> str:
    if value is True:
        return "dot-on"

    if value is False:
        return "dot-off"

    return "dot-unknown"


def percent_to_progress(value: Any) -> float:
    if value is None:
        return 0.0

    try:
        return max(0.0, min(1.0, float(value) / 100.0))
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


def create_metric_box(title: str, unit: str = "") -> dict[str, Any]:
    with ui.card().classes("metric-box"):
        ui.label(title).classes("metric-title")
        value = ui.label("-").classes("metric-value")
        if unit:
            ui.label(unit).classes("metric-unit")

    return {"value": value}


def create_actuator_row(title: str, subtitle: str) -> dict[str, Any]:
    with ui.row().classes("actuator-row"):
        with ui.column().classes("gap-0 flex-1"):
            ui.label(title).classes("actuator-title")
            ui.label(subtitle).classes("actuator-subtitle")
        badge = ui.label("-").classes("actuator-badge badge-unknown")

    return {"badge": badge}


def create_dashboard_page(controller: CdsController) -> None:
    ui.add_head_html(
        """
        <style>
            body {
                background:
                    radial-gradient(circle at top left, rgba(36, 112, 86, 0.22), transparent 28rem),
                    linear-gradient(135deg, #09111f 0%, #0d1728 45%, #101827 100%);
                color: #e5eefb;
                font-family: Inter, Arial, sans-serif;
            }

            .dashboard-root {
                max-width: 1680px;
                margin: 0 auto;
                padding: 32px;
            }

            .headline {
                font-size: 34px;
                line-height: 1;
                font-weight: 900;
                letter-spacing: -0.04em;
                color: #f8fafc;
                text-shadow: 0 0 24px rgba(56, 189, 248, 0.16);
            }

            .subtitle {
                color: #93a4ba;
                font-size: 15px;
                margin-top: 10px;
            }

            .top-badge {
                padding: 8px 12px;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 800;
                letter-spacing: 0.02em;
            }

            .badge-green {
                background: rgba(34, 197, 94, 0.16);
                color: #4ade80;
                border: 1px solid rgba(74, 222, 128, 0.35);
            }

            .badge-orange {
                background: rgba(249, 115, 22, 0.16);
                color: #fb923c;
                border: 1px solid rgba(251, 146, 60, 0.35);
            }

            .badge-blue {
                background: rgba(59, 130, 246, 0.16);
                color: #60a5fa;
                border: 1px solid rgba(96, 165, 250, 0.35);
            }

            .panel {
                background: linear-gradient(180deg, rgba(30, 41, 59, 0.86), rgba(15, 23, 42, 0.92));
                border: 1px solid rgba(148, 163, 184, 0.18);
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.24);
                border-radius: 18px;
                padding: 18px;
            }

            .panel-title {
                font-size: 18px;
                font-weight: 900;
                color: #f8fafc;
                margin-bottom: 4px;
            }

            .panel-subtitle {
                color: #8fa2b8;
                font-size: 13px;
                margin-bottom: 14px;
            }

            .status-row {
                width: 100%;
                align-items: center;
                gap: 12px;
                padding: 12px;
                background: rgba(15, 23, 42, 0.70);
                border: 1px solid rgba(148, 163, 184, 0.12);
                border-radius: 14px;
            }

            .status-dot {
                width: 14px;
                height: 14px;
                border-radius: 999px;
                flex-shrink: 0;
            }

            .dot-on {
                background: #22c55e;
                box-shadow: 0 0 16px rgba(34, 197, 94, 0.75);
            }

            .dot-off {
                background: #ef4444;
                box-shadow: 0 0 16px rgba(239, 68, 68, 0.55);
            }

            .dot-unknown {
                background: #64748b;
                box-shadow: 0 0 16px rgba(100, 116, 139, 0.45);
            }

            .status-title {
                font-weight: 800;
                color: #f1f5f9;
                font-size: 14px;
            }

            .status-subtitle {
                color: #8fa2b8;
                font-size: 12px;
            }

            .status-value {
                color: #cbd5e1;
                font-size: 13px;
                font-weight: 700;
            }

            .process-display {
                background: rgba(20, 83, 45, 0.28);
                border: 1px solid rgba(34, 197, 94, 0.38);
                border-radius: 18px;
                padding: 20px;
                min-width: 220px;
            }

            .process-label {
                color: #9fb2c7;
                font-size: 13px;
            }

            .process-state {
                font-size: 42px;
                line-height: 1;
                font-weight: 1000;
                color: #22c55e;
                margin-top: 6px;
                word-break: break-word;
            }

            .process-meta {
                color: #9fb2c7;
                font-size: 13px;
                margin-top: 10px;
            }

            .step-chip {
                min-width: 110px;
                justify-content: center;
                padding: 14px 10px;
                border-radius: 14px;
                border: 1px solid rgba(34, 197, 94, 0.30);
                background: rgba(15, 23, 42, 0.75);
                color: #22c55e;
                font-size: 12px;
                font-weight: 900;
                text-align: center;
            }

            .step-chip-active {
                background: rgba(34, 197, 94, 0.18);
                border-color: rgba(34, 197, 94, 0.80);
                box-shadow: 0 0 24px rgba(34, 197, 94, 0.20);
            }

            .metric-box {
                background: rgba(15, 23, 42, 0.72);
                border: 1px solid rgba(148, 163, 184, 0.14);
                border-radius: 16px;
                padding: 16px;
                min-width: 140px;
                flex: 1;
            }

            .metric-title {
                color: #9fb2c7;
                font-size: 13px;
                font-weight: 800;
            }

            .metric-value {
                color: #f8fafc;
                font-size: 28px;
                font-weight: 900;
                margin-top: 8px;
            }

            .metric-unit {
                color: #8fa2b8;
                font-size: 12px;
            }

            .tank-title {
                color: #f8fafc;
                font-size: 14px;
                font-weight: 900;
            }

            .tank-value {
                color: #f8fafc;
                font-size: 28px;
                font-weight: 900;
                margin-top: 8px;
            }

            .tank-percent {
                color: #9fb2c7;
                font-size: 13px;
            }

            .actuator-row {
                width: 100%;
                align-items: center;
                gap: 12px;
                padding: 12px;
                background: rgba(15, 23, 42, 0.70);
                border: 1px solid rgba(148, 163, 184, 0.12);
                border-radius: 14px;
            }

            .actuator-title {
                color: #f1f5f9;
                font-weight: 800;
                font-size: 14px;
            }

            .actuator-subtitle {
                color: #8fa2b8;
                font-size: 12px;
            }

            .actuator-badge {
                min-width: 54px;
                text-align: center;
                padding: 6px 8px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 900;
            }

            .badge-on {
                background: rgba(34, 197, 94, 0.16);
                color: #4ade80;
                border: 1px solid rgba(74, 222, 128, 0.35);
            }

            .badge-off {
                background: rgba(100, 116, 139, 0.18);
                color: #cbd5e1;
                border: 1px solid rgba(148, 163, 184, 0.22);
            }

            .badge-unknown {
                background: rgba(234, 179, 8, 0.15);
                color: #facc15;
                border: 1px solid rgba(250, 204, 21, 0.28);
            }

            .control-input .q-field__control {
                background: rgba(15, 23, 42, 0.75);
                color: #f8fafc;
                border-radius: 12px;
            }

            .log-box {
                height: 275px;
                overflow-y: auto;
                background: rgba(2, 6, 23, 0.88);
                border: 1px solid rgba(148, 163, 184, 0.14);
                border-radius: 14px;
                padding: 14px;
                font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                font-size: 12px;
                color: #cbd5e1;
                white-space: pre-wrap;
            }

            .warn-text {
                color: #fbbf24;
                font-size: 13px;
                font-weight: 700;
            }

            .error-text {
                color: #f87171;
                font-size: 13px;
                font-weight: 700;
            }
        </style>
        """
    )

    with ui.column().classes("dashboard-root gap-5"):

        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("CDS Dashboard").classes("headline")
                ui.label(
                    "NiceGUI-Oberfläche direkt auf dem Raspberry Pi mit echten Sensorwerten und Python-Core-Anbindung."
                ).classes("subtitle")

            with ui.row().classes("gap-2"):
                ui.label("RASPI LIVE").classes("top-badge badge-green")
                ui.label("PYTHON CORE").classes("top-badge badge-blue")
                last_update_badge = ui.label("Update: -").classes("top-badge badge-orange")

        with ui.grid(columns=3).classes("w-full gap-5"):

            with ui.column().classes("gap-5"):
                with ui.card().classes("panel"):
                    ui.label("Systemstatus").classes("panel-title")
                    ui.label("Kommunikations- und Prozessmodule").classes("panel-subtitle")

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
                    ui.label("Rezept / Sollwerte").classes("panel-title")
                    ui.label("Aktuelle Werte aus process_settings.json").classes("panel-subtitle")

                    hardware_enabled_label = ui.label("hardware_execution_enabled: -").classes(
                        "warn-text"
                    )
                    fill_settings_label = ui.label("Settings: -").classes("text-sm text-slate-300")
                    required_text_label = ui.label("required_confirmation_text: -").classes(
                        "text-sm text-slate-400"
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
                            ui.label("Aktueller Prozesszustand").classes("process-label")
                            process_state_label = ui.label("-").classes("process-state")
                            process_timestamp_label = ui.label("Process timestamp: -").classes(
                                "process-meta"
                            )
                            process_source_label = ui.label("Source: -").classes("process-meta")

                        with ui.column().classes("gap-2 flex-1"):
                            control_state_label = ui.label("Controller state: -").classes(
                                "text-sm text-slate-300"
                            )
                            control_message_label = ui.label("Message: -").classes(
                                "text-sm text-slate-300"
                            )
                            process_error_label = ui.label("").classes("error-text")
                            control_error_label = ui.label("").classes("error-text")

                    step_chips: dict[str, Any] = {}

                    with ui.row().classes("w-full gap-2 flex-wrap mt-4"):
                        for step in PROCESS_STEPS:
                            step_chips[step] = ui.label(step).classes("step-chip")

                    with ui.row().classes("w-full gap-3 mt-4"):
                        confirmation_input = ui.input(
                            label="Sicherheitsbestätigung",
                            placeholder="required_confirmation_text eingeben",
                            password=True,
                        ).classes("control-input flex-1")

                    with ui.row().classes("w-full gap-3 mt-2"):
                        start_button = ui.button("Start Fill & Measure").classes(
                            "flex-1 font-bold"
                        ).props("color=positive")
                        stop_button = ui.button("Emergency Stop").classes(
                            "flex-1 font-bold"
                        ).props("color=negative")

                    ui.label(
                        "GPIOs werden erst nach gültiger Bestätigung und hardware_execution_enabled=true initialisiert."
                    ).classes("warn-text mt-2")

                with ui.card().classes("panel"):
                    ui.label("Tanks / Füllstände").classes("panel-title")
                    ui.label("Livewerte aus Sensor-MQTT-Bridge").classes("panel-subtitle")

                    with ui.row().classes("w-full gap-4"):
                        with ui.column().classes("flex-1"):
                            ui.label("RO Tank").classes("tank-title")
                            ro_liters_label = ui.label("-").classes("tank-value")
                            ro_percent_label = ui.label("-").classes("tank-percent")
                            ro_progress = ui.linear_progress(value=0.0).classes("w-full mt-2")

                        with ui.column().classes("flex-1"):
                            ui.label("Mixing Tank").classes("tank-title")
                            mixer_liters_label = ui.label("-").classes("tank-value")
                            mixer_percent_label = ui.label("-").classes("tank-percent")
                            mixer_progress = ui.linear_progress(value=0.0).classes("w-full mt-2")

                with ui.card().classes("panel"):
                    ui.label("Sensorwerte").classes("panel-title")
                    ui.label("pH, EC, Temperatur und gelöster Sauerstoff").classes(
                        "panel-subtitle"
                    )

                    with ui.row().classes("w-full gap-3"):
                        ph_metric = create_metric_box("pH")
                        ec_metric = create_metric_box("EC", "mS/cm")
                        temperature_metric = create_metric_box("Temperatur", "°C")
                        do_metric = create_metric_box("DO", "mg/L")

            with ui.column().classes("gap-5"):
                with ui.card().classes("panel"):
                    ui.label("Prozesslog").classes("panel-title")
                    ui.label("Dashboard-Ereignisse und Live-Status").classes("panel-subtitle")
                    log_box = ui.label("").classes("log-box")

                with ui.card().classes("panel"):
                    ui.label("Nächste Ausbaustufen").classes("panel-title")
                    ui.label("Technisch sinnvolle Reihenfolge").classes("panel-subtitle")
                    ui.label("1. Layout finalisieren").classes("text-sm text-slate-300")
                    ui.label("2. Prozessstart unter realen Bedingungen testen").classes(
                        "text-sm text-slate-300"
                    )
                    ui.label("3. Emergency Stop validieren").classes("text-sm text-slate-300")
                    ui.label("4. Maintenance Mode mit Zeitlimit planen").classes(
                        "text-sm text-slate-300"
                    )
                    ui.label("5. Node-RED Dashboard später ablösen").classes(
                        "text-sm text-slate-300"
                    )

    event_log: list[str] = ["[OK] NiceGUI Dashboard geladen."]

    def add_log(message: str) -> None:
        if not event_log or event_log[-1] != message:
            event_log.append(message)

        if len(event_log) > 18:
            del event_log[:-18]

        log_box.set_text("\\n".join(event_log))

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

        result = controller.start_fill_and_measure(confirmation_input.value or "")

        if result["success"]:
            ui.notify(result["message"], color="positive")
            add_log(f"[OK] {result['message']}")
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

        ro_liters = sensor_data["ro_liters"]
        ro_max_liters = sensor_data["ro_max_liters"]
        ro_percent = sensor_data["ro_level_percent"]

        if ro_liters is not None and ro_max_liters is not None:
            ro_liters_label.set_text(f"{ro_liters} L / {ro_max_liters} L")
        else:
            ro_liters_label.set_text("-")

        ro_percent_label.set_text(f"{fmt(ro_percent, '%')}")
        ro_progress.set_value(percent_to_progress(ro_percent))

        mixer_liters = sensor_data["mixer_liters"]
        mixer_max_liters = sensor_data["mixer_max_liters"]
        mixer_percent = sensor_data["mixer_level_percent"]

        if mixer_liters is not None and mixer_max_liters is not None:
            mixer_liters_label.set_text(f"{mixer_liters} L / {mixer_max_liters} L")
        else:
            mixer_liters_label.set_text("-")

        mixer_percent_label.set_text(f"{fmt(mixer_percent, '%')}")
        mixer_progress.set_value(percent_to_progress(mixer_percent))

        ph_metric["value"].set_text(fmt(sensor_data["ph"], decimals=2))
        ec_metric["value"].set_text(fmt(sensor_data["ec_ms_cm"], decimals=3))
        temperature_metric["value"].set_text(
            fmt(sensor_data["water_temperature"], decimals=2)
        )
        do_metric["value"].set_text(fmt(sensor_data["dissolved_oxygen"], decimals=2))

        process_state = process_data["process_state"] or control_data["state_name"] or "-"

        process_state_label.set_text(fmt(process_state))
        process_timestamp_label.set_text(
            f"Process timestamp: {fmt(process_data['timestamp'])}"
        )
        process_source_label.set_text(f"Source: {fmt(process_data['source'])}")

        for step, chip in step_chips.items():
            chip.classes(remove="step-chip-active")
            if process_state == step:
                chip.classes("step-chip-active")

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
            add_log(f"[STATE] {fmt(process_state)}")
        elif not hardware_enabled:
            add_log("[SAFE] Hardware execution disabled. Start button locked.")

    refresh()
    ui.timer(1.0, refresh)
