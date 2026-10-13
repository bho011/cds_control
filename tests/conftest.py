"""
Shared fixtures for the pytest suite. See the "Architecture-Hardening-Roadmap"
plan, Phase 4, for why these tests exist and what they replaced
(scripts/manual_tests/phase1_locking_test.py / phase2_watchdog_test.py /
phase3_settings_validation_test.py).

None of these fixtures touch real GPIO/OPC-UA: process controller tests
monkeypatch ManualDrainJog._run/TankCleaningController._run to no-op
stand-ins before ever constructing a ProcessController.

AP-00: Unabhaengig davon (ein veralteter _run-Stub darf nie wieder echte
Hardware freilegen) installiert dieses Modul VOR der Test-Collection die
zentrale Hardware-Isolation aus tests/_hardware_guard.py: GPIO, Serial,
Hardware-Sperrdatei, Produktivdaten und Netzwerk sind blockiert; Subprozesse
erben die Isolation ueber tests/_guard_site/sitecustomize.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Muss vor jedem Import von Produktiv-/Hardwaremodulen laufen (conftest wird
# vor den Testmodulen importiert, also auch vor deren Import-Zeit-Effekten).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hardware_guard  # noqa: E402

_PRODUCTION_LOCK = _hardware_guard.REPO_ROOT / "logs" / ".hardware.lock"


def _lock_fingerprint():
    try:
        st = _PRODUCTION_LOCK.stat()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


_hardware_guard.install()
_LOCK_FINGERPRINT_AT_START = _lock_fingerprint()
# Subprozesse laden die Isolation ueber sitecustomize (siehe _guard_site/).
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (str(Path(__file__).resolve().parent / "_guard_site"), os.environ.get("PYTHONPATH", "")) if p
)

from nicegui_dashboard.process_controller import ProcessController  # noqa: E402


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


@pytest.fixture(autouse=True)
def _hardware_isolation(tmp_path, monkeypatch):
    """Pro Test: eigene Test-Sperrdatei; jeder (auch verschluckte) blockierte
    Hardwarezugriff laesst den Test scheitern, ausser er wurde ueber
    `expect_blocked_access` ausdruecklich erwartet."""
    import hardware.actuator_manager as actuator_manager

    lock_path = tmp_path / "test-hardware.lock"
    monkeypatch.setattr(actuator_manager, "LOCK_PATH", lock_path)
    monkeypatch.setenv(_hardware_guard.ENV_LOCK_PATH, str(lock_path))
    _hardware_guard.BLOCKED_ATTEMPTS.clear()
    _hardware_guard.drain_child_attempts()
    _hardware_guard.BLOCKED_ATTEMPTS.clear()
    yield
    _hardware_guard.drain_child_attempts()  # Blockaden aus Kindprozessen
    attempts = list(_hardware_guard.BLOCKED_ATTEMPTS)
    _hardware_guard.BLOCKED_ATTEMPTS.clear()
    assert not attempts, "Unerwarteter Hardwarezugriff im Test blockiert:\n" + "\n".join(attempts)


@pytest.fixture
def expect_blocked_access():
    """Fuer Tests, die einen Hardwarezugriff bewusst provozieren: liefert die
    Liste der blockierten Versuche und quittiert sie am Testende."""
    yield _hardware_guard.BLOCKED_ATTEMPTS
    _hardware_guard.drain_child_attempts()
    _hardware_guard.BLOCKED_ATTEMPTS.clear()


def pytest_sessionfinish(session, exitstatus):
    if _lock_fingerprint() != _LOCK_FINGERPRINT_AT_START:
        session.exitstatus = 1
        print("\n[AP-00] FEHLER: logs/.hardware.lock wurde waehrend des Testlaufs veraendert!")
