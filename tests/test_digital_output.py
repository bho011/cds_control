"""
AP-09A: Abschalt- und Freigabe-Semantik von hardware/digital_output.py.

Vollstaendig hardwarefrei: hardware.digital_output.OutputDevice wird durch ein
Fake ersetzt (der AP-00-Guard bleibt aktiv). Kein elektrischer Readback
existiert, daher gilt fail-closed: schlaegt off() fehl, wird das GPIO-Handle
NICHT freigegeben und der Zustand bleibt "aktiv".
"""

from __future__ import annotations

import pytest

import hardware.digital_output as digital_output
from hardware.digital_output import DigitalOutput


class DeviceClosed(RuntimeError):
    pass


class FakeOutputDevice:
    """Zeichnet Aufrufe auf; wirft nach close() wie gpiozero (GPIODeviceClosed)."""

    instances: list["FakeOutputDevice"] = []

    def __init__(self, pin, active_high, initial_value):
        self.pin = pin
        self.calls: list[str] = []
        self.is_closed = False
        self.fail_on = False
        self.fail_off = False
        self.fail_close = False
        FakeOutputDevice.instances.append(self)

    def _check_open(self):
        if self.is_closed:
            raise DeviceClosed("device is closed")

    def on(self):
        self._check_open()
        self.calls.append("on")
        if self.fail_on:
            raise OSError("on failed after the pin was driven")

    def off(self):
        self._check_open()
        self.calls.append("off")
        if self.fail_off:
            raise OSError("off failed")

    def close(self):
        self.calls.append("close")
        if self.fail_close:
            raise OSError("close failed")
        self.is_closed = True


@pytest.fixture(autouse=True)
def fake_device(monkeypatch):
    FakeOutputDevice.instances = []
    monkeypatch.setattr(digital_output, "OutputDevice", FakeOutputDevice)


def make_output(name: str = "transfer_pump", pin: int = 5) -> tuple[DigitalOutput, FakeOutputDevice]:
    output = DigitalOutput(name=name, gpio_pin=pin, active_low=True)
    return output, output.device


# --- off() ---------------------------------------------------------------------

def test_off_calls_device_off_even_if_flag_is_inactive():
    output, device = make_output()
    assert output.is_active is False

    output.off()

    assert device.calls == ["off"]
    assert output.is_active is False


def test_off_after_on_that_raised_still_switches_the_device_off():
    output, device = make_output()
    device.fail_on = True

    with pytest.raises(OSError):
        output.on()
    assert output.is_active is False  # Flag sagt "aus", der Pin koennte getrieben sein

    output.off()

    assert device.calls == ["on", "off"]


def test_failed_off_keeps_flag_active_and_raises_and_retry_works():
    output, device = make_output()
    output.on()
    device.fail_off = True

    with pytest.raises(OSError, match="off failed"):
        output.off()
    assert output.is_active is True

    device.fail_off = False
    output.off()

    assert output.is_active is False
    assert device.calls == ["on", "off", "off"]


def test_successful_off_clears_flag():
    output, _device = make_output()
    output.on()
    assert output.is_active is True

    output.off()

    assert output.is_active is False


# --- close() -------------------------------------------------------------------

def test_close_switches_off_before_releasing_the_handle():
    output, device = make_output()
    output.on()

    output.close()

    assert device.calls == ["on", "off", "close"]
    assert device.is_closed is True
    assert output.is_active is False


def test_close_does_not_release_the_handle_if_off_fails():
    output, device = make_output()
    output.on()
    device.fail_off = True

    with pytest.raises(OSError, match="off failed"):
        output.close()

    assert "close" not in device.calls
    assert device.is_closed is False
    assert output.is_active is True


def test_close_after_failed_off_can_be_retried_and_then_closes():
    output, device = make_output()
    output.on()
    device.fail_off = True
    with pytest.raises(OSError):
        output.close()

    device.fail_off = False
    output.close()

    assert device.calls == ["on", "off", "off", "close"]
    assert device.is_closed is True
    assert output.is_active is False


def test_failed_device_close_does_not_mark_closed_and_can_be_retried():
    output, device = make_output()
    device.fail_close = True

    with pytest.raises(OSError, match="close failed"):
        output.close()

    device.fail_close = False
    output.close()

    assert device.calls == ["off", "close", "off", "close"]
    assert device.is_closed is True


def test_repeated_close_and_off_after_successful_close_are_silent_noops():
    output, device = make_output()
    output.on()
    output.close()
    calls_after_close = list(device.calls)

    output.close()
    output.off()

    assert device.calls == calls_after_close
    assert output.is_active is False


