"""
AP-08: kontrollierter Transfer in Solution Tank 1-4. Hardwarefrei: ActuatorManager,
MqttPublisher und ProcessRunLogger werden per monkeypatch durch Fakes ersetzt; die
Sensorwerte kommen aus einem Skript, die Uhr (fuer Timeouts) aus einer Fake-Uhr.

Geprueft werden: Freigabepruefung (reine Logik), verbindliche Zuordnung, Reihenfolge
Ventil auf -> Settle -> Stop-Recheck -> Pumpe an und Pumpe aus -> Ventil zu, alle
Abbruchpfade, Einmaligkeit der Freigabe, Zuordnung zum Mischlauf, Verriegelung,
Emergency Stop, Dashboard-Handler und Logzeilen.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import time
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import process.target_transfer as target_transfer
from gpio_config import OUTPUTS
from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA, ProcessController
from process.fill_and_measure import FillAndMeasureController
from process.manual_drain_jog import ManualDrainJog
from process.pump_prime import PumpPrimeController
from process.target_transfer import (
    PRE_TRANSFER_CLOSE_VALVES,
    TARGET_TANK_ROUTING,
    TargetTransferController,
    TransferPhase,
    TransferRequest,
    resolve_routing,
    resolve_valve_closing,
    validate_transfer_release,
)
from process.tank_cleaning import TankCleaningController
from services.process_run_logger import ProcessRunLogger
from services.settings_validation import validate_settings


# --- Fakes ---------------------------------------------------------------------------


class FakeActuator:
    def __init__(self, name: str, pin: int, events: list) -> None:
        self.name = name
        self.pin = pin
        self.events = events
        self.is_on = False
        self.fail_off = False
        self.on_hook = None

    def on(self) -> None:
        self.events.append((self.name, "on"))
        self.is_on = True
        if self.on_hook is not None:
            self.on_hook()

    def off(self) -> None:
        self.events.append((self.name, "off"))
        if self.fail_off:
            raise RuntimeError(f"{self.name} off failed")
        self.is_on = False


class Rig:
    """Sammelt Ereignisse, erzeugte Fake-Aktoren, Publisher- und Logger-Aufrufe."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.registered: dict[str, FakeActuator] = {}
        self.manager_instances = 0
        self.shutdown_calls = 0
        self.close_calls = 0
        self.published: list[dict] = []
        self.log_rows: list[dict] = []
        # Fehlerinjektion fuer das Anlegen (= Schliessen) von Ausgaengen.
        self.fail_add: set[str] = set()  # add() wirft fuer diese Aktornamen
        self.preactivated: set[str] = set()  # Ausgang meldet nach dem Anlegen faelschlich "aktiv"
        self.add_hook = None  # wird nach jedem erfolgreichen add(name) aufgerufen
        rig = self

        class FakeManager:
            def __init__(self, active_low: bool = True) -> None:
                rig.manager_instances += 1
                self.actuators: dict[str, FakeActuator] = {}

            def add(self, name: str, gpio_pin: int) -> FakeActuator:
                if name in rig.fail_add:
                    raise RuntimeError(f"GPIO {gpio_pin} nicht verfuegbar")
                actuator = FakeActuator(name, gpio_pin, rig.events)
                if name in rig.preactivated:
                    actuator.is_on = True
                self.actuators[name] = actuator
                rig.registered[name] = actuator
                # Ein angelegter Ausgang ist (real wie hier) im inaktiven Zustand = geschlossen.
                rig.events.append((name, "add"))
                if rig.add_hook is not None:
                    rig.add_hook(name)
                return actuator

            def status_payload(self) -> dict[str, bool]:
                return {name: actuator.is_on for name, actuator in self.actuators.items()}

            def safe_shutdown_all(self) -> list[str]:
                rig.shutdown_calls += 1
                errors = []
                for actuator in self.actuators.values():
                    try:
                        actuator.off()
                    except Exception as exc:
                        errors.append(str(exc))
                return errors

            def close_all(self) -> list[str]:
                rig.close_calls += 1
                return []

        class FakePublisher:
            def publish_json(self, payload: dict) -> bool:
                rig.published.append(payload)
                return True

            def close(self) -> None:
                pass

        class FakeLogger:
            def __init__(self, process_name: str, log_dir: str = "logs") -> None:
                self.process_name = process_name

            def write_step(self, **kwargs) -> None:
                rig.log_rows.append(kwargs)

            def close(self) -> None:
                pass

        self.FakeManager = FakeManager
        self.FakePublisher = FakePublisher
        self.FakeLogger = FakeLogger

    def order(self, *items: tuple[str, str]) -> bool:
        """True, wenn die Ereignisse in dieser Reihenfolge (als Teilfolge) auftraten."""
        index = 0
        for event in self.events:
            if event == items[index]:
                index += 1
                if index == len(items):
                    return True
        return False

    def first(self, event: tuple[str, str]) -> int:
        return self.events.index(event)


class ScriptedSensor:
    """Snapshot pro Aufruf; Stempel steigt je Aufruf um 1 s. level None = kein Snapshot."""

    def __init__(self, levels, on_call=None) -> None:
        self.levels = list(levels)
        self.on_call = on_call
        self.calls = 0
        self._base = datetime(2026, 1, 1, 12, 0, 0)

    def __call__(self):
        index = self.calls
        self.calls += 1
        if self.on_call is not None:
            self.on_call(index)
        level = self.levels[min(index, len(self.levels) - 1)]
        if level is None:
            return None
        return {
            "timestamp": (self._base + timedelta(seconds=index)).isoformat(),
            "mixer": {"volume_liters_calc": level},
        }


class FakeClock:
    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


def _settings(**overrides) -> dict:
    settings = {
        "hardware_execution_enabled": True,
        "required_confirmation_text": "confirmed",
        "max_mixer_liters": 185.0,
        "max_negative_level_drift_liters": 3.0,
        "level_filter_samples": 1,
        "transfer_pump_liters_per_minute": 16.0,
        "drain_timeout_buffer_seconds": 180.0,
        "empty_threshold_liters": 0.3,
        "empty_confirm_samples": 2,
        "no_drain_progress_warning_seconds": 60.0,
        "valve_settle_seconds": 0.0,
    }
    settings.update(overrides)
    return settings


def _request(**overrides) -> TransferRequest:
    values = dict(
        run_id="run-1",
        tank=1,
        target_level_liters=100.0,
        capacity_confirmed=True,
        not_evaluated_acknowledged=False,
    )
    values.update(overrides)
    return TransferRequest(**values)


# Start-Aufrufe: 0 = Start-Gate, 1 = Planung, 2 = Pre-Pump-Recheck, ab 3 = Schleife.
SUCCESS_LEVELS = [50, 50, 50, 40, 30, 20, 10, 0, 0, 0, 0, 0]


@pytest.fixture
def rig(monkeypatch) -> Rig:
    rig = Rig()
    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", rig.FakeManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", rig.FakePublisher)
    monkeypatch.setattr("services.process_run_logger.ProcessRunLogger", rig.FakeLogger)
    monkeypatch.setattr(TargetTransferController, "poll_interval_seconds", 0.0)
    return rig


def _controller(levels, on_call=None) -> tuple[TargetTransferController, ScriptedSensor]:
    sensor = ScriptedSensor(levels, on_call=on_call)
    return TargetTransferController(get_sensor_snapshot=sensor), sensor


def _run_to_end(controller: TargetTransferController, settings: dict, request, evaluation="OK") -> dict:
    result = controller.start(settings, request, evaluation)
    if result["success"]:
        controller._thread.join(timeout=10.0)
        assert not controller._thread.is_alive()
    return result


# --- Freigabepruefung (reine Logik) -----------------------------------------------------


def _validate(**overrides) -> str | None:
    args = dict(
        request=_request(),
        evaluation_status="OK",
        mixer_start_liters=50.0,
        max_mixer_liters=185.0,
    )
    args.update(overrides)
    return validate_transfer_release(**args)


def test_valid_release_passes() -> None:
    assert _validate() is None


@pytest.mark.parametrize("tank", [0, 5, -1, None, "1", 1.0, True, False, 2.5, [1]])
def test_only_tank_1_to_4_as_plain_int_is_allowed(tank) -> None:
    assert _validate(request=_request(tank=tank)) is not None


@pytest.mark.parametrize("tank", [1, 2, 3, 4])
def test_each_valid_tank_is_allowed(tank) -> None:
    assert _validate(request=_request(tank=tank)) is None


@pytest.mark.parametrize("level", [-0.1, 300.01, 1000, math.nan, math.inf, -math.inf, True, False, "100", None, [1]])
def test_target_level_must_be_finite_number_within_nominal_volume(level) -> None:
    assert _validate(request=_request(target_level_liters=level)) is not None


def test_target_level_boundaries_0_and_exact_nominal_fit() -> None:
    assert _validate(request=_request(target_level_liters=0.0)) is None
    assert _validate(request=_request(target_level_liters=250.0), mixer_start_liters=50.0) is None


@pytest.mark.parametrize("confirmed", [False, None, 1, "yes", "true", 0])
def test_capacity_confirmation_must_be_strict_true(confirmed) -> None:
    assert _validate(request=_request(capacity_confirmed=confirmed)) is not None


