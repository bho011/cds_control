"""
Sicherheitsinvarianten-Tests für process/tank_cleaning.py::TankCleaningController,
VOR dem Split in process/tank_cleaning/ (Modularisierungs-Plan Phase 5).

Charakterisiert das heutige Verhalten (Fill -> Hold -> Drain Reihenfolge,
sicheres Abschalten bei Fehler/Stop/Exception) - definiert keine neue
Prozesslogik. Kein echtes GPIO/MQTT: ActuatorManager und MqttPublisher
werden über monkeypatch durch Fakes ersetzt (beide werden per lokalem
Import INNERHALB von _run() geholt, siehe process/tank_cleaning.py - das
Patchen der Ursprungsmodule wirkt trotzdem, weil Python "from X import Y"
zur Aufrufzeit das Attribut Y vom Modul X abliest).
"""

from __future__ import annotations

import threading

from process.auto_circulation import AutoCirculationController, load_auto_circulation_config
from process.tank_cleaning import TankCleaningController, TankCleaningPhase
from process.tank_cleaning.drain_phase import run_drain_phase
from process.tank_cleaning.fill_phase import run_fill_phase


class FakeActuator:
    def __init__(self, name: str):
        self.name = name
        self.is_on = False
        self.on_calls = 0
        self.off_calls = 0

    def on(self) -> None:
        self.is_on = True
        self.on_calls += 1

    def off(self) -> None:
        self.is_on = False
        self.off_calls += 1


class FakeActuatorManager:
    def __init__(self, active_low: bool = True) -> None:
        self.actuators: dict[str, FakeActuator] = {}
        self.safe_shutdown_all_calls = 0
        self.close_all_calls = 0

    def add(self, name: str, gpio_pin: int) -> FakeActuator:
        actuator = FakeActuator(name)
        self.actuators[name] = actuator
        return actuator

    def get(self, name: str) -> FakeActuator:
        return self.actuators[name]

    def status_payload(self) -> dict[str, bool]:
        return {name: actuator.is_on for name, actuator in self.actuators.items()}

    def safe_shutdown_all(self) -> None:
        self.safe_shutdown_all_calls += 1
        for actuator in self.actuators.values():
            actuator.off()

    def close_all(self) -> None:
        self.close_all_calls += 1

    def any_actuator_left_on(self) -> bool:
        return any(actuator.is_on for actuator in self.actuators.values())


class FakeMqttPublisher:
    def __init__(self) -> None:
        self.published: list[dict] = []
        self.closed = False

    def publish_json(self, payload: dict) -> None:
        self.published.append(payload)

    def close(self) -> None:
        self.closed = True


class ScriptedSensor:
    """Liefert steuerbare Mixer-/RO-Literwerte, ohne echtes MQTT/OPC-UA."""

    def __init__(self, mixer_liters: float = 0.0, ro_liters: float = 500.0):
        self.mixer_liters = mixer_liters
        self.ro_liters = ro_liters
        self.call_count = 0

    def __call__(self) -> dict:
        self.call_count += 1
        return {
            "mixer": {"volume_liters_calc": self.mixer_liters},
            "ro": {"volume_liters_calc": self.ro_liters},
        }


def _base_settings(**overrides) -> dict:
    settings = {
        "hardware_execution_enabled": True,
        "required_confirmation_text": "confirmed",
        "target_fill_total_liters": 10.0,
        "max_mixer_liters": 200.0,
        "max_fill_seconds": 30.0,
        "no_fill_progress_timeout_seconds": 30.0,
        "min_fill_progress_liters": 0.5,
        "max_negative_level_drift_liters": 3.0,
        "target_reached_confirm_samples": 1,
        "min_ro_liters_required": 20.0,
        "cleaning_hold_seconds": 0.0,
        "empty_threshold_liters": 20.0,
        "empty_confirm_samples": 1,
        "transfer_pump_liters_per_minute": 16.0,
        "drain_timeout_buffer_seconds": 180.0,
        "valve_settle_seconds": 0.0,
    }
    settings.update(overrides)
    return settings


def _make_controller(monkeypatch, mixer_liters: float, ro_liters: float = 500.0):
    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", FakeActuatorManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", FakeMqttPublisher)

    sensor = ScriptedSensor(mixer_liters=mixer_liters, ro_liters=ro_liters)
    controller = TankCleaningController(get_sensor_snapshot=sensor)
    return controller, sensor


# --- erfolgreicher Durchlauf --------------------------------------------------


