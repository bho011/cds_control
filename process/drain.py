from __future__ import annotations

import time

from .common import (
    PhaseResult,
    make_level_history,
    read_metrics,
    publish_process_status,
    log_step,
)
from .watchdog import EmptyConfirmCounter, calculate_drain_timeout_seconds


def run_drain_phase(settings, sensor_reader, actuators, mqtt_publisher, logger, auto_circulation=None) -> PhaseResult:
    print()
    print("[PHASE] DRAIN_BY_PUMP_CAPACITY")
    print("[INFO] Sensor is used for early empty detection and trend logging.")
    print("[INFO] Maximum runtime is calculated from tank content and transfer pump capacity.")

    level_history = make_level_history(settings)

    start_metrics = read_metrics(sensor_reader, settings, level_history)
    start_liters = start_metrics.mixer_liters_filtered

    if start_liters is None:
        print("[ERROR] Missing mixer level before drain.")
        return PhaseResult(False, "missing_mixer_level_before_drain", None, None, None)

    empty_threshold = float(settings.get("empty_threshold_liters", 0.3))
    empty_confirm_samples = int(settings.get("empty_confirm_samples", 5))

    if start_liters <= empty_threshold:
        print(f"[INFO] Tank already empty enough: {start_liters:.2f} L <= {empty_threshold:.2f} L")
        return PhaseResult(True, "already_empty_by_sensor", start_liters, start_liters, 0.0)

    pump_lpm = float(settings.get("transfer_pump_liters_per_minute", 16.0))
    buffer_seconds = float(settings.get("drain_timeout_buffer_seconds", 180.0))

    expected_seconds = (start_liters / pump_lpm) * 60.0
    max_drain_seconds = calculate_drain_timeout_seconds(start_liters, pump_lpm, buffer_seconds)

    no_progress_warning_seconds = float(settings.get("no_drain_progress_warning_seconds", 60.0))

    print(f"[DRAIN START] Mixer={start_liters:.2f} L")
    print(f"[DRAIN CALC] Transfer pump={pump_lpm:.2f} L/min")
    print(f"[DRAIN CALC] Expected drain time={expected_seconds:.1f}s")
    print(f"[DRAIN CALC] Max runtime with buffer={max_drain_seconds:.1f}s")
    print(f"[DRAIN TARGET] Empty threshold <= {empty_threshold:.2f} L for {empty_confirm_samples} samples")

    if auto_circulation is not None:
        auto_circulation.update(start_liters)

    drain_valve = actuators.get("drain_valve_0")
    transfer_pump = actuators.get("transfer_pump")

    drain_start_time = time.monotonic()
    stop_reason = None
    error = None
    final_liters = start_liters
    drained_liters = 0.0
    empty_confirm = EmptyConfirmCounter(empty_threshold, empty_confirm_samples)
    no_progress_warning_printed = False

    try:
        drain_valve.on()
        time.sleep(float(settings.get("valve_settle_seconds", 1.0)))

        transfer_pump.on()

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
            time.sleep(0.5)

            elapsed = time.monotonic() - drain_start_time
            metrics = read_metrics(sensor_reader, settings, level_history)

            if auto_circulation is not None:
                auto_circulation.update(metrics.mixer_liters_filtered)

            if metrics.mixer_liters_filtered is not None:
                final_liters = metrics.mixer_liters_filtered
                drained_liters = start_liters - final_liters
            else:
                final_liters = None

            is_empty_confirmed = empty_confirm.update(metrics.mixer_liters_filtered)

            # Warning only, deliberately not an abort: the calculated
            # max_drain_seconds timeout above already bounds worst-case
            # runtime, so a stalled sensor reading here can't run the pump
            # unboundedly. An early log line is more useful to an operator
            # watching the run than aborting an otherwise-healthy drain on a
            # transient reading.
            if (
                not no_progress_warning_printed
                and elapsed >= no_progress_warning_seconds
                and drained_liters < 0.3
            ):
                print(
                    f"[DRAIN WARN] Sensor shows little/no drain progress after "
                    f"{elapsed:.1f}s. Continuing until empty or calculated timeout."
                )
                no_progress_warning_printed = True

            print(
                f"[DRAIN] elapsed={elapsed:.1f}/{max_drain_seconds:.1f}s | "
                f"Mixer={final_liters} L | "
                f"Drained(sensor_calc)={drained_liters:.2f} L | "
                f"empty_confirm={empty_confirm.count}/{empty_confirm_samples}"
            )

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

    finally:
        transfer_pump.off()
        drain_valve.off()

    elapsed = time.monotonic() - drain_start_time
    metrics = read_metrics(sensor_reader, settings, level_history)

    if metrics.mixer_liters_filtered is not None:
        final_liters = metrics.mixer_liters_filtered
        drained_liters = start_liters - final_liters

    print(f"[DRAIN STOP] reason={stop_reason}")
    print(f"[DRAIN END] Mixer={final_liters} L | Drained(sensor_calc)={drained_liters:.2f} L")
    print("[SAFE] Transfer pump OFF, Drain valve OFF")

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
        },
    )

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
        success=(stop_reason == "empty_by_sensor"),
        stop_reason=stop_reason or "unknown",
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=drained_liters,
    )
