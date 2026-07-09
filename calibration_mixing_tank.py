import argparse
import asyncio
import csv
import json
import math
import select
import sys
import statistics
import time
from dataclasses import dataclass, asdict, replace
from datetime import datetime
from pathlib import Path
from services.system_config import get_mqtt_config, get_mixer_level_calibration, get_opcua_config
from typing import Any, Optional, Sequence

from asyncua import Client


# ============================================================
# KONFIGURATION
# ============================================================


_SYSTEM_OPCUA_CONFIG = get_opcua_config()
_SYSTEM_MQTT_CONFIG = get_mqtt_config()
_SYSTEM_MIXER_CALIBRATION = get_mixer_level_calibration()

OPCUA_ENDPOINT = str(_SYSTEM_OPCUA_CONFIG["endpoint"])

# Mixing-Tank-Wasserstand / Sensorwert.
# Wichtig: Das ist der OPC-UA-Wert, den wir kalibrieren wollen.
MIXER_RAW_NODE_ID = "ns=4;s=Values.CEL1.PV_WaterLevel"

# Optionaler zusätzlicher System-Literwert.
# Nur setzen, wenn der Node wirklich bekannt ist.
MIXER_SYSTEM_LITERS_NODE_ID: Optional[str] = None

# Bis hierhin wird von Hand mit Eimer/Kanne befüllt - feine Auflösung im
# unteren, ohnehin bekannt nichtlinearen Bereich. Danach übernimmt die Pumpe.
MANUAL_FILL_TARGET_L = 30.0
DEFAULT_FILL_STEP_L = 5.0

# Ab MANUAL_FILL_TARGET_L füllt die Mixer-Refill-Pumpe automatisch: einmal
# PUMP_FILL_FIRST_STEP_L (Aufrunden auf die erste Tankmarkierung), danach in
# PUMP_FILL_STEP_L-Schritten bis PUMP_FILL_TARGET_L.
PUMP_FILL_FIRST_STEP_L = 20.0
PUMP_FILL_STEP_L = 25.0
PUMP_FILL_TARGET_L = 200.0

# Der Drain nach dem Pump-Fill läuft in denselben 25L-Markierungsschritten
# wieder bis 0 L runter, ebenfalls pumpengesteuert.
PUMP_DRAIN_STEP_L = 25.0

SAMPLES_PER_MEASUREMENT = 30
DELAY_BETWEEN_SAMPLES_S = 0.2
SETTLING_TIME_S = 10

# Wie oft (in Sekunden) während eines laufenden Pump-Segments ein
# Rohmesswert in die Trace-CSV geloggt wird.
TRACE_LOG_INTERVAL_S = 1.0

DATA_DIR = Path("calibration_data")

# Settings aus deinem bestehenden Refill/Drain-Test.
# Wird für Sicherheitsbestätigung und valve_settle_seconds genutzt.
SETTINGS_PATH = Path("config/calibration_settings.json")

# Wenn True, kann das Skript Mixer-Refill-Pumpe, Transferpumpe und
# Drainventil für die pumpengesteuerten Fill-/Drain-Segmente steuern.
# Es wird trotzdem zusätzlich abgefragt und sicherheitsbestätigt.
PUMP_CONTROL_ENABLED = True

# Safety-Timeouts pro Pump-Segment (nicht für den ganzen Fill-/Drain-Vorgang).
# Enter (sobald die Markierung erreicht ist) stoppt normalerweise früher,
# aber die Pumpe darf nie unbegrenzt laufen.
DEFAULT_CALIBRATION_FILL_MAX_SECONDS = 300.0
DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS = 120.0

# ============================================================
# AKTUELLE MQTT-BRIDGE-KALIBRIERUNG NUR ZUR DOKUMENTATION
# Diese Werte werden im Kalibrier-Skript NICHT auf den Messwert angewendet,
# sondern nur mitgeloggt und zum Vergleich berechnet.
# ============================================================

BRIDGE_MIXER_VOLUME_LITERS = float(_SYSTEM_MIXER_CALIBRATION["volume_liters"])
BRIDGE_MIXER_SENSOR_LITER_FACTOR = float(_SYSTEM_MIXER_CALIBRATION["factor"])
BRIDGE_MIXER_SENSOR_LITER_OFFSET = float(_SYSTEM_MIXER_CALIBRATION["offset"])
BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS = str(_SYSTEM_MIXER_CALIBRATION["status"])


# ============================================================
# DATENMODELLE
# ============================================================

@dataclass
class SensorStats:
    avg: float
    min: float
    max: float
    std: float
    first: float
    last: float
    drift: float
    samples: int
    raw_series: str


@dataclass
class Measurement:
    timestamp: str
    phase: str
    step: int
    manual_added_l: float
    manual_drained_l: float
    reference_volume_l: float

    sensor_raw_avg: float
    sensor_raw_min: float
    sensor_raw_max: float
    sensor_raw_std: float
    sensor_raw_first: float
    sensor_raw_last: float
    sensor_raw_drift: float
    sensor_raw_samples: int
    sensor_raw_series: str

    system_liters_avg: Optional[float]
    system_liters_min: Optional[float]
    system_liters_max: Optional[float]
    system_liters_std: Optional[float]
    system_liters_first: Optional[float]
    system_liters_last: Optional[float]
    system_liters_drift: Optional[float]
    system_liters_samples: Optional[int]
    system_liters_series: Optional[str]

    bridge_mixer_volume_liters: float
    bridge_sensor_liter_factor: float
    bridge_sensor_liter_offset: float
    bridge_sensor_calibration_status: str
    bridge_calculated_liters: float
    bridge_error_liters: float

    valid_for_fit: bool
    invalid_reason: str

    note: str


@dataclass
class LinearFitResult:
    name: str
    slope: float
    offset: float
    r2: float
    max_abs_error_l: float
    mean_abs_error_l: float
    points: int


# ============================================================
# SETTINGS
# ============================================================
#
# Keine getippten Bestätigungsphrasen/hardware_execution_enabled-Abfrage
# mehr in diesem Skript - auf ausdrücklichen Wunsch entfernt, da dieses
# Skript ohnehin nur manuell, mit anwesendem Bediener, gestartet wird.
# Der Pumpen-Schutz (Auto-Stopp-Timeout pro Segment) bleibt bestehen, siehe
# calibration_fill_max_seconds / calibration_drain_max_seconds unten.

