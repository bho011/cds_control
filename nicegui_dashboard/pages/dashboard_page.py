from typing import Any

from nicegui import ui

from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.components.formatting import fmt, percent_to_display
from nicegui_dashboard.components.status_widgets import create_metric_box, create_tank_gauge
from nicegui_dashboard.pages.dashboard.maintenance_section import (
    build_maintenance,
    handle_run_preflight,
    make_add_log,
)
from nicegui_dashboard.pages.dashboard.prime_section import (
    build_prime_dialog,
    refresh_prime,
    wire_prime_handlers,
)
from nicegui_dashboard.pages.dashboard.process_control_section import (
    build_process_control,
    refresh_process_control,
    wire_process_control_handlers,
)
from nicegui_dashboard.pages.dashboard.recipe_section import (
    build_recipe_section,
    update_recipe_card,
    wire_recipe_handlers,
)
from nicegui_dashboard.pages.dashboard.status_actuators_section import (
    build_status_actuators,
    refresh_status_actuators,
)
from nicegui_dashboard.recipe_store import RecipeBookCorruptedError, default_recipe_book, load_recipe_book


def create_dashboard_page(controller: CdsController) -> None:
    ui.add_head_html('<link rel="stylesheet" href="/static/dashboard.css?v=control2">')

    recipe_state: dict[str, Any] = {}
    recipe_form_state: dict[str, Any] = {}

    try:
        recipe_state["book"] = load_recipe_book()
        recipe_state["load_error"] = None
    except RecipeBookCorruptedError as exc:
        # Fail closed: keep an in-memory default for display only, do NOT
        # persist it - the broken file on disk is left untouched for manual
        # recovery (see nicegui_dashboard/recipe_store.py::load_recipe_book).
        recipe_state["book"] = default_recipe_book()
        recipe_state["load_error"] = str(exc)

    with ui.column().classes("dashboard-root gap-5"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.row().classes("items-center gap-4"):
                ui.image("/static/logo.jpg").classes("w-16 h-16 rounded-xl")
                with ui.column().classes("gap-0"):
                    ui.label("Central Dosing System Dashboard").classes("headline")
                    ui.label(
                        "NiceGUI HMI – live sensor values via Python core on Raspberry Pi."
                    ).classes("subtitle")

            with ui.row().classes("gap-2 items-center"):
                dev_info_button = ui.button("Maintenance").classes(
                    "dev-info-button"
                ).props("outline color=grey-5 icon=terminal")

        with ui.element("div").classes("layout-grid w-full"):
            with ui.column().classes("gap-3 w-full area-left"):
                status_actuators_widgets = build_status_actuators()
                maintenance_widgets = build_maintenance()

                prime_widgets = build_prime_dialog()

            with ui.element("div").classes("content-grid w-full"):
                with ui.column().classes("gap-5 w-full area-main"):
                    with ui.card().classes("panel process-state-panel"):
                        ui.label("Current Process State").classes("panel-title")
                        #ui.label("Live process state from MQTT and local controller").classes(
                        #    "panel-subtitle"
                        #)

                        with ui.column().classes("process-display process-display-large"):
                            ui.label("Current state").classes("process-label")
                            process_state_label = ui.label("-").classes("process-state")
                            control_message_label = ui.label("Ready").classes("process-message")
                            process_error_label = ui.label("").classes("error-text")
                            control_error_label = ui.label("").classes("error-text")

                        process_timestamp_label = ui.label("Process timestamp: -").classes("hidden")
                        process_source_label = ui.label("Source: -").classes("hidden")
                        control_state_label = ui.label("Controller state: -").classes("hidden")

                    card_widgets, editor_widgets = build_recipe_section(recipe_state)

                with ui.column().classes("gap-5 w-full area-side"):
                    process_control_widgets = build_process_control()

                with ui.column().classes("gap-5 w-full area-data"):
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
    
    add_log = make_add_log(maintenance_widgets["log_box"])

    if recipe_state.get("load_error"):
        add_log(f"[RECIPE] {recipe_state['load_error']}")

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

    dev_info_button.on_click(maintenance_widgets["dev_info_dialog"].open)
    wire_recipe_handlers(recipe_state, recipe_form_state, card_widgets, editor_widgets, add_log)
    update_recipe_card(recipe_state, card_widgets)

    wire_process_control_handlers(controller, process_control_widgets, add_log)
    maintenance_widgets["preflight_button"].on_click(
        lambda: handle_run_preflight(controller, maintenance_widgets["preflight_button"], add_log)
    )
    wire_prime_handlers(controller, maintenance_widgets["prime_button"], prime_widgets, add_log)

    def refresh() -> None:
        sensor_data = controller.get_sensor_status()
        process_data = controller.get_process_status()
        control_data = controller.get_process_control_status()

        maintenance_widgets["last_update_badge"].set_text(f"Update: {fmt(sensor_data['timestamp'])}")

        refresh_status_actuators(status_actuators_widgets, sensor_data, process_data)

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

        hardware_enabled = control_data["hardware_execution_enabled"]

        manual_drain_status = control_data.get("manual_drain_jog", {}) or {}
        manual_drain_active = bool(manual_drain_status.get("is_active", False))

        tank_cleaning_status = control_data.get("tank_cleaning", {}) or {}
        tank_cleaning_active = bool(tank_cleaning_status.get("is_active", False))

        prime_status = control_data.get("prime", {}) or {}
        prime_active = bool(prime_status.get("is_active", False))

        refresh_process_control(
            process_control_widgets,
            control_data,
            manual_drain_status,
            tank_cleaning_status,
            manual_drain_active,
            tank_cleaning_active,
            prime_active,
        )

        prime_selection_locked = control_data["is_running"] or manual_drain_active or tank_cleaning_active or prime_active
        refresh_prime(prime_widgets, prime_status, prime_active, prime_selection_locked)

        prime_results = prime_status.get("pump_results", []) or []
        prime_plan_total = sum(len(pumps) for pumps in (prime_status.get("pumps_plan", {}) or {}).values())

        manual_drain_elapsed = float(manual_drain_status.get("elapsed_seconds", 0.0) or 0.0)
        manual_drain_max = float(manual_drain_status.get("max_seconds", 30.0) or 30.0)
        tank_cleaning_phase = tank_cleaning_status.get("phase", "IDLE")
        tank_cleaning_elapsed = float(tank_cleaning_status.get("phase_elapsed_seconds", 0.0) or 0.0)

        if control_data["is_running"]:
            add_log(f"[STATE] {fmt(process_state)}")
        elif manual_drain_active:
            add_log(f"[MANUAL DRAIN] running {manual_drain_elapsed:.1f}/{manual_drain_max:.0f} s")
        elif tank_cleaning_active:
            add_log(f"[TANK CLEANING] {tank_cleaning_phase} {tank_cleaning_elapsed:.0f}s")
        elif prime_active:
            add_log(
                f"[PRIME] {prime_status.get('current_controller')}/{prime_status.get('current_pump')} "
                f"({len(prime_results)}/{prime_plan_total})"
            )
        elif not hardware_enabled:
            add_log("[SAFE] Hardware execution disabled. Start and Manual Drain are locked.")

    refresh()
    ui.timer(1.0, refresh)
