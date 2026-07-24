from typing import Any

from nicegui import ui

from domain.recipe_limits import RecipeValidationError, nutrient_b_percent
from domain.recipe_model import RunOptions, StoredRecipe
from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.recipe_store import (
    RecipeBookCorruptedError,
    default_recipe_book,
    get_active_recipe,
    get_recipe_by_slot,
    get_recipe_preview,
    load_recipe_book,
    save_recipe_to_slot,
    set_active_slot,
)
from process.pump_prime import describe_pump_options
from services.peristaltic.models import VALID_CONTROLLERS, VALID_PUMPS

_PUMP_ROLE_DISPLAY_NAMES = {
    "nutrient_a_1": "Nährstoff A – Leitung 1",
    "nutrient_a_2": "Nährstoff A – Leitung 2",
    "nutrient_b_1": "Nährstoff B – Leitung 1",
    "nutrient_b_2": "Nährstoff B – Leitung 2",
    "ph_acid": "pH-Minus",
    "ph_base": "pH-Plus",
}


def pump_display_label(controller_id: str, pump: str, role: str | None) -> str:
    role_label = _PUMP_ROLE_DISPLAY_NAMES.get(role, role or "-")
    return f"{role_label} ({controller_id} / {pump})"


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



def number_or_zero(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def int_or_zero(value: Any) -> int:
    return int(round(number_or_zero(value)))


def yes_no(value: Any) -> str:
    return "yes" if value is True else "no"


def recipe_summary(recipe: dict[str, Any]) -> str:
    return (
        f"Tank={recipe.get('target_tank', '-')} | "
        f"RO Water={recipe.get('target_ro_water_l', '-')} L | "
        f"EC={recipe.get('target_ec_ms_cm', '-')} mS/cm | "
        f"pH={recipe.get('target_ph', '-')}"
    )


def dosing_summary(recipe: dict[str, Any]) -> str:
    try:
        preview = get_recipe_preview(recipe)
        nutrients_part = (
            f"Nutrients={preview.total_nutrients_ml:.1f} ml "
            f"(A={preview.nutrient_a_ml:.1f} / B={preview.nutrient_b_ml:.1f})"
        )
    except Exception:
        nutrients_part = "Nutrients=-"

    return (
        f"{nutrients_part} | "
        f"Acid1={recipe.get('volume_acid_1_ml', '-')} ml | "
        f"Acid2={recipe.get('volume_acid_2_ml', '-')} ml | "
        f"Base={recipe.get('volume_base_ml', '-')} ml"
    )


def volume_summary(recipe: dict[str, Any]) -> str:
    try:
        preview = get_recipe_preview(recipe)
        return (
            f"Est. recipe volume={preview.estimated_process_volume_l:.3f} L "
            f"(remaining to 185 L={preview.remaining_capacity_l:.3f} L) | "
            f"Max. possible later RO correction={preview.max_possible_ro_correction_l:.3f} L"
        )
    except Exception:
        return "Est. recipe volume=-"

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
                with ui.card().classes("panel"):
                    ui.label("System Status").classes("panel-title")
                    ui.label("Communication and process modules").classes(
                        "panel-subtitle"
                    )

                    mqtt_bridge_row = create_status_row(
                        "MQTT Sensor Bridge",
                        "OPC-UA → MQTT → SensorSnapshotReader",
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

                    mixer_refill_pump_row = create_actuator_row("Mixer Refill Pump")
                    supply_valve_6_row = create_actuator_row("Supply Valve 6")
                    drain_valve_0_row = create_actuator_row("Drain Valve 0")
                    transfer_pump_row = create_actuator_row("Transfer Pump")
                    mixing_circulation_pump_row = create_actuator_row(
                        "Mixing Circulation Pump"
                    )
                    sensor_circulation_pump_row = create_actuator_row(
                        "Sensor Circulation Pump"
                    )
                    valve_1_row = create_actuator_row("Valve 1")
                    valve_2_row = create_actuator_row("Valve 2")
                    valve_3_row = create_actuator_row("Valve 3")
                    valve_4_row = create_actuator_row("Valve 4")
                    valve_5_row = create_actuator_row("Valve 5")

                with ui.dialog() as dev_info_dialog, ui.card().classes(
                    "recipe-dialog-card dev-info-dialog-card"
                ):
                    ui.label("Maintenance").classes("panel-title")
                    ui.label(
                        "Live status badges and dashboard event log. For development and troubleshooting only."
                    ).classes("panel-subtitle")

                    with ui.row().classes("gap-2 mt-2"):
                        ui.label("RASPI LIVE").classes("top-badge badge-green")
                        ui.label("PYTHON CORE").classes("top-badge badge-blue")
                        last_update_badge = ui.label("Update: -").classes(
                            "top-badge badge-orange"
                        )

                    ui.separator().classes("control-separator")
                    ui.label("Peristaltikpumpen").classes("control-section-title")
                    prime_button = ui.button("Prime").props(
                        "outline color=grey-5 icon=water_drop"
                    )

                    with ui.row().classes("w-full items-center justify-between mt-4"):
                        ui.label("Process Log").classes("control-section-title")
                        preflight_button = ui.button("Run Preflight Check").props(
                            "outline color=grey-5 icon=fact_check"
                        )

                    log_box = ui.label("").classes("log-box")

                    with ui.row().classes("w-full justify-end gap-3 mt-3"):
                        ui.button("Close", on_click=dev_info_dialog.close).props(
                            "color=grey outline"
                        )

                with ui.dialog() as prime_dialog, ui.card().classes(
                    "recipe-dialog-card prime-dialog-card"
                ):
                    ui.label("Peristaltikpumpen primen").classes("panel-title")
                    ui.label(
                        "Entlüftet die ausgewählten Pumpen mit Wasser. Kein regulärer "
                        "Prozessschritt, sondern ein Wartungsvorgang."
                    ).classes("panel-subtitle")

                    ui.label(
                        "Vor dem Start sicherstellen, dass die Schlauchenden korrekt "
                        "positioniert sind und die austretende Flüssigkeit sicher "
                        "aufgefangen oder in den vorgesehenen Behälter geführt wird."
                    ).classes("confirmation-help")

                    ui.label("MCU_A – pH-Dosierung").classes("recipe-section-title mt-3")
                    mcu_a_info_label = ui.label("").classes("confirmation-help")
                    prime_pump_rows: dict[tuple[str, str], dict[str, Any]] = {}
                    for _pump in VALID_PUMPS:
                        with ui.row().classes("items-center gap-2") as _row:
                            _checkbox = ui.checkbox("")
                            _label = ui.label("")
                            _reason_label = ui.label("").classes("error-text prime-pump-reason")
                        prime_pump_rows[("MCU_A", _pump)] = {
                            "row": _row, "checkbox": _checkbox,
                            "label": _label, "reason_label": _reason_label,
                        }

                    ui.label("MCU_B – Nährstoffpumpen").classes("recipe-section-title mt-3")
                    mcu_b_info_label = ui.label("").classes("confirmation-help")
                    for _pump in VALID_PUMPS:
                        with ui.row().classes("items-center gap-2") as _row:
                            _checkbox = ui.checkbox("")
                            _label = ui.label("")
                            _reason_label = ui.label("").classes("error-text prime-pump-reason")
                        prime_pump_rows[("MCU_B", _pump)] = {
                            "row": _row, "checkbox": _checkbox,
                            "label": _label, "reason_label": _reason_label,
                        }

                    ui.label("Prime-Menge: 150 ml je ausgewählter Pumpe").classes(
                        "recipe-section-title mt-3"
                    )
                    ui.label("Jede ausgewählte Pumpe fördert 150 ml.").classes(
                        "confirmation-help"
                    )

                    prime_status_label = ui.label("Bereit").classes("manual-drain-status")
                    prime_progress = ui.linear_progress(value=0.0).classes(
                        "manual-drain-progress w-full"
                    )
                    prime_error_label = ui.label("").classes("error-text")

                    with ui.row().classes("w-full justify-end gap-3 mt-3"):
                        prime_close_button = ui.button("Schließen").props(
                            "color=grey outline"
                        )
                        prime_stop_button = ui.button("Stopp").props("color=negative")
                        prime_start_button = ui.button("Start Priming").props(
                            "color=positive"
                        )

                prime_dialog.props("persistent")

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

                    with ui.card().classes("panel recipe-panel"):
                        ui.label("Recipe / Setpoints").classes("panel-title")
                        ui.label("Active recipe from recipes/dashboard_recipes.json").classes(
                            "panel-subtitle"
                        )

                        recipe_name_label = ui.label("Recipe: -").classes("recipe-name")
                        recipe_summary_label = ui.label("-").classes("recipe-summary")
                        recipe_dosing_label = ui.label("-").classes("recipe-detail")
                        recipe_process_label = ui.label("-").classes("recipe-detail")
                        recipe_volume_label = ui.label("-").classes("recipe-detail")

                        with ui.row().classes("recipe-favorites-row"):
                            favorite_badges = {
                                1: ui.button("F1").classes("recipe-favorite-badge"),
                                2: ui.button("F2").classes("recipe-favorite-badge"),
                                3: ui.button("F3").classes("recipe-favorite-badge"),
                            }
                            for _slot, _badge in favorite_badges.items():
                                _badge.props("flat no-caps").on_click(
                                    lambda _, slot=_slot: handle_favorite_click(slot)
                                )

                        with ui.row().classes("w-full gap-3 mt-3"):
                            edit_recipe_button = ui.button("Edit Recipe").classes(
                                "flex-1 font-bold"
                            ).props("color=primary")
                            apply_recipe_button = ui.button("Reload Active Recipe").classes(
                                "flex-1 font-bold"
                            ).props("color=secondary outline")

                        with ui.dialog() as recipe_dialog, ui.card().classes("recipe-dialog-card"):
                            ui.label("Recipe Editor").classes("panel-title")
                            ui.label(
                                "Three favorites can be saved. Fill volume genuinely drives the next run. "
                                "Sensor circulation is a per-run choice made in Process Control, not part of "
                                "the recipe - loading a recipe never activates it. EC nutrient dosing is "
                                "calculated and validated here but still has no peristaltic-pump consumer "
                                "yet (see README.md Section 15/17)."
                            ).classes("panel-subtitle")

                            with ui.row().classes("w-full gap-3"):
                                recipe_slot_select = ui.select(
                                    options=[1, 2, 3],
                                    value=int(recipe_state["book"].get("active_slot", 1)),
                                    label="Favorite Slot",
                                ).classes("control-input flex-1")
                                recipe_make_active_switch = ui.switch(
                                    "Set as active recipe",
                                    value=True,
                                ).classes("text-slate-200")

                            ui.label("Grunddaten").classes("recipe-section-title")
                            with ui.element("div").classes("recipe-top-grid"):
                                recipe_name_input = ui.input("Recipe Name").classes(
                                    "control-input"
                                )
                                target_tank_input = ui.input("Tank Selection").classes(
                                    "control-input"
                                )
                                target_ro_water_input = ui.number(
                                    "RO Water Volume", suffix="L", min=0, max=180, step=1
                                ).classes("control-input")

                            recipe_legacy_warning_label = ui.label("").classes("error-text")
                            legacy_review_confirmed_switch = ui.switch(
                                "Legacy review completed - I confirm the checked dosing values"
                            ).classes("text-slate-200")

                            with ui.element("div").classes("recipe-form-grid"):
                                with ui.column().classes("recipe-form-section"):
                                    ui.label("EC").classes("recipe-section-title")
                                    target_ec_input = ui.number(
                                        "EC Setpoint", suffix="mS/cm", min=0.5, max=2.5, step=0.05
                                    )
                                    ec_mixing_time_input = ui.number(
                                        "Mixing time", suffix="s (seconds)", min=300, max=1200, step=10
                                    )
                                    nutrient_dosing_enabled_switch = ui.switch(
                                        "Enable nutrient dosing"
                                    )
                                    nutrients_dose_input = ui.number(
                                        "Nutrient total dose", suffix="ml / 100 L RO water", min=0, step=1
                                    )
                                    nutrient_a_percent_input = ui.number(
                                        "Nutrient solution A share", suffix="%", min=0, max=100, step=1
                                    )
                                    nutrient_b_percent_display = ui.number(
                                        "Nutrient solution B share (auto)", suffix="%"
                                    ).props("readonly")
                                    ec_adjustment_factor_input = ui.number(
                                        "EC adjustment factor", min=0.01, max=1, step=0.01
                                    )

                                    ui.label("Calculated for this run").classes(
                                        "recipe-section-title mt-2"
                                    )
                                    calc_total_nutrients_label = ui.label("Total dose: - ml")
                                    calc_nutrient_a_label = ui.label("Amount A: - ml")
                                    calc_nutrient_b_label = ui.label("Amount B: - ml")

                                with ui.column().classes("recipe-form-section"):
                                    ui.label("pH").classes("recipe-section-title")
                                    target_ph_input = ui.number("pH Setpoint", min=0, max=14, step=0.1)
                                    volume_acid_1_input = ui.number(
                                        "Volume acid 1", suffix="ml", min=0, step=1
                                    )
                                    volume_acid_2_input = ui.number(
                                        "Volume acid 2", suffix="ml", min=0, step=1
                                    )
                                    volume_base_input = ui.number(
                                        "Volume base", suffix="ml", min=0, step=1
                                    )
                                    ph_mixing_time_input = ui.number(
                                        "Mixing time", suffix="s", min=0, step=10
                                    )
                                    ph_adjustment_factor_input = ui.number(
                                        "Adjustment factor", min=0, step=0.01
                                    )
                                    ui.label(
                                        "pH correction is not yet included in the process-volume "
                                        "estimate - the exact amount is not known at recipe time."
                                    ).classes("confirmation-help")

                            ui.label("Volumenvorschau").classes("recipe-section-title mt-2")
                            with ui.element("div").classes("recipe-top-grid"):
                                calc_process_volume_label = ui.label("Estimated recipe volume: - L")
                                calc_remaining_capacity_label = ui.label("Remaining capacity to 185 L: - L")
                                calc_max_possible_ro_correction_label = ui.label(
                                    "Max. possible later RO correction: - L"
                                )
                            ui.label(
                                "A fixed RO correction is not part of a recipe - this is only an estimate "
                                "of how much correction capacity would technically remain. A real, "
                                "demand-driven EC correction is not implemented yet."
                            ).classes("confirmation-help")

                            recipe_validation_error_label = ui.label("").classes("error-text")

                            recipe_notes_input = ui.textarea("Note").classes(
                                "control-input w-full"
                            )

                            with ui.row().classes("w-full justify-end gap-3"):
                                ui.button("Cancel", on_click=recipe_dialog.close).props(
                                    "color=grey outline"
                                )
                                save_recipe_button = ui.button("Save Recipe").props(
                                    "color=positive"
                                )
    
                with ui.column().classes("gap-5 w-full area-side"):
                    with ui.card().classes("panel process-control-panel"):
                        ui.label("Process Control").classes("panel-title")
                        #ui.label("Start, reset, emergency stop and maintenance actions").classes(
                        #    "panel-subtitle"
                        #)

                        ui.label("Safety Confirmation").classes("control-section-title")
                        confirmation_input = ui.input(
                            label="Confirmation",
                            placeholder="type exactly: confirmed",
                            password=False,
                        ).classes("control-input w-full")

                        ui.label(
                            "To start the process, type exactly 'confirmed'. GPIOs are only initialized after valid confirmation and hardware_execution_enabled=true."
                        ).classes("confirmation-help")

                        run_sensor_circulation_switch = ui.switch(
                            "Sensor circulation for this run", value=False
                        ).classes("text-slate-200 mt-2")
                        ui.label(
                            "Per-run choice, not saved with the recipe - always resets to off. "
                            "Drives the real sensor-circulation pump."
                        ).classes("confirmation-help")

                        with ui.row().classes("w-full gap-3 mt-3"):
                            start_button = ui.button("Start Process").classes(
                                "flex-1 font-bold"
                            ).props("color=positive")
                            reset_button = ui.button("Reset / Ack").classes(
                                "flex-1 font-bold"
                            ).props("color=primary")

                        stop_button = ui.button("Emergency Stop").classes(
                            "w-full font-bold mt-3"
                        ).props("color=negative")

                        ui.separator().classes("control-separator")

                        ui.label("Maintenance").classes("control-section-title")
                        manual_drain_status_label = ui.label("Manual Drain Jog: ready").classes(
                            "manual-drain-status"
                        )
                        manual_drain_progress = ui.linear_progress(value=0.0).classes(
                            "manual-drain-progress w-full"
                        )
                        manual_drain_button = ui.button("Hold to Drain – max. 30 s").classes(
                            "manual-drain-button w-full font-bold"
                        ).props("color=warning")
                        ui.label(
                            "Press and hold. Release stops immediately. Server-side watchdog stops after 30 seconds."
                        ).classes("confirmation-help")

                        ui.separator().classes("control-separator")

                        ui.label("Tank Cleaning").classes("control-section-title")
                        tank_cleaning_status_label = ui.label(
                            "Tank Cleaning: ready"
                        ).classes("manual-drain-status")
                        tank_cleaning_progress = ui.linear_progress(value=0.0).classes(
                            "manual-drain-progress w-full"
                        )
                        with ui.row().classes("w-full gap-3"):
                            tank_cleaning_start_button = ui.button(
                                "Start Tank Cleaning"
                            ).classes("flex-1 font-bold").props("color=info")
                            tank_cleaning_stop_button = ui.button("Stop").classes(
                                "flex-1 font-bold"
                            ).props("color=negative outline")
                        ui.label(
                            "Fills the Mixing Tank with RO water (target from settings), "
                            "circulates for a fixed hold time, then drains automatically. "
                            "Runs unattended - use Stop or Emergency Stop to abort. "
                            "Uses the confirmation field above."
                        ).classes("confirmation-help")

                        hardware_enabled_label = ui.label(
                            "hardware_execution_enabled: -"
                        ).classes("warn-text mt-3")
                        fill_settings_label = ui.label("Water-cycle settings: -").classes(
                            "text-sm text-slate-300"
                        )
                        required_text_label = ui.label(
                            "required_confirmation_text: -"
                        ).classes("text-sm text-slate-400")

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
    
    event_log: list[str] = ["[OK] NiceGUI dashboard loaded."]

    def add_log(message: str) -> None:
        if not event_log or event_log[-1] != message:
            event_log.append(message)

        if len(event_log) > 18:
            del event_log[:-18]

        log_box.set_text("\n".join(event_log))

    if recipe_state.get("load_error"):
        add_log(f"[RECIPE] {recipe_state['load_error']}")

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

    def update_recipe_card() -> None:
        recipe_book = recipe_state["book"]
        active_slot = int(recipe_book.get("active_slot", 1))
        active_recipe = get_active_recipe(recipe_book)

        recipe_name_label.set_text(
            f"Favorite {active_slot}: {active_recipe.get('recipe_name', '-')}"
        )
        recipe_summary_label.set_text(recipe_summary(active_recipe))
        recipe_dosing_label.set_text(dosing_summary(active_recipe))
        recipe_process_label.set_text(
            f"Nutrient dosing enabled={yes_no(active_recipe.get('nutrient_dosing_enabled'))} | "
            f"Legacy review needed={yes_no(active_recipe.get('legacy_dosing_needs_review'))}"
        )
        recipe_volume_label.set_text(volume_summary(active_recipe))

        for slot, badge in favorite_badges.items():
            recipe = get_recipe_by_slot(recipe_book, slot)
            badge.set_text(f"F{slot}: {recipe.get('recipe_name', '-')}")
            badge.classes(remove="recipe-favorite-active")
            if slot == active_slot:
                badge.classes("recipe-favorite-active")

    def build_recipe_dict_from_form(slot: int) -> dict[str, Any]:
        return {
            "recipe_name": recipe_name_input.value or f"Favorit {slot}",
            "target_tank": target_tank_input.value or "Ch 1 - T1",
            "target_ro_water_l": number_or_zero(target_ro_water_input.value),

            "target_ec_ms_cm": number_or_zero(target_ec_input.value),
            "ec_mixing_time_seconds": int_or_zero(ec_mixing_time_input.value),
            "nutrient_dosing_enabled": bool(nutrient_dosing_enabled_switch.value),
            "nutrients_ml_per_100_l": number_or_zero(nutrients_dose_input.value),
            "nutrient_a_percent": number_or_zero(nutrient_a_percent_input.value),
            "ec_adjustment_factor": number_or_zero(ec_adjustment_factor_input.value),

            "target_ph": number_or_zero(target_ph_input.value),
            "volume_acid_1_ml": number_or_zero(volume_acid_1_input.value),
            "volume_acid_2_ml": number_or_zero(volume_acid_2_input.value),
            "volume_base_ml": number_or_zero(volume_base_input.value),
            "ph_mixing_time_seconds": int_or_zero(ph_mixing_time_input.value),
            "ph_adjustment_factor": number_or_zero(ph_adjustment_factor_input.value),

            "notes": recipe_notes_input.value or "",

            "legacy_volume_stock_1_ml": recipe_form_state.get("legacy_volume_stock_1_ml"),
            "legacy_volume_stock_2_ml": recipe_form_state.get("legacy_volume_stock_2_ml"),
            # The "Legacy review completed" switch is the ONLY way this flag
            # can go from true to false - it never clears itself just
            # because some number was typed (see docs/OPEN_RECIPE_DECISIONS.md).
            "legacy_dosing_needs_review": not bool(legacy_review_confirmed_switch.value),
        }

    def update_recipe_calculations() -> None:
        """Live recompute, called on every relevant field change - B always
        follows A, and every calculated/estimated value updates immediately.
        Deliberately never clamps or blocks typing; only the red error label
        and the Save button's behavior enforce the actual limits."""
        slot = int(recipe_slot_select.value or 1)
        raw_recipe = build_recipe_dict_from_form(slot)

        nutrient_b_percent_display.value = nutrient_b_percent(
            number_or_zero(nutrient_a_percent_input.value)
        )

        try:
            preview = get_recipe_preview(raw_recipe)
        except Exception:
            preview = None

        if preview is not None:
            calc_total_nutrients_label.set_text(f"Total dose: {preview.total_nutrients_ml:.3f} ml")
            calc_nutrient_a_label.set_text(f"Amount A: {preview.nutrient_a_ml:.3f} ml")
            calc_nutrient_b_label.set_text(f"Amount B: {preview.nutrient_b_ml:.3f} ml")
            calc_process_volume_label.set_text(
                f"Estimated recipe volume: {preview.estimated_process_volume_l:.3f} L"
            )
            calc_remaining_capacity_label.set_text(
                f"Remaining capacity to 185 L: {preview.remaining_capacity_l:.3f} L"
            )
            # Purely informational - no fixed RO correction is part of a
            # recipe or RunConfigSnapshot anymore, see
            # domain/recipe_limits.py::compute_max_possible_ro_correction_l.
            calc_max_possible_ro_correction_label.set_text(
                f"Max. possible later RO correction: {preview.max_possible_ro_correction_l:.3f} L"
            )

        errors = StoredRecipe.from_dict(raw_recipe).validate()
        recipe_validation_error_label.set_text("\n".join(errors))

    def load_recipe_form_from_slot() -> None:
        recipe_book = recipe_state["book"]
        slot = int(recipe_slot_select.value or 1)
        recipe = get_recipe_by_slot(recipe_book, slot)

        recipe_name_input.value = recipe.get("recipe_name", "")
        target_tank_input.value = recipe.get("target_tank", "")
        target_ro_water_input.value = recipe.get("target_ro_water_l", 0)

        target_ec_input.value = recipe.get("target_ec_ms_cm", 0)
        ec_mixing_time_input.value = recipe.get("ec_mixing_time_seconds", 0)
        nutrient_dosing_enabled_switch.value = bool(recipe.get("nutrient_dosing_enabled", False))
        nutrients_dose_input.value = recipe.get("nutrients_ml_per_100_l", 0)
        nutrient_a_percent_input.value = recipe.get("nutrient_a_percent", 50)
        ec_adjustment_factor_input.value = recipe.get("ec_adjustment_factor", 0)

        target_ph_input.value = recipe.get("target_ph", 0)
        volume_acid_1_input.value = recipe.get("volume_acid_1_ml", 0)
        volume_acid_2_input.value = recipe.get("volume_acid_2_ml", 0)
        volume_base_input.value = recipe.get("volume_base_ml", 0)
        ph_mixing_time_input.value = recipe.get("ph_mixing_time_seconds", 0)
        ph_adjustment_factor_input.value = recipe.get("ph_adjustment_factor", 0)

        recipe_notes_input.value = recipe.get("notes", "")

        recipe_form_state["legacy_volume_stock_1_ml"] = recipe.get("legacy_volume_stock_1_ml")
        recipe_form_state["legacy_volume_stock_2_ml"] = recipe.get("legacy_volume_stock_2_ml")
        recipe_form_state["legacy_dosing_needs_review"] = bool(
            recipe.get("legacy_dosing_needs_review", False)
        )
        legacy_review_confirmed_switch.value = not recipe_form_state["legacy_dosing_needs_review"]

        if recipe_form_state["legacy_dosing_needs_review"]:
            recipe_legacy_warning_label.set_text(
                "Legacy EC stock volumes need manual review (not auto-converted - the "
                "reference water amount they were sized for is not reliably known): "
                f"Stock 1={recipe_form_state['legacy_volume_stock_1_ml']} ml, "
                f"Stock 2={recipe_form_state['legacy_volume_stock_2_ml']} ml. "
                "Nutrient total dose defaults to 0 ml/100L until a confirmed value is entered."
            )
        else:
            recipe_legacy_warning_label.set_text("")

        update_recipe_calculations()

    def open_recipe_dialog() -> None:
        try:
            recipe_state["book"] = load_recipe_book()
        except RecipeBookCorruptedError as exc:
            ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
            add_log(f"[RECIPE] {exc}")
            return

        recipe_slot_select.value = int(recipe_state["book"].get("active_slot", 1))
        recipe_make_active_switch.value = True
        load_recipe_form_from_slot()
        recipe_dialog.open()

    def handle_apply_active_recipe() -> None:
        try:
            recipe_state["book"] = load_recipe_book()
        except RecipeBookCorruptedError as exc:
            ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
            add_log(f"[RECIPE] {exc}")
            return

        update_recipe_card()
        active_recipe = get_active_recipe(recipe_state["book"])
        ui.notify(
            f"Active recipe loaded: {active_recipe.get('recipe_name', '-')}",
            color="positive",
        )
        add_log(f"[RECIPE] Active recipe: {active_recipe.get('recipe_name', '-')}")

    def handle_save_recipe() -> None:
        slot = int(recipe_slot_select.value or 1)
        recipe = build_recipe_dict_from_form(slot)

        try:
            recipe_state["book"] = save_recipe_to_slot(
                slot=slot,
                recipe=recipe,
                make_active=bool(recipe_make_active_switch.value),
            )
        except RecipeValidationError as exc:
            recipe_validation_error_label.set_text(str(exc))
            ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
            add_log(f"[RECIPE] Save blocked (Slot {slot}): validation failed.")
            return

        update_recipe_card()
        recipe_dialog.close()
        ui.notify("Recipe saved.", color="positive")
        add_log(f"[RECIPE] Favorite {slot} saved: {recipe['recipe_name']}")

    def handle_favorite_click(slot: int) -> None:
        """F1/F2/F3 click: only changes which favorite is active - never
        starts a process, never touches hardware. Strict ordering: set the
        active slot (which itself loads fresh, validates, and saves
        atomically under lock) FIRST, only then update recipe_state and the
        UI - so update_recipe_card() never runs against a stale book."""
        try:
            fresh_book = set_active_slot(slot)
        except (RecipeBookCorruptedError, ValueError) as exc:
            ui.notify(str(exc), color="negative", multi_line=True, timeout=0, close_button=True)
            add_log(f"[RECIPE] Favorite {slot} activation blocked: {exc}")
            return

        recipe_state["book"] = fresh_book
        update_recipe_card()

        active_recipe = get_recipe_by_slot(recipe_state["book"], slot)
        ui.notify(f"Active recipe: {active_recipe.get('recipe_name', '-')}", color="positive")
        add_log(f"[RECIPE] Favorite {slot} activated: {active_recipe.get('recipe_name', '-')}")

    dev_info_button.on_click(dev_info_dialog.open)
    edit_recipe_button.on_click(open_recipe_dialog)
    apply_recipe_button.on_click(handle_apply_active_recipe)
    save_recipe_button.on_click(handle_save_recipe)
    recipe_slot_select.on_value_change(lambda _: load_recipe_form_from_slot())

    for _recipe_calc_input in (
        target_ro_water_input,
        nutrient_dosing_enabled_switch,
        nutrients_dose_input,
        nutrient_a_percent_input,
        ec_adjustment_factor_input,
        legacy_review_confirmed_switch,
    ):
        _recipe_calc_input.on_value_change(lambda _: update_recipe_calculations())

    update_recipe_card()

    def handle_start() -> None:
        control_data = controller.get_process_control_status()

        if not control_data["hardware_execution_enabled"]:
            message = (
                "Start blocked: hardware_execution_enabled is false. "
                "NiceGUI remains in monitor mode."
            )
            ui.notify(message, color="negative")
            add_log(f"[SAFE] {message}")
            return

        entered_text = (confirmation_input.value or "").strip()
        required_text = str(control_data["required_confirmation_text"]).strip()

        add_log(
            f"[DEBUG] Confirmation received: '{entered_text}' | required: '{required_text}'"
        )

        if entered_text != required_text:
            message = (
                "Start blocked: confirmation text is wrong. "
                f"Entered='{entered_text}' Required='{required_text}'"
            )
            ui.notify(message, color="negative")
            add_log(f"[BLOCKED] {message}")
            return

        # Raw value, deliberately NOT bool(...)-coerced here: RunOptions is
        # validated strictly (type(value) is bool) inside
        # controller.start_fill_and_measure() -> build_run_config() ->
        # RunConfigSnapshot.build() -> RunOptions.validate(), which already
        # turns an invalid value into a blocked-start message below - a
        # pre-emptive bool() would silently mask a bug that produced a
        # non-bool switch value instead of surfacing it.
        run_options = RunOptions(sensor_circulation_enabled=run_sensor_circulation_switch.value)
        result = controller.start_fill_and_measure(entered_text, run_options=run_options)

        if result["success"]:
            ui.notify(result["message"], color="positive")
            add_log(f"[OK] {result['message']}")
            # Reset ONLY after an actually accepted start - a blocked start
            # (wrong confirmation text, invalid recipe, already running,
            # validation error) must leave the user's choice untouched so
            # they can fix the problem and retry without re-selecting it.
            run_sensor_circulation_switch.value = False
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

    async def handle_stop() -> None:
        stop_button.props("loading")
        stop_button.disable()

        try:
            result = await controller.emergency_stop()

            if result["success"]:
                ui.notify(result["message"], color="warning")
                add_log(f"[STOP] {result['message']}")
            else:
                ui.notify(result["message"], color="negative")
                add_log(f"[ERROR] {result['message']}")
        finally:
            stop_button.props(remove="loading")
            stop_button.enable()

    def handle_manual_drain_start() -> None:
        result = controller.start_manual_drain_jog()

        if result["success"]:
            add_log(f"[MANUAL DRAIN] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[BLOCKED] {result['message']}")

    def handle_manual_drain_stop() -> None:
        result = controller.stop_manual_drain_jog()
        add_log(f"[MANUAL DRAIN] {result['message']}")

    def handle_tank_cleaning_start() -> None:
        entered_text = (confirmation_input.value or "").strip()
        result = controller.start_tank_cleaning(entered_text)

        if result["success"]:
            add_log(f"[TANK CLEANING] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[BLOCKED] {result['message']}")

    def handle_tank_cleaning_stop() -> None:
        result = controller.stop_tank_cleaning()
        add_log(f"[TANK CLEANING] {result['message']}")

    def update_prime_pump_checklist() -> None:
        options = describe_pump_options()

        for (controller_id, pump), option in options.items():
            row_refs = prime_pump_rows[(controller_id, pump)]
            checkbox = row_refs["checkbox"]
            label = row_refs["label"]
            reason_label = row_refs["reason_label"]
            row = row_refs["row"]

            if option["hidden"]:
                row.set_visibility(False)
                continue

            row.set_visibility(True)
            label.set_text(pump_display_label(controller_id, pump, option["role"]))

            if option["blocked"]:
                checkbox.set_value(False)
                checkbox.disable()
                reason_label.set_text("; ".join(option["blocked_reasons"]) or "Nicht verfügbar.")
                row.classes(add="prime-pump-row-disabled")
            else:
                checkbox.enable()
                reason_label.set_text("")
                row.classes(remove="prime-pump-row-disabled")

        for controller_id, info_label in (("MCU_A", mcu_a_info_label), ("MCU_B", mcu_b_info_label)):
            unassigned = [
                pump for pump in VALID_PUMPS if options[(controller_id, pump)]["hidden"]
            ]
            if unassigned:
                info_label.set_text(
                    f"{controller_id} ist noch nicht vollständig gemappt "
                    f"({', '.join(unassigned)} nicht zugeordnet)."
                )
            else:
                info_label.set_text("")

    def open_prime_dialog() -> None:
        update_prime_pump_checklist()
        prime_dialog.open()

    def _collect_selected_prime_pumps() -> dict[str, list[str]]:
        selected: dict[str, list[str]] = {}
        for controller_id in VALID_CONTROLLERS:
            pumps = [
                pump
                for pump in VALID_PUMPS
                if prime_pump_rows[(controller_id, pump)]["checkbox"].value
            ]
            if pumps:
                selected[controller_id] = pumps
        return selected

    def handle_prime_start() -> None:
        pumps = _collect_selected_prime_pumps()
        if not pumps:
            ui.notify("Bitte mindestens eine Pumpe auswählen.", color="negative")
            add_log("[PRIME] Bitte mindestens eine Pumpe auswählen.")
            return

        result = controller.start_prime(pumps)

        if result["success"]:
            ui.notify(result["message"], color="positive")
            add_log(f"[PRIME] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[BLOCKED] {result['message']}")

    async def handle_prime_stop() -> None:
        # Sofort, synchron, VOR dem await: der Nutzer soll augenblicklich
        # sehen, dass der Stopp angenommen wurde - nicht erst, wenn
        # stop_prime() (kann bis zu ~40s dauern) zurückkehrt. refresh()
        # übernimmt danach "Stopp wird ausgeführt", solange phase==STOPPING.
        prime_status_label.set_text(
            "Stopp angefordert – der aktuell laufende Dosierschritt wird beendet."
        )
        prime_stop_button.props("loading")
        prime_stop_button.disable()
        try:
            result = await controller.stop_prime()
            add_log(f"[PRIME] {result['message']}")
        finally:
            prime_stop_button.props(remove="loading")
        # Der Dialog bleibt beim normalen Stopp-Klick grundsätzlich offen
        # und zeigt anschließend das Ergebnis - kein prime_dialog.close() hier.

    async def handle_prime_close() -> None:
        """
        5-Schritt-Sequenz: (1) is_active prüfen, (2) falls aktiv: UI auf
        "Stopp angefordert"/"wird ausgeführt" setzen, (3) stop_prime()
        abwarten (request_stop()+wait_stopped() intern), (4) Status ERNEUT
        lesen (nicht nur das Ergebnis von stop_prime() selbst vertrauen),
        (5) nur schließen, wenn dieser erneute Check is_active==False
        bestätigt - sonst Dialog offen (persistent) lassen und einen
        verständlichen Hinweis zeigen.
        """
        prime_status = controller.get_process_control_status().get("prime", {})
        if not prime_status.get("is_active"):
            prime_dialog.close()
            return

        prime_status_label.set_text(
            "Stopp angefordert – der aktuell laufende Dosierschritt wird beendet."
        )
        prime_close_button.props("loading")
        prime_close_button.disable()
        try:
            result = await controller.stop_prime()
        finally:
            prime_close_button.props(remove="loading")
            prime_close_button.enable()

        final_status = controller.get_process_control_status().get("prime", {})
        if final_status.get("is_active"):
            prime_error_label.set_text(
                "Der Prime-Vorgang konnte noch nicht als beendet bestätigt werden. "
                "Der Dialog bleibt aus Sicherheitsgründen geöffnet."
            )
            add_log(f"[PRIME] Schließen abgelehnt - Stopp noch nicht bestätigt: {result.get('message')}")
            return  # Dialog bleibt offen (bereits persistent)

        add_log(
            "[PRIME] Dialog wurde während des Primings geschlossen - "
            f"gestoppt: {result.get('message')}"
        )
        prime_dialog.close()

    async def handle_run_preflight() -> None:
        preflight_button.props("loading")
        preflight_button.disable()
        add_log("[PREFLIGHT] Running checks...")

        try:
            result = await controller.run_preflight_check()
            add_log("[PREFLIGHT]\n" + "\n".join(result["lines"]))

            notify_color = {
                "OK": "positive",
                "WARN": "warning",
                "FAIL": "negative",
                "ERROR": "negative",
            }.get(result["status"], "negative")
            ui.notify(f"Preflight: {result['status']}", color=notify_color)
        finally:
            preflight_button.props(remove="loading")
            preflight_button.enable()

    start_button.on_click(handle_start)
    reset_button.on_click(handle_reset)
    stop_button.on_click(handle_stop)
    tank_cleaning_start_button.on_click(handle_tank_cleaning_start)
    tank_cleaning_stop_button.on_click(handle_tank_cleaning_stop)
    preflight_button.on_click(handle_run_preflight)

    prime_button.on_click(open_prime_dialog)
    prime_start_button.on_click(handle_prime_start)
    prime_stop_button.on_click(handle_prime_stop)
    prime_close_button.on_click(handle_prime_close)

    manual_drain_button.on("mousedown", lambda _: handle_manual_drain_start())
    manual_drain_button.on("mouseup", lambda _: handle_manual_drain_stop())
    manual_drain_button.on("mouseleave", lambda _: handle_manual_drain_stop())
    manual_drain_button.on("touchstart", lambda _: handle_manual_drain_start())
    manual_drain_button.on("touchend", lambda _: handle_manual_drain_stop())
    manual_drain_button.on("touchcancel", lambda _: handle_manual_drain_stop())

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
        mqtt_bridge_row["value"].set_text("connected" if mqtt_bridge_ok else "check")

        set_dot(process_reader_row, process_data["reader_started"])
        process_reader_row["value"].set_text(
            "running" if process_data["reader_started"] else "stopped"
        )

        set_dot(snapshot_row, sensor_available)
        snapshot_row["value"].set_text("current" if sensor_available else "stale")

        set_dot(process_payload_row, process_available)
        process_payload_row["value"].set_text(
            "current" if process_available else "no payload"
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
        set_actuator(valve_1_row, process_data["valve_1"])
        set_actuator(valve_2_row, process_data["valve_2"])
        set_actuator(valve_3_row, process_data["valve_3"])
        set_actuator(valve_4_row, process_data["valve_4"])
        set_actuator(valve_5_row, process_data["valve_5"])

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

        control_message_label.set_text(str(control_data["last_message"] or "Ready"))

        if control_data["error"]:
            control_error_label.set_text(f"Controller error: {control_data['error']}")
        else:
            control_error_label.set_text("")

        manual_drain_status = control_data.get("manual_drain_jog", {}) or {}
        manual_drain_active = bool(manual_drain_status.get("is_active", False))
        manual_drain_elapsed = float(manual_drain_status.get("elapsed_seconds", 0.0) or 0.0)
        manual_drain_max = float(manual_drain_status.get("max_seconds", 30.0) or 30.0)
        manual_drain_progress_value = float(manual_drain_status.get("progress", 0.0) or 0.0)

        manual_drain_progress.set_value(manual_drain_progress_value)

        if manual_drain_active:
            manual_drain_button.set_text(
                f"Draining... {manual_drain_elapsed:.1f}/{manual_drain_max:.0f} s"
            )
            manual_drain_button.classes("manual-drain-active")
            manual_drain_status_label.set_text(
                f"Manual Drain Jog: running | {manual_drain_elapsed:.1f}/{manual_drain_max:.0f} s"
            )
        else:
            manual_drain_button.set_text("Hold to Drain – max. 30 s")
            manual_drain_button.classes(remove="manual-drain-active")
            manual_drain_status_label.set_text(
                f"Manual Drain Jog: {manual_drain_status.get('last_message', 'ready')}"
            )

        tank_cleaning_status = control_data.get("tank_cleaning", {}) or {}
        tank_cleaning_active = bool(tank_cleaning_status.get("is_active", False))
        tank_cleaning_phase = tank_cleaning_status.get("phase", "IDLE")
        tank_cleaning_elapsed = float(tank_cleaning_status.get("phase_elapsed_seconds", 0.0) or 0.0)
        tank_cleaning_current = tank_cleaning_status.get("current_liters")
        tank_cleaning_target = (
            tank_cleaning_status.get("target_liters")
            or control_data.get("tank_cleaning_target_liters")
            or 0.0
        )
        tank_cleaning_hold = (
            tank_cleaning_status.get("hold_seconds")
            or control_data.get("tank_cleaning_hold_seconds")
            or 0.0
        )
        tank_cleaning_hardware_enabled = bool(
            control_data.get("tank_cleaning_hardware_execution_enabled", False)
        )

        if tank_cleaning_active:
            if tank_cleaning_phase == "FILLING" and tank_cleaning_target:
                progress_value = min(1.0, (tank_cleaning_current or 0.0) / tank_cleaning_target) * 0.4
            elif tank_cleaning_phase == "HOLDING" and tank_cleaning_hold:
                progress_value = 0.4 + min(1.0, tank_cleaning_elapsed / tank_cleaning_hold) * 0.3
            elif tank_cleaning_phase == "DRAINING":
                progress_value = 0.85
            else:
                progress_value = 0.0

            tank_cleaning_progress.set_value(progress_value)
            tank_cleaning_status_label.set_text(
                f"Tank Cleaning: {tank_cleaning_phase} | "
                f"{fmt(tank_cleaning_current, 'L')} | {tank_cleaning_elapsed:.0f}s"
            )
        else:
            tank_cleaning_progress.set_value(
                1.0 if tank_cleaning_phase == "FINISHED" else 0.0
            )
            tank_cleaning_status_label.set_text(
                f"Tank Cleaning: {tank_cleaning_status.get('last_message', 'ready')}"
            )

        prime_status = control_data.get("prime", {}) or {}
        prime_active = bool(prime_status.get("is_active", False))
        prime_phase = prime_status.get("phase", "IDLE")
        prime_results = prime_status.get("pump_results", []) or []
        prime_plan_total = sum(len(pumps) for pumps in (prime_status.get("pumps_plan", {}) or {}).values())

        if prime_active:
            progress_value = (len(prime_results) / prime_plan_total) if prime_plan_total else 0.0
            prime_progress.set_value(progress_value)
            if prime_phase == "STOPPING":
                # Keine Formulierung, die einen sofortigen Pumpenstopp
                # verspricht - der aktuell laufende Chunk kann intern noch
                # bis zu ~35s weiterlaufen, bevor der nächste Checkpoint
                # den Stopp tatsächlich wirksam werden lässt.
                prime_status_label.set_text(
                    "Stopp wird ausgeführt – der aktuell laufende Dosierschritt wird beendet."
                )
            else:
                current_controller = prime_status.get("current_controller") or "-"
                current_pump = prime_status.get("current_pump") or "-"
                current_role = prime_status.get("current_pump_role") or "-"
                prime_status_label.set_text(
                    f"Priming läuft: {current_controller}/{current_pump} ({current_role}) - "
                    f"{len(prime_results)}/{prime_plan_total} Pumpen abgeschlossen"
                )
            prime_error_label.set_text("")
        else:
            # Erst hier (is_active bestätigt False) gilt der Vorgang als
            # "Gestoppt"/abgeschlossen - last_message spiegelt das bereits
            # korrekt wider ("Priming gestoppt: ..."/"Priming abgeschlossen.").
            prime_progress.set_value(1.0 if prime_phase == "FINISHED" else 0.0)
            prime_status_label.set_text(prime_status.get("last_message", "Bereit"))
            prime_error_label.set_text(prime_status.get("last_error") or "")

        prime_selection_locked = control_data["is_running"] or manual_drain_active or tank_cleaning_active or prime_active

        if (
            control_data["is_running"]
            or manual_drain_active
            or tank_cleaning_active
            or prime_active
            or not hardware_enabled
        ):
            start_button.disable()
        else:
            start_button.enable()

        if control_data["is_running"] or manual_drain_active or tank_cleaning_active or prime_active:
            reset_button.disable()
        else:
            reset_button.enable()

        if control_data["is_running"] or tank_cleaning_active or prime_active or not hardware_enabled:
            manual_drain_button.disable()
        else:
            manual_drain_button.enable()

        if (
            control_data["is_running"]
            or manual_drain_active
            or tank_cleaning_active
            or prime_active
            or not tank_cleaning_hardware_enabled
        ):
            tank_cleaning_start_button.disable()
        else:
            tank_cleaning_start_button.enable()

        if tank_cleaning_active:
            tank_cleaning_stop_button.enable()
        else:
            tank_cleaning_stop_button.disable()

        if prime_selection_locked:
            prime_start_button.disable()
            for row_refs in prime_pump_rows.values():
                row_refs["checkbox"].disable()
        else:
            prime_start_button.enable()
            update_prime_pump_checklist()

        if prime_active:
            prime_stop_button.enable()
        else:
            prime_stop_button.disable()

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