# --- ActuatorManager mit Fake-Geraeten -------------------------------------------

def _manager_with(*names: str):
    from hardware.actuator_manager import ActuatorManager

    manager = ActuatorManager(active_low=True)
    for index, name in enumerate(names):
        manager.add(name, 10 + index)
    return manager


def test_manager_shutdown_and_close_after_successful_close_report_no_errors():
    manager = _manager_with("transfer_pump", "drain_valve_0")
    manager.get("transfer_pump").on()

    assert manager.close_all() == []
    assert all(d.is_closed for d in FakeOutputDevice.instances)

    # dokumentiertes Muster: Notaus/Shutdown nach bereits erfolgtem close_all()
    assert manager.safe_shutdown_all() == []
    assert manager.close_all() == []


def test_manager_failed_off_is_reported_handle_stays_open_and_retry_succeeds():
    manager = _manager_with("transfer_pump", "drain_valve_0")
    pump, valve = manager.get("transfer_pump"), manager.get("drain_valve_0")
    pump.on()
    valve.on()
    pump.device.fail_off = True

    errors = manager.close_all()

    assert any("transfer_pump" in e for e in errors)
    assert pump.device.is_closed is False and pump.is_active is True
    assert valve.device.is_closed is True and valve.is_active is False  # anderer Aktor nicht blockiert

    pump.device.fail_off = False
    assert manager.safe_shutdown_all() == []
    assert pump.is_active is False
    assert manager.close_all() == []
    assert pump.device.is_closed is True


def _lock_is_held() -> bool:
    """True, wenn die globale Hardwaresperre von einem anderen Besitzer gehalten wird
    (flock sperrt je offener Dateibeschreibung, die Probe konkurriert also auch im
    selben Prozess). Hat die Probe die Sperre bekommen, wird sie sofort wieder frei."""
    import fcntl

    import hardware.actuator_manager as actuator_manager

    probe = actuator_manager.LOCK_PATH.open("w")
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False
    finally:
        probe.close()


def test_manager_keeps_hardware_lock_while_a_handle_is_still_open():
    manager = _manager_with("transfer_pump", "drain_valve_0")
    pump, valve = manager.get("transfer_pump"), manager.get("drain_valve_0")
    pump.on()
    valve.on()
    pump.device.fail_off = True

    errors = manager.close_all()

    assert any("transfer_pump" in e for e in errors)  # Fehler bleibt sichtbar
    assert pump.device.is_closed is False
    assert valve.device.is_closed is True
    assert _lock_is_held() is True

    pump.device.fail_off = False  # Aufraeumen
    assert manager.close_all() == []


def test_manager_keeps_hardware_lock_while_device_close_fails():
    manager = _manager_with("transfer_pump")
    pump = manager.get("transfer_pump")
    pump.device.fail_close = True

    errors = manager.close_all()

    assert any("Could not close actuator 'transfer_pump'" in e for e in errors)
    assert _lock_is_held() is True

    pump.device.fail_close = False  # Aufraeumen
    assert manager.close_all() == []


def test_manager_retry_after_failed_cleanup_closes_everything_and_releases_lock():
    manager = _manager_with("transfer_pump", "drain_valve_0")
    pump, valve = manager.get("transfer_pump"), manager.get("drain_valve_0")
    pump.on()
    valve.on()
    pump.device.fail_off = True

    assert manager.close_all() != []
    assert _lock_is_held() is True

    # Der Fehler besteht fort: weiterhin gemeldet, Sperre weiter gehalten, Retry moeglich.
    again = manager.close_all()
    assert any("transfer_pump" in e for e in again)
    assert _lock_is_held() is True
    assert pump.device.is_closed is False and pump.is_active is True

    pump.device.fail_off = False
    assert manager.close_all() == []

    assert all(device.is_closed for device in FakeOutputDevice.instances)
    assert pump.is_active is False and valve.is_active is False
    assert _lock_is_held() is False
    assert manager._lock_file is None


def test_manager_cleanup_is_repeatable_after_retry_released_the_lock():
    manager = _manager_with("transfer_pump")
    pump = manager.get("transfer_pump")
    pump.on()
    pump.device.fail_off = True
    manager.close_all()
    pump.device.fail_off = False
    assert manager.close_all() == []
    calls_after_success = [list(d.calls) for d in FakeOutputDevice.instances]

    assert manager.safe_shutdown_all() == []
    assert manager.close_all() == []

    assert [list(d.calls) for d in FakeOutputDevice.instances] == calls_after_success
    assert _lock_is_held() is False
