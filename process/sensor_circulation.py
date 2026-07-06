from __future__ import annotations

import select
import sys
import time

from .common import (
    PhaseResult,
    make_level_history,
    read_metrics,
    publish_process_status,
    log_step,
)


def run_sensor_pump_phase(settings, sensor_reader, actuators, mqtt_publisher, logger) -> PhaseResult:
    print()
    print("[PHASE] SENSOR_PUMP")
    print("[INFO] Sensorpumpe läuft über contactor_2.")
    print("[INFO] Eingabe 'stop' + Enter beendet diese Phase kontrolliert.")
    print("[INFO] STRG+C bleibt nur für echten Notabbruch.")

    level_history = make_level_history(settings)
    sensor_pump_seconds = float(settings.get("sensor_pump_seconds", 200.0))

    start_metrics = read_metrics(sensor_reader, settings, level_history)
    start_liters = start_metrics.mixer_liters_filtered

    sensor_pump = actuators.get("sensor_circulation_pump")

    start_time = time.monotonic()
    stop_reason = "sensor_pump_time_completed"
    error = None
    final_liters = start_liters

    try:
        sensor_pump.on()

        publish_process_status(
            mqtt_publisher,
            "SENSOR_PUMP_RUNNING",
            actuators,
            details={
                "sensor_pump_seconds": sensor_pump_seconds,
                "stop_command": "stop",
            },
        )

        while True:
            elapsed = time.monotonic() - start_time
            metrics = read_metrics(sensor_reader, settings, level_history)
            final_liters = metrics.mixer_liters_filtered

            print(
                f"[SENSOR] elapsed={elapsed:.1f}/{sensor_pump_seconds:.1f}s | "
                f"Mixer={final_liters} L | "
                f"Eingabe 'stop' + Enter zum Beenden"
            )

            publish_process_status(
                mqtt_publisher,
                "SENSOR_PUMP_RUNNING",
                actuators,
                details={
                    "elapsed_seconds": round(elapsed, 1),
                    "sensor_pump_seconds": sensor_pump_seconds,
                    "mixer_liters_filtered": final_liters,
                },
            )

            log_step(
                logger=logger,
                phase="SENSOR_PUMP_RUNNING",
                stop_reason=None,
                error=None,
                metrics=metrics,
                actuators=actuators,
                start_liters=start_liters,
                added_liters=None,
                drained_liters=None,
                elapsed_seconds=elapsed,
                target_delta_liters=None,
            )

            if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                command = sys.stdin.readline().strip().lower()
                if command == "stop":
                    stop_reason = "sensor_pump_stopped_by_user"
                    break
                elif command:
                    print(f"[INFO] Unbekannte Eingabe ignoriert: {command}")

            if elapsed >= sensor_pump_seconds:
                stop_reason = "sensor_pump_time_completed"
                break

            time.sleep(1.0)

    finally:
        sensor_pump.off()

    elapsed = time.monotonic() - start_time
    metrics = read_metrics(sensor_reader, settings, level_history)

    if metrics.mixer_liters_filtered is not None:
        final_liters = metrics.mixer_liters_filtered

    print(f"[SENSOR STOP] reason={stop_reason}")
    print("[SAFE] Sensorpumpe AUS")

    publish_process_status(
        mqtt_publisher,
        "SENSOR_PUMP_STOPPED",
        actuators,
        error=error,
        details={
            "stop_reason": stop_reason,
            "elapsed_seconds": round(elapsed, 1),
            "mixer_liters_filtered": final_liters,
        },
    )

    log_step(
        logger=logger,
        phase="SENSOR_PUMP_STOPPED",
        stop_reason=stop_reason,
        error=error,
        metrics=metrics,
        actuators=actuators,
        start_liters=start_liters,
        added_liters=None,
        drained_liters=None,
        elapsed_seconds=elapsed,
        target_delta_liters=None,
    )

    return PhaseResult(
        success=True,
        stop_reason=stop_reason,
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=None,
    )
