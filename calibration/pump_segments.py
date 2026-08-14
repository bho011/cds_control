"""
Pumpengesteuerte Fill-/Drain-Segmente (ab MANUAL_FILL_TARGET_L) und ihr
sekündliches Trace-Logging.

Für jedes Segment gilt dasselbe Muster: Pumpe an -> jede Sekunde Rohwert in
die Trace-CSV loggen, bis Enter gedrückt wird (Markierung erreicht) oder ein
Safety-Timeout greift -> Pumpe/Ventil aus -> anschließend wie gewohnt ein
gemittelter, settle-Time-basierter Messpunkt (capture_measurement, siehe
sensor_reading.py) für die eigentliche Kalibrierformel.
"""

from __future__ import annotations

import asyncio
import csv
import select
import sys
import time
from datetime import datetime
from pathlib import Path

from asyncua import Client

from .config import MIXER_RAW_NODE_ID, OPCUA_ENDPOINT, TRACE_LOG_INTERVAL_S
from .sensor_reading import read_node_float


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

    try:
        actuators.add(name="mixer_refill_pump", gpio_pin=OUTPUTS["mixer_refill_pump"])
        actuators.add(name="transfer_pump", gpio_pin=OUTPUTS["transfer_pump"])
        actuators.add(name="drain_valve_0", gpio_pin=OUTPUTS["valve_0_drain"])
    except Exception:
        # A partial failure here must still release the cross-process
        # hardware lock ActuatorManager.__init__() already acquired -
        # otherwise it leaks until this object is garbage-collected.
        actuators.close_all()
        raise

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