def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        print(f"[WARN] Settings-Datei nicht gefunden: {SETTINGS_PATH}")
        print("[WARN] Nutze interne Default-Werte.")
        return {"valve_settle_seconds": 1.0}

    with SETTINGS_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


# ============================================================
# EINGABE-HILFSFUNKTIONEN
# ============================================================

def parse_float_input(text: str) -> float:
    normalized = text.strip().replace(",", ".")
    return float(normalized)


def ask_float(prompt: str, default: Optional[float] = None) -> float:
    while True:
        default_text = f" [{default}]" if default is not None else ""
        user_input = input(f"{prompt}{default_text}: ").strip()

        if user_input == "" and default is not None:
            return default

        try:
            return parse_float_input(user_input)
        except ValueError:
            print("Ungültige Eingabe. Bitte Zahl eingeben, z. B. 5 oder 5,0.")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    default_text = "J/n" if default else "j/N"

    while True:
        user_input = input(f"{prompt} [{default_text}]: ").strip().lower()

        if user_input == "":
            return default

        if user_input in ("j", "ja", "y", "yes"):
            return True

        if user_input in ("n", "nein", "no"):
            return False

        print("Bitte j oder n eingeben.")


# ============================================================
# SENSOR / OPC-UA
# ============================================================

def to_float(value) -> float:
    if value is None:
        raise ValueError("Sensorwert ist None")

    if isinstance(value, bool):
        raise ValueError(f"Sensorwert ist bool und nicht numerisch: {value}")

    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"Sensorwert kann nicht in float umgewandelt werden: {value!r}") from exc


async def read_node_float(node) -> float:
    value = await node.read_value()
    return to_float(value)


def calc_stats(values: Sequence[float]) -> SensorStats:
    if not values:
        raise ValueError("Keine gültigen Sensorwerte vorhanden.")

    avg = statistics.mean(values)
    min_value = min(values)
    max_value = max(values)
    std = statistics.stdev(values) if len(values) >= 2 else 0.0
    first = values[0]
    last = values[-1]
    drift = last - first
    raw_series = "|".join(f"{value:.6f}" for value in values)

    return SensorStats(
        avg=avg,
        min=min_value,
        max=max_value,
        std=std,
        first=first,
        last=last,
        drift=drift,
        samples=len(values),
        raw_series=raw_series,
    )


async def countdown(seconds: int, message: str) -> None:
    if seconds <= 0:
        return

    print(message)

    for remaining in range(seconds, 0, -1):
        print(f"  Messung startet in {remaining:2d} s", end="\r")
        await asyncio.sleep(1)

    print(" " * 60, end="\r")


async def collect_sensor_stats(
    client: Client,
    raw_node_id: str,
    system_liters_node_id: Optional[str],
    samples: int,
    delay_s: float,
) -> tuple[SensorStats, Optional[SensorStats]]:
    raw_node = client.get_node(raw_node_id)
    system_liters_node = client.get_node(system_liters_node_id) if system_liters_node_id else None

    raw_values: list[float] = []
    system_liters_values: list[float] = []

    print(f"Erfasse {samples} Sensorwerte...")

    for index in range(samples):
        try:
            raw_value = await read_node_float(raw_node)
            raw_values.append(raw_value)

            if system_liters_node is not None:
                system_liters_value = await read_node_float(system_liters_node)
                system_liters_values.append(system_liters_value)

            print(
                f"  Sample {index + 1:02d}/{samples}: raw={raw_value:.6f}",
                end="\r",
            )

        except Exception as exc:
            print(f"\nWarnung: Sample {index + 1} konnte nicht gelesen werden: {exc}")

        await asyncio.sleep(delay_s)

    print(" " * 100, end="\r")

    raw_stats = calc_stats(raw_values)
    system_liters_stats = calc_stats(system_liters_values) if system_liters_values else None

    return raw_stats, system_liters_stats


# ============================================================
# PUMPENSTEUERUNG (FILL + DRAIN) UND TRACE-LOGGING
# ============================================================
#
# Ab MANUAL_FILL_TARGET_L übernimmt die Hardware das Befüllen/Entleeren in
# Markierungs-Schritten (siehe build_pump_fill_checkpoints/
# build_pump_drain_checkpoints). Für jedes Segment gilt dasselbe Muster:
#   Pumpe an -> jede Sekunde Rohwert in die Trace-CSV loggen, bis Enter
#   gedrückt wird (Markierung erreicht) oder ein Safety-Timeout greift ->
#   Pumpe/Ventil aus -> anschließend wie gewohnt ein gemittelter,
#   settle-Time-basierter Messpunkt (capture_measurement) für die
#   eigentliche Kalibrierformel.

def create_calibration_pump_actuators():
    """
    Erstellt alle Aktoren für die pumpengesteuerten Kalibrier-Segmente:
    Mixer-Refill-Pumpe (Fill) sowie Transferpumpe + Drainventil (Drain).

    Die Imports sind bewusst hier drin, damit das Skript auch ohne Hardware-Umgebung
    zumindest bis zur manuellen Messung importierbar bleibt.
    """
    from gpio_config import OUTPUTS, ACTIVE_LOW
    from hardware.actuator_manager import ActuatorManager

    actuators = ActuatorManager(active_low=ACTIVE_LOW)

    actuators.add(name="mixer_refill_pump", gpio_pin=OUTPUTS["mixer_refill_pump"])
    actuators.add(name="transfer_pump", gpio_pin=OUTPUTS["transfer_pump"])
    actuators.add(name="drain_valve_0", gpio_pin=OUTPUTS["valve_0_drain"])

    return actuators


def shutdown_actuators(actuators) -> None:
    if actuators is None:
        return

    try:
        actuators.safe_shutdown_all()
    except Exception as exc:
        print(f"[WARN] safe_shutdown_all fehlgeschlagen: {exc}")

    try:
        actuators.close_all()
    except Exception as exc:
        print(f"[WARN] close_all fehlgeschlagen: {exc}")


def trace_csv_path(main_csv_path: Path) -> Path:
    return main_csv_path.with_name(main_csv_path.stem + "_trace.csv")


