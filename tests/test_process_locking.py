"""
Phase 1 (Teil A+B) regression tests: the start-race fix in
process_controller.py::start_manual_drain_jog/start_tank_cleaning, and the
emergency_stop() lock-scope/async fix. No real GPIO/OPC-UA involved -
ManualDrainJog._run/TankCleaningController._run are monkeypatched to no-op
stand-ins before any ProcessController touches them.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from process.manual_drain_jog import ManualDrainJog
from process.pump_prime import PumpPrimeController
from process.tank_cleaning import TankCleaningController


def _fake_run_waits_for_stop(self, settings) -> None:
    self._stop_event.wait(5.0)


def _fake_run_ignores_stop_briefly(self, settings) -> None:
    # Deliberately ignores the stop event for a while, simulating a slow
    # teardown - used to prove emergency_stop() no longer blocks get_status().
    time.sleep(1.5)


@pytest.fixture(autouse=True)
def fast_background_processes(monkeypatch):
    """Default stand-in for both background processes - overridden per-test where needed."""
    monkeypatch.setattr(ManualDrainJog, "_run", _fake_run_waits_for_stop)
    monkeypatch.setattr(TankCleaningController, "_run", _fake_run_waits_for_stop)
    monkeypatch.setattr(PumpPrimeController, "_run", _fake_run_waits_for_stop)
    # Diese generischen Locking-Tests wollen nur die ProcessController-Guards
    # gegeneinander pruefen, nicht PumpPrimeController's eigene Mapping-/
    # Kalibrierungs-Gates (die sind vollstaendig in
    # tests/test_pump_prime_controller.py abgedeckt) - deshalb hier bewusst
    # entkoppelt von echten config/peristaltic_*.json-Dateien.
    monkeypatch.setattr(PumpPrimeController, "_check_start_preconditions_locked", lambda self, settings: None)


def _reset(controller) -> None:
    controller.manual_drain_jog.stop(reason="test_cleanup")
    controller.tank_cleaning.stop(reason="test_cleanup")
    controller.prime.stop(reason="test_cleanup")


def test_start_manual_drain_and_tank_cleaning_never_both_active(controller):
    """
    The race this closes: start_manual_drain_jog()/start_tank_cleaning() used
    to check is_active() and mutate state under two different locks with a
    gap in between, so two concurrent calls could both pass the precondition
    check and both end up running - both driving the same physical GPIO pins.
    """
    iterations = 200
    both_active_count = 0
    exactly_one_success_count = 0

    for _ in range(iterations):
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def call_manual() -> None:
            barrier.wait()
            results["manual"] = controller.start_manual_drain_jog()

        def call_tank() -> None:
            barrier.wait()
            results["tank"] = controller.start_tank_cleaning("confirmed")

        t1 = threading.Thread(target=call_manual)
        t2 = threading.Thread(target=call_tank)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if controller.manual_drain_jog.is_active() and controller.tank_cleaning.is_active():
            both_active_count += 1

        if sum(1 for r in results.values() if r.get("success")) == 1:
            exactly_one_success_count += 1

        _reset(controller)

    assert both_active_count == 0, f"{both_active_count}/{iterations} iterations had both processes active at once"
    assert exactly_one_success_count > 0, "the race scenario never actually triggered a contested start"


def test_emergency_stop_does_not_block_get_status(controller, monkeypatch):
    """
    emergency_stop() used to hold ProcessController._lock across a blocking
    thread.join() of up to ~7s. Since NiceGUI dispatches handlers on a single
    event-loop thread, that held the whole dashboard hostage for every
    connected client during an emergency stop, not just get_status(). Fixed
    by splitting stop() into request_stop()/wait_stopped() and making
    emergency_stop() async with the wait phase in a worker thread.
    """
    monkeypatch.setattr(ManualDrainJog, "_run", _fake_run_ignores_stop_briefly)

    start_result = controller.start_manual_drain_jog()
    assert start_result["success"], start_result
    assert controller.manual_drain_jog.is_active()

    async def scenario():
        stop_task = asyncio.create_task(controller.emergency_stop())
        await asyncio.sleep(0.15)  # let the signal phase run and release the lock

        started = time.monotonic()
        status = controller.get_status()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"get_status() blocked for {elapsed:.2f}s during emergency_stop()"
        assert status["manual_drain_jog"]["is_active"] is True

        stop_result = await stop_task
        assert stop_result["success"]

    asyncio.run(scenario())


def test_start_during_emergency_stop_does_not_silently_negate_it(controller, monkeypatch):
    """
    Silent-negation scenario this closes: BackgroundHardwareProcess's
    _begin_run_locked() clears the stop event whenever a NEW thread is
    spawned. If a start() call could interleave with emergency_stop()'s
    signalling, it could wipe out a just-issued stop. With both now
    serialized under ProcessController._lock, a start attempt while the
    original run is still active must be the harmless "already running"
    case, not a fresh thread.
    """
    monkeypatch.setattr(ManualDrainJog, "_run", _fake_run_ignores_stop_briefly)

    start_result = controller.start_manual_drain_jog()
    assert start_result["success"], start_result

    async def scenario():
        stop_task = asyncio.create_task(controller.emergency_stop())
        await asyncio.sleep(0.15)

        thread_before = controller.manual_drain_jog._thread
        reentry_result = controller.start_manual_drain_jog()

        assert reentry_result["success"], "unexpected: reentry should be the harmless 'already running' case"
        assert controller.manual_drain_jog._thread is thread_before, (
            "start_manual_drain_jog() spawned a new thread during an in-flight "
            "emergency stop - that would be the silent-negation bug"
        )

        await stop_task

    asyncio.run(scenario())


def test_start_prime_and_tank_cleaning_never_both_active(controller):
    """
    Same race as test_start_manual_drain_and_tank_cleaning_never_both_active,
    but for the newly integrated Prime controller: start_prime() and
    start_tank_cleaning() must never both succeed for the same instant, since
    both ultimately drive real pumps.
    """
    iterations = 200
    both_active_count = 0
    exactly_one_success_count = 0

    for _ in range(iterations):
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def call_prime() -> None:
            barrier.wait()
            results["prime"] = controller.start_prime({"MCU_B": ["P1"]})

        def call_tank() -> None:
            barrier.wait()
            results["tank"] = controller.start_tank_cleaning("confirmed")

        t1 = threading.Thread(target=call_prime)
        t2 = threading.Thread(target=call_tank)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if controller.prime.is_active() and controller.tank_cleaning.is_active():
            both_active_count += 1

        if sum(1 for r in results.values() if r.get("success")) == 1:
            exactly_one_success_count += 1

        _reset(controller)

    assert both_active_count == 0, f"{both_active_count}/{iterations} iterations had both processes active at once"
    assert exactly_one_success_count > 0, "the race scenario never actually triggered a contested start"


def test_start_prime_and_manual_drain_never_both_active(controller):
    iterations = 200
    both_active_count = 0
    exactly_one_success_count = 0

    for _ in range(iterations):
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def call_prime() -> None:
            barrier.wait()
            results["prime"] = controller.start_prime({"MCU_B": ["P1"]})

        def call_manual() -> None:
            barrier.wait()
            results["manual"] = controller.start_manual_drain_jog()

        t1 = threading.Thread(target=call_prime)
        t2 = threading.Thread(target=call_manual)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if controller.prime.is_active() and controller.manual_drain_jog.is_active():
            both_active_count += 1

        if sum(1 for r in results.values() if r.get("success")) == 1:
            exactly_one_success_count += 1

        _reset(controller)

    assert both_active_count == 0, f"{both_active_count}/{iterations} iterations had both processes active at once"
    assert exactly_one_success_count > 0, "the race scenario never actually triggered a contested start"


def test_emergency_stop_stops_prime_too(controller, monkeypatch):
    """
    Mirrors test_emergency_stop_does_not_block_get_status: emergency_stop()
    must signal Prime to stop too (not just Manual Drain Jog/Tank Cleaning),
    and get_status() must stay responsive while Prime's stop is still in
    progress.
    """
    monkeypatch.setattr(PumpPrimeController, "_run", _fake_run_ignores_stop_briefly)

    start_result = controller.start_prime({"MCU_B": ["P1"]})
    assert start_result["success"], start_result
    assert controller.prime.is_active()

    async def scenario():
        stop_task = asyncio.create_task(controller.emergency_stop())
        await asyncio.sleep(0.15)  # let the signal phase run and release the lock

        started = time.monotonic()
        status = controller.get_status()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"get_status() blocked for {elapsed:.2f}s during emergency_stop()"
        assert status["prime"]["is_active"] is True

        stop_result = await stop_task
        assert stop_result["success"]

    asyncio.run(scenario())

    # request_stop(reason="emergency_stop") muss den Grund korrekt bis in
    # PumpPrimeController._stop_reason durchreichen - die daraus abgeleitete
    # completion_reason="emergency_stop" (statt des generischen "user_abort")
    # ist separat in tests/test_pump_prime_controller.py::
    # test_emergency_stop_reason_logs_as_emergency_stop_not_user_abort mit
    # einem echten (nicht gefakten) _run() nachgewiesen - hier reicht die
    # Bestätigung, dass ProcessController.emergency_stop() den richtigen
    # Grund überhaupt weitergibt.
    assert controller.prime.get_status()["stop_reason"] == "emergency_stop"


def test_prime_ignores_hardware_execution_enabled_but_other_processes_dont(controller):
    """
    Bewusste Architekturentscheidung (siehe process/pump_prime.py Modul-
    Docstring): Prime prüft hardware_execution_enabled NICHT - andere
    automatische Prozesse (Manual Drain Jog, Tank Cleaning) bleiben davon
    weiterhin abhängig. Die Ausnahme gilt ausschließlich für Prime, sie darf
    sich nicht unbeabsichtigt auf andere Prozesse übertragen.
    """
    controller.load_settings = lambda: {
        "hardware_execution_enabled": False,
        "required_confirmation_text": "confirmed",
    }
    controller.load_tank_cleaning_settings = lambda: {
        "hardware_execution_enabled": False,
        "required_confirmation_text": "confirmed",
    }

    prime_result = controller.start_prime({"MCU_B": ["P1"]})
    assert prime_result["success"] is True, prime_result
    _reset(controller)

    manual_drain_result = controller.start_manual_drain_jog()
    assert manual_drain_result["success"] is False
    assert "hardware safety setting" in manual_drain_result["message"]

    tank_cleaning_result = controller.start_tank_cleaning("confirmed")
    assert tank_cleaning_result["success"] is False
    assert "hardware safety setting" in tank_cleaning_result["message"]


def test_start_fill_and_measure_blocked_while_prime_active(controller):
    """
    The 3rd sibling's guard (start_fill_and_measure) must also treat an
    active Prime run as busy - this is checked before load_settings()/the
    active recipe are ever touched, so no recipe fixture is needed here.
    """
    start_result = controller.start_prime({"MCU_B": ["P1"]})
    assert start_result["success"], start_result
    assert controller.prime.is_active()

    result = controller.start_fill_and_measure("confirmed")

    assert result["success"] is False
    assert "Prime" in result["message"]

    _reset(controller)
