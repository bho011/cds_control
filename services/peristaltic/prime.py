"""
Wiederverwendbarer Kern des Priming-Vorgangs (Entlüften einer einzelnen
Pumpe mit Wasser, in Teilaufträgen von höchstens chunk_ml bis max_ml
gesamt) - extrahiert aus scripts/peristaltic_calibration_cli.py::cmd_prime,
damit sowohl die CLI als auch process/pump_prime.py (Dashboard-Anbindung)
denselben, bereits getesteten Ablauf verwenden.

Bewusst KEINE Kalibrierung: kein Eintrag in
calibration_data/peristaltic_calibration.json, kein
firmware_ml_per_step_used, kein candidate_ml_per_step.

prime_single_pump() öffnet/schließt weder den Client noch den
Session-Logger und ruft ensure_all_idle_before_dose() nicht selbst auf -
das sind Aufgaben des Aufrufers, der ggf. mehrere Pumpen über eine
Verbindung laufen lässt (die STATUS->STOPALL->STATUS-Sicherstellung ist
ein Pro-MCU-Gate, kein Pro-Pumpen-Gate).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from services.peristaltic.calibration import compute_priming_chunks
from services.peristaltic.models import LineKind
from services.peristaltic.safety import SafetyCheckFailed, best_effort_stop_all
from services.peristaltic.serial_client import (
    PeristalticCommandError,
    PeristalticConnectionError,
    PeristalticError,
    PeristalticSerialClient,
    PeristalticTimeoutError,
)
from services.peristaltic.session_log import PeristalticSessionLogger

_GUARDED_EXCEPTIONS = (PeristalticError, SafetyCheckFailed)

# Vollständiger Wertesatz für completion_reason (services/peristaltic/session_log.py
# CSV_FIELDNAMES-Spalte "completion_reason" verwendet dieselben Werte):
# completed | user_abort | emergency_stop | connection_error | safety_check_failed | timeout | error


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_guarded_exception(exc: Exception) -> str:
    """Ordnet eine gefangene PeristalticError/SafetyCheckFailed-Instanz
    einem der definierten completion_reason-Werte zu - gegen die
    tatsächlichen Wurfstellen in services/peristaltic/serial_client.py
    verifiziert (wait_for_dose()/_send_command()). Nie ein stiller
    Rückfall auf "completed" - unbekannte/nicht klassifizierte Fälle
    landen fail-closed bei "error"."""
    if isinstance(exc, SafetyCheckFailed):
        return "safety_check_failed"
    if isinstance(exc, PeristalticTimeoutError):
        return "timeout"  # Client selbst erhielt keine Antwort (toter Link)
    if isinstance(exc, PeristalticCommandError) and exc.kind is LineKind.ERR_TIMEOUT:
        return "timeout"  # Firmware meldet ERR Pn TIMEOUT (eigener 30s-Watchdog)
    if isinstance(exc, PeristalticConnectionError):
        return "connection_error"  # inkl. PeristalticDesyncError (Subklasse)
    return "error"


@dataclass
class PrimeChunkResult:
    index: int
    total: int
    requested_ml: float
    completed_total_ml: float
    firmware_reported_ml: float | None
    started_at: str
    finished_at: str
    elapsed_seconds: float


@dataclass
class PrimePumpOutcome:
    controller: str
    pump: str
    role: str
    requested_max_ml: float
    chunk_ml: float
    chunks_total: int
    chunks_completed: int
    completed_ml: float
    completion_reason: str  # "completed" | "user_abort" | "error"
    error_code: str | None = None
    error_message: str | None = None


def prime_single_pump(
    client: PeristalticSerialClient,
    session_logger: PeristalticSessionLogger,
    *,
    controller_id: str,
    controller_role: str | None,
    port: str,
    pump: str,
    role: str,
    max_ml: float,
    chunk_ml: float,
    stop_event: Any | None = None,
    abort_completion_reason: str | Callable[[], str] = "user_abort",
    on_chunk_complete: Callable[[PrimeChunkResult], None] | None = None,
) -> PrimePumpOutcome:
    """
    Führt einen vollständigen Priming-Vorgang für GENAU EINE Pumpe aus.

    Erwartet vom Aufrufer bereits erledigt:
    - client.open() wurde bereits aufgerufen
    - ensure_all_idle_before_dose(client) wurde für diesen Controller
      bereits ausgeführt (Pro-MCU-Gate, nicht Pro-Pumpe)
    - max_ml/chunk_ml wurden bereits über validate_priming_request() geprüft

    stop_event: wird zu Beginn jeder Chunk-Iteration geprüft (kooperativer
    Stop, kein Abbruch eines bereits laufenden DOSE-Kommandos).
    abort_completion_reason: welcher completion_reason bei gesetztem
    stop_event geloggt/zurückgegeben wird - "user_abort" (Standard) für
    einen normalen Stopp, "emergency_stop" wenn der Aufrufer weiß, dass
    der Stop von einem Emergency Stop ausgelöst wurde. Kann auch ein
    Callable[[], str] sein, das ERST beim tatsächlichen Erkennen des
    gesetzten stop_event ausgewertet wird (nicht vorher) - wichtig, weil
    der eigentliche Stop-Grund sich erst NACH dem Start dieses Aufrufs
    ändern kann (z.B. request_stop(reason="emergency_stop") während ein
    Chunk bereits läuft): ein einmalig VOR dem Aufruf ausgewerteter fester
    String würde in diesem Fall den zum Startzeitpunkt noch gültigen,
    inzwischen aber veralteten Grund einfrieren. prime_single_pump() selbst
    kennt den Unterschied zwischen "user_abort"/"emergency_stop" nicht -
    nur der Aufrufer (der self._stop_reason kennt) kann ihn korrekt angeben.

    Fängt PeristalticError/SafetyCheckFailed intern ab, klassifiziert sie
    über classify_guarded_exception() (safety_check_failed/timeout/
    connection_error/error) und gibt sie als entsprechenden
    completion_reason zurück - wirft sie NIE weiter.
    KeyboardInterrupt und sonstige Exceptions werden NICHT abgefangen und
    laufen zum Aufrufer durch (wie im bisherigen cmd_prime-Verhalten).

    Schreibt IMMER mindestens eine terminale CSV-Zeile für diese Pumpe -
    auch bei einem Stopp vor dem allerersten Chunk (0 abgeschlossene
    Chunks) - nie stillschweigend ohne jeden Logeintrag.
    """
    chunks = compute_priming_chunks(max_ml, chunk_ml)
    completed_ml = 0.0

    def _log_row(**overrides: Any) -> None:
        fields: dict[str, Any] = {
            "controller": controller_id,
            "controller_role": controller_role,
            "serial_port": port,
            "pump": pump,
            "pump_role": role,
            "command": "prime",
            "operation": "prime",
            "requested_max_ml": max_ml,
            "chunk_ml": chunk_ml,
            "completed_ml": completed_ml,
        }
        fields.update(overrides)
        session_logger.log_row(**fields)

    for index, this_chunk_ml in enumerate(chunks, start=1):
        if stop_event is not None and stop_event.is_set():
            # Erst JETZT auswerten (nicht vor der Chunk-Schleife) - der
            # tatsächliche Stop-Grund kann sich geändert haben, während
            # dieser Aufruf bereits lief (siehe Docstring oben).
            reason = abort_completion_reason() if callable(abort_completion_reason) else abort_completion_reason
            _log_row(
                requested_ml=None,
                started_at=None,
                finished_at=_iso_now(),
                elapsed_seconds=None,
                result="ABORTED",
                completion_reason=reason,
            )
            return PrimePumpOutcome(
                controller=controller_id,
                pump=pump,
                role=role,
                requested_max_ml=max_ml,
                chunk_ml=chunk_ml,
                chunks_total=len(chunks),
                chunks_completed=index - 1,
                completed_ml=completed_ml,
                completion_reason=reason,
            )

        started_at = _iso_now()
        started_monotonic = time.monotonic()

        try:
            result = client.dose(pump, this_chunk_ml)
            finished_at = _iso_now()
            elapsed_seconds = time.monotonic() - started_monotonic

            status = client.status()
            if status.get(pump) != "IDLE":
                raise SafetyCheckFailed(
                    f"{pump} ist nach Teilauftrag {index}/{len(chunks)} nicht IDLE (STATUS: {status})."
                )
        except _GUARDED_EXCEPTIONS as exc:
            best_effort_stop_all(client)
            reason = classify_guarded_exception(exc)
            _log_row(
                requested_ml=None,
                started_at=None,
                finished_at=_iso_now(),
                elapsed_seconds=None,
                result="FAILED",
                error_code=type(exc).__name__,
                notes=str(exc),
                completion_reason=reason,
            )
            return PrimePumpOutcome(
                controller=controller_id,
                pump=pump,
                role=role,
                requested_max_ml=max_ml,
                chunk_ml=chunk_ml,
                chunks_total=len(chunks),
                chunks_completed=index - 1,
                completed_ml=completed_ml,
                completion_reason=reason,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )

        completed_ml += this_chunk_ml
        is_last_chunk = index == len(chunks)

        _log_row(
            requested_ml=this_chunk_ml,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            result="DONE",
            firmware_reported_ml=result.ml,
            completed_ml=completed_ml,
            completion_reason="completed" if is_last_chunk else None,
        )

        if on_chunk_complete is not None:
            on_chunk_complete(
                PrimeChunkResult(
                    index=index,
                    total=len(chunks),
                    requested_ml=this_chunk_ml,
                    completed_total_ml=completed_ml,
                    firmware_reported_ml=result.ml,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed_seconds,
                )
            )

    return PrimePumpOutcome(
        controller=controller_id,
        pump=pump,
        role=role,
        requested_max_ml=max_ml,
        chunk_ml=chunk_ml,
        chunks_total=len(chunks),
        chunks_completed=len(chunks),
        completed_ml=completed_ml,
        completion_reason="completed",
    )
