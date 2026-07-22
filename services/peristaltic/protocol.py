"""
Wire-Format des Peristaltik-Seriell-Protokolls: Parsen eingehender Zeilen
und Formatieren ausgehender Befehle.

Exakt gegen docs/SERIAL_PROTOCOL.md UND den echten Firmware-Quelltext
src/main.cpp verifiziert (beides im separaten, nur lesend referenzierten
Repository ~/central_dosing_sys_peristaltic):

    OK READY                                        <- PING
    OK STATUS P1=IDLE P2=IDLE P3=IDLE P4=IDLE        <- STATUS (Zustände: IDLE/BUSY, NIE "RUNNING")
    OK START P1 5.000                                <- DOSE akzeptiert
    P1 RUNNING 3.120 ml remaining                    <- unsolicited Fortschritt
    DONE P1 5.000                                    <- unsolicited, Dosis fertig
    ERR P1 BUSY / ERR P1 LIMIT_EXCEEDED               <- synchrone Direktantwort auf DOSE/STOP
    ERR P1 TIMEOUT                                   <- unsolicited (Watchdog in loop())
    ERR <token> INVALID_PUMP                         <- echoter Roh-Token, nicht zwingend "Pn"-Form
    ERR INVALID_COMMAND                              <- synchrone Direktantwort
    OK STOP P1 / OK STOPALL                          <- synchrone Direktantwort
    BOOT READY, Pn DRV_STATUS 0x...                  <- unsolicited Diagnose beim Boot/Reset
"""

from __future__ import annotations

import re

from services.peristaltic.models import LineKind, ParsedLine

FIRMWARE_MAX_SINGLE_DOSE_ML = 50.0     # nur Referenz/Doku, keine CLI-Sicherheitsgrenze
FIRMWARE_MAX_RUNTIME_MS = 30000
FIRMWARE_DEFAULT_ML_PER_STEP = 0.000095548

_PUMP_TOKEN = r"P[1-4]"
_NUMBER = r"\d+(?:\.\d+)?"

_PATTERNS: tuple[tuple[re.Pattern[str], LineKind], ...] = (
    (re.compile(r"^BOOT READY$"), LineKind.BOOT),
    (re.compile(rf"^({_PUMP_TOKEN}) DRV_STATUS 0x[0-9A-Fa-f]+$"), LineKind.DIAGNOSTIC),
    (re.compile(r"^OK READY$"), LineKind.PING_OK),
    (
        re.compile(
            rf"^OK STATUS P1=(IDLE|BUSY) P2=(IDLE|BUSY) P3=(IDLE|BUSY) P4=(IDLE|BUSY)$"
        ),
        LineKind.STATUS_OK,
    ),
    (re.compile(rf"^OK START ({_PUMP_TOKEN}) ({_NUMBER})$"), LineKind.DOSE_STARTED),
    (re.compile(rf"^({_PUMP_TOKEN}) RUNNING ({_NUMBER}) ml remaining$"), LineKind.RUNNING),
    (re.compile(rf"^DONE ({_PUMP_TOKEN}) ({_NUMBER})$"), LineKind.DONE),
    (re.compile(rf"^ERR ({_PUMP_TOKEN}) BUSY$"), LineKind.ERR_BUSY),
    (re.compile(rf"^ERR ({_PUMP_TOKEN}) LIMIT_EXCEEDED$"), LineKind.ERR_LIMIT_EXCEEDED),
    (re.compile(rf"^ERR ({_PUMP_TOKEN}) TIMEOUT$"), LineKind.ERR_TIMEOUT),
    (re.compile(r"^ERR (\S+) INVALID_PUMP$"), LineKind.ERR_INVALID_PUMP),
    (re.compile(r"^ERR INVALID_COMMAND$"), LineKind.ERR_INVALID_COMMAND),
    (re.compile(rf"^OK STOP ({_PUMP_TOKEN})$"), LineKind.STOP_OK),
    (re.compile(r"^OK STOPALL$"), LineKind.STOPALL_OK),
)


def parse_line(raw: str) -> ParsedLine:
    """Totale Funktion - wirft nie. Alles, was keinem bekannten
    Antwortmuster entspricht, wird als LineKind.UNKNOWN zurückgegeben, nie
    stillschweigend verworfen (der Aufrufer entscheidet, was mit UNKNOWN
    passiert - siehe serial_client.py)."""
    text = raw.strip()

    for pattern, kind in _PATTERNS:
        match = pattern.match(text)
        if not match:
            continue

        if kind is LineKind.BOOT or kind is LineKind.PING_OK or kind is LineKind.ERR_INVALID_COMMAND or kind is LineKind.STOPALL_OK:
            return ParsedLine(raw=text, kind=kind)

        if kind is LineKind.DIAGNOSTIC:
            return ParsedLine(raw=text, kind=kind, pump=match.group(1))

        if kind is LineKind.STATUS_OK:
            statuses = {
                "P1": match.group(1),
                "P2": match.group(2),
                "P3": match.group(3),
                "P4": match.group(4),
            }
            return ParsedLine(raw=text, kind=kind, statuses=statuses)

        if kind in (LineKind.DOSE_STARTED, LineKind.RUNNING, LineKind.DONE):
            return ParsedLine(raw=text, kind=kind, pump=match.group(1), ml=float(match.group(2)))

        if kind in (LineKind.ERR_BUSY, LineKind.ERR_LIMIT_EXCEEDED, LineKind.ERR_TIMEOUT, LineKind.STOP_OK):
            return ParsedLine(raw=text, kind=kind, pump=match.group(1))

        if kind is LineKind.ERR_INVALID_PUMP:
            return ParsedLine(raw=text, kind=kind, pump_token=match.group(1))

    return ParsedLine(raw=text, kind=LineKind.UNKNOWN)


def format_dose_command(pump: str, ml: float) -> str:
    return f"DOSE {pump} {ml:.3f}"


def format_stop_command(pump: str) -> str:
    return f"STOP {pump}"
