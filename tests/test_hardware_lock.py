"""
Phase 1 (Teil C) regression tests: the cross-process fcntl.flock in
hardware/actuator_manager.py::ActuatorManager. ActuatorManager.__init__()
itself never touches gpiozero (only .add() does), so constructing instances
here is safe without any real pin ever being claimed.

AP-00: Diese Tests benutzen AUSSCHLIESSLICH eine temporaere Test-Sperrdatei
(autouse-Fixture in tests/conftest.py setzt hardware.actuator_manager.LOCK_PATH
und die Umgebungsvariable CDS_TEST_HARDWARE_LOCK_PATH, die auch der
Subprozess ueber die Test-Isolation uebernimmt). Die produktive Sperrdatei
logs/.hardware.lock wird weder geoeffnet noch gesperrt; die Sperrsemantik
(prozessuebergreifend, exklusiv, nicht-blockierend) ist unveraendert.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import hardware.actuator_manager as actuator_manager
from hardware.actuator_manager import ActuatorManager, HardwareLockError

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_LOCK = REPO_ROOT / "logs" / ".hardware.lock"


@pytest.fixture
def lock_path(tmp_path) -> Path:
    path = actuator_manager.LOCK_PATH  # bereits durch die autouse-Fixture auf tmp_path gesetzt
    assert path.parent == tmp_path
    assert path != PRODUCTION_LOCK
    return path


def test_second_in_process_lock_attempt_is_rejected(lock_path):
    first = ActuatorManager(active_low=True)
    try:
        try:
            ActuatorManager(active_low=True)
            assert False, "a second ActuatorManager should not have acquired the lock"
        except HardwareLockError:
            pass
    finally:
        first.close_all()

    # After release, a new instance must succeed again.
    second = ActuatorManager(active_low=True)
    second.close_all()
    assert lock_path.exists()


def test_lock_is_enforced_against_a_real_separate_process(lock_path):
    # Der Kindprozess bekommt die Test-Sperrdatei ueber die geerbte Umgebung
    # (CDS_TEST_HARDWARE_LOCK_PATH) + sitecustomize-Guard; zur Sicherheit
    # wird sie hier zusaetzlich ausdruecklich gesetzt und im Kind geprueft.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                "sys.path.insert(0, '.'); "
                "from hardware import actuator_manager as m; "
                f"assert str(m.LOCK_PATH) == {str(lock_path)!r}, m.LOCK_PATH; "
                "am = m.ActuatorManager(active_low=True); "
                "print('LOCKED', flush=True); "
                "time.sleep(3)"
            ),
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        text=True,
    )

    try:
        # Warten, bis das Kind die Sperre tatsaechlich haelt (statt fixem sleep).
        assert proc.stdout.readline().strip() == "LOCKED"
        try:
            ActuatorManager(active_low=True)
            assert False, "lock should have been held by the subprocess"
        except HardwareLockError:
            pass
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # The kernel releases the lock automatically once the subprocess exits.
    fourth = ActuatorManager(active_low=True)
    fourth.close_all()
