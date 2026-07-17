"""
No-hardware regression test for the Phase 1 locking/emergency-stop redesign
(see the "Architecture-Hardening-Roadmap" plan, Phase 1).

Never touches GPIO/gpiozero/OPC-UA: ManualDrainJog._run and
TankCleaningController._run are monkeypatched to no-op stand-ins before any
ProcessController is constructed, and ActuatorManager.__init__ (used
directly in the flock test) never calls .add(), so no real pin is ever
claimed. Safe to run on the real Pi at any time.

Usage:
    cd ~/cds_control && .venv/bin/python3 scripts/manual_tests/phase1_locking_test.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from process.manual_drain_jog import ManualDrainJog
from process.tank_cleaning import TankCleaningController
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


def _fake_manual_drain_run(self, settings) -> None:
    # Mimics the real _run's shape (wait on the stop event) without ever
    # importing gpio_config/hardware/ActuatorManager.
    self._stop_event.wait(5.0)


def _fake_tank_cleaning_run(self, settings) -> None:
    self._stop_event.wait(5.0)


def _fake_slow_manual_drain_run(self, settings) -> None:
    # Deliberately ignores the stop event for a while, simulating a slow
    # teardown - used to prove emergency_stop() no longer blocks get_status().
    time.sleep(1.5)


def build_test_controller() -> ProcessController:
    pc = ProcessController(get_sensor_snapshot=lambda: FAKE_SENSOR_SNAPSHOT)
    pc.load_settings = lambda: dict(FAKE_PROCESS_SETTINGS)
    pc.load_tank_cleaning_settings = lambda: dict(FAKE_TANK_CLEANING_SETTINGS)
    return pc


def reset_between_iterations(pc: ProcessController) -> None:
    pc.manual_drain_jog.stop(reason="test_cleanup")
    pc.tank_cleaning.stop(reason="test_cleanup")


def test_no_double_start_race(iterations: int = 200) -> None:
    print(f"[TEST] Teil A: start race, {iterations} Iterationen ...")
    ManualDrainJog._run = _fake_manual_drain_run
    TankCleaningController._run = _fake_tank_cleaning_run

    pc = build_test_controller()
    both_active_count = 0
    exactly_one_success_count = 0

    for i in range(iterations):
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def call_manual() -> None:
            barrier.wait()
            results["manual"] = pc.start_manual_drain_jog()

        def call_tank() -> None:
            barrier.wait()
            results["tank"] = pc.start_tank_cleaning("confirmed")

        t1 = threading.Thread(target=call_manual)
        t2 = threading.Thread(target=call_tank)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        both_active = pc.manual_drain_jog.is_active() and pc.tank_cleaning.is_active()
        if both_active:
            both_active_count += 1
            print(f"  [FAIL] iteration {i}: BOTH manual_drain_jog and tank_cleaning are active!")

        success_count = sum(1 for r in results.values() if r.get("success"))
        if success_count == 1:
            exactly_one_success_count += 1

        reset_between_iterations(pc)

    pc.shutdown()

    assert both_active_count == 0, f"{both_active_count}/{iterations} Iterationen hatten beide Prozesse gleichzeitig aktiv!"
    print(
        f"  [OK] Nie beide gleichzeitig aktiv. "
        f"Genau ein Start gewann in {exactly_one_success_count}/{iterations} Iterationen "
        f"(Rest: seltener Fall, in dem beide Vorprüfungen vor Erreichen des atomaren Blocks fehlschlugen)."
    )


async def test_emergency_stop_does_not_block_get_status() -> None:
    print("[TEST] Teil B: emergency_stop() darf get_status() nicht blockieren ...")
    ManualDrainJog._run = _fake_slow_manual_drain_run
    TankCleaningController._run = _fake_tank_cleaning_run

    pc = build_test_controller()

    start_result = pc.start_manual_drain_jog()
    assert start_result["success"], f"Setup fehlgeschlagen: {start_result}"
    assert pc.manual_drain_jog.is_active()

    stop_task = asyncio.create_task(pc.emergency_stop())
    await asyncio.sleep(0.15)  # give emergency_stop's signal phase time to run and release the lock

    measured = time.monotonic()
    status = pc.get_status()
    elapsed = time.monotonic() - measured

    assert elapsed < 0.5, f"get_status() blockierte {elapsed:.2f}s während emergency_stop() lief!"
    assert status["manual_drain_jog"]["is_active"] is True, "manual_drain_jog sollte während des Cleanups noch aktiv sein"

    # Silent-negation check: a start attempt while a stop is genuinely still
    # in flight must never spawn a new thread or clear the pending stop
    # event. ManualDrainJog.start() already handles "already active" by
    # returning success=True without touching the thread/stop event at all
    # (it only calls _begin_run_locked(), which is what clears the stop
    # event, when is_active() is False) - so the actual invariant to check
    # is thread identity + the stop event staying set, not the "success" flag.
    thread_before = pc.manual_drain_jog._thread
    stop_event_was_set = pc.manual_drain_jog._stop_event.is_set()
    reentry_result = pc.start_manual_drain_jog()
    assert reentry_result["success"], "unexpected: reentry should be the harmless 'already running' case"
    assert pc.manual_drain_jog._thread is thread_before, (
        "start_manual_drain_jog() durfte während eines laufenden Emergency Stops "
        "keinen neuen Thread starten - das wäre die Silent-Negation!"
    )
    assert not stop_event_was_set or pc.manual_drain_jog._stop_event.is_set(), (
        "start_manual_drain_jog() hat das bereits gesetzte _stop_event während "
        "eines laufenden Emergency Stops zurückgesetzt - Silent Negation!"
    )

    stop_result = await stop_task
    assert stop_result["success"]

    pc.shutdown()
    print(f"  [OK] get_status() antwortete nach {elapsed:.3f}s (während emergency_stop() noch lief).")
    print("  [OK] Start während laufendem Emergency Stop wurde korrekt blockiert (keine Silent Negation).")


def test_cross_process_hardware_lock() -> None:
    print("[TEST] Teil C: prozessübergreifende Sperre (fcntl.flock) ...")
    from hardware.actuator_manager import ActuatorManager, HardwareLockError

    am1 = ActuatorManager(active_low=True)
    try:
        try:
            ActuatorManager(active_low=True)
            raise AssertionError("Zweite ActuatorManager-Instanz hätte fehlschlagen müssen!")
        except HardwareLockError:
            print("  [OK] Zweite In-Prozess-Instanz wurde korrekt mit HardwareLockError abgewiesen.")
    finally:
        am1.close_all()

    am3 = ActuatorManager(active_low=True)
    am3.close_all()
    print("  [OK] Nach Freigabe konnte eine neue Instanz die Sperre wieder bekommen.")

    import subprocess

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
        cwd=str(Path(__file__).resolve().parent.parent.parent),
    )

    try:
        time.sleep(1.0)  # let the subprocess acquire the lock first
        try:
            ActuatorManager(active_low=True)
            raise AssertionError("Sperre hätte durch den Subprozess gehalten sein müssen!")
        except HardwareLockError:
            print("  [OK] Sperre wird korrekt auch gegen einen echten separaten Prozess durchgesetzt.")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # After the subprocess is gone, the kernel must have released the lock.
    am4 = ActuatorManager(active_low=True)
    am4.close_all()
    print("  [OK] Sperre wurde nach Prozessende automatisch vom Kernel freigegeben.")


def main() -> None:
    print("=" * 60)
    print("Phase 1 Locking/Emergency-Stop - Regressionstest (keine Hardware)")
    print("=" * 60)

    test_no_double_start_race()
    asyncio.run(test_emergency_stop_does_not_block_get_status())
    test_cross_process_hardware_lock()

    print()
    print("[RESULT] Alle Phase-1-Tests erfolgreich.")


if __name__ == "__main__":
    main()
