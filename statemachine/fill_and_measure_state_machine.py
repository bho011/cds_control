import time
from collections import deque
from enum import Enum, auto
from typing import Any, Callable

from domain.recipe.volume_balance import (
    UL_PER_L,
    VolumeModelError,
    VolumeReason,
    VolumeState,
    dynamic_phase_target_l,
    evaluate_final_volume,
    evaluate_ro_resume,
    evaluate_volume_state,
    prefill_phase_target_l,
    ul_to_ml,
)
from domain.recipe.limits import MIXER_SENSOR_RELIABLE_MIN_LITERS
from process.nutrient_dosing import DosingPhase
from process.watchdog import FillWatchdog
from services.sensor_stamp import Stamp, is_newer, read_stamp

from .measurement_evaluation import evaluate_final_values, overall_status
from .sensor_reading import filtered_mixer_liters, mixer_liters_from_snapshot, ro_liters_from_snapshot
from .start_validation import (
    nutrient_ro_resume_progress,
    validate_dosing_run_settings,
    validate_fill_runtime,
    validate_fill_start_with_reason,
)

# Maximum time without a NEW measurement (different MQTT message with a
# strictly later bridge timestamp) before a recipe run is aborted: before the
# pump may start, while filling, and between target confirmations. Basis: the
# same 5 s after which the existing sensor reader already treats a snapshot as
# unusable (nicegui_dashboard/cds_controller.py, max_age_seconds=5.0). Not
# hardware-validated.
MEASUREMENT_WAIT_TIMEOUT_S = 5.0

