"""AP-05: Vorfuellziel wird VOR dem Einschalten der Zirkulation bestaetigt und danach nicht erneut als identischer Pegel
verlangt (statemachine/fill_and_measure_state_machine.py, DOSING_START), hardwarefrei.

Die Zahlen (50 L RO, Vorfuellziel 25 L, Anzeige danach 16,31 L) sind Testeingaben nach einem realen Wassertest, KEINE
bestaetigten Messwerte. Der Sensorkreis wird mit dem vorhandenen ``on_hook`` des Test-Aktors nachgebildet: sobald die
Sensorzirkulationspumpe einschaltet, sinkt der angezeigte Pegel. Die Untergrenze ist die Sensorzuverlaessigkeitsgrenze
(10 L), KEIN Trockenlaufschutz; die Zirkulationspumpen laufen schon waehrend der Stabilisierungszeit ohne Pegelpruefung.
"""
from __future__ import annotations

import pytest
from test_mixing_process import make, run_until, tick
from test_nutrient_dosing import _plan

from domain.recipe.limits import MIXER_SENSOR_RELIABLE_MIN_LITERS as FLOOR
from statemachine.fill_and_measure_state_machine import FillAndMeasureState as S

RO = 50.0
PREFILL = 25.0
DROP = 8.69


def scenario(drop=DROP, **overrides):
    plan = _plan(ro_l=RO, prefill_target_l=PREFILL)
    sm, feed, pump, mixing, sensor, dosing, clock = make(plan, ro_l=RO, **overrides)
    sensor.on_hook = lambda: setattr(feed, "mixer", feed.mixer - drop)
    return sm, feed, pump, mixing, sensor, dosing, clock


def states_until(sm, feed, clock, pump, stop_states, limit=300):
    seen = []
    for _ in range(limit):
        if not seen or seen[-1] != sm.state:
            seen.append(sm.state)
        if sm.state in stop_states or sm.state == S.ERROR:
            break
        tick(sm, feed, clock, mixer=feed.mixer + (1.0 if pump.is_on else 0.0))
    return seen


# --- 1. Vorfuellung erreicht und bestaetigt -> Uebergang zur Zirkulation ---------------------------------


def test_prefill_is_confirmed_with_ro_off_before_the_circulation_starts():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario(drop=0.0)
    sm.start()
    seen = states_until(sm, feed, clock, pump, {S.DOSING_RUNNING})
    assert seen.index(S.CONFIRM_TARGET) < seen.index(S.START_CIRCULATION) < seen.index(S.DOSING_START)
    assert sm.state == S.DOSING_RUNNING and pump.is_on is False and pump.on_calls == 1


# --- 2. Pegel sinkt nach dem Zirkulationsstart plausibel -> Dosierstart scheitert nicht am frueheren Ziel ---


def test_plausible_level_drop_after_circulation_start_does_not_block_the_dosing_start():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario()
    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    assert sm.state == S.DOSING_RUNNING and sm.error_message is None and dosing.start_calls == 1
    assert feed.mixer == pytest.approx(PREFILL - DROP)  # 16,31 L: unter dem Vorfuellziel, trotzdem freigegeben
    assert mixing.is_on and sensor.is_on and pump.is_on is False and pump.on_calls == 1


# --- 3. Vorfuellziel nicht erreicht -> Prozess bleibt blockiert -------------------------------------------


def test_prefill_not_reached_keeps_the_process_blocked_without_any_circulation():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario()
    sm.start()
    for _ in range(320):  # RO laeuft, der Pegel steigt nie
        tick(sm, feed, clock, mixer=0.0)
        if sm.state == S.ERROR:
            break
    assert sm.state == S.ERROR and sm.stop_reason == "no_fill_progress_timeout"
    assert sensor.on_calls == 0 and mixing.on_calls == 0 and dosing.start_calls == 0


def test_prefill_not_confirmed_with_ro_off_is_an_error_before_any_circulation():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario()
    sm.start()
    run_until(sm, feed, clock, S.CONFIRM_TARGET, pump)
    tick(sm, feed, clock, mixer=PREFILL - 0.5)
    assert sm.state == S.ERROR and sm.stop_reason == "target_not_confirmed"
    assert sensor.on_calls == 0 and mixing.on_calls == 0 and dosing.start_calls == 0


