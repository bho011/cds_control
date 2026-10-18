"""
AP-05: RO-Befuellung PARALLEL zur Naehrstoffdosierung (statemachine/fill_and_measure_state_machine.py), hardwarefrei.

Ablauf: RO-Vorfuellung -> Zirkulation -> Dosierung (RO aus) -> ab dem bestaetigten Anteil
``nutrient_ro_resume_progress`` (config/process_settings.json, 0.5) startet die RO-Pumpe zusaetzlich
(DOSING_AND_RO_FILL) -> RO schaltet bei RO-Soll + bestaetigter + in Ausfuehrung befindlicher Menge sofort ab ->
Nachmischen erst, wenn die Dosierung vollstaendig bestaetigt UND das endgueltige Ziel bei ausgeschalteter RO
bestaetigt ist. Ein erreichtes Zwischenziel ist kein erfolgreicher Abschluss; die RO-Pumpe wird hoechstens
zweimal eingeschaltet, eine Nachfuelllogik gibt es nicht.

Fake-Aktoren, Fake-Uhr, Fake-Sensor, FakeDosing bzw. der echte NutrientDosingController ueber ``ManualCompletionMcu``
- keine Hardware, kein GPIO, kein Port.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fake_mcu import FakeSerialPort, ManualCompletionMcu, make_client_factory, wait_until
from test_fill_runtime_safety import FakeActuator, Feed
from test_mixing_process import RO_L, make, plan_for, run_until, tick, to_dosing_running
from test_nutrient_dosing import _plan, _setup

from domain.recipe.volume_balance import dynamic_phase_target_l
from process.nutrient_dosing import DosingPhase, NutrientDosingController
from statemachine.fill_and_measure_state_machine import FillAndMeasureState as S

PUMPS = ["P1", "P2", "P3", "P4"]
HALF = 10_000  # 50 % von 20 ml (Standardplan: 20 l RO, 20 ml Naehrstoff, Vorfuellziel 10 l)


class WaterFeed(Feed):
    """Feed mit Wasserwerten fuer die Endbewertung."""

    def __init__(self, *args, ec: float = 1.5, ph: float = 6.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.ec, self.ph = ec, ph

    def __call__(self):
        snap = super().__call__()
        snap["water_values"] = {"ec_ms_cm": self.ec, "ph": self.ph}
        return snap


def dyn(plan, confirmed_ul: int, in_flight_ul: int = 0) -> float:
    """RO-Abschaltziel parallel zur Dosierung: RO-Soll + bestaetigt + in Ausfuehrung."""
    return dynamic_phase_target_l(plan.volume, (confirmed_ul + in_flight_ul) / 1000.0)


def resumed(**overrides):
    """Lauf bis zur parallelen RO-Befuellung: 50 % bestaetigt, 2500 uL in Ausfuehrung."""
    sm, feed, pump, mixing, sensor, dosing, clock = make(**overrides)
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul, dosing.in_flight_ul = dosing.plan.total_ul // 2, 2500
    tick(sm, feed, clock)
    assert sm.state == S.DOSING_AND_RO_FILL and pump.is_on and pump.on_calls == 2
    return sm, feed, pump, mixing, sensor, dosing, clock


def advance(sm, feed, clock, pump, until, rate=1.0, limit=300):
    """Wie run_until (Pegel steigt nur bei eingeschalteter RO-Pumpe), liefert aber die besuchten Zustaende."""
    seen: list[S] = []
    for _ in range(limit):
        if not seen or seen[-1] != sm.state:
            seen.append(sm.state)
        if sm.state in (until, S.ERROR):
            break
        tick(sm, feed, clock, mixer=feed.mixer + (rate if pump.is_on else 0.0))
    return seen


def assert_everything_off(pump, mixing, sensor):
    assert not (pump.is_on or mixing.is_on or sensor.is_on)


# --- Schwelle: RO bleibt aus, startet bei Erreichen, nur nach Sicherheitspruefung ---------------------------


def test_ro_stays_off_below_the_threshold_and_resumes_in_parallel_at_it():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    assert pump.is_on is False and pump.on_calls == 1

    for confirmed in (0, 4_000, HALF - 1):  # knapp unter 50 %
        dosing.confirmed_ul = confirmed
        for _ in range(3):
            tick(sm, feed, clock)
        assert sm.state == S.DOSING_RUNNING and pump.is_on is False and pump.on_calls == 1

    dosing.confirmed_ul, dosing.in_flight_ul = HALF, 2_500  # exakt 50 % bestaetigt, Chunks laufen weiter
    tick(sm, feed, clock)
    assert sm.state == S.DOSING_AND_RO_FILL and pump.is_on and pump.on_calls == 2
    assert dosing.start_calls == 1 and dosing.stop_reasons == []  # die Dosierung laeuft weiter, nie neu gestartet
    assert mixing.is_on and sensor.is_on and mixing.off_calls == 0 and sensor.off_calls == 0


def test_resume_needs_a_new_measurement():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul = HALF
    clock.t += 1.0
    sm.update()  # dieselbe MQTT-Nachricht: nicht frisch
    assert sm.state == S.DOSING_RUNNING and pump.is_on is False
    tick(sm, feed, clock)  # neue Nachricht
    assert sm.state == S.DOSING_AND_RO_FILL and pump.on_calls == 2


def test_threshold_one_means_no_overlap_and_the_previous_flow():
    sm, feed, pump, mixing, sensor, dosing, clock = make(nutrient_ro_resume_progress=1.0)
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul = dosing.plan.total_ul  # 100 % bestaetigt, der Dosierer ist aber noch nicht beendet
    for _ in range(5):
        tick(sm, feed, clock)
    assert sm.state == S.DOSING_RUNNING and pump.is_on is False and pump.on_calls == 1

    dosing.finish()
    seen = advance(sm, feed, clock, pump, until=S.POST_MIX)
    assert S.DOSING_AND_RO_FILL not in seen and S.START_REFILL_PUMP in seen
    assert sm.state == S.POST_MIX and pump.on_calls == 2


def test_dosing_that_ends_before_the_threshold_uses_the_previous_flow():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul = 4_000
    tick(sm, feed, clock)
    dosing.finish()  # zwischen zwei Takten von 40 % auf 100 %: die Schwelle wurde nie gesehen
    seen = advance(sm, feed, clock, pump, until=S.POST_MIX)
    assert S.DOSING_AND_RO_FILL not in seen and sm.state == S.POST_MIX and pump.on_calls == 2


@pytest.mark.parametrize("bad", [0, 0.0, -0.1, 1.01, 2, "0.5", True, None, float("nan"), float("inf")])
def test_invalid_resume_progress_blocks_the_start_before_any_output(bad):
    sm, feed, pump, mixing, sensor, dosing, clock = make(nutrient_ro_resume_progress=bad)
    sm.start()
    assert sm.state == S.ERROR and sm.stop_reason == "resume_progress_invalid"
    assert "nutrient_ro_resume_progress" in sm.error_message
    assert pump.on_calls == 0 and dosing.start_calls == 0
    assert_everything_off(pump, mixing, sensor)


def test_missing_resume_progress_blocks_the_start_before_any_output():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    del sm.settings["nutrient_ro_resume_progress"]
    sm.start()
    assert sm.state == S.ERROR and sm.stop_reason == "resume_progress_invalid"
    assert pump.on_calls == 0 and dosing.start_calls == 0


@pytest.mark.parametrize("bad", [0, -0.1, None, "0.1", float("nan"), float("inf"), True])
def test_invalid_ec_tolerance_blocks_a_dosing_run_before_any_output(bad):
    sm, feed, pump, mixing, sensor, dosing, clock = make(ec_tolerance_ms_cm=bad)
    sm.start()
    assert sm.state == S.ERROR and sm.stop_reason == "tolerance_invalid"
    assert "ec_tolerance_ms_cm" in sm.error_message
    assert pump.on_calls == 0 and dosing.start_calls == 0
    assert_everything_off(pump, mixing, sensor)


def test_missing_ec_tolerance_blocks_a_dosing_run_before_any_output():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    del sm.settings["ec_tolerance_ms_cm"]
    sm.start()
    assert sm.state == S.ERROR and sm.stop_reason == "tolerance_invalid"
    assert "ec_tolerance_ms_cm" in sm.error_message and "fehlt" in sm.error_message
    assert pump.on_calls == 0 and dosing.start_calls == 0
    assert_everything_off(pump, mixing, sensor)


def test_invalid_ec_tolerance_does_not_block_a_run_without_dosing_work():
    sm, feed, pump, mixing, sensor, dosing, clock = make(SimpleNamespace(has_dosing_work=False), ec_tolerance_ms_cm=0)
    sm.start()
    assert sm.state == S.START_REFILL_PUMP and sm.dosing_enabled is False


def test_missing_ec_tolerance_does_not_block_a_run_without_dosing_work():
    sm, feed, pump, mixing, sensor, dosing, clock = make(SimpleNamespace(has_dosing_work=False))
    del sm.settings["ec_tolerance_ms_cm"]
    sm.start()
    assert sm.state == S.START_REFILL_PUMP and sm.dosing_enabled is False


def test_resume_is_refused_with_uncertain_nutrient():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul, dosing.uncertain_ul = HALF, 1_000  # unsichere Menge sperrt jede Wiederaufnahme
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "ro_resume_blocked"
    assert "RO-Wiederaufnahme blockiert" in sm.error_message and "Unsichere" in sm.error_message
    assert pump.on_calls == 1 and pump.is_on is False


def test_resume_is_refused_when_the_level_fell_below_the_phase_start():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul = HALF
    tick(sm, feed, clock, mixer=1.0)  # Phasenbeginn ca. 5 l, erlaubte Drift 2 l
    assert sm.state == S.ERROR and sm.stop_reason == "ro_resume_blocked" and pump.on_calls == 1


def test_resume_is_refused_when_the_ro_water_would_already_be_complete():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul = HALF
    tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF) + 0.5)  # widerspruechlicher Pegel: nie blind weiterfuellen
    assert sm.state == S.ERROR and sm.stop_reason == "ro_resume_blocked" and pump.on_calls == 1


# --- RO-Pumpe: hoechstens zwei Einschaltungen --------------------------------------------------------------


def test_the_ro_pump_can_never_be_switched_on_a_third_time():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    sm._ro_on_count = 2
    assert sm._switch_ro_on() is False
    assert sm.state == S.ERROR and sm.stop_reason == "ro_restart_limit" and pump.on_calls == 0


def test_a_wrong_on_count_refuses_the_resume():
    sm, feed, pump, mixing, sensor, dosing, clock = make()
    to_dosing_running(sm, feed, pump, clock)
    sm._ro_on_count = 2  # waere schon die zweite Einschaltung gewesen
    dosing.confirmed_ul = HALF
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "ro_resume_blocked" and pump.on_calls == 1


def test_emergency_stop_during_the_resume_switch_on_leaves_everything_off():
    pump = FakeActuator("mixer_refill_pump")
    sm, feed, pump, mixing, sensor, dosing, clock = make(pump=pump)
    to_dosing_running(sm, feed, pump, clock)
    pump.on_hook = lambda: sm.error("Emergency stop requested from NiceGUI.")  # feuert beim zweiten on()
    dosing.confirmed_ul = HALF
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and "Emergency" in sm.error_message
    assert_everything_off(pump, mixing, sensor)
    assert sm._ro_resumed is False and dosing.stop_reasons  # Dosierstopp wurde angefordert
    for _ in range(3):
        tick(sm, feed, clock)
    assert pump.on_calls == 2 and pump.is_on is False


# --- dynamisches Abschaltziel -----------------------------------------------------------------------------------


def test_ro_stops_in_the_same_update_at_confirmed_plus_in_flight_and_never_restarts():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    plan = dosing.plan
    target = dyn(plan, HALF, 2_500)
    assert target > dyn(plan, HALF) > RO_L  # mit Mengen in Ausfuehrung hoeher als mit bestaetigter Menge allein

    tick(sm, feed, clock, mixer=dyn(plan, HALF))  # das Ziel ohne in-flight-Menge wuerde zu frueh abschalten
    assert pump.is_on and not sm._ro_overlap_done
    tick(sm, feed, clock, mixer=target - 0.001)
    assert pump.is_on and not sm._ro_overlap_done
    tick(sm, feed, clock, mixer=target)
    assert pump.is_on is False and sm._ro_overlap_done  # Pumpe zuerst aus, im selben Update
    assert sm.state == S.DOSING_AND_RO_FILL and sm.stop_reason == "ro_target_reached_pump_off"
    for _ in range(6):  # die Dosierung laeuft weiter, der Pegel aendert sich kaum: RO bleibt aus
        tick(sm, feed, clock, mixer=target)
    assert pump.on_calls == 2 and sm.state == S.DOSING_AND_RO_FILL
    assert dosing.stop_reasons == [] and mixing.is_on and sensor.is_on


def test_the_target_rises_with_every_confirmed_chunk_while_ro_is_still_filling():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    plan = dosing.plan
    level = dyn(plan, HALF, 2_500) + 0.0005  # waere bei 12,5 ml bereits erreicht
    dosing.confirmed_ul, dosing.in_flight_ul = HALF + 5_000, 5_000  # inzwischen 15 ml bestaetigt, 5 ml in Ausfuehrung
    assert dyn(plan, HALF + 5_000, 5_000) > level
    tick(sm, feed, clock, mixer=level)
    assert pump.is_on and not sm._ro_overlap_done  # das Ziel ist mit der Dosierung mitgewandert


def test_early_ro_shutoff_never_leads_to_post_mix_when_the_final_level_is_not_reached():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    plan = dosing.plan
    tick(sm, feed, clock, mixer=dyn(plan, HALF, 2_500))  # RO erreicht ihr Zwischenziel, Chunks stehen noch aus
    assert sm._ro_overlap_done and pump.is_on is False

    for confirmed in (12_000, 15_000, 19_000):  # Dosierung laeuft weiter: kein Nachmischen, kein Abschluss
        dosing.confirmed_ul, dosing.in_flight_ul = confirmed, 0
        for _ in range(3):
            tick(sm, feed, clock)
        assert sm.state == S.DOSING_AND_RO_FILL

    dosing.finish()  # alles bestaetigt - aber der Pegel hat RO-Soll + gesamte Menge nie erreicht
    tick(sm, feed, clock)
    assert sm.state == S.CONFIRM_TARGET and sm._final_confirmed_ul == plan.total_ul
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "target_not_confirmed"
    assert pump.is_on is False and pump.on_calls == 2  # kein Nachfuellen, kein Nachmischen
    assert S.POST_MIX != sm.state and sm.final_evaluation is None


def test_ro_finished_first_then_complete_dosing_and_final_target_leads_to_post_mix():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    plan = dosing.plan
    tick(sm, feed, clock, mixer=dyn(plan, HALF, 2_500))
    assert sm._ro_overlap_done

    dosing.finish()
    final_target = dynamic_phase_target_l(plan.volume, plan.total_ml)
    feed.mixer = final_target + 0.001  # die restlichen Naehrstoffe haben den Pegel angehoben
    seen = advance(sm, feed, clock, pump, until=S.POST_MIX)
    assert sm.state == S.POST_MIX
    assert S.START_REFILL_PUMP not in seen and S.FILL_UNTIL_TARGET not in seen  # RO war schon fertig
    assert pump.on_calls == 2 and pump.is_on is False and mixing.is_on and sensor.is_on
    for _ in range(60):
        tick(sm, feed, clock)
        if sm.is_done:
            break
    assert sm.state == S.FINISHED and sm.error_message is None
    assert dosing.start_calls == 1 and pump.on_calls == 2  # ein Dosierstart, genau eine Wiederaufnahme


def test_dosing_finished_first_while_ro_is_still_filling_continues_to_the_final_target():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    dosing.finish()
    tick(sm, feed, clock, mixer=feed.mixer + 0.5)
    assert sm.state == S.FILL_UNTIL_TARGET and pump.is_on and pump.on_calls == 2  # Pumpe bleibt an, kein drittes on()
    assert sm._final_confirmed_ul == dosing.plan.total_ul

    seen = advance(sm, feed, clock, pump, until=S.POST_MIX)
    assert sm.state == S.POST_MIX and pump.on_calls == 2 and pump.is_on is False
    assert S.START_REFILL_PUMP not in seen and seen.index(S.CONFIRM_TARGET) < seen.index(S.POST_MIX)
    assert feed.mixer >= dynamic_phase_target_l(dosing.plan.volume, dosing.plan.total_ml)


def test_dosing_and_ro_finishing_in_the_same_update_waits_for_the_final_confirmation():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    dosing.finish()
    tick(sm, feed, clock, mixer=dynamic_phase_target_l(dosing.plan.volume, dosing.plan.total_ml) + 0.2)
    assert sm.state == S.FILL_UNTIL_TARGET and pump.is_on  # Dosierung fertig, RO laeuft noch -> Fuellziel pruefen
    tick(sm, feed, clock)
    assert pump.is_on is False and sm.state == S.CONFIRM_TARGET  # Pumpe zuerst aus, dann bestaetigen
    assert sm.state != S.POST_MIX


@pytest.mark.parametrize("ro_done", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {"confirmed_ul": 19_000, "uncertain_ul": 1_000},  # eine Menge ist unklar (Summe bleibt konsistent)
        {"confirmed_ul": 19_000, "in_flight_ul": 1_000},  # eine Menge ist noch in Ausfuehrung
        {"confirmed_ul": 19_000},  # FINISHED, aber nicht die gesamte Menge bestaetigt
    ],
)
def test_finished_dosing_with_open_or_uncertain_amounts_never_reaches_post_mix(ro_done, change):
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    if ro_done:
        tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
        assert sm._ro_overlap_done
    dosing.finish()
    for name, value in change.items():
        setattr(dosing, name, value)
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "dosing_incomplete"
    assert pump.is_on is False and pump.on_calls == 2


def test_post_mix_is_unreachable_while_dosing_is_still_running_even_with_a_full_tank():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    for _ in range(40):  # Pegel weit ueber dem Endziel, Dosierung aber nicht beendet
        tick(sm, feed, clock, mixer=dynamic_phase_target_l(dosing.plan.volume, dosing.plan.total_ml) + 0.3)
        assert sm.state == S.DOSING_AND_RO_FILL
    assert pump.on_calls == 2 and pump.is_on is False


# --- Fehler in einem der beiden Teilprozesse ------------------------------------------------------------------------


@pytest.mark.parametrize("ro_done", [False, True])
@pytest.mark.parametrize(
    "phase,reason,uncertain",
    [
        (DosingPhase.ERROR, "timeout", 2_500),
        (DosingPhase.ABORTED, "user_abort", 2_500),
        (DosingPhase.ERROR, "connection_error", 0),
        (DosingPhase.FINISHED, "error", 0),
    ],
)
def test_dosing_failure_during_the_overlap_stops_everything_without_redosing(ro_done, phase, reason, uncertain):
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    if ro_done:
        tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    dosing.phase, dosing.completion_reason, dosing.uncertain_ul, dosing.in_flight_ul = phase, reason, uncertain, 0
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "dosing_failed"
    assert_everything_off(pump, mixing, sensor)
    for _ in range(5):
        tick(sm, feed, clock)
    assert pump.on_calls == 2 and dosing.start_calls == 1 and sm.state == S.ERROR


def test_dosing_that_reports_stopping_switches_ro_off_before_reading_any_sensor():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    reads_before = feed.reads
    dosing.phase = DosingPhase.STOPPING  # der Dosierer bricht ab (z. B. Timeout) und meldet das sofort
    clock.t += 1.0
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "dosing_stopped"
    assert pump.is_on is False and feed.reads == reads_before  # keine Sensorlesung noetig, RO sofort aus


def test_unreadable_dosing_status_is_an_error():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    dosing.get_status = lambda: {"phase": DosingPhase.RUNNING}  # in_flight_ul/confirmed_ul fehlen: nie stilles 0
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "dosing_status_unreadable" and pump.is_on is False


@pytest.mark.parametrize("ro_done", [False, True])
def test_sensor_loss_during_the_overlap_stops_everything_and_the_dosing(ro_done):
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    if ro_done:
        tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    feed.raises = RuntimeError("sensor down")
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "sensor_read_failed"
    assert_everything_off(pump, mixing, sensor)
    assert dosing.stop_reasons, "Dosier-Stopp wurde nicht angefordert"


@pytest.mark.parametrize("ro_done", [False, True])
def test_stale_measurement_during_the_overlap_stops_everything_and_the_dosing(ro_done):
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    if ro_done:
        tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    clock.t += 6.0  # keine neue Messung
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "measurement_stale_during_dosing"
    assert_everything_off(pump, mixing, sensor)
    assert dosing.stop_reasons


@pytest.mark.parametrize("bad_mixer", [None, float("nan"), -1.0, 186.0, "12"])
def test_implausible_mixer_value_during_the_overlap_stops_everything(bad_mixer):
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    clock.t += 1.0
    feed.advance(mixer=bad_mixer)  # (tick() liest mixer=None als "unveraendert", hier soll der Wert fehlen)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "mixer_implausible_during_fill"
    assert_everything_off(pump, mixing, sensor)
    assert dosing.stop_reasons


def test_missing_ro_supply_value_during_the_overlap_stops_everything():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    feed.ro = None
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "ro_invalid_during_fill"
    assert_everything_off(pump, mixing, sensor)


def test_ro_supply_running_low_during_the_overlap_stops_everything():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    feed.ro = 12.0  # Rest bis zum Ziel + Reserve (20 l) nicht mehr gedeckt
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "ro_reserve_violated_during_fill"
    assert_everything_off(pump, mixing, sensor)


def test_a_stuck_ro_pump_is_not_masked_by_the_nutrient_volume():
    """Der Fortschrittswaechter rechnet das seit der Wiederaufnahme bestaetigte Naehrstoffvolumen heraus."""
    sm, feed, pump, mixing, sensor, dosing, clock = resumed(
        min_fill_progress_liters=0.005, no_fill_progress_timeout_seconds=20.0
    )
    dosing.in_flight_ul = 0
    ticks = 0
    for ticks in range(1, 23):  # 22 Takte: bis 19,9 ml bestaetigt, also noch unter der Planmenge
        dosing.confirmed_ul = HALF + 450 * ticks  # Naehrstoff steigt, die RO-Pumpe foerdert aber nichts
        tick(sm, feed, clock, mixer=feed.mixer + 0.00045)
        if sm.state == S.ERROR:
            break
    assert sm.state == S.ERROR and sm.stop_reason == "no_fill_progress_timeout"
    assert ticks >= 20  # erst nach der Wartezeit, nicht sofort
    assert_everything_off(pump, mixing, sensor)


def test_a_working_ro_pump_passes_the_progress_watchdog_during_the_overlap():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed(
        min_fill_progress_liters=0.005, no_fill_progress_timeout_seconds=20.0
    )
    dosing.in_flight_ul = 0
    for tick_no in range(1, 40):
        dosing.confirmed_ul = HALF + 200 * tick_no
        tick(sm, feed, clock, mixer=feed.mixer + 0.02)  # RO foerdert 20 ml je Takt
    assert sm.state == S.DOSING_AND_RO_FILL and pump.is_on and sm.error_message is None


# --- Volumenbilanz und 185 l ----------------------------------------------------------------------------------------


def _big_plan_run():
    """180 l RO (Maximum) + 180 ml Naehrstoff, Wiederaufnahme bei 55 ml bestaetigt: 125 ml (= 0,125 l, exakt als
    Binaerzahl darstellbar) sind dann noch nicht bestaetigt. Schwelle 0,25, damit 55 ml (30,6 %) genuegen."""
    plan = _plan(ro_l=180.0, ml_per_100_l=100.0, max_chunk_ml=10.0, prefill_target_l=30.0)
    assert plan.total_ul == 180_000
    sm, feed, pump, mixing, sensor, dosing, clock = make(plan, ro_l=180.0, nutrient_ro_resume_progress=0.25)
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul, dosing.in_flight_ul = 55_000, 0
    tick(sm, feed, clock)
    assert sm.state == S.DOSING_AND_RO_FILL
    return sm, feed, pump, mixing, sensor, dosing, clock, plan


def test_capacity_check_counts_the_unconfirmed_nutrient_against_185_l():
    sm, feed, pump, mixing, sensor, dosing, clock, plan = _big_plan_run()
    # 184.9 l + 0.125 l noch nicht bestaetigt = 185.025 l > 185 l -> der Lauf scheitert kontrolliert.
    tick(sm, feed, clock, mixer=184.9)
    assert sm.state == S.ERROR and sm.stop_reason == "volume_check_failed"
    assert "Volumenbilanz" in sm.error_message
    assert_everything_off(pump, mixing, sensor)


def test_capacity_check_accepts_exactly_185_l_and_the_ro_pump_then_stops():
    sm, feed, pump, mixing, sensor, dosing, clock, plan = _big_plan_run()
    # 184.875 l + 0.125 l = 185.000 l: genau auf der Grenze erlaubt (beide Werte sind exakte Binaerzahlen).
    # Das RO-Abschaltziel (180.055 l) ist laengst erreicht: die Pumpe stoppt, es gibt keinen Fehler.
    tick(sm, feed, clock, mixer=184.875)
    assert sm.state == S.DOSING_AND_RO_FILL and sm.error_message is None
    assert pump.is_on is False and sm._ro_overlap_done


def test_mixer_above_the_process_limit_is_an_error_even_without_open_nutrient():
    sm, feed, pump, mixing, sensor, dosing, clock, plan = _big_plan_run()
    tick(sm, feed, clock, mixer=185.2)
    assert sm.state == S.ERROR and sm.stop_reason == "mixer_implausible_during_fill"
    assert_everything_off(pump, mixing, sensor)


def test_accounting_that_exceeds_the_plan_is_an_error():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    dosing.confirmed_ul, dosing.in_flight_ul = 18_000, 5_000  # 23 ml von 20 ml geplant: Doppelbuchung
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "volume_check_failed" and pump.is_on is False


# --- Emergency Stop in jeder Teilphase -----------------------------------------------------------------------------


@pytest.mark.parametrize("ro_done", [False, True])
def test_emergency_stop_during_the_overlap_switches_everything_off_and_stops_the_dosing(ro_done):
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    if ro_done:
        tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    sm.error("Emergency stop requested from NiceGUI.")
    assert sm.state == S.ERROR
    assert_everything_off(pump, mixing, sensor)
    assert dosing.stop_reasons == ["process_error"]
    for _ in range(4):
        tick(sm, feed, clock)
    assert pump.on_calls == 2 and sm.state == S.ERROR and dosing.start_calls == 1


def test_circulations_stay_on_for_the_whole_overlap():
    sm, feed, pump, mixing, sensor, dosing, clock = resumed()
    tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    dosing.finish()
    feed.mixer = dynamic_phase_target_l(dosing.plan.volume, dosing.plan.total_ml) + 0.001
    advance(sm, feed, clock, pump, until=S.POST_MIX)
    assert sm.state == S.POST_MIX
    assert mixing.is_on and sensor.is_on and mixing.off_calls == 0 and sensor.off_calls == 0


# --- EC-Abschluss im Gesamtablauf: bewertet, aber nie nachgeregelt --------------------------------------------------


def _ec_run(ec: float):
    feed = WaterFeed(mixer=0.0, ec=ec, ph=6.0)
    sm, feed, pump, mixing, sensor, dosing, clock = make(
        feed=feed, ec_tolerance_ms_cm=0.1, ph_min=5.0, ph_max=8.9
    )
    snapshot = sm.settings["recipe_run_config_snapshot"]
    snapshot["nutrient_dosing_enabled"] = True
    snapshot["target_ec_ms_cm"] = 1.5
    to_dosing_running(sm, feed, pump, clock)
    dosing.confirmed_ul, dosing.in_flight_ul = HALF, 2_500
    tick(sm, feed, clock)
    assert sm.state == S.DOSING_AND_RO_FILL
    tick(sm, feed, clock, mixer=dyn(dosing.plan, HALF, 2_500))
    dosing.finish()
    feed.mixer = dynamic_phase_target_l(dosing.plan.volume, dosing.plan.total_ml) + 0.001
    advance(sm, feed, clock, pump, until=S.POST_MIX)
    assert sm.state == S.POST_MIX
    for _ in range(80):
        tick(sm, feed, clock)
        if sm.is_done:
            break
    return sm, feed, pump, mixing, sensor, dosing, clock


@pytest.mark.parametrize("ec", [1.4, 1.5, 1.55, 1.6])
def test_ec_inside_the_tolerance_finishes_with_ok(ec):
    sm, feed, pump, mixing, sensor, dosing, clock = _ec_run(ec)
    assert sm.state == S.FINISHED and sm.error_message is None
    assert sm.final_evaluation["EC"]["status"] == "ok" and sm.final_evaluation_status == "OK"


@pytest.mark.parametrize("ec", [1.399, 1.601, 2.4, 0.02])
def test_ec_outside_the_tolerance_is_a_controlled_error_without_any_correction(ec):
    sm, feed, pump, mixing, sensor, dosing, clock = _ec_run(ec)
    assert sm.state == S.ERROR and sm.stop_reason == "ec_out_of_tolerance"
    assert sm.final_evaluation_status == "FAILED"
    assert_everything_off(pump, mixing, sensor)
    # Keine automatische Nachregelung: weder neue Dosierung noch zusaetzliches RO-Wasser, auch nicht spaeter.
    for _ in range(20):
        tick(sm, feed, clock, mixer=feed.mixer)
        sm.update()
    assert dosing.start_calls == 1 and pump.on_calls == 2 and sm.state == S.ERROR
    assert_everything_off(pump, mixing, sensor)


@pytest.mark.parametrize("water_values", [{"ph": 6.0}, {"ec_ms_cm": None, "ph": 6.0}, {"ec_ms_cm": float("nan"), "ph": 6.0}])
def test_missing_ec_value_in_a_dosing_run_is_an_error(water_values):
    sm, feed, pump, mixing, sensor, dosing, clock = make(feed=WaterFeed(mixer=0.0), ec_tolerance_ms_cm=0.1)
    snapshot = sm.settings["recipe_run_config_snapshot"]
    snapshot["nutrient_dosing_enabled"] = True
    snapshot["target_ec_ms_cm"] = 1.5
    sm.get_sensor_snapshot = _with_water_values(feed, water_values)
    to_dosing_running(sm, feed, pump, clock)
    dosing.finish()
    advance(sm, feed, clock, pump, until=S.POST_MIX)
    for _ in range(80):
        tick(sm, feed, clock)
        if sm.is_done:
            break
    assert sm.state == S.ERROR and sm.stop_reason == "measurement_implausible"


def _with_water_values(feed, water_values):
    def snapshot():
        snap = Feed.__call__(feed)
        snap["water_values"] = dict(water_values)
        return snap

    return snapshot


def test_water_run_with_dosing_disabled_is_not_failed_by_the_ec_check_in_the_whole_flow():
    feed = WaterFeed(mixer=0.0, ec=0.02, ph=6.0)  # RO-Wasser
    sm, feed, pump, mixing, sensor, dosing, clock = make(
        SimpleNamespace(has_dosing_work=False), feed=feed, ec_tolerance_ms_cm=0.1, ph_min=5.0, ph_max=8.9
    )
    snapshot = sm.settings["recipe_run_config_snapshot"]
    snapshot["nutrient_dosing_enabled"] = False
    snapshot["target_ec_ms_cm"] = 1.5
    sm.start()
    for _ in range(400):
        tick(sm, feed, clock, mixer=feed.mixer + (1.0 if pump.is_on else 0.0))
        if sm.is_done:
            break
    assert sm.state == S.FINISHED and sm.error_message is None
    assert sm.final_evaluation["EC"] == {"status": "NOT_EVALUATED", "reason": "no_nutrient_dosing"}
    assert sm.final_evaluation["pH"]["status"] == "ok"
    assert sm.final_evaluation_status == "NOT_EVALUATED"  # keine bestaetigte Sollwerterfuellung (Transfer nur mit Bestaetigung)


# --- echter Dosier-Controller mit gesteuertem MCU: tatsaechliche Ueberlappung -----------------------------------------


@pytest.fixture
def real_overlap(tmp_path, monkeypatch):
    """State Machine + echter NutrientDosingController ueber ManualCompletionMcu, im Zustand DOSING_AND_RO_FILL:
    Runde 1 ist bestaetigt (50 %), Runde 2 laeuft (alle vier Pumpen BUSY) und die RO-Pumpe ist EIN."""
    _setup(monkeypatch, tmp_path)
    mcu = ManualCompletionMcu()
    port = FakeSerialPort(mcu)
    mcu.attach(port)
    dosing = NutrientDosingController(
        get_sensor_snapshot=lambda: {"ok": True}, serial_factory=make_client_factory(port),
        session_log_dir=tmp_path / "logs",
    )
    plan = plan_for(RO_L)
    sm, feed, pump, mixing, sensor, _fake, clock = make(plan)
    sm.dosing = dosing
    sm.settings["hardware_execution_enabled"] = True
    env = SimpleNamespace(sm=sm, feed=feed, pump=pump, mixing=mixing, sensor=sensor, clock=clock, dosing=dosing,
                          mcu=mcu, port=port, plan=plan)
    try:
        sm.start()
        run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
        assert wait_until(lambda: len(mcu.started()) == 4)  # Runde 1: alle vier laufen
        for _ in range(3):
            tick(sm, feed, clock)
        assert pump.is_on is False and sm.state == S.DOSING_RUNNING  # unter der Schwelle: RO aus
        for name in PUMPS:
            mcu.finish(name)
        assert wait_until(lambda: len(mcu.started()) == 8)  # Runde 2 beginnt erst nach der ganzen Runde 1
        for _ in range(20):
            tick(sm, feed, clock)
            if sm.state == S.DOSING_AND_RO_FILL:
                break
            time.sleep(0.01)
        assert sm.state == S.DOSING_AND_RO_FILL, sm.error_message
        yield env
    finally:
        dosing.request_stop("test_cleanup")
        dosing.wait_stopped(5.0)


def test_ro_runs_while_all_four_mcu_pumps_are_busy(real_overlap):
    e = real_overlap
    # Der Nachweis der Ueberlappung: RO-Pumpe EIN, gleichzeitig laufen alle vier MCU_B-Pumpen.
    assert e.pump.is_on and e.pump.on_calls == 2
    assert e.mcu.busy_pumps() == PUMPS and e.mcu.max_busy == 4
    status = e.dosing.get_status()
    assert status["phase"] == DosingPhase.RUNNING and status["confirmed_ul"] == HALF and status["in_flight_ul"] == HALF

    # RO erreicht ihr dynamisches Ziel (RO-Soll + 10 ml bestaetigt + 10 ml in Ausfuehrung) und schaltet ab,
    # waehrend die Dosierung weiterlaeuft.
    target = dyn(e.plan, HALF, HALF)
    while e.pump.is_on and e.feed.mixer < target + 5:
        tick(e.sm, e.feed, e.clock, mixer=e.feed.mixer + 1.0)
    assert e.pump.is_on is False and e.sm._ro_overlap_done
    assert e.mcu.busy_pumps() == PUMPS and e.sm.state == S.DOSING_AND_RO_FILL  # Dosierung laeuft weiter
    for _ in range(5):
        tick(e.sm, e.feed, e.clock)
    assert e.sm.state == S.DOSING_AND_RO_FILL and e.pump.on_calls == 2

    # Letzte Runde fertig: erst jetzt (Dosierung vollstaendig + Endziel bestaetigt) darf nachgemischt werden.
    for name in PUMPS:
        e.mcu.finish(name)
    assert wait_until(lambda: e.dosing.get_status()["phase"] == DosingPhase.FINISHED)
    e.feed.mixer += 0.020  # die Naehrstoffe haben den Pegel angehoben
    seen = advance(e.sm, e.feed, e.clock, e.pump, until=S.POST_MIX)
    assert e.sm.state == S.POST_MIX, e.sm.error_message
    assert S.START_REFILL_PUMP not in seen and e.pump.on_calls == 2
    status = e.dosing.get_status()
    assert status["confirmed_ul"] == e.plan.total_ul and status["uncertain_ul"] == 0 and status["in_flight_ul"] == 0


def test_emergency_stop_during_real_overlap_stops_ro_and_all_four_mcu_pumps(real_overlap):
    e = real_overlap
    assert e.pump.is_on and e.mcu.busy_pumps() == PUMPS
    e.sm.error("Emergency stop requested from NiceGUI.")
    assert e.pump.is_on is False and not e.mixing.is_on and not e.sensor.is_on  # sofort aus
    assert wait_until(lambda: not e.dosing.is_active(), 5.0)
    assert not any(e.mcu.busy.values())  # STOPALL hat alle vier gestoppt
    status = e.dosing.get_status()
    assert status["phase"] == DosingPhase.ABORTED and status["in_flight_ul"] == 0
    assert status["confirmed_ul"] == HALF and status["uncertain_ul"] == HALF  # Runde 2 ist unsicher, nie wiederholt
    assert len(e.mcu.started()) == 8 and e.pump.on_calls == 2


def test_pump_failure_during_real_overlap_switches_ro_off_within_one_update(real_overlap):
    e = real_overlap
    e.mcu.fail("P3")  # ERR P3 TIMEOUT waehrend RO laeuft
    for _ in range(40):
        tick(e.sm, e.feed, e.clock, mixer=e.feed.mixer)
        if e.sm.state == S.ERROR:
            break
        time.sleep(0.01)
    assert e.sm.state == S.ERROR and e.sm.stop_reason in ("dosing_stopped", "dosing_failed")
    assert e.pump.is_on is False and not e.mixing.is_on and not e.sensor.is_on
    assert wait_until(lambda: not e.dosing.is_active(), 5.0)
    assert not any(e.mcu.busy.values())
    status = e.dosing.get_status()
    assert status["phase"] == DosingPhase.ERROR and status["confirmed_ul"] == HALF
    assert status["uncertain_ul"] == HALF and status["in_flight_ul"] == 0
    assert len(e.mcu.started()) == 8 and e.pump.on_calls == 2  # keine Wiederholung, kein neuer Dosierstart