def test_nominal_volume_is_checked_against_target_level_plus_mixer_amount() -> None:
    assert _validate(request=_request(target_level_liters=250.01), mixer_start_liters=50.0) is not None
    assert _validate(request=_request(target_level_liters=240.0), mixer_start_liters=60.5) is not None
    assert _validate(request=_request(target_level_liters=240.0), mixer_start_liters=60.0) is None


@pytest.mark.parametrize("mixer", [None, math.nan, math.inf, -1.0, 0.0, 9.99, True])
def test_mixer_amount_below_reliable_range_or_unusable_blocks(mixer) -> None:
    assert _validate(mixer_start_liters=mixer) is not None


def test_mixer_amount_at_reliable_minimum_is_allowed() -> None:
    assert _validate(mixer_start_liters=10.0) is None


def test_mixer_amount_above_max_mixer_liters_blocks() -> None:
    assert _validate(mixer_start_liters=185.01) is not None
    assert _validate(mixer_start_liters=100.0, max_mixer_liters=None) is not None


def test_not_evaluated_needs_separate_acknowledgement() -> None:
    assert _validate(evaluation_status="NOT_EVALUATED") is not None
    assert _validate(
        evaluation_status="NOT_EVALUATED", request=_request(not_evaluated_acknowledged=False)
    ) is not None
    assert _validate(
        evaluation_status="NOT_EVALUATED", request=_request(not_evaluated_acknowledged="yes")
    ) is not None
    assert _validate(
        evaluation_status="NOT_EVALUATED", request=_request(not_evaluated_acknowledged=True)
    ) is None


@pytest.mark.parametrize("status", [None, "FAILED", "ok", "", "UNKNOWN"])
def test_missing_or_invalid_evaluation_status_blocks(status) -> None:
    assert _validate(evaluation_status=status, request=_request(not_evaluated_acknowledged=True)) is not None


def test_non_request_object_blocks() -> None:
    assert _validate(request=None) is not None
    assert _validate(request={"tank": 1}) is not None


# --- Verbindliche Zuordnung ---------------------------------------------------------------


def test_routing_matches_confirmed_assignment() -> None:
    assert TARGET_TANK_ROUTING == {1: ("valve_1", 27), 2: ("valve_2", 19), 3: ("valve_3", 24), 4: ("valve_4", 25)}
    assert resolve_routing(1) == ("valve_1", 27, 26)
    assert resolve_routing(2) == ("valve_2", 19, 26)
    assert resolve_routing(3) == ("valve_3", 24, 26)
    assert resolve_routing(4) == ("valve_4", 25, 26)


def test_gpio_config_agrees_with_confirmed_assignment() -> None:
    assert OUTPUTS["transfer_pump"] == 26
    assert [OUTPUTS[f"valve_{n}"] for n in (1, 2, 3, 4)] == [27, 19, 24, 25]


def test_drain_and_valve_6_are_never_target_valves() -> None:
    pins = {pin for _name, pin in TARGET_TANK_ROUTING.values()}
    assert OUTPUTS["valve_0_drain"] == 21 and 21 not in pins
    assert OUTPUTS["test_supply_valve_6"] == 6 and 6 not in pins
    for bad in (0, 5, 6, 7, 9):
        assert isinstance(resolve_routing(bad), str)


def test_changed_gpio_config_blocks_instead_of_switching_another_valve(monkeypatch) -> None:
    monkeypatch.setitem(OUTPUTS, "valve_1", 4)  # gueltig und eindeutig, aber nicht die bestaetigte Zuordnung
    assert isinstance(resolve_routing(1), str)
    assert resolve_routing(2) == ("valve_2", 19, 26)


def test_gpio_config_collision_with_another_valve_blocks_every_tank(monkeypatch) -> None:
    # Valve 1 auf dem Pin von Valve 6: weder Tank 1 (weicht ab) noch die uebrigen Tanks lassen sich
    # noch vorab schliessen, weil zwei Ventile denselben Pin haetten. Alles blockiert, keine Hardware.
    monkeypatch.setitem(OUTPUTS, "valve_1", 6)
    assert all(isinstance(resolve_routing(tank), str) for tank in (1, 2, 3, 4))


def test_changed_pump_gpio_blocks_all_tanks(monkeypatch) -> None:
    monkeypatch.setitem(OUTPUTS, "transfer_pump", 12)
    assert all(isinstance(resolve_routing(tank), str) for tank in (1, 2, 3, 4))


# --- Erfolgreicher Lauf -------------------------------------------------------------------


@pytest.mark.parametrize("tank", [1, 2, 3, 4])
def test_successful_transfer_opens_exactly_one_target_valve_then_pump(rig, tank) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    result = _run_to_end(controller, _settings(), _request(tank=tank))

    assert result["success"] is True
    status = controller.get_status()
    assert status["phase"] == TransferPhase.FINISHED
    assert status["result"] == "COMPLETED"
    assert status["stop_reason"] == "empty_by_sensor_zero"
    assert status["last_error"] is None
    assert status["target_tank"] == tank

    valve_name, valve_pin = TARGET_TANK_ROUTING[tank]
    # Pumpe plus alle dokumentierten Ventile (Valve 0 bis 6) wurden angelegt; das Zielventil mit dem
    # bestaetigten Pin. Eingeschaltet wurden aber nur Zielventil und Pumpe, sonst nichts.
    assert sorted(rig.registered) == sorted(["transfer_pump", *(name for name, _key in PRE_TRANSFER_CLOSE_VALVES)])
    assert rig.registered["transfer_pump"].pin == 26
    assert rig.registered[valve_name].pin == valve_pin
    assert [name for name, action in rig.events if action == "on"] == [valve_name, "transfer_pump"]

    assert rig.order((valve_name, "on"), ("transfer_pump", "on"))
    assert rig.first((valve_name, "on")) < rig.first(("transfer_pump", "on"))
    # Stopp: Pumpe aus, DANN Ventil zu.
    assert rig.first(("transfer_pump", "off")) < rig.first((valve_name, "off"))
    assert not rig.registered["transfer_pump"].is_on and not rig.registered[valve_name].is_on


def test_status_reports_sensor_based_progress_and_logs_release(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request(tank=3, target_level_liters=42.5))

    status = controller.get_status()
    assert status["start_mixer_liters"] == 50
    assert status["transferred_liters_sensor_calc"] == pytest.approx(50.0)
    assert status["target_level_liters_operator_input"] == 42.5

    states = [row["state"] for row in rig.log_rows]
    assert states[0] == "TARGET_TRANSFER_RELEASED"
    assert "TARGET_TRANSFER_VALVE_OPEN" in states and "TARGET_TRANSFER_RUNNING" in states
    assert states[-1] == "TARGET_TRANSFER_STOPPED"
    first_extra = rig.log_rows[0]["extra"]
    assert first_extra["transfer_target_tank"] == 3
    assert first_extra["transfer_run_id"] == "run-1"
    release = json.loads(first_extra["transfer_release"])
    assert release["target_level_liters_operator_input"] == 42.5
    assert release["operator_confirmed_free_capacity_incl_reserve"] is True
    assert release["nominal_capacity_liters"] == 300.0

    # Dashboard-/MQTT-Status mit Aktorzustand und Transferstatus.
    published_states = [payload["process_state"] for payload in rig.published]
    assert "TARGET_TRANSFER_RUNNING" in published_states
    assert published_states[-1] == "TARGET_TRANSFER_STOPPED"
    assert rig.published[-1]["target_transfer"]["result"] == "COMPLETED"
    assert rig.published[-1]["actuators"]["transfer_pump"] is False
    assert rig.published[-1]["actuators"]["valve_3"] is False


def test_not_evaluated_run_records_acknowledgement_not_quality(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    result = _run_to_end(
        controller, _settings(), _request(not_evaluated_acknowledged=True), evaluation="NOT_EVALUATED"
    )

    assert result["success"] is True
    status = controller.get_status()
    assert status["evaluation_status"] == "NOT_EVALUATED"
    assert status["not_evaluated_acknowledged"] is True
    release = json.loads(rig.log_rows[0]["extra"]["transfer_release"])
    assert release["evaluation_status"] == "NOT_EVALUATED"
    assert release["not_evaluated_acknowledged"] is True


# --- Vorab-Schliessen aller dokumentierten Ventile ---------------------------------------------

EXPECTED_CLOSE_VALVES = {
    "drain_valve_0": 21,
    "valve_1": 27,
    "valve_2": 19,
    "valve_3": 24,
    "valve_4": 25,
    "valve_5": 5,
    "supply_valve_6": 6,
}
CLOSE_VALVE_NAMES = [name for name, _key in PRE_TRANSFER_CLOSE_VALVES]


def _switched_on(rig) -> list[str]:
    return [name for name, action in rig.events if action == "on"]


def test_close_list_is_exactly_the_documented_valve_0_to_6_with_gpio_config_pins() -> None:
    closing = resolve_valve_closing("valve_1", 27, 26)

    assert closing == tuple(EXPECTED_CLOSE_VALVES.items())  # inkl. Reihenfolge: Drain zuerst
    # Die Pins kommen aus gpio_config.OUTPUTS.
    assert {name: OUTPUTS[key] for name, key in PRE_TRANSFER_CLOSE_VALVES} == EXPECTED_CLOSE_VALVES


def test_close_list_excludes_pumps_contactors_and_undocumented_valves() -> None:
    closing_pins = {pin for _name, pin in resolve_valve_closing("valve_1", 27, 26)}
    never_touched = (
        "transfer_pump", "mixer_refill_pump", "sensor_circulation_pump", "mixing_circulation_pump",
        "contactor_0", "contactor_5", "valve_7", "valve_8", "valve_9",
    )

    assert closing_pins.isdisjoint({OUTPUTS[key] for key in never_touched})
    # Jedes Zielventil steht in der Liste.
    assert {name for name, _pin in TARGET_TANK_ROUTING.values()} <= set(CLOSE_VALVE_NAMES)


def test_target_valve_missing_from_close_list_blocks() -> None:
    assert isinstance(resolve_valve_closing("valve_7", 16, 26), str)
    assert isinstance(resolve_valve_closing("valve_1", 99, 26), str)


@pytest.mark.parametrize("tank", [1, 2, 3, 4])
def test_all_documented_valves_are_closed_before_target_valve_opens_and_pump_starts(rig, tank) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request(tank=tank))

    assert controller.get_status()["phase"] == TransferPhase.FINISHED
    valve_name, _pin = TARGET_TANK_ROUTING[tank]
    # Pumpe zuerst angelegt, danach alle sieben Ventile in fester Reihenfolge (Drain zuerst).
    assert [name for name, action in rig.events if action == "add"] == ["transfer_pump", *CLOSE_VALVE_NAMES]
    # Jedes Ventil ist geschlossen angelegt, bevor das Zielventil oeffnet und bevor die Pumpe foerdert.
    for name in CLOSE_VALVE_NAMES:
        assert rig.first((name, "add")) < rig.first((valve_name, "on")), name
        assert rig.first((name, "add")) < rig.first(("transfer_pump", "on")), name
    assert rig.first((valve_name, "on")) < rig.first(("transfer_pump", "on"))
    # Kein anderes Ventil wurde je eingeschaltet; am Ende ist alles aus.
    assert _switched_on(rig) == [valve_name, "transfer_pump"]
    assert all(not actuator.is_on for actuator in rig.registered.values())


