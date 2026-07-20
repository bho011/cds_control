"""
nicegui_dashboard/process_controller.py::get_status() must show the
RunConfigSnapshot a process was ACTUALLY started with for as long as that
process is running/tearing down, never a freshly recomputed live preview of
whatever recipe happens to be active right now. Worked example from the
task: a process starts with Recipe A (sensor_circulation_enabled=True);
while it's running, the user activates Recipe B as the new favorite; the
running process's status must keep showing Recipe A's snapshot with
sensor_circulation_enabled=True the whole time - Recipe B only applies to a
later run.

Uses the same fixture pattern as tests/conftest.py / test_process_locking.py:
ProcessController._run_fill_and_measure is replaced with a no-op stand-in
(installed on the instance, not the class) before any real run is started,
so the background thread never imports gpio_config/ActuatorManager or
touches real hardware. RECIPE_BOOK_PATH is monkeypatched to a tmp_path so
the real recipes/dashboard_recipes.json is never touched.
"""

from __future__ import annotations

import threading

import nicegui_dashboard.recipe_store as recipe_store
from domain.recipe_model import RunOptions
from nicegui_dashboard.recipe_store import save_recipe_to_slot, set_active_slot

FULL_PROCESS_SETTINGS = {
    "fill_mode": "delta",
    "target_add_liters": 1.0,
    "target_total_liters": 1.0,
    "max_mixer_liters": 185.0,
    "min_ro_liters_required": 20.0,
    "max_fill_seconds": 20.0,
    "min_fill_progress_liters": 0.2,
    "no_fill_progress_timeout_seconds": 8.0,
    "max_negative_level_drift_liters": 2.0,
    "level_filter_samples": 5,
    "target_reached_confirm_samples": 3,
    "level_settle_seconds": 5.0,
    "sensor_stabilize_seconds": 20.0,
    "enable_mixing_circulation": False,
    "enable_sensor_circulation": False,
    "hardware_execution_enabled": True,
    "required_confirmation_text": "confirmed",
}


def _install_blocking_fake_run(controller):
    """Replaces _run_fill_and_measure on this ProcessController INSTANCE
    only (not the class) with a stand-in that blocks until released, then
    tears itself down the same way the real method's finally block would -
    never touches GPIO/ActuatorManager/state machine."""
    started_event = threading.Event()
    release_event = threading.Event()

    def fake_run(settings) -> None:
        started_event.set()
        release_event.wait(5.0)
        with controller._lock:
            controller.is_running = False
            controller._teardown_in_progress = False
            controller.last_message = "Fake run finished."

    controller._run_fill_and_measure = fake_run
    return started_event, release_event


def _release_and_join(controller, release_event) -> None:
    release_event.set()
    thread = controller._thread
    if thread is not None:
        thread.join(timeout=5.0)


def test_running_process_keeps_showing_the_recipe_it_was_actually_started_with(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setattr(recipe_store, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS)

    save_recipe_to_slot(
        slot=1,
        recipe=dict(recipe_store.DEFAULT_RECIPE, recipe_name="Recipe A", target_ro_water_l=30.0),
        make_active=True,
    )
    save_recipe_to_slot(
        slot=2,
        recipe=dict(recipe_store.DEFAULT_RECIPE, recipe_name="Recipe B", target_ro_water_l=45.0),
        make_active=False,
    )

    started_event, release_event = _install_blocking_fake_run(controller)

    try:
        result = controller.start_fill_and_measure(
            "confirmed", run_options=RunOptions(sensor_circulation_enabled=True)
        )
        assert result["success"], result

        assert started_event.wait(2.0), "fake run never started"
        assert controller.is_running is True

        status_while_running = controller.get_status()
        snapshot = status_while_running["recipe_run_config_snapshot"]
        assert snapshot["recipe_name"] == "Recipe A"
        assert status_while_running["target_total_liters"] == 30.0
        assert status_while_running["enable_sensor_circulation"] is True
        assert snapshot["sensor_circulation_enabled"] is True

        # Activate Recipe B as the new favorite WHILE the process is still
        # running - this must not retroactively change what the running
        # process reports about itself.
        set_active_slot(2)

        status_still_running = controller.get_status()
        snapshot_still_running = status_still_running["recipe_run_config_snapshot"]
        assert snapshot_still_running["recipe_name"] == "Recipe A"
        assert status_still_running["target_total_liters"] == 30.0
        assert status_still_running["enable_sensor_circulation"] is True

        _release_and_join(controller, release_event)

        assert controller.is_running is False

        status_after_finish = controller.get_status()
        snapshot_after_finish = status_after_finish["recipe_run_config_snapshot"]
        assert snapshot_after_finish["recipe_name"] == "Recipe B"
        assert status_after_finish["target_total_liters"] == 45.0
    finally:
        _release_and_join(controller, release_event)


def test_get_status_shows_a_live_preview_when_no_run_has_ever_been_started(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setattr(recipe_store, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS)

    save_recipe_to_slot(
        slot=1,
        recipe=dict(recipe_store.DEFAULT_RECIPE, recipe_name="Live Preview Recipe", target_ro_water_l=22.0),
        make_active=True,
    )

    assert controller.is_running is False
    status = controller.get_status()
    assert status["recipe_run_config_snapshot"]["recipe_name"] == "Live Preview Recipe"
    assert status["target_total_liters"] == 22.0


def test_get_status_returns_to_live_preview_after_a_run_completes(controller, monkeypatch, tmp_path):
    monkeypatch.setattr(recipe_store, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS)

    save_recipe_to_slot(
        slot=1,
        recipe=dict(recipe_store.DEFAULT_RECIPE, recipe_name="Recipe A", target_ro_water_l=30.0),
        make_active=True,
    )

    started_event, release_event = _install_blocking_fake_run(controller)

    try:
        result = controller.start_fill_and_measure("confirmed")
        assert result["success"], result
        assert started_event.wait(2.0), "fake run never started"

        _release_and_join(controller, release_event)
        assert controller.is_running is False
        assert controller._teardown_in_progress is False

        status = controller.get_status()
        assert status["recipe_run_config_snapshot"]["recipe_name"] == "Recipe A"
    finally:
        _release_and_join(controller, release_event)
