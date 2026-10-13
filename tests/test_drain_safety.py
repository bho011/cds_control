"""AP-04A: Drain-Sicherheit (process/drain.py, tank_cleaning/drain_phase.py,
manual_drain_jog.py, process/drain_safety.py).

Vollstaendig hardwarefrei (AP-00-Isolation aus conftest): Aktoren, MQTT, Sensor,
Zeit und Stop-Event sind Fakes. Alle Aktoren schreiben in EIN gemeinsames
Ereignisprotokoll, damit die Reihenfolge (Ventil vor Pumpe, Pumpe aus vor
Ventil zu) pruefbar ist.

O1 (offene Hardwareentscheidung): Das Verhalten nach einer fehlschlagenden
transfer_pump.off()-Abschaltung ist bewusst NICHT neu entschieden. Die Tests
dokumentieren nur, dass der Fehler gemeldet wird und beide Abschaltversuche
stattfinden (wie bisher beim Jog bzw. ueber safe_shutdown_all).
"""

from __future__ import annotations

import math
import types
from datetime import datetime, timedelta

import pytest

import process.drain as drain_module
import process.manual_drain_jog as jog_module
import process.tank_cleaning.drain_phase as tc_drain_module
from process.auto_circulation import AutoCirculationController, load_auto_circulation_config
from process.drain import run_drain_phase as run_standalone_drain
from process.drain_safety import (
    DrainSafetyLimits,
    DrainSafetySettingsError,
    DrainSensorWatchdog,
    filtered_after_observation,
    safe_stop_drain,
)
from process.manual_drain_jog import ManualDrainJog, parse_jog_timing
from process.tank_cleaning import TankCleaningController, TankCleaningPhase
from process.tank_cleaning.drain_phase import run_drain_phase as run_tank_cleaning_drain


# --- Fakes -----------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def as_time_module(self):
        return types.SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep)


class ClockEvent:
    """threading.Event-Ersatz auf FakeClock. `stop_at`: wird ab dieser Zeit gesetzt.
    `race_on_wait`: beim n-ten wait() liefert wait() False, danach ist das Flag aber gesetzt
    (Stop trifft unmittelbar nach dem Settle-Wait, vor dem Pumpenstart ein)."""

    def __init__(self, clock: FakeClock, stop_at: float | None = None, race_on_wait: int | None = None) -> None:
        self.clock = clock
        self.stop_at = stop_at
        self.race_on_wait = race_on_wait
        self.flag = False
        self.wait_calls = 0

    def wait(self, timeout=None) -> bool:
        self.wait_calls += 1
        self.clock.sleep(timeout or 0.0)
        if self.race_on_wait is not None and self.wait_calls == self.race_on_wait:
            self.flag = True
            return False
        if self.stop_at is not None and self.clock.t >= self.stop_at:
            self.flag = True
        return self.flag

    def is_set(self) -> bool:
        return self.flag

    def set(self) -> None:
        self.flag = True

    def clear(self) -> None:
        self.flag = False


class Log:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.events: list[tuple[str, str, float]] = []
        self.clock = clock

    def add(self, name: str, action: str) -> None:
        self.events.append((name, action, self.clock.t if self.clock else 0.0))

    def names(self) -> list[str]:
        return [f"{n}.{a}" for n, a, _ in self.events]

    def time_of(self, label: str) -> float:
        name, action = label.split(".")
        return next(t for n, a, t in self.events if n == name and a == action)


class FakeActuator:
    def __init__(self, name: str, log: Log, off_error: Exception | None = None, on_error: Exception | None = None):
        self.name = name
        self.log = log
        self.is_on = False
        self.off_error = off_error
        self.on_error = on_error

    def on(self) -> None:
        self.log.add(self.name, "on")
        if self.on_error is not None:
            raise self.on_error
        self.is_on = True

    def off(self) -> None:
        self.log.add(self.name, "off")
        if self.off_error is not None:
            raise self.off_error
        self.is_on = False


class FakeActuators:
    def __init__(self, log: Log, **kwargs) -> None:
        self.log = log
        self.map = {
            "transfer_pump": FakeActuator("pump", log, **kwargs.get("pump", {})),
            "drain_valve_0": FakeActuator("valve", log, **kwargs.get("valve", {})),
        }
        self.shutdown_errors: list[str] = []
        self.close_errors: list[str] = []
        self.shutdown_calls = 0
        self.close_calls = 0

    def get(self, name):
        return self.map[name]

    def add(self, name, gpio_pin):
        return self.map[name]

    def status_payload(self):
        return {name: a.is_on for name, a in self.map.items()}

    def safe_shutdown_all(self):
        self.shutdown_calls += 1
        return list(self.shutdown_errors)

    def close_all(self):
        self.close_calls += 1
        return list(self.close_errors)

    @property
    def pump(self):
        return self.map["transfer_pump"]

    @property
    def valve(self):
        return self.map["drain_valve_0"]


class FakeMqtt:
    def __init__(self, fail_from: int | None = None) -> None:
        self.payloads: list[dict] = []
        self.fail_from = fail_from
        self.closed = False

    def publish_json(self, payload):
        self.payloads.append(payload)
        if self.fail_from is not None and len(self.payloads) >= self.fail_from:
            raise RuntimeError("broker down")

    def close(self):
        self.closed = True


class FakeLogger:
    def write_step(self, **kwargs):
        pass


def ts(n: int) -> str:
    """Gueltiger, aufsteigender ISO-Zeitstempel wie von der Bridge (Sekundenaufloesung)."""
    return (datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=int(n))).isoformat(timespec="seconds")


def snap(level, t: float, stamp: str | None = None) -> dict:
    return {"timestamp": stamp if stamp is not None else ts(int(t)), "mixer": {"volume_liters_calc": level}}


class Sensor:
    """Bridge-Simulation: Snapshot-Zeitstempel wechselt einmal pro Sekunde (die
    Drain-Schleife liest alle 0,5 s, jeder Snapshot wird also zweimal gelesen)."""

    def __init__(self, clock: FakeClock, script) -> None:
        self.clock = clock
        self.script = script
        self.reads: list[tuple[str, object]] = []
        self.delay: tuple[float, float] | None = None  # (ab Zeit, Dauer) - einmalige Verzoegerung des Abrufs

    def get_latest(self, max_age_seconds=None):
        value = self.script(self.clock.t)
        if self.delay is not None and self.clock.t >= self.delay[0]:
            self.clock.sleep(self.delay[1])  # Abruf blockiert, waehrend die Deadline ablaeuft
            self.delay = None
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        snapshot = snap(value, self.clock.t)
        self.reads.append((snapshot["timestamp"], value))
        return snapshot


def draining(start: float, rate: float = 1.0):
    return lambda t: max(start - rate * t, 0.0)


