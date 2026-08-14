"""
Charakterisierungs-Tests für statemachine/fill_and_measure_state_machine.py,
VOR dem kleinen Schnitt in Phase 6 (Modularisierungs-Plan).

Deckt reguläre Zustandsübergänge, is_done, die 3-stufige Mixer-Sensor-
Fallback-Kette, die 2-stufige RO-Fallback-Kette und die Fehler-/
Abbruchzustände ab. update() hat selbst keine internen Wartezeiten (die
Schleife mit time.sleep liegt beim Aufrufer, nicht in der State Machine),
alle Tests sind deshalb ohne echte Wartezeit deterministisch.
"""

from __future__ import annotations

from statemachine.fill_and_measure_state_machine import FillAndMeasureState, FillAndMeasureStateMachine


class FakeActuator:
    def __init__(self, name: str = "actuator"):
        self.name = name
        self.is_on = False
        self.on_calls = 0
        self.off_calls = 0

    def on(self) -> None:
        self.is_on = True
        self.on_calls += 1

    def off(self) -> None:
        self.is_on = False
        self.off_calls += 1


class ScriptedSensor:
    def __init__(self, mixer_liters: float = 0.0, ro_liters: float = 500.0):
        self.mixer_liters = mixer_liters
        self.ro_liters = ro_liters

    def __call__(self) -> dict:
        return {
            "mixer": {"volume_liters_calc": self.mixer_liters},
            "ro": {"volume_liters_calc": self.ro_liters},
        }


def _base_settings(**overrides) -> dict:
    settings = {
        "min_ro_liters_required": 20.0,
        "level_filter_samples": 5,
        "max_mixer_liters": 200.0,
        "max_fill_seconds": 900.0,
        "no_fill_progress_timeout_seconds": 30.0,
        "min_fill_progress_liters": 0.5,
        "max_negative_level_drift_liters": 3.0,
        "target_reached_confirm_samples": 1,
        "fill_mode": "absolute",
        "target_total_liters": 10.0,
        "level_settle_seconds": 0.0,
        "enable_mixing_circulation": False,
        "enable_sensor_circulation": False,
        "sensor_stabilize_seconds": 0.0,
    }
    settings.update(overrides)
    return settings


def _make_state_machine(sensor=None, settings=None, **actuators):
    return FillAndMeasureStateMachine(
        mixer_refill_pump=actuators.get("mixer_refill_pump", FakeActuator("mixer_refill_pump")),
        ro_inlet_valve=actuators.get("ro_inlet_valve"),
        mixing_circulation_pump=actuators.get("mixing_circulation_pump"),
        sensor_circulation_pump=actuators.get("sensor_circulation_pump"),
        get_sensor_snapshot=sensor or ScriptedSensor(),
        settings=settings or _base_settings(),
    )


# --- reguläre Zustandsübergänge ------------------------------------------------


def test_regular_transitions_reach_finished_in_expected_order() -> None:
    mixer_refill_pump = FakeActuator("mixer_refill_pump")
    sensor = ScriptedSensor(mixer_liters=0.0, ro_liters=500.0)
    # level_filter_samples=1: kein Rolling-Average über die History, damit
    # ein einzelner geänderter Sensorwert sofort als Zielwert erkannt wird.
    settings = _base_settings(target_total_liters=10.0, target_reached_confirm_samples=1, level_filter_samples=1)
    sm = _make_state_machine(sensor=sensor, settings=settings, mixer_refill_pump=mixer_refill_pump)

    sm.start()
    assert sm.state == FillAndMeasureState.START_REFILL_PUMP

    sm.update()
    assert sm.state == FillAndMeasureState.FILL_UNTIL_TARGET
    assert mixer_refill_pump.is_on is True

    sensor.mixer_liters = 15.0  # Zielwert erreicht
    sm.update()
    assert sm.state == FillAndMeasureState.STOP_REFILL_PUMP

    sm.update()
    assert sm.state == FillAndMeasureState.SETTLE_LEVEL
    assert mixer_refill_pump.is_on is False

    sm.update()
    assert sm.state == FillAndMeasureState.START_CIRCULATION

    sm.update()
    assert sm.state == FillAndMeasureState.SENSOR_STABILIZE

    sm.update()
    assert sm.state == FillAndMeasureState.MEASURE_VALUES

    sm.update()
    assert sm.state == FillAndMeasureState.FINISHED
    assert sm.is_done is True
    assert sm.error_message is None


