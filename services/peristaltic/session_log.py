"""
Pro-Programmlauf-Logger für den Peristaltik-Testclient: maschinenlesbare
CSV-Datei, rohe serielle Textdatei (jede gesendete/empfangene Zeile mit
Zeitstempel) und optionale JSON-Zusammenfassung, alle drei unter
logs/peristaltic/YYYYMMDD/.

Thread-sicher: log_raw() wird sowohl vom Hauptthread (TX) als auch vom
Seriell-Lesethread (RX) aufgerufen - ein gemeinsamer Lock schützt beide
Dateien vor verschachteltem Schreiben. Überschreibt nie eine bestehende
Datei - der Dateiname (gemeinsam für CSV/Rohlog/JSON) wird bei Kollision
mit einem hochzählenden Suffix versehen.
"""

from __future__ import annotations

import csv
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CSV_FIELDNAMES: tuple[str, ...] = (
    "timestamp_utc",
    "session_id",
    "controller",
    "controller_role",
    "serial_port",
    "pump",
    "pump_role",
    "command",
    "requested_ml",
    "measured_ml",
    "measurement_method",
    "water_temperature_c",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "result",
    "firmware_reported_ml",
    "firmware_ml_per_step_used",   # umbenannt von "current_ml_per_step" - siehe Kalibrierdatenmodell
    "candidate_ml_per_step",
    "error_code",
    "notes",
    "raw_log_file",
)


def _resolve_unique_base_name(directory: Path, base_name: str) -> str:
    """Eine einzige Basis für CSV/Rohlog/JSON, gegen alle drei Endungen
    geprüft - so bleiben alle drei Dateien einer Session garantiert unter
    demselben Namen gruppiert, statt unabhängig voneinander verschiedene
    Kollisions-Suffixe zu bekommen."""
    candidate = base_name
    counter = 1
    while (
        (directory / f"{candidate}.csv").exists()
        or (directory / f"{candidate}.log").exists()
        or (directory / f"{candidate}.json").exists()
    ):
        candidate = f"{base_name}_{counter}"
        counter += 1
    return candidate


class PeristalticSessionLogger:
    def __init__(self, command: str, log_dir: Path = Path("logs/peristaltic")) -> None:
        self.command = command
        self._lock = threading.Lock()

        now = datetime.now(timezone.utc)
        day_dir = log_dir / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        base_name = f"{command}_{now.strftime('%Y%m%dT%H%M%S')}"
        unique_base_name = _resolve_unique_base_name(day_dir, base_name)
        self.session_id = unique_base_name

        self.csv_path = day_dir / f"{unique_base_name}.csv"
        self.raw_log_path = day_dir / f"{unique_base_name}.log"
        self.json_path = day_dir / f"{unique_base_name}.json"

        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_FIELDNAMES)
        self._csv_writer.writeheader()
        self._csv_file.flush()

        self._raw_file = self.raw_log_path.open("w", encoding="utf-8")

    def log_raw(self, direction: str, line: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            self._raw_file.write(f"{timestamp} {direction} {line}\n")
            self._raw_file.flush()

    def log_row(self, **fields: Any) -> None:
        row: dict[str, Any] = dict.fromkeys(CSV_FIELDNAMES)
        row["session_id"] = self.session_id
        row["raw_log_file"] = self.raw_log_path.name
        row.update({key: value for key, value in fields.items() if key in CSV_FIELDNAMES})
        with self._lock:
            self._csv_writer.writerow(row)
            self._csv_file.flush()

    def close(self, summary: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._csv_file.flush()
            self._csv_file.close()
            self._raw_file.flush()
            self._raw_file.close()
            if summary is not None:
                with self.json_path.open("w", encoding="utf-8") as file:
                    json.dump(summary, file, indent=2, ensure_ascii=False, default=str)
                    file.write("\n")
