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

    wire_process_control_handlers(controller, process_control_widgets, add_log)
    maintenance_widgets["preflight_button"].on_click(
        lambda: handle_run_preflight(controller, maintenance_widgets["preflight_button"], add_log)
    )

    maintenance_widgets["prime_button"].on_click(open_prime_dialog)
    prime_start_button.on_click(handle_prime_start)
    prime_stop_button.on_click(handle_prime_stop)
    prime_close_button.on_click(handle_prime_close)

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
