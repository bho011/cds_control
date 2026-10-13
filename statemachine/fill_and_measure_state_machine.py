import time
from collections import deque
from enum import Enum, auto
from typing import Any, Callable

from process.watchdog import FillWatchdog
from services.sensor_stamp import Stamp, is_newer, read_stamp

from .sensor_reading import filtered_mixer_liters, mixer_liters_from_snapshot, ro_liters_from_snapshot
from .start_validation import validate_fill_runtime, validate_fill_start_with_reason

# Maximum time without a NEW measurement (different MQTT message with a
# strictly later bridge timestamp) before a recipe run is aborted: before the
# pump may start, while filling, and between target confirmations. Basis: the
# same 5 s after which the existing sensor reader already treats a snapshot as
# unusable (nicegui_dashboard/cds_controller.py, max_age_seconds=5.0). Not
# hardware-validated.
MEASUREMENT_WAIT_TIMEOUT_S = 5.0

# validate_fill_runtime reason code -> stop_reason during the run
_RUNTIME_STOP_REASONS = {
    "mixer_invalid": "mixer_implausible_during_fill",
    "mixer_implausible": "mixer_implausible_during_fill",
    "ro_invalid": "ro_invalid_during_fill",
    "ro_capacity_invalid": "ro_invalid_during_fill",
    "ro_insufficient": "ro_reserve_violated_during_fill",
}


class FillAndMeasureState(Enum):
    IDLE = auto()
    START_REFILL_PUMP = auto()
    FILL_UNTIL_TARGET = auto()
    CONFIRM_TARGET = auto()
    STOP_REFILL_PUMP = auto()
    SETTLE_LEVEL = auto()
    START_CIRCULATION = auto()
    SENSOR_STABILIZE = auto()
    MEASURE_VALUES = auto()
    FINISHED = auto()
    ERROR = auto()


