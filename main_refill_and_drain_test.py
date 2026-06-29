import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpio_config import OUTPUTS, ACTIVE_LOW
from hardware.actuator_manager import ActuatorManager
from services.mqtt_publisher import MqttPublisher
from services.process_run_logger import ProcessRunLogger
from services.sensor_snapshot import SensorSnapshotReader


SETTINGS_PATH = Path("config/refill_and_drain_test_settings.json")


@dataclass
class Metrics:
    snapshot: dict[str, Any]
    mixer_liters_filtered: float | None
    ro_liters: float | None


@dataclass
class PhaseResult:
    success: bool
    stop_reason: str
    start_liters: float | None
    end_liters: float | None
    delta_liters: float | None


def load_settings() -> dict:
    with SETTINGS_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def require_hardware_confirmation(settings: dict) -> bool:
    if not settings.get("hardware_execution_enabled", False):
        print("[BLOCKED] Hardware execution is disabled.")
        print("[INFO] Set hardware_execution_enabled to true only when you are on site")
        print("[INFO] and the RO water path / drain path is physically confirmed.")
        return False

    required_text = settings.get("required_confirmation_text", "confirmed")

    print()
    print("Sicherheitsbestätigung erforderlich.")
    print(f"Zum Start exakt eingeben: {required_text}")
    confirmation = input("Bestätigung: ").strip()

    if confirmation != required_text:
        print("[BLOCKED] Sicherheitsbestätigung falsch. Abbruch.")
        return False

    return True


def confirm_flush(settings: dict) -> bool:
    answer = input("Flush über Transferpumpe + Valve_0_Drain starten? ja/nein: ").strip().lower()

    if answer != "ja":
        print("[INFO] Flush wurde nicht bestätigt. Wasser bleibt im Mixing Tank.")
        return False

    required_text = settings.get("required_flush_confirmation_text", "flush_confirmed")

    print()
    print("Flush-Sicherheitsbestätigung erforderlich.")
    print(f"Zum Flush exakt eingeben: {required_text}")
    confirmation = input("Flush-Bestätigung: ").strip()

    if confirmation != required_text:
        print("[BLOCKED] Flush-Bestätigung falsch. Kein Flush.")
        return False

    return True


def make_level_history(settings: dict):
    samples = int(settings.get("level_filter_samples", 5))
    return deque(maxlen=max(1, samples))


def apply_mixer_sensor_calibration(raw_liters: float, settings: dict) -> float:
    factor = float(settings.get("mixer_sensor_liter_factor", 1.0))
    offset = float(settings.get("mixer_sensor_liter_offset", 0.0))

    calibrated = (raw_liters * factor) + offset

    # Negative Literwerte vermeiden, falls Offset/Faktor später angepasst werden.
    return max(0.0, calibrated)


def mixer_liters_from_snapshot(snapshot: dict[str, Any], settings: dict) -> float | None:
    mixer = snapshot.get("mixer") or {}
    level_percent = mixer.get("level_percent")

    if level_percent is not None:
        max_liters = float(settings["max_mixer_liters"])
        raw_liters = (float(level_percent) / 100.0) * max_liters
        return apply_mixer_sensor_calibration(raw_liters, settings)

    fallback = mixer.get("volume_liters_calc")

    if fallback is not None:
        return apply_mixer_sensor_calibration(float(fallback), settings)

    return None


def ro_liters_from_snapshot(snapshot: dict[str, Any]) -> float | None:
    ro = snapshot.get("ro") or {}
    level_percent = ro.get("level_percent")
    configured_max = ro.get("configured_max_liters")

    if level_percent is not None and configured_max is not None:
        return (float(level_percent) / 100.0) * float(configured_max)

    fallback = ro.get("volume_liters_calc")
    return float(fallback) if fallback is not None else None


def read_metrics(sensor_reader, settings, level_history) -> Metrics:
    snapshot = sensor_reader.get_latest(max_age_seconds=5.0)

    if snapshot is None:
        return Metrics(snapshot={}, mixer_liters_filtered=None, ro_liters=None)

    mixer_liters = mixer_liters_from_snapshot(snapshot, settings)

    if mixer_liters is not None:
        level_history.append(mixer_liters)
        mixer_filtered = sum(level_history) / len(level_history)
    else:
        mixer_filtered = None

    ro_liters = ro_liters_from_snapshot(snapshot)

    return Metrics(
        snapshot=snapshot,
        mixer_liters_filtered=mixer_filtered,
        ro_liters=ro_liters,
    )