def settings(**overrides) -> dict:
    base = {
        "max_mixer_liters": 185.0,
        "max_negative_level_drift_liters": 3.0,
        "level_filter_samples": 1,
        "empty_threshold_liters": 0.3,
        "empty_confirm_samples": 3,
        "transfer_pump_liters_per_minute": 60.0,
        "drain_timeout_buffer_seconds": 10.0,
        "no_drain_progress_warning_seconds": 60.0,
        "valve_settle_seconds": 1.0,
    }
    base.update(overrides)
    return base


def assert_no_on_after_stop(log: Log) -> None:
    """Nach dem ersten off-Aufruf darf kein Aktor mehr eingeschaltet werden."""
    seen_off = False
    for name, action, _ in log.events:
        if action == "off":
            seen_off = True
        if seen_off:
            assert action == "off", f"unbeabsichtigtes {name}.{action} nach Stop: {log.names()}"


def assert_safe_end(log: Log) -> None:
    names = log.names()
    assert names[-2:] == ["pump.off", "valve.off"], names


def run_standalone(
    *, script, settings_=None, stop_event=None, actuators_kwargs=None, mqtt=None, monkeypatch, sensor_delay=None
):
    clock = FakeClock()
    monkeypatch.setattr(drain_module, "time", clock.as_time_module())
    log = Log(clock)
    actuators = FakeActuators(log, **(actuators_kwargs or {}))
    sensor = Sensor(clock, script)
    sensor.delay = sensor_delay
    mqtt = mqtt or FakeMqtt()
    event = stop_event(clock) if callable(stop_event) else stop_event
    result = run_standalone_drain(
        settings_ or settings(), sensor, actuators, mqtt, FakeLogger(), auto_circulation=None, stop_event=event
    )
    return result, log, actuators, mqtt, clock, sensor


# --- Watchdog / Helfer (rein logisch) ---------------------------------------


TIMING_BAD_VALUES = {
    "transfer_pump_liters_per_minute": [float("nan"), float("inf"), 0.0, -1.0, "16", None, True],
    "drain_timeout_buffer_seconds": [float("nan"), float("inf"), -1.0, "180", None],
    "valve_settle_seconds": [float("nan"), float("inf"), -0.1, "1", None],
    "empty_threshold_liters": [float("nan"), float("inf"), -0.1, "0.3", None],
    "empty_confirm_samples": [0, -1, True, "5", 5.5, 5.0, None, float("nan")],
}


def _limits(**overrides) -> DrainSafetyLimits:
    return DrainSafetyLimits.from_settings(settings(**overrides), 40.0)


def test_limits_reject_unusable_settings():
    for bad in (
        {"max_mixer_liters": None},
        {"max_mixer_liters": float("nan")},
        {"max_mixer_liters": 0.0},
        {"max_negative_level_drift_liters": float("inf")},
        {"max_negative_level_drift_liters": -1.0},
        {"no_drain_progress_warning_seconds": 0.0},
        {"no_drain_progress_warning_seconds": float("nan")},
        {"no_drain_progress_warning_seconds": "60"},
    ):
        with pytest.raises(DrainSafetySettingsError):
            _limits(**bad)
    with pytest.raises(DrainSafetySettingsError):
        DrainSafetyLimits.from_settings(settings(), float("nan"))
    for key, bads in TIMING_BAD_VALUES.items():
        for bad in bads:
            with pytest.raises(DrainSafetySettingsError):
                _limits(**{key: bad})
    with pytest.raises(DrainSafetySettingsError):
        DrainSafetyLimits.from_settings(settings(max_mixer_liters=30.0), 40.0)


def test_watchdog_rejects_non_finite_and_invalid_values_and_times_out_on_loss():
    for bad in (None, float("nan"), float("inf"), -float("inf"), "12", True, -1.0):
        wd = DrainSensorWatchdog(_limits(), started_at=0.0)
        assert wd.observe(bad, ts(1), 1.0).violation is None  # kurzer Ausfall wird toleriert
        assert wd.observe(bad, ts(2), 5.0).violation is None  # genau 5,0 s: noch innerhalb
        violation = wd.observe(bad, ts(3), 5.01).violation
        assert violation is not None and violation[0] == "sensor_lost_during_drain", bad


def test_watchdog_short_gap_recovers():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(1), 1.0).is_new_sample
    assert wd.observe(None, None, 3.0).violation is None
    obs = wd.observe(36.0, ts(4), 4.0)
    assert obs.is_new_sample and obs.violation is None
    assert wd.observe(None, None, 8.9).violation is None  # 4,9 s seit letztem neuen Wert


def test_watchdog_repeated_snapshot_is_not_a_new_measurement():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(1), 1.0).is_new_sample
    assert not wd.observe(39.0, ts(1), 1.5).is_new_sample
    # Ein eingefrorener/wiederholter Snapshot haelt den Verlust-Timer NICHT zurueck.
    violation = wd.observe(39.0, ts(1), 6.5).violation
    assert violation is not None and violation[0] == "sensor_lost_during_drain"


@pytest.mark.parametrize("bad_stamp", [None, "", "   ", 123, 1.5, [ts(1)], {"a": 1}])
def test_watchdog_missing_or_invalid_stamp_is_not_a_new_measurement(bad_stamp):
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    for t in (1.0, 2.0, 3.0, 5.0):
        obs = wd.observe(39.0, bad_stamp, t)
        assert not obs.is_new_sample and obs.violation is None
    # Unbrauchbare Kennzeichnung haelt den Verlust-Timer nicht zurueck.
    violation = wd.observe(39.0, bad_stamp, 5.01).violation
    assert violation is not None and violation[0] == "sensor_lost_during_drain"


def test_watchdog_deadline_is_checked_before_a_late_measurement_is_accepted():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(1), 1.0).is_new_sample
    obs = wd.observe(38.0, ts(7), 7.0)  # 6,0 s nach der letzten neuen Messung
    assert obs.violation is not None and obs.violation[0] == "sensor_lost_during_drain"
    assert not obs.is_new_sample  # die verspaetete Messung wurde nicht angenommen
    assert wd.latest_raw_liters == 39.0
    # Die Frist wird auch durch eine spaetere Messung nicht rueckwirkend verlaengert.
    assert wd.observe(37.0, ts(8), 8.0).violation is not None


def test_watchdog_deadline_boundary_contract():
    # Vertrag: Abbruch nur bei STRIKT mehr als 5,0 s ohne neue gueltige Messung.
    for offset, expect_abort in ((4.9, False), (5.0, False), (5.01, True), (6.0, True)):
        wd = DrainSensorWatchdog(_limits(), started_at=0.0)
        assert wd.observe(39.0, ts(1), 1.0).is_new_sample
        obs = wd.observe(38.0, ts(2), 1.0 + offset)
        assert (obs.violation is not None) is expect_abort, offset
        if not expect_abort:
            assert obs.is_new_sample and obs.violation is None


