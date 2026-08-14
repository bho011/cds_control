"""'discover'-Unterbefehl: verfügbare serielle Ports auflisten.

Öffnet keinen Port, bewegt keine Pumpe.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DiscoveredPort:
    device: str
    by_id_path: str | None
    description: str


def discover_ports() -> list[DiscoveredPort]:
    """Listet verfügbare serielle Ports auf, bevorzugt stabile
    /dev/serial/by-id-Pfade. Öffnet keinen Port, bewegt keine Pumpe."""
    import serial.tools.list_ports

    by_id_map: dict[str, str] = {}
    by_id_dir = Path("/dev/serial/by-id")
    if by_id_dir.is_dir():
        for entry in sorted(by_id_dir.iterdir()):
            try:
                resolved = os.path.realpath(str(entry))
            except OSError:
                continue
            by_id_map[resolved] = str(entry)

    discovered: list[DiscoveredPort] = []
    for info in serial.tools.list_ports.comports():
        resolved_device = os.path.realpath(info.device)
        discovered.append(
            DiscoveredPort(device=info.device, by_id_path=by_id_map.get(resolved_device), description=info.description or "")
        )
    return discovered


def cmd_discover(args: argparse.Namespace) -> int:
    ports = discover_ports()
    if not ports:
        print("Keine seriellen Ports gefunden.")
        return 0

    print("Gefundene serielle Ports:")
    for port in ports:
        stable = port.by_id_path or "(kein /dev/serial/by-id-Eintrag - Pfad ist NICHT stabil)"
        print(f"  {port.device}  [{port.description}]")
        print(f"    stabiler Pfad: {stable}")
    print()
    print("Hinweis: 'discover' führt keine Pumpenbewegung aus und öffnet keine Verbindung.")
    return 0