def test_status_and_log_report_every_closed_valve_from_the_first_row(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request(tank=3))

    all_closed = {"transfer_pump": False, **{name: False for name in CLOSE_VALVE_NAMES}}
    assert rig.published[0]["process_state"] == "TARGET_TRANSFER_RELEASED"
    assert rig.published[0]["actuators"] == all_closed
    assert rig.log_rows[0]["state"] == "TARGET_TRANSFER_RELEASED"
    assert rig.log_rows[0]["actuator_status"] == all_closed


def test_journal_lists_every_closed_valve_in_order(rig, capsys) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request())

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[CLOSE]")]
    assert lines == [f"[CLOSE] {name} | GPIO {pin}" for name, pin in EXPECTED_CLOSE_VALVES.items()]


@pytest.mark.parametrize("failing", CLOSE_VALVE_NAMES)
def test_valve_that_cannot_be_closed_aborts_before_anything_is_switched_on(rig, failing) -> None:
    rig.fail_add.add(failing)
    controller, _sensor = _controller(SUCCESS_LEVELS)

    result = _run_to_end(controller, _settings(), _request(tank=2))  # valve_2 ist hier auch Zielventil

    assert result["success"] is True  # der Start wurde angenommen, der Thread bricht ab
    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR and status["result"] == "ERROR"
    assert status["stop_reason"] == "valve_close_failed"
    assert failing in status["last_error"] and "Weder Zielventil noch Pumpe" in status["last_error"]
    assert status["fault_latched"] is True
    # Weder Zielventil noch Pumpe noch ein anderes Ventil wurde eingeschaltet; alles wird freigegeben.
    assert _switched_on(rig) == []
    assert rig.shutdown_calls == 1 and rig.close_calls == 1
    assert all(not actuator.is_on for actuator in rig.registered.values())
    assert not controller.is_run_used("run-1")  # Freigabe des Mischlaufs bleibt erhalten
    blocked = controller.start(_settings(), _request(tank=2), "OK")
    assert blocked["success"] is False and "quittiert" in blocked["message"]


@pytest.mark.parametrize("reported_active", ["drain_valve_0", "valve_3", "supply_valve_6"])
def test_valve_not_confirmed_inactive_aborts_before_anything_is_switched_on(rig, reported_active) -> None:
    rig.preactivated.add(reported_active)
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request(tank=4))

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "valve_close_failed"
    assert "nicht als geschlossen bestaetigt" in status["last_error"] and reported_active in status["last_error"]
    assert _switched_on(rig) == []
    assert rig.shutdown_calls == 1 and rig.close_calls == 1


def test_unreadable_valve_state_aborts_before_anything_is_switched_on(rig) -> None:
    original = rig.FakeManager.status_payload
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:  # erster Aufruf = Pruefung der geschlossenen Ventile
            raise RuntimeError("status boom")
        return original(self)

    rig.FakeManager.status_payload = flaky
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "valve_close_failed"
    assert "nicht lesbar" in status["last_error"]
    assert _switched_on(rig) == []


@pytest.mark.parametrize("how", ["request_stop", "event_only"])
def test_stop_during_pre_close_never_opens_the_target_valve(rig, how) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    def stop_after_last_valve(name: str) -> None:
        if name == CLOSE_VALVE_NAMES[-1]:  # direkt vor dem Oeffnen des Zielventils
            if how == "request_stop":
                controller.request_stop("stopped_by_user")
            else:
                controller._stop_event.set()

    rig.add_hook = stop_after_last_valve

    _run_to_end(controller, _settings(), _request())

    assert _switched_on(rig) == []
    status = controller.get_status()
    assert status["phase"] == TransferPhase.STOPPED
    assert status["stop_reason"] == ("stopped_by_user" if how == "request_stop" else "stopped_before_valve_open")
    assert rig.shutdown_calls == 1 and rig.close_calls == 1
    assert not controller.is_run_used("run-1")


@pytest.mark.parametrize(
    "key, bad_pin",
    [
        ("valve_5", 26),  # Pin der Transferpumpe
        ("valve_5", 27),  # Pin von Valve 1
        ("test_supply_valve_6", 21),  # Pin des Drain
        ("valve_5", True),
        ("valve_5", "5"),
        ("valve_5", 5.0),
        ("valve_5", None),
        ("valve_5", -1),
        ("valve_5", 28),
    ],
)
def test_unusable_valve_pins_block_every_tank_before_hardware(rig, monkeypatch, key, bad_pin) -> None:
    monkeypatch.setitem(OUTPUTS, key, bad_pin)
    controller, _sensor = _controller([50])

    assert all(isinstance(resolve_routing(tank), str) for tank in (1, 2, 3, 4))
    result = controller.start(_settings(), _request(), "OK")

    assert result["success"] is False
    assert rig.manager_instances == 0 and controller._thread is None
    assert not controller.is_run_used("run-1")


def test_missing_valve_in_gpio_config_blocks_every_tank_before_hardware(rig, monkeypatch) -> None:
    monkeypatch.delitem(OUTPUTS, "valve_5")
    controller, _sensor = _controller([50])

    assert all(isinstance(resolve_routing(tank), str) for tank in (1, 2, 3, 4))
    assert controller.start(_settings(), _request(), "OK")["success"] is False
    assert rig.manager_instances == 0 and controller._thread is None


