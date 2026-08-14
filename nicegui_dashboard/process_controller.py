import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from domain.recipe_limits import MAX_PROCESS_VOLUME_L
from domain.recipe_model import RunOptions
from nicegui_dashboard.recipe_store import build_run_config, get_active_recipe, load_recipe_book
from process.fill_and_measure import FillAndMeasureController
from process.manual_drain_jog import ManualDrainJog
from process.pump_prime import PRIME_CHUNK_ML, PRIME_MAX_ML_PER_PUMP, PumpPrimeController
from process.tank_cleaning import TankCleaningController
from services.settings_validation import SettingField, validate_settings


SETTINGS_PATH = Path("config/process_settings.json")
TANK_CLEANING_SETTINGS_PATH = Path("config/tank_cleaning_settings.json")

# Required (no default listed) matches the hard settings["key"] reads in
# statemachine/fill_and_measure_state_machine.py - a missing key here means
# the process cannot run safely, so it must fail loudly at load time instead
# of falling back to a possibly-wrong code default (the bug this schema
# closes: no_fill_progress_timeout_seconds/min_fill_progress_liters/
# max_negative_level_drift_liters used to have their own, diverging,
# hardcoded fallback values in fill_and_measure_state_machine.py).
#
# Stays here (not moved into process/fill_and_measure.py): validated
# against the settings dict BEFORE process.fill_and_measure.FillAndMeasureController.start()
# is ever called (see start_fill_and_measure() below) - the sub-controller
# itself only receives an already-loaded, already-validated dict, exactly
# like TankCleaningController/ManualDrainJog already do. See
# Modularisierungs-Plan Phase 7 for why this avoids a process -> nicegui_dashboard
# layering inversion.
PROCESS_SETTINGS_SCHEMA = [
    SettingField("fill_mode", str, required=False, default="delta", allowed_values={"delta", "absolute"}),
    SettingField("target_add_liters", float, min_value=0.0),
    # max_value=MAX_PROCESS_VOLUME_L (185.0), not the 200.0 physical tank rim
    # capacity - every operational fill target is capped at the same limit.
    SettingField("target_total_liters", float, min_value=0.0, max_value=MAX_PROCESS_VOLUME_L),
    # max_value=MAX_PROCESS_VOLUME_L (185.0), not the 200.0 physical tank rim
    # capacity - an operational safety cap, single source of truth in
    # domain/recipe_limits.py (see docs/RECIPE_AND_DOSING_RULES.md).
    SettingField("max_mixer_liters", float, min_value=0.0, max_value=MAX_PROCESS_VOLUME_L),
    SettingField("min_ro_liters_required", float, min_value=0.0),
    SettingField("max_fill_seconds", float, min_value=0.0),
    SettingField("min_fill_progress_liters", float, min_value=0.0),
    SettingField("no_fill_progress_timeout_seconds", float, min_value=0.0),
    SettingField("max_negative_level_drift_liters", float, min_value=0.0),
    SettingField("level_filter_samples", int, required=False, default=5, min_value=1),
    SettingField("target_reached_confirm_samples", int, required=False, default=3, min_value=1),
    SettingField("level_settle_seconds", float, min_value=0.0),
    SettingField("sensor_stabilize_seconds", float, min_value=0.0),
    SettingField("enable_mixing_circulation", bool, required=False, default=False),
    SettingField("enable_sensor_circulation", bool, required=False, default=False),
    SettingField("hardware_execution_enabled", bool, required=False, default=False),
    SettingField("required_confirmation_text", str, required=False, default="confirmed"),
]

