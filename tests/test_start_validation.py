"""AP-02B: sichere Startfreigabe fuer Rezeptlaeufe (hardwarefrei).

Prueft die reine Funktion statemachine/start_validation.py, die Betreiber-
bestaetigung in RunOptions/RunConfigSnapshot und die Ablehnung in
FillAndMeasureController.start() VOR is_running, Thread und GPIO-Init.
"""

from __future__ import annotations

import ast
import copy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_process_controller_run_snapshot import FULL_PROCESS_SETTINGS

import nicegui_dashboard.recipe_store as recipe_store
from domain.recipe.limits import (
    MAX_PROCESS_VOLUME_L,
    MIXER_EMPTY_VETO_L,
    RO_TANK_CAPACITY_L,
)
from domain.recipe_model import RunConfigSnapshot, RunOptions, StoredRecipe
from nicegui_dashboard.cds_controller import CdsController
from nicegui_dashboard.pages.dashboard import process_control_section as pcs
from nicegui_dashboard.recipe_store import build_run_config
from process.fill_and_measure import FillAndMeasureController
from statemachine.start_validation import required_ro_liters, validate_fill_start

REPO = Path(__file__).resolve().parent.parent


def _settings(target=30.0, confirmed=True, **overrides):
    recipe = dict(recipe_store.DEFAULT_RECIPE, target_ro_water_l=target)
    base = dict(FULL_PROCESS_SETTINGS)
    config = build_run_config(base, recipe, RunOptions(tank_emptied_confirmed=confirmed))
    config.update(overrides)
    return config


def _sensors(mixer=0.0, ro=500.0, capacity=1300.0):
    return {
        "mixer": {"volume_liters_calc": mixer},
        "ro": {"volume_liters_calc": ro, "configured_max_liters": capacity},
    }


def _blocked(settings, sensors, fragment):
    ok, message = validate_fill_start(settings, sensors)
    assert ok is False, message
    assert fragment in message, message


# --- Betreiberbestaetigung ---------------------------------------------------


def test_run_options_default_is_false_and_strict():
    assert RunOptions().tank_emptied_confirmed is False
    assert RunOptions(tank_emptied_confirmed=True).validate() == []
    for bad in ("true", 1, 0, None):
        assert RunOptions(tank_emptied_confirmed=bad).validate(), bad  # type: ignore[arg-type]


def test_confirmation_is_part_of_the_immutable_snapshot_but_not_the_recipe():
    config = _settings(confirmed=True)
    assert config["recipe_run_config_snapshot"]["tank_emptied_confirmed"] is True
    assert "tank_emptied_confirmed" not in StoredRecipe.from_dict(dict(recipe_store.DEFAULT_RECIPE)).to_dict()
    default = build_run_config(dict(FULL_PROCESS_SETTINGS), dict(recipe_store.DEFAULT_RECIPE))
    assert default["recipe_run_config_snapshot"]["tank_emptied_confirmed"] is False
    snapshot = RunConfigSnapshot.build(
        StoredRecipe.from_dict(dict(recipe_store.DEFAULT_RECIPE)), dict(FULL_PROCESS_SETTINGS), RunOptions(tank_emptied_confirmed=True)
    )
    with pytest.raises(Exception):
        snapshot.tank_emptied_confirmed = False  # type: ignore[misc]


def test_valid_start_is_accepted():
    assert validate_fill_start(_settings(), _sensors()) == (True, None)


@pytest.mark.parametrize("value", [False, None, "true", 1, "", 0])
def test_missing_or_non_bool_confirmation_blocks(value):
    settings = _settings()
    settings["recipe_run_config_snapshot"]["tank_emptied_confirmed"] = value
    _blocked(settings, _sensors(), "nicht bestaetigt")


def test_missing_field_and_missing_snapshot_block():
    settings = _settings()
    del settings["recipe_run_config_snapshot"]["tank_emptied_confirmed"]
    _blocked(settings, _sensors(), "nicht bestaetigt")
    settings = _settings()
    del settings["recipe_run_config_snapshot"]
    _blocked(settings, _sensors(), "recipe_run_config_snapshot")
    _blocked(None, _sensors(), "Prozesseinstellungen")


# --- Sensorplausibilitaet ----------------------------------------------------


@pytest.mark.parametrize("mixer", [0.0, 0.1, MIXER_EMPTY_VETO_L])
def test_mixer_within_empty_veto_is_accepted_with_confirmation(mixer):
    assert validate_fill_start(_settings(), _sensors(mixer=mixer))[0] is True