def open_trace_writer(path: Path):
    """
    Öffnet die Trace-CSV im Append-Modus und schreibt den Header nur, wenn
    die Datei neu/leer ist. Bleibt für die gesamte Pump-Fill-/Pump-Drain-
    Phase geöffnet, jede Zeile wird sofort geflusht (Crash-Sicherheit).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a", newline="", encoding="utf-8")
    fieldnames = ["timestamp", "phase", "segment_index", "segment_target_liters", "elapsed_seconds", "sensor_raw"]
    writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")

    if file.tell() == 0:
        writer.writeheader()

    return file, writer


def append_trace_row(
    writer: "csv.DictWriter",
    file,
    phase: str,
    segment_index: int,
    segment_target_liters: float,
    elapsed_seconds: float,
    sensor_raw: float,
) -> None:
    writer.writerow(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "phase": phase,
            "segment_index": segment_index,
            "segment_target_liters": segment_target_liters,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "sensor_raw": sensor_raw,
        }
    )
    file.flush()


def build_pump_fill_checkpoints(
    start_l: float, first_step_l: float, step_l: float, target_l: float
) -> list[float]:
    """[50, 75, 100, ...] for start=30, first_step=20, step=25, target=200."""
    if start_l >= target_l:
        return []

    checkpoints = [min(start_l + first_step_l, target_l)]
    while checkpoints[-1] < target_l:
        checkpoints.append(min(checkpoints[-1] + step_l, target_l))

    return checkpoints


def build_pump_drain_checkpoints(start_l: float, step_l: float) -> list[float]:
    """[175, 150, ..., 25, 0] for start=200, step=25."""
    checkpoints: list[float] = []
    current = start_l

    while current > 0:
        current = max(current - step_l, 0.0)
        checkpoints.append(current)

    return checkpoints


async def _pump_segment_loop(
    client: Client,
    max_segment_seconds: float,
    phase: str,
    segment_index: int,
    segment_target_liters: float,
    trace_writer: "csv.DictWriter",
    trace_file,
) -> tuple[str, float]:
    """
    Sekündliches Trace-Logging plus Enter-/Timeout-Überwachung, gemeinsam
    genutzt von Pump-Fill- und Pump-Drain-Segmenten. Schaltet selbst keine
    Aktoren - der Aufrufer schaltet sie vor dem Aufruf ein und in einem
    finally-Block danach wieder aus.
    """
    raw_node = client.get_node(MIXER_RAW_NODE_ID)
    start_time = time.monotonic()
    stop_reason = "unknown"
    last_raw = float("nan")

    while True:
        elapsed = time.monotonic() - start_time

        try:
            last_raw = await read_node_float(raw_node)
            append_trace_row(
                trace_writer, trace_file, phase, segment_index, segment_target_liters, elapsed, last_raw
            )
        except Exception as exc:
            print(f"\n[WARN] Trace-Messwert konnte nicht gelesen werden: {exc}")

        remaining = max(0.0, max_segment_seconds - elapsed)
        print(
            f"  [{phase} #{segment_index} -> {segment_target_liters:.1f}L] "
            f"läuft seit {elapsed:5.1f}s | raw={last_raw:.3f} | "
            f"Enter=stop | Auto-Stopp in {remaining:5.1f}s",
            end="\r",
        )

        if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()
            stop_reason = "stopped_by_enter"
            break

        if elapsed >= max_segment_seconds:
            stop_reason = "segment_timeout"
            break

        await asyncio.sleep(TRACE_LOG_INTERVAL_S)

    print(" " * 100, end="\r")
    total_elapsed = time.monotonic() - start_time
    print(f"[INFO] Segment beendet: {stop_reason} nach {total_elapsed:.1f}s")

    return stop_reason, total_elapsed


async def run_fill_pump_segment(
    refill_pump,
    max_segment_seconds: float,
    segment_index: int,
    segment_target_liters: float,
    trace_writer: "csv.DictWriter",
    trace_file,
) -> tuple[str, float]:
    print(f"\n[FILL CONTROL] Starte Mixer-Refill-Pumpe (Ziel ca. {segment_target_liters:.1f} L).")
    print(f"[SAFETY] Max. Segment-Laufzeit: {max_segment_seconds:.0f} s")
    print("[INFO] Enter drücken, sobald die Tank-Markierung erreicht ist.")

    refill_pump.on()
    try:
        async with Client(url=OPCUA_ENDPOINT) as client:
            return await _pump_segment_loop(
                client, max_segment_seconds, "fill", segment_index, segment_target_liters, trace_writer, trace_file
            )
    finally:
        refill_pump.off()
        print("[SAFE] Mixer-Refill-Pumpe AUS.")


async def run_drain_pump_segment(
    transfer_pump,
    drain_valve,
    max_segment_seconds: float,
    valve_settle_seconds: float,
    segment_index: int,
    segment_target_liters: float,
    trace_writer: "csv.DictWriter",
    trace_file,
) -> tuple[str, float]:
    print(f"\n[DRAIN CONTROL] Öffne Drainventil (Ziel ca. {segment_target_liters:.1f} L)...")
    print(f"[SAFETY] Max. Segment-Laufzeit: {max_segment_seconds:.0f} s")
    drain_valve.on()

    try:
        await asyncio.sleep(valve_settle_seconds)

        print("[DRAIN CONTROL] Starte Transferpumpe.")
        print("[INFO] Enter drücken, sobald die Tank-Markierung erreicht ist.")
        transfer_pump.on()

        try:
            async with Client(url=OPCUA_ENDPOINT) as client:
                return await _pump_segment_loop(
                    client, max_segment_seconds, "drain", segment_index, segment_target_liters, trace_writer, trace_file
                )
        finally:
            transfer_pump.off()
    finally:
        drain_valve.off()
        print("[SAFE] Transferpumpe AUS, Drainventil AUS.")


# ============================================================
# MESSPUNKTE / CSV
# ============================================================

def create_measurement(
    phase: str,
    step: int,
    manual_added_l: float,
    manual_drained_l: float,
    reference_volume_l: float,
    raw_stats: SensorStats,
    system_liters_stats: Optional[SensorStats],
    note: str,
) -> Measurement:
    bridge_calculated_liters = (
        raw_stats.avg * BRIDGE_MIXER_SENSOR_LITER_FACTOR
        + BRIDGE_MIXER_SENSOR_LITER_OFFSET
    )

    bridge_error_liters = bridge_calculated_liters - reference_volume_l

    return Measurement(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        phase=phase,
        step=step,
        manual_added_l=manual_added_l,
        manual_drained_l=manual_drained_l,
        reference_volume_l=reference_volume_l,

        sensor_raw_avg=raw_stats.avg,
        sensor_raw_min=raw_stats.min,
        sensor_raw_max=raw_stats.max,
        sensor_raw_std=raw_stats.std,
        sensor_raw_first=raw_stats.first,
        sensor_raw_last=raw_stats.last,
        sensor_raw_drift=raw_stats.drift,
        sensor_raw_samples=raw_stats.samples,
        sensor_raw_series=raw_stats.raw_series,

        system_liters_avg=system_liters_stats.avg if system_liters_stats else None,
        system_liters_min=system_liters_stats.min if system_liters_stats else None,
        system_liters_max=system_liters_stats.max if system_liters_stats else None,
        system_liters_std=system_liters_stats.std if system_liters_stats else None,
        system_liters_first=system_liters_stats.first if system_liters_stats else None,
        system_liters_last=system_liters_stats.last if system_liters_stats else None,
        system_liters_drift=system_liters_stats.drift if system_liters_stats else None,
        system_liters_samples=system_liters_stats.samples if system_liters_stats else None,
        system_liters_series=system_liters_stats.raw_series if system_liters_stats else None,

        bridge_mixer_volume_liters=BRIDGE_MIXER_VOLUME_LITERS,
        bridge_sensor_liter_factor=BRIDGE_MIXER_SENSOR_LITER_FACTOR,
        bridge_sensor_liter_offset=BRIDGE_MIXER_SENSOR_LITER_OFFSET,
        bridge_sensor_calibration_status=BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS,
        bridge_calculated_liters=bridge_calculated_liters,
        bridge_error_liters=bridge_error_liters,

        valid_for_fit=True,
        invalid_reason="",

        note=note,
    )


def print_measurement_summary(measurement: Measurement) -> None:
    print("\nMesspunkt gespeichert:")
    print(f"  Phase:              {measurement.phase}")
    print(f"  Referenzvolumen:    {measurement.reference_volume_l:.3f} L")
    print(f"  Sensor raw avg:     {measurement.sensor_raw_avg:.6f}")
    print(f"  Sensor raw min/max: {measurement.sensor_raw_min:.6f} / {measurement.sensor_raw_max:.6f}")
    print(f"  Sensor raw std:     {measurement.sensor_raw_std:.6f}")
    print(f"  Sensor raw drift:   {measurement.sensor_raw_drift:.6f}")
    print(f"  Bridge gerechnet:   {measurement.bridge_calculated_liters:.3f} L")
    print(f"  Bridge Fehler:      {measurement.bridge_error_liters:+.3f} L")

    if measurement.system_liters_avg is not None:
        print(f"  System Liter avg:   {measurement.system_liters_avg:.6f} L")

    print()


def ask_measurement_validity(measurement: Measurement) -> None:
    valid = ask_yes_no("Messpunkt für spätere Formel verwenden?", default=True)
    measurement.valid_for_fit = valid

    if not valid:
        reason = input("Grund für ungültigen Messpunkt: ").strip()
        measurement.invalid_reason = reason or "not specified"

        if measurement.note:
            measurement.note = f"{measurement.note} | invalid: {measurement.invalid_reason}"
        else:
            measurement.note = f"invalid: {measurement.invalid_reason}"


def save_csv(path: Path, measurements: list[Measurement]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not measurements:
        return

    fieldnames = list(asdict(measurements[0]).keys())

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for measurement in measurements:
            writer.writerow(asdict(measurement))


# ============================================================
# CSV EINLESEN (fuer --analyze, ohne Hardware)
# ============================================================

def _row_float(row: dict[str, str], key: str, default: float) -> float:
    value = (row.get(key) or "").strip()
    return float(value) if value else default


def _row_optional_float(row: dict[str, str], key: str) -> Optional[float]:
    value = (row.get(key) or "").strip()
    return float(value) if value else None


def _row_int(row: dict[str, str], key: str, default: int) -> int:
    value = (row.get(key) or "").strip()
    return int(float(value)) if value else default


def _row_optional_int(row: dict[str, str], key: str) -> Optional[int]:
    value = (row.get(key) or "").strip()
    return int(float(value)) if value else None


def _row_bool(row: dict[str, str], key: str, default: bool) -> bool:
    value = row.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() == "true"


def load_csv(path: Path) -> list[Measurement]:
    """
    Loads previously saved calibration measurements back from CSV.

    Inverse of save_csv(). Used by --analyze to re-run the fit on
    already-collected data without a live hardware session. Older CSVs
    (from before the bridge_*/valid_for_fit/invalid_reason columns existed)
    are still readable: missing columns fall back to sensible defaults and
    never block loading.
    """
    with path.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file, delimiter=";")
        rows = list(reader)

    measurements = []

    for row in rows:
        measurements.append(
            Measurement(
                timestamp=row.get("timestamp", ""),
                phase=row.get("phase", ""),
                step=_row_int(row, "step", 0),
                manual_added_l=_row_float(row, "manual_added_l", 0.0),
                manual_drained_l=_row_float(row, "manual_drained_l", 0.0),
                reference_volume_l=_row_float(row, "reference_volume_l", 0.0),

                sensor_raw_avg=_row_float(row, "sensor_raw_avg", 0.0),
                sensor_raw_min=_row_float(row, "sensor_raw_min", 0.0),
                sensor_raw_max=_row_float(row, "sensor_raw_max", 0.0),
                sensor_raw_std=_row_float(row, "sensor_raw_std", 0.0),
                sensor_raw_first=_row_float(row, "sensor_raw_first", 0.0),
                sensor_raw_last=_row_float(row, "sensor_raw_last", 0.0),
                sensor_raw_drift=_row_float(row, "sensor_raw_drift", 0.0),
                sensor_raw_samples=_row_int(row, "sensor_raw_samples", 0),
                sensor_raw_series=row.get("sensor_raw_series", ""),

                system_liters_avg=_row_optional_float(row, "system_liters_avg"),
                system_liters_min=_row_optional_float(row, "system_liters_min"),
                system_liters_max=_row_optional_float(row, "system_liters_max"),
                system_liters_std=_row_optional_float(row, "system_liters_std"),
                system_liters_first=_row_optional_float(row, "system_liters_first"),
                system_liters_last=_row_optional_float(row, "system_liters_last"),
                system_liters_drift=_row_optional_float(row, "system_liters_drift"),
                system_liters_samples=_row_optional_int(row, "system_liters_samples"),
                system_liters_series=row.get("system_liters_series") or None,

                bridge_mixer_volume_liters=_row_float(
                    row, "bridge_mixer_volume_liters", BRIDGE_MIXER_VOLUME_LITERS
                ),
                bridge_sensor_liter_factor=_row_float(
                    row, "bridge_sensor_liter_factor", BRIDGE_MIXER_SENSOR_LITER_FACTOR
                ),
                bridge_sensor_liter_offset=_row_float(
                    row, "bridge_sensor_liter_offset", BRIDGE_MIXER_SENSOR_LITER_OFFSET
                ),
                bridge_sensor_calibration_status=row.get(
                    "bridge_sensor_calibration_status", BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS
                ),
                bridge_calculated_liters=_row_float(row, "bridge_calculated_liters", 0.0),
                bridge_error_liters=_row_float(row, "bridge_error_liters", 0.0),

                valid_for_fit=_row_bool(row, "valid_for_fit", True),
                invalid_reason=row.get("invalid_reason", "") or "",

                note=row.get("note", "") or "",
            )
        )

    return measurements


async def capture_measurement(
    phase: str,
    step: int,
    manual_added_l: float,
    manual_drained_l: float,
    reference_volume_l: float,
    note: str,
) -> Measurement:
    await countdown(
        SETTLING_TIME_S,
        f"Beruhigungszeit: {SETTLING_TIME_S} Sekunden warten, damit sich der Wasserstand stabilisiert.",
    )

    # OPC-UA-Verbindung pro Messpunkt neu öffnen.
    # Dadurch läuft die Session während manueller Eingaben nicht ab.
    async with Client(url=OPCUA_ENDPOINT) as client:
        raw_stats, system_liters_stats = await collect_sensor_stats(
            client=client,
            raw_node_id=MIXER_RAW_NODE_ID,
            system_liters_node_id=MIXER_SYSTEM_LITERS_NODE_ID,
            samples=SAMPLES_PER_MEASUREMENT,
            delay_s=DELAY_BETWEEN_SAMPLES_S,
        )

    measurement = create_measurement(
        phase=phase,
        step=step,
        manual_added_l=manual_added_l,
        manual_drained_l=manual_drained_l,
        reference_volume_l=reference_volume_l,
        raw_stats=raw_stats,
        system_liters_stats=system_liters_stats,
        note=note,
    )

    print_measurement_summary(measurement)
    ask_measurement_validity(measurement)

    return measurement


# ============================================================
# LINEARE AUSWERTUNG
# ============================================================

def linear_regression(xs: Sequence[float], ys: Sequence[float], name: str) -> Optional[LinearFitResult]:
    if len(xs) < 2 or len(ys) < 2:
        return None

    if len(xs) != len(ys):
        raise ValueError("xs und ys müssen gleich lang sein.")

    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)

    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))

    if math.isclose(ss_xx, 0.0):
        return None

    slope = ss_xy / ss_xx
    offset = y_mean - slope * x_mean

    predictions = [slope * x + offset for x in xs]
    errors = [prediction - y for prediction, y in zip(predictions, ys)]

    ss_res = sum(error ** 2 for error in errors)
    ss_tot = sum((y - y_mean) ** 2 for y in ys)

    r2 = 1.0 - (ss_res / ss_tot) if not math.isclose(ss_tot, 0.0) else 1.0

    abs_errors = [abs(error) for error in errors]

    return LinearFitResult(
        name=name,
        slope=slope,
        offset=offset,
        r2=r2,
        max_abs_error_l=max(abs_errors),
        mean_abs_error_l=statistics.mean(abs_errors),
        points=len(xs),
    )


def build_fit(
    measurements: list[Measurement],
    phases: Optional[set[str]],
    name: str,
    min_reference_volume_l: float = 0.0,
) -> Optional[LinearFitResult]:
    selected = []

    for measurement in measurements:
        if phases is not None and measurement.phase not in phases:
            continue

        if not measurement.valid_for_fit:
            continue

        if measurement.reference_volume_l < min_reference_volume_l:
            continue

        if not math.isfinite(measurement.sensor_raw_avg):
            continue

        selected.append(measurement)

    xs = [measurement.sensor_raw_avg for measurement in selected]
    ys = [measurement.reference_volume_l for measurement in selected]

    return linear_regression(xs, ys, name)


# ============================================================
# MEHRERE CSVs GEMEINSAM AUSWERTEN (--analyze)
# ============================================================

def find_zero_raw(measurements: list[Measurement]) -> Optional[float]:
    """Average raw sensor value at this session's empty-tank zero point(s), if any."""
    zero_values = [
        measurement.sensor_raw_avg
        for measurement in measurements
        if measurement.phase in ("zero", "zero_after_drain") and measurement.valid_for_fit
    ]
    return statistics.mean(zero_values) if zero_values else None


