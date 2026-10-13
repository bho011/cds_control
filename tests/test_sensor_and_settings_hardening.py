"""
AP-02A: strict sensor value reading (statemachine/sensor_reading.py) and
strict settings types (services/settings_validation.py). Hardware-free,
pure logic.
"""

from __future__ import annotations

import math
from collections import deque

import pytest

from services.settings_validation import SettingField, SettingsValidationError, validate_settings
from statemachine.fill_and_measure_state_machine import FillAndMeasureState, FillAndMeasureStateMachine
from statemachine.sensor_reading import (
    filtered_mixer_liters,
    finite_number,
    mixer_liters_from_snapshot,
    ro_liters_from_snapshot,
)

SETTINGS = {"max_mixer_liters": 200.0}
BAD_VALUES = [float("nan"), float("inf"), float("-inf"), True, False, "12", "12.5", "nan", [1.0], {"v": 1}]


# --- finite_number -----------------------------------------------------------


@pytest.mark.parametrize("value", [0, 0.0, 12, 12.5, -3.2])
def test_finite_number_accepts_real_finite_numbers(value):
    assert finite_number(value) == float(value)


@pytest.mark.parametrize("value", [None, *BAD_VALUES])
def test_finite_number_rejects_everything_else(value):
    assert finite_number(value) is None


# --- mixer -------------------------------------------------------------------


def test_valid_mixer_values_are_still_processed_unchanged():
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_calc": 42.5}}, SETTINGS) == 42.5
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_calc": 0}}, SETTINGS) == 0.0
    assert mixer_liters_from_snapshot(
        {"mixer": {"volume_liters_raw": 10.0}},
        {**SETTINGS, "mixer_sensor_liter_factor": 2.0, "mixer_sensor_liter_offset": 1.0},
    ) == 21.0
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": 50.0}}, SETTINGS) == 100.0


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_invalid_preferred_mixer_value_is_not_masked_by_fallbacks(bad):
    snapshot = {"mixer": {"volume_liters_calc": bad, "volume_liters_raw": 50.0, "level_percent": 50.0}}
    assert mixer_liters_from_snapshot(snapshot, SETTINGS) is None


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_invalid_raw_value_is_not_masked_by_percent_fallback(bad):
    snapshot = {"mixer": {"volume_liters_raw": bad, "level_percent": 50.0}}
    assert mixer_liters_from_snapshot(snapshot, SETTINGS) is None


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_invalid_percent_fallback_is_unusable(bad):
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": bad}}, SETTINGS) is None


@pytest.mark.parametrize("bad", [float("nan"), "2.0", True])
def test_invalid_calibration_override_is_unusable(bad):
    settings = {**SETTINGS, "mixer_sensor_liter_factor": bad}
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 10.0}}, settings) is None


def test_missing_values_are_none():
    assert mixer_liters_from_snapshot({"mixer": {}}, SETTINGS) is None
    assert mixer_liters_from_snapshot({}, SETTINGS) is None


@pytest.mark.parametrize("snapshot", [None, "x", [], {"mixer": "x"}, {"mixer": [1]}, {"mixer": None}])
def test_malformed_snapshot_structure_reads_as_no_value_not_an_exception(snapshot):
    assert mixer_liters_from_snapshot(snapshot, SETTINGS) is None
    assert ro_liters_from_snapshot(snapshot) is None


# --- moving average ----------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_invalid_values_never_enter_the_moving_average(bad):
    history: deque = deque(maxlen=5)
    assert filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": 10.0}}, SETTINGS) == 10.0
    assert filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": bad}}, SETTINGS) is None
    assert list(history) == [10.0]
    assert filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": 20.0}}, SETTINGS) == 15.0


# --- RO ----------------------------------------------------------------------


def test_valid_ro_values_are_still_processed_unchanged():
    assert ro_liters_from_snapshot({"ro": {"volume_liters_calc": 300.0}}) == 300.0
    assert ro_liters_from_snapshot({"ro": {"level_percent": 50.0, "configured_max_liters": 1000.0}}) == 500.0
    assert ro_liters_from_snapshot({"ro": {}}) is None


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_invalid_preferred_ro_value_is_not_masked_by_percent_fallback(bad):
    snapshot = {"ro": {"volume_liters_calc": bad, "level_percent": 50.0, "configured_max_liters": 1000.0}}
    assert ro_liters_from_snapshot(snapshot) is None


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_invalid_ro_percent_or_max_is_unusable(bad):
    assert ro_liters_from_snapshot({"ro": {"level_percent": bad, "configured_max_liters": 1000.0}}) is None
    assert ro_liters_from_snapshot({"ro": {"level_percent": 50.0, "configured_max_liters": bad}}) is None