def test_watchdog_progress_is_a_rolling_window_not_a_one_time_total():
    wd = DrainSensorWatchdog(_limits(no_drain_progress_warning_seconds=60.0), started_at=0.0)
    # 40 -> 39 (Fortschritt bei t=1), danach dauerhaft 39 mit immer neuen Zeitstempeln.
    assert wd.observe(39.0, ts(1), 1.0).violation is None
    for t in range(2, 61):
        assert wd.observe(39.0, ts(t), float(t)).violation is None, t
    violation = wd.observe(39.0, ts(61), 61.0).violation  # 60 s nach dem letzten Fortschritt
    assert violation is not None and violation[0] == "no_drain_progress"


def test_watchdog_continuing_progress_restarts_window_and_small_steps_do_not():
    wd = DrainSensorWatchdog(_limits(no_drain_progress_warning_seconds=60.0), started_at=0.0)
    level = 40.0
    for t in range(1, 400):
        if t % 40 == 0:
            level -= 0.5  # >= vorhandene Schwelle 0,3 L: gilt als Fortschritt, Fenster startet neu
        assert wd.observe(level, ts(t), float(t)).violation is None, t
    # Rauschen unterhalb der Schwelle (0,2 L Ausschlag) ist kein Fortschritt.
    wd = DrainSensorWatchdog(_limits(no_drain_progress_warning_seconds=60.0), started_at=0.0)
    violation = None
    for t in range(1, 200):
        violation = wd.observe(40.0 - (0.2 if t % 2 else 0.0), ts(t), float(t)).violation
        if violation:
            break
    assert violation is not None and violation[0] == "no_drain_progress"


def test_watchdog_repeated_snapshot_is_no_progress_evidence():
    wd = DrainSensorWatchdog(_limits(no_drain_progress_warning_seconds=60.0), started_at=0.0)
    assert wd.observe(40.0, ts(1), 1.0).violation is None
    # Pegel faellt, aber nur ueber wiederholte (nicht neue) Snapshots gemeldet -> kein Fortschritt
    assert not wd.observe(40.0, ts(1), 2.0).is_new_sample
    assert wd.latest_raw_liters == 40.0


def test_filter_history_is_not_weighted_twice_by_repeated_snapshots():
    from collections import deque

    history = deque([10.0, 12.0], maxlen=5)
    before = list(history)
    history.append(12.0)  # Reader haengt den Wiederholungs-Snapshot an
    filtered = filtered_after_observation(history, before, 11.3333, raw_valid=True, is_new=False)
    assert list(history) == [10.0, 12.0] and filtered == 11.0

    before = list(history)
    history.append(8.0)
    assert filtered_after_observation(history, before, 10.0, raw_valid=True, is_new=True) == 10.0
    assert list(history) == [10.0, 12.0, 8.0]

    before = list(history)  # ungueltige Messung: Reader aendert nichts, Semantik (None) bleibt
    assert filtered_after_observation(history, before, None, raw_valid=False, is_new=False) is None
    assert list(history) == before


def test_watchdog_plausibility_uses_unfiltered_values():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(43.0, ts(1), 1.0).violation is None  # genau start + drift
    violation = wd.observe(43.01, ts(2), 2.0).violation
    assert violation is not None and violation[0] == "level_rise_during_drain"
    wd = DrainSensorWatchdog(_limits(max_mixer_liters=185.0), started_at=0.0)
    wd.limits = DrainSafetyLimits(40.0, 41.0, 100.0, 60.0)
    violation = wd.observe(41.5, ts(1), 1.0).violation
    assert violation is not None and violation[0] == "mixer_over_max_limit"


def test_safe_stop_drain_order_and_isolated_errors():
    log = Log()
    pump = FakeActuator("pump", log)
    valve = FakeActuator("valve", log)
    assert safe_stop_drain(pump, valve).ok
    assert log.names() == ["pump.off", "valve.off"]

    log = Log()
    pump = FakeActuator("pump", log, off_error=RuntimeError("relay stuck"))
    valve = FakeActuator("valve", log)
    result = safe_stop_drain(pump, valve)
    assert not result.ok and "relay stuck" in result.errors[0]
    assert log.names() == ["pump.off", "valve.off"]  # O1: beide Versuche, wie bisher

    log = Log()
    result = safe_stop_drain(
        FakeActuator("pump", log, off_error=RuntimeError("a")), FakeActuator("valve", log, off_error=RuntimeError("b"))
    )
    assert len(result.errors) == 2
    assert safe_stop_drain(None, None).ok


# --- process/drain.py --------------------------------------------------------


def test_standalone_normal_sequence_valve_before_pump_and_pump_off_before_valve_closed(monkeypatch):
    result, log, actuators, mqtt, clock, sensor = run_standalone(script=draining(20.0), monkeypatch=monkeypatch)
    assert result.success and result.stop_reason == "empty_by_sensor"
    assert log.names() == ["valve.on", "pump.on", "pump.off", "valve.off"]
    assert log.time_of("pump.on") - log.time_of("valve.on") >= 1.0  # Settle-Zeit
    assert log.time_of("valve.off") >= log.time_of("pump.off")
    assert not actuators.pump.is_on and not actuators.valve.is_on
    assert_no_on_after_stop(log)


