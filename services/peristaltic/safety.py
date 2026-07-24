"""
Gemeinsame Sicherheits-Hilfsfunktionen für jede hardwareberührende
Peristaltik-Aktion (test/calibrate/prime/pair-test/all-four-test) - sowohl
für scripts/peristaltic_calibration_cli.py als auch für
process/pump_prime.py (Dashboard-Anbindung).

Bewusst getrennt von services/peristaltic/prime.py: dieses Modul enthält
nichts Priming-Spezifisches, sondern die STATUS/STOPALL-Vorabprüfung und das
best-effort-Stoppen, die vor bzw. nach JEDER Pumpenbewegung gelten.
"""

from __future__ import annotations

from services.peristaltic.serial_client import ConnectionState, PeristalticSerialClient


class SafetyCheckFailed(RuntimeError):
    """Interne Sicherheitsinvariante verletzt (z.B. nicht alle Pumpen sind
    nach STOPALL tatsächlich IDLE) - kein Firmwarefehler, aber genauso ein
    Grund, die Dosis nicht zu erlauben."""


def ensure_all_idle_before_dose(client: PeristalticSerialClient) -> dict[str, str]:
    """STATUS abfragen -> STOPALL senden (unconditional) -> STATUS erneut
    prüfen -> nur wenn danach alle 4 Pumpen IDLE sind, darf dosiert werden."""
    client.status()
    client.stop_all()
    final_status = client.status()
    if any(state != "IDLE" for state in final_status.values()):
        raise SafetyCheckFailed(f"Nach STOPALL sind nicht alle Pumpen IDLE: {final_status}")
    return final_status


def best_effort_stop_all(client: PeristalticSerialClient) -> None:
    try:
        if client.state == ConnectionState.OPEN:
            client.stop_all()
    except Exception:
        pass
