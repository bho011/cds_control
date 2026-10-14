"""
AP-01: RunOptions must travel unchanged from the dashboard's handle_start()
through the REAL CdsController.start_fill_and_measure() signature to the
ProcessController. A MagicMock stand-in for CdsController hid the original
TypeError (the dashboard passed run_options=, CdsController did not accept
it), so these tests use the real class.

Hardware-free: CdsController is built without __init__ (which would start
MQTT readers); the ProcessController is the normal test fixture whose
FillAndMeasureController._run is replaced by a fake, so no GPIO, serial
port, MQTT or real process run is ever involved. The recipe book is
redirected to tmp_path.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from test_process_controller_run_snapshot import (
    FULL_PROCESS_SETTINGS,
    _install_blocking_fake_run,
    _release_and_join,
)

import nicegui_dashboard.recipe_store as recipe_store
from domain.recipe_model import RunOptions
from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.pages.dashboard import process_control_section as pcs


@pytest.fixture(autouse=True)
def _plausible_start_sensors(empty_tank):
    """AP-02B: a recipe start needs a plausible empty-tank reading (simulated)."""
    return empty_tank


CONFIRMED = RunOptions(tank_emptied_confirmed=True)


@pytest.fixture
def cds(controller, monkeypatch, tmp_path):
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS)
    instance = object.__new__(CdsController)  # skip __init__: no MQTT readers
    instance.controller_errors = []
    instance.process_controller = controller
    return instance


@pytest.fixture
def spy(controller):
    """Records the run_options that actually arrive at ProcessController."""
    received = []
    real = controller.start_fill_and_measure

    def recording(confirmation_text, run_options=None):
        received.append(run_options)
        return real(confirmation_text, run_options)

    controller.start_fill_and_measure = recording
    return received


@pytest.fixture
def notifications(monkeypatch):
    notes = []
    monkeypatch.setattr(pcs.ui, "notify", lambda message, **kw: notes.append((message, kw.get("color"))))
    return notes


def _widgets(confirmed=True, circulation=False, tank_emptied=True):
    return SimpleNamespace(
        safety_confirm_switch=SimpleNamespace(value=confirmed),
        run_sensor_circulation_switch=SimpleNamespace(value=circulation),
        tank_emptied_confirm_switch=SimpleNamespace(value=tank_emptied),
    )


def test_cds_controller_signature_accepts_run_options():
    params = inspect.signature(CdsController.start_fill_and_measure).parameters
    assert "run_options" in params
    assert params["run_options"].default is None


def test_a_enabled_run_option_reaches_process_controller_unchanged(cds, controller, spy):
    started, release = _install_blocking_fake_run(controller)
    options = RunOptions(sensor_circulation_enabled=True, tank_emptied_confirmed=True)
    try:
        result = cds.start_fill_and_measure("confirmed", run_options=options)
        assert result["success"], result
        assert spy == [options]
        assert spy[0] is options
        assert started.wait(2.0)
        assert controller.get_status()["enable_sensor_circulation"] is True
    finally:
        _release_and_join(controller, release)


def test_b_default_call_without_tank_confirmation_is_blocked(cds, controller, spy):
    result = cds.start_fill_and_measure("confirmed")
    assert result["success"] is False
    assert "nicht bestaetigt" in result["message"]
    assert spy == [None]
    assert controller.fill_and_measure.is_active() is False
    assert controller.fill_and_measure.is_running is False


def test_b_confirmed_call_without_other_options_enables_nothing(cds, controller, spy):
    started, release = _install_blocking_fake_run(controller)
    try:
        result = cds.start_fill_and_measure("confirmed", run_options=CONFIRMED)
        assert result["success"], result
        assert spy == [CONFIRMED]
        assert started.wait(2.0)
        status = controller.get_status()
        assert status["enable_sensor_circulation"] is False
        assert status["recipe_run_config_snapshot"]["sensor_circulation_enabled"] is False
    finally:
        _release_and_join(controller, release)


@pytest.mark.parametrize("circulation", [True, False])
def test_c_handle_start_reaches_real_cds_controller_method(cds, controller, spy, notifications, circulation):
    started, release = _install_blocking_fake_run(controller)
    widgets = _widgets(circulation=circulation)
    log = []
    try:
        pcs.handle_start(cds, widgets, log.append)  # old code raised TypeError here
        assert len(spy) == 1
        assert spy[0] == RunOptions(sensor_circulation_enabled=circulation, tank_emptied_confirmed=True)
        assert notifications[-1][1] == "positive"
        assert started.wait(2.0)
        assert controller.get_status()["enable_sensor_circulation"] is circulation
        assert widgets.run_sensor_circulation_switch.value is False  # reset after accepted start
        assert widgets.tank_emptied_confirm_switch.value is False
    finally:
        _release_and_join(controller, release)


def test_d_hardware_disabled_blocks_start(cds, controller, spy, notifications):
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS, hardware_execution_enabled=False)
    widgets = _widgets(circulation=True)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert spy == []
    assert notifications[-1][1] == "negative"
    assert controller.fill_and_measure.is_active() is False
    assert widgets.tank_emptied_confirm_switch.value is False  # reset even on early block


def test_d_hardware_disabled_blocks_direct_controller_call(cds, controller):
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS, hardware_execution_enabled=False)
    result = cds.start_fill_and_measure(
        "confirmed", run_options=RunOptions(sensor_circulation_enabled=True, tank_emptied_confirmed=True)
    )
    assert result["success"] is False
    assert "hardware_execution_enabled" in result["message"]


def test_d_wrong_confirmation_text_blocks_start(cds, controller, spy, notifications):
    widgets = _widgets(confirmed=False, circulation=True)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert spy == []
    assert notifications[-1][1] == "negative"
    assert widgets.run_sensor_circulation_switch.value is True  # blocked start keeps the choice
    assert widgets.tank_emptied_confirm_switch.value is False  # but never the tank confirmation


def test_d_wrong_confirmation_text_blocks_direct_controller_call(cds):
    result = cds.start_fill_and_measure("wrong", run_options=RunOptions(sensor_circulation_enabled=True))
    assert result["success"] is False
    assert "Bestätigungstext" in result["message"]


def test_d_missing_sensor_snapshot_blocks_start(cds, controller, spy, notifications):
    controller.get_sensor_snapshot = lambda: None
    widgets = _widgets(circulation=True)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert len(spy) == 1
    assert notifications[-1][1] == "negative"
    assert "SensorSnapshot" in notifications[-1][0]
    assert controller.fill_and_measure.is_active() is False
    assert widgets.run_sensor_circulation_switch.value is True
    assert widgets.tank_emptied_confirm_switch.value is False


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, None])
def test_e_invalid_run_option_is_rejected_not_coerced(cds, controller, spy, notifications, bad):
    pcs.handle_start(cds, _widgets(circulation=bad), lambda m: None)
    assert len(spy) == 1
    assert spy[0].sensor_circulation_enabled is bad  # passed through, not bool()-ed
    assert notifications[-1][1] == "negative"
    assert controller.fill_and_measure.is_active() is False


def test_e_invalid_run_option_rejected_on_direct_call(cds, controller):
    result = cds.start_fill_and_measure("confirmed", run_options=RunOptions(sensor_circulation_enabled="true"))  # type: ignore[arg-type]
    assert result["success"] is False
    assert controller.fill_and_measure.is_active() is False


def test_safety_confirmation_is_consumed_by_every_start_attempt(cds, controller, spy, notifications):
    widgets = _widgets(confirmed=False)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert widgets.safety_confirm_switch.value is False
    assert spy == []

    widgets = _widgets(confirmed=True, tank_emptied=False)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert widgets.safety_confirm_switch.value is False


def test_safety_confirmation_requires_real_true(cds, controller, spy, notifications):
    widgets = _widgets(confirmed="confirmed")
    pcs.handle_start(cds, widgets, lambda m: None)
    assert spy == []
    assert notifications[-1][1] == "negative"


def test_tank_cleaning_without_safety_confirmation_is_blocked(notifications):
    calls = []
    fake = SimpleNamespace(
        start_tank_cleaning=lambda text: calls.append(text) or {"success": True, "message": "x"},
        get_process_control_status=lambda: {"required_confirmation_text": "confirmed"},
    )
    widgets = _widgets(confirmed=False)
    pcs.handle_tank_cleaning_start(fake, widgets, lambda m: None)
    assert calls == []
    assert notifications[-1][1] == "negative"

    widgets = _widgets(confirmed=True)
    pcs.handle_tank_cleaning_start(fake, widgets, lambda m: None)
    assert calls == ["confirmed"]
    assert widgets.safety_confirm_switch.value is False