# Every key here is already read via settings.get(key, default) in
# process/tank_cleaning/, consistently (unlike process_settings.json,
# nothing here is a hard KeyError today) - the schema mirrors those same
# defaults rather than tightening them, so this only adds an early, clear
# error for genuinely wrong types/ranges without changing today's behavior.
TANK_CLEANING_SETTINGS_SCHEMA = [
    SettingField("hardware_execution_enabled", bool, required=False, default=False),
    SettingField("required_confirmation_text", str, required=False, default="confirmed"),
    # default/max_value=MAX_PROCESS_VOLUME_L (185.0), not the former 200.0 -
    # every operational fill target is capped at the same limit, the
    # physical 200 l rim capacity is never a usable operational value.
    SettingField(
        "target_fill_total_liters",
        float,
        required=False,
        default=MAX_PROCESS_VOLUME_L,
        min_value=0.0,
        max_value=MAX_PROCESS_VOLUME_L,
    ),
    SettingField(
        "max_mixer_liters",
        float,
        required=False,
        default=MAX_PROCESS_VOLUME_L,
        min_value=0.0,
        max_value=MAX_PROCESS_VOLUME_L,
    ),
    SettingField("min_ro_liters_required", float, required=False, default=20.0, min_value=0.0),
    SettingField("max_fill_seconds", float, required=False, default=900.0, min_value=0.0),
    SettingField("min_fill_progress_liters", float, required=False, default=0.5, min_value=0.0),
    SettingField("no_fill_progress_timeout_seconds", float, required=False, default=30.0, min_value=0.0),
    SettingField("max_negative_level_drift_liters", float, required=False, default=3.0, min_value=0.0),
    SettingField("level_filter_samples", int, required=False, default=5, min_value=1),
    SettingField("target_reached_confirm_samples", int, required=False, default=3, min_value=1),
    SettingField("cleaning_hold_seconds", float, required=False, default=300.0, min_value=0.0),
    SettingField("auto_circulation_enabled", bool, required=False, default=False),
    SettingField("auto_circulation_start_liters", float, required=False, default=30.0, min_value=0.0),
    SettingField("auto_circulation_stop_liters", float, required=False, default=25.0, min_value=0.0),
    SettingField("auto_circulation_outputs", list, required=False, default=[]),
    SettingField("transfer_pump_liters_per_minute", float, required=False, default=16.0, min_value=0.1),
    SettingField("drain_timeout_buffer_seconds", float, required=False, default=180.0, min_value=0.0),
    SettingField("empty_threshold_liters", float, required=False, default=0.3, min_value=0.0),
    SettingField("empty_confirm_samples", int, required=False, default=5, min_value=1),
    SettingField("valve_settle_seconds", float, required=False, default=1.0, min_value=0.0),
]


