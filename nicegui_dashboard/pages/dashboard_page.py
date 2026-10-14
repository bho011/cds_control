from datetime import datetime
from typing import Any

from nicegui import ui

from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.components.formatting import fmt, format_clock
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
    build_target_transfer,
    format_dosing_status,
    format_evaluation_status,
    format_transfer_status,
    refresh_process_control,
    wire_process_control_handlers,
)
from nicegui_dashboard.pages.dashboard.recipe_section import (
    build_recipe_section,
    update_recipe_card,
    wire_recipe_handlers,
)
from nicegui_dashboard.pages.dashboard.sensor_tanks_section import build_sensor_tanks, refresh_sensor_tanks
from nicegui_dashboard.pages.dashboard.status_actuators_section import (
    build_status_actuators,
    refresh_status_actuators,
)
from nicegui_dashboard.recipe_store import RecipeBookCorruptedError, default_recipe_book, load_recipe_book


def create_dashboard_page(controller: CdsController) -> None:
    ui.add_head_html('<link rel="stylesheet" href="/static/dashboard.css?v=control3">')

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

    with ui.column().classes("dashboard-root"):
        with ui.row().classes("dash-header w-full items-center justify-between"):
            with ui.row().classes("items-center gap-3"):
                ui.image("/static/logo.jpg").classes("dash-logo rounded-lg")
                with ui.column().classes("gap-0"):
                    ui.label("Central Dosing System Dashboard").classes("headline")
                    ui.label("vGREENS - DRIVEN BY PURPOSE. POWERED BY DATA.").classes("subtitle")

            with ui.row().classes("gap-3 items-center"):
                with ui.column().classes("gap-0 items-end"):
                    header_clock_label = ui.label(format_clock(datetime.now())).classes("header-clock")
                    header_update_label = ui.label("Update: -").classes("header-update")
                dev_info_button = ui.button("Maintenance").classes(
                    "dev-info-button"
                ).props("outline color=grey-5 icon=terminal")

        # Ein Raster fuer alle Karten (Bereiche per Klasse, siehe dashboard.css "dash-grid"):
        #   status | process state | process control | sensor values
        #   actuators | transfer   |  (Process Control und Sensor/Tanks laufen ueber alle Zeilen)
        #   recipe (ueber Spalte 1 und 2)
        with ui.element("div").classes("dash-grid"):
            status_actuators_widgets = build_status_actuators()

            with ui.card().classes("panel process-state-panel grid-process-state"):
                ui.label("Current Process State").classes("panel-title")

                with ui.column().classes("process-display process-display-large"):
                    ui.label("Current state").classes("process-label")
                    process_state_label = ui.label("-").classes("process-state")
                    control_message_label = ui.label("Ready").classes("process-message")
                    process_error_label = ui.label("").classes("error-text")
                    control_error_label = ui.label("").classes("error-text")
                    dosing_label = ui.label("Dosierung: -").classes("process-message")
                    dosing_progress = ui.linear_progress(value=0.0, show_value=False).classes("w-full")
                    dosing_error_label = ui.label("").classes("error-text")

                process_timestamp_label = ui.label("Process timestamp: -").classes("hidden")
                process_source_label = ui.label("Source: -").classes("hidden")
                control_state_label = ui.label("Controller state: -").classes("hidden")

            transfer_widgets = build_target_transfer()
            card_widgets, editor_widgets = build_recipe_section(recipe_state)

            with ui.column().classes("grid-side"):
                process_control_widgets = build_process_control(transfer_widgets)

            with ui.column().classes("grid-data"):
                sensor_tanks_widgets = build_sensor_tanks()

        # Dialoge liegen ausserhalb des Rasters (kein Layout-Platz).
        maintenance_widgets = build_maintenance()
        prime_widgets = build_prime_dialog()

    add_log = make_add_log(maintenance_widgets["log_box"])

    if recipe_state.get("load_error"):
        add_log(f"[RECIPE] {recipe_state['load_error']}")

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

        update_text = f"Update: {fmt(sensor_data['timestamp'])}"
        maintenance_widgets["last_update_badge"].set_text(update_text)
        header_update_label.set_text(update_text)
        header_clock_label.set_text(format_clock(datetime.now()))

        refresh_status_actuators(status_actuators_widgets, sensor_data, process_data)
        refresh_sensor_tanks(sensor_tanks_widgets, sensor_data)

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

        control_message = control_data.get("last_message") or "Ready"
        evaluation_text = format_evaluation_status(control_data.get("final_evaluation"))
        control_message_label.set_text(f"{control_message} | {evaluation_text}" if evaluation_text else control_message)
        control_error_label.set_text(f"Error: {control_data['error']}" if control_data.get("error") else "")
        transfer_data = control_data.get("target_transfer") or {}
        if transfer_data.get("pump_off_failed"):
            # Kritisch und dauerhaft sichtbar, bis quittiert (kein Pumpenstillstand bestaetigt).
            control_error_label.set_text(format_transfer_status(transfer_data))

        dosing_text, dosing_value, dosing_error = format_dosing_status(control_data.get("dosing"))
        dosing_label.set_text(dosing_text)
        dosing_progress.set_value(dosing_value)
        dosing_error_label.set_text(dosing_error)

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
        elif (control_data.get("target_transfer") or {}).get("is_active"):
            add_log(f"[TRANSFER] {format_transfer_status(control_data.get('target_transfer'))}")
        elif prime_active:
            add_log(
                f"[PRIME] {prime_status.get('current_controller')}/{prime_status.get('current_pump')} "
                f"({len(prime_results)}/{prime_plan_total})"
            )
        elif not hardware_enabled:
            add_log("[SAFE] Hardware execution disabled. Start and Manual Drain are locked.")

    refresh()
    ui.timer(1.0, refresh)
