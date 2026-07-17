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


def _reset(controller) -> None:
    controller.manual_drain_jog.stop(reason="test_cleanup")
    controller.tank_cleaning.stop(reason="test_cleanup")


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
