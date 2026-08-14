"""Wartungs-Dialog: Live-Status-Badges, Preflight-Check, Event-Log. Für Entwicklung/Troubleshooting."""

from __future__ import annotations

from typing import Any, Callable

from nicegui import ui


def build_maintenance() -> dict[str, Any]:
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

    return {
        "dev_info_dialog": dev_info_dialog,
        "last_update_badge": last_update_badge,
        "prime_button": prime_button,
        "preflight_button": preflight_button,
        "log_box": log_box,
    }


def make_add_log(log_box: ui.label) -> Callable[[str], None]:
    """Builds the add_log(message) callback used across every section's
    handlers. Owns its own event_log list in closure, capped at the last 18
    lines - same cap the original inline implementation used."""
    event_log: list[str] = ["[OK] NiceGUI dashboard loaded."]

    def add_log(message: str) -> None:
        if not event_log or event_log[-1] != message:
            event_log.append(message)

        if len(event_log) > 18:
            del event_log[:-18]

        log_box.set_text("\n".join(event_log))

    return add_log


async def handle_run_preflight(
    controller: Any, preflight_button: ui.button, add_log: Callable[[str], None]
) -> None:
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
