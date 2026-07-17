from __future__ import annotations

import time

from .common import (
    PhaseResult,
    make_level_history,
    read_metrics,
    publish_process_status,
    log_step,
)
from .watchdog import FillWatchdog


def run_fill_phase(settings, sensor_reader, actuators, mqtt_publisher, logger, auto_circulation=None) -> PhaseResult:
    print()
    print("[PHASE] FILL_ABSOLUTE")
    print("[INFO] RO refill line must lead directly into the Mixing Tank.")
    print("[INFO] No valves are opened during refill.")

    level_history = make_level_history(settings)
    target_total = float(settings["target_fill_total_liters"])

    initial_metrics = read_metrics(sensor_reader, settings, level_history)

    if initial_metrics.mixer_liters_filtered is None:
        return PhaseResult(False, "missing_mixer_level", None, None, None)

    if initial_metrics.ro_liters is None:
        return PhaseResult(False, "missing_ro_level", initial_metrics.mixer_liters_filtered, None, None)

    min_ro = float(settings["min_ro_liters_required"])
    if initial_metrics.ro_liters < min_ro:
        return PhaseResult(
            False,
            "not_enough_ro_water",
            initial_metrics.mixer_liters_filtered,
            initial_metrics.mixer_liters_filtered,
            0.0,
        )

    start_liters = initial_metrics.mixer_liters_filtered
    target_delta_display = max(0.0, target_total - start_liters)

    print(f"[START] Mixer filtered start: {start_liters:.2f} L")
    print(f"[START] RO available: {initial_metrics.ro_liters:.2f} L")
    print(f"[TARGET] Fill until absolute level: {target_total:.2f} L")
    print(f"[TARGET] Expected additional fill: {target_delta_display:.2f} L")

    if start_liters >= target_total:
        print("[INFO] Start level already at or above target.")
        return PhaseResult(
            True,
            "already_at_or_above_target",
            start_liters,
            start_liters,
            0.0,
        )

    refill_pump = actuators.get("mixer_refill_pump")

    watchdog = FillWatchdog(
        start_liters=start_liters,
        max_liters=float(settings["max_mixer_liters"]),
        max_seconds=float(settings["max_fill_seconds"]),
        no_progress_timeout_seconds=float(settings["no_fill_progress_timeout_seconds"]),
        min_progress_liters=float(settings["min_fill_progress_liters"]),
        max_negative_drift_liters=float(settings.get("max_negative_level_drift_liters", 3.0)),
    )

    fill_start_time = time.monotonic()
    target_confirm_count = 0
    stop_reason = None
    error = None
    final_liters = start_liters
    added_liters = 0.0

    try:
        refill_pump.on()

        publish_process_status(
            mqtt_publisher,
            "FILL_RUNNING",
            actuators,
            details={
                "fill_target_mode": "absolute",
                "target_total_liters": target_total,
                "start_mixer_liters": start_liters,
            },
        )

        while True:
            time.sleep(0.5)

            elapsed = time.monotonic() - fill_start_time
            metrics = read_metrics(sensor_reader, settings, level_history)

            if auto_circulation is not None:
                auto_circulation.update(metrics.mixer_liters_filtered)

            watchdog_result = watchdog.check(metrics.mixer_liters_filtered, elapsed)
            if watchdog_result is not None:
                stop_reason, error = watchdog_result
                break

            final_liters = metrics.mixer_liters_filtered
            added_liters = final_liters - start_liters

            if final_liters >= target_total:
                target_confirm_count += 1
            else:
                target_confirm_count = 0

            required_confirm = int(settings.get("target_reached_confirm_samples", 3))

            print(
                f"[FILL] Mixer={final_liters:.2f}/{target_total:.2f} L | "
                f"Added={added_liters:.2f} L | "
                f"elapsed={elapsed:.1f}s | "
                f"confirm={target_confirm_count}/{required_confirm}"
            )

            publish_process_status(
                mqtt_publisher,
                "FILL_RUNNING",
                actuators,
                details={
                    "mixer_liters_filtered": final_liters,
                    "added_liters": added_liters,
                    "target_total_liters": target_total,
                    "elapsed_seconds": round(elapsed, 1),
                    "target_confirm_count": target_confirm_count,
                },
            )

            log_step(
                logger=logger,
                phase="FILL_RUNNING",
                stop_reason=None,
                error=None,
                metrics=metrics,
                actuators=actuators,
                start_liters=start_liters,
                added_liters=added_liters,
                drained_liters=None,
                elapsed_seconds=elapsed,
                target_delta_liters=target_delta_display,
            )

            if target_confirm_count >= required_confirm:
                stop_reason = "target_reached_by_sensor"
                error = None
                break

    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
        error = "KeyboardInterrupt"
        print("\n[ABORT] Fill durch Benutzer abgebrochen.")

    finally:
        refill_pump.off()

    elapsed = time.monotonic() - fill_start_time
    metrics = read_metrics(sensor_reader, settings, level_history)

    if metrics.mixer_liters_filtered is not None:
        final_liters = metrics.mixer_liters_filtered
        added_liters = final_liters - start_liters

    print(f"[FILL STOP] reason={stop_reason}")
    print(f"[FILL END] Mixer={final_liters:.2f} L | Added={added_liters:.2f} L")

    publish_process_status(
        mqtt_publisher,
        "FILL_STOPPED",
        actuators,
        error=error,
        details={
            "stop_reason": stop_reason,
            "target_total_liters": target_total,
            "mixer_liters_filtered": final_liters,
            "added_liters": added_liters,
            "elapsed_seconds": round(elapsed, 1),
        },
    )

    log_step(
        logger=logger,
        phase="FILL_STOPPED",
        stop_reason=stop_reason,
        error=error,
        metrics=metrics,
        actuators=actuators,
        start_liters=start_liters,
        added_liters=added_liters,
        drained_liters=None,
        elapsed_seconds=elapsed,
        target_delta_liters=target_delta_display,
    )

    return PhaseResult(
        success=(stop_reason == "target_reached_by_sensor"),
        stop_reason=stop_reason or "unknown",
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=added_liters,
    )