def test_standalone_empty_confirm_needs_independent_samples(monkeypatch):
    result, log, _, _, clock, sensor = run_standalone(
        script=draining(10.0, rate=1.0), settings_=settings(empty_confirm_samples=3), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "empty_by_sensor"
    low_stamps = {stamp for stamp, level in sensor.reads if level <= 0.3}
    # Jeder Snapshot wird zweimal gelesen; die Bestaetigung zaehlt nur 3 verschiedene.
    assert len(low_stamps) >= 3


def test_standalone_stop_during_valve_settle_never_starts_pump_and_closes_valve(monkeypatch):
    result, log, actuators, _, _, _ = run_standalone(
        script=draining(20.0), stop_event=lambda c: ClockEvent(c, stop_at=0.5), monkeypatch=monkeypatch
    )
    assert not result.success and result.stop_reason == "stop_requested_during_valve_settle"
    assert log.names() == ["valve.on", "pump.off", "valve.off"]
    assert "pump.on" not in log.names()


def test_standalone_stop_immediately_before_pump_start_never_starts_pump(monkeypatch):
    result, log, _, _, _, _ = run_standalone(
        script=draining(20.0), stop_event=lambda c: ClockEvent(c, race_on_wait=1), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "stop_requested_before_pump_start"
    assert "pump.on" not in log.names()
    assert_safe_end(log)


def test_standalone_stop_during_pump_run_and_repeated_stop_requests(monkeypatch):
    result, log, actuators, _, _, _ = run_standalone(
        script=draining(100.0), stop_event=lambda c: ClockEvent(c, stop_at=6.0), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "stop_requested" and not result.success
    assert log.names() == ["valve.on", "pump.on", "pump.off", "valve.off"]
    # Bereits gesetztes Stop-Event (wiederholte Anforderung): Pumpe startet nie.
    monkeypatch.undo()
    result2, log2, _, _, _, _ = run_standalone(
        script=draining(100.0), stop_event=lambda c: _preset(c), monkeypatch=monkeypatch
    )
    assert "pump.on" not in log2.names()
    assert_no_on_after_stop(log2)


def _preset(clock):
    event = ClockEvent(clock)
    event.set()
    event.set()
    return event


def test_standalone_sensor_loss_aborts_after_provisional_window_and_stops_pump(monkeypatch):
    script = lambda t: draining(100.0)(t) if t < 10.0 else None
    result, log, _, mqtt, clock, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "sensor_lost_during_drain" and not result.success
    assert_safe_end(log)
    assert clock.t <= 16.0  # letzter neuer Wert t=9, Grenze 5 s
    assert mqtt.payloads[-1]["process_state"] == "DRAIN_STOPPED"
    assert "sensor_lost" in (mqtt.payloads[-1]["error"] or "")


def test_standalone_short_sensor_gap_does_not_abort(monkeypatch):
    script = lambda t: None if 5.0 <= t < 7.5 else draining(20.0)(t)
    result, log, _, _, _, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "empty_by_sensor"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "oops", True, -3.0])
def test_standalone_invalid_values_are_treated_as_loss(monkeypatch, bad):
    script = lambda t: draining(100.0)(t) if t < 6.0 else snap(bad, t)
    result, log, _, _, _, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "sensor_lost_during_drain"
    assert_safe_end(log)


def test_standalone_reader_exception_during_run_stops_pump(monkeypatch):
    script = lambda t: draining(100.0)(t) if t < 6.0 else RuntimeError("reader crashed")
    result, log, _, _, _, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "error" and not result.success
    assert_safe_end(log)


def test_standalone_frozen_value_runs_into_no_progress_abort(monkeypatch):
    # Frischer Zeitstempel jede Sekunde, aber der Pegel bewegt sich nicht.
    result, log, _, _, clock, _ = run_standalone(
        script=lambda t: 40.0, settings_=settings(transfer_pump_liters_per_minute=1.0), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "no_drain_progress" and not result.success
    assert 60.0 <= clock.t <= 63.0
    assert_safe_end(log)


def test_standalone_level_rise_and_over_max_abort(monkeypatch):
    script = lambda t: 40.0 - t if t < 5.0 else 60.0
    result, log, _, _, _, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "level_rise_during_drain"
    assert_safe_end(log)

    script = lambda t: 40.0 - t if t < 5.0 else 250.0
    result, log, _, _, _, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "mixer_over_max_limit"
    assert_safe_end(log)


def test_standalone_calculated_timeout_still_bounds_runtime(monkeypatch):
    # 20 L bei 60 L/min = 20 s + 10 s Puffer = 30 s; langsamer, aber messbarer Abfluss.
    s = settings(no_drain_progress_warning_seconds=30.0)
    result, log, _, _, clock, _ = run_standalone(
        script=draining(20.0, rate=0.02), settings_=s, monkeypatch=monkeypatch
    )
    assert result.stop_reason == "calculated_drain_timeout" and not result.success
    assert clock.t <= 32.0
    assert_safe_end(log)


def test_standalone_mqtt_publish_exception_while_pump_runs_stops_pump(monkeypatch):
    mqtt = FakeMqtt(fail_from=3)
    result, log, actuators, _, _, _ = run_standalone(script=draining(100.0), mqtt=mqtt, monkeypatch=monkeypatch)
    assert result.stop_reason == "error" and not result.success
    assert_safe_end(log)
    assert not actuators.pump.is_on and not actuators.valve.is_on


def test_standalone_final_publish_failure_does_not_hide_result(monkeypatch):
    mqtt = FakeMqtt(fail_from=1)
    result, log, _, _, _, _ = run_standalone(script=draining(100.0), mqtt=mqtt, monkeypatch=monkeypatch)
    assert result.stop_reason == "error"
    assert_safe_end(log)


def test_standalone_pump_off_failure_is_reported_and_valve_off_still_attempted_O1(monkeypatch):
    result, log, _, mqtt, _, _ = run_standalone(
        script=draining(20.0),
        actuators_kwargs={"pump": {"off_error": RuntimeError("relay stuck")}},
        monkeypatch=monkeypatch,
    )
    assert not result.success and result.stop_reason == "drain_cleanup_failed"
    assert log.names()[-2:] == ["pump.off", "valve.off"]
    final = mqtt.payloads[-1]
    assert final["process_state"] == "DRAIN_STOPPED"
    assert "relay stuck" in final["error"] and final["process_details"]["cleanup_errors"]


def test_standalone_valve_on_exception_never_starts_pump_and_still_cleans_up(monkeypatch):
    result, log, _, _, _, _ = run_standalone(
        script=draining(20.0),
        actuators_kwargs={"valve": {"on_error": RuntimeError("gpio busy")}},
        monkeypatch=monkeypatch,
    )
    assert result.stop_reason == "error"
    assert "pump.on" not in log.names()
    assert_safe_end(log)


@pytest.mark.parametrize(
    "bad",
    [
        {"max_mixer_liters": None},
        {"max_mixer_liters": float("nan")},
        {"no_drain_progress_warning_seconds": 0.0},
        {"max_negative_level_drift_liters": float("inf")},
    ],
)
def test_standalone_unusable_safety_settings_block_before_any_actuator(monkeypatch, bad):
    result, log, _, _, _, _ = run_standalone(
        script=draining(20.0), settings_=settings(**bad), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "invalid_drain_safety_settings" and not result.success
    assert log.events == []


def test_standalone_start_sensor_exception_or_missing_level_touches_no_actuator(monkeypatch):
    result, log, _, _, _, _ = run_standalone(script=lambda t: RuntimeError("down"), monkeypatch=monkeypatch)
    assert result.stop_reason == "sensor_read_error_before_drain" and log.events == []
    result, log, _, _, _, _ = run_standalone(script=lambda t: None, monkeypatch=monkeypatch)
    assert result.stop_reason == "missing_mixer_level_before_drain" and log.events == []


def test_standalone_zero_liters_is_not_treated_as_physical_proof_only_sensor_result(monkeypatch):
    result, *_ = run_standalone(script=draining(5.0, rate=5.0), monkeypatch=monkeypatch)
    # Das Ergebnis heisst bewusst "empty_by_sensor", nicht "empty".
    assert result.stop_reason == "empty_by_sensor"


# --- Tank Cleaning drain_phase ----------------------------------------------


def make_tc(monkeypatch, script, stop_event=None, actuators_kwargs=None, **settings_overrides):
    clock = FakeClock()
    monkeypatch.setattr(tc_drain_module, "time", clock.as_time_module())
    log = Log(clock)
    actuators = FakeActuators(log, **(actuators_kwargs or {}))
    sensor = Sensor(clock, script)
    controller = TankCleaningController(get_sensor_snapshot=lambda: sensor.get_latest())
    controller._stop_event = stop_event(clock) if callable(stop_event) else ClockEvent(clock)
    s = settings(**settings_overrides)
    controller._auto_circulation = AutoCirculationController(
        actuators=actuators, config=load_auto_circulation_config(s)
    )
    return controller, s, actuators, log, clock


def run_tc(controller, s, actuators):
    return run_tank_cleaning_drain(controller, s, actuators, actuators.pump, actuators.valve)


def test_tc_normal_sequence(monkeypatch):
    controller, s, actuators, log, clock = make_tc(monkeypatch, draining(20.0))
    run_tc(controller, s, actuators)
    assert controller._phase == TankCleaningPhase.DRAINING  # Phase wird vom Controller beendet
    assert controller._stop_reason is None
    assert log.names() == ["valve.on", "pump.on", "pump.off", "valve.off"]
    assert log.time_of("pump.on") - log.time_of("valve.on") >= 1.0
    assert_no_on_after_stop(log)


def test_tc_stop_during_valve_settle_closes_valve_and_never_starts_pump(monkeypatch):
    controller, s, actuators, log, _ = make_tc(
        monkeypatch, draining(20.0), stop_event=lambda c: ClockEvent(c, stop_at=0.5)
    )
    run_tc(controller, s, actuators)
    assert "pump.on" not in log.names()
    assert not actuators.valve.is_on and not actuators.pump.is_on
    assert_safe_end(log)


def test_tc_stop_immediately_before_pump_start(monkeypatch):
    controller, s, actuators, log, _ = make_tc(
        monkeypatch, draining(20.0), stop_event=lambda c: ClockEvent(c, race_on_wait=1)
    )
    run_tc(controller, s, actuators)
    assert "pump.on" not in log.names()
    assert_safe_end(log)


def test_tc_repeated_stop_requests_do_not_restart_anything(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(100.0))
    controller.request_stop("a")
    controller.request_stop("b")
    controller.request_stop("c")
    run_tc(controller, s, actuators)
    assert "pump.on" not in log.names()
    assert_no_on_after_stop(log)


def test_tc_sensor_loss_aborts_with_error_status(monkeypatch):
    script = lambda t: draining(100.0)(t) if t < 10.0 else None
    controller, s, actuators, log, _ = make_tc(monkeypatch, script)
    run_tc(controller, s, actuators)
    assert controller._phase == TankCleaningPhase.ERROR
    assert controller._stop_reason == "sensor_lost_during_drain"
    assert_safe_end(log)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", -1.0])
def test_tc_invalid_values_abort(monkeypatch, bad):
    script = lambda t: draining(100.0)(t) if t < 6.0 else snap(bad, t)
    controller, s, actuators, log, _ = make_tc(monkeypatch, script)
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "sensor_lost_during_drain"
    assert_safe_end(log)


def test_tc_level_rise_and_no_progress_and_timeout(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, lambda t: 40.0 - t if t < 5 else 60.0)
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "level_rise_during_drain"
    assert_safe_end(log)

    controller, s, actuators, log, _ = make_tc(monkeypatch, lambda t: 40.0, transfer_pump_liters_per_minute=1.0)
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "no_drain_progress"
    assert_safe_end(log)

    controller, s, actuators, log, clock = make_tc(
        monkeypatch, draining(20.0, rate=0.02), no_drain_progress_warning_seconds=30.0
    )
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "calculated_drain_timeout"
    assert_safe_end(log)


def test_tc_unusable_safety_settings_block_before_any_actuator(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0), max_mixer_liters=None)
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "invalid_drain_safety_settings"
    assert log.events == []


def test_tc_exception_while_pump_runs_still_cleans_up_and_propagates(monkeypatch):
    script = lambda t: draining(100.0)(t) if t < 6.0 else RuntimeError("reader crashed")
    controller, s, actuators, log, _ = make_tc(monkeypatch, script)
    with pytest.raises(RuntimeError, match="reader crashed"):
        run_tc(controller, s, actuators)
    assert_safe_end(log)


def test_tc_pump_off_failure_is_recorded_not_reported_as_success_O1(monkeypatch):
    controller, s, actuators, log, _ = make_tc(
        monkeypatch, draining(20.0), actuators_kwargs={"pump": {"off_error": RuntimeError("relay stuck")}}
    )
    run_tc(controller, s, actuators)
    assert controller._phase == TankCleaningPhase.ERROR
    assert controller._stop_reason == "cleanup_failed"
    assert "relay stuck" in controller._last_error
    assert log.names()[-2:] == ["pump.off", "valve.off"]


def test_tc_shutdown_error_list_is_not_reported_as_clean_shutdown(monkeypatch):
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", FakeMqtt)
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0))
    actuators.shutdown_errors = ["Could not switch off actuator 'transfer_pump': boom"]
    controller._mqtt_publisher = FakeMqtt()
    controller._safe_shutdown_and_publish(actuators)
    assert controller._phase == TankCleaningPhase.ERROR
    assert controller._stop_reason == "cleanup_failed"
    assert "transfer_pump" in controller._last_error
    assert actuators.close_calls == 1  # Cleanup lief trotz Fehler weiter


