"""
Shared scaffolding for dashboard-driven background hardware processes.

Both ManualDrainJog (process/manual_drain_jog.py) and TankCleaningController
(process/tank_cleaning.py) run as a background thread inside the NiceGUI
dashboard process, started and stopped from button clicks, and both must
react to a stop request (their own Stop button, or an Emergency Stop
elsewhere on the dashboard) within well under a second.

Before this module existed, that thread/lock/status bookkeeping was
duplicated almost verbatim between the two classes. This base class holds
the part that is genuinely identical:

    - the lock, stop event, thread handle and common status fields
    - is_active()
    - the two safety-gate checks every start() must pass
      (hardware_execution_enabled, current sensor snapshot)
    - the state reset + thread spawn done on a successful start()
    - stop() (set the event, join with a bounded timeout, report status)

Everything that differs between the two processes - which settings they
read, how many phases they have, what they publish - stays in the
subclasses. This is intentionally not a "do everything" base class: start()
and get_status() are implemented per subclass because their shape genuinely
differs (progress-based vs. phase-based), forcing a shared shape there would
make both harder to read, not easier.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable


class BackgroundHardwareProcess:
    """
    Base class for a start/stop-able background hardware process that is
    safe to drive from NiceGUI button clicks.

    Subclasses must:
        - set the class attribute `label` (used in default status messages)
        - implement `_run(self, settings)`, the actual work, executed in the
          background thread
        - implement `start(self, settings)`, calling
          `_check_start_preconditions_locked()` and `_begin_run_locked()`
          (see their docstrings) while holding `self._lock`
        - implement `get_status(self)`

    All protected attributes below are only ever mutated while holding
    `self._lock` (an RLock, so nested acquisition from the same thread -
    e.g. calling a `_locked` helper from within a `with self._lock:` block -
    is safe).
    """

    #: Human-readable name used in default status/result messages, e.g. "Tank Cleaning".
    label: str = "Process"
    #: Overrides `label` only for the two stop() messages, if a subclass's
    #: existing wording differs from `label` (kept for exact backward
    #: compatibility with pre-refactor message text).
    stop_label: str | None = None
    #: How long stop() waits for the background thread to actually exit.
    #: The phase loops in _run() react to the stop event within ~0.5s, so
    #: this is a safety margin, not the expected normal-case wait.
    stop_join_timeout_seconds: float = 2.0

    def __init__(self, get_sensor_snapshot: Callable[[], dict[str, Any] | None]) -> None:
        self.get_sensor_snapshot = get_sensor_snapshot

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._actuators = None
        self._mqtt_publisher = None
        self._started_at_iso: str | None = None
        self._stop_reason: str | None = None
        self._last_error: str | None = None
        self._last_message = f"{self.label} ready."

    @property
    def _stop_label(self) -> str:
        return self.stop_label or self.label

    def is_active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _check_start_preconditions_locked(self, settings: dict[str, Any]) -> str | None:
        """
        The two safety gates every process must pass before starting.
        Call while holding self._lock. Returns a block message if the
        process must not start, or None if it is safe to proceed.
        """

        if not bool(settings.get("hardware_execution_enabled", False)):
            self._last_error = "hardware_execution_enabled is false."
            self._last_message = f"{self.label} locked by hardware safety setting."
            return self._last_message

        if self.get_sensor_snapshot() is None:
            self._last_error = "No current sensor snapshot available."
            self._last_message = f"{self.label} locked: no current sensor snapshot."
            return self._last_message

        return None

    def _begin_run_locked(self, thread_name: str, settings: dict[str, Any]) -> None:
        """
        Common state reset and thread spawn for a successful start().
        Call while holding self._lock, after any subclass-specific state
        (target values, initial phase, ...) has already been set.
        """

        self._stop_event.clear()
        self._stop_reason = None
        self._last_error = None
        self._started_at_iso = datetime.now().isoformat(timespec="seconds")

        self._thread = threading.Thread(
            target=self._run,
            args=(settings,),
            daemon=False,
            name=thread_name,
        )
        self._thread.start()

    def request_stop(self, reason: str = "stopped_by_user") -> None:
        """
        Signal-only half of stop(): sets the stop reason/event and returns
        immediately, without joining the background thread. Safe to call
        while holding a caller-side lock (e.g. ProcessController._lock),
        since it never blocks. Always pair with a later wait_stopped() call -
        calling wait_stopped() without a prior request_stop() on a thread
        that never gets signalled will block until stop_join_timeout_seconds.
        """
        with self._lock:
            self._stop_reason = reason
            self._stop_event.set()

    def wait_stopped(self, timeout: float | None = None) -> dict[str, Any]:
        """
        Join-only half of stop(): waits for the background thread (if any)
        to exit and reports the resulting status. Must be called without
        holding any lock that the background thread's teardown might need,
        since this can block for up to `timeout` seconds.
        """
        if timeout is None:
            timeout = self.stop_join_timeout_seconds

        thread_to_join: threading.Thread | None = None

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                thread_to_join = self._thread

        if thread_to_join is not None and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=timeout)

        with self._lock:
            if self.is_active():
                return self._result(True, f"{self._stop_label} stop requested; cleanup still running.")

        return self._result(True, f"{self._stop_label} stopped.")

    def stop(self, reason: str = "stopped_by_user") -> dict[str, Any]:
        self.request_stop(reason)
        return self.wait_stopped(self.stop_join_timeout_seconds)

    def shutdown(self) -> None:
        self.stop(reason="shutdown")

    def _run(self, settings: dict[str, Any]) -> None:
        raise NotImplementedError

    @staticmethod
    def _result(success: bool, message: str) -> dict[str, Any]:
        return {"success": success, "message": message}