# Die RO-Pumpe wird in einem Lauf hoechstens zweimal eingeschaltet: Vorfuellung und eine Wiederaufnahme
# (parallel zur Dosierung oder, ohne Ueberlappung, nach deren Ende). Es gibt nie ein drittes Einschalten und
# keine stille Nachfuelllogik: Wird das Endvolumen nicht erreicht, endet der Lauf kontrolliert im Fehler.
MAX_RO_ON_COUNT = 2

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
    DOSING_START = auto()
    DOSING_RUNNING = auto()
    DOSING_AND_RO_FILL = auto()
    POST_MIX = auto()
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
        nutrient_plan=None,
        dosing=None,
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
        self.final_evaluation: dict[str, Any] | None = None
        self.final_evaluation_status: str | None = None
        self.error_message: str | None = None

        filter_samples = int(self.settings.get("level_filter_samples", 5))
        self.mixer_liter_history = deque(maxlen=max(1, filter_samples))

        self.target_confirm_count = 0
        self.last_added_liters: float | None = None
        self.stop_reason: str | None = None

        # AP-05 (nur Rezeptlauf): Mischablauf mit Naehrstoffdosierung.
        # RO-Vorfuellung -> Zirkulation -> Dosierung (MCU_B, eigener Controller, rundenweise
        # parallel; RO-Pumpe zunaechst aus). Sobald der bestaetigte Anteil die Schwelle
        # nutrient_ro_resume_progress erreicht, startet die RO-Pumpe PARALLEL zur Dosierung
        # erneut (DOSING_AND_RO_FILL, genau eine Wiederaufnahme) und schaltet bei RO-Soll +
        # bestaetigter + in Ausfuehrung befindlicher Menge sofort ab. Nachmischen erst, wenn die
        # Dosierung vollstaendig bestaetigt UND das endgueltige Ziel (RO-Soll + gesamte bestaetigte
        # Menge) bei ausgeschalteter RO-Pumpe bestaetigt ist. Schwelle 1.0 (oder eine Dosierung, die
        # vor der Schwelle endet) ergibt den bisherigen Ablauf: zweite RO-Phase erst nach der Dosierung.
        # ``dosing`` ist die Schnittstelle des NutrientDosingController (start/get_status/request_stop).
        # Ohne Plan mit Dosierarbeit bleibt der bisherige Ablauf unveraendert.
        self.nutrient_plan = nutrient_plan
        self.dosing = dosing
        self.dosing_enabled = bool(nutrient_plan is not None and getattr(nutrient_plan, "has_dosing_work", False))
        self._fill_phase = "prefill" if self.dosing_enabled else "single"  # single | prefill | final
        self._dosing_started = False  # Einmalstart: nie ein zweiter Dosierstart
        self._dosing_active = False  # gestartet und noch kein Endstatus gesehen
        self._phase_start_liters: float | None = None
        self._final_confirmed_ul: int | None = None
        self._last_unfiltered_mixer: float | None = None
        self._ro_on_count = 0  # bisherige Einschaltungen der RO-Pumpe (hoechstens MAX_RO_ON_COUNT)
        self._ro_resumed = False  # RO wurde PARALLEL zur Dosierung wieder eingeschaltet
        self._ro_overlap_done = False  # ... und hat ihr Abschaltziel erreicht (aus, nie wieder ein)
        self._resume_confirmed_ul = 0  # bestaetigte Menge bei der Wiederaufnahme (Basis der RO-Fortschrittspruefung)

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

        if self.dosing_enabled and not self._dosing_preconditions_ok(mixer_liters):
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

        elif self.state == FillAndMeasureState.DOSING_START:
            self._handle_dosing_start()

        elif self.state == FillAndMeasureState.DOSING_RUNNING:
            self._handle_dosing_running()

        elif self.state == FillAndMeasureState.DOSING_AND_RO_FILL:
            self._handle_dosing_and_ro_fill()

        elif self.state == FillAndMeasureState.POST_MIX:
            self._handle_post_mix()

        elif self.state == FillAndMeasureState.MEASURE_VALUES:
            self._handle_measure_values()

        elif self.state == FillAndMeasureState.FINISHED:
            return

        elif self.state == FillAndMeasureState.ERROR:
            self.safe_shutdown()

    def _handle_start_refill_pump(self):
        if self.recipe_run and not self._ready_for_pump_start():
            return

        if not self._switch_ro_on():
            return

        self.fill_started_at = self._clock()
        self.target_confirm_count = 0
        self._change_state(FillAndMeasureState.FILL_UNTIL_TARGET)

    def _switch_ro_on(self) -> bool:
        """Einzige Stelle, die die RO-Pumpe einschaltet (Vorfuellung, Wiederaufnahme parallel zur Dosierung
        und zweite RO-Phase nach der Dosierung). True = eingeschaltet, False = nicht eingeschaltet und alles
        aus (Fehler). Hoechstens MAX_RO_ON_COUNT Einschaltungen je Lauf."""
        if self._ro_on_count >= MAX_RO_ON_COUNT:
            self.stop_reason = "ro_restart_limit"
            self.error(
                f"RO-Pumpe darf in einem Lauf hoechstens {MAX_RO_ON_COUNT}-mal eingeschaltet werden "
                "(Vorfuellung + eine Wiederaufnahme); es wird nicht nachgefuellt."
            )
            return False

        # Check, switch on, check again: error() sets error_message BEFORE it
        # switches anything off, so a concurrent emergency stop either is seen
        # here or switches the pump off after our on().
        if self.error_message is not None:
            self.safe_shutdown()
            return False

        self._ro_on_count += 1  # vor on() gezaehlt: auch ein fehlgeschlagener Versuch zaehlt
        self.mixer_refill_pump.on()

        if self.error_message is not None:
            self.safe_shutdown()
            return False
        return True

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

        if self._fill_phase == "final":
            return self._ready_for_final_fill(snapshot)

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

        target_total = self._phase_target_l()
        if target_total is None:
            return
        self._last_unfiltered_mixer = mixer_unfiltered
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

        # Mit Dosierung gilt max_fill_seconds je RO-Phase (die Dosierung selbst hat
        # eigene Zeitgrenzen im NutrientDosingController), sonst fuer den ganzen Lauf.
        watchdog = FillWatchdog(
            start_liters=self._phase_start_liters if self._phase_start_liters is not None else self.start_mixer_liters,
            max_liters=float(self.settings["max_mixer_liters"]),
            max_seconds=float(self.settings["max_fill_seconds"]),
            no_progress_timeout_seconds=float(self.settings["no_fill_progress_timeout_seconds"]),
            min_progress_liters=float(self.settings["min_fill_progress_liters"]),
            max_negative_drift_liters=float(self.settings["max_negative_level_drift_liters"]),
        )
        fill_elapsed = self._clock() - self.fill_started_at
        watchdog_result = watchdog.check(
            mixer_filtered,
            fill_elapsed,
            max_elapsed_seconds=fill_elapsed if self.dosing_enabled else self._process_elapsed_seconds(),
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

        target_total = self._phase_target_l()
        if target_total is None:
            return

        if fresh:
            mixer_unfiltered = self._mixer_liters_from_snapshot(snapshot)
            self._last_unfiltered_mixer = mixer_unfiltered
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

        if self._fill_phase == "final":
            if not self._final_volume_ok():
                return
            self._change_state(FillAndMeasureState.POST_MIX)
            return

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
            if self.dosing_enabled and self._fill_phase == "prefill":
                # Pumpen-aus-Zustaende (Settle/Stabilize) pruefen keine Messung: das
                # Frischefenster beginnt hier neu, eine NEUE Messung ist Pflicht.
                self._last_accepted_at = self._clock()
                self._change_state(FillAndMeasureState.DOSING_START)
                return
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

        if self.recipe_run:
            ok, message, reason, details = evaluate_final_values(self.settings, snapshot)
            self.final_evaluation = details
            self.final_evaluation_status = overall_status(ok, details)
            print(f"[MEASURE] EC/pH evaluation: {details}")
            if not ok:
                # Keine Korrektur, kein Drain: Pumpen aus (error -> safe_shutdown),
                # Bedienerentscheidung erforderlich.
                self.stop_reason = reason
                self.error(message or "EC/pH-Bewertung fehlgeschlagen.")
                return

        self._change_state(FillAndMeasureState.FINISHED)

    # ---- AP-05 Schritt 2: Mischablauf mit Naehrstoffdosierung -------------------

    def _dosing_preconditions_ok(self, mixer_liters: float) -> bool:
        plan = self.nutrient_plan
        if not self.recipe_run:
            self.error("Naehrstoffdosierung nur im Rezeptlauf erlaubt.")
            return False
        if self.dosing is None:
            self.error("Dosierplan vorhanden, aber kein Dosier-Controller angebunden.")
            return False
        if not plan.verify_hash():
            self.error("Dosierplan-Hash stimmt nicht mit dem Planinhalt ueberein.")
            return False
        if abs(plan.volume.target_ro_water_l - float(self.settings["target_total_liters"])) > 1e-6:
            self.error("Dosierplan passt nicht zum RO-Fuellziel der Einstellungen.")
            return False
        if not (self.settings.get("enable_mixing_circulation") is True and self.settings.get("enable_sensor_circulation") is True):
            self.error("Mit Dosierung muessen Mixing- und Sensorzirkulation aktiviert sein.")
            return False
        if self.mixing_circulation_pump is None or self.sensor_circulation_pump is None:
            self.error("Mit Dosierung muessen beide Zirkulationsaktoren vorhanden sein.")
            return False
        problem = validate_dosing_run_settings(self.settings)
        if problem is not None:
            self.stop_reason = problem[0]
            self.error(f"Start blockiert: {problem[1]}")
            return False
        mixing_seconds = self.settings.get("mixing_seconds")
        if isinstance(mixing_seconds, bool) or not isinstance(mixing_seconds, (int, float)) or not 0 <= mixing_seconds < float("inf"):
            self.error("mixing_seconds fehlt oder ist ungueltig.")
            return False
        try:
            prefill = prefill_phase_target_l(plan.volume)
        except VolumeModelError as exc:
            self.error(f"Vorfuellziel ungueltig: {exc}")
            return False
        if mixer_liters >= prefill:
            self.error(f"Mixerstand {mixer_liters:.2f} L liegt bereits auf oder ueber dem Vorfuellziel {prefill:.2f} L.")
            return False
        return True

    def _phase_target_l(self) -> float | None:
        """Aktuelles RO-Ziel (Gesamtfuellstand). None nach error()."""
        if self._fill_phase == "single":
            return float(self.settings["target_total_liters"])
        try:
            if self._fill_phase == "prefill":
                return prefill_phase_target_l(self.nutrient_plan.volume)
            return dynamic_phase_target_l(self.nutrient_plan.volume, ul_to_ml(self._final_confirmed_ul or 0))
        except VolumeModelError as exc:
            self.stop_reason = "volume_target_invalid"
            self.error(f"Phasenziel nicht berechenbar: {exc}")
            return None

    def _ready_for_final_fill(self, snapshot: dict[str, Any]) -> bool:
        """Zweite RO-Phase: wie der Pumpenstart, aber mit Laufpruefung statt
        Startfreigabe (Mixer ist nicht mehr leer)."""
        self._filtered_mixer_liters(snapshot)
        if self._abort_if_runtime_invalid(snapshot):
            return False
        target = self._phase_target_l()
        mixer = self._mixer_liters_from_snapshot(snapshot)
        if target is None:
            return False
        if mixer is None or mixer >= target:
            self.stop_reason = "pump_start_blocked"
            self.error(f"Zweite RO-Phase blockiert: Mixerstand {mixer} L ist ungueltig oder bereits auf/ueber dem Ziel {target:.3f} L.")
            return False
        return True

    def _handle_dosing_start(self):
        """Genau ein Dosierstart je Lauf; es gibt keinen Wiederholungsversuch."""
        snapshot = self._read_snapshot("dosing start")
        if snapshot is None:
            return
        fresh = self._accept_measurement(
            snapshot,
            "no_new_measurement_before_dosing",
            f"Dosierstart abgebrochen: innerhalb von {MEASUREMENT_WAIT_TIMEOUT_S:.0f} s keine neue Messung empfangen.",
        )
        if not fresh:  # None (error) oder False (warten, Pumpen aus)
            return
        self._filtered_mixer_liters(snapshot)
        if self._abort_if_runtime_invalid(snapshot):
            return
        mixer = self._mixer_liters_from_snapshot(snapshot)
        evaluation = evaluate_volume_state(
            self.nutrient_plan.volume,
            VolumeState.from_ul(mixer_measured_l=mixer if mixer is not None else -1.0),
        )
        if self._error_latched():
            return
        if not evaluation.ok:
            self.stop_reason = "volume_check_failed"
            self.error(f"Dosierstart blockiert: {evaluation.message}")
            return
        # Das Vorfuellziel wurde VOR dem Einschalten der Zirkulation mit RO aus durch mehrere frische Messungen
        # bestaetigt (CONFIRM_TARGET ist der einzige Weg hierher). Mit laufender Sensorzirkulation zeigt der Tank
        # bewusst weniger an (Wasser im Sensorkreis); der Pegel wird deshalb nicht erneut gegen das Vorfuellziel
        # verglichen. Die Bestaetigung wird aber ausdruecklich vorausgesetzt (fail-closed).
        if self.target_confirm_count < int(self.settings.get("target_reached_confirm_samples", 3)):
            self.stop_reason = "prefill_not_confirmed"
            self.error("Dosierstart blockiert: Das Vorfuellziel wurde nicht bestaetigt.")
            return
        # Untere Sensorzuverlaessigkeitsgrenze. KEIN Trockenlaufschutz: Ein sicherer Mindestfuellstand der
        # Zirkulationspumpen ist nicht validiert, und die Pumpen laufen schon waehrend der Stabilisierungszeit
        # (sensor_stabilize_seconds) ohne Pegelpruefung; diese Pruefung kann sie dort nicht schuetzen.
        if mixer < MIXER_SENSOR_RELIABLE_MIN_LITERS:
            self.stop_reason = "level_below_sensor_reliable_range"
            self.error(
                f"Dosierstart blockiert: Mixerstand {mixer:.2f} L liegt unter {MIXER_SENSOR_RELIABLE_MIN_LITERS:g} L "
                "(Fuellstandssensor dort nicht belastbar)."
            )
            return
        if self._dosing_started:
            self.error("Dosierung wurde bereits gestartet; kein zweiter Start.")
            return

        self._dosing_started = True
        self._dosing_active = True
        try:
            result = self.dosing.start(self.settings, self.nutrient_plan)
        except Exception as exc:
            self.stop_reason = "dosing_start_failed"
            self.error(f"Dosierstart fehlgeschlagen: {type(exc).__name__}: {exc}")
            return
        if self._error_latched():  # Emergency Stop waehrend start(): Stop kann dort verloren gehen
            self._stop_dosing("process_error")
            return
        if not isinstance(result, dict) or result.get("success") is not True:
            self._dosing_active = False
            self.stop_reason = "dosing_start_blocked"
            self.error(f"Dosierstart blockiert: {result.get('message') if isinstance(result, dict) else result!r}")
            return
        self._phase_start_liters = mixer
        print("[DOSING] Nutrient dosing started (RO pump off).")
        self._change_state(FillAndMeasureState.DOSING_RUNNING)

    def _read_dosing_status(self) -> dict[str, Any] | None:
        """Dosierstatus lesen und auf Gesundheit pruefen. None nach error() (alles ist dann aus)."""
        try:
            status = self.dosing.get_status()
            phase = status["phase"]
            confirmed_ul = status["confirmed_ul"]
            in_flight_ul = status["in_flight_ul"]
            uncertain_ul = status["uncertain_ul"]
        except Exception as exc:
            self.stop_reason = "dosing_status_unreadable"
            self.error(f"Dosierstatus nicht lesbar: {type(exc).__name__}: {exc}")
            return None

        amounts = f"bestaetigt {confirmed_ul} uL, in Ausfuehrung {in_flight_ul} uL, unklar {uncertain_ul} uL"
        if phase in (DosingPhase.ABORTED, DosingPhase.ERROR) or (
            phase == DosingPhase.FINISHED and status.get("completion_reason") != "completed"
        ):
            self._dosing_active = False
            self.stop_reason = "dosing_failed"
            self.error(
                f"Dosierung nicht erfolgreich ({phase}, {status.get('completion_reason')}): {amounts}. "
                "Es wird nicht nachdosiert."
            )
            return None
        if phase not in (DosingPhase.RUNNING, DosingPhase.FINISHED):
            # STOPPING (die Dosierung bricht gerade ab oder jemand anderes hat gestoppt) bzw. IDLE: sofort
            # alles aus, ohne erst die Sensoren zu lesen.
            self.stop_reason = "dosing_stopped"
            self.error(f"Dosierung ist nicht mehr aktiv (Phase {phase}): {amounts}.")
            return None
        return status

    def _supervise_dosing(self) -> dict[str, Any] | None:
        """Gemeinsame Ueberwachung waehrend der Dosierung, mit RO aus (DOSING_RUNNING) und parallel zur
        RO-Befuellung (DOSING_AND_RO_FILL): Dosierstatus, Messfrische, Mixer-/RO-Plausibilitaet und die
        Volumenbilanz MIT den Mengen in Ausfuehrung. Rueckgabe None nach error(); sonst der Dosierstatus
        plus ``mixer`` (ungefiltert), ``fresh`` (neue Messung) und ``state`` (VolumeState)."""
        status = self._read_dosing_status()
        if status is None:
            return None
        snapshot = self._read_snapshot("dosing")
        if snapshot is None:
            return None
        fresh = self._accept_measurement(
            snapshot,
            "measurement_stale_during_dosing",
            f"Dosierung abgebrochen: seit {MEASUREMENT_WAIT_TIMEOUT_S:.0f} s keine neue Messung empfangen.",
        )
        if fresh is None:
            return None
        self._filtered_mixer_liters(snapshot)
        if self._abort_if_runtime_invalid(snapshot):
            return None
        mixer = self._mixer_liters_from_snapshot(snapshot)
        try:
            state = VolumeState.from_ul(
                mixer_measured_l=mixer if mixer is not None else -1.0,
                confirmed_ul=status["confirmed_ul"],
                in_flight_ul=status["in_flight_ul"],
                uncertain_ul=status["uncertain_ul"],
            )
        except VolumeModelError as exc:
            self.stop_reason = "volume_check_failed"
            self.error(f"Dosierbilanz ungueltig: {exc}")
            return None
        evaluation = evaluate_volume_state(self.nutrient_plan.volume, state)
        if not evaluation.ok:
            self.stop_reason = "volume_check_failed"
            self.error(f"Volumenbilanz waehrend der Dosierung verletzt: {evaluation.message}")
            return None
        return {**status, "mixer": mixer, "fresh": fresh, "state": state}

    def _handle_dosing_running(self):
        """RO-Pumpe aus, Zirkulation an. Erreicht der bestaetigte Anteil die Schwelle, startet die RO-Pumpe
        PARALLEL zur Dosierung (_try_resume_ro). Endet die Dosierung vorher, gilt der bisherige Ablauf
        (zweite RO-Phase nach der Dosierung)."""
        supervised = self._supervise_dosing()
        if supervised is None:
            return
        if supervised["phase"] == DosingPhase.FINISHED:
            self._dosing_finished(supervised)
            return
        self._try_resume_ro(supervised)

    def _try_resume_ro(self, supervised: dict[str, Any]) -> None:
        """Wiederaufnahme der RO-Befuellung parallel zur laufenden Dosierung, sobald der BESTAETIGTE Anteil die
        Schwelle nutrient_ro_resume_progress erreicht. Nur auf einer NEUEN Messung; die uebrigen Bedingungen
        (nichts unsicher, Pegel nicht unter Phasenbeginn, genau eine vorherige Einschaltung, Volumenbilanz)
        prueft evaluate_ro_resume. Schwelle 1.0 = keine Ueberlappung (RO erst nach vollstaendiger Dosierung)."""
        threshold = nutrient_ro_resume_progress(self.settings)
        if threshold is None:  # beim Start geprueft; hier nur Absicherung (fail-closed)
            self.stop_reason = "resume_progress_invalid"
            self.error("nutrient_ro_resume_progress fehlt oder ist ungueltig.")
            return
        if threshold >= 1.0 or not supervised["fresh"]:
            return

        decision = evaluate_ro_resume(
            self.nutrient_plan.volume,
            supervised["state"],
            progress_threshold=threshold,
            phase_baseline_l=self._phase_start_liters,
            max_negative_drift_l=float(self.settings["max_negative_level_drift_liters"]),
            ro_on_count=self._ro_on_count,
        )
        if not decision.allowed:
            if decision.reason is VolumeReason.RESUME_PROGRESS_BELOW_THRESHOLD:
                return  # noch nicht genug bestaetigt: weiter warten
            self.stop_reason = "ro_resume_blocked"
            self.error(f"RO-Wiederaufnahme blockiert: {decision.message}")
            return

        mixer = supervised["mixer"]
        target = self._ro_stop_target_l(supervised)
        if target is None:
            return
        if mixer >= target:  # RO-Wasser waere schon vollstaendig: widerspruechlich, nie blind weiterfuellen
            self.stop_reason = "ro_resume_blocked"
            self.error(f"RO-Wiederaufnahme blockiert: Mixerstand {mixer} L liegt bereits auf/ueber dem Abschaltziel {target:.3f} L.")
            return

        if not self._switch_ro_on():
            return
        # Unmittelbar nach dem Einschalten muss die Dosierung weiter gesund sein (sonst alles aus).
        if self._read_dosing_status() is None:
            return
        self.fill_started_at = self._clock()
        self._phase_start_liters = mixer
        self._resume_confirmed_ul = supervised["confirmed_ul"]
        self._ro_resumed = True
        self.target_confirm_count = 0
        print(
            f"[OVERLAP] {supervised['confirmed_ul']} uL bestaetigt (Schwelle {threshold:.0%}): "
            "RO-Pumpe parallel zur Dosierung EIN."
        )
        self._change_state(FillAndMeasureState.DOSING_AND_RO_FILL)

    def _ro_stop_target_l(self, supervised: dict[str, Any]) -> float | None:
        """RO-Abschaltziel parallel zur Dosierung: RO-Soll + bestaetigte + noch in Ausfuehrung befindliche
        Naehrstoffmenge (obere Schranke dessen, was schon im Tank sein kann). So schaltet die RO-Pumpe nie zu
        frueh ab; die Schlusspruefung (RO-Soll + gesamte bestaetigte Menge) bleibt erreichbar. Mengen ohne
        Bestaetigung zaehlen nie als bestaetigt. None nach error()."""
        try:
            return dynamic_phase_target_l(
                self.nutrient_plan.volume, ul_to_ml(supervised["confirmed_ul"] + supervised["in_flight_ul"])
            )
        except VolumeModelError as exc:
            self.stop_reason = "volume_target_invalid"
            self.error(f"Abschaltziel nicht berechenbar: {exc}")
            return None

    def _handle_dosing_and_ro_fill(self):
        """Dosierung und RO-Befuellung laufen parallel. Die RO-Pumpe schaltet SOFORT ab (auf dem ersten
        ungefilterten Wert), sobald der Mixer das dynamische Ziel erreicht, und wird nie wieder eingeschaltet;
        die Dosierung wird bis zum Ende weiterueberwacht. Ein erreichtes Zwischenziel ist KEIN erfolgreicher
        Abschluss: Nachmischen folgt erst nach _dosing_finished und der Schlusspruefung."""
        supervised = self._supervise_dosing()
        if supervised is None:
            return
        if supervised["phase"] == DosingPhase.FINISHED:
            self._dosing_finished(supervised)
            return
        if self._ro_overlap_done:
            return  # RO ist fertig und bleibt aus; die Dosierung laeuft weiter

        mixer = supervised["mixer"]
        target = self._ro_stop_target_l(supervised)
        if target is None:
            return
        self._last_unfiltered_mixer = mixer
        if self.start_mixer_liters is not None:
            self.last_added_liters = mixer - self.start_mixer_liters

        if mixer >= target:
            # Pumpe zuerst aus, in diesem Update - nie auf Filter oder einen spaeteren Takt warten.
            self._pump_off_now()
            self._ro_overlap_done = True
            self.stop_reason = "ro_target_reached_pump_off"
            print(
                f"[OVERLAP] Mixer {mixer:.3f} L >= Abschaltziel {target:.3f} L: RO-Pumpe AUS, "
                "Dosierung laeuft weiter."
            )
            return
        self._check_ro_fill_watchdog(supervised)

    def _check_ro_fill_watchdog(self, supervised: dict[str, Any]) -> None:
        """Fortschritts- und Zeitwaechter der RO-Phase, bezogen auf den RO-ANTEIL: das seit der Wiederaufnahme
        bestaetigte Naehrstoffvolumen wird herausgerechnet, damit es eine stehende RO-Pumpe nicht verdeckt."""
        if self._phase_start_liters is None or self.fill_started_at is None:
            self.stop_reason = "mixer_implausible_during_fill"
            self.error("Startzustand der RO-Phase fehlt waehrend der Befuellung.")
            return
        nutrient_l = (supervised["confirmed_ul"] - self._resume_confirmed_ul) / UL_PER_L
        watchdog = FillWatchdog(
            start_liters=self._phase_start_liters + nutrient_l,
            max_liters=float(self.settings["max_mixer_liters"]),
            max_seconds=float(self.settings["max_fill_seconds"]),
            no_progress_timeout_seconds=float(self.settings["no_fill_progress_timeout_seconds"]),
            min_progress_liters=float(self.settings["min_fill_progress_liters"]),
            max_negative_drift_liters=float(self.settings["max_negative_level_drift_liters"]),
        )
        fill_elapsed = self._clock() - self.fill_started_at
        result = watchdog.check(self.last_filtered_mixer_liters, fill_elapsed, max_elapsed_seconds=fill_elapsed)
        if result is not None:
            self.stop_reason, message = result
            self.error(message)

    def _dosing_finished(self, supervised: dict[str, Any]) -> None:
        """Dosierung beendet. Nur vollstaendig bestaetigt und ohne unklare oder offene Menge geht es weiter.
        Das endgueltige Ziel ist danach RO-Soll + gesamte bestaetigte Menge. Nachmischen gibt es erst nach
        der Bestaetigung dieses Ziels bei ausgeschalteter RO-Pumpe (CONFIRM_TARGET, Schlussbilanz)."""
        self._dosing_active = False
        confirmed_ul = supervised["confirmed_ul"]
        if supervised["uncertain_ul"] != 0 or supervised["in_flight_ul"] != 0 or confirmed_ul != self.nutrient_plan.total_ul:
            self.stop_reason = "dosing_incomplete"
            self.error(
                f"Dosierung unvollstaendig: bestaetigt {confirmed_ul} von {self.nutrient_plan.total_ul} uL, "
                f"in Ausfuehrung {supervised['in_flight_ul']} uL, unklar {supervised['uncertain_ul']} uL."
            )
            return
        self._final_confirmed_ul = confirmed_ul
        self._fill_phase = "final"
        self.target_confirm_count = 0
        if not self._ro_resumed:
            # Bisheriger Ablauf: die zweite RO-Phase beginnt erst jetzt.
            self._phase_start_liters = supervised["mixer"]
            self.fill_started_at = None
            print(f"[DOSING] Dosing confirmed ({confirmed_ul} uL). Resuming RO fill.")
            self._change_state(FillAndMeasureState.START_REFILL_PUMP)
        elif self._ro_overlap_done:
            print(f"[DOSING] Dosing confirmed ({confirmed_ul} uL), RO already off: confirming the final target.")
            self._change_state(FillAndMeasureState.CONFIRM_TARGET)
        else:
            # RO laeuft noch: Pumpe bleibt an (kein erneutes Einschalten), das Ziel ist jetzt endgueltig. Die
            # Fortschrittsbasis wird um das seit der Wiederaufnahme bestaetigte Naehrstoffvolumen angehoben.
            self._phase_start_liters += (confirmed_ul - self._resume_confirmed_ul) / UL_PER_L
            print(f"[DOSING] Dosing confirmed ({confirmed_ul} uL), RO still filling to the final target.")
            self._change_state(FillAndMeasureState.FILL_UNTIL_TARGET)

    def _final_volume_ok(self) -> bool:
        """Schlusspruefung der Mengenbilanz bei ausgeschalteter RO-Pumpe."""
        mixer = self._last_unfiltered_mixer
        if mixer is None or self._final_confirmed_ul is None:
            self.stop_reason = "volume_check_failed"
            self.error("Schlussbilanz nicht moeglich: Mixerstand oder bestaetigte Menge fehlt.")
            return False
        evaluation = evaluate_final_volume(
            self.nutrient_plan.volume, VolumeState.from_ul(mixer_measured_l=mixer, confirmed_ul=self._final_confirmed_ul)
        )
        if not evaluation.ok:
            self.stop_reason = "final_volume_check_failed"
            self.error(f"Schlussbilanz verletzt: {evaluation.message}")
            return False
        return True

    def _handle_post_mix(self):
        """Nachmischen bei laufender Zirkulation, RO-Pumpe aus; Messfrische und
        Plausibilitaet werden weiter geprueft."""
        snapshot = self._read_snapshot("post mix")
        if snapshot is None:
            return
        fresh = self._accept_measurement(
            snapshot,
            "measurement_stale_during_post_mix",
            f"Nachmischen abgebrochen: seit {MEASUREMENT_WAIT_TIMEOUT_S:.0f} s keine neue Messung empfangen.",
        )
        if fresh is None:
            return
        self._filtered_mixer_liters(snapshot)
        if self._abort_if_runtime_invalid(snapshot):
            return
        if self._state_elapsed_seconds() >= float(self.settings["mixing_seconds"]):
            self._change_state(FillAndMeasureState.MEASURE_VALUES)

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

        self._stop_dosing("process_error")

    def _stop_dosing(self, reason: str) -> None:
        """Signal-only: der Dosier-Controller sendet selbst STOPALL und beendet
        seinen Thread. Nur solange eine gestartete Dosierung noch keinen
        Endstatus gemeldet hat."""
        if self.dosing is None or not self._dosing_active:
            return
        try:
            self.dosing.request_stop(reason)
        except Exception as exc:  # Abschaltpfad darf nie werfen
            print(f"[ERROR] Dosing stop request failed: {type(exc).__name__}: {exc}")

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