class ProcessController:
    """
    Koordiniert die vier Hintergrundprozesse (Fill-and-Measure, Manual Drain
    Jog, Tank Cleaning, Prime) aus NiceGUI heraus: exklusiver Start/Stop,
    Settings-Laden, Rezept-Merge, Status-Aggregation, Emergency Stop/Shutdown.
    Die eigentliche Prozesslogik jedes einzelnen Prozesses lebt in eigenen
    Controllern (process/fill_and_measure.py, process/manual_drain_jog.py,
    process/tank_cleaning/, process/pump_prime.py) - siehe deren Docstrings.

    Sicherheitsprinzip:
    - Beim Laden des Dashboards werden keine GPIOs initialisiert.
    - GPIOs werden erst nach gültiger Sicherheitsprüfung erzeugt.
    - hardware_execution_enabled muss true sein.
    - required_confirmation_text muss exakt passen.
    - Ein Emergency Stop darf nicht durch einen parallelen Update-Tick
      wieder überschrieben werden.
    """

    def __init__(
        self,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.get_sensor_snapshot = get_sensor_snapshot

        self._lock = threading.RLock()

        self.last_error: str | None = None
        self.last_message: str = "ProcessController initialized."

        # The full RunConfig (recipe + process_settings.json merge) actually
        # used by the run currently in progress, set once at start time and
        # never re-derived - see get_status() below. None whenever no run
        # has ever been started, or after a run has fully finished (teardown
        # complete) - get_status() then falls back to a fresh live preview
        # of the currently active recipe again.
        self._last_run_settings: dict[str, Any] | None = None

        self.fill_and_measure = FillAndMeasureController(
            get_sensor_snapshot=self.get_sensor_snapshot,
        )
        self.manual_drain_jog = ManualDrainJog(
            get_sensor_snapshot=self.get_sensor_snapshot,
        )
        self.tank_cleaning = TankCleaningController(
            get_sensor_snapshot=self.get_sensor_snapshot,
        )
        self.prime = PumpPrimeController(
            get_sensor_snapshot=self.get_sensor_snapshot,
        )

    def load_settings(self) -> dict[str, Any]:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            settings = json.load(file)

        return validate_settings(settings, PROCESS_SETTINGS_SCHEMA, str(SETTINGS_PATH))

    def load_tank_cleaning_settings(self) -> dict[str, Any]:
        with TANK_CLEANING_SETTINGS_PATH.open("r", encoding="utf-8") as file:
            settings = json.load(file)

        return validate_settings(
            settings, TANK_CLEANING_SETTINGS_SCHEMA, str(TANK_CLEANING_SETTINGS_PATH)
        )

    def _busy_reason(self, starting: str) -> str | None:
        """
        The one shared exclusivity check, replacing 8 near-identical
        copy-pasted boolean blocks (2 each across the four start_*()
        methods below - a first fail-fast check and a second, race-free
        check under the same lock right before actually starting).

        `starting` is which process is about to start
        ("fill_and_measure"/"manual_drain_jog"/"tank_cleaning"/"prime") -
        each variant below reproduces exactly the set of sibling checks
        (and exact message) the original code used for that process, which
        was NOT symmetric: start_tank_cleaning() already checked its own
        tank_cleaning.is_active() (harmless, always False before it starts,
        but present), while start_manual_drain_jog()/start_prime() did not
        check their own is_active() at all. That asymmetry is preserved
        here rather than "cleaned up", since changing it would be an
        actual (even if minor) behavior change.
        """
        fill_and_measure_busy = self.fill_and_measure.is_running or self.fill_and_measure.is_active()

        if starting == "fill_and_measure":
            if (
                fill_and_measure_busy
                or self.fill_and_measure._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
                or self.prime.is_active()
            ):
                return (
                    "Start blocked: process, cleanup, background thread, Manual Drain Jog, "
                    "Tank Cleaning, or Prime is still active."
                )
            return None

        if starting == "manual_drain_jog":
            if (
                fill_and_measure_busy
                or self.fill_and_measure._teardown_in_progress
                or self.tank_cleaning.is_active()
                or self.prime.is_active()
            ):
                return "Manual Drain Jog blocked: main process, cleanup, Tank Cleaning, or Prime is active."
            return None

        if starting == "tank_cleaning":
            if (
                fill_and_measure_busy
                or self.fill_and_measure._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
                or self.prime.is_active()
            ):
                return "Tank Cleaning blocked: another process, cleanup, Manual Drain Jog, or Prime is active."
            return None

        if starting == "prime":
            if (
                fill_and_measure_busy
                or self.fill_and_measure._teardown_in_progress
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
            ):
                return "Prime blocked: another process, cleanup, Manual Drain Jog, or Tank Cleaning is active."
            return None

        raise ValueError(f"Unknown process for _busy_reason(): {starting!r}")

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            run_in_progress = self.fill_and_measure.is_running or self.fill_and_measure._teardown_in_progress
            last_run_settings = self._last_run_settings

        if run_in_progress and last_run_settings is not None:
            # A process is actually running (or tearing down): show the
            # RunConfigSnapshot it was actually started with, never a fresh
            # live recomputation - the active recipe may have changed since
            # the run started (e.g. a different favorite was activated), but
            # that must not retroactively change what a running process
            # reports about itself.
            settings = dict(last_run_settings)
            settings_error = None
        else:
            try:
                settings = self.load_settings()
            except Exception as exc:
                settings = {}
                settings_error = str(exc)
            else:
                settings_error = None

                # Best-effort recipe preview: if the active recipe is missing or
                # invalid, fall back to the raw file settings rather than
                # breaking the whole status endpoint over a display value.
                try:
                    recipe = get_active_recipe(load_recipe_book())
                    settings = build_run_config(settings, recipe)
                except Exception:
                    pass

        try:
            tank_cleaning_settings = self.load_tank_cleaning_settings()
            tank_cleaning_settings_error = None
        except Exception as exc:
            tank_cleaning_settings = {}
            tank_cleaning_settings_error = str(exc)

        with self._lock:
            fill_and_measure_status = self.fill_and_measure.get_status()

            return {
                "is_running": fill_and_measure_status["is_running"],
                "thread_alive": fill_and_measure_status["thread_alive"],
                "teardown_in_progress": fill_and_measure_status["teardown_in_progress"],
                "state_name": fill_and_measure_status["state_name"],
                "error": fill_and_measure_status["error"],
                "last_message": fill_and_measure_status["last_message"],
                "last_start_request": fill_and_measure_status["last_start_request"],
                "settings_error": settings_error,

                "hardware_execution_enabled": settings.get(
                    "hardware_execution_enabled", False
                ),
                "required_confirmation_text": settings.get(
                    "required_confirmation_text", ""
                ),
                "fill_mode": settings.get("fill_mode"),
                "target_add_liters": settings.get("target_add_liters"),
                "target_total_liters": settings.get("target_total_liters"),
                "max_fill_seconds": settings.get("max_fill_seconds"),
                "enable_mixing_circulation": settings.get(
                    "enable_mixing_circulation", False
                ),
                "enable_sensor_circulation": settings.get(
                    "enable_sensor_circulation", False
                ),
                "recipe_run_config_snapshot": settings.get("recipe_run_config_snapshot"),

                "start_mixer_liters": fill_and_measure_status["start_mixer_liters"],
                "added_liters": fill_and_measure_status["added_liters"],
                "manual_drain_jog": self.manual_drain_jog.get_status(),

                "tank_cleaning": self.tank_cleaning.get_status(),
                "tank_cleaning_hardware_execution_enabled": tank_cleaning_settings.get(
                    "hardware_execution_enabled", False
                ),
                "tank_cleaning_required_confirmation_text": tank_cleaning_settings.get(
                    "required_confirmation_text", ""
                ),
                "tank_cleaning_target_liters": tank_cleaning_settings.get(
                    "target_fill_total_liters"
                ),
                "tank_cleaning_hold_seconds": tank_cleaning_settings.get(
                    "cleaning_hold_seconds"
                ),
                "tank_cleaning_settings_error": tank_cleaning_settings_error,

                "prime": self.prime.get_status(),
            }

    def start_fill_and_measure(
        self, confirmation_text: str, run_options: RunOptions | None = None
    ) -> dict[str, Any]:
        """
        run_options: per-run-only choices (sensor circulation, drain after
        process) - never sourced from the recipe. Defaults to RunOptions()
        (everything off) if omitted, matching the fail-closed contract of
        RunConfigSnapshot.build(): selecting/loading a recipe must never by
        itself turn anything on.
        """
        with self._lock:
            busy_message = self._busy_reason("fill_and_measure")
            if busy_message is not None:
                return self._result(False, busy_message)

        try:
            settings = self.load_settings()
        except Exception as exc:
            message = f"Settings konnten nicht geladen werden: {exc}"
            self._set_fill_and_measure_error(message)
            return self._result(False, message)

        try:
            recipe = get_active_recipe(load_recipe_book())
            settings = build_run_config(settings, recipe, run_options)
            settings = validate_settings(settings, PROCESS_SETTINGS_SCHEMA, "RunConfig (Rezept + process_settings.json)")
        except Exception as exc:
            message = f"Rezept konnte nicht angewendet werden: {exc}"
            self._set_fill_and_measure_error(message)
            return self._result(False, message)

        hardware_enabled = settings.get("hardware_execution_enabled", False)

        if not hardware_enabled:
            message = (
                "Start blockiert: hardware_execution_enabled ist false. "
                "Es wurden keine GPIOs initialisiert."
            )
            self._set_fill_and_measure_error(message)
            return self._result(False, message)

        required_text = settings.get("required_confirmation_text", "confirmed")

        if confirmation_text.strip() != required_text:
            message = "Start blockiert: Bestätigungstext ist falsch."
            self._set_fill_and_measure_error(message)
            return self._result(False, message)

        snapshot = self.get_sensor_snapshot()

        if snapshot is None:
            message = "Start blockiert: Kein aktueller SensorSnapshot vorhanden."
            self._set_fill_and_measure_error(message)
            return self._result(False, message)

        with self._lock:
            # Re-check and start atomically under the same lock: the precondition
            # check above only fails fast, it does not prevent a concurrent
            # start call for another process from slipping in between it and
            # fill_and_measure.start() actually flipping is_active(). This second
            # check-and-start is the one that actually has to be race-free.
            busy_message = self._busy_reason("fill_and_measure")
            if busy_message is not None:
                return self._result(False, busy_message)

            self._last_run_settings = settings
            result = self.fill_and_measure.start(settings)

            if not result.get("success"):
                self.last_error = result.get("message")

        return result

    async def emergency_stop(self) -> dict[str, Any]:
        """
        Signals every hardware process to stop, then waits for them to
        actually exit. The signalling half runs under self._lock but never
        blocks; the waiting half can take several seconds (thread joins) and
        deliberately runs in a worker thread (asyncio.to_thread) without
        holding self._lock, so get_status()/other dashboard calls are never
        stalled for the duration of an emergency stop - see Phase 1 plan.
        """
        with self._lock:
            self.last_message = "Emergency Stop requested from NiceGUI."

            try:
                if self.fill_and_measure.state_machine is not None:
                    self.fill_and_measure.state_machine.error("Emergency stop requested from NiceGUI.")
            except Exception as exc:
                self.last_error = f"State machine emergency stop failed: {exc}"

            try:
                self.fill_and_measure.request_stop(reason="emergency_stop")
            except Exception as exc:
                self.last_error = f"Fill-and-Measure emergency stop failed: {exc}"

            try:
                self.manual_drain_jog.request_stop(reason="emergency_stop")
            except Exception as exc:
                self.last_error = f"Manual Drain Jog emergency stop failed: {exc}"

            try:
                self.tank_cleaning.request_stop(reason="emergency_stop")
            except Exception as exc:
                self.last_error = f"Tank Cleaning emergency stop failed: {exc}"

            try:
                self.prime.request_stop(reason="emergency_stop")
            except Exception as exc:
                self.last_error = f"Prime emergency stop failed: {exc}"

            try:
                if self.fill_and_measure._actuators is not None:
                    self.fill_and_measure._actuators.safe_shutdown_all()
            except Exception as exc:
                self.last_error = f"Actuator emergency stop failed: {exc}"

        def _wait_for_everything() -> None:
            try:
                self.fill_and_measure.wait_stopped()
            except Exception as exc:
                self.last_error = f"Fill-and-Measure emergency stop failed: {exc}"

            try:
                self.manual_drain_jog.wait_stopped()
            except Exception as exc:
                self.last_error = f"Manual Drain Jog emergency stop failed: {exc}"

            try:
                self.tank_cleaning.wait_stopped()
            except Exception as exc:
                self.last_error = f"Tank Cleaning emergency stop failed: {exc}"

            try:
                self.prime.wait_stopped()
            except Exception as exc:
                self.last_error = f"Prime emergency stop failed: {exc}"

        await asyncio.to_thread(_wait_for_everything)

        with self._lock:
            if (
                self.fill_and_measure.is_active()
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
                or self.prime.is_active()
            ):
                return self._result(
                    True,
                    "Emergency Stop wurde ausgelöst. Cleanup läuft noch.",
                )

        return self._result(True, "Emergency Stop wurde ausgelöst und bestätigt.")

    def start_manual_drain_jog(self) -> dict[str, Any]:
        with self._lock:
            busy_message = self._busy_reason("manual_drain_jog")
            if busy_message is not None:
                return self._result(False, busy_message)

        try:
            settings = self.load_settings()
        except Exception as exc:
            message = f"Settings could not be loaded: {exc}"
            self._set_error(message)
            return self._result(False, message)

        # Re-check and start atomically under the same lock: the precondition
        # check above only fails fast, it does not prevent a concurrent
        # start_tank_cleaning() call from slipping in between it and
        # manual_drain_jog.start() actually flipping is_active(). This second
        # check-and-start is the one that actually has to be race-free.
        with self._lock:
            busy_message = self._busy_reason("manual_drain_jog")
            if busy_message is not None:
                return self._result(False, busy_message)

            result = self.manual_drain_jog.start(settings)

            self.last_message = result.get("message", "Manual Drain Jog start requested.")
            if not result.get("success"):
                self.last_error = result.get("message")

        return result

    def stop_manual_drain_jog(self) -> dict[str, Any]:
        result = self.manual_drain_jog.stop(reason="button_released")

        with self._lock:
            self.last_message = result.get("message", "Manual Drain Jog stop requested.")

        return result

    def shutdown(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """
        Für späteren NiceGUI/App-Shutdown:
        Stop anfordern, Aktoren abschalten, Thread kurz joinen.
        """
        with self._lock:
            self.last_message = "Controller shutdown requested."

            try:
                self.fill_and_measure.request_stop(reason="controller_shutdown")
            except Exception as exc:
                self.last_error = f"Fill-and-Measure shutdown failed: {exc}"

            try:
                self.manual_drain_jog.request_stop(reason="controller_shutdown")
            except Exception as exc:
                self.last_error = f"Manual Drain Jog shutdown failed: {exc}"

            try:
                self.tank_cleaning.request_stop(reason="controller_shutdown")
            except Exception as exc:
                self.last_error = f"Tank Cleaning shutdown failed: {exc}"

            try:
                self.prime.request_stop(reason="controller_shutdown")
            except Exception as exc:
                self.last_error = f"Prime shutdown failed: {exc}"

            try:
                if self.fill_and_measure._actuators is not None:
                    self.fill_and_measure._actuators.safe_shutdown_all()
            except Exception as exc:
                self.last_error = f"Actuator shutdown failed: {exc}"

        try:
            self.fill_and_measure.wait_stopped(timeout_seconds)
        except Exception as exc:
            self.last_error = f"Fill-and-Measure shutdown failed: {exc}"

        try:
            self.manual_drain_jog.wait_stopped()
        except Exception as exc:
            self.last_error = f"Manual Drain Jog shutdown failed: {exc}"

        try:
            self.tank_cleaning.wait_stopped()
        except Exception as exc:
            self.last_error = f"Tank Cleaning shutdown failed: {exc}"

        try:
            self.prime.wait_stopped()
        except Exception as exc:
            self.last_error = f"Prime shutdown failed: {exc}"

        with self._lock:
            if (
                self.fill_and_measure.is_active()
                or self.manual_drain_jog.is_active()
                or self.tank_cleaning.is_active()
                or self.prime.is_active()
            ):
                return self._result(False, "Shutdown timeout: Hintergrundthread läuft noch.")

        return self._result(True, "Controller shutdown abgeschlossen.")

    def start_tank_cleaning(self, confirmation_text: str) -> dict[str, Any]:
        with self._lock:
            busy_message = self._busy_reason("tank_cleaning")
            if busy_message is not None:
                return self._result(False, busy_message)

        try:
            settings = self.load_tank_cleaning_settings()
        except Exception as exc:
            message = f"Tank Cleaning settings could not be loaded: {exc}"
            self._set_error(message)
            return self._result(False, message)

        required_text = settings.get("required_confirmation_text", "confirmed")

        if confirmation_text.strip() != required_text:
            message = "Tank Cleaning blocked: confirmation text is wrong."
            self._set_error(message)
            return self._result(False, message)

        # Re-check and start atomically under the same lock: the precondition
        # check above only fails fast, it does not prevent a concurrent
        # start_manual_drain_jog() call from slipping in between it and
        # tank_cleaning.start() actually flipping is_active(). This second
        # check-and-start is the one that actually has to be race-free.
        with self._lock:
            busy_message = self._busy_reason("tank_cleaning")
            if busy_message is not None:
                return self._result(False, busy_message)

            result = self.tank_cleaning.start(settings)

            self.last_message = result.get("message", "Tank Cleaning start requested.")
            if not result.get("success"):
                self.last_error = result.get("message")

        return result

    def stop_tank_cleaning(self) -> dict[str, Any]:
        result = self.tank_cleaning.stop(reason="stopped_by_user")

        with self._lock:
            self.last_message = result.get("message", "Tank Cleaning stop requested.")

        return result

    def start_prime(self, pumps: dict[str, list[str]]) -> dict[str, Any]:
        """
        pumps: {"MCU_A": ["P1"], "MCU_B": ["P1", "P2", ...]} - Auswahl aus
        dem Prime-Dialog. Menge/Chunkgröße kommen NICHT vom Aufrufer, sondern
        sind feste Konstanten (process/pump_prime.py::PRIME_MAX_ML_PER_PUMP/
        PRIME_CHUNK_ML) - die UI darf die 150 ml je Pumpe nicht verändern.
        """
        with self._lock:
            busy_message = self._busy_reason("prime")
            if busy_message is not None:
                return self._result(False, busy_message)

        # Re-check and start atomically under the same lock: the precondition
        # check above only fails fast, it does not prevent a concurrent
        # start_manual_drain_jog()/start_tank_cleaning() call from slipping in
        # between it and prime.start() actually flipping is_active(). This
        # second check-and-start is the one that actually has to be race-free.
        with self._lock:
            busy_message = self._busy_reason("prime")
            if busy_message is not None:
                return self._result(False, busy_message)

            settings = {
                "pumps": pumps,
                "max_ml_per_pump": PRIME_MAX_ML_PER_PUMP,
                "chunk_ml": PRIME_CHUNK_ML,
            }
            result = self.prime.start(settings)

            self.last_message = result.get("message", "Prime start requested.")
            if not result.get("success"):
                self.last_error = result.get("message")

        return result

    async def stop_prime(self) -> dict[str, Any]:
        """
        Async wie emergency_stop(): der Join kann wegen
        PumpPrimeController.stop_join_timeout_seconds (40s) deutlich länger
        dauern als bei Manual Drain Jog/Tank Cleaning - ein synchroner Aufruf
        würde die NiceGUI-Event-Loop für alle verbundenen Clients blockieren.
        """
        self.prime.request_stop(reason="stopped_by_user")
        result = await asyncio.to_thread(self.prime.wait_stopped)

        with self._lock:
            self.last_message = result.get("message", "Prime stop requested.")

        return result

    def acknowledge_error(self) -> dict[str, Any]:
        with self._lock:
            if (
                self.fill_and_measure.is_active()
                or self.fill_and_measure._teardown_in_progress
                or self.tank_cleaning.is_active()
                or self.prime.is_active()
            ):
                return self._result(
                    False,
                    "Reset blockiert: Prozess, Hintergrundthread, Cleanup, Tank Cleaning oder Prime läuft noch."
                )

            result = self.fill_and_measure.acknowledge_error()
            self.last_error = None
            self.last_message = "Reset acknowledged. Controller ready."

        return result

    def _set_error(self, message: str) -> None:
        with self._lock:
            self.last_error = message
            self.last_message = message

    def _set_fill_and_measure_error(self, message: str) -> None:
        """
        Facade-level Fill-and-Measure-Fehler (Settings/Rezept/Bestätigung/
        Sensor-Snapshot, alle VOR dem eigentlichen Start) landen direkt auf
        dem FillAndMeasureController, nicht auf dem geteilten self.last_error
        dieser Fassade - get_status() liest error/last_message für den
        obersten Statusbereich ausschließlich von self.fill_and_measure,
        siehe dortiges get_status(). Kleine, bewusste Vereinfachung
        gegenüber dem Vorzustand (in dem ALLE vier Prozesse denselben
        self.last_error-Slot der Fassade teilten, wodurch z.B. ein
        Tank-Cleaning-Settings-Fehler theoretisch im Fill-and-Measure-
        Statusbereich aufgetaucht wäre) - ungetestet, siehe Modularisierungs-
        Plan Phase 7b.
        """
        with self.fill_and_measure._lock:
            self.fill_and_measure._last_error = message
            self.fill_and_measure._last_message = message

    @staticmethod
    def _result(success: bool, message: str) -> dict[str, Any]:
        return {
            "success": success,
            "message": message,
        }
