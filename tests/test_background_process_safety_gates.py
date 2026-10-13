"""AP-03B: gemeinsames Start-Gate von BackgroundHardwareProcess.

Hardwarefrei: _run wird durch einen Zaehler ersetzt, der Sensorreader ist ein
Fake. Die AP-00-Isolation (conftest) laesst jeden Hardwarezugriff scheitern.
"""

from __future__ import annotations

import pytest

from process.background_process import BackgroundHardwareProcess
from process.fill_and_measure import FillAndMeasureController
from process.manual_drain_jog import ManualDrainJog
from process.pump_prime import PumpPrimeController
from process.tank_cleaning import TankCleaningController

SNAPSHOT = {"mixer": {"volume_liters_calc": 10.0}, "ro": {"volume_liters_calc": 500.0}}

NOT_TRUE = [False, "false", "true", "True", 1, 0, 1.0, None, [], {}, "yes"]


class _Dummy(BackgroundHardwareProcess):
    label = "Dummy"

    def _run(self, settings):  # pragma: no cover - must never run in blocked cases
        raise AssertionError("_run must not be executed")


def _gate(reader, settings):
    proc = _Dummy(reader)
    with proc._lock:
        return proc, proc._check_start_preconditions_locked(settings)


def _raising():
    raise RuntimeError("sensor boom")


# --- R1: strict hardware flag ------------------------------------------

def test_true_with_snapshot_passes():
    proc, result = _gate(lambda: SNAPSHOT, {"hardware_execution_enabled": True})
    assert result is None
    assert proc._last_error is None


@pytest.mark.parametrize("value", NOT_TRUE)
def test_non_true_flag_blocks_without_reading_sensor(value):
    calls = []
    proc, result = _gate(lambda: calls.append(1) or SNAPSHOT, {"hardware_execution_enabled": value})
    assert result is not None and "locked" in result
    assert "hardware_execution_enabled" in proc._last_error
    assert proc._last_message == result
    assert calls == []


def test_missing_flag_blocks():
    proc, result = _gate(lambda: SNAPSHOT, {})
    assert result is not None
    assert "hardware_execution_enabled" in proc._last_error


@pytest.mark.parametrize("settings", [None, "text", ["hardware_execution_enabled"], 5, True])
def test_invalid_settings_structure_blocks(settings):
    proc, result = _gate(lambda: SNAPSHOT, settings)
    assert result is not None
    assert "hardware_execution_enabled" in proc._last_error


# --- R2: sensor reader --------------------------------------------------

def test_snapshot_none_blocks():
    proc, result = _gate(lambda: None, {"hardware_execution_enabled": True})
    assert result == "Dummy locked: no current sensor snapshot."
    assert proc._last_error == "No current sensor snapshot available."


def test_sensor_exception_blocks_in_controlled_way():
    proc, result = _gate(_raising, {"hardware_execution_enabled": True})
    assert result == "Dummy locked: sensor snapshot could not be read (RuntimeError: sensor boom)."
    assert proc._last_error == result
    assert proc._last_message == result


# --- controllers: no run, no thread -------------------------------------

def _make(kind, reader):
    return {"jog": ManualDrainJog, "tank": TankCleaningController, "fill": FillAndMeasureController}[kind](reader)


@pytest.fixture
def run_calls(monkeypatch):
    calls = []
    for cls in (ManualDrainJog, TankCleaningController, FillAndMeasureController):
        monkeypatch.setattr(cls, "_run", lambda self, settings: calls.append(type(self).__name__))
    return calls


@pytest.mark.parametrize("kind", ["jog", "tank", "fill"])
@pytest.mark.parametrize("settings", [{"hardware_execution_enabled": "true"}, {"hardware_execution_enabled": 1}, {}, None, "x"])
def test_controller_blocks_bad_flag(kind, settings, run_calls):
    proc = _make(kind, lambda: SNAPSHOT)
    result = proc.start(settings)
    assert result["success"] is False
    assert run_calls == []
    assert proc._thread is None and not proc.is_active()
    assert proc._last_error


@pytest.mark.parametrize("kind", ["jog", "tank", "fill"])
@pytest.mark.parametrize("reader", [lambda: None, _raising])
def test_controller_blocks_bad_sensor(kind, reader, run_calls):
    proc = _make(kind, reader)
    result = proc.start({"hardware_execution_enabled": True})
    assert result["success"] is False
    assert run_calls == []
    assert proc._thread is None and not proc.is_active()
    assert proc._last_error
    assert proc._last_message


@pytest.mark.parametrize("kind", ["jog", "tank"])
def test_controller_valid_start_still_runs(kind, run_calls):
    proc = _make(kind, lambda: SNAPSHOT)
    result = proc.start({"hardware_execution_enabled": True})
    assert result["success"] is True
    proc._thread.join(timeout=2.0)
    assert len(run_calls) == 1


def test_fill_and_measure_own_gates_still_apply(run_calls):
    # Flag fine, snapshot present but start validation (mixer/RO data missing) refuses.
    proc = FillAndMeasureController(lambda: {"mixer": {}, "ro": {}})
    result = proc.start({"hardware_execution_enabled": True, "target_fill_total_liters": 50.0})
    assert result["success"] is False
    assert run_calls == []
    assert proc._thread is None and not proc.is_running


# --- Prime is deliberately unchanged ------------------------------------

def test_prime_keeps_its_own_override():
    assert PumpPrimeController._check_start_preconditions_locked is not BackgroundHardwareProcess._check_start_preconditions_locked


def test_prime_does_not_use_base_gate(monkeypatch, tmp_path):
    from process import pump_prime

    reads = []
    monkeypatch.setattr(pump_prime, "MAPPING_PATH", tmp_path / "missing_mapping.json")
    prime = PumpPrimeController(
        get_sensor_snapshot=lambda: reads.append(1) or _raising(),
        session_log_dir=tmp_path / "logs",
    )
    # No hardware flag and a failing sensor reader: the block comes from
    # Prime's own mapping gate, never from the base gate, and the reader is untouched.
    result = prime.start({"pumps": {"MCU_B": ["P1"]}})
    assert result["success"] is False
    assert "Mapping" in result["message"]
    assert reads == []
    assert prime._thread is None