def test_pump_off_failure_keeps_every_other_valve_closed_and_never_switches_them_on(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    _make_pump_fail(rig)

    _run_to_end(controller, _settings(), _request(tank=2))

    assert controller.get_status()["stop_reason"] == "pump_off_failed"
    assert _switched_on(rig) == ["valve_2", "transfer_pump"]
    held_open = [name for name, actuator in rig.registered.items() if actuator.is_on]
    assert sorted(held_open) == ["transfer_pump", "valve_2"]  # nur Pumpe (unbestaetigt) und Zielventil
    assert not any(action == "off" for name, action in rig.events if name != "transfer_pump")


# --- Einmalige Freigabe / blockierter Start -----------------------------------------------



def test_successfully_completed_transfer_cannot_be_started_again(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS * 2)

    first = _run_to_end(controller, _settings(), _request())
    instances_after_first = rig.manager_instances
    second = controller.start(_settings(), _request(), "OK")

    assert first["success"] is True
    assert second["success"] is False
    assert "erfolgreich abgeschlossen" in second["message"]
    assert controller.is_run_used("run-1")
    assert rig.manager_instances == instances_after_first  # keine weitere Hardware


def test_failed_transfer_does_not_consume_release_but_latches_until_acknowledged(rig) -> None:
    controller, sensor = _controller([50, 50, 50, 60])  # Pegelanstieg -> Fehler

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["fault_latched"] is True
    assert not controller.is_run_used("run-1")  # Mischlauf bleibt wiederverwendbar

    # Aktive Stoerung: kein neuer Start, bevor quittiert wurde.
    blocked = controller.start(_settings(), _request(), "OK")
    assert blocked["success"] is False and "quittiert" in blocked["message"]

    assert controller.acknowledge()["success"] is True
    sensor.levels = SUCCESS_LEVELS
    sensor.calls = 0
    retry = _run_to_end(controller, _settings(), _request())
    assert retry["success"] is True
    assert controller.get_status()["phase"] == TransferPhase.FINISHED



def test_acknowledge_keeps_successful_run_consumed(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS * 2)
    _run_to_end(controller, _settings(), _request())

    assert controller.acknowledge()["success"] is True

    assert controller.get_status()["phase"] == TransferPhase.IDLE
    assert controller.start(_settings(), _request(), "OK")["success"] is False


def test_blocked_start_touches_no_hardware_and_keeps_release(rig) -> None:
    controller, _sensor = _controller([50])

    blocked = controller.start(_settings(), _request(target_level_liters=290.0), "OK")  # 290 + 50 > 300

    assert blocked["success"] is False
    assert rig.manager_instances == 0
    assert controller._thread is None
    assert not controller.is_run_used("run-1")
    # Nach Korrektur der Eingabe ist dieselbe Freigabe noch verwendbar.
    assert controller.start(_settings(), _request(target_level_liters=100.0), "OK")["success"] is True
    controller._thread.join(timeout=10.0)


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_hardware_execution_flag_must_be_strict_true(rig, flag) -> None:
    controller, _sensor = _controller([50])

    result = controller.start(_settings(hardware_execution_enabled=flag), _request(), "OK")

    assert result["success"] is False
    assert rig.manager_instances == 0 and controller._thread is None


def test_no_sensor_snapshot_blocks_start(rig) -> None:
    controller, _sensor = _controller([None])

    assert controller.start(_settings(), _request(), "OK")["success"] is False
    assert rig.manager_instances == 0


def test_snapshot_without_valid_timestamp_blocks_start(rig) -> None:
    controller = TargetTransferController(
        get_sensor_snapshot=lambda: {"mixer": {"volume_liters_calc": 50.0}}
    )

    result = controller.start(_settings(), _request(), "OK")

    assert result["success"] is False
    assert rig.manager_instances == 0


def test_mixer_below_10_liters_blocks_start(rig) -> None:
    controller, _sensor = _controller([9.0])

    result = controller.start(_settings(), _request(), "OK")

    assert result["success"] is False and "10" in result["message"]
    assert rig.manager_instances == 0


def test_unusable_safety_settings_block_before_hardware(rig) -> None:
    for bad in (
        {"transfer_pump_liters_per_minute": 0.0},
        {"transfer_pump_liters_per_minute": math.nan},
        {"empty_confirm_samples": 0},
        {"valve_settle_seconds": -1.0},
        {"max_mixer_liters": 40.0},  # Start 50 L liegt ueber dem Maximum
    ):
        controller, _sensor = _controller([50])
        assert controller.start(_settings(**bad), _request(), "OK")["success"] is False, bad
        assert rig.manager_instances == 0


# --- Stop-Pfade ---------------------------------------------------------------------------


def test_stop_during_valve_settle_never_starts_pump(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    original_add = rig.FakeManager.add

    def add(self, name, gpio_pin):
        actuator = original_add(self, name, gpio_pin)
        if name == "valve_1":
            actuator.on_hook = lambda: controller.request_stop("emergency_stop")
        return actuator

    rig.FakeManager.add = add

    _run_to_end(controller, _settings(), _request())

    assert ("transfer_pump", "on") not in rig.events
    assert ("valve_1", "on") in rig.events and ("valve_1", "off") in rig.events
    status = controller.get_status()
    assert status["phase"] == TransferPhase.STOPPED
    assert status["stop_reason"] == "emergency_stop"


def test_pump_starts_only_after_the_valve_settle_time_has_elapsed(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    stamps: dict[str, float] = {}
    original_add = rig.FakeManager.add

    def add(self, name, gpio_pin):
        actuator = original_add(self, name, gpio_pin)
        if name in ("valve_2", "transfer_pump"):
            actuator.on_hook = lambda name=name: stamps.setdefault(name, time.monotonic())
        return actuator

    rig.FakeManager.add = add

    _run_to_end(controller, _settings(valve_settle_seconds=0.4), _request(tank=2))

    assert controller.get_status()["phase"] == TransferPhase.FINISHED
    # Das Zielventil oeffnet zuerst; die Pumpe laeuft erst nach der Settle-Zeit an (Toleranz fuer Timer-Jitter).
    assert stamps["transfer_pump"] - stamps["valve_2"] >= 0.35


def test_stop_between_settle_and_pump_never_starts_pump(rig) -> None:
    holder: dict = {}
    controller, _sensor = _controller(
        SUCCESS_LEVELS, on_call=lambda i: holder["c"].request_stop("stopped_by_user") if i == 2 else None
    )
    holder["c"] = controller

    _run_to_end(controller, _settings(), _request())

    assert ("transfer_pump", "on") not in rig.events
    assert controller.get_status()["phase"] == TransferPhase.STOPPED
    assert not rig.registered["valve_1"].is_on


def test_stop_before_hardware_is_touched(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    with controller._lock:
        plan = controller._prepare_plan_locked(_settings(), _request(), "OK")
    assert not isinstance(plan, str)
    controller._plan = plan
    controller._stop_event.set()  # Stop liegt vor, bevor der Thread die Hardware anfasst

    controller._run(_settings())

    assert rig.manager_instances == 0
    assert rig.events == []
    status = controller.get_status()
    assert status["phase"] == TransferPhase.STOPPED
    assert status["stop_reason"] == "stopped_before_start"


def test_stop_while_pumping_turns_pump_off_before_valve(rig) -> None:
    holder: dict = {}
    controller, _sensor = _controller(
        SUCCESS_LEVELS, on_call=lambda i: holder["c"].request_stop("emergency_stop") if i == 5 else None
    )
    holder["c"] = controller

    _run_to_end(controller, _settings(), _request(tank=2))

    assert ("transfer_pump", "on") in rig.events
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_2", "off"))
    status = controller.get_status()
    assert status["phase"] == TransferPhase.STOPPED
    assert status["result"] == "STOPPED"
    assert status["stop_reason"] == "emergency_stop"
    assert not rig.registered["transfer_pump"].is_on and not rig.registered["valve_2"].is_on


def test_controller_stop_method_during_run(rig) -> None:
    holder: dict = {}
    controller, _sensor = _controller(
        SUCCESS_LEVELS, on_call=lambda i: holder["c"].request_stop("stopped_by_user") if i == 4 else None
    )
    holder["c"] = controller

    _run_to_end(controller, _settings(), _request())

    assert controller.get_status()["stop_reason"] == "stopped_by_user"
    assert controller.stop()["success"] is True  # idempotent nach Ende


# --- Fehlerpfade (Sensor, Laufzeit, Fortschritt) ---------------------------------------------


def test_missing_mixer_level_right_before_pump_blocks_pump(rig) -> None:
    controller, _sensor = _controller([50, 50, None])

    _run_to_end(controller, _settings(), _request())

    assert ("transfer_pump", "on") not in rig.events
    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "missing_mixer_level_before_pump"
    assert not rig.registered["valve_1"].is_on


def test_sensor_loss_during_transfer_stops_fail_closed(rig, monkeypatch) -> None:
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    controller, _sensor = _controller([50, 50, 50, 45, None])

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "sensor_lost_during_drain"
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))


def test_level_rise_during_transfer_stops(rig) -> None:
    controller, _sensor = _controller([50, 50, 50, 60])

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "level_rise_during_drain"


def test_no_progress_window_stops_transfer(rig, monkeypatch) -> None:
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    controller, _sensor = _controller([50])  # Pegel haengt bei 50 L

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "no_drain_progress"
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))


def test_maximum_runtime_stops_transfer(rig, monkeypatch) -> None:
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    controller, _sensor = _controller([50])
    # Fortschrittsfenster ausser Kraft, damit das berechnete Maximum greift:
    # 50 L / 16 L/min * 60 + 180 s = 367.5 s.
    settings = _settings(no_drain_progress_warning_seconds=1.0e9)

    _run_to_end(controller, settings, _request())

    status = controller.get_status()
    assert status["max_seconds"] == pytest.approx(367.5)
    assert status["stop_reason"] == "calculated_transfer_timeout"
    assert status["phase"] == TransferPhase.ERROR
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))


def test_repeated_snapshot_does_not_confirm_empty(rig, monkeypatch) -> None:
    """Derselbe Snapshot (gleicher Stempel) ist keine neue Messung: keine Leer-Bestaetigung."""
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    calls = {"n": 0}

    def sensor():
        calls["n"] += 1
        level = 50.0 if calls["n"] <= 3 else 0.0
        stamp = datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=min(calls["n"], 3))
        return {"timestamp": stamp.isoformat(), "mixer": {"volume_liters_calc": level}}

    controller = TargetTransferController(get_sensor_snapshot=sensor)

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] != "empty_by_sensor"


def test_sensor_exception_during_transfer_fails_closed(rig) -> None:
    def explode(i):
        if i == 4:
            raise RuntimeError("sensor boom")

    controller, _sensor = _controller(SUCCESS_LEVELS, on_call=explode)

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert "sensor boom" in status["last_error"]
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))



def _make_pump_fail(rig, fail: bool = True) -> None:
    original_add = rig.FakeManager.add

    def add(self, name, gpio_pin):
        actuator = original_add(self, name, gpio_pin)
        if name == "transfer_pump":
            actuator.fail_off = fail
        return actuator

    rig.FakeManager.add = add