def test_tc_shutdown_exception_does_not_skip_close_all(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0))

    def boom():
        raise RuntimeError("shutdown boom")

    actuators.safe_shutdown_all = boom
    controller._mqtt_publisher = FakeMqtt()
    controller._safe_shutdown_and_publish(actuators)
    assert actuators.close_calls == 1
    assert controller._stop_reason == "cleanup_failed"


# --- Manual Drain Jog --------------------------------------------------------


def _jog_settings(**overrides):
    base = {"hardware_execution_enabled": True}
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "key,value",
    [
        ("manual_drain_jog_max_seconds", float("nan")),
        ("manual_drain_jog_max_seconds", float("inf")),
        ("manual_drain_jog_max_seconds", -1.0),
        ("manual_drain_jog_max_seconds", 0.0),
        ("manual_drain_jog_max_seconds", 30.01),
        ("manual_drain_jog_max_seconds", 3600),
        ("manual_drain_jog_max_seconds", "30"),
        ("manual_drain_jog_max_seconds", None),
        ("manual_drain_jog_max_seconds", True),
        ("manual_drain_jog_valve_settle_seconds", float("nan")),
        ("manual_drain_jog_valve_settle_seconds", float("inf")),
        ("manual_drain_jog_valve_settle_seconds", -0.1),
        ("manual_drain_jog_valve_settle_seconds", "0.5"),
        ("manual_drain_jog_valve_settle_seconds", None),
    ],
)
def test_jog_invalid_timing_blocks_start_without_thread(key, value):
    started = []
    jog = ManualDrainJog(lambda: {"mixer": {"volume_liters_calc": 10.0}})
    jog._run = lambda settings: started.append(1)  # darf nie laufen
    result = jog.start(_jog_settings(**{key: value}))
    assert result["success"] is False and key in result["message"]
    assert jog._thread is None and started == []
    assert jog.max_seconds == 30.0


