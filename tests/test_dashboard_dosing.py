"""AP-05 Schritt 3: Dosierung im Dashboard (hardwarefrei).

ProcessController gibt den Dosierstatus weiter, der Emergency Stop/Shutdown
erreicht den Dosierer, Reset blendet den Dosierstatus eines beendeten Laufs aus,
und die Anzeigezeile weist bestaetigte und unklare Mengen aus. Dosierer ueber
FakeMcu/Fake-Objekte - kein Port, kein GPIO.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from test_process_controller_run_snapshot import FULL_PROCESS_SETTINGS, _install_blocking_fake_run, _release_and_join
from fake_mcu import FakeMcu, FakeSerialPort, make_client_factory
from test_nutrient_dosing import SETTINGS, _plan, _setup

import nicegui_dashboard.recipe_store as recipe_store
from domain.recipe_model import RunOptions
from nicegui_dashboard.pages.dashboard.process_control_section import format_dosing_status
from process.nutrient_dosing import DosingPhase


class RecordingDosing:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.stop_reasons: list[str] = []
        self.waited = 0

    def is_active(self) -> bool:
        return self.active

    def request_stop(self, reason: str = "stopped_by_user") -> None:
        self.stop_reasons.append(reason)

    def wait_stopped(self, timeout=None) -> dict:
        self.waited += 1
        self.active = False
        return {"success": True, "message": "stopped"}

    def get_status(self) -> dict:
        return {"phase": DosingPhase.IDLE}


# --- Anzeige ---------------------------------------------------------------------------------------


def _status(**overrides) -> dict:
    status = {
        "phase": "RUNNING", "current_pump": "P2", "target_ul": 20000, "confirmed_ul": 7500, "uncertain_ul": 0,
        "completion_reason": None, "last_error": None,
        "pumps": [
            {"pump": "P1", "role": "nutrient_a_1", "target_ul": 5000, "confirmed_ul": 5000, "uncertain_ul": 0},
            {"pump": "P2", "role": "nutrient_b_1", "target_ul": 5000, "confirmed_ul": 2500, "uncertain_ul": 0},
        ],
    }
    status.update(overrides)
    return status


def test_no_dosing_run_shows_placeholder():
    assert format_dosing_status(None) == ("Dosierung: -", 0.0, "")


def test_running_shows_active_pump_and_progress():
    text, progress, error = format_dosing_status(_status())
    assert "laeuft" in text and "Pumpe P2 (nutrient_b_1)" in text
    assert "7.5/20.0 ml bestaetigt" in text and "P1 5.0/5.0" in text
    assert progress == 7500 / 20000 and error == ""


def test_parallel_pumps_and_amount_in_flight_are_shown_without_counting_as_progress():
    text, progress, error = format_dosing_status(
        _status(current_pump="P1", active_pumps=["P1", "P2", "P3", "P4"], in_flight_ul=10000)
    )
    assert "Pumpen P1 + P2 + P3 + P4" in text and "10.0 ml in Ausfuehrung" in text
    assert progress == 7500 / 20000 and error == ""  # nur bestaetigte Mengen sind Fortschritt


def test_error_shows_reason_and_uncertain_amount_without_counting_it_as_progress():
    text, progress, error = format_dosing_status(
        _status(phase="ERROR", completion_reason="timeout", last_error="PeristalticTimeoutError: x", uncertain_ul=2500)
    )
    assert "FEHLER" in text and progress == 7500 / 20000  # unklare Menge zaehlt nicht
    assert "timeout" in error and "PeristalticTimeoutError" in error and "2.5 ml unklar" in error


def test_aborted_and_finished():
    assert "abgebrochen" in format_dosing_status(_status(phase="ABORTED", completion_reason="emergency_stop"))[0]
    assert "emergency_stop" in format_dosing_status(_status(phase="ABORTED", completion_reason="emergency_stop"))[2]
    text, progress, error = format_dosing_status(_status(phase="FINISHED", confirmed_ul=20000, completion_reason="completed"))
    assert "abgeschlossen" in text and progress == 1.0 and error == ""


# --- ProcessController -----------------------------------------------------------------------------


def test_dosing_controller_is_shared_with_fill_and_measure(controller):
    assert controller.fill_and_measure._dosing_controller is controller.nutrient_dosing


def test_status_exposes_dosing_only_for_a_dosing_run_and_reset_clears_it(controller, monkeypatch, tmp_path):
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    assert controller.get_status()["dosing"] is None

    controller.fill_and_measure._nutrient_plan = _plan()  # Lauf mit Dosierplan
    assert controller.get_status()["dosing"]["phase"] == DosingPhase.IDLE

    assert controller.acknowledge_error()["success"]
    assert controller.get_status()["dosing"] is None


def test_reset_is_blocked_while_dosing_thread_is_alive(controller):
    controller.fill_and_measure._dosing_controller = RecordingDosing(active=True)
    assert not controller.acknowledge_error()["success"]


def test_emergency_stop_and_shutdown_reach_the_dosing_controller(controller):
    controller.nutrient_dosing = recorder = RecordingDosing()
    asyncio.run(controller.emergency_stop())
    assert recorder.stop_reasons == ["emergency_stop"] and recorder.waited == 1

    controller.shutdown()
    assert recorder.stop_reasons[-1] == "controller_shutdown" and recorder.waited == 2


def test_emergency_stop_interrupts_a_real_running_dose(controller, tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    mcu = FakeMcu()  # DONE kommt nie: Dosis laeuft bis zum Stopp
    port = FakeSerialPort(mcu)
    dosing = controller.nutrient_dosing
    dosing._serial_factory = make_client_factory(port)
    dosing._session_log_dir = tmp_path / "logs"

    assert dosing.start(SETTINGS, _plan())["success"]
    deadline = time.monotonic() + 3
    while not mcu.busy["P1"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mcu.busy["P1"]

    asyncio.run(controller.emergency_stop())

    assert not mcu.busy["P1"]  # STOPALL ist angekommen
    status = controller.nutrient_dosing.get_status()
    assert status["phase"] == DosingPhase.ABORTED and status["completion_reason"] == "emergency_stop"
    assert status["confirmed_ul"] == 0 and status["uncertain_ul"] > 0
    assert not dosing.is_active()


# --- Start ueber den vorhandenen Start-Pfad --------------------------------------------------------------


@pytest.fixture
def dosing_start(controller, monkeypatch, tmp_path, empty_tank):
    """Rezept mit aktivierter Dosierung, Mapping/Profil/Kalibrierung in tmp_path, Fake-Lauf."""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: dict(
        FULL_PROCESS_SETTINGS, enable_mixing_circulation=True, nutrient_ro_resume_progress=0.5, ec_tolerance_ms_cm=0.1
    )
    recipe_store.save_recipe_to_slot(
        slot=1,
        recipe=dict(recipe_store.DEFAULT_RECIPE, recipe_name="Dosing", target_ro_water_l=10.0,
                    nutrients_ml_per_100_l=200.0, nutrient_dosing_enabled=True),
        make_active=True,
    )
    controller.fill_and_measure._plan_builder = lambda settings: _plan(ro_l=10.0)
    started, release = _install_blocking_fake_run(controller)
    yield controller, started, release
    _release_and_join(controller, release)


def test_recipe_start_with_dosing_builds_plan_and_shows_dosing_status(dosing_start):
    controller, started, release = dosing_start
    result = controller.start_fill_and_measure(
        "confirmed", run_options=RunOptions(sensor_circulation_enabled=True, tank_emptied_confirmed=True)
    )
    assert result["success"], result
    assert started.wait(2.0)
    dosing = controller.get_status()["dosing"]
    assert dosing["phase"] == DosingPhase.IDLE and dosing["planned_ul"] == _plan(ro_l=10.0).total_ul


_DOSING_RUN = RunOptions(sensor_circulation_enabled=True, tank_emptied_confirmed=True)


def _assert_blocked_before_anything_starts(controller, started, result, fragment: str) -> None:
    assert not result["success"] and fragment in result["message"], result
    assert not started.is_set() and not controller.fill_and_measure.is_running  # kein Thread, kein GPIO
    assert controller.get_status()["dosing"] is None


@pytest.mark.parametrize("override", [{"nutrient_ro_resume_progress": 0}, {"nutrient_ro_resume_progress": -0.1},
                                      {"nutrient_ro_resume_progress": 1.01}, {"nutrient_ro_resume_progress": True},
                                      {"nutrient_ro_resume_progress": None}, {"nutrient_ro_resume_progress": "abc"}])
def test_dosing_start_with_invalid_resume_progress_is_blocked_before_anything_starts(dosing_start, override):
    controller, started, _release = dosing_start
    controller.load_settings = lambda: dict(
        FULL_PROCESS_SETTINGS, enable_mixing_circulation=True, **{"nutrient_ro_resume_progress": 0.5, **override}
    )
    _assert_blocked_before_anything_starts(
        controller, started, controller.start_fill_and_measure("confirmed", run_options=_DOSING_RUN),
        "nutrient_ro_resume_progress",
    )


def test_dosing_start_without_resume_progress_is_blocked_before_anything_starts(dosing_start):
    controller, started, _release = dosing_start
    controller.load_settings = lambda: dict(FULL_PROCESS_SETTINGS, enable_mixing_circulation=True)  # Schluessel fehlt
    _assert_blocked_before_anything_starts(
        controller, started, controller.start_fill_and_measure("confirmed", run_options=_DOSING_RUN),
        "nutrient_ro_resume_progress fehlt oder ist ungueltig",
    )


def _settings_without_ec_tolerance(**overrides):
    """Prozesseinstellungen OHNE ec_tolerance_ms_cm (auch dann, wenn FULL_PROCESS_SETTINGS den Schluessel je erhaelt)."""
    settings = {key: value for key, value in FULL_PROCESS_SETTINGS.items() if key != "ec_tolerance_ms_cm"}
    settings.update(overrides)
    return settings


def test_dosing_start_without_ec_tolerance_is_blocked_before_anything_starts(dosing_start):
    controller, started, _release = dosing_start
    controller.load_settings = lambda: _settings_without_ec_tolerance(
        enable_mixing_circulation=True, nutrient_ro_resume_progress=0.5
    )
    assert "ec_tolerance_ms_cm" not in controller.load_settings()
    _assert_blocked_before_anything_starts(
        controller, started, controller.start_fill_and_measure("confirmed", run_options=_DOSING_RUN),
        "EC-Toleranz (ec_tolerance_ms_cm) fehlt",
    )


@pytest.mark.parametrize("bad", [0, -0.1, None, "0.1", float("nan"), float("inf"), True])
def test_dosing_start_with_invalid_ec_tolerance_is_blocked_before_anything_starts(dosing_start, bad):
    controller, started, _release = dosing_start
    controller.load_settings = lambda: dict(
        FULL_PROCESS_SETTINGS, enable_mixing_circulation=True, nutrient_ro_resume_progress=0.5, ec_tolerance_ms_cm=bad
    )
    _assert_blocked_before_anything_starts(
        controller, started, controller.start_fill_and_measure("confirmed", run_options=_DOSING_RUN),
        "EC-Toleranz (ec_tolerance_ms_cm) ist ungueltig",
    )


def test_dosing_start_with_valid_resume_progress_and_ec_tolerance_is_accepted(dosing_start):
    controller, started, _release = dosing_start
    controller.load_settings = lambda: dict(
        FULL_PROCESS_SETTINGS, enable_mixing_circulation=True, nutrient_ro_resume_progress=1.0, ec_tolerance_ms_cm=0.1
    )
    assert controller.start_fill_and_measure("confirmed", run_options=_DOSING_RUN)["success"]
    assert started.wait(2.0)


@pytest.mark.parametrize("tolerance", [{"ec_tolerance_ms_cm": 0}, {}], ids=["tolerance invalid", "tolerance missing"])
def test_water_run_does_not_need_dosing_settings(controller, empty_tank, monkeypatch, tmp_path, tolerance):
    """Ohne Naehrstoffdosierung greift weder die Schwellenpruefung noch die Vorabpruefung der EC-Toleranz
    (auch nicht bei fehlendem Schluessel): EC wird dort ohnehin nicht bewertet (NOT_EVALUATED)."""
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller.load_settings = lambda: _settings_without_ec_tolerance(**tolerance)  # kein nutrient_ro_resume_progress
    recipe_store.save_recipe_to_slot(
        slot=1, recipe=dict(recipe_store.DEFAULT_RECIPE, recipe_name="Wasser", target_ro_water_l=10.0), make_active=True
    )
    started, release = _install_blocking_fake_run(controller)
    try:
        assert controller.start_fill_and_measure("confirmed", run_options=_DOSING_RUN)["success"]
        assert started.wait(2.0)
    finally:
        _release_and_join(controller, release)


def test_start_without_sensor_circulation_is_blocked_with_clear_message_and_enables_nothing(dosing_start):
    controller, started, _release = dosing_start
    result = controller.start_fill_and_measure(
        "confirmed", run_options=RunOptions(sensor_circulation_enabled=False, tank_emptied_confirmed=True)
    )
    assert not result["success"] and "zirkulation" in result["message"]
    assert not started.is_set() and not controller.fill_and_measure.is_running
    assert controller.get_status()["dosing"] is None


def test_start_and_prime_are_blocked_while_a_dosing_thread_is_alive(dosing_start):
    controller, started, _release = dosing_start
    controller.fill_and_measure._dosing_controller = RecordingDosing(active=True)
    result = controller.start_fill_and_measure(
        "confirmed", run_options=RunOptions(sensor_circulation_enabled=True, tank_emptied_confirmed=True)
    )
    assert not result["success"] and not started.is_set()
    assert not controller.start_prime({"MCU_B": ["P1"]})["success"]
    assert not controller.acknowledge_error()["success"]


def test_format_evaluation_status() -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_evaluation_status

    assert format_evaluation_status(None) == ""
    assert format_evaluation_status({"status": "OK"}) == "EC/pH-Bewertung: OK"
    assert format_evaluation_status({"status": "FAILED"}) == "EC/pH-Bewertung: FAILED"
    assert "nicht bestaetigt" in format_evaluation_status({"status": "NOT_EVALUATED"})