def test_mixer_above_veto_blocks():
    _blocked(_settings(), _sensors(mixer=0.3000001), "nicht entleert")
    _blocked(_settings(), _sensors(mixer=10.0), "nicht belastbar")


@pytest.mark.parametrize("mixer", [-0.1, float("nan"), float("inf"), "5", None, True])
def test_invalid_mixer_values_block(mixer):
    ok, _ = validate_fill_start(_settings(), _sensors(mixer=mixer))
    assert ok is False


def test_negative_and_above_process_limit_mixer_have_specific_messages():
    _blocked(_settings(), _sensors(mixer=-1.0), "negativ")
    _blocked(_settings(), _sensors(mixer=MAX_PROCESS_VOLUME_L + 0.01), "Prozessgrenze")


def test_mixer_at_or_above_target_blocks():
    # Der Veto liegt bei 0.3 L; ein Ziel darunter zeigt die eigene Pruefung 6.
    settings = _settings()
    settings["target_total_liters"] = 0.2
    settings["recipe_run_config_snapshot"]["target_ro_water_l"] = 0.2
    settings["recipe_run_config_snapshot"]["estimated_process_volume_l"] = 0.2
    _blocked(settings, _sensors(mixer=0.2), "Rezeptziel")
    _blocked(settings, _sensors(mixer=0.25), "Rezeptziel")


@pytest.mark.parametrize("snapshot", [None, {}, [], "x"])
def test_missing_sensor_snapshot_blocks(snapshot):
    ok, _ = validate_fill_start(_settings(), snapshot)
    assert ok is False


@pytest.mark.parametrize("ro", [-1.0, float("nan"), float("inf"), "500", None, False])
def test_invalid_ro_values_block(ro):
    ok, _ = validate_fill_start(_settings(), _sensors(ro=ro))
    assert ok is False


def test_ro_capacity_checks():
    _blocked(_settings(), _sensors(ro=1300.1), "uebersteigt")
    assert validate_fill_start(_settings(), _sensors(ro=1300.0))[0] is True
    for capacity in (None, 0, -5, "1300", float("nan"), True):
        _blocked(_settings(), _sensors(capacity=capacity), "Kapazitaet")
    _blocked(_settings(), _sensors(capacity=1000.0), "widerspricht")
    sensors = _sensors()
    del sensors["ro"]["configured_max_liters"]
    _blocked(_settings(), sensors, "Kapazitaet")


def test_ro_capacity_constant_matches_sensor_bridge():
    tree = ast.parse((REPO / "mqtt_sensor_bridge.py").read_text(encoding="utf-8"))
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "RO_VOLUME_LITERS" for t in node.targets)
    ]
    assert values == [RO_TANK_CAPACITY_L]


# --- RO-Bedarf ---------------------------------------------------------------


def test_required_ro_formula():
    assert required_ro_liters(30.0, 0.0) == 30.0
    assert required_ro_liters(30.0, 0.3) == pytest.approx(29.7)
    assert required_ro_liters(30.0, 40.0) == 0.0


def test_ro_supply_exact_and_just_short():
    settings = _settings(target=30.0)  # Reserve 20 L
    assert validate_fill_start(settings, _sensors(mixer=0.0, ro=50.0))[0] is True
    _blocked(settings, _sensors(mixer=0.0, ro=49.99), "Unzureichender RO-Vorrat")
    # Restwert im Veto verringert den Bedarf
    assert validate_fill_start(settings, _sensors(mixer=0.3, ro=49.7))[0] is True
    _blocked(settings, _sensors(mixer=0.3, ro=49.69), "Unzureichender RO-Vorrat")


@pytest.mark.parametrize("target", [10.0, 100.0, 180.0])
def test_ro_supply_scales_with_recipe_amount(target):
    settings = _settings(target=target)
    assert validate_fill_start(settings, _sensors(ro=target + 20.0))[0] is True
    _blocked(settings, _sensors(ro=target + 19.9), "Unzureichender RO-Vorrat")


def test_reserve_comes_from_settings():
    settings = _settings(target=30.0, min_ro_liters_required=0.0)
    assert validate_fill_start(settings, _sensors(ro=30.0))[0] is True
    settings["min_ro_liters_required"] = -1.0
    _blocked(settings, _sensors(ro=500.0), "min_ro_liters_required")
    del settings["min_ro_liters_required"]
    _blocked(settings, _sensors(ro=500.0), "min_ro_liters_required")