# --- state machine: existing safety behavior keeps working ---------------------


class _Pump:
    def __init__(self):
        self.is_on = False

    def on(self):
        self.is_on = True

    def off(self):
        self.is_on = False


def _sm(snapshot, **settings):
    base = {
        "min_ro_liters_required": 20.0, "level_filter_samples": 5, "max_mixer_liters": 185.0,
        "max_fill_seconds": 900.0, "no_fill_progress_timeout_seconds": 30.0, "min_fill_progress_liters": 0.5,
        "max_negative_level_drift_liters": 3.0, "target_reached_confirm_samples": 1,
        "fill_mode": "absolute", "target_total_liters": 10.0, "level_settle_seconds": 0.0,
        "sensor_stabilize_seconds": 0.0,
    }
    base.update(settings)
    pump = _Pump()
    sm = FillAndMeasureStateMachine(mixer_refill_pump=pump, get_sensor_snapshot=lambda: snapshot, settings=base)
    return sm, pump


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "500", True])
def test_start_is_rejected_for_invalid_ro_value_nan_used_to_pass(bad):
    sm, pump = _sm({"mixer": {"volume_liters_calc": 0.0}, "ro": {"volume_liters_calc": bad}})
    sm.start()
    assert sm.state == FillAndMeasureState.ERROR
    assert pump.is_on is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "5", False])
def test_start_is_rejected_for_invalid_mixer_value(bad):
    sm, pump = _sm({"mixer": {"volume_liters_calc": bad}, "ro": {"volume_liters_calc": 500.0}})
    sm.start()
    assert sm.state == FillAndMeasureState.ERROR
    assert pump.is_on is False


def test_nan_mixer_during_fill_stops_the_pump_immediately():
    snapshot = {"mixer": {"volume_liters_calc": 0.0}, "ro": {"volume_liters_calc": 500.0}}
    sm, pump = _sm(snapshot)
    sm.start()
    sm.update()  # START_REFILL_PUMP -> pump on
    assert pump.is_on is True
    snapshot["mixer"]["volume_liters_calc"] = float("nan")
    sm.update()
    assert sm.state == FillAndMeasureState.ERROR
    assert pump.is_on is False
    assert sm.stop_reason == "missing_mixer_level_during_fill"


def test_nan_ro_during_fill_stops_the_pump():
    snapshot = {"mixer": {"volume_liters_calc": 0.0}, "ro": {"volume_liters_calc": 500.0}}
    sm, pump = _sm(snapshot)
    sm.start()
    sm.update()
    snapshot["ro"]["volume_liters_calc"] = float("nan")
    sm.update()
    assert sm.state == FillAndMeasureState.ERROR
    assert pump.is_on is False


# --- settings validation -----------------------------------------------------


BOOL = [SettingField("hardware_execution_enabled", bool, required=False, default=False)]
FLOAT = [SettingField("x", float, min_value=0.0, max_value=10.0)]
INT = [SettingField("n", int, min_value=1)]


@pytest.mark.parametrize("value", [True, False])
def test_bool_field_accepts_only_genuine_booleans(value):
    assert validate_settings({"hardware_execution_enabled": value}, BOOL, "t")["hardware_execution_enabled"] is value


@pytest.mark.parametrize("value", ["false", "true", "False", "", "0", 0, 1, None, [], 0.0])
def test_bool_field_rejects_everything_else(value):
    with pytest.raises(SettingsValidationError) as exc:
        validate_settings({"hardware_execution_enabled": value}, BOOL, "t")
    assert "hardware_execution_enabled" in str(exc.value)


def test_missing_optional_bool_still_defaults_to_false():
    assert validate_settings({}, BOOL, "t")["hardware_execution_enabled"] is False


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-Infinity"])
def test_numeric_fields_reject_non_finite_values_even_with_range_checks(value):
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": value}, FLOAT, "t")
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": value}, [SettingField("x", float)], "t")  # without min/max too


@pytest.mark.parametrize("value", [True, False, None, [], {}])
def test_numeric_fields_reject_bool_and_non_numbers(value):
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": value}, FLOAT, "t")
    with pytest.raises(SettingsValidationError):
        validate_settings({"n": value}, INT, "t")


