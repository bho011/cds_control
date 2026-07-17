"""
Phase 1 (Teil C) regression tests: the cross-process fcntl.flock in
hardware/actuator_manager.py::ActuatorManager. ActuatorManager.__init__()
itself never touches gpiozero (only .add() does), so constructing instances
here is safe without any real pin ever being claimed.

Note: the second test acquires the REAL lock path
(hardware/actuator_manager.py::LOCK_PATH), the same one the live dashboard/
water_cycle.py/calibration_mixing_tank.py use. If a real hardware process
happens to be actively running at the exact moment this test runs, the
"second lock must fail" assertions could see a lock held by that unrelated
process instead of the one this test created - that is the mechanism working
correctly, not a test bug, but it does mean this test is not perfectly
isolated from the live system's state.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from hardware.actuator_manager import ActuatorManager, HardwareLockError

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_second_in_process_lock_attempt_is_rejected():
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


def test_lock_is_enforced_against_a_real_separate_process():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                "sys.path.insert(0, '.'); "
                "from hardware.actuator_manager import ActuatorManager; "
                "am = ActuatorManager(active_low=True); "
                "time.sleep(3)"
            ),
        ],
        cwd=str(REPO_ROOT),
    )

    try:
        time.sleep(1.0)  # let the subprocess acquire the lock first
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