def test_start_circulation_turns_on_configured_pumps() -> None:
    mixing_pump = FakeActuator("mixing_circulation_pump")
    sensor_pump = FakeActuator("sensor_circulation_pump")
    settings = _base_settings(
        target_total_liters=1.0,
        enable_mixing_circulation=True,
        enable_sensor_circulation=True,
    )
    sensor = ScriptedSensor(mixer_liters=5.0, ro_liters=500.0)
    sm = _make_state_machine(
        sensor=sensor,
        settings=settings,
        mixing_circulation_pump=mixing_pump,
        sensor_circulation_pump=sensor_pump,
    )
    sm.start()
    sm.update()  # state was START_REFILL_PUMP -> becomes FILL_UNTIL_TARGET
    sm.update()  # state was FILL_UNTIL_TARGET -> target already reached -> STOP_REFILL_PUMP
    sm.update()  # state was STOP_REFILL_PUMP -> SETTLE_LEVEL
    sm.update()  # state was SETTLE_LEVEL -> START_CIRCULATION (pumps not yet switched on)
    sm.update()  # state was START_CIRCULATION -> turns both pumps on, moves to SENSOR_STABILIZE

    assert mixing_pump.is_on is True
    assert sensor_pump.is_on is True
    assert sm.state == FillAndMeasureState.SENSOR_STABILIZE


# --- is_done --------------------------------------------------------------------


def test_is_done_false_for_every_state_except_finished_and_error() -> None:
    sm = _make_state_machine()
    non_terminal_states = [
        state for state in FillAndMeasureState if state not in (FillAndMeasureState.FINISHED, FillAndMeasureState.ERROR)
    ]
    for state in non_terminal_states:
        sm.state = state
        assert sm.is_done is False, state

    sm.state = FillAndMeasureState.FINISHED
    assert sm.is_done is True

    sm.state = FillAndMeasureState.ERROR
    assert sm.is_done is True


# --- Mixer-Sensor-Fallback-Kette (3-stufig) -------------------------------------


def test_mixer_liters_primary_source_volume_liters_calc() -> None:
    sm = _make_state_machine()
    snapshot = {"mixer": {"volume_liters_calc": 42.5}}
    assert sm._mixer_liters_from_snapshot(snapshot) == 42.5


def test_mixer_liters_first_fallback_raw_with_factor_and_offset() -> None:
    settings = _base_settings(mixer_sensor_liter_factor=2.0, mixer_sensor_liter_offset=1.0)
    sm = _make_state_machine(settings=settings)
    snapshot = {"mixer": {"volume_liters_raw": 10.0}}
    assert sm._mixer_liters_from_snapshot(snapshot) == 21.0  # 10*2 + 1


def test_mixer_liters_second_fallback_level_percent() -> None:
    settings = _base_settings(max_mixer_liters=200.0)
    sm = _make_state_machine(settings=settings)
    snapshot = {"mixer": {"level_percent": 50.0}}
    assert sm._mixer_liters_from_snapshot(snapshot) == 100.0


def test_mixer_liters_no_valid_source_returns_none() -> None:
    sm = _make_state_machine()
    snapshot = {"mixer": {}}
    assert sm._mixer_liters_from_snapshot(snapshot) is None


def test_filtered_mixer_liters_averages_over_history() -> None:
    sm = _make_state_machine(settings=_base_settings(level_filter_samples=3))
    assert sm._filtered_mixer_liters({"mixer": {"volume_liters_calc": 10.0}}) == 10.0
    assert sm._filtered_mixer_liters({"mixer": {"volume_liters_calc": 20.0}}) == 15.0
    assert sm._filtered_mixer_liters({"mixer": {"volume_liters_calc": 30.0}}) == 20.0
    # 4. Wert verdrängt den ältesten (maxlen=3): (20+30+40)/3 statt (10+20+30+40)/4
    assert sm._filtered_mixer_liters({"mixer": {"volume_liters_calc": 40.0}}) == 30.0


# --- RO-Sensor-Fallback-Kette (2-stufig) ----------------------------------------


