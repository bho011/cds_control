import time
from collections import deque
from enum import Enum, auto
from typing import Any, Callable

from process.watchdog import FillWatchdog

from .sensor_reading import filtered_mixer_liters, mixer_liters_from_snapshot, ro_liters_from_snapshot


class FillAndMeasureState(Enum):
    IDLE = auto()
    START_REFILL_PUMP = auto()
    FILL_UNTIL_TARGET = auto()
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
    ):
        self.state = FillAndMeasureState.IDLE

        self.mixer_refill_pump = mixer_refill_pump
        self.ro_inlet_valve = ro_inlet_valve
        self.mixing_circulation_pump = mixing_circulation_pump
        self.sensor_circulation_pump = sensor_circulation_pump

        self.get_sensor_snapshot = get_sensor_snapshot
        self.settings = settings or {}

        self.state_started_at = time.monotonic()
        self.process_started_at = time.monotonic()
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
        if snapshot is None:
            self.error("No valid sensor snapshot available at process start.")
            return

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
        self.process_started_at = time.monotonic()
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

    def update(self):
        if self.state == FillAndMeasureState.IDLE:
            return

        if self.state == FillAndMeasureState.START_REFILL_PUMP:
            self._handle_start_refill_pump()

        elif self.state == FillAndMeasureState.FILL_UNTIL_TARGET:
            self._handle_fill_until_target()

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
        self.mixer_refill_pump.on()
        self.fill_started_at = time.monotonic()
        self.target_confirm_count = 0
        self._change_state(FillAndMeasureState.FILL_UNTIL_TARGET)

    def _handle_fill_until_target(self):
        if self.get_sensor_snapshot is None:
            self.error("No sensor snapshot function configured during filling.")
            return

        snapshot = self.get_sensor_snapshot()

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

        fill_elapsed = time.monotonic() - self.fill_started_at
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
        if self.settings.get("enable_mixing_circulation", False):
            if self.mixing_circulation_pump is None:
                self.error("Mixing circulation enabled but actuator is missing.")
                return
            self.mixing_circulation_pump.on()

        if self.settings.get("enable_sensor_circulation", False):
            if self.sensor_circulation_pump is None:
                self.error("Sensor circulation enabled but actuator is missing.")
                return
            self.sensor_circulation_pump.on()

        self._change_state(FillAndMeasureState.SENSOR_STABILIZE)

    def _handle_sensor_stabilize(self):
        if self._state_elapsed_seconds() >= float(self.settings["sensor_stabilize_seconds"]):
            self._change_state(FillAndMeasureState.MEASURE_VALUES)

    def _handle_measure_values(self):
        if self.get_sensor_snapshot is None:
            self.error("No sensor snapshot function configured for final measurement.")
            return

        snapshot = self.get_sensor_snapshot()

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
        print(f"[STATE] {self.state.name} -> {new_state.name}")
        self.state = new_state
        self.state_started_at = time.monotonic()

    def _state_elapsed_seconds(self) -> float:
        return time.monotonic() - self.state_started_at

    def _process_elapsed_seconds(self) -> float:
        return time.monotonic() - self.process_started_at

    def _filtered_mixer_liters(self, snapshot: dict[str, Any]) -> float | None:
        """Thin wrapper around statemachine/sensor_reading.py::filtered_mixer_liters -
        kept as a method because nicegui_dashboard/process_controller.py calls
        this on a live state_machine instance (self.state_machine._filtered_mixer_liters(...))."""
        return filtered_mixer_liters(self.mixer_liter_history, snapshot, self.settings)

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
