import time
from collections import deque
from enum import Enum, auto
from typing import Any, Callable


class FillAndMeasureState(Enum):
    IDLE = auto()
    OPEN_RO_INLET = auto()
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
    def __init__(
        self,
        mixer_refill_pump,
        ro_inlet_valve,
        get_sensor_snapshot: Callable[[], dict[str, Any] | None],
        settings: dict[str, Any],
        mixing_circulation_pump=None,
        sensor_circulation_pump=None,
    ):
        self.state = FillAndMeasureState.IDLE

        self.mixer_refill_pump = mixer_refill_pump
        self.ro_inlet_valve = ro_inlet_valve
        self.mixing_circulation_pump = mixing_circulation_pump
        self.sensor_circulation_pump = sensor_circulation_pump

        self.get_sensor_snapshot = get_sensor_snapshot
        self.settings = settings

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

    def start(self):
        if self.state != FillAndMeasureState.IDLE:
            print("[WARN] FillAndMeasure process already started.")
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

        print(f"[START] Mixer start level filtered: {mixer_liters:.2f} L")
        print(f"[START] RO available: {ro_liters:.2f} L")
        print(
            f"[START] Filter samples: "
            f"{self.settings.get('level_filter_samples', 5)}"
        )

        self._change_state(FillAndMeasureState.OPEN_RO_INLET)

    def update(self):
        if self.state == FillAndMeasureState.IDLE:
            return

        if self.state == FillAndMeasureState.OPEN_RO_INLET:
            self._handle_open_ro_inlet()

        elif self.state == FillAndMeasureState.START_REFILL_PUMP:
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

    def _handle_open_ro_inlet(self):
        self.ro_inlet_valve.on()

        if self._state_elapsed_seconds() >= float(self.settings["valve_settle_seconds"]):
            self._change_state(FillAndMeasureState.START_REFILL_PUMP)

    def _handle_start_refill_pump(self):
        self.mixer_refill_pump.on()
        self.fill_started_at = time.monotonic()
        self.target_confirm_count = 0
        self._change_state(FillAndMeasureState.FILL_UNTIL_TARGET)

    def _handle_fill_until_target(self):
        snapshot = self.get_sensor_snapshot()

        if snapshot is None:
            self.error("No recent sensor snapshot during filling.")
            return

        mixer_liters = self._filtered_mixer_liters(snapshot)
        ro_liters = self._ro_liters(snapshot)

        if mixer_liters is None:
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

        max_fill_seconds = float(self.settings["max_fill_seconds"])
        if process_elapsed > max_fill_seconds:
            self.error(f"Max fill time exceeded: {max_fill_seconds:.1f} seconds.")
            return

        max_mixer_liters = float(self.settings["max_mixer_liters"])
        if mixer_liters > max_mixer_liters:
            self.error(
                f"Mixer over max limit: "
                f"{mixer_liters:.2f} L > {max_mixer_liters:.2f} L."
            )
            return

        added_liters = mixer_liters - self.start_mixer_liters
        self.last_added_liters = added_liters

        max_negative_drift = float(
            self.settings.get("max_negative_level_drift_liters", 2.0)
        )

        if added_liters < -max_negative_drift:
            self.error(
                f"Mixer level drift too negative: "
                f"added={added_liters:.2f} L, "
                f"allowed=-{max_negative_drift:.2f} L."
            )
            return

        no_progress_timeout = float(
            self.settings.get("no_fill_progress_timeout_seconds", 15.0)
        )
        min_progress = float(
            self.settings.get("min_fill_progress_liters", 1.5)
        )

        if fill_elapsed >= no_progress_timeout and added_liters < min_progress:
            self.error(
                f"No plausible fill progress. "
                f"Added={added_liters:.2f} L after {fill_elapsed:.1f}s. "
                f"Required at least {min_progress:.2f} L."
            )
            return

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
            print(
                f"[TARGET] Target confirmed with "
                f"{self.target_confirm_count}/{required_confirm_samples} samples."
            )
            self._change_state(FillAndMeasureState.STOP_REFILL_PUMP)

    def _handle_stop_refill_pump(self):
        self.mixer_refill_pump.off()
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
        print(f"          RO:             {ro.get('volume_liters_calc')} L / {ro.get('level_percent')} %")
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
        mixer_liters = self._mixer_liters_from_percent(snapshot)

        if mixer_liters is None:
            return None

        self.mixer_liter_history.append(mixer_liters)

        return sum(self.mixer_liter_history) / len(self.mixer_liter_history)

    def _mixer_liters_from_percent(self, snapshot: dict[str, Any]) -> float | None:
        mixer = snapshot.get("mixer") or {}
        level_percent = mixer.get("level_percent")

        if level_percent is not None:
            max_liters = float(self.settings["max_mixer_liters"])
            return (float(level_percent) / 100.0) * max_liters

        fallback_value = mixer.get("volume_liters_calc")
        return float(fallback_value) if fallback_value is not None else None

    @staticmethod
    def _ro_liters(snapshot: dict[str, Any]) -> float | None:
        ro = snapshot.get("ro") or {}

        level_percent = ro.get("level_percent")
        configured_max_liters = ro.get("configured_max_liters")

        if level_percent is not None and configured_max_liters is not None:
            return (float(level_percent) / 100.0) * float(configured_max_liters)

        fallback_value = ro.get("volume_liters_calc")
        return float(fallback_value) if fallback_value is not None else None

    @property
    def is_done(self) -> bool:
        return self.state in [
            FillAndMeasureState.FINISHED,
            FillAndMeasureState.ERROR,
        ]