from typing import Any

from nicegui import ui

from domain.recipe_model import RunOptions
from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.components.formatting import fmt, percent_to_display
from nicegui_dashboard.components.status_widgets import create_metric_box, create_tank_gauge
from nicegui_dashboard.pages.dashboard.maintenance_section import (
    build_maintenance,
    handle_run_preflight,
    make_add_log,
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

                    card_widgets, editor_widgets = build_recipe_section(recipe_state)

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

    start_button.on_click(handle_start)
    reset_button.on_click(handle_reset)
    stop_button.on_click(handle_stop)
    tank_cleaning_start_button.on_click(handle_tank_cleaning_start)
    tank_cleaning_stop_button.on_click(handle_tank_cleaning_stop)
    maintenance_widgets["preflight_button"].on_click(
        lambda: handle_run_preflight(controller, maintenance_widgets["preflight_button"], add_log)
    )

    maintenance_widgets["prime_button"].on_click(open_prime_dialog)
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