def test_ro_liters_primary_source_volume_liters_calc() -> None:
    assert FillAndMeasureStateMachine._ro_liters({"ro": {"volume_liters_calc": 300.0}}) == 300.0


def test_ro_liters_fallback_level_percent_with_configured_max() -> None:
    snapshot = {"ro": {"level_percent": 50.0, "configured_max_liters": 1000.0}}
    assert FillAndMeasureStateMachine._ro_liters(snapshot) == 500.0


def test_ro_liters_no_valid_source_returns_none() -> None:
    assert FillAndMeasureStateMachine._ro_liters({"ro": {}}) is None


# --- Fehler-/Abbruchzustände ----------------------------------------------------


def test_start_errors_without_sensor_snapshot_function() -> None:
    sm = FillAndMeasureStateMachine(
        mixer_refill_pump=FakeActuator(),
        get_sensor_snapshot=None,
        settings=_base_settings(),
    )
    sm.start()
    assert sm.state == FillAndMeasureState.ERROR
    assert sm.error_message is not None


def test_start_errors_when_snapshot_is_none() -> None:
    sm = _make_state_machine(sensor=lambda: None)
    sm.start()
    assert sm.state == FillAndMeasureState.ERROR


def test_start_errors_when_not_enough_ro_water() -> None:
    sensor = ScriptedSensor(mixer_liters=0.0, ro_liters=1.0)
    sm = _make_state_machine(sensor=sensor, settings=_base_settings(min_ro_liters_required=20.0))
    sm.start()
    assert sm.state == FillAndMeasureState.ERROR
    assert "RO" in sm.error_message or "ro" in (sm.error_message or "").lower()


def test_fill_until_target_errors_when_mixer_level_lost() -> None:
    sensor = ScriptedSensor(mixer_liters=0.0, ro_liters=500.0)
    sm = _make_state_machine(sensor=sensor, settings=_base_settings(target_total_liters=10.0))
    sm.start()
    sm.update()  # -> FILL_UNTIL_TARGET

    sensor.mixer_liters = None  # Sensor liefert unbrauchbaren Wert
    sensor_snapshot_without_mixer = {"mixer": {}, "ro": {"volume_liters_calc": 500.0}}
    sm.get_sensor_snapshot = lambda: sensor_snapshot_without_mixer

    sm.update()

    assert sm.state == FillAndMeasureState.ERROR
    assert sm.stop_reason == "missing_mixer_level_during_fill"


def test_error_calls_safe_shutdown_and_turns_off_only_present_actuators() -> None:
    mixer_refill_pump = FakeActuator("mixer_refill_pump")
    ro_inlet_valve = FakeActuator("ro_inlet_valve")
    sm = _make_state_machine(mixer_refill_pump=mixer_refill_pump, ro_inlet_valve=ro_inlet_valve)
    # mixing_circulation_pump/sensor_circulation_pump bleiben None (nie konfiguriert)

    mixer_refill_pump.on()
    ro_inlet_valve.on()

    sm.error("simulated failure")

    assert sm.state == FillAndMeasureState.ERROR
    assert sm.error_message == "simulated failure"
    assert mixer_refill_pump.is_on is False
    assert ro_inlet_valve.is_on is False
    # kein AttributeError, obwohl mixing/sensor circulation pump None sind


def test_safe_shutdown_turns_off_all_configured_actuators() -> None:
    mixer_refill_pump = FakeActuator("mixer_refill_pump")
    ro_inlet_valve = FakeActuator("ro_inlet_valve")
    mixing_pump = FakeActuator("mixing_circulation_pump")
    sensor_pump = FakeActuator("sensor_circulation_pump")

    sm = _make_state_machine(
        mixer_refill_pump=mixer_refill_pump,
        ro_inlet_valve=ro_inlet_valve,
        mixing_circulation_pump=mixing_pump,
        sensor_circulation_pump=sensor_pump,
    )
    for actuator in (mixer_refill_pump, ro_inlet_valve, mixing_pump, sensor_pump):
        actuator.on()

    sm.safe_shutdown()

    for actuator in (mixer_refill_pump, ro_inlet_valve, mixing_pump, sensor_pump):
        assert actuator.is_on is False
