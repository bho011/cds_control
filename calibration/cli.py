"""CLI-Einstiegspunkt: --analyze (hardwarefrei) oder eine Live-Kalibrier-Session."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .analysis import analyze_csv_files
from .session import run_calibration


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