# --- 4. fehlende, veraltete, unplausible Messung am Dosierstart -> kontrollierter Fehler --------------------


@pytest.mark.parametrize("failure", ["stale", "raises", "negative", "nan", "too_high"])
def test_missing_stale_or_implausible_measurement_at_the_dosing_start_is_a_controlled_error(failure):
    sm, feed, pump, mixing, sensor, dosing, clock = scenario()
    sm.start()
    run_until(sm, feed, clock, S.DOSING_START, pump)
    assert sm.state == S.DOSING_START and sensor.is_on
    if failure == "stale":
        clock.t += 6.0
        sm.update()
    elif failure == "raises":
        feed.raises = RuntimeError("Sensor weg")
        tick(sm, feed, clock)
    else:
        tick(sm, feed, clock, mixer={"negative": -1.0, "nan": float("nan"), "too_high": 250.0}[failure])
    assert sm.state == S.ERROR and dosing.start_calls == 0
    assert not (pump.is_on or mixing.is_on or sensor.is_on)


# --- 5. gefaehrlicher Restfuellstand wird nicht freigegeben (vorgeschlagene Absicherung) ----------------------


def test_level_just_below_the_existing_reliable_range_limit_is_not_released():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario(drop=PREFILL - (FLOOR - 0.01))
    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    assert sm.state == S.ERROR and sm.stop_reason == "level_below_sensor_reliable_range"
    assert dosing.start_calls == 0 and not (pump.is_on or mixing.is_on or sensor.is_on)


def test_level_exactly_at_the_existing_reliable_range_limit_is_released():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario(drop=PREFILL - FLOOR)
    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    assert sm.state == S.DOSING_RUNNING and feed.mixer == FLOOR and dosing.start_calls == 1


def test_dosing_start_without_a_confirmed_prefill_target_is_refused():
    sm, feed, pump, mixing, sensor, dosing, clock = scenario()
    sm.start()
    run_until(sm, feed, clock, S.DOSING_START, pump)
    sm.target_confirm_count = 0  # Bestaetigung "verloren" (Zustandsmaschine manipuliert)
    tick(sm, feed, clock)
    assert sm.state == S.ERROR and sm.stop_reason == "prefill_not_confirmed" and dosing.start_calls == 0


# --- 6. Wasserlauf ohne Naehrstoffdosierung bleibt unveraendert ---------------------------------------------


def test_water_run_without_dosing_work_is_unchanged():
    from types import SimpleNamespace

    sm, feed, pump, mixing, sensor, dosing, clock = make(SimpleNamespace(has_dosing_work=False))
    sm.start()
    run_until(sm, feed, clock, S.MEASURE_VALUES, pump)
    assert dosing.start_calls == 0 and pump.on_calls == 1


# --- Demonstration der Volumenfrage (Modell, KEINE Messung) ---------------------------------------------------


def test_demonstration_ro_amount_equals_recipe_ro_plus_loop_volume():
    """Die Zielpruefung vergleicht den ANGEZEIGTEN Pegel bei laufender Zirkulation. Das Wasser im Sensorkreis (hier
    nachgebildet durch DROP) steckt in keiner Rechnung: Die RO-Pumpe liefert deshalb RO-Soll + Kreisvolumen."""
    sm, feed, pump, mixing, sensor, dosing, clock = scenario()
    sm.start()
    run_until(sm, feed, clock, S.DOSING_RUNNING, pump)
    dosing.finish()
    nutrient_l = dosing.plan.total_ul / 1_000_000
    rate = 0.1
    for _ in range(900):
        if sm.state in (S.POST_MIX, S.ERROR):
            break
        tick(sm, feed, clock, mixer=feed.mixer + (rate if pump.is_on else 0.0))
    assert sm.state == S.POST_MIX and sm.error_message is None
    indicated = feed.mixer
    total_water = indicated + DROP  # Tankinhalt + Wasser im Sensorkreis
    ro_pumped = total_water - nutrient_l
    assert RO + nutrient_l <= indicated < RO + nutrient_l + 2 * rate
    assert ro_pumped == pytest.approx(RO + DROP, abs=0.25)
    print(f"\nangezeigt {indicated:.3f} L, Gesamtwasser {total_water:.3f} L, RO gefoerdert {ro_pumped:.3f} L (Rezept {RO} L)")
