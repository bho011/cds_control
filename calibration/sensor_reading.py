"""Sensorwerte per OPC-UA lesen, mitteln und zu einem Messpunkt zusammenfassen."""

from __future__ import annotations

import asyncio
import statistics
from typing import Optional, Sequence

from asyncua import Client

from .config import (
    DELAY_BETWEEN_SAMPLES_S,
    MIXER_RAW_NODE_ID,
    MIXER_SYSTEM_LITERS_NODE_ID,
    OPCUA_ENDPOINT,
    SAMPLES_PER_MEASUREMENT,
    SETTLING_TIME_S,
)
from .measurement_csv import ask_measurement_validity, create_measurement, print_measurement_summary
from .models import Measurement, SensorStats


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
