"""Process-Control-Panel: Start/Reset/Emergency-Stop, Manual Drain Jog, Tank Cleaning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from nicegui import ui

from domain.recipe_model import RunOptions
from nicegui_dashboard.components.formatting import fmt


@dataclass
class ProcessControlWidgets:
    safety_confirm_switch: ui.switch
    run_sensor_circulation_switch: ui.switch
    tank_emptied_confirm_switch: ui.switch
    start_button: ui.button
    reset_button: ui.button
    stop_button: ui.button
    manual_drain_status_label: ui.label
    manual_drain_progress: ui.linear_progress
    manual_drain_button: ui.button
    tank_cleaning_status_label: ui.label
    tank_cleaning_progress: ui.linear_progress
    tank_cleaning_start_button: ui.button
    tank_cleaning_stop_button: ui.button
    hardware_enabled_label: ui.label
    fill_settings_label: ui.label
    required_text_label: ui.label


_DOSING_PHASE_TEXT = {
    "RUNNING": "laeuft",
    "STOPPING": "wird gestoppt",
    "FINISHED": "abgeschlossen",
    "ABORTED": "abgebrochen",
    "ERROR": "FEHLER",
    "IDLE": "bereit",
}


def format_dosing_status(dosing: dict[str, Any] | None) -> tuple[str, float, str]:
    """Anzeige der MCU_B-Naehrstoffdosierung: (Zeile, Fortschritt 0..1, Fehlertext).
    Rein, ohne UI. ``dosing`` ist ``NutrientDosingController.get_status()`` oder None
    (kein Dosierlauf). Nur softwareseitig bestaetigte Mengen zaehlen als Fortschritt;
    eine unklare Menge wird immer ausdruecklich ausgewiesen."""
    if not dosing:
        return "Dosierung: -", 0.0, ""

    phase = str(dosing.get("phase") or "IDLE")
    target_ul = int(dosing.get("planned_ul") or dosing.get("target_ul") or 0)
    confirmed_ul = int(dosing.get("confirmed_ul") or 0)
    uncertain_ul = int(dosing.get("uncertain_ul") or 0)
    progress = min(1.0, confirmed_ul / target_ul) if target_ul > 0 else 0.0

    parts = [f"Dosierung: {_DOSING_PHASE_TEXT.get(phase, phase)}"]
    pump = dosing.get("current_pump")
    if pump:
        role = next((p.get("role") for p in dosing.get("pumps") or [] if p.get("pump") == pump), None)
        parts.append(f"Pumpe {pump} ({role})" if role else f"Pumpe {pump}")
    parts.append(f"{confirmed_ul / 1000:.1f}/{target_ul / 1000:.1f} ml bestaetigt")
    per_pump = [
        f"{p['pump']} {int(p.get('confirmed_ul') or 0) / 1000:.1f}/{int(p.get('target_ul') or 0) / 1000:.1f}"
        for p in dosing.get("pumps") or []
    ]
    if per_pump:
        parts.append(" | ".join(per_pump))

    error = ""
    if phase in ("ABORTED", "ERROR"):
        error = f"Dosierung {dosing.get('completion_reason') or phase}"
        if dosing.get("last_error"):
            error += f": {dosing['last_error']}"
    if uncertain_ul > 0:
        error = (error + " | " if error else "") + (
            f"{uncertain_ul / 1000:.1f} ml unklar (Chunk nicht wiederholt, vor Neustart pruefen)"
        )
    return " | ".join(parts), progress, error


def build_process_control() -> ProcessControlWidgets:
    with ui.card().classes("panel process-control-panel"):
        ui.label("Process Control").classes("panel-title")
        #ui.label("Start, reset, emergency stop and maintenance actions").classes(
        #    "panel-subtitle"
        #)

        ui.label("Safety Confirmation").classes("control-section-title")
        safety_confirm_switch = ui.switch(
            "Ich bestätige die Sicherheitsfreigabe für diesen Start.",
            value=False,
        ).classes("text-slate-200 mt-2")

        ui.label(
            "Gilt nur für den nächsten Startversuch (Prozess oder Tank Cleaning) und wird nach "
            "jedem Versuch zurückgesetzt. GPIOs werden nur initialisiert, wenn die Bestätigung "
            "gesetzt ist und hardware_execution_enabled=true."
        ).classes("confirmation-help")

        run_sensor_circulation_switch = ui.switch(
            "Sensor circulation for this run", value=False
        ).classes("text-slate-200 mt-2")
        ui.label(
            "Per-run choice, not saved with the recipe - always resets to off. "
            "Drives the real sensor-circulation pump."
        ).classes("confirmation-help")

        tank_emptied_confirm_switch = ui.switch(
            "Ich bestätige, dass der Mischtank für diesen Rezeptlauf entleert wurde.",
            value=False,
        ).classes("text-slate-200 mt-2")
        ui.label(
            "Eigene Betreiberbestätigung, keine automatische Sensormessung: Der Füllstandssensor "
            "kann Leere unterhalb von ca. 10 L nicht nachweisen. Gilt nur für den nächsten "
            "Startversuch und wird nach jedem Versuch (auch nach einem blockierten) zurückgesetzt."
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
            "Uses the safety confirmation switch above."
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

    return ProcessControlWidgets(
        safety_confirm_switch=safety_confirm_switch,
        run_sensor_circulation_switch=run_sensor_circulation_switch,
        tank_emptied_confirm_switch=tank_emptied_confirm_switch,
        start_button=start_button,
        reset_button=reset_button,
        stop_button=stop_button,
        manual_drain_status_label=manual_drain_status_label,
        manual_drain_progress=manual_drain_progress,
        manual_drain_button=manual_drain_button,
        tank_cleaning_status_label=tank_cleaning_status_label,
        tank_cleaning_progress=tank_cleaning_progress,
        tank_cleaning_start_button=tank_cleaning_start_button,
        tank_cleaning_stop_button=tank_cleaning_stop_button,
        hardware_enabled_label=hardware_enabled_label,
        fill_settings_label=fill_settings_label,
        required_text_label=required_text_label,
    )


def _consume_safety_confirmation(widgets: ProcessControlWidgets) -> bool:
    """Liest die Sicherheitsbestaetigung genau einmal und setzt den Schalter sofort
    zurueck (gilt nur fuer einen Versuch). Nur ein echtes True zaehlt (fail-closed)."""
    confirmed = widgets.safety_confirm_switch.value is True
    widgets.safety_confirm_switch.value = False
    return confirmed


def handle_start(controller: Any, widgets: ProcessControlWidgets, add_log: Callable[[str], None]) -> None:
    # The tank-emptied confirmation is valid for this single start attempt
    # only: read the raw value once and reset the switch immediately, so every
    # path below (early dashboard block, controller block, success, exception)
    # leaves it off. Unlike the circulation switch it is never kept for a retry.
    tank_emptied_confirmed = widgets.tank_emptied_confirm_switch.value
    widgets.tank_emptied_confirm_switch.value = False
    safety_confirmed = _consume_safety_confirmation(widgets)

    control_data = controller.get_process_control_status()

    if not control_data["hardware_execution_enabled"]:
        message = (
            "Start blocked: hardware_execution_enabled is false. "
            "NiceGUI remains in monitor mode."
        )
        ui.notify(message, color="negative")
        add_log(f"[SAFE] {message}")
        return

    required_text = str(control_data["required_confirmation_text"]).strip()
    if not safety_confirmed:
        message = "Start blocked: safety confirmation is not set."
        ui.notify(message, color="negative")
        add_log(f"[BLOCKED] {message}")
        return
    entered_text = required_text

    # Raw value, deliberately NOT bool(...)-coerced here: RunOptions is
    # validated strictly (type(value) is bool) inside
    # controller.start_fill_and_measure() -> build_run_config() ->
    # RunConfigSnapshot.build() -> RunOptions.validate(), which already
    # turns an invalid value into a blocked-start message below - a
    # pre-emptive bool() would silently mask a bug that produced a
    # non-bool switch value instead of surfacing it.
    run_options = RunOptions(
        sensor_circulation_enabled=widgets.run_sensor_circulation_switch.value,
        tank_emptied_confirmed=tank_emptied_confirmed,
    )
    result = controller.start_fill_and_measure(entered_text, run_options=run_options)

    if result["success"]:
        ui.notify(result["message"], color="positive")
        add_log(f"[OK] {result['message']}")
        # Reset ONLY after an actually accepted start - a blocked start
        # (wrong confirmation text, invalid recipe, already running,
        # validation error) must leave the user's choice untouched so
        # they can fix the problem and retry without re-selecting it.
        widgets.run_sensor_circulation_switch.value = False
    else:
        ui.notify(result["message"], color="negative")
        add_log(f"[BLOCKED] {result['message']}")


def handle_reset(controller: Any, add_log: Callable[[str], None]) -> None:
    result = controller.acknowledge_error()

    if result["success"]:
        ui.notify(result["message"], color="positive")
        add_log(f"[RESET] {result['message']}")
    else:
        ui.notify(result["message"], color="negative")
        add_log(f"[BLOCKED] {result['message']}")


async def handle_stop(controller: Any, widgets: ProcessControlWidgets, add_log: Callable[[str], None]) -> None:
    widgets.stop_button.props("loading")
    widgets.stop_button.disable()

    try:
        result = await controller.emergency_stop()

        if result["success"]:
            ui.notify(result["message"], color="warning")
            add_log(f"[STOP] {result['message']}")
        else:
            ui.notify(result["message"], color="negative")
            add_log(f"[ERROR] {result['message']}")
    finally:
        widgets.stop_button.props(remove="loading")
        widgets.stop_button.enable()


def handle_manual_drain_start(controller: Any, add_log: Callable[[str], None]) -> None:
    result = controller.start_manual_drain_jog()

    if result["success"]:
        add_log(f"[MANUAL DRAIN] {result['message']}")
    else:
        ui.notify(result["message"], color="negative")
        add_log(f"[BLOCKED] {result['message']}")


def handle_manual_drain_stop(controller: Any, add_log: Callable[[str], None]) -> None:
    result = controller.stop_manual_drain_jog()
    add_log(f"[MANUAL DRAIN] {result['message']}")


def handle_tank_cleaning_start(
    controller: Any, widgets: ProcessControlWidgets, add_log: Callable[[str], None]
) -> None:
    if not _consume_safety_confirmation(widgets):
        message = "Tank cleaning blocked: safety confirmation is not set."
        ui.notify(message, color="negative")
        add_log(f"[BLOCKED] {message}")
        return
    control_data = controller.get_process_control_status()
    entered_text = str(
        control_data.get("tank_cleaning_required_confirmation_text")
        or control_data["required_confirmation_text"]
    ).strip()
    result = controller.start_tank_cleaning(entered_text)

    if result["success"]:
        add_log(f"[TANK CLEANING] {result['message']}")
    else:
        ui.notify(result["message"], color="negative")
        add_log(f"[BLOCKED] {result['message']}")


def handle_tank_cleaning_stop(controller: Any, add_log: Callable[[str], None]) -> None:
    result = controller.stop_tank_cleaning()
    add_log(f"[TANK CLEANING] {result['message']}")


def wire_process_control_handlers(
    controller: Any, widgets: ProcessControlWidgets, add_log: Callable[[str], None]
) -> None:
    widgets.start_button.on_click(lambda: handle_start(controller, widgets, add_log))
    widgets.reset_button.on_click(lambda: handle_reset(controller, add_log))
    widgets.stop_button.on_click(lambda: handle_stop(controller, widgets, add_log))
    widgets.tank_cleaning_start_button.on_click(
        lambda: handle_tank_cleaning_start(controller, widgets, add_log)
    )
    widgets.tank_cleaning_stop_button.on_click(lambda: handle_tank_cleaning_stop(controller, add_log))

    widgets.manual_drain_button.on("mousedown", lambda _: handle_manual_drain_start(controller, add_log))
    widgets.manual_drain_button.on("mouseup", lambda _: handle_manual_drain_stop(controller, add_log))
    widgets.manual_drain_button.on("mouseleave", lambda _: handle_manual_drain_stop(controller, add_log))
    widgets.manual_drain_button.on("touchstart", lambda _: handle_manual_drain_start(controller, add_log))
    widgets.manual_drain_button.on("touchend", lambda _: handle_manual_drain_stop(controller, add_log))
    widgets.manual_drain_button.on("touchcancel", lambda _: handle_manual_drain_stop(controller, add_log))


def refresh_process_control(
    widgets: ProcessControlWidgets,
    control_data: dict[str, Any],
    manual_drain_status: dict[str, Any],
    tank_cleaning_status: dict[str, Any],
    manual_drain_active: bool,
    tank_cleaning_active: bool,
    prime_active: bool,
) -> None:
    hardware_enabled = control_data["hardware_execution_enabled"]

    widgets.hardware_enabled_label.set_text(
        f"hardware_execution_enabled: {hardware_enabled}"
    )

    widgets.required_text_label.set_text(
        f"required_confirmation_text: {control_data['required_confirmation_text']}"
    )

    widgets.fill_settings_label.set_text(
        f"mode={control_data['fill_mode']} | "
        f"target_add={control_data['target_add_liters']} L | "
        f"target_total={control_data['target_total_liters']} L | "
        f"max_fill={control_data['max_fill_seconds']} s | "
        f"mixing_circulation={control_data['enable_mixing_circulation']} | "
        f"sensor_circulation={control_data['enable_sensor_circulation']}"
    )

    manual_drain_elapsed = float(manual_drain_status.get("elapsed_seconds", 0.0) or 0.0)
    manual_drain_max = float(manual_drain_status.get("max_seconds", 30.0) or 30.0)
    manual_drain_progress_value = float(manual_drain_status.get("progress", 0.0) or 0.0)

    widgets.manual_drain_progress.set_value(manual_drain_progress_value)

    if manual_drain_active:
        widgets.manual_drain_button.set_text(
            f"Draining... {manual_drain_elapsed:.1f}/{manual_drain_max:.0f} s"
        )
        widgets.manual_drain_button.classes("manual-drain-active")
        widgets.manual_drain_status_label.set_text(
            f"Manual Drain Jog: running | {manual_drain_elapsed:.1f}/{manual_drain_max:.0f} s"
        )
    else:
        widgets.manual_drain_button.set_text("Hold to Drain – max. 30 s")
        widgets.manual_drain_button.classes(remove="manual-drain-active")
        widgets.manual_drain_status_label.set_text(
            f"Manual Drain Jog: {manual_drain_status.get('last_message', 'ready')}"
        )

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

        widgets.tank_cleaning_progress.set_value(progress_value)
        widgets.tank_cleaning_status_label.set_text(
            f"Tank Cleaning: {tank_cleaning_phase} | "
            f"{fmt(tank_cleaning_current, 'L')} | {tank_cleaning_elapsed:.0f}s"
        )
    else:
        widgets.tank_cleaning_progress.set_value(
            1.0 if tank_cleaning_phase == "FINISHED" else 0.0
        )
        widgets.tank_cleaning_status_label.set_text(
            f"Tank Cleaning: {tank_cleaning_status.get('last_message', 'ready')}"
        )

    if (
        control_data["is_running"]
        or manual_drain_active
        or tank_cleaning_active
        or prime_active
        or not hardware_enabled
    ):
        widgets.start_button.disable()
    else:
        widgets.start_button.enable()

    if control_data["is_running"] or manual_drain_active or tank_cleaning_active or prime_active:
        widgets.reset_button.disable()
    else:
        widgets.reset_button.enable()

    if control_data["is_running"] or tank_cleaning_active or prime_active or not hardware_enabled:
        widgets.manual_drain_button.disable()
    else:
        widgets.manual_drain_button.enable()

    if (
        control_data["is_running"]
        or manual_drain_active
        or tank_cleaning_active
        or prime_active
        or not tank_cleaning_hardware_enabled
    ):
        widgets.tank_cleaning_start_button.disable()
    else:
        widgets.tank_cleaning_start_button.enable()

    if tank_cleaning_active:
        widgets.tank_cleaning_stop_button.enable()
    else:
        widgets.tank_cleaning_stop_button.disable()