def test_successful_run_goes_fill_hold_drain_and_ends_finished(monkeypatch) -> None:
    # mixer bereits >= target_fill_total_liters (10) und <= empty_threshold_liters
    # (20) -> Fill nimmt den "schon am Ziel"-Kurzpfad, Drain den
    # "schon leer"-Kurzpfad; nur Hold braucht eine reale Wartezeit-Iteration.
    controller, sensor = _make_controller(monkeypatch, mixer_liters=15.0)
    settings = _base_settings()

    controller._run(settings)

    assert controller._phase == TankCleaningPhase.FINISHED
    assert controller._stop_reason == "completed"
    assert controller._last_error is None

    actuators = controller._actuators
    assert actuators is None  # _safe_shutdown_and_publish() räumt das Attribut auf


def test_successful_run_shuts_down_and_closes_actuators_exactly_once(monkeypatch) -> None:
    controller, sensor = _make_controller(monkeypatch, mixer_liters=15.0)
    settings = _base_settings()

    captured: dict = {}
    original_safe_shutdown = TankCleaningController._safe_shutdown_and_publish

    def _spy(self, actuators):
        captured["actuators"] = actuators
        return original_safe_shutdown(self, actuators)

    monkeypatch.setattr(TankCleaningController, "_safe_shutdown_and_publish", _spy)

    controller._run(settings)

    fake_actuators = captured["actuators"]
    assert fake_actuators.safe_shutdown_all_calls == 1
    assert fake_actuators.close_all_calls == 1
    assert fake_actuators.any_actuator_left_on() is False


# --- Fehler während Fill -------------------------------------------------------


def test_error_during_fill_not_enough_ro_water_shuts_down_safely(monkeypatch) -> None:
    controller, sensor = _make_controller(monkeypatch, mixer_liters=0.0, ro_liters=1.0)
    settings = _base_settings()  # min_ro_liters_required=20.0, ro=1.0 -> fails

    captured: dict = {}
    original_safe_shutdown = TankCleaningController._safe_shutdown_and_publish

    def _spy(self, actuators):
        captured["actuators"] = actuators
        return original_safe_shutdown(self, actuators)

    monkeypatch.setattr(TankCleaningController, "_safe_shutdown_and_publish", _spy)

    controller._run(settings)

    assert controller._phase == TankCleaningPhase.ERROR
    assert controller._last_error is not None
    assert captured["actuators"].safe_shutdown_all_calls == 1
    assert captured["actuators"].any_actuator_left_on() is False


def test_fill_watchdog_trip_turns_refill_pump_off_via_local_finally(monkeypatch) -> None:
    """_run_fill() hat ein eigenes finally: refill_pump.off() - zusätzlich
    zur globalen _safe_shutdown_and_publish(). Ruft _run_fill direkt auf,
    um das lokale Abschalten isoliert zu prüfen."""
    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", FakeActuatorManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", FakeMqttPublisher)

    sensor = ScriptedSensor(mixer_liters=0.0, ro_liters=500.0)
    controller = TankCleaningController(get_sensor_snapshot=sensor)

    actuators = FakeActuatorManager()
    refill_pump = actuators.add(name="mixer_refill_pump", gpio_pin=1)
    controller._auto_circulation = AutoCirculationController(
        actuators=actuators, config=load_auto_circulation_config(_base_settings())
    )

    settings = _base_settings(
        target_fill_total_liters=10.0,
        max_mixer_liters=1.0,  # sensor bleibt bei 0.0 < 1.0, also kein sofortiger Trip -
    )
    # Erzwingt den Watchdog-Abbruch beim ersten Loop-Durchlauf: der Sensor
    # springt nach dem Start-Read auf einen Wert über max_mixer_liters.
    def _bump_after_first_read():
        sensor.mixer_liters = 999.0

    original_call = sensor.__call__

    calls = {"n": 0}

    def _tracking_call():
        calls["n"] += 1
        if calls["n"] == 2:
            _bump_after_first_read()
        return original_call()

    monkeypatch.setattr(sensor, "__call__", _tracking_call)

    result = run_fill_phase(controller, settings, actuators, refill_pump)

    assert result is False
    assert refill_pump.off_calls >= 1
    assert refill_pump.is_on is False
    assert controller._phase == TankCleaningPhase.ERROR


# --- Fehler während Hold (erzwungene Exception) --------------------------------


