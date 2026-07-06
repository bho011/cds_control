from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