def test_jog_valid_timing_and_defaults():
    assert parse_jog_timing({}) == (30.0, 0.5)
    assert parse_jog_timing({"manual_drain_jog_max_seconds": 30, "manual_drain_jog_valve_settle_seconds": 0}) == (30.0, 0.0)
    assert parse_jog_timing({"manual_drain_jog_max_seconds": 12.5}) == (12.5, 0.5)
    assert not any(isinstance(v, float) and math.isnan(v) for v in parse_jog_timing({}))


def _prepare_jog(monkeypatch, *, pump=None, valve=None, mqtt_cls=FakeMqtt):
    log = Log()
    holder = {}

    class Manager(FakeActuators):
        def __init__(self, active_low):
            super().__init__(log, pump=pump or {}, valve=valve or {})
            holder["actuators"] = self

    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", Manager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", mqtt_cls)
    jog = ManualDrainJog(lambda: {"mixer": {"volume_liters_calc": 10.0}})
    return jog, log, holder


def test_jog_normal_run_order_and_watchdog(monkeypatch):
    jog, log, holder = _prepare_jog(monkeypatch)
    jog.max_seconds = 0.25
    jog.valve_settle_seconds = 0.0
    jog._run(_jog_settings())
    assert log.names()[:2] == ["valve.on", "pump.on"]
    assert jog._stop_reason == "max_runtime_reached"
    assert log.names()[2:4] == ["pump.off", "valve.off"]
    assert holder["actuators"].close_calls == 1


def test_jog_stop_during_settle_never_starts_pump(monkeypatch):
    jog, log, _ = _prepare_jog(monkeypatch)
    jog.valve_settle_seconds = 5.0
    jog.request_stop("button_released")  # Event schon gesetzt -> wait() kehrt sofort zurueck
    jog._run(_jog_settings())
    assert "pump.on" not in log.names()
    assert log.names()[:1] == ["valve.on"] and log.names()[1:3] == ["pump.off", "valve.off"]
    assert_no_on_after_stop(log)


def test_jog_stop_immediately_before_pump_start(monkeypatch):
    jog, log, _ = _prepare_jog(monkeypatch)
    jog.valve_settle_seconds = 0.0
    jog._stop_event = ClockEvent(FakeClock(), race_on_wait=1)
    jog._run(_jog_settings())
    assert "pump.on" not in log.names()
    assert jog._stop_reason == "button_released_before_pump_start"
    assert_no_on_after_stop(log)


def test_jog_repeated_stop_requests_are_harmless(monkeypatch):
    jog, log, _ = _prepare_jog(monkeypatch)
    for _ in range(3):
        jog.request_stop("button_released")
    jog.valve_settle_seconds = 0.0
    jog._run(_jog_settings())
    assert "pump.on" not in log.names()
    assert jog.stop()["success"] is True


def test_jog_exception_while_pump_runs_cleans_up(monkeypatch):
    jog, log, _ = _prepare_jog(monkeypatch)
    jog.valve_settle_seconds = 0.0

    def boom(_seconds):
        raise RuntimeError("loop crashed")

    monkeypatch.setattr(jog_module, "time", types.SimpleNamespace(monotonic=FakeClock().monotonic, sleep=boom))
    jog._run(_jog_settings())
    assert jog._stop_reason == "error"
    assert log.names()[:2] == ["valve.on", "pump.on"] and log.names()[2:4] == ["pump.off", "valve.off"]


def test_jog_pump_off_failure_is_reported_and_valve_off_attempted_O1(monkeypatch):
    jog, log, _ = _prepare_jog(monkeypatch, pump={"off_error": RuntimeError("relay stuck")})
    jog.max_seconds = 0.15
    jog.valve_settle_seconds = 0.0
    jog._run(_jog_settings())
    assert jog._stop_reason == "cleanup_failed"
    assert "relay stuck" in (jog._last_error or "")
    assert "valve.off" in log.names()


def test_jog_shutdown_error_list_is_not_reported_as_success(monkeypatch):
    jog, log, holder = _prepare_jog(monkeypatch)
    jog.max_seconds = 0.15
    jog.valve_settle_seconds = 0.0
    original = FakeActuators.safe_shutdown_all

    def with_errors(self):
        original(self)
        return ["Could not switch off actuator 'transfer_pump': boom"]

    monkeypatch.setattr(FakeActuators, "safe_shutdown_all", with_errors)
    jog._run(_jog_settings())
    assert jog._stop_reason == "cleanup_failed"
    assert "transfer_pump" in (jog._last_error or "")
    assert holder["actuators"].close_calls == 1


def test_jog_mqtt_construction_failure_leaves_everything_off(monkeypatch):
    class Broken:
        def __init__(self):
            raise RuntimeError("no broker")

    jog, log, _ = _prepare_jog(monkeypatch, mqtt_cls=Broken)
    jog._run(_jog_settings())
    assert "pump.on" not in log.names() and "valve.on" not in log.names()
    assert jog._stop_reason == "error"


# --- Korrekturen R1-R5 (Review) ----------------------------------------------