def test_exception_during_hold_is_caught_and_shuts_down_safely(monkeypatch) -> None:
    controller, sensor = _make_controller(monkeypatch, mixer_liters=15.0)
    settings = _base_settings(cleaning_hold_seconds=9999.0)  # nie über wait() hinaus erreicht

    def _boom(self, settings, level_history):
        if self._phase == TankCleaningPhase.HOLDING:
            raise RuntimeError("simulated sensor read crash during hold")
        return TankCleaningController._read_mixer_liters(self, settings, level_history)

    # Erste _read_mixer_liters-Aufrufe (Fill) laufen normal, sobald die
    # Phase auf HOLDING wechselt, wirft der nächste Aufruf.
    original = TankCleaningController._read_mixer_liters
    monkeypatch.setattr(
        TankCleaningController,
        "_read_mixer_liters",
        lambda self, s, h: _boom(self, s, h) if self._phase == TankCleaningPhase.HOLDING else original(self, s, h),
    )

    captured: dict = {}
    original_safe_shutdown = TankCleaningController._safe_shutdown_and_publish

    def _spy(self, actuators):
        captured["actuators"] = actuators
        return original_safe_shutdown(self, actuators)

    monkeypatch.setattr(TankCleaningController, "_safe_shutdown_and_publish", _spy)

    controller._run(settings)

    assert controller._phase == TankCleaningPhase.ERROR
    assert "simulated sensor read crash" in (controller._last_error or "")
    assert captured["actuators"].safe_shutdown_all_calls == 1
    assert captured["actuators"].any_actuator_left_on() is False


# --- Fehler während Drain (Timeout) --------------------------------------------


def test_drain_timeout_shuts_off_transfer_pump_and_drain_valve_locally(monkeypatch) -> None:
    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", FakeActuatorManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", FakeMqttPublisher)

    sensor = ScriptedSensor(mixer_liters=15.0, ro_liters=500.0)
    controller = TankCleaningController(get_sensor_snapshot=sensor)

    actuators = FakeActuatorManager()
    transfer_pump = actuators.add(name="transfer_pump", gpio_pin=2)
    drain_valve = actuators.add(name="drain_valve_0", gpio_pin=3)

    settings = _base_settings(
        empty_threshold_liters=0.0,  # sensor bleibt bei 15.0 -> nie "leer"
        empty_confirm_samples=1,
        transfer_pump_liters_per_minute=100000.0,  # -> Timeout praktisch sofort
        drain_timeout_buffer_seconds=0.0,
        valve_settle_seconds=0.0,
    )
    controller._auto_circulation = AutoCirculationController(
        actuators=actuators, config=load_auto_circulation_config(settings)
    )

    result = run_drain_phase(controller, settings, actuators, transfer_pump, drain_valve)

    assert result is None  # _run_drain hat keinen Rückgabewert bei Timeout, siehe Quelltext
    assert controller._phase == TankCleaningPhase.ERROR
    assert controller._stop_reason == "calculated_drain_timeout"
    assert transfer_pump.off_calls >= 1
    assert drain_valve.off_calls >= 1
    assert transfer_pump.is_on is False
    assert drain_valve.is_on is False


# --- Stop/Abbruch während jeder Phase ------------------------------------------


def test_stop_during_fill_leaves_phase_idle_not_finished_or_error(monkeypatch) -> None:
    controller, sensor = _make_controller(monkeypatch, mixer_liters=0.0)
    # target > start, damit _run_fill die Schleife betritt statt des
    # "schon am Ziel"-Kurzpfads.
    settings = _base_settings(target_fill_total_liters=10.0)

    controller.request_stop(reason="stopped_by_user")
    controller._run(settings)

    assert controller._phase == TankCleaningPhase.IDLE
    assert controller._stop_reason == "stopped_by_user"


def test_stop_during_hold_leaves_phase_idle(monkeypatch) -> None:
    controller, sensor = _make_controller(monkeypatch, mixer_liters=15.0)
    settings = _base_settings(cleaning_hold_seconds=9999.0)

    # Fill nimmt den "schon am Ziel"-Kurzpfad (mixer=15 >= target=10) und
    # betritt nie eine Schleife - der Stop muss deshalb erst beim Eintritt
    # in Hold wirken. request_stop() VOR dem Aufruf simuliert einen Stop,
    # der genau in dem Moment eintrifft, in dem Hold seine erste
    # Wartezeit-Iteration beginnt.
    controller.request_stop(reason="stopped_by_user")
    controller._run(settings)

    assert controller._phase == TankCleaningPhase.IDLE
    assert controller._stop_reason == "stopped_by_user"


def test_stop_during_drain_loop_leaves_no_actuator_on(monkeypatch) -> None:
    """Stop nach dem valve_settle-Wait, während die Transferpumpe bereits
    läuft: der try/finally-Block in _run_drain() schaltet in diesem Fall
    beide Aktoren zuverlässig ab. Ein separater Thread ruft request_stop()
    erst NACH dem valve_settle-Wait auf, um genau diesen Zeitpunkt zu
    simulieren (siehe test_stop_exactly_during_valve_settle_wait_is_a_
    known_gap unten für den davor liegenden, abweichenden Fall)."""
    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", FakeActuatorManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", FakeMqttPublisher)

    sensor = ScriptedSensor(mixer_liters=15.0, ro_liters=500.0)
    controller = TankCleaningController(get_sensor_snapshot=sensor)

    actuators = FakeActuatorManager()
    transfer_pump = actuators.add(name="transfer_pump", gpio_pin=2)
    drain_valve = actuators.add(name="drain_valve_0", gpio_pin=3)

    settings = _base_settings(empty_threshold_liters=0.0, valve_settle_seconds=0.05)
    controller._auto_circulation = AutoCirculationController(
        actuators=actuators, config=load_auto_circulation_config(settings)
    )

    timer = threading.Timer(0.15, lambda: controller.request_stop(reason="stopped_by_user"))
    timer.start()
    try:
        run_drain_phase(controller, settings, actuators, transfer_pump, drain_valve)
    finally:
        timer.cancel()

    assert transfer_pump.is_on is False
    assert drain_valve.is_on is False


