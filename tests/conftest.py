"""
Shared fixtures for the pytest suite. See the "Architecture-Hardening-Roadmap"
plan, Phase 4, for why these tests exist and what they replaced
(scripts/manual_tests/phase1_locking_test.py / phase2_watchdog_test.py /
phase3_settings_validation_test.py).

None of these fixtures touch real GPIO/OPC-UA: process controller tests
monkeypatch ManualDrainJog._run/TankCleaningController._run to no-op
stand-ins before ever constructing a ProcessController.
"""

from __future__ import annotations

import pytest

from nicegui_dashboard.process_controller import ProcessController


FAKE_SENSOR_SNAPSHOT = {"mixer": {"volume_liters_calc": 10.0}, "ro": {"volume_liters_calc": 500.0}}

FAKE_PROCESS_SETTINGS = {
    "hardware_execution_enabled": True,
    "required_confirmation_text": "confirmed",
}

FAKE_TANK_CLEANING_SETTINGS = {
    "hardware_execution_enabled": True,
    "required_confirmation_text": "confirmed",
}


@pytest.fixture
def controller() -> ProcessController:
    """A ProcessController wired to fake settings/sensor data, no real files touched."""
    pc = ProcessController(get_sensor_snapshot=lambda: FAKE_SENSOR_SNAPSHOT)
    pc.load_settings = lambda: dict(FAKE_PROCESS_SETTINGS)
    pc.load_tank_cleaning_settings = lambda: dict(FAKE_TANK_CLEANING_SETTINGS)
    yield pc
    pc.shutdown()