# --- Konsistenz der Zielwerte ------------------------------------------------


def test_contradictory_targets_block():
    settings = _settings()
    settings["target_total_liters"] = 31.0
    _blocked(settings, _sensors(), "Widerspruechliche Zielmengen")

    settings = _settings()
    settings["recipe_run_config_snapshot"]["target_ro_water_l"] = 181.0
    settings["target_total_liters"] = 181.0
    _blocked(settings, _sensors(), "Maximum")

    settings = _settings()
    settings["recipe_run_config_snapshot"]["estimated_process_volume_l"] = 185.5
    _blocked(settings, _sensors(), "Prozessvolumen")

    settings = _settings()
    settings["recipe_run_config_snapshot"]["estimated_process_volume_l"] = 10.0
    _blocked(settings, _sensors(), "Prozessvolumen")

    settings = _settings()
    settings["recipe_run_config_snapshot"]["max_mixer_liters"] = 20.0
    _blocked(settings, _sensors(), "Mixergrenze")

    settings = _settings()
    settings["recipe_run_config_snapshot"]["estimated_process_volume_l"] = float("nan")
    _blocked(settings, _sensors(), "Zielmengen")

    settings = _settings()
    settings["fill_mode"] = "delta"
    _blocked(settings, _sensors(), "absolute")


def test_validation_exception_blocks_instead_of_raising():
    class Boom(dict):
        def get(self, *_args, **_kw):
            raise RuntimeError("kaputt")

    ok, message = validate_fill_start(Boom(), _sensors())
    assert ok is False and "Startpruefung fehlgeschlagen" in message


# --- Controller: Ablehnung vor Thread/GPIO -----------------------------------


def _fresh_fill_controller(sensors, tripwire):
    fill = FillAndMeasureController(lambda: sensors)

    def forbidden_run(settings):
        tripwire.append("run")

    fill._run = forbidden_run  # type: ignore[method-assign]
    return fill


def _assert_nothing_started(fill, tripwire):
    assert tripwire == []
    assert fill.is_running is False
    assert fill.is_active() is False
    assert fill._thread is None
    assert fill._actuators is None
    assert fill.last_start_request is None
    assert fill.display_state == "IDLE"
    assert fill.state_machine is None


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
def test_direct_start_without_valid_confirmation_is_blocked_before_thread(confirmed):
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    settings = _settings()
    settings["recipe_run_config_snapshot"]["tank_emptied_confirmed"] = confirmed
    result = fill.start(settings)
    assert result["success"] is False
    assert "nicht bestaetigt" in result["message"]
    _assert_nothing_started(fill, tripwire)


def test_direct_start_without_snapshot_is_blocked():
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    settings = _settings()
    del settings["recipe_run_config_snapshot"]
    assert fill.start(settings)["success"] is False
    _assert_nothing_started(fill, tripwire)


def test_sensor_read_exception_blocks_before_thread():
    tripwire: list = []
    calls = {"n": 0}

    def reader():
        calls["n"] += 1
        if calls["n"] > 1:  # base precondition passes, our read fails
            raise OSError("sensor weg")
        return _sensors()

    fill = FillAndMeasureController(reader)
    fill._run = lambda settings: tripwire.append("run")  # type: ignore[method-assign]
    result = fill.start(_settings())
    assert result["success"] is False
    assert "OSError" in result["message"]
    _assert_nothing_started(fill, tripwire)


@pytest.mark.parametrize("mixer", [5.0, -1.0, float("nan")])
def test_implausible_sensors_block_before_thread(mixer):
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(mixer=mixer), tripwire)
    assert fill.start(_settings())["success"] is False
    _assert_nothing_started(fill, tripwire)


def test_existing_gate_hardware_disabled_stays_effective():
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    result = fill.start(_settings(hardware_execution_enabled=False))
    assert result["success"] is False
    assert "hardware" in result["message"].lower()
    _assert_nothing_started(fill, tripwire)


def test_valid_direct_start_still_runs_with_fake_run():
    ran = threading.Event()
    fill = FillAndMeasureController(lambda: _sensors())
    fill._run = lambda settings: ran.set()  # type: ignore[method-assign]
    assert fill.start(_settings())["success"] is True
    assert ran.wait(2.0)
    fill.stop()