def zero_normalize(measurements: list[Measurement], zero_raw: float) -> list[Measurement]:
    """
    Returns copies of the measurements with sensor_raw_avg shifted so this
    session's own zero point sits at 0. Lets fits pool raw values across
    calibration sessions whose absolute zero-point drifted (e.g. between
    two runs on different days), without touching the saved CSVs.
    """
    return [replace(measurement, sensor_raw_avg=measurement.sensor_raw_avg - zero_raw) for measurement in measurements]


def check_monotonic(measurements: list[Measurement], phase: str) -> list[str]:
    """
    Advisory warnings (not auto-exclusion) for points where the raw sensor
    value didn't increase even though reference_volume_l did, within one
    phase, in step order. A real hydrostatic sensor should be monotonic
    while filling or draining; a dip usually means a data entry error, a
    splash, or a sensor glitch worth reviewing by hand.
    """
    warnings: list[str] = []
    ordered = sorted(
        (measurement for measurement in measurements if measurement.phase == phase),
        key=lambda measurement: measurement.step,
    )

    for previous, current in zip(ordered, ordered[1:]):
        if current.reference_volume_l > previous.reference_volume_l and current.sensor_raw_avg <= previous.sensor_raw_avg:
            warnings.append(
                f"  [WARN] nicht-monoton in phase={phase}: step {previous.step}->{current.step}, "
                f"ref {previous.reference_volume_l:.1f}L->{current.reference_volume_l:.1f}L, "
                f"raw {previous.sensor_raw_avg:.3f}->{current.sensor_raw_avg:.3f}"
            )

    return warnings