def test_pump_off_failure_is_critical_valve_stays_open_and_actuators_are_held(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    _make_pump_fail(rig)

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR  # nie als sicher beendet dargestellt
    assert status["result"] == "ERROR"
    assert status["stop_reason"] == "pump_off_failed"
    assert status["pump_off_failed"] is True and status["fault_latched"] is True
    assert "NOT-AUS" in status["last_error"] and "NICHT bestaetigt" in status["last_error"]
    assert status["cleanup_errors"]
    # Ventil wird NICHT gegen eine moeglicherweise laufende Pumpe geschlossen, nichts freigegeben.
    assert ("valve_1", "off") not in rig.events
    assert rig.registered["valve_1"].is_on is True
    assert rig.shutdown_calls == 0 and rig.close_calls == 0
    assert controller.hardware_fault_pending() is True
    assert not controller.is_run_used("run-1")
    assert rig.published[-1]["process_state"] == "TARGET_TRANSFER_PUMP_OFF_FAILED"
    assert "NOT-AUS" in rig.published[-1]["error"]


def test_pump_off_failure_blocks_new_start_until_cleanup_succeeds_and_is_acknowledged(rig) -> None:
    controller, sensor = _controller(SUCCESS_LEVELS * 3)
    _make_pump_fail(rig)
    _run_to_end(controller, _settings(), _request())

    blocked = controller.start(_settings(), _request(), "OK")
    assert blocked["success"] is False and "quittiert" in blocked["message"]

    # Pumpe laesst sich weiter nicht abschalten: Quittierung wird verweigert, Ventil bleibt offen.
    refused = controller.acknowledge()
    assert refused["success"] is False and "NOT-AUS" in refused["message"]
    assert controller.hardware_fault_pending() is True
    assert ("valve_1", "off") not in rig.events

    # Stoerung behoben (z. B. nach NOT-AUS): Abschalten gelingt, Pumpe zuerst, dann Ventil.
    rig.registered["transfer_pump"].fail_off = False
    assert controller.acknowledge()["success"] is True
    assert controller.hardware_fault_pending() is False
    assert controller.get_status()["pump_off_failed"] is False
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))
    assert rig.close_calls == 1

    # Erst jetzt wieder ein Start (neue Freigabe, neue Messung).
    sensor.calls = 0
    _make_pump_fail(rig, fail=False)
    assert _run_to_end(controller, _settings(), _request())["success"] is True


def test_retry_hardware_cleanup_never_closes_valve_while_pump_off_fails(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    _make_pump_fail(rig)
    _run_to_end(controller, _settings(), _request())

    result = controller.retry_hardware_cleanup()

    assert result["success"] is False
    assert ("valve_1", "off") not in rig.events
    assert controller.hardware_fault_pending() is True
    assert controller.retry_hardware_cleanup()["success"] is False

    rig.registered["transfer_pump"].fail_off = False
    assert controller.retry_hardware_cleanup()["success"] is True
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))
    assert controller.hardware_fault_pending() is False


def test_retry_hardware_cleanup_without_held_actuators_is_noop(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)

    assert controller.retry_hardware_cleanup()["success"] is True
    assert rig.events == []


def test_pump_off_failure_after_sensor_error_still_critical(rig, monkeypatch) -> None:
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    controller, _sensor = _controller([50, 50, 50, 45, None])
    _make_pump_fail(rig)

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["stop_reason"] == "pump_off_failed"
    assert ("valve_1", "off") not in rig.events


def test_valve_close_failure_is_reported_after_pump_is_off(rig) -> None:
    controller, _sensor = _controller(SUCCESS_LEVELS)
    original_add = rig.FakeManager.add

    def add(self, name, gpio_pin):
        actuator = original_add(self, name, gpio_pin)
        if name == "valve_1":
            actuator.fail_off = True
        return actuator

    rig.FakeManager.add = add

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert status["stop_reason"] == "cleanup_failed"
    assert status["fault_latched"] is True and status["pump_off_failed"] is False
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))
    assert not controller.is_run_used("run-1")  # nicht sauber abgeschlossen


def test_actuator_manager_failure_is_reported_without_hardware(rig, monkeypatch) -> None:
    class Boom:
        def __init__(self, active_low=True):
            raise RuntimeError("Hardware wird bereits verwendet")

    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", Boom)
    controller, _sensor = _controller(SUCCESS_LEVELS)

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.ERROR
    assert "Hardware" in status["last_error"]
    assert rig.events == []


# --- Entleerung: erste plausible 0 L beendet den Transfer --------------------------------------------


def test_first_fresh_zero_ends_transfer_immediately_pump_off_before_valve(rig) -> None:
    levels = [50, 50, 50, 40, 20, 8, 0, 30, 30, 30]  # nach der ersten Null schwanken die Werte wieder
    controller, sensor = _controller(levels)

    _run_to_end(controller, _settings(empty_confirm_samples=5), _request())

    status = controller.get_status()
    assert status["phase"] == TransferPhase.FINISHED
    assert status["stop_reason"] == "empty_by_sensor_zero"
    assert status["current_mixer_liters"] == 0.0
    assert sensor.calls == 7  # keine weitere Messung nach der ersten Null
    assert rig.first(("transfer_pump", "off")) < rig.first(("valve_1", "off"))
    assert controller.is_run_used("run-1")


def test_single_zero_in_high_level_does_not_end_transfer(rig) -> None:
    # Eine Null bei noch hohem Pegel ist unplausibel; erst die Null im unteren Bereich beendet.
    levels = [50, 50, 50, 40, 0, 35, 25, 15, 5, 0, 20, 20]
    controller, sensor = _controller(levels)

    _run_to_end(controller, _settings(), _request())

    assert controller.get_status()["phase"] == TransferPhase.FINISHED
    assert sensor.calls == 10  # beendet erst mit dem Messwert 0 an Index 9, nicht bei Index 4


def test_repeated_implausible_zeros_do_not_make_each_other_plausible(rig) -> None:
    levels = [50, 50, 50, 0, 0, 0, 40, 30, 20, 10, 0, 0]
    controller, sensor = _controller(levels)

    _run_to_end(controller, _settings(), _request())

    assert controller.get_status()["phase"] == TransferPhase.FINISHED
    assert sensor.calls == 11  # nur die Null nach dem plausiblen Verlauf bis 10 L zaehlt


def test_zero_directly_after_start_level_above_reliable_range_is_ignored(rig, monkeypatch) -> None:
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    controller, _sensor = _controller([12, 12, 12, 0, 0, 0, 0])

    _run_to_end(controller, _settings(), _request())

    status = controller.get_status()
    assert status["stop_reason"] != "empty_by_sensor_zero"  # kein Erfolg durch Null aus 12 L
    assert status["phase"] == TransferPhase.ERROR


def test_zero_after_normal_low_course_from_small_start_amount_is_accepted(rig) -> None:
    controller, _sensor = _controller([12, 12, 12, 11, 10, 6, 0])

    _run_to_end(controller, _settings(), _request())

    assert controller.get_status()["phase"] == TransferPhase.FINISHED


def test_repeated_snapshot_with_zero_is_not_a_fresh_measurement(rig, monkeypatch) -> None:
    monkeypatch.setattr(target_transfer, "time", FakeClock(step=1.0))
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    calls = {"n": 0}

    def sensor():
        calls["n"] += 1
        level = 8.0 if calls["n"] <= 3 else 0.0
        # ab Aufruf 4 immer derselbe (alte) Stempel wie zuvor: keine neue Messung
        return {"timestamp": (stamp + timedelta(seconds=min(calls["n"], 3))).isoformat(),
                "mixer": {"volume_liters_calc": level}}

    controller = TargetTransferController(get_sensor_snapshot=sensor)

    _run_to_end(controller, _settings(), _request(target_level_liters=10.0))

    assert controller.get_status()["stop_reason"] != "empty_by_sensor_zero"


def test_other_safety_checks_stay_active_with_zero_logic(rig) -> None:
    # Pegelanstieg ueber Start + Drift wird weiter erkannt, auch nach einer Null im Verlauf.
    controller, _sensor = _controller([50, 50, 50, 40, 0, 70])

    _run_to_end(controller, _settings(), _request())

    assert controller.get_status()["stop_reason"] == "level_rise_during_drain"


# --- Wiederholung eines abgebrochenen Transfers ------------------------------------------------------


def test_user_stopped_transfer_can_be_retried_with_fresh_measurement_and_new_release(rig) -> None:
    holder: dict = {}
    controller, sensor = _controller(
        [50, 50, 50, 40, 30, 30, 30], on_call=lambda i: holder["c"].request_stop("stopped_by_user") if i == 4 else None
    )
    holder["c"] = controller
    _run_to_end(controller, _settings(), _request(tank=1))
    assert controller.get_status()["phase"] == TransferPhase.STOPPED
    assert controller.fault_latched() is False  # normaler Benutzerstopp ist keine Stoerung
    assert not controller.is_run_used("run-1")

    # Kein automatischer Neustart; Neustart nur mit neuer Freigabe. Verbleibender Mixerstand ~30 L
    # wird neu erfasst, der (hoehere) Zieltank-Stand neu geprueft: 280 + 30 > 300 -> blockiert.
    sensor.levels = [30, 30, 30, 20, 10, 0]
    sensor.calls = 0
    sensor.on_call = None
    too_full = controller.start(_settings(), _request(tank=2, target_level_liters=280.0), "OK")
    assert too_full["success"] is False and "Nennvolumen" in too_full["message"]

    sensor.calls = 0
    retry = _run_to_end(controller, _settings(), _request(tank=2, target_level_liters=100.0))
    assert retry["success"] is True
    status = controller.get_status()
    assert status["phase"] == TransferPhase.FINISHED
    assert status["target_tank"] == 2
    assert status["start_mixer_liters"] == 30  # neu erfasst, nicht der alte Startwert
    assert controller.is_run_used("run-1")
    assert controller.start(_settings(), _request(tank=2), "OK")["success"] is False