# --- ProcessController / Dashboard -------------------------------------------


@pytest.fixture
def cds(controller, monkeypatch, tmp_path, empty_tank):
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS)
    instance = object.__new__(CdsController)
    instance.controller_errors = []
    instance.process_controller = controller
    return instance


def _dash_widgets(tank_emptied):
    return SimpleNamespace(
        confirmation_input=SimpleNamespace(value="confirmed"),
        run_sensor_circulation_switch=SimpleNamespace(value=False),
        tank_emptied_confirm_switch=SimpleNamespace(value=tank_emptied),
    )


def test_dashboard_start_without_tank_confirmation_is_blocked_and_leaves_no_run_status(
    cds, controller, monkeypatch
):
    notes = []
    monkeypatch.setattr(pcs.ui, "notify", lambda message, **kw: notes.append((message, kw.get("color"))))
    tripwire: list = []
    controller.fill_and_measure._run = lambda settings: tripwire.append("run")  # type: ignore[method-assign]
    widgets = _dash_widgets(False)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert notes[-1][1] == "negative"
    assert "Mischtanks" in notes[-1][0]
    _assert_nothing_started(controller.fill_and_measure, tripwire)
    status = controller.fill_and_measure.get_status()
    assert status["is_running"] is False and status["last_start_request"] is None


def test_dashboard_confirmation_is_reset_after_blocked_and_accepted_start(cds, controller, monkeypatch):
    monkeypatch.setattr(pcs.ui, "notify", lambda *a, **k: None)
    from test_process_controller_run_snapshot import _install_blocking_fake_run, _release_and_join

    # blockiert (falscher Text): Schalter wird trotzdem zurueckgesetzt
    widgets = _dash_widgets(True)
    widgets.confirmation_input.value = "wrong"
    pcs.handle_start(cds, widgets, lambda m: None)
    assert widgets.tank_emptied_confirm_switch.value is False

    # blockiert durch Controller (Mixer nicht leer)
    controller_sensors = controller.get_sensor_snapshot()
    controller_sensors["mixer"]["volume_liters_calc"] = 12.0
    widgets = _dash_widgets(True)
    pcs.handle_start(cds, widgets, lambda m: None)
    assert widgets.tank_emptied_confirm_switch.value is False
    assert controller.fill_and_measure.is_active() is False
    controller_sensors["mixer"]["volume_liters_calc"] = 0.0

    # akzeptiert
    started, release = _install_blocking_fake_run(controller)
    widgets = _dash_widgets(True)
    try:
        pcs.handle_start(cds, widgets, lambda m: None)
        assert started.wait(2.0)
        assert widgets.tank_emptied_confirm_switch.value is False
        snap = controller.get_status()["recipe_run_config_snapshot"]
        assert snap["tank_emptied_confirmed"] is True
    finally:
        _release_and_join(controller, release)

    # nichts davon wird als naechste Bestaetigung uebernommen
    result = cds.start_fill_and_measure("confirmed")
    assert result["success"] is False and "nicht bestaetigt" in result["message"]


def test_confirmation_is_not_persisted_in_recipe_book(cds, controller, tmp_path, monkeypatch):
    from test_process_controller_run_snapshot import _install_blocking_fake_run, _release_and_join

    started, release = _install_blocking_fake_run(controller)
    try:
        assert cds.start_fill_and_measure("confirmed", run_options=RunOptions(tank_emptied_confirmed=True))["success"]
        assert started.wait(2.0)
    finally:
        _release_and_join(controller, release)
    book = tmp_path / "dashboard_recipes.json"
    if book.exists():
        assert "tank_emptied" not in book.read_text(encoding="utf-8")
    for path in (REPO / "config").glob("*.json"):
        assert "tank_emptied" not in path.read_text(encoding="utf-8")


def test_dashboard_resets_confirmation_even_if_controller_raises(monkeypatch):
    monkeypatch.setattr(pcs.ui, "notify", lambda *a, **k: None)

    class Exploding:
        def get_process_control_status(self):
            raise RuntimeError("boom")

    widgets = _dash_widgets(True)
    with pytest.raises(RuntimeError):
        pcs.handle_start(Exploding(), widgets, lambda m: None)
    assert widgets.tank_emptied_confirm_switch.value is False