def analyze_csv_files(paths: list[Path]) -> None:
    """
    Offline analysis of already-collected calibration CSVs. Never touches
    GPIO, OPC-UA, or any actuator - pure CSV parsing plus the same
    regression math used at the end of a live session.
    """
    print("============================================================")
    print("Kalibrier-Auswertung (offline, aus gespeicherten CSV-Dateien)")
    print("============================================================")

    raw_measurements: list[Measurement] = []
    normalized_measurements: list[Measurement] = []
    zero_points: dict[str, float] = {}

    for path in paths:
        measurements = load_csv(path)
        print(f"\n{path.name}: {len(measurements)} Messpunkte geladen.")

        for phase in ("fill", "drain"):
            for warning in check_monotonic(measurements, phase):
                print(warning)

        zero_raw = find_zero_raw(measurements)
        if zero_raw is not None:
            zero_points[path.name] = zero_raw
            normalized_measurements.extend(zero_normalize(measurements, zero_raw))
        else:
            print(f"  [WARN] Kein Nullpunkt in {path.name} gefunden, wird nicht zero-normalisiert.")
            normalized_measurements.extend(measurements)

        raw_measurements.extend(measurements)

    if len(zero_points) >= 2:
        print("\nNullpunkt-Vergleich zwischen Sessions (sollte bei stabilem Sensor nahe 0 liegen):")
        names = list(zero_points)
        for name in names:
            print(f"  {name}: raw={zero_points[name]:.3f}")
        for a, b in zip(names, names[1:]):
            print(f"  Delta {a} -> {b}: {zero_points[b] - zero_points[a]:+.3f}")

    print("\n--- Unveraenderte Rohdaten, wie am Ende einer Live-Session ---")
    analyze_measurements(raw_measurements)

    print("\n--- Zero-normalisiert und ueber alle Dateien gepoolt ---")
    print_fit_result(
        build_fit(normalized_measurements, phases={"zero", "fill"}, name="Zero-normalisiert, FILL, alle Dateien")
    )
    print_fit_result(
        build_fit(
            normalized_measurements,
            phases={"zero", "fill"},
            name="Zero-normalisiert, FILL, alle Dateien, ref >= 10L",
            min_reference_volume_l=10.0,
        )
    )
    print_fit_result(
        build_fit(
            normalized_measurements,
            phases=None,
            name="Zero-normalisiert, FILL+DRAIN, alle Dateien, ref >= 10L",
            min_reference_volume_l=10.0,
        )
    )