def test_retry_needs_full_release_again(rig) -> None:
    controller, sensor = _controller([50, 50, 50, 60])
    _run_to_end(controller, _settings(), _request())  # endet im Fehler, nicht abgeschlossen
    controller.acknowledge()
    sensor.levels = [40]
    sensor.calls = 0

    assert controller.start(_settings(), _request(capacity_confirmed=False), "OK")["success"] is False
    assert controller.start(_settings(), _request(tank=7), "OK")["success"] is False
    assert controller.start(_settings(), _request(), "NOT_EVALUATED")["success"] is False
    assert controller.start(_settings(), _request(target_level_liters=None), "OK")["success"] is False


def test_remaining_amount_below_reliable_range_blocks_retry(rig) -> None:
    controller, sensor = _controller([50, 50, 50, 60])
    _run_to_end(controller, _settings(), _request())
    controller.acknowledge()
    sensor.levels = [6.0]
    sensor.calls = 0

    result = controller.start(_settings(), _request(), "OK")

    assert result["success"] is False and "10" in result["message"]


def test_emergency_stop_of_transfer_latches_fault(rig) -> None:
    holder: dict = {}
    controller, _sensor = _controller(
        SUCCESS_LEVELS, on_call=lambda i: holder["c"].request_stop("emergency_stop") if i == 5 else None
    )
    holder["c"] = controller

    _run_to_end(controller, _settings(), _request())

    assert controller.get_status()["phase"] == TransferPhase.STOPPED
    assert controller.fault_latched() is True
    assert controller.start(_settings(), _request(), "OK")["success"] is False
    assert controller.acknowledge()["success"] is True
    assert controller.fault_latched() is False


# --- Prozesslog-Spalten --------------------------------------------------------------------


def test_process_run_logger_has_transfer_and_valve_columns(tmp_path) -> None:
    logger = ProcessRunLogger(process_name="target_transfer", log_dir=str(tmp_path))
    logger.write_step(
        state="TARGET_TRANSFER_RUNNING",
        error=None,
        snapshot={"mixer": {"volume_liters_calc": 12.0}},
        actuator_status={"transfer_pump": True, "valve_4": True, "valve_1": False},
        extra={"transfer_target_tank": 4, "transfer_run_id": "abc", "transfer_release": "{}"},
    )
    logger.close()

    with logger.file_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["valve_4"] == "True" and row["valve_1"] == "False" and row["valve_2"] == ""
    assert row["transfer_target_tank"] == "4"
    assert row["transfer_run_id"] == "abc"
    assert row["transfer_pump"] == "True"


# --- Zuordnung zum Mischlauf, Verriegelung, Emergency Stop (ProcessController) ------------------


def _stub_runs(monkeypatch) -> None:
    def waits(self, settings) -> None:
        self._stop_event.wait(5.0)

    for cls in (ManualDrainJog, TankCleaningController, PumpPrimeController, TargetTransferController):
        monkeypatch.setattr(cls, "_run", waits)
    monkeypatch.setattr(
        PumpPrimeController, "start", lambda self, settings: {"success": True, "message": "stub"}
    )


def _process_controller(monkeypatch, mixer_liters: float = 60.0) -> ProcessController:
    _stub_runs(monkeypatch)
    sensor = ScriptedSensor([mixer_liters])
    pc = ProcessController(get_sensor_snapshot=sensor)
    pc.load_settings = lambda: _settings()
    pc.load_tank_cleaning_settings = lambda: {
        "hardware_execution_enabled": True,
        "required_confirmation_text": "confirmed",
    }
    return pc


def _finish_mixing_run(pc: ProcessController, status="OK", run_id="run-1", **machine_overrides) -> None:
    machine = SimpleNamespace(
        state=SimpleNamespace(name="FINISHED"),
        error_message=None,
        final_evaluation_status=status,
        final_evaluation={},
        start_mixer_liters=0.0,
        last_added_liters=0.0,
    )
    for key, value in machine_overrides.items():
        setattr(machine, key, value)
    pc.fill_and_measure.state_machine = machine
    pc.fill_and_measure._run_id = run_id


def _finish(pc: ProcessController) -> None:
    pc.shutdown()


def test_release_status_available_only_for_finished_error_free_run(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        assert pc.get_status()["transfer_release"]["available"] is False

        _finish_mixing_run(pc, status="NOT_EVALUATED")
        release = pc.get_status()["transfer_release"]
        assert release["available"] is True
        assert release["evaluation_status"] == "NOT_EVALUATED"
        assert release["run_id"] == "run-1"
    finally:
        _finish(pc)


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": SimpleNamespace(name="ERROR")},
        {"state": SimpleNamespace(name="MIXING")},
        {"error_message": "Stop requested"},
        {"final_evaluation_status": None},
        {"final_evaluation_status": "FAILED"},
    ],
)
def test_release_status_blocked_for_unfinished_or_failed_run(monkeypatch, overrides) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc, **overrides)
        assert pc.get_status()["transfer_release"]["available"] is False
        result = pc.start_target_transfer("confirmed", _request())
        assert result["success"] is False
        assert pc.target_transfer.is_active() is False
    finally:
        _finish(pc)