def test_r1_slow_sensor_read_cannot_hide_an_expired_deadline(monkeypatch):
    # Der Abruf bei t>=3 blockiert 6 s; er liefert zwar einen NEUEN gueltigen Wert,
    # aber die Frist ist waehrenddessen abgelaufen -> Abbruch, Messung nicht angenommen.
    result, log, _, _, clock, _ = run_standalone(
        script=draining(100.0), sensor_delay=(3.0, 6.0), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "sensor_lost_during_drain" and not result.success
    assert_safe_end(log)


def test_r2_snapshots_without_stamp_never_count_as_new_in_standalone(monkeypatch):
    def script(t):
        if t < 3.0:
            return draining(100.0)(t)
        return {"mixer": {"volume_liters_calc": 0.0}}  # leer gemeldet, aber ohne Kennzeichnung

    result, log, _, _, clock, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "sensor_lost_during_drain"  # nicht "empty_by_sensor"
    assert clock.t <= 9.0
    assert_safe_end(log)


def test_r2_snapshots_without_stamp_never_count_as_new_in_tank_cleaning(monkeypatch):
    def script(t):
        if t < 3.0:
            return draining(100.0)(t)
        return {"mixer": {"volume_liters_calc": 0.0}}

    controller, s, actuators, log, _ = make_tc(monkeypatch, script)
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "sensor_lost_during_drain"
    assert_safe_end(log)


def test_r3_one_off_progress_does_not_satisfy_the_watchdog_standalone(monkeypatch):
    # 50 L -> 49 L, danach dauerhaft 49 L trotz fortlaufender neuer Nachrichten.
    s = settings(transfer_pump_liters_per_minute=1.0)
    result, log, _, _, clock, _ = run_standalone(
        script=lambda t: 50.0 if t < 1.0 else 49.0, settings_=s, monkeypatch=monkeypatch
    )
    assert result.stop_reason == "no_drain_progress" and not result.success
    assert 60.0 <= clock.t <= 63.0
    assert log.names() == ["valve.on", "pump.on", "pump.off", "valve.off"]  # genau ein Pumpenstart
    assert_no_on_after_stop(log)


def test_r3_one_off_progress_does_not_satisfy_the_watchdog_tank_cleaning(monkeypatch):
    controller, s, actuators, log, _ = make_tc(
        monkeypatch, lambda t: 50.0 if t < 1.0 else 49.0, transfer_pump_liters_per_minute=1.0
    )
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "no_drain_progress"
    assert log.names() == ["valve.on", "pump.on", "pump.off", "valve.off"]
    assert_no_on_after_stop(log)


def test_r3_calculated_total_timeout_stays_independent(monkeypatch):
    # Stetiger Fortschritt (0,3 L je 15 s) haelt das Fenster offen; der Gesamt-Timeout greift trotzdem.
    s = settings(no_drain_progress_warning_seconds=30.0)
    result, log, _, _, clock, _ = run_standalone(
        script=draining(20.0, rate=0.02), settings_=s, monkeypatch=monkeypatch
    )
    assert result.stop_reason == "calculated_drain_timeout"


@pytest.mark.parametrize("key", list(TIMING_BAD_VALUES))
def test_r4_non_finite_or_invalid_timing_settings_block_before_any_actuator(monkeypatch, key):
    for bad in TIMING_BAD_VALUES[key]:
        result, log, *_ = run_standalone(
            script=draining(20.0), settings_=settings(**{key: bad}), monkeypatch=monkeypatch
        )
        assert result.stop_reason == "invalid_drain_safety_settings", (key, bad)
        assert not result.success and log.events == []

        controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0), **{key: bad})
        run_tc(controller, s, actuators)
        assert controller._stop_reason == "invalid_drain_safety_settings", (key, bad)
        assert log.events == []


def test_r4_invalid_threshold_is_not_masked_by_already_empty_shortcut(monkeypatch):
    result, log, *_ = run_standalone(
        script=lambda t: 0.0, settings_=settings(empty_threshold_liters=float("inf")), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "invalid_drain_safety_settings" and not result.success


def test_r4_validated_timeout_is_finite_and_effective(monkeypatch):
    s = settings(transfer_pump_liters_per_minute=60.0, drain_timeout_buffer_seconds=0.0, no_drain_progress_warning_seconds=1e6)
    result, *_ = run_standalone(script=lambda t: 20.0 - 0.01 * t, settings_=s, monkeypatch=monkeypatch)
    assert result.stop_reason == "calculated_drain_timeout"


def test_r5_tank_cleaning_shutdown_error_is_in_the_published_final_status(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0))
    mqtt = FakeMqtt()
    controller._mqtt_publisher = mqtt
    actuators.shutdown_errors = ["Could not switch off actuator 'transfer_pump': boom"]
    controller._safe_shutdown_and_publish(actuators)
    assert "transfer_pump" in (mqtt.payloads[-1]["error"] or "")  # schon im ersten Status


def test_r5_error_known_only_after_last_publish_is_republished(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0))
    mqtt = FakeMqtt()
    controller._mqtt_publisher = mqtt
    actuators.close_errors = ["Could not close actuator 'drain_valve_0': busy"]
    controller._safe_shutdown_and_publish(actuators)
    assert len(mqtt.payloads) == 2
    assert not mqtt.payloads[0]["error"]
    assert "drain_valve_0" in mqtt.payloads[-1]["error"]
    assert controller._phase == TankCleaningPhase.ERROR and controller._stop_reason == "cleanup_failed"


def test_r5_earlier_drain_cleanup_error_is_not_lost_by_later_shutdown_errors(monkeypatch):
    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(20.0))
    controller._record_cleanup_failure(["transfer_pump.off() failed: RuntimeError: relay stuck"])
    actuators.close_errors = ["Could not close actuator 'x': y"]
    controller._mqtt_publisher = FakeMqtt()
    controller._safe_shutdown_and_publish(actuators)
    assert "relay stuck" in controller._last_error and "'x'" in controller._last_error


def test_r5_jog_shutdown_errors_reach_the_final_published_status(monkeypatch):
    mqtts = []

    class RecordingMqtt(FakeMqtt):
        def __init__(self):
            super().__init__()
            mqtts.append(self)

    jog, log, holder = _prepare_jog(monkeypatch, mqtt_cls=RecordingMqtt)
    jog.max_seconds = 0.15
    jog.valve_settle_seconds = 0.0
    monkeypatch.setattr(FakeActuators, "close_all", lambda self: ["Could not close actuator 'transfer_pump': busy"])
    jog._run(_jog_settings())
    final = mqtts[0].payloads[-1]
    assert final["process_state"] == "Manual Drain: Stopped"
    assert "transfer_pump" in (final["error"] or "")
    assert jog._stop_reason == "cleanup_failed"


def test_r5_jog_pump_off_error_is_in_the_first_stopped_status(monkeypatch):
    mqtts = []

    class RecordingMqtt(FakeMqtt):
        def __init__(self):
            super().__init__()
            mqtts.append(self)

    jog, log, _ = _prepare_jog(monkeypatch, pump={"off_error": RuntimeError("relay stuck")}, mqtt_cls=RecordingMqtt)
    jog.max_seconds = 0.15
    jog.valve_settle_seconds = 0.0
    jog._run(_jog_settings())
    stopped = [p for p in mqtts[0].payloads if p["process_state"] == "Manual Drain: Stopped"]
    assert stopped and "relay stuck" in (stopped[0]["error"] or "")


# --- Korrekturen B1/B2 (finales Review) ----------------------------------------


def test_b1_same_timestamp_with_different_level_is_not_new():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(1), 1.0).is_new_sample
    for t, level in ((2.0, 38.0), (3.0, 37.0), (5.0, 36.0)):
        obs = wd.observe(level, ts(1), t)
        assert not obs.is_new_sample and obs.violation is None
    assert wd.latest_raw_liters == 39.0  # abweichende Pegel unter altem Zeitstempel zaehlen nicht
    violation = wd.observe(35.0, ts(1), 6.01).violation  # kein Timer-Reset (letzte neue Messung bei 1,0 s)
    assert violation is not None and violation[0] == "sensor_lost_during_drain"


