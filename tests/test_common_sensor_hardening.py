"""AP-03A: Haertung des gemeinsamen Sensorpfads in process/common.py.

Hardwarefrei: reine Funktionen und ein Fake-Sensor-Reader.
"""

from __future__ import annotations

import math
from collections import deque

import pytest

import process.common as common
from process.common import (
    make_level_history,
    mixer_liters_from_snapshot,
    read_metrics,
    ro_liters_from_snapshot,
)

SETTINGS = {"max_mixer_liters": 200.0, "level_filter_samples": 3}
BAD = [float("nan"), float("inf"), float("-inf"), True, False, "12", "nan", 10**400, [1], {"a": 1}]


class FakeReader:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)

    def get_latest(self, max_age_seconds=None):
        return self.snapshots.pop(0)


@pytest.fixture
def calibration(monkeypatch):
    cal = {"factor": 0.5, "offset": 1.0}
    monkeypatch.setattr(common, "get_mixer_level_calibration", lambda: cal)
    return cal


# --- valid behaviour is preserved ---------------------------------------

def test_valid_mixer_values():
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_calc": 42.5}}, SETTINGS) == 42.5
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_calc": 0}}, SETTINGS) == 0.0
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": 50}}, SETTINGS) == 100.0
    assert mixer_liters_from_snapshot({}, SETTINGS) is None
    assert mixer_liters_from_snapshot({"mixer": {}}, SETTINGS) is None


def test_valid_raw_calibration_and_zero_clamp(calibration):
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 10.0}}, SETTINGS) == 6.0
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": -10.0}}, SETTINGS) == 0.0


def test_valid_ro_values():
    assert ro_liters_from_snapshot({"ro": {"volume_liters_calc": 300.0}}) == 300.0
    assert ro_liters_from_snapshot({"ro": {"level_percent": 50, "configured_max_liters": 1000}}) == 500.0
    assert ro_liters_from_snapshot({"ro": {}}) is None
    assert ro_liters_from_snapshot({}) is None


# --- invalid values -----------------------------------------------------

@pytest.mark.parametrize("bad", BAD)
def test_invalid_preferred_mixer_value_not_masked_by_fallback(bad):
    snapshot = {"mixer": {"volume_liters_calc": bad, "volume_liters_raw": 10.0, "level_percent": 50.0}}
    assert mixer_liters_from_snapshot(snapshot, SETTINGS) is None


@pytest.mark.parametrize("bad", BAD)
def test_invalid_raw_and_percent_mixer(bad, calibration):
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": bad}}, SETTINGS) is None
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": bad}}, SETTINGS) is None
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": 50.0}}, {"max_mixer_liters": bad}) is None


def test_missing_max_mixer_liters_is_unavailable():
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": 50.0}}, {}) is None


@pytest.mark.parametrize("bad", BAD)
def test_invalid_ro_values(bad):
    snapshot = {"ro": {"volume_liters_calc": bad, "level_percent": 50.0, "configured_max_liters": 1000.0}}
    assert ro_liters_from_snapshot(snapshot) is None
    assert ro_liters_from_snapshot({"ro": {"level_percent": bad, "configured_max_liters": 1000.0}}) is None
    assert ro_liters_from_snapshot({"ro": {"level_percent": 50.0, "configured_max_liters": bad}}) is None


@pytest.mark.parametrize("snapshot", ["text", 5, [1, 2], None, {"mixer": "x", "ro": [1]}, {"mixer": None, "ro": 3}])
def test_malformed_snapshot_structures(snapshot):
    assert mixer_liters_from_snapshot(snapshot, SETTINGS) is None
    assert ro_liters_from_snapshot(snapshot) is None


# --- calibration --------------------------------------------------------

def test_calibration_overflow_is_not_zero_liters(monkeypatch):
    monkeypatch.setattr(common, "get_mixer_level_calibration", lambda: {"factor": 1e308, "offset": 0.0})
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": -1e308}}, SETTINGS) is None
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 1e308}}, SETTINGS) is None


@pytest.mark.parametrize(
    "cal",
    [{"factor": float("nan"), "offset": 0.0}, {"factor": "1", "offset": 0.0}, {"factor": 1.0, "offset": True}, {"factor": 1.0}, None],
)
def test_invalid_calibration_is_unavailable(monkeypatch, cal):
    monkeypatch.setattr(common, "get_mixer_level_calibration", lambda: cal)
    assert mixer_liters_from_snapshot({"mixer": {"volume_liters_raw": 10.0}}, SETTINGS) is None


def test_percent_overflow_is_unavailable():
    assert mixer_liters_from_snapshot({"mixer": {"level_percent": 1e308}}, {"max_mixer_liters": 1e308}) is None
    assert ro_liters_from_snapshot({"ro": {"level_percent": 1e308, "configured_max_liters": 1e308}}) is None


# --- read_metrics / history --------------------------------------------

def test_read_metrics_valid_average_and_ro():
    history = make_level_history(SETTINGS)
    reader = FakeReader(
        {"mixer": {"volume_liters_calc": 10.0}, "ro": {"volume_liters_calc": 500.0}},
        {"mixer": {"volume_liters_calc": 20.0}, "ro": {"volume_liters_calc": 490.0}},
    )
    assert read_metrics(reader, SETTINGS, history).mixer_liters_filtered == 10.0
    m = read_metrics(reader, SETTINGS, history)
    assert m.mixer_liters_filtered == 15.0
    assert m.ro_liters == 490.0


@pytest.mark.parametrize("bad", BAD)
def test_invalid_reading_does_not_poison_history(bad):
    history = make_level_history(SETTINGS)
    reader = FakeReader(
        {"mixer": {"volume_liters_calc": 10.0}},
        {"mixer": {"volume_liters_calc": bad}, "ro": {"volume_liters_calc": bad}},
        {"mixer": {"volume_liters_calc": 20.0}},
    )
    read_metrics(reader, SETTINGS, history)
    m = read_metrics(reader, SETTINGS, history)
    assert m.mixer_liters_filtered is None
    assert m.ro_liters is None
    assert list(history) == [10.0]
    assert read_metrics(reader, SETTINGS, history).mixer_liters_filtered == 15.0


@pytest.mark.parametrize("snapshot", [None, "x", [], 7])
def test_read_metrics_non_dict_snapshot(snapshot):
    history = deque(maxlen=3)
    m = read_metrics(FakeReader(snapshot), SETTINGS, history)
    assert m.snapshot == {}
    assert m.mixer_liters_filtered is None and m.ro_liters is None
    assert not history


def test_mean_overflow_does_not_become_infinity():
    history = make_level_history(SETTINGS)
    reader = FakeReader(
        {"mixer": {"volume_liters_calc": 1e308}},
        {"mixer": {"volume_liters_calc": 1e308}},
    )
    read_metrics(reader, SETTINGS, history)
    m = read_metrics(reader, SETTINGS, history)
    assert math.isfinite(m.mixer_liters_filtered)
    assert m.mixer_liters_filtered == pytest.approx(1e308)
