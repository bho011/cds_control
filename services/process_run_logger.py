import csv
from datetime import datetime
from pathlib import Path
from typing import Any


class ProcessRunLogger:
    def __init__(
        self,
        process_name: str,
        log_dir: str = "logs",
    ):
        self.process_name = process_name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.file_path = self.log_dir / f"{process_name}_{timestamp}.csv"

        self.fieldnames = [
            "timestamp",
            "process_name",
            "phase",
            "state",
            "stop_reason",
            "error",

            "elapsed_seconds",
            "target_delta_liters",

            "mixer_level_percent",
            "mixer_liters_payload",
            "mixer_liters_filtered",
            "start_mixer_liters",
            "added_liters",
            "drained_liters",

            "ro_level_percent",
            "ro_liters",

            "ec_ms_cm",
            "ph",
            "water_temperature",
            "dissolved_oxygen",

            "mixer_refill_pump",
            "transfer_pump",
            "supply_valve_6",
            "drain_valve_0",
            "mixing_circulation_pump",
            "sensor_circulation_pump",
        ]

        self._file = self.file_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames)
        self._writer.writeheader()
        self._file.flush()

        print(f"[LOG] Process log created: {self.file_path}")

    def write_step(
        self,
        state: str,
        error: str | None,
        snapshot: dict[str, Any] | None,
        actuator_status: dict[str, bool],
        mixer_liters_filtered: float | None = None,
        start_mixer_liters: float | None = None,
        added_liters: float | None = None,
        extra: dict[str, Any] | None = None,
    ):
        snapshot = snapshot or {}
        extra = extra or {}

        mixer = snapshot.get("mixer") or {}
        ro = snapshot.get("ro") or {}
        water_values = snapshot.get("water_values") or {}

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "process_name": self.process_name,
            "phase": extra.get("phase"),
            "state": state,
            "stop_reason": extra.get("stop_reason"),
            "error": error,

            "elapsed_seconds": extra.get("elapsed_seconds"),
            "target_delta_liters": extra.get("target_delta_liters"),

            "mixer_level_percent": mixer.get("level_percent"),
            "mixer_liters_payload": mixer.get("volume_liters_calc"),
            "mixer_liters_filtered": mixer_liters_filtered,
            "start_mixer_liters": start_mixer_liters,
            "added_liters": added_liters,
            "drained_liters": extra.get("drained_liters"),

            "ro_level_percent": ro.get("level_percent"),
            "ro_liters": ro.get("volume_liters_calc"),

            "ec_ms_cm": water_values.get("ec_ms_cm"),
            "ph": water_values.get("ph"),
            "water_temperature": water_values.get("water_temperature"),
            "dissolved_oxygen": water_values.get("dissolved_oxygen"),

            "mixer_refill_pump": actuator_status.get("mixer_refill_pump"),
            "transfer_pump": actuator_status.get("transfer_pump"),
            "supply_valve_6": actuator_status.get("supply_valve_6"),
            "drain_valve_0": actuator_status.get("drain_valve_0"),
            "mixing_circulation_pump": actuator_status.get("mixing_circulation_pump"),
            "sensor_circulation_pump": actuator_status.get("sensor_circulation_pump"),
        }

        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        self._file.flush()
        self._file.close()
        print(f"[LOG] Process log closed: {self.file_path}")