def test_release_blocked_while_mixing_runs_or_after_stop(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        pc.fill_and_measure.is_running = True
        assert pc.get_status()["transfer_release"]["available"] is False
        pc.fill_and_measure.is_running = False

        pc.fill_and_measure._stop_reason = "emergency_stop"
        assert pc.get_status()["transfer_release"]["available"] is False
    finally:
        _finish(pc)


def test_acknowledge_error_removes_mixing_release(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.acknowledge_error()["success"] is True
        assert pc.get_status()["transfer_release"]["available"] is False
    finally:
        _finish(pc)


def test_release_bound_to_displayed_run_id(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc, run_id="run-2")

        stale = pc.start_target_transfer("confirmed", _request(run_id="run-1"))

        assert stale["success"] is False
        assert "Mischlauf" in stale["message"]
        assert pc.target_transfer.is_active() is False
        assert not pc.target_transfer.is_run_used("run-1")
    finally:
        _finish(pc)



def test_accepted_start_shows_in_status_and_blocks_a_second_start(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)

        accepted = pc.start_target_transfer("confirmed", _request(tank=4))
        status = pc.get_status()

        assert accepted["success"] is True, accepted
        assert pc.target_transfer.is_active() is True
        assert status["target_transfer"]["target_tank"] == 4
        again = pc.start_target_transfer("confirmed", _request(tank=4))
        assert again["success"] is False  # Exklusivitaet waehrend des Laufs
    finally:
        _finish(pc)


def test_stopped_transfer_leaves_mixing_run_available_for_a_new_release(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.start_target_transfer("confirmed", _request(tank=2))["success"] is True
        pc.stop_target_transfer()

        release = pc.get_status()["transfer_release"]
        assert release["available"] is True and release["run_id"] == "run-1"

        retry = pc.start_target_transfer("confirmed", _request(tank=3))
        assert retry["success"] is True
        assert pc.get_status()["target_transfer"]["target_tank"] == 3
    finally:
        _finish(pc)


def test_successfully_completed_run_is_no_longer_available(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        pc.target_transfer._completed_run_ids.add("run-1")

        release = pc.get_status()["transfer_release"]
        assert release["available"] is False and "erfolgreich abgeschlossen" in release["reason"]
        assert pc.start_target_transfer("confirmed", _request())["success"] is False
    finally:
        _finish(pc)


def test_latched_transfer_fault_hides_release_until_acknowledged_and_keeps_mixing_run(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        pc.target_transfer._fault_latched = True

        release = pc.get_status()["transfer_release"]
        assert release["available"] is False and "quittiert" in release["reason"]
        assert pc.start_target_transfer("confirmed", _request())["success"] is False

        # Erstes Reset quittiert nur die Transferstoerung; der Mischlauf bleibt erhalten.
        assert pc.acknowledge_error()["success"] is True
        assert pc.target_transfer.fault_latched() is False
        assert pc.get_status()["transfer_release"]["available"] is True

        # Zweites Reset verhaelt sich wie bisher und verwirft den Mischlauf.
        assert pc.acknowledge_error()["success"] is True
        assert pc.get_status()["transfer_release"]["available"] is False
    finally:
        _finish(pc)


def test_pending_pump_fault_blocks_every_process_start(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        pc.target_transfer._held_actuators = object()
        for starting in ("fill_and_measure", "manual_drain_jog", "tank_cleaning", "prime", "target_transfer"):
            assert pc._busy_reason(starting) is not None, starting
    finally:
        pc.target_transfer._held_actuators = None
        _finish(pc)


def test_emergency_stop_retries_held_pump_cleanup_and_never_closes_valve_first(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    calls: list[str] = []

    class Held:
        def safe_shutdown_all(self):
            calls.append("shutdown_all")
            return []

        def close_all(self):
            calls.append("close_all")
            return []

    class Pump:
        fail = True

        def off(self):
            calls.append("pump_off")
            if self.fail:
                raise RuntimeError("contactor stuck")

    class Valve:
        def off(self):
            calls.append("valve_off")

    pump = Pump()
    try:
        pc.target_transfer._held_actuators = Held()
        pc.target_transfer._held_pump = pump
        pc.target_transfer._held_valve = Valve()

        asyncio.run(pc.emergency_stop())
        assert calls == ["pump_off"]  # Ventil bleibt unberuehrt
        assert pc.target_transfer.hardware_fault_pending() is True
        assert "weiterhin nicht abschalten" in str(pc.last_error)

        pump.fail = False
        asyncio.run(pc.emergency_stop())
        assert calls[-4:] == ["pump_off", "valve_off", "shutdown_all", "close_all"]
        assert pc.target_transfer.hardware_fault_pending() is False
    finally:
        _finish(pc)


def test_not_evaluated_run_requires_acknowledgement_via_controller(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc, status="NOT_EVALUATED")

        without = pc.start_target_transfer("confirmed", _request(not_evaluated_acknowledged=False))
        assert without["success"] is False and "NOT_EVALUATED" in without["message"]
        assert not pc.target_transfer.is_run_used("run-1")

        with_ack = pc.start_target_transfer("confirmed", _request(not_evaluated_acknowledged=True))
        assert with_ack["success"] is True
    finally:
        _finish(pc)


def test_wrong_confirmation_text_blocks(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.start_target_transfer("nope", _request())["success"] is False
        assert pc.start_target_transfer(None, _request())["success"] is False
        assert pc.target_transfer.is_active() is False
    finally:
        _finish(pc)


def test_capacity_and_mixer_checks_apply_through_controller(monkeypatch) -> None:
    pc = _process_controller(monkeypatch, mixer_liters=60.0)
    try:
        _finish_mixing_run(pc)
        too_full = pc.start_target_transfer("confirmed", _request(target_level_liters=241.0))
        assert too_full["success"] is False
        unconfirmed = pc.start_target_transfer("confirmed", _request(capacity_confirmed=False))
        assert unconfirmed["success"] is False
        assert not pc.target_transfer.is_run_used("run-1")
    finally:
        _finish(pc)


def test_hardware_flag_false_blocks_transfer(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    pc.load_settings = lambda: _settings(hardware_execution_enabled=False)
    try:
        _finish_mixing_run(pc)
        assert pc.start_target_transfer("confirmed", _request())["success"] is False
        assert pc.target_transfer.is_active() is False
    finally:
        _finish(pc)


def test_running_transfer_blocks_every_other_process(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.start_target_transfer("confirmed", _request())["success"] is True

        for starting in ("fill_and_measure", "manual_drain_jog", "tank_cleaning", "prime", "target_transfer"):
            assert pc._busy_reason(starting) is not None, starting
        assert pc.start_manual_drain_jog()["success"] is False
        assert pc.start_tank_cleaning("confirmed")["success"] is False
        assert pc.start_prime({"MCU_B": ["P1"]})["success"] is False
        assert pc.start_fill_and_measure("confirmed")["success"] is False
        assert pc.acknowledge_error()["success"] is False
    finally:
        _finish(pc)


@pytest.mark.parametrize("busy", ["is_running", "manual_drain_jog", "tank_cleaning", "prime"])
def test_other_running_process_blocks_transfer(monkeypatch, busy) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        if busy == "is_running":
            pc.fill_and_measure.is_running = True
        else:
            monkeypatch.setattr(getattr(pc, busy), "is_active", lambda: True)

        result = pc.start_target_transfer("confirmed", _request())

        assert result["success"] is False
        assert pc.target_transfer.is_active() is False
    finally:
        pc.fill_and_measure.is_running = False
        monkeypatch.undo()
        _finish(pc)


def test_other_mixer_changing_process_invalidates_release(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.get_status()["transfer_release"]["available"] is True

        assert pc.start_tank_cleaning("confirmed")["success"] is True

        assert pc.get_status()["transfer_release"]["available"] is False
    finally:
        _finish(pc)

    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.start_manual_drain_jog()["success"] is True
        assert pc.get_status()["transfer_release"]["available"] is False
    finally:
        _finish(pc)


def test_emergency_stop_reaches_running_transfer(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.start_target_transfer("confirmed", _request())["success"] is True
        assert pc.target_transfer.is_active() is True

        result = asyncio.run(pc.emergency_stop())

        assert result["success"] is True
        assert pc.target_transfer.is_active() is False
        assert pc.target_transfer._stop_reason == "emergency_stop"
    finally:
        _finish(pc)


def test_shutdown_stops_running_transfer(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    _finish_mixing_run(pc)
    assert pc.start_target_transfer("confirmed", _request())["success"] is True

    result = pc.shutdown()

    assert result["success"] is True
    assert pc.target_transfer.is_active() is False


def test_stop_target_transfer_via_controller(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        assert pc.start_target_transfer("confirmed", _request())["success"] is True

        result = pc.stop_target_transfer()

        assert result["success"] is True
        assert pc.target_transfer.is_active() is False
    finally:
        _finish(pc)


def test_acknowledge_error_resets_finished_transfer(monkeypatch) -> None:
    pc = _process_controller(monkeypatch)
    try:
        _finish_mixing_run(pc)
        pc.start_target_transfer("confirmed", _request())
        pc.stop_target_transfer()
        assert pc.get_status()["target_transfer"]["phase"] != TransferPhase.IDLE

        assert pc.acknowledge_error()["success"] is True

        assert pc.get_status()["target_transfer"]["phase"] == TransferPhase.IDLE
        # Ohne Transferstoerung setzt Reset wie bisher auch den Mischlauf zurueck.
        assert pc.get_status()["transfer_release"]["available"] is False
    finally:
        _finish(pc)


# --- Fill-and-Measure: Kennung pro Lauf ---------------------------------------------------------


def test_every_accepted_mixing_start_gets_a_fresh_run_id(monkeypatch) -> None:
    monkeypatch.setattr(FillAndMeasureController, "_run", lambda self, settings: None)
    monkeypatch.setattr("process.fill_and_measure.validate_fill_start", lambda settings, snapshot: (True, ""))
    controller = FillAndMeasureController(get_sensor_snapshot=lambda: {"mixer": {}})
    assert controller.get_status()["run_id"] is None

    assert controller.start({"hardware_execution_enabled": True})["success"] is True
    first = controller.get_status()["run_id"]
    controller._thread.join(timeout=5.0)
    controller.is_running = False
    assert controller.start({"hardware_execution_enabled": True})["success"] is True
    second = controller.get_status()["run_id"]
    controller._thread.join(timeout=5.0)
    controller.is_running = False

    assert first and second and first != second


def test_rejected_mixing_start_keeps_old_run_id(monkeypatch) -> None:
    controller = FillAndMeasureController(get_sensor_snapshot=lambda: {"mixer": {}})
    controller._run_id = "old"

    result = controller.start({"hardware_execution_enabled": False})

    assert result["success"] is False
    assert controller.get_status()["run_id"] == "old"


# --- Konfiguration --------------------------------------------------------------------------------


def test_process_settings_file_validates_and_carries_transfer_safety_values() -> None:
    path = target_transfer.__file__.replace("process/target_transfer.py", "config/process_settings.json")
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)

    settings = validate_settings(raw, PROCESS_SETTINGS_SCHEMA, "process_settings.json")

    assert settings["transfer_pump_liters_per_minute"] == 16.0
    assert settings["drain_timeout_buffer_seconds"] == 180.0
    assert settings["empty_threshold_liters"] == 0.3
    assert settings["empty_confirm_samples"] == 5
    assert settings["no_drain_progress_warning_seconds"] == 60.0
    # Der Transfer wartet 3 s zwischen "Ventil auf" und "Pumpe an" (Pumpe nicht gegen ein noch oeffnendes Ventil).
    assert settings["valve_settle_seconds"] == 3.0


# --- Dashboard-Handler und Anzeige ----------------------------------------------------------------


def _fake_widgets(**values):
    def switch(value=False):
        return SimpleNamespace(value=value)

    widgets = SimpleNamespace(
        safety_confirm_switch=switch(values.get("safety", True)),
        transfer_capacity_confirm_switch=switch(values.get("capacity", True)),
        transfer_not_evaluated_switch=switch(values.get("ack", False)),
        transfer_tank_select=SimpleNamespace(value=values.get("tank", 2)),
        transfer_level_input=SimpleNamespace(value=values.get("level", 120.0)),
        transfer_state={"run_id": values.get("run_id", "run-1")},
    )
    return widgets


class _FakeDashboardController:
    def __init__(self, result=None) -> None:
        self.requests: list[tuple] = []
        self.result = result or {"success": True, "message": "ok"}

    def get_process_control_status(self) -> dict:
        return {"required_confirmation_text": "confirmed"}

    def start_target_transfer(self, text, request):
        self.requests.append((text, request))
        return self.result


@pytest.fixture
def dashboard(monkeypatch):
    import nicegui_dashboard.pages.dashboard.process_control_section as section

    notified: list[tuple] = []
    monkeypatch.setattr(section.ui, "notify", lambda message, color=None, **kw: notified.append((message, color)))
    return section, notified


def test_handler_builds_strict_request_from_displayed_run_and_resets_confirmations(dashboard) -> None:
    section, _notified = dashboard
    widgets = _fake_widgets(ack=True)
    controller = _FakeDashboardController()
    logs: list[str] = []

    section.handle_transfer_start(controller, widgets, logs.append)

    (text, request), = controller.requests
    assert text == "confirmed"
    assert request == TransferRequest(
        run_id="run-1", tank=2, target_level_liters=120.0, capacity_confirmed=True, not_evaluated_acknowledged=True
    )
    assert widgets.safety_confirm_switch.value is False
    assert widgets.transfer_capacity_confirm_switch.value is False
    assert widgets.transfer_not_evaluated_switch.value is False
    # Nach akzeptiertem Start: Auswahl und abgelesener Wert muessen neu eingegeben werden.
    assert widgets.transfer_tank_select.value is None and widgets.transfer_level_input.value is None


def test_handler_without_safety_switch_does_not_call_controller_and_still_resets(dashboard) -> None:
    section, _notified = dashboard
    widgets = _fake_widgets(safety=False)
    controller = _FakeDashboardController()

    section.handle_transfer_start(controller, widgets, lambda _m: None)

    assert controller.requests == []
    assert widgets.transfer_capacity_confirm_switch.value is False
    assert widgets.transfer_not_evaluated_switch.value is False


def test_handler_without_displayed_release_does_not_call_controller(dashboard) -> None:
    section, _notified = dashboard
    widgets = _fake_widgets(run_id=None)
    controller = _FakeDashboardController()

    section.handle_transfer_start(controller, widgets, lambda _m: None)

    assert controller.requests == []


def test_handler_keeps_tank_and_level_after_blocked_attempt(dashboard) -> None:
    section, _notified = dashboard
    widgets = _fake_widgets()
    controller = _FakeDashboardController(result={"success": False, "message": "blocked"})

    section.handle_transfer_start(controller, widgets, lambda _m: None)

    assert widgets.transfer_tank_select.value == 2 and widgets.transfer_level_input.value == 120.0
    assert widgets.transfer_capacity_confirm_switch.value is False


def test_release_text_never_presents_not_evaluated_as_quality_proof() -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_transfer_release

    ok = format_transfer_release({"available": True, "run_id": "abcdef123456", "evaluation_status": "OK"})
    not_evaluated = format_transfer_release(
        {"available": True, "run_id": "abcdef123456", "evaluation_status": "NOT_EVALUATED"}
    )
    blocked = format_transfer_release({"available": False, "reason": "Mischlauf laeuft noch."})

    assert "NOT_EVALUATED" not in ok and "OK" in ok
    assert "NICHT bestaetigt" in not_evaluated and "kein Qualitaetsnachweis" in not_evaluated
    assert "keine freigabe" in blocked.lower() and "laeuft noch" in blocked
    assert "keine freigabe" in format_transfer_release(None).lower()


def test_status_text_shows_result_and_error() -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_transfer_status

    running = format_transfer_status(
        {"phase": "TRANSFERRING", "target_tank": 2, "current_mixer_liters": 30.0,
         "transferred_liters_sensor_calc": 20.0, "is_active": True, "elapsed_seconds": 12.0, "max_seconds": 367.5}
    )
    failed = format_transfer_status(
        {"phase": "ERROR", "target_tank": 2, "is_active": False, "stop_reason": "sensor_lost_during_drain",
         "last_error": "sensor_lost_during_drain: x"}
    )

    assert "TRANSFERRING" in running and "Tank 2" in running and "12/368" in running or "12/367" in running
    assert "FEHLER" in failed and "sensor_lost_during_drain" in failed
    assert format_transfer_status(None) == "Target Transfer: bereit"


def test_status_text_for_pump_off_failure_demands_physical_emergency_stop() -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_transfer_status

    text = format_transfer_status({"phase": "ERROR", "pump_off_failed": True, "fault_latched": True, "is_active": False})

    assert "KRITISCH" in text and "NOT-AUS" in text and "nicht bestaetigt" in text
    assert "nicht geschlossen" in text and "Quittierung" in text


def test_status_text_asks_for_acknowledgement_after_fault() -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_transfer_status

    text = format_transfer_status(
        {"phase": "ERROR", "target_tank": 1, "is_active": False, "fault_latched": True, "last_error": "x"}
    )

    assert "quittieren" in text


def test_dashboard_grid_places_transfer_above_recipe_in_one_grid(tmp_path, monkeypatch) -> None:
    """Alle Karten sind direkte Kinder EINES Rasters; die Transfer-Card steht (im DOM und per
    Rasterbereich) vor dem Rezept und ist nicht Teil des Process-Control-Panels."""
    import collections
    from unittest.mock import MagicMock

    from nicegui import context, ui

    import nicegui_dashboard.recipe_store as recipe_store
    from nicegui_dashboard.pages.dashboard_page import create_dashboard_page

    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller = MagicMock()
    for name in ("get_sensor_status", "get_process_status", "get_process_control_status"):
        getattr(controller, name).return_value = collections.defaultdict(lambda: None)

    before = set(context.client.elements)
    with ui.column():
        create_dashboard_page(controller)
    new = [el for eid, el in context.client.elements.items() if eid not in before]

    grids = [el for el in new if "dash-grid" in el.classes]
    assert len(grids) == 1
    children = list(grids[0])
    classes = [" ".join(child.classes) for child in children]

    def index_of(marker: str) -> int:
        matches = [i for i, c in enumerate(classes) if marker in c]
        assert len(matches) == 1, (marker, classes)
        return matches[0]

    for marker in ("grid-status", "grid-process-state", "grid-actuators", "grid-transfer", "recipe-panel", "grid-side", "grid-data"):
        index_of(marker)
    assert index_of("grid-transfer") < index_of("recipe-panel")

    transfer_card = children[index_of("grid-transfer")]
    side_column = children[index_of("grid-side")]
    assert side_column not in list(_ancestors(transfer_card))
    titles = [el.text for el in new if isinstance(el, ui.label)]
    assert titles.count("Transfer in Solution Tank") == 1
    # Kopfzeile: Datum/Uhrzeit und Update stehen neben Maintenance.
    assert any(t.startswith("Update:") for t in titles)
    assert any("·" in t and t[2:3] == "," for t in titles)


def _ancestors(element):
    node = element
    while getattr(node, "parent_slot", None) is not None:
        node = node.parent_slot.parent
        yield node


def test_header_clock_shows_weekday_date_and_time() -> None:
    from nicegui_dashboard.components.formatting import format_clock

    assert format_clock(datetime(2026, 10, 15, 11, 42, 7)) == "Do, 15.10.2026 · 11:42:07"
    assert format_clock(datetime(2026, 1, 4, 0, 0, 0)) == "So, 04.01.2026 · 00:00:00"


@pytest.mark.parametrize(
    "transfer, release, expected",
    [
        ({}, {}, ("Bereit", "idle")),
        ({}, {"available": True, "evaluation_status": "OK"}, ("Freigabe möglich", "ready")),
        ({}, {"available": True, "evaluation_status": "NOT_EVALUATED"}, ("Freigabe mit Bestätigung", "warn")),
        ({"is_active": True, "phase": "TRANSFERRING"}, {"available": True}, ("Läuft", "run")),
        ({"phase": "FINISHED"}, {}, ("Abgeschlossen", "ready")),
        ({"phase": "STOPPED"}, {}, ("Gestoppt", "warn")),
        ({"phase": "ERROR"}, {}, ("Störung", "crit")),
        ({"fault_latched": True, "phase": "STOPPED"}, {}, ("Störung", "crit")),
        ({"pump_off_failed": True, "phase": "ERROR"}, {}, ("Störung", "crit")),
        (None, None, ("Bereit", "idle")),
    ],
)
def test_transfer_chip_reflects_state(transfer, release, expected) -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_transfer_chip

    assert format_transfer_chip(transfer, release) == expected


def test_header_subtitle_text_is_set_exactly(tmp_path, monkeypatch) -> None:
    """Der Untertitel steht exakt so im Label; die Grossschreibung kommt per CSS (text-transform)."""
    import collections
    from unittest.mock import MagicMock

    from nicegui import context, ui

    import nicegui_dashboard.recipe_store as recipe_store
    from nicegui_dashboard.pages.dashboard_page import create_dashboard_page

    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    controller = MagicMock()
    for name in ("get_sensor_status", "get_process_status", "get_process_control_status"):
        getattr(controller, name).return_value = collections.defaultdict(lambda: None)

    before = set(context.client.elements)
    with ui.column():
        create_dashboard_page(controller)
    new = [el for eid, el in context.client.elements.items() if eid not in before]

    subtitles = [el.text for el in new if isinstance(el, ui.label) and "subtitle" in el.classes]
    assert subtitles == ["vGREENS - DRIVEN BY PURPOSE. POWERED BY DATA."]
    css = (Path(__file__).resolve().parent.parent / "nicegui_dashboard" / "static" / "dashboard.css").read_text(encoding="utf-8")
    assert "text-transform: uppercase" in css


def test_idle_status_text_is_not_prefixed_twice() -> None:
    from nicegui_dashboard.pages.dashboard.process_control_section import format_transfer_status

    assert format_transfer_status({"phase": "IDLE", "last_message": "Target Transfer ready."}) == "Target Transfer ready."
    assert (
        format_transfer_status({"phase": "IDLE", "last_message": "Transfer blockiert: Zieltank muss 1 bis 4 sein."})
        == "Transfer blockiert: Zieltank muss 1 bis 4 sein."
    )
    assert format_transfer_status({"phase": "IDLE"}) == "Target Transfer: bereit"
    # laufende/beendete Phasen: Name genau einmal am Anfang
    running = format_transfer_status({"phase": "TRANSFERRING", "target_tank": 2, "is_active": True, "elapsed_seconds": 5, "max_seconds": 300})
    assert running.count("Target Transfer") == 1