@pytest.mark.parametrize(
    "bad",
    ["T1", "not-a-date", "2026-13-40T00:00:00", "12:00", "yesterday", " ", "", "2026-01-01T12:00:01Z\\n", 20260101, None],
)
def test_b1_unparsable_timestamps_are_never_new_and_run_into_the_loss_timeout(bad):
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    for t in (1.0, 2.0, 3.0, 4.0, 5.0):
        obs = wd.observe(39.0 - t, bad, t)
        assert not obs.is_new_sample and obs.violation is None
    assert wd.latest_raw_liters == 40.0
    violation = wd.observe(30.0, bad, 5.5).violation
    assert violation is not None and violation[0] == "sensor_lost_during_drain"


def test_b1_backwards_jumping_timestamp_is_not_new_and_does_not_rewind_the_reference():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(10), 1.0).is_new_sample
    assert not wd.observe(38.0, ts(5), 2.0).is_new_sample  # rueckwaerts
    assert not wd.observe(37.5, ts(10), 3.0).is_new_sample  # nicht strikt spaeter
    assert wd.observe(37.0, ts(11), 4.0).is_new_sample  # strikt spaeter als der letzte akzeptierte


def test_b1_strictly_later_valid_timestamp_is_new_and_refreshes_the_timer():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(1), 1.0).is_new_sample
    obs = wd.observe(38.0, ts(2), 5.9)
    assert obs.is_new_sample and obs.violation is None
    assert wd.observe(38.0, ts(3), 10.8).violation is None  # 4,9 s nach der letzten neuen Messung


def test_b1_naive_and_timezone_aware_stamps_are_not_comparable():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0)
    assert wd.observe(39.0, ts(1), 1.0).is_new_sample
    aware = "2026-01-01T12:00:09+00:00"
    obs = wd.observe(38.0, aware, 2.0)
    assert not obs.is_new_sample and obs.violation is None


def test_b1_start_snapshot_reread_is_not_a_new_measurement():
    wd = DrainSensorWatchdog(_limits(), started_at=0.0, start_stamp=ts(1))
    assert not wd.observe(40.0, ts(1), 0.5).is_new_sample
    assert wd.observe(39.0, ts(2), 1.5).is_new_sample


def test_b1_invalid_stamps_do_not_touch_history_empty_confirm_or_progress_standalone(monkeypatch):
    def script(t):
        if t < 3.0:
            return draining(100.0)(t)
        return {"timestamp": "T9", "mixer": {"volume_liters_calc": 0.0}}  # leer, aber ungueltige Kennung

    result, log, _, _, clock, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "sensor_lost_during_drain" and not result.success
    assert_safe_end(log)


def test_b1_same_timestamp_with_falling_level_cannot_confirm_empty(monkeypatch):
    stamp = ts(1)
    script = lambda t: {"timestamp": stamp, "mixer": {"volume_liters_calc": max(20.0 - t * 5.0, 0.0)}}
    result, log, _, _, _, _ = run_standalone(script=script, monkeypatch=monkeypatch)
    assert result.stop_reason == "sensor_lost_during_drain"  # nie "empty_by_sensor"
    assert_safe_end(log)


def test_b1_tank_cleaning_same_timestamp_with_falling_level_cannot_confirm_empty(monkeypatch):
    stamp = ts(1)
    script = lambda t: {"timestamp": stamp, "mixer": {"volume_liters_calc": max(20.0 - t * 5.0, 0.0)}}
    controller, s, actuators, log, _ = make_tc(monkeypatch, script)
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "sensor_lost_during_drain"
    assert_safe_end(log)


def test_b2_extremely_small_pump_rate_gives_non_finite_timeout_and_is_blocked():
    assert 50.0 / 1e-320 * 60.0 == float("inf")  # Reproduktion: Einzelwerte endlich, Ergebnis inf
    with pytest.raises(DrainSafetySettingsError, match="not finite"):
        DrainSafetyLimits.from_settings(
            settings(transfer_pump_liters_per_minute=1e-320, drain_timeout_buffer_seconds=180.0), 50.0
        )


def test_b2_regular_pump_rate_gives_the_documented_finite_timeout():
    limits = DrainSafetyLimits.from_settings(
        settings(transfer_pump_liters_per_minute=16.0, drain_timeout_buffer_seconds=180.0), 50.0
    )
    assert limits.expected_seconds == pytest.approx(50.0 / 16.0 * 60.0)
    assert limits.max_drain_seconds == pytest.approx(50.0 / 16.0 * 60.0 + 180.0)


def test_b2_non_positive_timeout_is_blocked_only_when_a_drain_would_run():
    # Pegel ueber der Schwelle, Berechnung unterlaeuft auf exakt 0 s (Puffer 0) -> blockiert.
    assert 5e-324 / 1e10 * 60.0 == 0.0
    with pytest.raises(DrainSafetySettingsError, match="> 0"):
        DrainSafetyLimits.from_settings(
            settings(transfer_pump_liters_per_minute=1e10, drain_timeout_buffer_seconds=0.0, empty_threshold_liters=0.0),
            5e-324,
        )
    # Tank bereits leer: kein Drain, kein Timeout noetig -> nicht blockiert
    limits = DrainSafetyLimits.from_settings(settings(drain_timeout_buffer_seconds=0.0), 0.0)
    assert limits.max_drain_seconds == 0.0


def test_b2_standalone_blocks_before_any_actuator(monkeypatch):
    s = settings(transfer_pump_liters_per_minute=1e-320)
    result, log, *_ = run_standalone(script=draining(50.0), settings_=s, monkeypatch=monkeypatch)
    assert result.stop_reason == "invalid_drain_safety_settings" and not result.success
    assert log.events == []

    result, log, *_ = run_standalone(
        script=draining(50.0), settings_=settings(transfer_pump_liters_per_minute=16.0), monkeypatch=monkeypatch
    )
    assert result.stop_reason == "empty_by_sensor"  # reguaerer Wert laeuft normal


def test_b2_tank_cleaning_blocks_before_any_actuator(monkeypatch):
    controller, s, actuators, log, _ = make_tc(
        monkeypatch, draining(50.0), transfer_pump_liters_per_minute=1e-320
    )
    run_tc(controller, s, actuators)
    assert controller._stop_reason == "invalid_drain_safety_settings"
    assert "not finite" in controller._last_error
    assert log.events == []

    controller, s, actuators, log, _ = make_tc(monkeypatch, draining(50.0), transfer_pump_liters_per_minute=16.0)
    run_tc(controller, s, actuators)
    assert controller._stop_reason is None and log.names()[:2] == ["valve.on", "pump.on"]