def test_validation_does_not_mutate_inputs():
    settings, sensors = _settings(), _sensors()
    before = (copy.deepcopy(settings), copy.deepcopy(sensors))
    validate_fill_start(settings, sensors)
    assert (settings, sensors) == before


# --- AP-02B Review-Korrekturen R1-R4 -----------------------------------------


@pytest.mark.parametrize("flag", ["false", "true", "0", "", 1, 0, None, 1.0, [], "yes"])
def test_r1_direct_start_requires_strict_true_hardware_flag(flag):
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    result = fill.start(_settings(hardware_execution_enabled=flag))
    assert result["success"] is False
    assert "hardware_execution_enabled" in result["message"]
    _assert_nothing_started(fill, tripwire)


def test_r1_missing_hardware_flag_blocks():
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    settings = _settings()
    del settings["hardware_execution_enabled"]
    assert fill.start(settings)["success"] is False
    _assert_nothing_started(fill, tripwire)


@pytest.mark.parametrize("settings", [None, "settings", [], 5, ()])
def test_r1_invalid_settings_structures_block(settings):
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    result = fill.start(settings)  # type: ignore[arg-type]
    assert result["success"] is False
    assert "Prozesseinstellungen" in result["message"]
    _assert_nothing_started(fill, tripwire)


def test_r1_strict_true_still_starts():
    ran = threading.Event()
    fill = FillAndMeasureController(lambda: _sensors())
    fill._run = lambda settings: ran.set()  # type: ignore[method-assign]
    assert fill.start(_settings(hardware_execution_enabled=True))["success"] is True
    assert ran.wait(2.0)
    fill.stop()


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_r2_sensor_reader_exception_on_any_read_is_a_controlled_rejection(fail_on_call):
    tripwire: list = []
    calls = {"n": 0}

    def reader():
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise OSError("sensor weg")
        return _sensors()

    fill = FillAndMeasureController(reader)
    fill._run = lambda settings: tripwire.append("run")  # type: ignore[method-assign]
    result = fill.start(_settings())
    assert result["success"] is False
    assert "OSError" in result["message"]
    _assert_nothing_started(fill, tripwire)
    assert fill.get_status()["error"] == result["message"]


def test_r2_reader_returning_none_still_blocked_by_base_gate():
    tripwire: list = []
    fill = FillAndMeasureController(lambda: None)
    fill._run = lambda settings: tripwire.append("run")  # type: ignore[method-assign]
    assert fill.start(_settings())["success"] is False
    _assert_nothing_started(fill, tripwire)


def test_r3_settings_and_snapshot_max_mixer_must_agree():
    settings = _settings(max_mixer_liters=184.0)  # Snapshot: 185.0
    _blocked(settings, _sensors(), "Mixergrenze")
    over = _settings(max_mixer_liters=200.0)  # 185 L im Snapshot, 200 L in den Settings
    assert validate_fill_start(over, _sensors())[0] is False
    tripwire: list = []
    fill = _fresh_fill_controller(_sensors(), tripwire)
    assert fill.start(settings)["success"] is False
    _assert_nothing_started(fill, tripwire)


@pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf"), "185", True, 185.5, 200.0])
def test_r3_invalid_operative_max_mixer_blocks(bad):
    settings = _settings()
    settings["max_mixer_liters"] = bad
    ok, _ = validate_fill_start(settings, _sensors())
    assert ok is False


def test_r3_missing_operative_max_mixer_blocks():
    settings = _settings()
    del settings["max_mixer_liters"]
    _blocked(settings, _sensors(), "max_mixer_liters")


def test_r3_tolerance_and_equal_values_accepted():
    settings = _settings()
    settings["max_mixer_liters"] = 185.0 + 1e-9
    assert validate_fill_start(settings, _sensors())[0] is True
    settings["max_mixer_liters"] = 184.99
    _blocked(settings, _sensors(), "Mixergrenze")


def test_r4_empty_veto_matches_productive_drain_thresholds():
    import json

    for name in ("tank_cleaning_settings.json", "water_cycle_settings.json"):
        config = json.loads((REPO / "config" / name).read_text(encoding="utf-8"))
        assert config["empty_threshold_liters"] == MIXER_EMPTY_VETO_L, (
            f"{name}: empty_threshold_liters weicht von MIXER_EMPTY_VETO_L ab - "
            "bewusste fachliche Entscheidung erforderlich. Der Wert ist nur ein "
            "Plausibilitaets-Veto, kein Leernachweis."
        )
