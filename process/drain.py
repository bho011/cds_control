from __future__ import annotations

import time
from dataclasses import replace

from .common import (
    PhaseResult,
    make_level_history,
    read_metrics,
    publish_process_status,
    log_step,
)
from .drain_safety import (
    DrainSafetyLimits,
    DrainSafetySettingsError,
    DrainSensorWatchdog,
    filtered_after_observation,
    raw_sample_from_snapshot,
    safe_stop_drain,
    snapshot_stamp,
)
from .watchdog import EmptyConfirmCounter


def _wait(seconds: float, stop_event) -> bool:
    """Sleep `seconds`; True if a stop was requested meanwhile (only with a stop_event)."""
    if stop_event is not None:
        return bool(stop_event.wait(seconds))
    time.sleep(seconds)
    return False


def run_drain_phase(
    settings,
    sensor_reader,
    actuators,
    mqtt_publisher,
    logger,
    auto_circulation=None,
    stop_event=None,
) -> PhaseResult:
    print()
    print("[PHASE] DRAIN_BY_PUMP_CAPACITY")
    print("[INFO] Sensor is used for early empty detection and trend logging.")
    print("[INFO] Maximum runtime is calculated from tank content and transfer pump capacity.")

    level_history = make_level_history(settings)

    try:
        start_metrics = read_metrics(sensor_reader, settings, level_history)
    except Exception as exc:
        print(f"[ERROR] Sensor read failed before drain: {type(exc).__name__}: {exc}")
        return PhaseResult(False, "sensor_read_error_before_drain", None, None, None)
    start_liters = start_metrics.mixer_liters_filtered

    if start_liters is None:
        print("[ERROR] Missing mixer level before drain.")
        return PhaseResult(False, "missing_mixer_level_before_drain", None, None, None)

    # AP-04A: every setting that feeds the timeout, settle time, empty detection
    # and plausibility checks is validated BEFORE any actuator is touched
    # (fail closed). Direct callers can not slip NaN/Infinity past this.
    try:
        safety_limits = DrainSafetyLimits.from_settings(settings, start_liters)
    except DrainSafetySettingsError as exc:
        print(f"[ERROR] Drain blocked, unusable safety settings: {exc}")
        return PhaseResult(False, "invalid_drain_safety_settings", start_liters, start_liters, 0.0)

    empty_threshold = safety_limits.empty_threshold_liters
    empty_confirm_samples = safety_limits.empty_confirm_samples

    if start_liters <= empty_threshold:
        print(f"[INFO] Tank already empty enough: {start_liters:.2f} L <= {empty_threshold:.2f} L")
        return PhaseResult(True, "already_empty_by_sensor", start_liters, start_liters, 0.0)

    pump_lpm = safety_limits.transfer_pump_liters_per_minute

    expected_seconds = safety_limits.expected_seconds
    max_drain_seconds = safety_limits.max_drain_seconds

    print(f"[DRAIN START] Mixer={start_liters:.2f} L")
    print(f"[DRAIN CALC] Transfer pump={pump_lpm:.2f} L/min")
    print(f"[DRAIN CALC] Expected drain time={expected_seconds:.1f}s")
    print(f"[DRAIN CALC] Max runtime with buffer={max_drain_seconds:.1f}s")
    print(f"[DRAIN TARGET] Empty threshold <= {empty_threshold:.2f} L for {empty_confirm_samples} samples")

    if auto_circulation is not None:
        auto_circulation.update(start_liters)

    try:
        drain_valve = actuators.get("drain_valve_0")
        transfer_pump = actuators.get("transfer_pump")
    except Exception as exc:
        print(f"[ERROR] Drain blocked, actuator lookup failed: {type(exc).__name__}: {exc}")
        return PhaseResult(False, "actuator_lookup_failed", start_liters, start_liters, 0.0)

    drain_start_time = time.monotonic()
    stop_reason = None
    error = None
    final_liters = start_liters
    drained_liters = 0.0
    empty_confirm = EmptyConfirmCounter(empty_threshold, empty_confirm_samples)
    metrics = start_metrics
    elapsed = 0.0

    try:
        # Start: valve open -> settle -> re-check stop -> pump on.
        drain_valve.on()

        if _wait(safety_limits.valve_settle_seconds, stop_event):
            stop_reason = "stop_requested_during_valve_settle"
            error = "Stop requested during valve settle time; pump never started."
        elif stop_event is not None and stop_event.is_set():
            stop_reason = "stop_requested_before_pump_start"
            error = "Stop requested before pump start; pump never started."
        else:
            transfer_pump.on()
            sensor_watchdog = DrainSensorWatchdog(
                safety_limits, time.monotonic(), snapshot_stamp(start_metrics.snapshot)
            )

            publish_process_status(
                mqtt_publisher,
                "DRAIN_RUNNING",
                actuators,
                details={
                    "start_liters": start_liters,
                    "pump_liters_per_minute": pump_lpm,
                    "expected_seconds": round(expected_seconds, 1),
                    "max_drain_seconds": round(max_drain_seconds, 1),
                    "empty_threshold_liters": empty_threshold,
                },
            )

            while True:
                if _wait(0.5, stop_event):
                    stop_reason = "stop_requested"
                    error = "Drain stopped on request."
                    break

                elapsed = time.monotonic() - drain_start_time
                history_before = list(level_history)
                metrics = read_metrics(sensor_reader, settings, level_history)
                raw_liters, stamp = raw_sample_from_snapshot(metrics.snapshot, settings)
                # `now` is taken AFTER the read, so a slow read can not hide an expired deadline.
                observation = sensor_watchdog.observe(raw_liters, stamp, time.monotonic())
                # A re-read of the same snapshot must not weight the moving average twice.
                metrics = replace(
                    metrics,
                    mixer_liters_filtered=filtered_after_observation(
                        level_history,
                        history_before,
                        metrics.mixer_liters_filtered,
                        raw_liters is not None,
                        observation.is_new_sample,
                    ),
                )

                if auto_circulation is not None:
                    auto_circulation.update(metrics.mixer_liters_filtered)

                if metrics.mixer_liters_filtered is not None:
                    final_liters = metrics.mixer_liters_filtered
                    drained_liters = start_liters - final_liters
                else:
                    final_liters = None

                # A repeated snapshot is not an independent measurement and
                # must not advance the empty confirmation.
                if observation.is_new_sample:
                    is_empty_confirmed = empty_confirm.update(metrics.mixer_liters_filtered)
                else:
                    is_empty_confirmed = False

                print(
                    f"[DRAIN] elapsed={elapsed:.1f}/{max_drain_seconds:.1f}s | "
                    f"Mixer={final_liters} L | "
                    f"Drained(sensor_calc)={drained_liters:.2f} L | "
                    f"empty_confirm={empty_confirm.count}/{empty_confirm_samples}"
                )

                if observation.violation is not None:
                    stop_reason, message = observation.violation
                    error = f"{stop_reason}: {message}"
                    break

                publish_process_status(
                    mqtt_publisher,
                    "DRAIN_RUNNING",
                    actuators,
                    details={
                        "elapsed_seconds": round(elapsed, 1),
                        "max_drain_seconds": round(max_drain_seconds, 1),
                        "mixer_liters_filtered": final_liters,
                        "drained_liters_sensor_calc": drained_liters,
                        "empty_confirm_count": empty_confirm.count,
                        "empty_confirm_samples": empty_confirm_samples,
                    },
                )

                log_step(
                    logger=logger,
                    phase="DRAIN_RUNNING",
                    stop_reason=None,
                    error=None,
                    metrics=metrics,
                    actuators=actuators,
                    start_liters=start_liters,
                    added_liters=None,
                    drained_liters=drained_liters,
                    elapsed_seconds=elapsed,
                    target_delta_liters=None,
                )

                if is_empty_confirmed:
                    stop_reason = "empty_by_sensor"
                    error = None
                    break

                if elapsed >= max_drain_seconds:
                    stop_reason = "calculated_drain_timeout"
                    error = "Drain timeout reached before empty sensor confirmation."
                    break

    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
        error = "KeyboardInterrupt"
        print("\n[ABORT] Drain durch Benutzer abgebrochen.")

    except Exception as exc:
        stop_reason = "error"
        error = f"{type(exc).__name__}: {exc}"
        print(f"\n[ERROR] Drain aborted by exception: {error}")

    finally:
        # Stop: transfer pump OFF, then drain valve OFF (both always attempted).
        cleanup = safe_stop_drain(transfer_pump, drain_valve)

    if not cleanup.ok:
        # O1 (open hardware decision): behaviour after a failing pump switch-off
        # is unchanged. The failure is reported, never reported as success.
        print(f"[ERROR] Switch-off errors: {'; '.join(cleanup.errors)}")
        print("[ERROR] Verify transfer pump and drain valve PHYSICALLY.")
        error = "; ".join(filter(None, [error, f"cleanup_failed: {'; '.join(cleanup.errors)}"]))
        stop_reason = "drain_cleanup_failed"
    else:
        print("[SAFE] Switch-off commands issued (transfer pump, then drain valve); no physical feedback available.")

    elapsed = time.monotonic() - drain_start_time

    try:
        metrics = read_metrics(sensor_reader, settings, level_history)
    except Exception as exc:
        print(f"[WARN] Final sensor read failed: {type(exc).__name__}: {exc}")

    if metrics.mixer_liters_filtered is not None:
        final_liters = metrics.mixer_liters_filtered
        drained_liters = start_liters - final_liters

    print(f"[DRAIN STOP] reason={stop_reason}")
    print(f"[DRAIN END] Mixer={final_liters} L | Drained(sensor_calc)={drained_liters:.2f} L")

    try:
        publish_process_status(
            mqtt_publisher,
            "DRAIN_STOPPED",
            actuators,
            error=error,
            details={
                "stop_reason": stop_reason,
                "elapsed_seconds": round(elapsed, 1),
                "mixer_liters_filtered": final_liters,
                "drained_liters_sensor_calc": drained_liters,
                "cleanup_errors": cleanup.errors,
            },
        )
    except Exception as exc:
        print(f"[WARN] Final status publish failed: {type(exc).__name__}: {exc}")

    log_step(
        logger=logger,
        phase="DRAIN_STOPPED",
        stop_reason=stop_reason,
        error=error,
        metrics=metrics,
        actuators=actuators,
        start_liters=start_liters,
        added_liters=None,
        drained_liters=drained_liters,
        elapsed_seconds=elapsed,
        target_delta_liters=None,
    )

    return PhaseResult(
        success=(stop_reason == "empty_by_sensor" and cleanup.ok),
        stop_reason=stop_reason or "unknown",
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=drained_liters,
    )