def publish_process_status(
    mqtt_publisher,
    phase: str,
    actuators,
    error: str | None = None,
    details: dict[str, Any] | None = None,
):
    status = actuators.status_payload()

    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "python",
        "process_state": phase,
        "actuators": {
            "mixer_refill_pump": status.get("mixer_refill_pump", False),
            "transfer_pump": status.get("transfer_pump", False),
            "drain_valve_0": status.get("drain_valve_0", False),
            "supply_valve_6": status.get("supply_valve_6"),
            "mixing_circulation_pump": status.get("mixing_circulation_pump"),
            "sensor_circulation_pump": status.get("sensor_circulation_pump"),
        },
        "process_details": details or {},
        "error": error,
    }

    mqtt_publisher.publish_json(payload)


def log_step(
    logger,
    phase: str,
    stop_reason: str | None,
    error: str | None,
    metrics: Metrics,
    actuators,
    start_liters: float | None,
    added_liters: float | None,
    drained_liters: float | None,
    elapsed_seconds: float | None,
    target_delta_liters: float | None,
):
    try:
        logger.write_step(
            state=phase,
            error=error,
            snapshot=metrics.snapshot,
            actuator_status=actuators.status_payload(),
            mixer_liters_filtered=metrics.mixer_liters_filtered,
            start_mixer_liters=start_liters,
            added_liters=added_liters,
            extra={
                "phase": phase,
                "stop_reason": stop_reason,
                "elapsed_seconds": elapsed_seconds,
                "target_delta_liters": target_delta_liters,
                "drained_liters": drained_liters,
            },
        )
    except Exception as exc:
        print(f"[LOG WARN] Could not write process log step: {exc}")


def run_fill_phase(settings, sensor_reader, actuators, mqtt_publisher, logger) -> PhaseResult:
    print()
    print("[PHASE] FILL_DELTA")
    print("[INFO] RO refill line must lead directly into the Mixing Tank.")
    print("[INFO] No valves are opened during refill.")

    level_history = make_level_history(settings)
    target_delta = float(settings["target_fill_delta_liters"])

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
    print(f"[START] Mixer filtered start: {start_liters:.2f} L")
    print(f"[START] RO available: {initial_metrics.ro_liters:.2f} L")
    print(f"[TARGET] Fill delta: {target_delta:.2f} L")

    refill_pump = actuators.get("mixer_refill_pump")

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
                "target_delta_liters": target_delta,
                "start_mixer_liters": start_liters,
                "stop_mode": "sensor_or_timeout",
            },
        )

        while True:
            time.sleep(0.5)

            elapsed = time.monotonic() - fill_start_time
            metrics = read_metrics(sensor_reader, settings, level_history)

            if metrics.mixer_liters_filtered is None:
                stop_reason = "missing_mixer_level_during_fill"
                error = stop_reason
                break

            final_liters = metrics.mixer_liters_filtered
            added_liters = final_liters - start_liters

            max_mixer_liters = float(settings["max_mixer_liters"])
            if final_liters > max_mixer_liters:
                stop_reason = "mixer_over_max_limit"
                error = f"Mixer level {final_liters:.2f} L > {max_mixer_liters:.2f} L"
                break

            max_negative_drift = float(settings.get("max_negative_level_drift_liters", 2.0))
            if added_liters < -max_negative_drift:
                stop_reason = "negative_level_drift"
                error = f"Added liters {added_liters:.2f} below allowed drift"
                break

            no_progress_timeout = float(settings["no_fill_progress_timeout_seconds"])
            min_progress = float(settings["min_fill_progress_liters"])

            if elapsed >= no_progress_timeout and added_liters < min_progress:
                stop_reason = "no_fill_progress_timeout"
                error = (
                    f"No plausible fill progress. "
                    f"Added={added_liters:.2f} L after {elapsed:.1f}s"
                )
                break

            max_fill_seconds = float(settings["max_fill_seconds"])
            if elapsed >= max_fill_seconds:
                stop_reason = "max_fill_timeout"
                error = (
                    f"Max fill timeout reached. "
                    f"Added={added_liters:.2f}/{target_delta:.2f} L after {elapsed:.1f}s"
                )
                break

            if added_liters >= target_delta:
                target_confirm_count += 1
            else:
                target_confirm_count = 0

            required_confirm = int(settings.get("target_reached_confirm_samples", 3))

            print(
                f"[FILL] Mixer={final_liters:.2f} L | "
                f"Added={added_liters:.2f}/{target_delta:.2f} L | "
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
                    "target_delta_liters": target_delta,
                    "elapsed_seconds": round(elapsed, 1),
                    "target_confirm_count": target_confirm_count,
                    "stop_mode": "sensor_or_timeout",
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
                target_delta_liters=target_delta,
            )

            if target_confirm_count >= required_confirm:
                stop_reason = "target_reached_by_sensor"
                error = None
                break

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
            "mixer_liters_filtered": final_liters,
            "added_liters": added_liters,
            "target_delta_liters": target_delta,
            "elapsed_seconds": round(elapsed, 1),
            "stop_mode": "sensor_or_timeout",
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
        target_delta_liters=target_delta,
    )

    return PhaseResult(
        success=(stop_reason == "target_reached_by_sensor"),
        stop_reason=stop_reason or "unknown",
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=added_liters,
    )


