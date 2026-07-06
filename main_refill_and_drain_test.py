import json
import select
import sys
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


def confirm_sensor_pump(settings: dict) -> bool:
    if not settings.get("enable_sensor_pump_phase", True):
        return False

    answer = input("Sensorpumpe laufen lassen? ja/nein: ").strip().lower()

    if answer != "ja":
        print("[INFO] Sensorpumpenphase übersprungen.")
        return False

    return True


def confirm_drain(settings: dict) -> bool:
    answer = input("Drain über Transferpumpe + Valve_0_Drain starten? ja/nein: ").strip().lower()

    if answer != "ja":
        print("[INFO] Drain wurde nicht bestätigt. Wasser bleibt im Mixing Tank.")
        return False

    required_text = settings.get("required_drain_confirmation_text", "drain_confirmed")

    print()
    print("Drain-Sicherheitsbestätigung erforderlich.")
    print(f"Zum Drain exakt eingeben: {required_text}")
    confirmation = input("Drain-Bestätigung: ").strip()

    if confirmation != required_text:
        print("[BLOCKED] Drain-Bestätigung falsch. Kein Drain.")
        return False

    return True


def make_level_history(settings: dict):
    samples = int(settings.get("level_filter_samples", 5))
    return deque(maxlen=max(1, samples))


def mixer_liters_from_snapshot(snapshot: dict[str, Any], settings: dict) -> float | None:
    mixer = snapshot.get("mixer") or {}

    value = mixer.get("volume_liters_calc")
    if value is not None:
        return float(value)

    raw_liters = mixer.get("volume_liters_raw")
    if raw_liters is not None:
        factor = float(settings.get("mixer_sensor_liter_factor", 0.1))
        offset = float(settings.get("mixer_sensor_liter_offset", 0.0))
        return max(0.0, (float(raw_liters) * factor) + offset)

    level_percent = mixer.get("level_percent")
    if level_percent is not None:
        max_liters = float(settings["max_mixer_liters"])
        return (float(level_percent) / 100.0) * max_liters

    return None


def ro_liters_from_snapshot(snapshot: dict[str, Any]) -> float | None:
    ro = snapshot.get("ro") or {}

    value = ro.get("volume_liters_calc")
    if value is not None:
        return float(value)

    level_percent = ro.get("level_percent")
    configured_max = ro.get("configured_max_liters")

    if level_percent is not None and configured_max is not None:
        return (float(level_percent) / 100.0) * float(configured_max)

    return None


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
            "sensor_circulation_pump": status.get("sensor_circulation_pump", False),
            "supply_valve_6": status.get("supply_valve_6"),
            "mixing_circulation_pump": status.get("mixing_circulation_pump"),
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

            max_negative_drift = float(settings.get("max_negative_level_drift_liters", 3.0))
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
                    f"Mixer={final_liters:.2f}/{target_total:.2f} L after {elapsed:.1f}s"
                )
                break

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

    except KeyboardInterrupt:
        stop_reason = "sensor_pump_keyboard_interrupt"
        error = "KeyboardInterrupt"
        print("\n[ABORT] Sensorpumpenphase durch Benutzer abgebrochen.")

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
        success=(stop_reason != "sensor_pump_keyboard_interrupt"),
        stop_reason=stop_reason,
        start_liters=start_liters,
        end_liters=final_liters,
        delta_liters=None,
    )


def run_drain_phase(settings, sensor_reader, actuators, mqtt_publisher, logger) -> PhaseResult:
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
    max_drain_seconds = expected_seconds + buffer_seconds

    no_progress_warning_seconds = float(settings.get("no_drain_progress_warning_seconds", 60.0))

    print(f"[DRAIN START] Mixer={start_liters:.2f} L")
    print(f"[DRAIN CALC] Transfer pump={pump_lpm:.2f} L/min")
    print(f"[DRAIN CALC] Expected drain time={expected_seconds:.1f}s")
    print(f"[DRAIN CALC] Max runtime with buffer={max_drain_seconds:.1f}s")
    print(f"[DRAIN TARGET] Empty threshold <= {empty_threshold:.2f} L for {empty_confirm_samples} samples")

    drain_valve = actuators.get("drain_valve_0")
    transfer_pump = actuators.get("transfer_pump")

    drain_start_time = time.monotonic()
    stop_reason = None
    error = None
    final_liters = start_liters
    drained_liters = 0.0
    empty_confirm_count = 0
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

            if metrics.mixer_liters_filtered is not None:
                final_liters = metrics.mixer_liters_filtered
                drained_liters = start_liters - final_liters

                if final_liters <= empty_threshold:
                    empty_confirm_count += 1
                else:
                    empty_confirm_count = 0
            else:
                final_liters = None
                empty_confirm_count = 0

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
                f"empty_confirm={empty_confirm_count}/{empty_confirm_samples}"
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
                    "empty_confirm_count": empty_confirm_count,
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

            if empty_confirm_count >= empty_confirm_samples:
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


