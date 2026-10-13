"""AP-02C: Pumpensicherheit waehrend der RO-Befuellung (hardwarefrei).

Prueft die State Machine im Rezeptlauf-Modus (recipe_run): vollstaendige
AP-02B-Freigabe unmittelbar vor Pumpe-an auf einer NEUEN Messung, laufende
ungefilterte Mixer-/RO-Pruefung, Pumpe aus beim ersten Zielstand mit
Bestaetigung ohne Nachfuellen, Fehler als endgueltiger Zustand sowie den
MQTT-Empfangszaehler. Fake-Aktoren, Fake-Uhr, Fake-Sensor - keine Hardware.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_start_validation import _settings

import statemachine.fill_and_measure_state_machine as sm_module
from process.fill_and_measure import FillAndMeasureController
from services.mqtt_topic_reader import MqttTopicReader
from services.sensor_stamp import RX_META_KEY, is_newer, read_stamp
from statemachine.fill_and_measure_state_machine import (
    MEASUREMENT_WAIT_TIMEOUT_S,
    FillAndMeasureState as S,
    FillAndMeasureStateMachine,
)


class FakeActuator:
    def __init__(self, name="pump", on_hook=None):
        self.name = name
        self.is_on = False
        self.on_calls = 0
        self.off_calls = 0
        self.on_hook = on_hook

    def on(self):
        self.on_calls += 1
        self.is_on = True
        if self.on_hook:
            self.on_hook()

    def off(self):
        self.off_calls += 1
        self.is_on = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Feed:
    """Scripted sensor: same message (stamp) until advance() is called."""

    def __init__(self, mixer=0.0, ro=500.0, capacity=1300.0):
        self.mixer, self.ro, self.capacity = mixer, ro, capacity
        self.seq = 1
        self.t = datetime(2026, 1, 1, 12, 0, 0)
        self.stamped = True
        self.raises = None
        self.reads = 0

    def advance(self, **values):
        for key, value in values.items():
            setattr(self, key, value)
        self.seq += 1
        self.t += timedelta(seconds=1)

    def __call__(self):
        self.reads += 1
        if self.raises:
            raise self.raises
        snap = {
            "timestamp": self.t.isoformat(timespec="seconds"),
            "mixer": {"volume_liters_calc": self.mixer},
            "ro": {"volume_liters_calc": self.ro, "configured_max_liters": self.capacity},
        }
        if self.stamped:
            snap[RX_META_KEY] = {"seq": self.seq}
        return snap


def make(feed=None, target=30.0, pump=None, valve=None, recipe_run=True, **overrides):
    feed = feed or Feed()
    clock = Clock()
    pump = pump or FakeActuator("mixer_refill_pump")
    settings = _settings(target=target)
    settings.update(
        max_fill_seconds=900.0,
        level_settle_seconds=0.0,
        sensor_stabilize_seconds=0.0,
        target_reached_confirm_samples=3,
        no_fill_progress_timeout_seconds=300.0,
    )
    settings.update(overrides)
    sm = FillAndMeasureStateMachine(
        mixer_refill_pump=pump,
        ro_inlet_valve=valve,
        get_sensor_snapshot=feed,
        settings=settings,
        recipe_run=recipe_run,
        clock=clock,
    )
    return sm, feed, pump, clock


def start_pump(sm, feed):
    """start -> wait on the same message -> NEW message -> pump on."""
    sm.start()
    assert sm.state == S.START_REFILL_PUMP
    sm.update()
    assert sm.state == S.START_REFILL_PUMP  # same message: pump stays off
    feed.advance()
    sm.update()
    assert sm.state == S.FILL_UNTIL_TARGET


# --- Pumpenstart -------------------------------------------------------------


def test_valid_start_waits_for_new_measurement_then_switches_on_once():
    sm, feed, pump, _ = make()
    sm.start()
    sm.update()
    sm.update()
    assert pump.on_calls == 0  # repeated reads of the same message never start it
    feed.advance()
    sm.update()
    assert pump.on_calls == 1 and pump.is_on is True
    assert sm.state == S.FILL_UNTIL_TARGET


def test_no_new_measurement_times_out_before_pump_start():
    sm, feed, pump, clock = make()
    sm.start()
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S - 0.1
    sm.update()
    assert sm.state == S.START_REFILL_PUMP
    clock.t += 0.2
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "no_new_measurement_before_pump_start"
    assert pump.on_calls == 0


def test_same_timestamp_with_new_message_is_not_a_new_measurement_and_wait_stays_bounded():
    sm, feed, pump, clock = make()
    sm.start()
    for _ in range(3):
        feed.seq += 1  # new message, but the bridge timestamp did not move
        sm.update()
    assert pump.on_calls == 0 and sm.state == S.START_REFILL_PUMP
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S
    feed.seq += 1
    sm.update()  # continuous messages must not extend the wait forever
    assert sm.state == S.ERROR and pump.on_calls == 0


def test_older_timestamp_is_not_new():
    stamp = (5, datetime(2026, 1, 1, 12, 0, 5))
    assert is_newer((6, datetime(2026, 1, 1, 12, 0, 6)), stamp) is True
    assert is_newer((6, datetime(2026, 1, 1, 12, 0, 5)), stamp) is False
    assert is_newer((6, datetime(2026, 1, 1, 12, 0, 4)), stamp) is False
    assert is_newer((5, datetime(2026, 1, 1, 12, 0, 6)), stamp) is False
    aware = (9, datetime(2026, 1, 1, 12, 0, 9).astimezone())
    assert is_newer(aware, stamp) is False


@pytest.mark.parametrize("breaker", ["missing_meta", "bad_ts", "no_ts", "bad_seq"])
def test_missing_or_invalid_stamp_blocks(breaker):
    sm, feed, pump, _ = make()
    original = feed.__call__

    def broken():
        snap = original()
        if breaker == "missing_meta":
            snap.pop(RX_META_KEY)
        elif breaker == "bad_ts":
            snap["timestamp"] = "gestern"
        elif breaker == "no_ts":
            snap.pop("timestamp")
        else:
            snap[RX_META_KEY] = {"seq": True}
        return snap

    sm.get_sensor_snapshot = broken
    sm.start()
    assert sm.state == S.ERROR and sm.stop_reason == "measurement_stamp_invalid"
    assert pump.on_calls == 0


def test_stamp_lost_after_start_blocks_pump():
    sm, feed, pump, _ = make()
    sm.start()
    feed.stamped = False
    sm.update()
    assert sm.state == S.ERROR and pump.on_calls == 0


@pytest.mark.parametrize(
    "values",
    [
        {"mixer": 5.0},  # nicht leer (> 0,3 L Veto)
        {"mixer": 0.31},
        {"mixer": -1.0},
        {"mixer": 186.0},
        {"mixer": float("nan")},
        {"mixer": 30.0},  # Ziel bereits erreicht
        {"mixer": 40.0},
        {"ro": float("nan")},
        {"ro": float("inf")},
        {"ro": -1.0},
        {"ro": "500"},
        {"ro": 1301.0},
        {"capacity": 1000.0},
        {"capacity": None},
        {"ro": 49.99},  # Bedarf 30 + Reserve 20
    ],
)
def test_implausible_values_right_before_pump_on_block(values):
    sm, feed, pump, _ = make()
    sm.start()
    feed.advance(**values)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "pump_start_blocked"
    assert pump.on_calls == 0 and sm.error_message


@pytest.mark.parametrize("ro", [50.0, 50.01, 500.0])
def test_ro_exactly_sufficient_and_above_starts(ro):
    sm, feed, pump, _ = make(Feed(ro=ro))
    sm.start()
    feed.advance()
    sm.update()
    assert pump.on_calls == 1


def test_residual_mixer_within_veto_starts_and_lowers_need():
    sm, feed, pump, _ = make(Feed(mixer=0.3, ro=49.7))  # need 29.7 + 20
    sm.start()
    feed.advance()
    sm.update()
    assert pump.on_calls == 1


def test_missing_tank_confirmation_blocks_pump_even_with_good_sensors():
    sm, feed, pump, _ = make()
    sm.settings["recipe_run_config_snapshot"]["tank_emptied_confirmed"] = False
    sm.start()
    feed.advance()
    sm.update()
    assert sm.state == S.ERROR and pump.on_calls == 0
    assert "bestaetigt" in sm.error_message


def test_sensor_exception_right_before_pump_on_is_controlled():
    sm, feed, pump, _ = make()
    sm.start()
    feed.raises = OSError("sensor weg")
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "sensor_read_failed"
    assert pump.on_calls == 0


# --- R4: Delta-Modus / Alt-Pfad bleibt, Rezeptlauf nicht umgehbar ------------


def test_recipe_run_cannot_start_in_delta_mode():
    sm, feed, pump, _ = make(fill_mode="delta", target_add_liters=5.0)
    sm.start()
    feed.advance()
    sm.update()
    assert sm.state == S.ERROR and pump.on_calls == 0


def test_recipe_run_flag_without_run_snapshot_blocks():
    sm, feed, pump, _ = make()
    del sm.settings["recipe_run_config_snapshot"]
    sm.start()
    feed.advance()
    sm.update()
    assert sm.state == S.ERROR and pump.on_calls == 0


def test_present_but_invalid_run_snapshot_activates_strict_mode_without_flag():
    sm, feed, pump, _ = make(recipe_run=False)
    assert sm.recipe_run is True  # snapshot key present -> strict
    sm.settings["recipe_run_config_snapshot"] = "kaputt"
    sm.start()
    feed.advance()
    sm.update()
    assert sm.state == S.ERROR and pump.on_calls == 0


def test_legacy_mode_without_snapshot_keeps_old_behavior():
    pump = FakeActuator()
    settings = {
        "min_ro_liters_required": 20.0, "level_filter_samples": 1, "max_mixer_liters": 200.0,
        "max_fill_seconds": 900.0, "no_fill_progress_timeout_seconds": 30.0,
        "min_fill_progress_liters": 0.5, "max_negative_level_drift_liters": 3.0,
        "target_reached_confirm_samples": 1, "fill_mode": "delta", "target_add_liters": 5.0,
        "level_settle_seconds": 0.0,
    }
    plain = lambda: {"mixer": {"volume_liters_calc": 4.0}, "ro": {"volume_liters_calc": 500.0}}  # noqa: E731
    sm = FillAndMeasureStateMachine(mixer_refill_pump=pump, get_sensor_snapshot=plain, settings=settings)
    assert sm.recipe_run is False
    sm.start()
    sm.update()  # sofort, ohne Stempel/Wartezeit (unveraendert)
    assert pump.on_calls == 1 and sm.state == S.FILL_UNTIL_TARGET


def test_controller_builds_state_machine_in_recipe_run_mode(monkeypatch):
    captured = {}

    class RecordingSM:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here")

    class FakeManager:
        def __init__(self, active_low=None):
            pass

        def add(self, name, gpio_pin):
            return FakeActuator(name)

        def safe_shutdown_all(self):
            return []

        def close_all(self):
            pass

    class Dummy:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", FakeManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", Dummy)
    monkeypatch.setattr("services.process_run_logger.ProcessRunLogger", Dummy)
    monkeypatch.setattr(sm_module, "FillAndMeasureStateMachine", RecordingSM)
    fill = FillAndMeasureController(lambda: None)
    fill._run(_settings())
    assert captured["recipe_run"] is True


# --- Laufende Ueberwachung ---------------------------------------------------


def fill_to(sm, feed, mixer):
    feed.advance(mixer=mixer)
    sm.update()


def test_ro_loss_during_fill_aborts_and_pump_off():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    fill_to(sm, feed, 5.0)
    assert sm.state == S.FILL_UNTIL_TARGET
    feed.advance(ro=40.0)  # need 25 + 20 reserve = 45
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "ro_reserve_violated_during_fill"
    assert pump.is_on is False


def test_ro_consumption_matching_fill_stays_valid_with_margin():
    sm, feed, pump, _ = make(Feed(ro=70.0))
    start_pump(sm, feed)
    feed.advance(mixer=10.0, ro=60.0)  # true need 20 + 20; filtered (lagging) need 25 + 20 = 45
    sm.update()
    assert sm.state == S.FILL_UNTIL_TARGET


def test_exact_fit_ro_supply_aborts_mid_fill_because_need_uses_the_lagging_filtered_value():
    # Consequence of mixer_for_need = min(unfiltered, filtered): a start with
    # exactly need + reserve has no margin for the filter lag. Fail-closed,
    # documented as a residual behavior (no invented tolerance).
    sm, feed, pump, _ = make(Feed(ro=50.0))
    start_pump(sm, feed)
    feed.advance(mixer=10.0, ro=40.0)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "ro_reserve_violated_during_fill"
    assert pump.is_on is False


@pytest.mark.parametrize("ro", [float("nan"), float("inf"), -5.0, None, "x"])
def test_invalid_ro_during_fill_aborts(ro):
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    feed.advance(mixer=2.0, ro=ro)
    sm.update()
    assert sm.state == S.ERROR and pump.is_on is False
    assert sm.stop_reason == "ro_invalid_during_fill"


def test_ro_capacity_inconsistent_during_fill_aborts():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    feed.advance(mixer=2.0, capacity=1000.0)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "ro_invalid_during_fill"


@pytest.mark.parametrize("mixer", [float("nan"), -2.0, None])
def test_invalid_mixer_during_fill_aborts(mixer):
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    feed.advance(mixer=mixer)
    sm.update()
    assert sm.state == S.ERROR and pump.is_on is False
    assert sm.stop_reason == "mixer_implausible_during_fill"


def test_unfiltered_over_process_limit_aborts_although_filtered_is_below():
    sm, feed, pump, _ = make(target=180.0, ro=500.0)
    feed.ro = 1000.0
    start_pump(sm, feed)
    fill_to(sm, feed, 0.1)
    fill_to(sm, feed, 0.2)
    feed.advance(mixer=186.0, ro=1000.0)
    sm.update()
    assert sm.last_filtered_mixer_liters < 185.0
    assert sm.state == S.ERROR and sm.stop_reason == "mixer_implausible_during_fill"
    assert pump.is_on is False


def test_no_new_measurement_during_fill_times_out():
    sm, feed, pump, clock = make()
    start_pump(sm, feed)
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "measurement_stale_during_fill"
    assert pump.is_on is False


def test_existing_watchdog_still_applies_in_recipe_mode():
    sm, feed, pump, clock = make(no_fill_progress_timeout_seconds=8.0, min_fill_progress_liters=0.2)
    start_pump(sm, feed)
    clock.t += 4.5  # each step inside the 5 s measurement deadline
    feed.advance(mixer=0.0)
    sm.update()
    assert sm.state == S.FILL_UNTIL_TARGET
    clock.t += 4.5
    feed.advance(mixer=0.0)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "no_fill_progress_timeout"
    assert pump.is_on is False


# --- Zielerkennung: Pumpe zuerst aus, Bestaetigung ohne Nachfuellen ----------


def reach_target(sm, feed, mixer=30.0):
    feed.advance(mixer=mixer)
    sm.update()


def test_pump_off_in_the_same_update_as_first_target_reading_even_if_filter_lags():
    valve = FakeActuator("valve")
    sm, feed, pump, _ = make(valve=valve)
    start_pump(sm, feed)
    fill_to(sm, feed, 10.0)
    fill_to(sm, feed, 20.0)
    feed.advance(mixer=30.0)
    sm.update()  # ONE update
    assert sm.last_filtered_mixer_liters < 30.0  # the filter would not have confirmed yet
    assert pump.is_on is False and valve.off_calls >= 1
    assert sm.state == S.CONFIRM_TARGET
    assert sm.stop_reason == "target_first_reached_pump_off"
    assert pump.on_calls == 1


def test_confirmation_counts_distinct_measurements_with_pump_off_then_settles():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    reach_target(sm, feed)
    assert sm.target_confirm_count == 1
    sm.update()
    sm.update()  # same message repeatedly: no new confirmation
    assert sm.target_confirm_count == 1 and sm.state == S.CONFIRM_TARGET
    feed.advance(mixer=30.1)
    sm.update()
    assert sm.target_confirm_count == 2 and sm.state == S.CONFIRM_TARGET
    feed.advance(mixer=30.2)
    sm.update()
    assert sm.state == S.STOP_REFILL_PUMP and sm.stop_reason == "target_reached_by_sensor"
    sm.update()
    assert sm.state == S.SETTLE_LEVEL
    assert pump.on_calls == 1 and pump.is_on is False


def test_configured_confirmation_count_is_kept():
    sm, feed, pump, _ = make(target_reached_confirm_samples=1)
    start_pump(sm, feed)
    reach_target(sm, feed)
    sm.update()  # trigger already counted -> completes after the safety checks
    assert sm.state == S.STOP_REFILL_PUMP


def test_contradicting_confirmation_ends_in_error_without_refill():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    reach_target(sm, feed)
    feed.advance(mixer=29.0)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "target_not_confirmed"
    for _ in range(3):
        feed.advance(mixer=10.0)
        sm.update()
    assert pump.on_calls == 1 and pump.is_on is False


def test_confirmation_times_out_without_new_messages():
    sm, feed, pump, clock = make()
    start_pump(sm, feed)
    reach_target(sm, feed)
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "target_confirm_timeout"
    assert pump.on_calls == 1 and pump.is_on is False


def test_confirmation_phase_has_no_fill_progress_false_alarm():
    sm, feed, pump, clock = make(no_fill_progress_timeout_seconds=1.0, max_negative_level_drift_liters=0.1)
    start_pump(sm, feed)
    fill_to(sm, feed, 30.0)
    clock.t += 4.0  # long after the no-progress timeout; pump is off
    feed.advance(mixer=30.0)
    sm.update()
    assert sm.state == S.CONFIRM_TARGET and sm.error_message is None


def test_safety_checks_stay_active_during_confirmation():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    reach_target(sm, feed)
    feed.advance(mixer=186.0)
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == "mixer_implausible_during_fill"
    sm2, feed2, pump2, _ = make()
    start_pump(sm2, feed2)
    reach_target(sm2, feed2)
    feed2.advance(mixer=30.0, ro=float("nan"))
    sm2.update()
    assert sm2.state == S.ERROR and sm2.stop_reason == "ro_invalid_during_fill"


# --- R5: Filterhistorie und Metadaten ---------------------------------------


def test_filter_history_is_fed_once_per_message():
    sm, feed, pump, _ = make()
    sm.start()
    assert len(sm.mixer_liter_history) == 1
    for _ in range(4):
        sm.update()  # same message: pump-start wait
        sm._filtered_mixer_liters(feed())
    assert len(sm.mixer_liter_history) == 1
    feed.advance(mixer=0.2)
    sm.update()
    assert pump.on_calls == 1
    assert len(sm.mixer_liter_history) == 1  # pump-start check does not feed the filter
    feed.advance(mixer=1.0)
    sm.update()
    sm.update()
    sm._filtered_mixer_liters(feed())
    assert len(sm.mixer_liter_history) == 2
    assert sm.last_filtered_mixer_liters == pytest.approx(0.5)  # (0.0 + 1.0) / 2


def test_log_step_does_not_feed_the_filter_again():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    fill_to(sm, feed, 1.0)
    before = list(sm.mixer_liter_history)
    written = {}
    logger = SimpleNamespace(write_step=lambda **kw: written.update(kw))
    fc = FillAndMeasureController(feed)
    fc.state_machine, fc.process_logger = sm, logger
    fc._actuators = SimpleNamespace(status_payload=lambda: {})
    for _ in range(3):
        fc._log_step()
    assert list(sm.mixer_liter_history) == before
    assert written["mixer_liters_filtered"] == sm.last_filtered_mixer_liters


def test_stamp_metadata_does_not_change_other_consumers():
    from statemachine.sensor_reading import mixer_liters_from_snapshot, ro_liters_from_snapshot

    snap = Feed(mixer=3.0, ro=400.0)()
    assert RX_META_KEY in snap
    assert mixer_liters_from_snapshot(snap, {}) == 3.0
    assert ro_liters_from_snapshot(snap) == 400.0
    assert read_stamp(snap)[0] == 1


def test_reader_counts_messages_and_keeps_get_latest_contract():
    reader = MqttTopicReader("localhost", 1883, "t", max_age_seconds=5.0)

    def deliver(text):
        reader._on_message(None, None, SimpleNamespace(payload=text.encode()))

    assert reader.get_latest_stamped() is None
    deliver('{"a": 1}')
    first = reader.get_latest_stamped()
    again = reader.get_latest_stamped()
    assert first[RX_META_KEY] == again[RX_META_KEY] == {"seq": 1}  # same message
    assert RX_META_KEY not in reader.get_latest()  # public contract unchanged
    deliver('{"a": 1}')  # identical content, different message
    assert reader.get_latest_stamped()[RX_META_KEY] == {"seq": 2}
    reader.clear()
    assert reader.get_latest_stamped() is None
    deliver('{"a": 2}')
    assert reader.get_latest_stamped()[RX_META_KEY] == {"seq": 3}  # never reset
    deliver("[1, 2]")
    assert reader.get_latest_stamped() is None  # non-dict payload is unusable
    assert reader.get_latest_stamped(max_age_seconds=-1.0) is None


# --- R6: Fehler endgueltig, Emergency Stop ----------------------------------


def test_error_is_final_no_further_on_and_first_cause_kept():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    sm.error("Emergency stop requested from NiceGUI.")
    sm.error("spaeterer Folgefehler")
    assert sm.error_message == "Emergency stop requested from NiceGUI."
    for _ in range(3):
        feed.advance(mixer=1.0)
        sm.update()
    assert pump.on_calls == 1 and pump.is_on is False and sm.state == S.ERROR


def test_error_before_first_update_never_switches_on():
    sm, feed, pump, _ = make()
    sm.start()
    sm.error("Emergency")
    feed.advance()
    sm.update()
    assert pump.on_calls == 0


def test_error_raised_while_pump_on_is_executing_switches_off_immediately():
    holder = {}
    pump = FakeActuator(on_hook=lambda: holder["sm"].error("Emergency waehrend on()"))
    sm, feed, pump, _ = make(pump=pump)
    holder["sm"] = sm
    sm.start()
    feed.advance()
    sm.update()
    assert pump.on_calls == 1 and pump.is_on is False
    assert sm.state == S.ERROR and sm.fill_started_at is None


def test_concurrent_error_cannot_be_overwritten_by_running_handler():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    original = feed.__call__

    def reader_with_emergency():
        snap = original()
        sm.error("Emergency waehrend des Ticks")  # concurrent thread, simulated
        return snap

    sm.get_sensor_snapshot = reader_with_emergency
    feed.advance(mixer=30.0)  # handler would switch to CONFIRM_TARGET
    sm.update()
    assert sm.state == S.ERROR and pump.is_on is False
    assert sm.error_message == "Emergency waehrend des Ticks"


def test_real_threads_emergency_during_pump_on():
    entered, release = threading.Event(), threading.Event()

    def hook():
        entered.set()
        release.wait(2.0)

    pump = FakeActuator(on_hook=hook)
    sm, feed, pump, _ = make(pump=pump)
    sm.start()
    feed.advance()
    worker = threading.Thread(target=sm.update)
    worker.start()
    assert entered.wait(2.0)
    sm.error("Emergency aus anderem Thread")  # does not take the machine's lock
    release.set()
    worker.join(2.0)
    assert not worker.is_alive()
    assert pump.is_on is False and sm.state == S.ERROR
    assert pump.on_calls == 1


def test_emergency_stop_of_process_controller_stops_pump_and_stays_final(controller):
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    controller.fill_and_measure.state_machine = sm
    asyncio.run(controller.emergency_stop())
    assert pump.is_on is False and sm.state == S.ERROR
    assert "Emergency stop" in sm.error_message
    feed.advance(mixer=5.0)
    sm.update()
    assert pump.on_calls == 1 and sm.state == S.ERROR


def test_stop_event_handling_of_controller_loop_is_not_replaced():
    # Dokumentiert die Grenze: ein Stop-Event wird erst nach einem laufenden
    # update() gesetzt (request_stop braucht denselben Lock wie _run). Die
    # Abschaltung im Notfall laeuft ueber state_machine.error() (lock-frei).
    import inspect

    from process.background_process import BackgroundHardwareProcess

    assert "with self._lock" in inspect.getsource(BackgroundHardwareProcess.request_stop)


# --- validate_fill_runtime / Verdrahtung -------------------------------------


def test_runtime_validation_skips_start_only_rules_and_uses_min_for_need():
    from statemachine.start_validation import validate_fill_runtime, validate_fill_start

    settings = _settings(target=30.0)
    snap = Feed(mixer=12.0, ro=44.0)()
    assert validate_fill_start(settings, snap)[0] is False  # veto: start only
    assert validate_fill_runtime(settings, snap, 12.0)[0] is True  # need 18 + 20 = 38
    ok, message, reason = validate_fill_runtime(settings, snap, 5.0)  # lagging filter -> need 25 + 20
    assert (ok, reason) == (False, "ro_insufficient") and message.startswith("Lauf abgebrochen:")
    assert validate_fill_runtime(settings, snap, None)[0] is True  # invalid filtered: unfiltered only
    settings["recipe_run_config_snapshot"]["tank_emptied_confirmed"] = False
    assert validate_fill_runtime(settings, snap, 12.0)[2] == "tank_not_confirmed"


def test_cds_controller_feeds_processes_with_stamped_snapshots():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "nicegui_dashboard" / "cds_controller.py").read_text(
        encoding="utf-8"
    )
    assert "get_latest_stamped(" in source


# --- Review-Korrekturen R1/R2 ------------------------------------------------


def _circulation_machine(first_on_hook=None, second_on_hook=None):
    mixing = FakeActuator("mixing_circulation_pump", on_hook=first_on_hook)
    sensor = FakeActuator("sensor_circulation_pump", on_hook=second_on_hook)
    feed = Feed()
    clock = Clock()
    settings = _settings(target=30.0)
    settings.update(enable_mixing_circulation=True, enable_sensor_circulation=True, sensor_stabilize_seconds=0.0)
    sm = FillAndMeasureStateMachine(
        mixer_refill_pump=FakeActuator("mixer_refill_pump"),
        mixing_circulation_pump=mixing,
        sensor_circulation_pump=sensor,
        get_sensor_snapshot=feed,
        settings=settings,
        recipe_run=True,
        clock=clock,
    )
    sm.state = S.START_CIRCULATION
    return sm, mixing, sensor


def test_r1_emergency_during_first_circulation_pump_never_switches_on_the_second():
    holder = {}
    sm, mixing, sensor = _circulation_machine(first_on_hook=lambda: holder["sm"].error("Emergency Stop"))
    holder["sm"] = sm
    sm.update()
    assert sm.state == S.ERROR
    assert mixing.on_calls == 1 and mixing.is_on is False
    assert sensor.on_calls == 0 and sensor.is_on is False
    for _ in range(3):
        sm.update()
    assert sm.state == S.ERROR and sensor.on_calls == 0  # never reaches SENSOR_STABILIZE
    assert sm.error_message == "Emergency Stop"


def test_r1_emergency_during_second_circulation_pump_switches_it_off():
    holder = {}
    sm, mixing, sensor = _circulation_machine(second_on_hook=lambda: holder["sm"].error("Emergency Stop"))
    holder["sm"] = sm
    sm.update()
    assert sm.state == S.ERROR
    assert mixing.is_on is False and sensor.is_on is False


def test_r1_error_before_circulation_handler_switches_nothing_on():
    sm, mixing, sensor = _circulation_machine()
    sm.error("Emergency Stop")
    sm.state = S.START_CIRCULATION  # even if a stale state were left behind
    sm.update()
    assert mixing.on_calls == 0 and sensor.on_calls == 0


def test_r1_normal_circulation_sequence_is_unchanged():
    sm, mixing, sensor = _circulation_machine()
    sm.update()
    assert mixing.is_on and sensor.is_on and sm.state == S.SENSOR_STABILIZE


def test_r2_emergency_during_sensor_read_is_not_processed_further():
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    original = feed.__call__

    def reader():
        sm.error("Emergency Stop")  # concurrent emergency during the read
        feed.advance(mixer=30.0)  # ... and the reader returns a snapshot at the target
        return original()

    sm.get_sensor_snapshot = reader
    sm.update()
    assert sm.state == S.ERROR and pump.is_on is False
    assert pump.on_calls == 1
    assert sm.stop_reason != "target_first_reached_pump_off"
    assert sm.target_confirm_count == 0
    assert sm.error_message == "Emergency Stop"
    sm.update()
    sm.update()
    assert pump.on_calls == 1 and sm.state == S.ERROR


def test_r2_same_mechanism_in_confirmation_and_pump_start_handlers():
    # CONFIRM_TARGET
    sm, feed, pump, _ = make()
    start_pump(sm, feed)
    reach_target(sm, feed)
    original = feed.__call__

    def reader():
        sm.error("Emergency Stop")
        feed.advance(mixer=30.0)
        return original()

    sm.get_sensor_snapshot = reader
    count = sm.target_confirm_count
    sm.update()
    assert sm.state == S.ERROR and sm.target_confirm_count == count
    assert sm.stop_reason != "target_reached_by_sensor" and sm.error_message == "Emergency Stop"

    # START_REFILL_PUMP
    sm2, feed2, pump2, _ = make()
    sm2.start()
    original2 = feed2.__call__

    def reader2():
        sm2.error("Emergency Stop")
        feed2.advance()
        return original2()

    sm2.get_sensor_snapshot = reader2
    sm2.update()
    assert sm2.state == S.ERROR and pump2.on_calls == 0 and sm2.stop_reason is None


def test_r2_stop_reason_is_not_rewritten_after_error_latched():
    sm, feed, pump, _ = make()
    sm.stop_reason = "erster_grund"
    sm.error("Fehler A")
    sm.stop_reason = "spaeterer_grund"
    assert sm.stop_reason == "erster_grund"


def test_r2_legacy_snapshot_reads_also_stop_processing_after_concurrent_error():
    pump = FakeActuator()
    settings = {
        "min_ro_liters_required": 20.0, "level_filter_samples": 1, "max_mixer_liters": 200.0,
        "max_fill_seconds": 900.0, "no_fill_progress_timeout_seconds": 30.0,
        "min_fill_progress_liters": 0.5, "max_negative_level_drift_liters": 3.0,
        "target_reached_confirm_samples": 1, "fill_mode": "delta", "target_add_liters": 5.0,
        "level_settle_seconds": 0.0, "enable_mixing_circulation": False,
        "enable_sensor_circulation": False, "sensor_stabilize_seconds": 0.0,
    }
    holder = {}

    def reader():
        if holder.get("armed"):
            holder["sm"].error("Emergency Stop")
        return {"mixer": {"volume_liters_calc": 40.0}, "ro": {"volume_liters_calc": 500.0}}

    sm = FillAndMeasureStateMachine(mixer_refill_pump=pump, get_sensor_snapshot=reader, settings=settings)
    holder["sm"] = sm
    sm.start()
    sm.update()
    holder["armed"] = True
    sm.update()  # fill handler: target would be reached, but the error is latched
    assert sm.state == S.ERROR and sm.stop_reason is None
    assert sm.error_message == "Emergency Stop"


# --- Review-Korrekturen R3/R4 ------------------------------------------------


def _to_phase(phase):
    sm, feed, pump, clock = make()
    if phase == "pump_start":
        sm.start()
    elif phase == "fill":
        start_pump(sm, feed)
    else:
        start_pump(sm, feed)
        reach_target(sm, feed)
    return sm, feed, pump, clock


_TIMEOUT_REASONS = {
    "pump_start": "no_new_measurement_before_pump_start",
    "fill": "measurement_stale_during_fill",
    "confirm": "target_confirm_timeout",
}


@pytest.mark.parametrize("phase", ["pump_start", "fill", "confirm"])
@pytest.mark.parametrize("gap", [5.0, 6.0])
@pytest.mark.parametrize("fresh", [True, False])
def test_r3_expired_deadline_is_never_healed_by_a_new_measurement(phase, gap, fresh):
    sm, feed, pump, clock = _to_phase(phase)
    on_calls = pump.on_calls
    clock.t += gap
    if fresh:
        feed.advance(mixer=30.0 if phase == "confirm" else (0.0 if phase == "pump_start" else 5.0))
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == _TIMEOUT_REASONS[phase]
    assert pump.on_calls == on_calls and pump.is_on is False


@pytest.mark.parametrize("phase", ["pump_start", "fill", "confirm"])
def test_r3_just_inside_the_deadline_still_works_with_a_new_measurement(phase):
    sm, feed, pump, clock = _to_phase(phase)
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S - 0.1  # 4.9 s
    feed.advance(mixer=30.0 if phase == "confirm" else (0.0 if phase == "pump_start" else 5.0))
    sm.update()
    assert sm.state != S.ERROR and sm.error_message is None
    if phase == "pump_start":
        assert pump.on_calls == 1
    # the new measurement restarts the deadline
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S - 0.1
    feed.advance(mixer=30.0 if phase == "confirm" else (0.0 if phase == "pump_start" else 5.0))
    sm.update()
    assert sm.state != S.ERROR


@pytest.mark.parametrize("phase", ["pump_start", "fill", "confirm"])
def test_r3_same_message_inside_the_deadline_keeps_waiting(phase):
    sm, feed, pump, clock = _to_phase(phase)
    clock.t += MEASUREMENT_WAIT_TIMEOUT_S - 0.1
    sm.update()
    assert sm.state != S.ERROR


@pytest.mark.parametrize("phase", ["pump_start", "fill", "confirm"])
def test_r3_time_consumed_by_the_sensor_call_itself_counts(phase):
    sm, feed, pump, clock = _to_phase(phase)
    original = feed.__call__
    on_calls = pump.on_calls

    def slow_reader():
        clock.t += MEASUREMENT_WAIT_TIMEOUT_S + 1.0  # the call itself took 6 s
        feed.advance(mixer=30.0 if phase == "confirm" else (0.0 if phase == "pump_start" else 5.0))
        return original()

    sm.get_sensor_snapshot = slow_reader
    sm.update()
    assert sm.state == S.ERROR and sm.stop_reason == _TIMEOUT_REASONS[phase]
    assert pump.on_calls == on_calls and pump.is_on is False


def test_r3_deadline_uses_the_injected_monotonic_clock_only():
    import inspect

    source = inspect.getsource(FillAndMeasureStateMachine)
    assert "time.monotonic()" not in source and "datetime.now" not in source


class _EmergencyOnReadSettings(dict):
    """Settings whose evaluation of one key triggers an emergency stop."""

    machine = None
    trigger = None

    def get(self, key, default=None):
        if key == self.trigger and self.machine is not None:
            self.machine.error("Emergency Stop")
        return super().get(key, default)


def test_r4_emergency_between_check_and_second_pump_on_never_switches_it_on():
    mixing = FakeActuator("mixing_circulation_pump")
    sensor = FakeActuator("sensor_circulation_pump")
    settings = _EmergencyOnReadSettings(_settings(target=30.0))
    settings.update(enable_mixing_circulation=True, enable_sensor_circulation=True, sensor_stabilize_seconds=0.0)
    settings.trigger = "enable_sensor_circulation"
    sm = FillAndMeasureStateMachine(
        mixer_refill_pump=FakeActuator("mixer_refill_pump"),
        mixing_circulation_pump=mixing,
        sensor_circulation_pump=sensor,
        get_sensor_snapshot=Feed(),
        settings=settings,
        recipe_run=True,
        clock=Clock(),
    )
    settings.machine = sm
    sm.state = S.START_CIRCULATION
    sm.update()
    assert sm.state == S.ERROR
    assert mixing.is_on is False and sensor.is_on is False
    assert sensor.on_calls == 0
    assert mixing.on_calls == 1  # switched on before the emergency, then off
    sm.update()
    assert sm.state == S.ERROR and sensor.on_calls == 0  # no transition to SENSOR_STABILIZE


def test_r4_emergency_while_evaluating_the_first_pump_setting_switches_nothing_on():
    mixing = FakeActuator("mixing_circulation_pump")
    sensor = FakeActuator("sensor_circulation_pump")
    settings = _EmergencyOnReadSettings(_settings(target=30.0))
    settings.update(enable_mixing_circulation=True, enable_sensor_circulation=True)
    settings.trigger = "enable_mixing_circulation"
    sm = FillAndMeasureStateMachine(
        mixer_refill_pump=FakeActuator("mixer_refill_pump"),
        mixing_circulation_pump=mixing,
        sensor_circulation_pump=sensor,
        get_sensor_snapshot=Feed(),
        settings=settings,
        recipe_run=True,
        clock=Clock(),
    )
    settings.machine = sm
    sm.state = S.START_CIRCULATION
    sm.update()
    assert sm.state == S.ERROR and mixing.on_calls == 0 and sensor.on_calls == 0