def test_numeric_fields_still_accept_ints_floats_and_numeric_strings():
    assert validate_settings({"x": 5}, FLOAT, "t")["x"] == 5.0
    assert isinstance(validate_settings({"x": 5}, FLOAT, "t")["x"], float)
    assert validate_settings({"x": 2.5}, FLOAT, "t")["x"] == 2.5
    assert validate_settings({"x": "2.5"}, FLOAT, "t")["x"] == 2.5  # unchanged, documented behavior
    assert validate_settings({"n": 3}, INT, "t")["n"] == 3
    assert validate_settings({"n": 3.0}, INT, "t")["n"] == 3
    assert validate_settings({"n": "3"}, INT, "t")["n"] == 3


@pytest.mark.parametrize("value", [2.7, "2.7", float("nan"), float("inf")])
def test_int_field_does_not_silently_truncate(value):
    with pytest.raises(SettingsValidationError):
        validate_settings({"n": value}, INT, "t")


def test_range_checks_still_apply():
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": 11.0}, FLOAT, "t")
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": -0.1}, FLOAT, "t")
    assert not math.isnan(validate_settings({"x": 0.0}, FLOAT, "t")["x"])


# --- Review-Korrekturen R1-R3 ------------------------------------------------


def test_r1_calibration_overflow_is_none_not_clamped_to_zero():
    settings = {**SETTINGS, "mixer_sensor_liter_factor": 1e308, "mixer_sensor_liter_offset": 0.0}
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": -1e308}}, settings) is None
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 1e308}}, settings) is None
    settings = {**SETTINGS, "mixer_sensor_liter_factor": 1.0, "mixer_sensor_liter_offset": -1e308}
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 1e308}}, {**settings, "mixer_sensor_liter_factor": 1e308}) is None


def test_r1_legitimate_negative_calibration_result_is_still_clamped_to_zero():
    settings = {**SETTINGS, "mixer_sensor_liter_factor": 0.610566, "mixer_sensor_liter_offset": -26.093067}
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 10.0}}, settings) == 0.0
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 100.0}}, settings) == pytest.approx(34.963533)


def test_r1_percent_fallback_overflow_is_none():
    settings = {"max_mixer_liters": 1e308}
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": 1e308}}, settings) is None
    assert ro_liters_from_snapshot({"ro": {"level_percent": 1e308, "configured_max_liters": 1e308}}) is None


@pytest.mark.parametrize("huge", [10**400, -(10**400)])
def test_r2_finite_number_rejects_huge_ints_without_overflow_error(huge):
    assert finite_number(huge) is None


def test_r2_huge_int_in_sensor_payload_is_unusable_not_an_exception():
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_calc": 10**400}}, SETTINGS) is None
    assert ro_liters_from_snapshot({"ro": {"volume_liters_calc": 10**400}}) is None


def test_r2_large_but_representable_int_is_still_accepted():
    assert finite_number(10**300) == 1e300


@pytest.mark.parametrize("huge", [10**400, -(10**400)])
def test_r2_settings_huge_int_raises_settings_validation_error(huge):
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": huge}, FLOAT, "t")
    with pytest.raises(SettingsValidationError):
        validate_settings({"n": huge}, INT, "t")
    with pytest.raises(SettingsValidationError):
        validate_settings({"x": huge}, [SettingField("x", float)], "t")


def test_r3_mean_of_two_1e308_values_is_finite_and_correct():
    history: deque = deque(maxlen=5)
    assert filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": 1e308}}, SETTINGS) == 1e308
    result = filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": 1e308}}, SETTINGS)
    assert result is not None and math.isfinite(result)
    assert result == pytest.approx(1e308)


def test_r3_mean_overflow_never_returns_a_non_finite_value():
    history: deque = deque([1.7e308, 1.7e308, 1.7e308], maxlen=5)
    result = filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": 1.7e308}}, SETTINGS)
    assert result is not None and math.isfinite(result)
    assert result == pytest.approx(1.7e308)


def test_r3_normal_means_are_unchanged():
    history: deque = deque(maxlen=3)
    for value, expected in ((10.0, 10.0), (20.0, 15.0), (30.0, 20.0), (40.0, 30.0)):
        assert filtered_mixer_liters(history, {"mixer": {"volume_liters_calc": value}}, SETTINGS) == expected
