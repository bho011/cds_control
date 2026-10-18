"""AP-05 Schritt 2: Mischablauf mit Naehrstoffdosierung (hardwarefrei).

State Machine im Rezeptlauf mit Dosierplan: RO-Vorfuellung -> Zirkulation ->
Dosierung (RO-Pumpe aus) -> RO bis RO-Soll + bestaetigte Menge -> Nachmischen.
Fake-Aktoren, Fake-Uhr, Fake-Sensor, Fake-Dosierer bzw. der echte
NutrientDosingController ueber FakeMcu - keine Hardware, kein GPIO, kein Port.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fake_mcu import AutoCompletingFakeMcu, FakeSerialPort, make_client_factory
from test_fill_runtime_safety import Clock, FakeActuator, Feed
from test_nutrient_dosing import SETTINGS, _plan, _setup
from test_start_validation import _settings

from process.fill_and_measure import FillAndMeasureController
from process.nutrient_dosing import DosingPhase, NutrientDosingController
from statemachine.fill_and_measure_state_machine import FillAndMeasureState as S
from statemachine.fill_and_measure_state_machine import FillAndMeasureStateMachine

RO_L = 20.0  # Plan: 20 l RO, 20 ml Naehrstoff, Vorfuellziel 10 l (= untere Sensorzuverlaessigkeitsgrenze)


class FakeDosing:
    """Skriptbarer Dosier-Controller (gleiche Schnittstelle wie NutrientDosingController)."""

    def __init__(self, plan):
        self.plan = plan
        self.start_calls = 0
        self.stop_reasons: list[str] = []
        self.start_result = {"success": True, "message": "ok"}
        self.phase = DosingPhase.IDLE
        self.confirmed_ul = 0
        self.in_flight_ul = 0
        self.uncertain_ul = 0
        self.completion_reason = None

    def start(self, settings, plan):
        self.start_calls += 1
        if self.start_result["success"]:
            self.phase = DosingPhase.RUNNING
        return self.start_result

    def request_stop(self, reason="stopped_by_user"):
        self.stop_reasons.append(reason)

    def finish(self):
        self.phase, self.completion_reason = DosingPhase.FINISHED, "completed"
        self.confirmed_ul = self.plan.total_ul
        self.in_flight_ul = 0

    def get_status(self):
        return {"phase": self.phase, "confirmed_ul": self.confirmed_ul, "in_flight_ul": self.in_flight_ul,
                "uncertain_ul": self.uncertain_ul, "completion_reason": self.completion_reason}


def plan_for(ro_l=RO_L, **overrides):
    """Standardplan der Mischlauf-Tests: 20 ml Naehrstoff, Vorfuellziel = halbe RO-Menge (wie build_run_plan)."""
    return _plan(ro_l=ro_l, ml_per_100_l=2000.0 / ro_l, prefill_target_l=ro_l / 2, **overrides)


def make(plan=None, ro_l=RO_L, feed=None, pump=None, **overrides):
    plan = plan or plan_for(ro_l)
    feed, clock = feed or Feed(mixer=0.0), Clock()
    pump = pump or FakeActuator("mixer_refill_pump")
    mixing, sensor = FakeActuator("mixing_circulation_pump"), FakeActuator("sensor_circulation_pump")
    dosing = FakeDosing(plan)
    settings = _settings(target=ro_l)
    settings.update(
        max_fill_seconds=900.0, level_settle_seconds=0.0, sensor_stabilize_seconds=0.0,
        no_fill_progress_timeout_seconds=300.0, target_reached_confirm_samples=2,
        enable_mixing_circulation=True, enable_sensor_circulation=True, mixing_seconds=30.0,
        nutrient_ro_resume_progress=0.5, ec_tolerance_ms_cm=0.1,  # wie config/process_settings.json
    )
    settings.update(overrides)
    sm = FillAndMeasureStateMachine(
        mixer_refill_pump=pump, mixing_circulation_pump=mixing, sensor_circulation_pump=sensor,
        get_sensor_snapshot=feed, settings=settings, recipe_run=True, clock=clock,
        nutrient_plan=plan, dosing=dosing,
    )
    return sm, feed, pump, mixing, sensor, dosing, clock


def tick(sm, feed, clock, mixer=None, seconds=1.0):
    """Neue Messung (optional mit neuem Mixerstand), dann ein update()."""
    clock.t += seconds
    feed.advance(**({} if mixer is None else {"mixer": mixer}))
    sm.update()


def run_until(sm, feed, clock, state, pump, rate=1.0, limit=200):
    """Simuliert den Fuellstand (laeuft nur bei eingeschalteter RO-Pumpe)."""
    for _ in range(limit):
        if sm.state == state or sm.state == S.ERROR:
            return
        tick(sm, feed, clock, mixer=feed.mixer + (rate if pump.is_on else 0.0))
    raise AssertionError(f"Zustand {state} nicht erreicht, aktuell {sm.state}")


def to_dosing_running(sm, feed, pump, clock):
    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    assert sm.state == S.DOSING_RUNNING


def test_full_mixing_sequence_with_volume_accounting():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)

    # Dosierung: RO-Pumpe aus, beide Zirkulationen an, genau ein Dosierstart.
    assert pump.is_on is False and mixing.is_on and sensor.is_on
    assert dosing.start_calls == 1
    assert 10.0 <= feed.mixer < 12.0
    for _ in range(3):
        tick(sm, feed, clock)
    assert sm.state == S.DOSING_RUNNING and pump.is_on is False

    dosing.finish()
    tick(sm, feed, clock)
    assert sm.state == S.START_REFILL_PUMP
    run_until(sm, feed, clock, S.POST_MIX, pump)
    assert sm.state == S.POST_MIX and pump.is_on is False and mixing.is_on and sensor.is_on
    # Ziel = RO-Soll + bestaetigte Menge (20 ml): erst darueber wurde abgeschaltet.
    assert feed.mixer >= RO_L + 0.020
    assert pump.on_calls == 2  # Vorfuellung + zweite Phase, nie ein dritter Start

    for _ in range(40):
        tick(sm, feed, clock)
        if sm.is_done:
            break
    assert sm.state == S.FINISHED and sm.error_message is None
    assert dosing.start_calls == 1


def test_dosing_failure_stops_everything_and_never_redoses():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.phase, dosing.completion_reason, dosing.uncertain_ul = DosingPhase.ERROR, "error", 4000
    tick(sm, feed, clock)
    assert sm.state == S.ERROR
    assert not (pump.is_on or mixing.is_on or sensor.is_on)
    assert pump.on_calls == 1  # keine zweite RO-Phase
    assert dosing.start_calls == 1
    for _ in range(5):
        tick(sm, feed, clock)
    assert dosing.start_calls == 1 and pump.on_calls == 1


def test_uncertain_amount_after_finished_dosing_is_an_error():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.finish()
    dosing.uncertain_ul = 1000
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and pump.on_calls == 1


def test_emergency_stop_during_dosing_requests_dosing_stop_and_switches_off():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    sm.error("Emergency stop requested from NiceGUI.")
    assert sm.state == S.ERROR
    assert dosing.stop_reasons, "Dosier-Stopp wurde nicht angefordert"
    assert not (pump.is_on or mixing.is_on or sensor.is_on)
    sm.update()
    assert pump.on_calls == 1


def test_dosing_start_refused_is_an_error_without_ro_phase_two():
    plan = plan_for(RO_L)
    sm, feed, pump, mixing, sensor, dosing, clock = make(plan)
    dosing.start_result = {"success": False, "message": "Port nicht gefunden"}
    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    assert sm.state == S.ERROR and "Port nicht gefunden" in sm.error_message
    assert pump.on_calls == 1 and not (pump.is_on or mixing.is_on or sensor.is_on)
    assert dosing.stop_reasons == []  # nie gestartet: nichts zu stoppen


def test_stale_measurement_during_dosing_aborts_and_stops_dosing():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    clock.t += 6.0  # keine neue MQTT-Messung
    sm.update()
    assert sm.state == S.ERROR and dosing.stop_reasons
    assert not (pump.is_on or mixing.is_on or sensor.is_on)


def test_dosing_requires_both_circulations_before_any_output():
    sm, feed, pump, mixing, sensor, dosing, clock = make(enable_sensor_circulation=False)
    sm.start()
    assert sm.state == S.ERROR and pump.on_calls == 0 and dosing.start_calls == 0


def test_plan_not_matching_recipe_target_blocks_start():
    sm, feed, pump, *_ = make(_plan(ro_l=12.0))
    sm.start()
    assert sm.state == S.ERROR and pump.on_calls == 0


def test_level_below_final_target_after_ro_off_is_an_error():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.finish()
    tick(sm, feed, clock)
    # Messung sinkt waehrend der Bestaetigung unter das Ziel: keine Nachfuellung.
    run_until(sm, feed, clock, S.CONFIRM_TARGET, pump)
    tick(sm, feed, clock, mixer=RO_L - 1.0)
    assert sm.state == S.ERROR and pump.is_on is False
    assert pump.on_calls == 2


def test_without_dosing_work_the_legacy_flow_is_unchanged():
    plan = SimpleNamespace(has_dosing_work=False)
    sm, feed, pump, mixing, sensor, dosing, clock = make(plan)
    assert sm.dosing_enabled is False
    sm.start()
    run_until(sm, feed, clock, S.MEASURE_VALUES, pump)
    assert dosing.start_calls == 0 and pump.on_calls == 1


# --- Integration mit dem echten Dosier-Controller ueber FakeMcu ---------------


def test_real_dosing_controller_full_run_over_fake_mcu(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    port = FakeSerialPort(AutoCompletingFakeMcu())
    dosing = NutrientDosingController(
        get_sensor_snapshot=lambda: {"ok": True}, serial_factory=make_client_factory(port),
        session_log_dir=tmp_path / "logs",
    )
    plan = plan_for(RO_L)
    # Schwelle 1.0 = keine Ueberlappung: der Ablauf haengt hier nicht vom Takt der echten Dosier-Threads ab.
    # Die Ueberlappung mit dem echten Controller prueft tests/test_mixing_overlap.py mit gesteuertem MCU.
    sm, feed, pump, mixing, sensor, _fake, clock = make(plan, nutrient_ro_resume_progress=1.0)
    sm.dosing = dosing
    sm.settings["hardware_execution_enabled"] = True
    assert dosing.check_plan(plan) is None

    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    deadline = time.monotonic() + 10
    while sm.state == S.DOSING_RUNNING and time.monotonic() < deadline:
        tick(sm, feed, clock)
        time.sleep(0.02)
    assert sm.state == S.START_REFILL_PUMP, sm.error_message
    assert sm._final_confirmed_ul == plan.total_ul
    doses = [line for line in port.written_lines if line.startswith("DOSE ")]
    assert doses and sum(float(line.split()[2]) for line in doses) == pytest.approx(plan.total_ml)
    run_until(sm, feed, clock, S.POST_MIX, pump)
    assert sm.state == S.POST_MIX and pump.is_on is False


def test_controller_blocks_start_when_plan_cannot_be_built():
    def failing_builder(_settings):
        raise ValueError("Profil nicht bestaetigt")

    controller = FillAndMeasureController(lambda: {"ok": True}, dosing_controller=SimpleNamespace(is_active=lambda: False), plan_builder=failing_builder)
    settings = _settings(target=RO_L, hardware_execution_enabled=True)
    settings["recipe_run_config_snapshot"]["nutrient_dosing_enabled"] = True
    message = controller._prepare_nutrient_plan_locked(settings)
    assert message is not None and "Profil nicht bestaetigt" in message
    assert controller.is_active() is False


def test_controller_blocks_start_with_dosing_but_without_circulation():
    plan = plan_for(RO_L)
    controller = FillAndMeasureController(lambda: {"ok": True}, dosing_controller=FakeDosing(plan), plan_builder=lambda _s: plan)
    settings = _settings(target=RO_L, enable_mixing_circulation=True, enable_sensor_circulation=False)
    settings["recipe_run_config_snapshot"]["nutrient_dosing_enabled"] = True
    message = controller._prepare_nutrient_plan_locked(settings)
    assert message is not None and "zirkulation" in message


def test_dosing_thread_outliving_the_wait_keeps_the_process_busy():
    class StuckDosing(FakeDosing):
        active = True
        waits = 0

        def is_active(self):
            return self.active

        def wait_stopped(self, timeout=None):
            self.waits += 1  # Frist laeuft ab, der Thread laeuft weiter

    plan = plan_for(RO_L)
    dosing = StuckDosing(plan)
    controller = FillAndMeasureController(lambda: {"ok": True}, dosing_controller=dosing, plan_builder=lambda _s: plan)
    controller._nutrient_plan = plan

    controller._shutdown_and_wait_for_dosing()
    assert dosing.waits == 1 and dosing.stop_reasons == ["process_end"]
    assert controller._last_error is not None
    # Kein Neustart, kein Reset, Prozess gilt weiter als aktiv.
    assert controller.is_active() is True
    started = controller.start(_settings(target=RO_L, hardware_execution_enabled=True))
    assert started["success"] is False and "Dosier-Thread" in started["message"]
    assert controller.acknowledge_error()["success"] is False

    dosing.active = False  # erst jetzt ist MCU_B sicher frei
    assert controller.is_active() is False
    assert controller.acknowledge_error()["success"] is True


def test_get_status_exposes_final_evaluation() -> None:
    controller = FillAndMeasureController(lambda: {"ok": True})
    assert controller.get_status()["final_evaluation"] is None
    controller.state_machine = SimpleNamespace(
        state=SimpleNamespace(name="FINISHED"),
        error_message=None,
        start_mixer_liters=None,
        last_added_liters=None,
        final_evaluation_status="NOT_EVALUATED",
        final_evaluation={"EC": {"status": "NOT_EVALUATED"}},
    )
    status = controller.get_status()
    assert status["final_evaluation"]["status"] == "NOT_EVALUATED"
    assert status["final_evaluation"]["details"] == {"EC": {"status": "NOT_EVALUATED"}}
