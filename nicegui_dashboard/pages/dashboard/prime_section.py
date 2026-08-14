"""Prime-Dialog: Peristaltikpumpen manuell entlüften (Wartungsvorgang, kein Prozessschritt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from nicegui import ui

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


@dataclass
class PrimeDialogWidgets:
    prime_dialog: ui.dialog
    mcu_a_info_label: ui.label
    mcu_b_info_label: ui.label
    prime_pump_rows: dict[tuple[str, str], dict[str, Any]]
    prime_status_label: ui.label
    prime_progress: ui.linear_progress
    prime_error_label: ui.label
    prime_close_button: ui.button
    prime_stop_button: ui.button
    prime_start_button: ui.button


def build_prime_dialog() -> PrimeDialogWidgets:
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

    return PrimeDialogWidgets(
        prime_dialog=prime_dialog,
        mcu_a_info_label=mcu_a_info_label,
        mcu_b_info_label=mcu_b_info_label,
        prime_pump_rows=prime_pump_rows,
        prime_status_label=prime_status_label,
        prime_progress=prime_progress,
        prime_error_label=prime_error_label,
        prime_close_button=prime_close_button,
        prime_stop_button=prime_stop_button,
        prime_start_button=prime_start_button,
    )


def update_prime_pump_checklist(widgets: PrimeDialogWidgets) -> None:
    options = describe_pump_options()

    for (controller_id, pump), option in options.items():
        row_refs = widgets.prime_pump_rows[(controller_id, pump)]
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

    for controller_id, info_label in (
        ("MCU_A", widgets.mcu_a_info_label),
        ("MCU_B", widgets.mcu_b_info_label),
    ):
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


def open_prime_dialog(widgets: PrimeDialogWidgets) -> None:
    update_prime_pump_checklist(widgets)
    widgets.prime_dialog.open()


def _collect_selected_prime_pumps(widgets: PrimeDialogWidgets) -> dict[str, list[str]]:
    selected: dict[str, list[str]] = {}
    for controller_id in VALID_CONTROLLERS:
        pumps = [
            pump
            for pump in VALID_PUMPS
            if widgets.prime_pump_rows[(controller_id, pump)]["checkbox"].value
        ]
        if pumps:
            selected[controller_id] = pumps
    return selected


def handle_prime_start(controller: Any, widgets: PrimeDialogWidgets, add_log: Callable[[str], None]) -> None:
    pumps = _collect_selected_prime_pumps(widgets)
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


async def handle_prime_stop(
    controller: Any, widgets: PrimeDialogWidgets, add_log: Callable[[str], None]
) -> None:
    # Sofort, synchron, VOR dem await: der Nutzer soll augenblicklich
    # sehen, dass der Stopp angenommen wurde - nicht erst, wenn
    # stop_prime() (kann bis zu ~40s dauern) zurückkehrt. refresh()
    # übernimmt danach "Stopp wird ausgeführt", solange phase==STOPPING.
    widgets.prime_status_label.set_text(
        "Stopp angefordert – der aktuell laufende Dosierschritt wird beendet."
    )
    widgets.prime_stop_button.props("loading")
    widgets.prime_stop_button.disable()
    try:
        result = await controller.stop_prime()
        add_log(f"[PRIME] {result['message']}")
    finally:
        widgets.prime_stop_button.props(remove="loading")
    # Der Dialog bleibt beim normalen Stopp-Klick grundsätzlich offen
    # und zeigt anschließend das Ergebnis - kein prime_dialog.close() hier.


async def handle_prime_close(
    controller: Any, widgets: PrimeDialogWidgets, add_log: Callable[[str], None]
) -> None:
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
        widgets.prime_dialog.close()
        return

    widgets.prime_status_label.set_text(
        "Stopp angefordert – der aktuell laufende Dosierschritt wird beendet."
    )
    widgets.prime_close_button.props("loading")
    widgets.prime_close_button.disable()
    try:
        result = await controller.stop_prime()
    finally:
        widgets.prime_close_button.props(remove="loading")
        widgets.prime_close_button.enable()

    final_status = controller.get_process_control_status().get("prime", {})
    if final_status.get("is_active"):
        widgets.prime_error_label.set_text(
            "Der Prime-Vorgang konnte noch nicht als beendet bestätigt werden. "
            "Der Dialog bleibt aus Sicherheitsgründen geöffnet."
        )
        add_log(f"[PRIME] Schließen abgelehnt - Stopp noch nicht bestätigt: {result.get('message')}")
        return  # Dialog bleibt offen (bereits persistent)

    add_log(
        "[PRIME] Dialog wurde während des Primings geschlossen - "
        f"gestoppt: {result.get('message')}"
    )
    widgets.prime_dialog.close()


def wire_prime_handlers(
    controller: Any,
    open_prime_button: ui.button,
    widgets: PrimeDialogWidgets,
    add_log: Callable[[str], None],
) -> None:
    open_prime_button.on_click(lambda: open_prime_dialog(widgets))
    widgets.prime_start_button.on_click(lambda: handle_prime_start(controller, widgets, add_log))
    widgets.prime_stop_button.on_click(lambda: handle_prime_stop(controller, widgets, add_log))
    widgets.prime_close_button.on_click(lambda: handle_prime_close(controller, widgets, add_log))


def refresh_prime(
    widgets: PrimeDialogWidgets,
    prime_status: dict[str, Any],
    prime_active: bool,
    prime_selection_locked: bool,
) -> None:
    prime_phase = prime_status.get("phase", "IDLE")
    prime_results = prime_status.get("pump_results", []) or []
    prime_plan_total = sum(len(pumps) for pumps in (prime_status.get("pumps_plan", {}) or {}).values())

    if prime_active:
        progress_value = (len(prime_results) / prime_plan_total) if prime_plan_total else 0.0
        widgets.prime_progress.set_value(progress_value)
        if prime_phase == "STOPPING":
            # Keine Formulierung, die einen sofortigen Pumpenstopp
            # verspricht - der aktuell laufende Chunk kann intern noch
            # bis zu ~35s weiterlaufen, bevor der nächste Checkpoint
            # den Stopp tatsächlich wirksam werden lässt.
            widgets.prime_status_label.set_text(
                "Stopp wird ausgeführt – der aktuell laufende Dosierschritt wird beendet."
            )
        else:
            current_controller = prime_status.get("current_controller") or "-"
            current_pump = prime_status.get("current_pump") or "-"
            current_role = prime_status.get("current_pump_role") or "-"
            widgets.prime_status_label.set_text(
                f"Priming läuft: {current_controller}/{current_pump} ({current_role}) - "
                f"{len(prime_results)}/{prime_plan_total} Pumpen abgeschlossen"
            )
        widgets.prime_error_label.set_text("")
    else:
        # Erst hier (is_active bestätigt False) gilt der Vorgang als
        # "Gestoppt"/abgeschlossen - last_message spiegelt das bereits
        # korrekt wider ("Priming gestoppt: ..."/"Priming abgeschlossen.").
        widgets.prime_progress.set_value(1.0 if prime_phase == "FINISHED" else 0.0)
        widgets.prime_status_label.set_text(prime_status.get("last_message", "Bereit"))
        widgets.prime_error_label.set_text(prime_status.get("last_error") or "")

    if prime_selection_locked:
        widgets.prime_start_button.disable()
        for row_refs in widgets.prime_pump_rows.values():
            row_refs["checkbox"].disable()
    else:
        widgets.prime_start_button.enable()
        update_prime_pump_checklist(widgets)

    if prime_active:
        widgets.prime_stop_button.enable()
    else:
        widgets.prime_stop_button.disable()
