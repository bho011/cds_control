"""
process/nutrient_dosing.py (AP-05 Schritt 1, Prototyp) - hardwarefrei über
tests/fake_mcu.py. Mapping/Profil/Kalibrierung/Logs liegen in tmp_path.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest
from fake_mcu import AutoCompletingFakeMcu, FakeMcu, FakeSerialPort, make_client_factory
from process import nutrient_dosing
from process.nutrient_dosing import NUTRIENT_MAX_DOSE_ML, DosingPhase, NutrientDosingController
from domain.recipe.models import RunConfigSnapshot, RunOptions, StoredRecipe
from domain.recipe.nutrient_plan import PUMP_ROLES, build_nutrient_dosing_plan
from services.peristaltic.calibration import default_calibration_data, save_calibration_data
from services.peristaltic.firmware_profiles import (
    ControllerFirmwareProfile,
    PumpFirmwareEntry,
    default_firmware_profiles,
    save_firmware_profiles,
)
from services.peristaltic.models import default_mapping, save_mapping

FW_ML_PER_STEP = 0.000191096
PROFILE_ID = "test-profile"
SETTINGS = {"hardware_execution_enabled": True}


def _profile(max_runtime_ms: int = 30000) -> ControllerFirmwareProfile:
    return ControllerFirmwareProfile(
        status="confirmed", profile_id=PROFILE_ID, microsteps=8, speed_rpm=120,
        acceleration_steps_per_s2=2000, max_runtime_ms=max_runtime_ms,
        pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=FW_ML_PER_STEP) for pump in ("P1", "P2", "P3", "P4")},
    )


def _setup(monkeypatch, tmp_path: Path, *, max_runtime_ms: int = 30000, calibrated: bool = True) -> str:
    port = tmp_path / "fake-mcu-b"
    port.touch()
    profiles = default_firmware_profiles()
    profiles.controllers["MCU_B"] = _profile(max_runtime_ms)
    save_firmware_profiles(tmp_path / "profiles.json", profiles)
    mapping = default_mapping()
    mapping.controllers["MCU_B"].port = str(port)
    save_mapping(tmp_path / "mapping.json", mapping)
    data = default_calibration_data()
    if calibrated:
        for entry in data["controllers"]["MCU_B"].values():
            entry["status"] = "candidate"
    save_calibration_data(tmp_path / "calibration.json", data)
    monkeypatch.setattr(nutrient_dosing, "MAPPING_PATH", tmp_path / "mapping.json")
    monkeypatch.setattr(nutrient_dosing, "FIRMWARE_PROFILES_PATH", tmp_path / "profiles.json")
    monkeypatch.setattr(nutrient_dosing, "CALIBRATION_DATA_PATH", tmp_path / "calibration.json")
    return str(port)


def _plan(*, ro_l: float = 10.0, ml_per_100_l: float = 200.0, max_chunk_ml: float = 4.0, **overrides):
    """10 l x 200 ml/100 l = 20 ml gesamt, je Pumpe 5 ml, Chunks <= max_chunk_ml."""
    recipe = StoredRecipe(
        slot=1, recipe_name="Test", target_ro_water_l=ro_l, target_ec_ms_cm=1.5, ec_mixing_time_seconds=300,
        nutrients_ml_per_100_l=ml_per_100_l, nutrient_a_percent=50.0, ec_adjustment_factor=1.0,
        nutrient_dosing_enabled=True,
    )
    snapshot = RunConfigSnapshot.build(recipe, {"max_mixer_liters": 185.0}, RunOptions())
    kwargs = dict(
        pump_roles=dict(PUMP_ROLES), pump_weights={p: FW_ML_PER_STEP for p in PUMP_ROLES},
        profile_id=PROFILE_ID, max_chunk_ml=max_chunk_ml, prefill_target_l=5.0,
    )
    kwargs.update(overrides)
    return build_nutrient_dosing_plan(snapshot, **kwargs)


def _controller(tmp_path: Path, port: FakeSerialPort | None = None, factory=None) -> NutrientDosingController:
    def must_not_open(*_a):
        raise AssertionError("Serieller Port darf nicht geöffnet werden.")

    return NutrientDosingController(
        get_sensor_snapshot=lambda: {"ok": True},
        serial_factory=factory or (make_client_factory(port) if port else must_not_open),
        session_log_dir=tmp_path / "logs",
    )


def _wait(controller: NutrientDosingController, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while controller.is_active() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not controller.is_active()


def _doses(port: FakeSerialPort) -> list[str]:
    return [line for line in port.written_lines if line.startswith("DOSE ")]


class _TimeoutOnNthDose(AutoCompletingFakeMcu):
    def __init__(self, n: int) -> None:
        super().__init__()
        self.n, self.count = n, 0

    def handle_command(self, line: str) -> list[str]:
        lines = super().handle_command(line)
        if line.startswith("DOSE "):
            self.count += 1
            if self.count == self.n:
                pump = line.split()[1]
                return [lines[0], *self.timeout_dose(pump)]
        return lines


class _RefusingMcu(AutoCompletingFakeMcu):
    def handle_command(self, line: str) -> list[str]:
        if line.startswith("DOSE "):
            return [f"ERR {line.split()[1]} BUSY"]
        return super().handle_command(line)


class _WrongAmountMcu(AutoCompletingFakeMcu):
    def handle_command(self, line: str) -> list[str]:
        lines = super().handle_command(line)
        return [lines[0], f"DONE {line.split()[1]} 0.500"] if line.startswith("DOSE ") else lines


# --- Erfolg und Reihenfolge ------------------------------------------------------------------


def test_full_run_is_sequential_in_pump_order_and_counts_confirmed(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    plan = _plan()
    port = FakeSerialPort(AutoCompletingFakeMcu())
    controller = _controller(tmp_path, port)

    assert controller.start(SETTINGS, plan)["success"]
    _wait(controller)

    doses = _doses(port)
    pumps_in_order = [line.split()[1] for line in doses]
    assert pumps_in_order == sorted(pumps_in_order)  # P1.. dann P2.. dann P3.. dann P4
    assert set(pumps_in_order) == {"P1", "P2", "P3", "P4"}
    assert all(float(line.split()[2]) <= NUTRIENT_MAX_DOSE_ML for line in doses)
    status = controller.get_status()
    assert status["phase"] == DosingPhase.FINISHED and status["completion_reason"] == "completed"
    assert status["confirmed_ul"] == plan.total_ul == status["target_ul"]
    assert status["uncertain_ul"] == 0
    assert port.written_lines[-1] == "STOPALL" and not port.is_open


# --- Gates -----------------------------------------------------------------------------------


@pytest.mark.parametrize("settings", [{}, {"hardware_execution_enabled": False}, {"hardware_execution_enabled": 1}])
def test_hardware_execution_enabled_is_mandatory(tmp_path, monkeypatch, settings):
    _setup(monkeypatch, tmp_path)
    controller = _controller(tmp_path)  # Port-Öffnen würde AssertionError werfen
    assert not controller.start(settings, _plan())["success"]
    assert not controller.is_active()


def test_missing_sensor_snapshot_blocks(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    controller = NutrientDosingController(lambda: None, serial_factory=lambda *_: pytest.fail("open"), session_log_dir=tmp_path)
    assert not controller.start(SETTINGS, _plan())["success"]


def test_chunk_above_prototype_limit_is_blocked(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    plan = _plan(ml_per_100_l=2000.0, max_chunk_ml=20.0)  # Plan erlaubt 20 ml, Prototyp nur 10
    assert max(max(t.chunks_ul) for t in plan.pump_targets) > 10_000
    result = _controller(tmp_path).start(SETTINGS, plan)
    assert not result["success"] and "Prototypgrenze" in result["message"]


def test_runtime_estimate_above_firmware_limit_is_blocked(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, max_runtime_ms=1000)  # 4 ml brauchen ~6 s
    result = _controller(tmp_path).start(SETTINGS, _plan())
    assert not result["success"] and "Laufzeit" in result["message"]


def test_plan_mismatches_are_blocked(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    plan = _plan()
    cases = {
        "hash": dataclasses.replace(plan, total_ul=plan.total_ul + 1),
        "profile": dataclasses.replace(plan, profile_id="anderes-profil"),
        "controller": dataclasses.replace(plan, controller="MCU_A"),
        "kein plan": "plan",
    }
    for name, bad in cases.items():
        result = _controller(tmp_path).start(SETTINGS, bad)
        assert not result["success"], name


def test_mapping_role_differs_from_plan_is_blocked(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    mapping = default_mapping()
    mapping.controllers["MCU_B"].port = str(tmp_path / "fake-mcu-b")
    mapping.controllers["MCU_B"].pumps.update({"P1": "nutrient_b_1", "P2": "nutrient_a_1"})
    save_mapping(tmp_path / "mapping.json", mapping)
    result = _controller(tmp_path).start(SETTINGS, _plan())
    assert not result["success"] and "Mapping" in result["message"]


def test_uncalibrated_pump_is_blocked(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, calibrated=False)
    assert not _controller(tmp_path).start(SETTINGS, _plan())["success"]


# --- Fehlerpfade: stoppen, nie wiederholen -------------------------------------------------------


def test_firmware_timeout_stops_run_without_retry_and_marks_chunk_uncertain(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    plan = _plan()
    port = FakeSerialPort(_TimeoutOnNthDose(3))
    controller = _controller(tmp_path, port)
    controller.start(SETTINGS, plan)
    _wait(controller)

    assert len(_doses(port)) == 3  # kein Wiederholen, keine weiteren Pumpen
    status = controller.get_status()
    assert status["phase"] == DosingPhase.ERROR and status["completion_reason"] == "timeout"
    p1, p2 = plan.pump_targets[0], plan.pump_targets[1]
    assert len(p1.chunks_ul) == 2  # Dosis 1+2 = P1, Dosis 3 = erster P2-Chunk
    assert status["confirmed_ul"] == p1.target_ul
    assert status["uncertain_ul"] == p2.chunks_ul[0]
    assert port.written_lines.count("STOPALL") >= 2


def test_firmware_refusal_is_not_counted_uncertain(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    port = FakeSerialPort(_RefusingMcu())
    controller = _controller(tmp_path, port)
    controller.start(SETTINGS, _plan())
    _wait(controller)
    status = controller.get_status()
    assert status["phase"] == DosingPhase.ERROR
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] == 0
    assert len(_doses(port)) == 1


def test_done_with_wrong_amount_is_an_error_and_not_confirmed(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    port = FakeSerialPort(_WrongAmountMcu())
    controller = _controller(tmp_path, port)
    controller.start(SETTINGS, _plan())
    _wait(controller)
    status = controller.get_status()
    assert status["completion_reason"] == "safety_check_failed"
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] > 0
    assert len(_doses(port)) == 1


def test_connection_loss_stops_run(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    port = FakeSerialPort(FakeMcu())  # DONE kommt nie
    controller = _controller(tmp_path, port)
    controller.start(SETTINGS, _plan())
    deadline = time.monotonic() + 3
    while not _doses(port) and time.monotonic() < deadline:
        time.sleep(0.01)
    port.simulate_disconnect()
    _wait(controller)
    status = controller.get_status()
    assert status["completion_reason"] == "connection_error" and status["phase"] == DosingPhase.ERROR
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] > 0
    assert len(_doses(port)) == 1


# --- Stop / Emergency Stop unterbricht einen laufenden Befehl --------------------------------------


@pytest.mark.parametrize("reason,expected", [("stopped_by_user", "user_abort"), ("emergency_stop", "emergency_stop")])
def test_stop_interrupts_running_dose_with_stopall(tmp_path, monkeypatch, reason, expected):
    _setup(monkeypatch, tmp_path)
    mcu = FakeMcu()  # DOSE startet, DONE kommt nie: Befehl läuft "für immer"
    port = FakeSerialPort(mcu)
    controller = _controller(tmp_path, port)
    controller.start(SETTINGS, _plan())
    deadline = time.monotonic() + 3
    while not _doses(port) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mcu.busy["P1"]

    t0 = time.monotonic()
    controller.request_stop(reason=reason)
    _wait(controller, timeout=3.0)

    assert time.monotonic() - t0 < 2.0  # deutlich unter den 35 s DONE-Wartezeit
    assert not mcu.busy["P1"]  # STOPALL wurde wirklich gesendet und wirkt
    status = controller.get_status()
    assert status["phase"] == DosingPhase.ABORTED and status["completion_reason"] == expected
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] > 0
    assert len(_doses(port)) == 1


# --- Einmalnutzung je Plan ---------------------------------------------------------------------


def test_same_plan_cannot_be_started_again_after_failure_or_completion(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    plan = _plan()
    port = FakeSerialPort(_TimeoutOnNthDose(2))
    controller = _controller(tmp_path, port)
    assert controller.start(SETTINGS, plan)["success"]
    _wait(controller)
    doses_before = len(_doses(port))
    assert controller.get_status()["phase"] == DosingPhase.ERROR

    result = controller.start(SETTINGS, plan)  # derselbe Plan nach Teil-/Fehllauf
    assert not result["success"] and "bereits gestartet" in result["message"]
    assert not controller.is_active() and len(_doses(port)) == doses_before

    done_port = FakeSerialPort(AutoCompletingFakeMcu())
    ok = _controller(tmp_path, done_port)
    assert ok.start(SETTINGS, plan)["success"]
    _wait(ok)
    assert not ok.start(SETTINGS, plan)["success"]  # auch nach vollständigem Lauf

    other = _plan(ml_per_100_l=100.0)  # neuer Mischvorgang = neuer Plan
    assert other.plan_hash != plan.plan_hash
    assert ok.start(SETTINGS, other)["success"]
    _wait(ok)


# --- Stopp, während start_dose() noch auf OK START wartet ---------------------------------------------


class _DelayedStartReplyPort(FakeSerialPort):
    """Hält die Antwort auf DOSE (OK START) zurück, bis release() aufgerufen wird."""

    def __init__(self, mcu) -> None:
        super().__init__(mcu)
        self.hold = True
        self._held: list[str] = []

    def write(self, data: bytes) -> int:
        text = data.decode("ascii").strip()
        if self.hold and text.startswith("DOSE "):
            self.written_lines.append(text)
            self._held = self.mcu.handle_command(text)
            return len(data)
        return super().write(data)

    def release(self) -> None:
        for line in self._held:
            self.inject_line(line)


def test_stop_while_waiting_for_ok_start_is_controlled(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    mcu = FakeMcu()
    port = _DelayedStartReplyPort(mcu)
    controller = _controller(tmp_path, port)
    controller.start(SETTINGS, _plan())
    deadline = time.monotonic() + 3
    while not _doses(port) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _doses(port) and mcu.busy["P1"]  # DOSE geschrieben, OK START noch nicht angekommen

    controller.request_stop(reason="emergency_stop")
    time.sleep(0.3)
    assert controller.is_active()  # start_dose() wartet noch; der Stopp ist nur vorgemerkt
    stopalls_before = port.written_lines.count("STOPALL")

    port.release()  # OK START trifft ein -> Stopp wird jetzt ausgeführt
    _wait(controller, timeout=3.0)

    assert port.written_lines.count("STOPALL") > stopalls_before
    assert not mcu.busy["P1"]
    status = controller.get_status()
    assert status["phase"] == DosingPhase.ABORTED and status["completion_reason"] == "emergency_stop"
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] > 0
    assert len(_doses(port)) == 1