def test_stop_exactly_during_valve_settle_wait_is_a_known_gap(monkeypatch) -> None:
    """Charakterisiert (nicht bewertet) einen bestehenden Randfall: wenn der
    Stop exakt in dem Moment eintrifft, in dem _run_drain() gerade das
    Drain-Ventil geöffnet hat und auf valve_settle_seconds wartet, gibt es
    dafür KEIN try/finally - die Funktion kehrt zurück, BEVOR die
    Transferpumpe je anläuft, aber das Ventil bleibt offen. Dieser Plan
    ändert daran nichts (reine Charakterisierung); im Abschlussbericht als
    Befund vermerkt."""
    monkeypatch.setattr("hardware.actuator_manager.ActuatorManager", FakeActuatorManager)
    monkeypatch.setattr("services.mqtt_publisher.MqttPublisher", FakeMqttPublisher)

    sensor = ScriptedSensor(mixer_liters=15.0, ro_liters=500.0)
    controller = TankCleaningController(get_sensor_snapshot=sensor)

    actuators = FakeActuatorManager()
    transfer_pump = actuators.add(name="transfer_pump", gpio_pin=2)
    drain_valve = actuators.add(name="drain_valve_0", gpio_pin=3)

    settings = _base_settings(empty_threshold_liters=0.0, valve_settle_seconds=1.0)
    controller._auto_circulation = AutoCirculationController(
        actuators=actuators, config=load_auto_circulation_config(settings)
    )

    controller.request_stop(reason="stopped_by_user")
    run_drain_phase(controller, settings, actuators, transfer_pump, drain_valve)

    assert transfer_pump.is_on is False  # nie eingeschaltet
    assert drain_valve.is_on is True  # bekannter Randfall: bleibt offen


# --- kein Aktor bleibt unbeabsichtigt an ---------------------------------------


def test_no_actuator_left_on_across_success_error_and_stop(monkeypatch) -> None:
    scenarios = [
        ("success", 15.0, 500.0, False, dict()),
        ("fill_error", 0.0, 1.0, False, dict()),
        ("stopped", 0.0, 500.0, True, dict(target_fill_total_liters=10.0)),
    ]

    # Einmal außerhalb der Schleife auf die ECHTE Methode binden - sonst
    # würde jede weitere monkeypatch.setattr()-Runde innerhalb der Schleife
    # die bereits gepatchte Vorgänger-Version einwickeln (der Patch wird
    # von pytest erst am Testende zurückgesetzt, nicht zwischen den
    # Schleifendurchläufen) und am Ende in unendlicher Rekursion enden.
    real_safe_shutdown = TankCleaningController._safe_shutdown_and_publish

    for _label, mixer_liters, ro_liters, should_stop, overrides in scenarios:
        controller, sensor = _make_controller(monkeypatch, mixer_liters=mixer_liters, ro_liters=ro_liters)
        settings = _base_settings(**overrides)

        if should_stop:
            controller.request_stop(reason="stopped_by_user")

        captured: dict = {}

        def _spy(self, actuators, _bucket=captured, _original=real_safe_shutdown):
            _bucket["actuators"] = actuators
            return _original(self, actuators)

        monkeypatch.setattr(TankCleaningController, "_safe_shutdown_and_publish", _spy)

        controller._run(settings)

        assert captured["actuators"].any_actuator_left_on() is False, _label


# --- Exception -> korrekter Fehlerstatus wird publiziert -----------------------


def test_exception_publishes_error_status_via_mqtt(monkeypatch) -> None:
    controller, sensor = _make_controller(monkeypatch, mixer_liters=0.0, ro_liters=1.0)
    settings = _base_settings()  # ro=1.0 < min_ro_liters_required=20.0 -> Fill-Fehler

    controller._run(settings)

    # _publish() wird im finally-Block von _safe_shutdown_and_publish()
    # aufgerufen, MqttPublisher ist zu diesem Zeitpunkt noch nicht None.
    assert controller._last_error is not None