def main():
    print("CDS Refill / Sensor Pump / Drain Hardware Test")
    print("==============================================")
    print()
    print("Ablauf:")
    print("1. RO Refill Pump füllt den Mixing Tank bis zum absoluten Zielwert.")
    print("2. Optional läuft die Sensorpumpe über contactor_2.")
    print("3. Sensorpumpenphase kann mit 'stop' + Enter beendet werden.")
    print("4. Danach wird gefragt, ob über Transferpumpe + Valve_0_Drain geleert wird.")
    print()
    print("Wichtig:")
    print("- RO-Leitung muss direkt in den Mixing Tank führen.")
    print("- Sensorpumpe hängt an contactor_2.")
    print("- Transferpumpe hängt an transfer_pump.")
    print("- Drain läuft über Valve_0_Drain.")
    print("- Keine Chemie, kein Routing zu Solution Tanks.")
    print()

    settings = load_settings()

    print("Settings:")
    print(f"- target_fill_total_liters: {settings.get('target_fill_total_liters')}")
    print(f"- max_fill_seconds: {settings.get('max_fill_seconds')}")
    print(f"- sensor_pump_seconds: {settings.get('sensor_pump_seconds')}")
    print(f"- transfer_pump_liters_per_minute: {settings.get('transfer_pump_liters_per_minute')}")
    print(f"- drain_timeout_buffer_seconds: {settings.get('drain_timeout_buffer_seconds')}")
    print(f"- empty_threshold_liters: {settings.get('empty_threshold_liters')}")
    print(f"- hardware_execution_enabled: {settings.get('hardware_execution_enabled')}")
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
        logger = ProcessRunLogger(process_name="refill_sensor_drain_test")
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
            name="sensor_circulation_pump",
            gpio_pin=OUTPUTS["contactor_2"],
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

        if not fill_result.success:
            print()
            print("[ABORT] Water-cycle wird nach fehlgeschlagener Fill-Phase beendet.")

            publish_process_status(
                mqtt_publisher,
                "WATER_CYCLE_ABORTED",
                actuators,
                error=fill_result.stop_reason,
                details={
                    "abort_phase": "fill",
                    "fill_success": fill_result.success,
                    "fill_stop_reason": fill_result.stop_reason,
                },
            )
            return

        sensor_result = None

        if confirm_sensor_pump(settings):
            sensor_result = run_sensor_pump_phase(
                settings=settings,
                sensor_reader=sensor_reader,
                actuators=actuators,
                mqtt_publisher=mqtt_publisher,
                logger=logger,
            )

            print()
            print(f"[SENSOR RESULT] success={sensor_result.success}, reason={sensor_result.stop_reason}")

            if not sensor_result.success:
                print()
                print("[ABORT] Water-cycle wird nach fehlgeschlagener Sensorpumpenphase beendet.")

                publish_process_status(
                    mqtt_publisher,
                    "WATER_CYCLE_ABORTED",
                    actuators,
                    error=sensor_result.stop_reason,
                    details={
                        "abort_phase": "sensor_circulation",
                        "sensor_success": sensor_result.success,
                        "sensor_stop_reason": sensor_result.stop_reason,
                    },
                )
                return

        drain_result = None

        if confirm_drain(settings):
            drain_result = run_drain_phase(
                settings=settings,
                sensor_reader=sensor_reader,
                actuators=actuators,
                mqtt_publisher=mqtt_publisher,
                logger=logger,
            )

            print()
            print(f"[DRAIN RESULT] success={drain_result.success}, reason={drain_result.stop_reason}")

        publish_process_status(
            mqtt_publisher,
            "REFILL_SENSOR_DRAIN_TEST_FINISHED",
            actuators,
            details={
                "fill_success": fill_result.success,
                "fill_stop_reason": fill_result.stop_reason,
                "sensor_success": sensor_result.success if sensor_result else None,
                "sensor_stop_reason": sensor_result.stop_reason if sensor_result else None,
                "drain_success": drain_result.success if drain_result else None,
                "drain_stop_reason": drain_result.stop_reason if drain_result else None,
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

    print("[END] Refill / Sensor Pump / Drain hardware test finished.")


if __name__ == "__main__":
    main()