def run_flush_phase(settings, sensor_reader, actuators, mqtt_publisher, logger) -> PhaseResult:
    print()
    print("[PHASE] FLUSH_DRAIN_TIMED")
    print("[INFO] Sensor wird nur geloggt und NICHT als Stop-Bedingung verwendet.")
    print("[INFO] Opening Valve_0_Drain before starting transfer pump.")

    flush_seconds = float(settings.get("flush_seconds", 30.0))
    level_history = make_level_history(settings)

    drain_valve = actuators.get("drain_valve_0")
    transfer_pump = actuators.get("transfer_pump")

    start_metrics = read_metrics(sensor_reader, settings, level_history)
    start_liters = start_metrics.mixer_liters_filtered

    start_time = time.monotonic()
    stop_reason = "flush_time_completed"
    error = None
    final_liters = start_liters
    drained_liters = None

    try:
        drain_valve.on()
        time.sleep(float(settings.get("valve_settle_seconds", 1.0)))

        transfer_pump.on()

        publish_process_status(
            mqtt_publisher,
            "FLUSH_RUNNING",
            actuators,
            details={
                "flush_seconds": flush_seconds,
                "sensor_used_for_stop": False,
            },
        )

        while True:
            elapsed = time.monotonic() - start_time
            metrics = read_metrics(sensor_reader, settings, level_history)

            final_liters = metrics.mixer_liters_filtered

            if start_liters is not None and final_liters is not None:
                drained_liters = start_liters - final_liters

            print(
                f"[FLUSH] elapsed={elapsed:.1f}/{flush_seconds:.1f}s | "
                f"Mixer(sensor_filtered)={final_liters} | "
                f"Drained(sensor_calc)={drained_liters}"
            )

            log_step(
                logger=logger,
                phase="FLUSH_RUNNING",
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

            publish_process_status(
                mqtt_publisher,
                "FLUSH_RUNNING",
                actuators,
                details={
                    "elapsed_seconds": round(elapsed, 1),
                    "flush_seconds": flush_seconds,
                    "sensor_used_for_stop": False,
                    "mixer_liters_filtered": final_liters,
                    "drained_liters_sensor_calc": drained_liters,
                },
            )

            if elapsed >= flush_seconds:
                break

            time.sleep(0.5)

    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
        error = "KeyboardInterrupt"
        print("\n[ABORT] Flush durch Benutzer abgebrochen.")

    finally:
        transfer_pump.off()
        drain_valve.off()

    elapsed = time.monotonic() - start_time
    metrics = read_metrics(sensor_reader, settings, level_history)

    final_liters = metrics.mixer_liters_filtered
    if start_liters is not None and final_liters is not None:
        drained_liters = start_liters - final_liters

    print(f"[FLUSH STOP] reason={stop_reason}")
    print("[SAFE] Transfer pump OFF, Drain valve OFF")

    publish_process_status(
        mqtt_publisher,
        "FLUSH_STOPPED",
        actuators,
        error=error,
        details={
            "stop_reason": stop_reason,
            "elapsed_seconds": round(elapsed, 1),
            "sensor_used_for_stop": False,
            "mixer_liters_filtered": final_liters,
            "drained_liters_sensor_calc": drained_liters,
        },
    )

    log_step(
        logger=logger,
        phase="FLUSH_STOPPED",
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
        success=(stop_reason == "flush_time_completed"),
        stop_reason=stop_reason,
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=drained_liters,
    )


def main():
    print("CDS Refill and Flush Hardware Test")
    print("==================================")
    print()
    print("Ablauf:")
    print("1. RO Refill Pump füllt den Mixing Tank um Delta-Zielmenge.")
    print("2. Stopgrund wird geloggt: Sensorziel oder Timeout/Sicherheitsfehler.")
    print("3. Danach wird gefragt, ob zeitbasiert über Transferpumpe + Valve_0_Drain geflusht wird.")
    print("4. Beim Flush wird der Sensor nur geloggt und NICHT als Stop-Bedingung verwendet.")
    print()
    print("Wichtig:")
    print("- RO-Leitung muss direkt in den Mixing Tank führen.")
    print("- Transferpumpe hängt an transfer_pump.")
    print("- Drain/Flush läuft über Valve_0_Drain.")
    print("- Keine Chemie, kein Routing zu Solution Tanks.")
    print()

    settings = load_settings()

    print("Settings:")
    print(f"- target_fill_delta_liters: {settings.get('target_fill_delta_liters')}")
    print(f"- max_fill_seconds: {settings.get('max_fill_seconds')}")
    print(f"- flush_seconds: {settings.get('flush_seconds')}")
    print(f"- hardware_execution_enabled: {settings.get('hardware_execution_enabled')}")
    print(f"- mixer_sensor_liter_factor: {settings.get('mixer_sensor_liter_factor')}")
    print(f"- mixer_sensor_liter_offset: {settings.get('mixer_sensor_liter_offset')}")
    print(f"- mixer_sensor_calibration_status: {settings.get('mixer_sensor_calibration_status')}")
    print()

    confirm = input("Fortfahren? ja/nein: ").strip().lower()
    if confirm != "ja":
        print("Abgebrochen.")
        return

    if not require_hardware_confirmation(settings):
        return

    sensor_reader = None
    mqtt_publisher = None
    logger = None
    actuators = None

    try:
        sensor_reader = SensorSnapshotReader()
        mqtt_publisher = MqttPublisher()
        logger = ProcessRunLogger(process_name="refill_and_flush_test")
        actuators = ActuatorManager(active_low=ACTIVE_LOW)

        sensor_reader.start()

        print("[INFO] Warte auf erstes Sensor-MQTT-Payload...")
        if not sensor_reader.wait_for_first_snapshot(timeout_seconds=5.0):
            print("[ERROR] Kein Sensor-Payload empfangen. Abbruch.")
            return

        print("[OK] Sensor-Payload empfangen.")

        actuators.add(
            name="mixer_refill_pump",
            gpio_pin=OUTPUTS["mixer_refill_pump"],
        )

        actuators.add(
            name="transfer_pump",
            gpio_pin=OUTPUTS["transfer_pump"],
        )

        actuators.add(
            name="drain_valve_0",
            gpio_pin=OUTPUTS["valve_0_drain"],
        )

        fill_result = run_fill_phase(
            settings=settings,
            sensor_reader=sensor_reader,
            actuators=actuators,
            mqtt_publisher=mqtt_publisher,
            logger=logger,
        )

        print()
        print(f"[FILL RESULT] success={fill_result.success}, reason={fill_result.stop_reason}")

        flush_result = None

        if confirm_flush(settings):
            flush_result = run_flush_phase(
                settings=settings,
                sensor_reader=sensor_reader,
                actuators=actuators,
                mqtt_publisher=mqtt_publisher,
                logger=logger,
            )

            print()
            print(f"[FLUSH RESULT] success={flush_result.success}, reason={flush_result.stop_reason}")

        publish_process_status(
            mqtt_publisher,
            "REFILL_FLUSH_TEST_FINISHED",
            actuators,
            details={
                "fill_success": fill_result.success,
                "fill_stop_reason": fill_result.stop_reason,
                "flush_success": flush_result.success if flush_result else None,
                "flush_stop_reason": flush_result.stop_reason if flush_result else None,
            },
        )

    except KeyboardInterrupt:
        print("\n[ABORT] KeyboardInterrupt.")

        if mqtt_publisher is not None and actuators is not None:
            publish_process_status(
                mqtt_publisher,
                "ERROR",
                actuators,
                error="KeyboardInterrupt",
            )

    finally:
        print("[SAFE] Shutdown all actuators.")

        if actuators is not None:
            actuators.safe_shutdown_all()

        if mqtt_publisher is not None and actuators is not None:
            publish_process_status(
                mqtt_publisher,
                "SAFE_SHUTDOWN",
                actuators,
            )

        if actuators is not None:
            actuators.close_all()

        if sensor_reader is not None:
            sensor_reader.close()

        if mqtt_publisher is not None:
            mqtt_publisher.close()

        if logger is not None:
            logger.close()

    print("[END] Refill and flush hardware test finished.")


if __name__ == "__main__":
    main()