def print_fit_result(result: Optional[LinearFitResult]) -> None:
    if result is None:
        print("Keine ausreichenden Daten für diese Auswertung vorhanden.")
        return

    print(f"\n{result.name}")
    print("-" * len(result.name))
    print(f"Messpunkte:             {result.points}")
    print("Formel:")
    print(f"  Liter_real = {result.slope:.6f} * sensor_raw + {result.offset:.6f}")
    print(f"R² Linearität:          {result.r2:.6f}")
    print(f"Max. Fehler:            {result.max_abs_error_l:.3f} L")
    print(f"Mittlerer Fehler:       {result.mean_abs_error_l:.3f} L")

    if result.r2 >= 0.995 and result.max_abs_error_l <= 1.0:
        print("Bewertung:              sehr gut linear")
    elif result.r2 >= 0.985 and result.max_abs_error_l <= 2.0:
        print("Bewertung:              brauchbar linear")
    else:
        print("Bewertung:              auffällig - Interpolation/Kalibriertabelle prüfen")


def print_invalid_measurements(measurements: list[Measurement]) -> None:
    invalid = [measurement for measurement in measurements if not measurement.valid_for_fit]

    if not invalid:
        return

    print("\nAusgeschlossene Messpunkte:")
    for measurement in invalid:
        print(
            f"  phase={measurement.phase}, step={measurement.step}, "
            f"ref={measurement.reference_volume_l:.3f} L, "
            f"raw={measurement.sensor_raw_avg:.6f}, "
            f"reason={measurement.invalid_reason}"
        )


def analyze_measurements(measurements: list[Measurement]) -> None:
    print("\n\n============================================================")
    print("AUSWERTUNG")
    print("============================================================")

    if not measurements:
        print("Keine Messpunkte vorhanden.")
        return

    fill_fit = build_fit(
        measurements,
        phases={"zero", "fill"},
        name="Lineare Kalibrierung - Fill inklusive 0-L-Punkt",
    )

    drain_fit = build_fit(
        measurements,
        phases={"drain_start", "drain", "zero_after_drain"},
        name="Lineare Kalibrierung - Drain inklusive Drain-Start/0-L-Punkt",
    )

    all_fit = build_fit(
        measurements,
        phases=None,
        name="Lineare Kalibrierung - Gesamt",
    )

    print_fit_result(fill_fit)
    print_fit_result(drain_fit)
    print_fit_result(all_fit)

    print_invalid_measurements(measurements)

    print("\nHinweis:")
    print("  Die Formeln werden nur aus Messpunkten mit valid_for_fit=True berechnet.")
    print("  Die aktuelle MQTT-Bridge-Umrechnung wurde nur dokumentiert und nicht angewendet.")
    print("  Wenn Fill und Drain deutlich abweichen, besser Interpolation/Kalibriertabelle nutzen.")


# ============================================================
# HAUPTABLAUF
# ============================================================