class FillAndMeasureStateMachine:
    """
    Fill-and-measure process for the current CDS hardware state.

    Current hardware assumption:
    RO Tank -> Mixer Refill Pump -> Mixing Tank

    No RO inlet valve is used during filling anymore.
    ro_inlet_valve is kept as optional compatibility parameter only.
    """

    def __init__(
        self,
        mixer_refill_pump,
        ro_inlet_valve=None,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None] | None = None,
        settings: dict[str, Any] | None = None,
        mixing_circulation_pump=None,
        sensor_circulation_pump=None,
        recipe_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.state = FillAndMeasureState.IDLE
        self._clock = clock

        self.mixer_refill_pump = mixer_refill_pump
        self.ro_inlet_valve = ro_inlet_valve
        self.mixing_circulation_pump = mixing_circulation_pump
        self.sensor_circulation_pump = sensor_circulation_pump

        self.get_sensor_snapshot = get_sensor_snapshot
        self.settings = settings or {}

        # Strict recipe-run mode (AP-02C): complete AP-02B release check before
        # the pump starts, measurement freshness, runtime RO/mixer checks and
        # the pump-off-first target confirmation. Active when the controller
        # says so OR whenever a recipe_run_config_snapshot is present (even an
        # invalid one - fail closed). Without either (legacy/delta use) the
        # previous behavior is unchanged.
        self.recipe_run = bool(recipe_run) or "recipe_run_config_snapshot" in (settings or {})
        self._last_accepted_stamp: Stamp | None = None
        self._last_accepted_at: float | None = None
        self._filter_last_seq: int | None = None
        self.last_filtered_mixer_liters: float | None = None

        self.state_started_at = self._clock()
        self.process_started_at = self._clock()
        self.fill_started_at: float | None = None

        self.start_mixer_liters: float | None = None
        self.final_snapshot: dict[str, Any] | None = None
        self.error_message: str | None = None

        filter_samples = int(self.settings.get("level_filter_samples", 5))
        self.mixer_liter_history = deque(maxlen=max(1, filter_samples))

        self.target_confirm_count = 0
        self.last_added_liters: float | None = None
        self.stop_reason: str | None = None

    def start(self):
        if self.state != FillAndMeasureState.IDLE:
            print("[WARN] FillAndMeasure process already started.")
            return

        if self.get_sensor_snapshot is None:
            self.error("No sensor snapshot function configured.")
            return

        snapshot = self.get_sensor_snapshot()
        if self._error_latched():
            return
        if snapshot is None:
            self.error("No valid sensor snapshot available at process start.")
            return

        if self.recipe_run:
            stamp = read_stamp(snapshot)
            if stamp is None:
                self.stop_reason = "measurement_stamp_invalid"
                self.error("Start blockiert: Messkennzeichnung (MQTT-Zaehler/Zeitstempel) fehlt oder ist ungueltig.")
                return
            self._last_accepted_stamp = stamp
            self._last_accepted_at = self._clock()

        mixer_liters = self._filtered_mixer_liters(snapshot)
        ro_liters = self._ro_liters(snapshot)

        if mixer_liters is None:
            self.error("Mixer liters not available.")
            return

        if ro_liters is None:
            self.error("RO liters not available.")
            return

        min_ro = float(self.settings["min_ro_liters_required"])
        if ro_liters < min_ro:
            self.error(
                f"Not enough RO water. Required={min_ro:.2f} L, "
                f"available={ro_liters:.2f} L."
            )
            return

        self.start_mixer_liters = mixer_liters
        self.process_started_at = self._clock()
        self.target_confirm_count = 0
        self.last_added_liters = 0.0
        self.stop_reason = None

        print(f"[START] Mixer start level filtered: {mixer_liters:.2f} L")
        print(f"[START] RO available: {ro_liters:.2f} L")
        print("[INFO] Direct RO refill path: no RO inlet valve will be opened.")
        print(
            f"[START] Filter samples: "
            f"{self.settings.get('level_filter_samples', 5)}"
        )

        self._change_state(FillAndMeasureState.START_REFILL_PUMP)

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    @stop_reason.setter
    def stop_reason(self, value: str | None) -> None:
        # Once an error is latched the cause is final: a handler that was
        # still running concurrently must not rewrite the stop reason.
        if getattr(self, "error_message", None) is not None:
            return
        self._stop_reason = value

    def update(self):
        if self.state == FillAndMeasureState.IDLE:
            return

        # An error is final: whatever state a concurrent emergency stop left
        # behind, never run another handler (which could switch something on).
        if self.error_message is not None:
            self.safe_shutdown()
            return

        if self.state == FillAndMeasureState.START_REFILL_PUMP:
            self._handle_start_refill_pump()

        elif self.state == FillAndMeasureState.FILL_UNTIL_TARGET:
            self._handle_fill_until_target()

        elif self.state == FillAndMeasureState.CONFIRM_TARGET:
            self._handle_confirm_target()

        elif self.state == FillAndMeasureState.STOP_REFILL_PUMP:
            self._handle_stop_refill_pump()

        elif self.state == FillAndMeasureState.SETTLE_LEVEL:
            self._handle_settle_level()

        elif self.state == FillAndMeasureState.START_CIRCULATION:
            self._handle_start_circulation()

        elif self.state == FillAndMeasureState.SENSOR_STABILIZE:
            self._handle_sensor_stabilize()

        elif self.state == FillAndMeasureState.MEASURE_VALUES:
            self._handle_measure_values()

        elif self.state == FillAndMeasureState.FINISHED:
            return

        elif self.state == FillAndMeasureState.ERROR:
            self.safe_shutdown()

    def _handle_start_refill_pump(self):
        if self.recipe_run and not self._ready_for_pump_start():
            return

        # Check, switch on, check again: error() sets error_message BEFORE it
        # switches anything off, so a concurrent emergency stop either is seen
        # here or switches the pump off after our on().
        if self.error_message is not None:
            self.safe_shutdown()
            return

        self.mixer_refill_pump.on()

        if self.error_message is not None:
            self.safe_shutdown()
            return

        self.fill_started_at = self._clock()
        self.target_confirm_count = 0
        self._change_state(FillAndMeasureState.FILL_UNTIL_TARGET)

    def _ready_for_pump_start(self) -> bool:
        """Recipe runs only: True once a NEW measurement passed the complete
        AP-02B release check. While waiting the state stays START_REFILL_PUMP
        (pump off); errors are final."""
        snapshot = self._read_snapshot("pump start")
        if snapshot is None:
            return False

        fresh = self._accept_measurement(
            snapshot,
            "no_new_measurement_before_pump_start",
            "Pumpenstart abgebrochen: innerhalb von "
            f"{MEASUREMENT_WAIT_TIMEOUT_S:.0f} s keine neue Messung empfangen.",
        )
        if not fresh:  # None (error) or False (same message: keep waiting, pump off)
            return False

        ok, message, _reason = validate_fill_start_with_reason(self.settings, snapshot)
        if not ok:
            self.stop_reason = "pump_start_blocked"
            self.error(message or "Pumpenstart blockiert.")
            return False

        return True

    def _read_snapshot(self, context: str) -> dict[str, Any] | None:
        """Sensor snapshot or None after a controlled error()."""
        if self.get_sensor_snapshot is None:
            self.error(f"No sensor snapshot function configured ({context}).")
            return None
        try:
            snapshot = self.get_sensor_snapshot()
        except Exception as exc:
            self.stop_reason = "sensor_read_failed"
            self.error(f"Sensor snapshot could not be read ({context}): {type(exc).__name__}: {exc}")
            return None
        if self._error_latched():
            # error()/emergency stop happened while the reader ran: no further
            # processing of this snapshot (it must not alter reason/state).
            return None
        if snapshot is None:
            self.error(f"No recent sensor snapshot ({context}).")
            return None
        return snapshot

    def _accept_measurement(self, snapshot: dict[str, Any], timeout_reason: str, timeout_message: str) -> bool | None:
        """Recipe runs: True if this snapshot is a NEW measurement (different
        MQTT message AND strictly later bridge timestamp, then remembered),
        False if it is not new, None after error() for a missing/invalid stamp
        or an expired deadline. The deadline (monotonic clock, time since the
        last accepted measurement) is checked FIRST and after the sensor call
        returned, so a late measurement never heals an already expired
        deadline. Reading the same snapshot again never counts as new."""
        if self._measurement_timed_out():
            self.stop_reason = timeout_reason
            self.error(timeout_message)
            return None
        stamp = read_stamp(snapshot)
        if stamp is None:
            self.stop_reason = "measurement_stamp_invalid"
            self.error("Messkennzeichnung (MQTT-Zaehler/Zeitstempel) fehlt oder ist ungueltig.")
            return None
        if is_newer(stamp, self._last_accepted_stamp):
            self._last_accepted_stamp = stamp
            self._last_accepted_at = self._clock()
            return True
        return False

    def _measurement_timed_out(self) -> bool:
        if self._last_accepted_at is None:
            return True
        return self._clock() - self._last_accepted_at >= MEASUREMENT_WAIT_TIMEOUT_S

    def _abort_if_runtime_invalid(self, snapshot: dict[str, Any]) -> bool:
        """Recipe runs: unfiltered safety checks every tick. True after error()."""
        ok, message, reason = validate_fill_runtime(
            self.settings, snapshot, self.last_filtered_mixer_liters
        )
        if ok:
            return False
        self.stop_reason = _RUNTIME_STOP_REASONS.get(reason or "", "run_config_invalid_during_fill")
        self.error(message or "Laufpruefung fehlgeschlagen.")
        return True

    def _handle_fill_until_target_recipe(self):
        """Recipe run (strict mode): unfiltered safety limits and target
        detection, filtered value only for the watchdog/diagnostics. The first
        valid unfiltered reading at/above the absolute target switches the pump
        off in this very update; confirmations follow with the pump off."""
        snapshot = self._read_snapshot("fill")
        if snapshot is None:
            return

        fresh = self._accept_measurement(
            snapshot,
            "measurement_stale_during_fill",
            f"Befuellung abgebrochen: seit {MEASUREMENT_WAIT_TIMEOUT_S:.0f} s keine neue Messung empfangen.",
        )
        if fresh is None:
            return

        mixer_filtered = self._filtered_mixer_liters(snapshot)

        if self._abort_if_runtime_invalid(snapshot):
            return

        mixer_unfiltered = self._mixer_liters_from_snapshot(snapshot)
        if mixer_unfiltered is None or self.start_mixer_liters is None or self.fill_started_at is None:
            self.stop_reason = "mixer_implausible_during_fill"
            self.error("Mixerwert oder Startzustand fehlt waehrend der Befuellung.")
            return

        target_total = float(self.settings["target_total_liters"])
        self.last_added_liters = mixer_unfiltered - self.start_mixer_liters

        if mixer_unfiltered >= target_total:
            # Pump off FIRST, in this update - never wait for the filter or a
            # later tick. Never switched on again by this machine.
            self._pump_off_now()
            self.stop_reason = "target_first_reached_pump_off"
            self.target_confirm_count = 1 if fresh else 0
            print(
                f"[TARGET] Unfiltered {mixer_unfiltered:.2f} L >= {target_total:.2f} L: "
                "pump OFF, confirming with pump off."
            )
            self._change_state(FillAndMeasureState.CONFIRM_TARGET)
            return

        watchdog = FillWatchdog(
            start_liters=self.start_mixer_liters,
            max_liters=float(self.settings["max_mixer_liters"]),
            max_seconds=float(self.settings["max_fill_seconds"]),
            no_progress_timeout_seconds=float(self.settings["no_fill_progress_timeout_seconds"]),
            min_progress_liters=float(self.settings["min_fill_progress_liters"]),
            max_negative_drift_liters=float(self.settings["max_negative_level_drift_liters"]),
        )
        watchdog_result = watchdog.check(
            mixer_filtered,
            self._clock() - self.fill_started_at,
            max_elapsed_seconds=self._process_elapsed_seconds(),
        )
        if watchdog_result is not None:
            self.stop_reason, message = watchdog_result
            self.error(message)
            return

        print(
            f"[FILL] Mixer(unfiltered)={mixer_unfiltered:.2f}/{target_total:.2f} L | "
            f"filtered={mixer_filtered} | RO={self._ro_liters(snapshot)} L"
        )

    def _handle_confirm_target(self):
        """Pump is already off. Only different, plausible measurements count;
        a contradicting value or a missing measurement ends in ERROR - the
        pump is never switched on again to make up a presumed shortfall.
        Progress checks of the active fill deliberately do not apply here."""
        snapshot = self._read_snapshot("target confirmation")
        if snapshot is None:
            return

        fresh = self._accept_measurement(
            snapshot,
            "target_confirm_timeout",
            f"Zielbestaetigung abgebrochen: seit {MEASUREMENT_WAIT_TIMEOUT_S:.0f} s keine neue Messung.",
        )
        if fresh is None:
            return

        self._filtered_mixer_liters(snapshot)

        if self._abort_if_runtime_invalid(snapshot):
            return

        target_total = float(self.settings["target_total_liters"])

        if fresh:
            mixer_unfiltered = self._mixer_liters_from_snapshot(snapshot)
            if mixer_unfiltered is not None and mixer_unfiltered >= target_total:
                self.target_confirm_count += 1
            else:
                self.stop_reason = "target_not_confirmed"
                self.error(
                    f"Zielstand nicht bestaetigt: Messwert {mixer_unfiltered} L liegt unter dem Ziel "
                    f"{target_total:.2f} L. Es wird nicht nachgefuellt."
                )
                return

        required = int(self.settings.get("target_reached_confirm_samples", 3))
        if self.target_confirm_count >= required:
            self.stop_reason = "target_reached_by_sensor"
            print(f"[TARGET] Target confirmed with {self.target_confirm_count}/{required} measurements.")
            self._change_state(FillAndMeasureState.STOP_REFILL_PUMP)

    def _error_latched(self) -> bool:
        """True once error() ran (also from another thread). Checked before
        and after every external call that may race with an emergency stop."""
        return self.error_message is not None

    def _pump_off_now(self):
        self.mixer_refill_pump.off()
        if self.ro_inlet_valve is not None:
            self.ro_inlet_valve.off()

    def _handle_fill_until_target(self):
        if self.recipe_run:
            self._handle_fill_until_target_recipe()
            return

        if self.get_sensor_snapshot is None:
            self.error("No sensor snapshot function configured during filling.")
            return

        snapshot = self.get_sensor_snapshot()

        if self._error_latched():
            return

        if snapshot is None:
            self.error("No recent sensor snapshot during filling.")
            return

        mixer_liters = self._filtered_mixer_liters(snapshot)
        ro_liters = self._ro_liters(snapshot)

        if mixer_liters is None:
            # Same reason code refill.py/TankCleaningController use for the
            # equivalent case, for consistency (this field was previously
            # left unset here - harmless since nothing reads it externally,
            # but worth fixing while touching this code).
            self.stop_reason = "missing_mixer_level_during_fill"
            self.error("Mixer liters lost during filling.")
            return

        if ro_liters is None:
            self.error("RO liters lost during filling.")
            return

        if self.start_mixer_liters is None:
            self.error("Missing start_mixer_liters.")
            return

        if self.fill_started_at is None:
            self.error("Missing fill_started_at.")
            return

        fill_elapsed = self._clock() - self.fill_started_at
        process_elapsed = self._process_elapsed_seconds()

        # No .get(key, default) fallbacks here: process_controller.py's
        # load_settings() now validates all of these keys are present
        # (services/settings_validation.py, PROCESS_SETTINGS_SCHEMA) before
        # this state machine ever runs, so a direct subscript is both
        # correct and safer than a hardcoded fallback - this used to carry
        # its own defaults (15.0/1.5/2.0) that silently diverged from what
        # config/process_settings.json actually specified (8.0/0.2/2.0).
        watchdog = FillWatchdog(
            start_liters=self.start_mixer_liters,
            max_liters=float(self.settings["max_mixer_liters"]),
            max_seconds=float(self.settings["max_fill_seconds"]),
            no_progress_timeout_seconds=float(self.settings["no_fill_progress_timeout_seconds"]),
            min_progress_liters=float(self.settings["min_fill_progress_liters"]),
            max_negative_drift_liters=float(self.settings["max_negative_level_drift_liters"]),
        )
        # max_fill_seconds is measured against the whole process's elapsed
        # time here, not just fill_elapsed since this phase started - the
        # one real difference found between the three call sites FillWatchdog
        # was extracted from, hence the separate max_elapsed_seconds arg.
        watchdog_result = watchdog.check(
            mixer_liters, fill_elapsed, max_elapsed_seconds=process_elapsed
        )
        if watchdog_result is not None:
            self.stop_reason, message = watchdog_result
            self.error(message)
            return

        added_liters = mixer_liters - self.start_mixer_liters
        self.last_added_liters = added_liters

        fill_mode = self.settings.get("fill_mode", "delta")

        if fill_mode == "delta":
            target_add = float(self.settings["target_add_liters"])
            target_reached = added_liters >= target_add

            print(
                f"[FILL] Mixer(filtered)={mixer_liters:.2f} L | "
                f"Added={added_liters:.2f}/{target_add:.2f} L | "
                f"RO={ro_liters:.2f} L | "
                f"fill_elapsed={fill_elapsed:.1f}s | "
                f"confirm={self.target_confirm_count}"
            )

        elif fill_mode == "absolute":
            target_total = float(self.settings["target_total_liters"])
            target_reached = mixer_liters >= target_total

            print(
                f"[FILL] Mixer(filtered)={mixer_liters:.2f}/{target_total:.2f} L | "
                f"Added={added_liters:.2f} L | "
                f"RO={ro_liters:.2f} L | "
                f"fill_elapsed={fill_elapsed:.1f}s | "
                f"confirm={self.target_confirm_count}"
            )

        else:
            self.stop_reason = "unknown_fill_mode"
            self.error(f"Unknown fill_mode: {fill_mode}")
            return

        if target_reached:
            self.target_confirm_count += 1
        else:
            self.target_confirm_count = 0

        required_confirm_samples = int(
            self.settings.get("target_reached_confirm_samples", 3)
        )

        if self.target_confirm_count >= required_confirm_samples:
            self.stop_reason = "target_reached_by_sensor"
            print(
                f"[TARGET] Target confirmed with "
                f"{self.target_confirm_count}/{required_confirm_samples} samples."
            )
            self._change_state(FillAndMeasureState.STOP_REFILL_PUMP)

    def _handle_stop_refill_pump(self):
        self.mixer_refill_pump.off()

        # Compatibility/safety only:
        # Old controller code may still pass a valve object.
        # It is not opened by this state machine, but we ensure it is off.
        if self.ro_inlet_valve is not None:
            self.ro_inlet_valve.off()

        self._change_state(FillAndMeasureState.SETTLE_LEVEL)

    def _handle_settle_level(self):
        if self._state_elapsed_seconds() >= float(self.settings["level_settle_seconds"]):
            self._change_state(FillAndMeasureState.START_CIRCULATION)

    def _handle_start_circulation(self):
        if self._error_latched():
            self.safe_shutdown()
            return

        if self.settings.get("enable_mixing_circulation", False):
            if self.mixing_circulation_pump is None:
                self.error("Mixing circulation enabled but actuator is missing.")
                return
            if self._error_latched():  # immediately before on()
                self.safe_shutdown()
                return
            self.mixing_circulation_pump.on()
            if self._error_latched():  # emergency stop during on(): nothing else on
                self.safe_shutdown()
                return

        if self.settings.get("enable_sensor_circulation", False):
            if self.sensor_circulation_pump is None:
                self.error("Sensor circulation enabled but actuator is missing.")
                return
            if self._error_latched():  # immediately before on()
                self.safe_shutdown()
                return
            self.sensor_circulation_pump.on()
            if self._error_latched():
                self.safe_shutdown()
                return

        self._change_state(FillAndMeasureState.SENSOR_STABILIZE)

    def _handle_sensor_stabilize(self):
        if self._state_elapsed_seconds() >= float(self.settings["sensor_stabilize_seconds"]):
            self._change_state(FillAndMeasureState.MEASURE_VALUES)

    def _handle_measure_values(self):
        if self.get_sensor_snapshot is None:
            self.error("No sensor snapshot function configured for final measurement.")
            return

        snapshot = self.get_sensor_snapshot()

        if self._error_latched():
            return

        if snapshot is None:
            self.error("No recent sensor snapshot for final measurement.")
            return

        self.final_snapshot = snapshot

        water_values = snapshot.get("water_values", {})
        mixer = snapshot.get("mixer", {})
        ro = snapshot.get("ro", {})

        mixer_liters_filtered = self._filtered_mixer_liters(snapshot)

        print("[MEASURE] Final values:")
        print(
            f"          Mixer filtered: "
            f"{mixer_liters_filtered:.2f} L"
            if mixer_liters_filtered is not None
            else "          Mixer filtered: None"
        )
        print(
            f"          Mixer payload:  "
            f"{mixer.get('volume_liters_calc')} L / {mixer.get('level_percent')} %"
        )
        print(
            f"          RO:             "
            f"{ro.get('volume_liters_calc')} L / {ro.get('level_percent')} %"
        )
        print(f"          EC:             {water_values.get('ec_ms_cm')} mS/cm")
        print(f"          pH:             {water_values.get('ph')}")
        print(f"          Temp:           {water_values.get('water_temperature')} °C")
        print(f"          DO:             {water_values.get('dissolved_oxygen')}")

        self._change_state(FillAndMeasureState.FINISHED)

    def error(self, message: str):
        # The first error is final: its cause is kept, later calls (e.g. a
        # concurrent emergency stop or the controller's stop handling) still
        # shut everything off but never replace the original message.
        if self.error_message is None:
            self.error_message = message
        print(f"[ERROR] {message}")
        self._change_state(FillAndMeasureState.ERROR)
        self.safe_shutdown()

    def safe_shutdown(self):
        print("[SAFE] FillAndMeasure safe shutdown.")

        self.mixer_refill_pump.off()

        if self.ro_inlet_valve is not None:
            self.ro_inlet_valve.off()

        if self.mixing_circulation_pump is not None:
            self.mixing_circulation_pump.off()

        if self.sensor_circulation_pump is not None:
            self.sensor_circulation_pump.off()

    def _change_state(self, new_state: FillAndMeasureState):
        # ERROR is final: a handler that was running concurrently with
        # error()/emergency stop must not move the machine out of it.
        if new_state != FillAndMeasureState.ERROR and (
            self.state == FillAndMeasureState.ERROR or self.error_message is not None
        ):
            print(f"[STATE] {self.state.name} -> {new_state.name} refused (error is final)")
            return
        print(f"[STATE] {self.state.name} -> {new_state.name}")
        self.state = new_state
        self.state_started_at = self._clock()

    def _state_elapsed_seconds(self) -> float:
        return self._clock() - self.state_started_at

    def _process_elapsed_seconds(self) -> float:
        return self._clock() - self.process_started_at

    def _filtered_mixer_liters(self, snapshot: dict[str, Any]) -> float | None:
        """Thin wrapper around statemachine/sensor_reading.py::filtered_mixer_liters -
        kept as a method because nicegui_dashboard/process_controller.py calls
        this on a live state_machine instance (self.state_machine._filtered_mixer_liters(...))."""
        if self.recipe_run:
            # At most one history entry per received message: repeated reads
            # of the same snapshot (update tick, logging, ...) return the
            # cached value and neither move the average nor count anything.
            stamp = read_stamp(snapshot)
            if stamp is None:
                return None
            if stamp[0] == self._filter_last_seq:
                return self.last_filtered_mixer_liters
            self._filter_last_seq = stamp[0]

        value = filtered_mixer_liters(self.mixer_liter_history, snapshot, self.settings)
        self.last_filtered_mixer_liters = value
        return value

    def _mixer_liters_from_snapshot(self, snapshot: dict[str, Any]) -> float | None:
        return mixer_liters_from_snapshot(snapshot, self.settings)

    @staticmethod
    def _ro_liters(snapshot: dict[str, Any]) -> float | None:
        return ro_liters_from_snapshot(snapshot)

        return None

    @property
    def is_done(self) -> bool:
        return self.state in [
            FillAndMeasureState.FINISHED,
            FillAndMeasureState.ERROR,
        ]