async def run_calibration() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = DATA_DIR / f"mixing_tank_calibration_{timestamp}.csv"
    trace_path = trace_csv_path(csv_path)

    settings = load_settings()
    measurements: list[Measurement] = []
    current_reference_volume_l = 0.0
    fill_step = 0
    drain_step = 0
    actuators = None
    pump_control_active = False
    trace_file = None
    trace_writer = None

    print("============================================================")
    print("Mixing-Tank Sensor-Kalibrierung")
    print("============================================================")
    print(f"OPC-UA Endpoint:      {OPCUA_ENDPOINT}")
    print(f"Raw Node:             {MIXER_RAW_NODE_ID}")
    print(f"System Liter Node:    {MIXER_SYSTEM_LITERS_NODE_ID}")
    print(f"Manuelles Fill bis:   {MANUAL_FILL_TARGET_L:.1f} L")
    print(
        f"Pumpen-Fill bis:      {PUMP_FILL_TARGET_L:.1f} L "
        f"(1x {PUMP_FILL_FIRST_STEP_L:.0f}L, dann {PUMP_FILL_STEP_L:.0f}L-Schritte)"
    )
    print(f"Samples/Messpunkt:    {SAMPLES_PER_MEASUREMENT}")
    print(f"CSV-Datei:            {csv_path}")
    print(f"Trace-CSV-Datei:      {trace_path}")
    print()
    print("Bridge-Dokumentation:")
    print(f"  factor:             {BRIDGE_MIXER_SENSOR_LITER_FACTOR}")
    print(f"  offset:             {BRIDGE_MIXER_SENSOR_LITER_OFFSET}")
    print(f"  status:             {BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS}")
    print("============================================================")
    print()
    print("Ablauf:")
    print("  1. Tank leer starten, Nullpunkt erfassen.")
    print(f"  2. Manuell Wasser zufügen bis {MANUAL_FILL_TARGET_L:.0f} L (Eimer/Kanne).")
    print(f"  3. Ab {MANUAL_FILL_TARGET_L:.0f} L füllt die Mixer-Refill-Pumpe automatisch,")
    print(f"     in Markierungs-Schritten bis {PUMP_FILL_TARGET_L:.0f} L. Enter stoppt die Pumpe,")
    print("     sobald die Markierung erreicht ist - jedes Segment loggt den Rohwert")
    print("     sekündlich in die Trace-CSV.")
    print(f"  4. Danach entleert dieselbe Pumpen-/Ventilkombination in {PUMP_DRAIN_STEP_L:.0f}L-")
    print("     Schritten wieder bis 0 L, ebenfalls mit sekündlichem Trace-Log.")
    print("  5. Finalen Nullpunkt nach Drain erfassen.")
    print()
    print("Wichtig:")
    print("  Kalibriereinstellungen NICHT mehr ändern.")
    print("  Bewegte/gestörte Messpunkte als ungültig markieren.")
    print()

    if not ask_yes_no("Kalibrierung starten?", default=True):
        print("Abgebrochen.")
        return

    try:
        if PUMP_CONTROL_ENABLED and ask_yes_no(
            f"Pumpengesteuerte Fill-/Drain-Segmente (ab {MANUAL_FILL_TARGET_L:.0f} L) durchführen?",
            default=True,
        ):
            try:
                actuators = create_calibration_pump_actuators()
                trace_file, trace_writer = open_trace_writer(trace_path)
                pump_control_active = True
                print("[OK] Fill-/Drain-Aktoren initialisiert.")
            except Exception as exc:
                print(f"[ERROR] Aktoren/Trace-Datei konnten nicht initialisiert werden: {exc}")
                print("[INFO] Pumpengesteuerte Segmente werden übersprungen.")
                pump_control_active = False

        # ----------------------------------------------------
        # 0-Liter-Punkt
        # ----------------------------------------------------
        print("\n------------------------------------------------------------")
        print("NULLPUNKT")
        print("------------------------------------------------------------")
        input("Tank vollständig leeren. Danach Enter drücken, um 0 L zu erfassen...")

        zero_measurement = await capture_measurement(
            phase="zero",
            step=0,
            manual_added_l=0.0,
            manual_drained_l=0.0,
            reference_volume_l=0.0,
            note="empty tank zero point",
        )

        measurements.append(zero_measurement)
        save_csv(csv_path, measurements)
        print(f"CSV aktualisiert: {csv_path}")

        # ----------------------------------------------------
        # MANUELLE FILL-PHASE (0 -> MANUAL_FILL_TARGET_L)
        # ----------------------------------------------------
        print("\n------------------------------------------------------------")
        print(f"MANUELLE FILL-PHASE (bis {MANUAL_FILL_TARGET_L:.0f} L)")
        print("------------------------------------------------------------")

        while current_reference_volume_l < MANUAL_FILL_TARGET_L:
            print(f"\nAktuelles Referenzvolumen: {current_reference_volume_l:.3f} L")
            print(f"Ziel: {MANUAL_FILL_TARGET_L:.3f} L")

            if not ask_yes_no("Weiteren Fill-Messpunkt erfassen?", default=True):
                break

            print()
            print("Bitte Wasser manuell in den Tank füllen.")
            added_l = ask_float("Wie viele Liter wurden hinzugefügt?", default=DEFAULT_FILL_STEP_L)

            if added_l <= 0:
                print("Wert muss größer als 0 sein.")
                continue

            current_reference_volume_l += added_l
            fill_step += 1

            measurement = await capture_measurement(
                phase="fill",
                step=fill_step,
                manual_added_l=added_l,
                manual_drained_l=0.0,
                reference_volume_l=current_reference_volume_l,
                note="manual fill",
            )

            measurements.append(measurement)
            save_csv(csv_path, measurements)
            print(f"CSV aktualisiert: {csv_path}")

            if current_reference_volume_l >= MANUAL_FILL_TARGET_L:
                print(f"\nManuelles Zielvolumen erreicht: {current_reference_volume_l:.3f} L")
                break

        # ----------------------------------------------------
        # PUMPEN-FILL-PHASE (MANUAL_FILL_TARGET_L -> PUMP_FILL_TARGET_L)
        # ----------------------------------------------------
        if pump_control_active and current_reference_volume_l < PUMP_FILL_TARGET_L:
            print("\n------------------------------------------------------------")
            print(f"PUMPEN-FILL-PHASE (bis {PUMP_FILL_TARGET_L:.0f} L)")
            print("------------------------------------------------------------")

            fill_checkpoints = build_pump_fill_checkpoints(
                current_reference_volume_l, PUMP_FILL_FIRST_STEP_L, PUMP_FILL_STEP_L, PUMP_FILL_TARGET_L
            )
            max_fill_segment_seconds = float(
                settings.get("calibration_fill_max_seconds", DEFAULT_CALIBRATION_FILL_MAX_SECONDS)
            )
            refill_pump = actuators.get("mixer_refill_pump")

            for checkpoint in fill_checkpoints:
                print(f"\nAktuelles Referenzvolumen: {current_reference_volume_l:.3f} L")
                print(f"Nächste Markierung: {checkpoint:.1f} L")

                if not ask_yes_no("Pump-Fill-Segment starten?", default=True):
                    break

                fill_step += 1

                await run_fill_pump_segment(
                    refill_pump=refill_pump,
                    max_segment_seconds=max_fill_segment_seconds,
                    segment_index=fill_step,
                    segment_target_liters=checkpoint,
                    trace_writer=trace_writer,
                    trace_file=trace_file,
                )

                confirmed_l = ask_float(
                    "Welchen Literwert zeigt die Markierung am Tank gerade an?",
                    default=checkpoint,
                )
                current_reference_volume_l = confirmed_l

                measurement = await capture_measurement(
                    phase="fill",
                    step=fill_step,
                    manual_added_l=0.0,
                    manual_drained_l=0.0,
                    reference_volume_l=current_reference_volume_l,
                    note=f"pump fill checkpoint, target={checkpoint:.1f}L",
                )

                measurements.append(measurement)
                save_csv(csv_path, measurements)
                print(f"CSV aktualisiert: {csv_path}")

                if current_reference_volume_l >= PUMP_FILL_TARGET_L:
                    print(f"\nPumpen-Zielvolumen erreicht: {current_reference_volume_l:.3f} L")
                    break

        # ----------------------------------------------------
        # PUMPEN-DRAIN-PHASE (aktuelles Volumen -> 0 L)
        # ----------------------------------------------------
        if pump_control_active and current_reference_volume_l > 0:
            print("\n------------------------------------------------------------")
            print("PUMPEN-DRAIN-PHASE (bis 0 L)")
            print("------------------------------------------------------------")

            drain_checkpoints = build_pump_drain_checkpoints(current_reference_volume_l, PUMP_DRAIN_STEP_L)
            max_drain_segment_seconds = float(
                settings.get("calibration_drain_max_seconds", DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS)
            )
            valve_settle_seconds = float(settings.get("valve_settle_seconds", 1.0))
            transfer_pump = actuators.get("transfer_pump")
            drain_valve = actuators.get("drain_valve_0")

            for checkpoint in drain_checkpoints:
                print(f"\nAktuelles Referenzvolumen: {current_reference_volume_l:.3f} L")
                print(f"Nächste Markierung: {checkpoint:.1f} L")

                if not ask_yes_no("Pump-Drain-Segment starten?", default=True):
                    break

                drain_step += 1

                await run_drain_pump_segment(
                    transfer_pump=transfer_pump,
                    drain_valve=drain_valve,
                    max_segment_seconds=max_drain_segment_seconds,
                    valve_settle_seconds=valve_settle_seconds,
                    segment_index=drain_step,
                    segment_target_liters=checkpoint,
                    trace_writer=trace_writer,
                    trace_file=trace_file,
                )

                confirmed_l = ask_float(
                    "Welchen Literwert zeigt die Markierung am Tank gerade an?",
                    default=checkpoint,
                )
                current_reference_volume_l = confirmed_l

                measurement = await capture_measurement(
                    phase="drain",
                    step=drain_step,
                    manual_added_l=0.0,
                    manual_drained_l=0.0,
                    reference_volume_l=current_reference_volume_l,
                    note=f"pump drain checkpoint, target={checkpoint:.1f}L",
                )

                measurements.append(measurement)
                save_csv(csv_path, measurements)
                print(f"CSV aktualisiert: {csv_path}")

                if current_reference_volume_l <= 0:
                    print("\nTank rechnerisch leer.")
                    break

        # ----------------------------------------------------
        # FINALER NULLPUNKT
        # ----------------------------------------------------
        print("\n------------------------------------------------------------")
        print("FINALER NULLPUNKT")
        print("------------------------------------------------------------")

        if ask_yes_no("Finalen 0-L-Punkt nach Drain erfassen?", default=True):
            input("Tank so gut wie möglich leeren. Danach Enter drücken...")

            final_zero = await capture_measurement(
                phase="zero_after_drain",
                step=0,
                manual_added_l=0.0,
                manual_drained_l=0.0,
                reference_volume_l=0.0,
                note="empty tank after drain",
            )

            measurements.append(final_zero)
            save_csv(csv_path, measurements)
            print(f"CSV aktualisiert: {csv_path}")

    finally:
        if actuators is not None:
            print("[SAFE] Shutdown all calibration actuators.")
            shutdown_actuators(actuators)

        if trace_file is not None:
            trace_file.close()

    analyze_measurements(measurements)

    print("\n============================================================")
    print("KALIBRIERUNG BEENDET")
    print("============================================================")
    print("CSV-Datei gespeichert unter:")
    print(f"  {csv_path}")
    if trace_writer is not None:
        print("Trace-CSV (sekündliche Rohwerte während Pump-Segmenten) unter:")
        print(f"  {trace_path}")
    print()
    print("Diese CSV bitte aufheben. Daraus können wir später")
    print("eine Kalibriertabelle, Interpolation oder finale Sensorformel ableiten.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mixing Tank Sensor-Kalibrierung")
    parser.add_argument(
        "--analyze",
        nargs="+",
        metavar="CSV",
        help=(
            "Nur bereits gespeicherte Kalibrier-CSVs auswerten und beenden. "
            "Startet keine Hardware, OPC-UA oder Live-Session."
        ),
    )
    args = parser.parse_args()

    if args.analyze:
        analyze_csv_files([Path(csv_path) for csv_path in args.analyze])
        return

    try:
        asyncio.run(run_calibration())
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer.")
    except Exception as exc:
        print("\nFehler während der Kalibrierung:")
        print(exc)


if __name__ == "__main__":
    main()
