"""
Thread-sicherer, line-basierter Seriell-Client für die Peristaltik-MCUs.

Nebenläufigkeits-Design (gegen den echten Firmware-Quelltext src/main.cpp
verifiziert, siehe docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md):

- Ein einziger Hintergrund-Lesethread liest zeilenbasiert, schreibt JEDE
  Rohzeile sofort in den Rohlog-Callback (bevor überhaupt klassifiziert
  wird) und verteilt dann:
    * BOOT/DIAGNOSTIC -> Diagnose-Liste
    * RUNNING/DONE/ERR_TIMEOUT -> je Pumpe eine eigene Queue (diese drei
      sind IMMER unsolicited/asynchron, nie Antwort auf den gerade
      gesendeten Befehl - Pumpen laufen parallel, keine globale Sperre)
    * alles andere Strukturierte (inkl. UNKNOWN) -> eine gemeinsame
      Immediate-Reply-Queue, da immer genau ein Befehl gleichzeitig
      unterwegs ist (Schreiben wird durch einen Lock serialisiert)
- Fail-closed Desynchronisation: ein Command-Timeout, ein vom Lesethread
  erkannter Verbindungsabbruch, oder ein vor dem Senden gefundener
  "verirrter" Rest in der Immediate-Reply-Queue markieren den Client als
  DESYNCED. Ab dann wird JEDER weitere Befehl sofort abgelehnt
  (PeristalticDesyncError), ohne jeden I/O-Versuch - eine verspätete
  Antwort eines alten Befehls kann so nie mehr als Antwort eines neuen
  Befehls interpretiert werden. Reconnect (close()+open()) ist zwingend.
- Nach STOP/STOPALL wird nie mehr auf DONE für die betroffene(n) Pumpe(n)
  gewartet - hard_stop_pump() in der Firmware gibt kein DONE aus.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Protocol

from services.peristaltic.models import VALID_PUMPS, LineKind, ParsedLine
from services.peristaltic.protocol import format_dose_command, format_stop_command, parse_line


class SerialLike(Protocol):
    def write(self, data: bytes) -> int: ...
    def readline(self) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


SerialFactory = Callable[[str, int, float], SerialLike]


def default_serial_factory(port: str, baudrate: int, timeout: float) -> SerialLike:
    import serial

    return serial.Serial(port, baudrate=baudrate, timeout=timeout, exclusive=True)


class PeristalticError(Exception):
    """Basisklasse aller Peristaltik-Client-Fehler."""


class PeristalticConnectionError(PeristalticError):
    """Verbindungsfehler: Öffnen fehlgeschlagen, Verbindungsabbruch, oder
    ein Befehl wurde in einem nicht erlaubten Zustand versucht."""


class PeristalticDesyncError(PeristalticConnectionError):
    """Wird für JEDEN Befehl geworfen, solange der Client als
    desynchronisiert markiert ist - der Aufrufer muss close()+open()
    verwenden, bevor irgendein normaler Befehl wieder versucht wird. Es
    wird dabei NICHTS auf den Port geschrieben."""


class PeristalticProtocolError(PeristalticError):
    """Eine Antwort kam an, die keinem für den gesendeten Befehl
    erwarteten, bekannten Muster entspricht - niemals stillschweigend
    ignoriert."""


class PeristalticCommandError(PeristalticError):
    """Die Firmware hat einen wohlgeformten ERR-Fehler gemeldet (BUSY,
    LIMIT_EXCEEDED, INVALID_PUMP, INVALID_COMMAND) oder einen asynchronen
    Dosis-TIMEOUT ausgelöst."""

    def __init__(self, message: str, *, pump: str | None, kind: LineKind, raw: str) -> None:
        super().__init__(message)
        self.pump = pump
        self.kind = kind
        self.raw = raw


class PeristalticTimeoutError(PeristalticError):
    """Der Client selbst hat innerhalb seines konfigurierten Timeouts
    überhaupt keine Antwort erhalten (toter Link) - zu unterscheiden vom
    firmwareseitigen Dosis-Timeout (ERR Pn TIMEOUT), der als
    PeristalticCommandError geworfen wird."""


class ConnectionState(Enum):
    CLOSED = auto()
    OPENING = auto()
    OPEN = auto()
    DESYNCED = auto()
    CLOSING = auto()


@dataclass
class ClientConfig:
    baudrate: int = 115200
    read_timeout_s: float = 0.2
    command_timeout_s: float = 5.0
    dose_wait_timeout_s: float = 35.0   # FIRMWARE_MAX_RUNTIME_MS (30s) + Marge
    boot_drain_quiet_period_s: float = 0.3
    boot_drain_max_wait_s: float = 2.0


class _Disconnected:
    """Interner Sentinel, der über alle Queues verteilt wird, sobald der
    Lesethread einen Verbindungsabbruch erkennt - weckt jeden gerade
    wartenden Consumer sofort, statt ihn bis zum eigenen Timeout hängen zu
    lassen."""


_DISCONNECTED = _Disconnected()

_IMMEDIATE_REPLY_KINDS = frozenset(
    {
        LineKind.PING_OK,
        LineKind.STATUS_OK,
        LineKind.DOSE_STARTED,
        LineKind.ERR_BUSY,
        LineKind.ERR_LIMIT_EXCEEDED,
        LineKind.ERR_INVALID_PUMP,
        LineKind.ERR_INVALID_COMMAND,
        LineKind.STOP_OK,
        LineKind.STOPALL_OK,
        LineKind.UNKNOWN,
    }
)
_PUMP_EVENT_KINDS = frozenset({LineKind.RUNNING, LineKind.DONE, LineKind.ERR_TIMEOUT})
_ERR_KINDS = frozenset(
    {LineKind.ERR_BUSY, LineKind.ERR_LIMIT_EXCEEDED, LineKind.ERR_INVALID_PUMP, LineKind.ERR_INVALID_COMMAND}
)


class PeristalticSerialClient:
    def __init__(
        self,
        port_name: str,
        *,
        config: ClientConfig | None = None,
        serial_factory: SerialFactory = default_serial_factory,
        on_raw_line: Callable[[str, str], None] | None = None,
        on_event: Callable[[ParsedLine], None] | None = None,
    ) -> None:
        self._port_name = port_name
        self._config = config if config is not None else ClientConfig()
        self._serial_factory = serial_factory
        self._on_raw_line = on_raw_line
        self._on_event = on_event

        self._port: SerialLike | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop_event = threading.Event()
        self._write_lock = threading.Lock()

        self._state: ConnectionState = ConnectionState.CLOSED
        self._immediate_replies: "queue.Queue[Any]" = queue.Queue()
        self._pump_queues: dict[str, "queue.Queue[Any]"] = {pump: queue.Queue() for pump in VALID_PUMPS}
        self._diagnostics: list[str] = []
        self._protocol_errors: list[str] = []

    # --- öffentlicher Zustand -------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def protocol_errors(self) -> list[str]:
        return list(self._protocol_errors)

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    # --- Lebenszyklus -----------------------------------------------------

    def open(self) -> None:
        if self._state != ConnectionState.CLOSED:
            raise PeristalticConnectionError(
                f"open() nur aus CLOSED erlaubt (aktuell: {self._state.name})."
            )

        self._state = ConnectionState.OPENING
        try:
            # 1. Port MIT exclusive=True öffnen
            self._port = self._serial_factory(self._port_name, self._config.baudrate, self._config.read_timeout_s)
            # 2. reset_input_buffer() - MUSS vor Reader-Start passieren
            self._port.reset_input_buffer()
            # 3. alle internen Queues kontrolliert leeren
            self._reset_queues()
            # 4. Readerthread starten
            self._start_reader_thread()
            # 5. Boot-/Quiet-Phase
            self.drain_boot_messages()
            # 6. interner Handshake, NUR in OPENING erlaubt
            opening = frozenset({ConnectionState.OPENING})
            self._send_command("STOPALL", expected_kinds=frozenset({LineKind.STOPALL_OK}), allowed_states=opening)
            self._send_command("PING", expected_kinds=frozenset({LineKind.PING_OK}), allowed_states=opening)
            self._send_command("STATUS", expected_kinds=frozenset({LineKind.STATUS_OK}), allowed_states=opening)
        except Exception as exc:
            self._stop_all_best_effort()
            self._stop_reader_thread_if_running()
            self._close_port_if_open()
            self._reset_queues()
            self._state = ConnectionState.CLOSED
            raise PeristalticConnectionError(f"open() fehlgeschlagen: {exc}") from exc

        self._state = ConnectionState.OPEN

    def close(self) -> None:
        """Idempotent: ein zweiter/wiederholter Aufruf ist ein No-Op."""
        if self._state == ConnectionState.CLOSED:
            return
        self._state = ConnectionState.CLOSING
        self._stop_all_best_effort()
        self._stop_reader_thread_if_running()
        self._close_port_if_open()
        self._state = ConnectionState.CLOSED

    def __enter__(self) -> "PeristalticSerialClient":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _reset_queues(self) -> None:
        self._immediate_replies = queue.Queue()
        self._pump_queues = {pump: queue.Queue() for pump in VALID_PUMPS}
        self._diagnostics = []
        # protocol_errors bleibt bewusst als Audit-Historie über die
        # gesamte Objekt-Lebensdauer erhalten (reine Log-Liste, keine
        # Zuordnungsgefahr wie bei den Queues).

    def _start_reader_thread(self) -> None:
        self._reader_stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _stop_reader_thread_if_running(self) -> None:
        self._reader_stop_event.set()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

    def _close_port_if_open(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None

    # --- Cleanup / STOPALL --------------------------------------------------

    def _stop_all_best_effort(self) -> None:
        """Interner Cleanup-Pfad - verwendbar in OPENING, OPEN, DESYNCED,
        CLOSING. Schreibt STOPALL direkt auf den Port (kein
        _send_command(), keine Queue-Interaktion, keine Antwort
        erwartet/abgewartet), schluckt JEDE Exception. Protokolliert den
        Versuch trotzdem im TX-Rohlog - Schreiben und Loggen sind zwei
        unabhängige try/except-Blöcke, damit weder ein kaputter Port das
        Logging noch ein fehlerhafter Log-Callback den Schreibversuch
        verhindert."""
        try:
            if self._port is not None:
                self._port.write(b"STOPALL\n")
        except Exception:
            pass
        try:
            if self._on_raw_line is not None:
                self._on_raw_line("TX", "STOPALL")
        except Exception:
            pass

    def _mark_desynced_if_open(self) -> None:
        """Transition nur OPEN -> DESYNCED. Passiert derselbe Fehler
        während OPENING, bleibt der Zustand unverändert - open()s eigener
        except-Pfad übernimmt in dem Fall die Aufräumarbeit (state=CLOSED,
        nie DESYNCED für eine nie erfolgreich aufgebaute Verbindung)."""
        if self._state == ConnectionState.OPEN:
            self._state = ConnectionState.DESYNCED
            self._stop_all_best_effort()

    # --- Reader-Thread ------------------------------------------------------

    def _reader_loop(self) -> None:
        while not self._reader_stop_event.is_set():
            try:
                raw_bytes = self._port.readline() if self._port is not None else b""
            except Exception as exc:
                self._handle_reader_disconnect(exc)
                return

            if not raw_bytes:
                continue  # Lesetimeout ohne Daten - weiterprüfen (auch Stop-Flag)

            text = raw_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            self._safe_on_raw_line("RX", text)
            parsed = parse_line(text)
            self._dispatch_parsed_line(parsed)

    def _handle_reader_disconnect(self, exc: Exception) -> None:
        self._protocol_errors.append(f"Serieller Lesethread: Verbindung verloren ({exc}).")
        was_open = self._state == ConnectionState.OPEN
        if was_open:
            self._state = ConnectionState.DESYNCED
            self._stop_all_best_effort()
        self._immediate_replies.put(_DISCONNECTED)
        for pump_queue in self._pump_queues.values():
            pump_queue.put(_DISCONNECTED)

    def _dispatch_parsed_line(self, parsed: ParsedLine) -> None:
        if parsed.kind in _PUMP_EVENT_KINDS:
            if self._on_event is not None:
                self._safe_on_event(parsed)
            pump = parsed.pump
            if pump is not None and pump in self._pump_queues:
                self._pump_queues[pump].put(parsed)
            return

        if parsed.kind in (LineKind.BOOT, LineKind.DIAGNOSTIC):
            self._diagnostics.append(parsed.raw)
            return

        if parsed.kind is LineKind.UNKNOWN:
            self._protocol_errors.append(f"Unbekannte/unerwartete Antwortzeile: {parsed.raw!r}")

        self._immediate_replies.put(parsed)

    def _safe_on_raw_line(self, direction: str, line: str) -> None:
        if self._on_raw_line is None:
            return
        try:
            self._on_raw_line(direction, line)
        except Exception:
            pass

    def _safe_on_event(self, parsed: ParsedLine) -> None:
        try:
            self._on_event(parsed)  # type: ignore[misc]
        except Exception:
            pass

    # --- Befehle --------------------------------------------------------------

    def _send_command(
        self,
        command: str,
        *,
        expected_kinds: frozenset[LineKind],
        allowed_states: frozenset[ConnectionState] = frozenset({ConnectionState.OPEN}),
        timeout: float | None = None,
    ) -> ParsedLine:
        if self._state not in allowed_states:
            if self._state == ConnectionState.DESYNCED:
                raise PeristalticDesyncError(
                    f"Client ist desynchronisiert - '{command}' wird nicht gesendet. "
                    "close()+open() (Reconnect) erforderlich."
                )
            raise PeristalticConnectionError(
                f"Befehl '{command}' ist im Zustand {self._state.name} nicht erlaubt "
                f"(erlaubt: {sorted(s.name for s in allowed_states)})."
            )

        effective_timeout = self._config.command_timeout_s if timeout is None else timeout

        with self._write_lock:
            stale: list[Any] = []
            while True:
                try:
                    stale.append(self._immediate_replies.get_nowait())
                except queue.Empty:
                    break

            if stale:
                for item in stale:
                    if item is _DISCONNECTED:
                        continue
                    self._protocol_errors.append(
                        f"Verirrte/nicht zugeordnete Antwort vor dem Senden von '{command}' gefunden: {item.raw!r}"
                    )
                self._mark_desynced_if_open()
                raise PeristalticDesyncError(
                    f"Nicht eindeutig zuordenbarer Immediate-Reply-Zustand vor '{command}' - "
                    "close()+open() (Reconnect) erforderlich."
                )

            try:
                if self._port is None:
                    raise PeristalticConnectionError("Port ist nicht geöffnet.")
                self._port.write((command + "\n").encode("ascii"))
            except PeristalticConnectionError:
                self._mark_desynced_if_open()
                raise
            except Exception as exc:
                self._mark_desynced_if_open()
                raise PeristalticConnectionError(f"Schreiben von '{command}' fehlgeschlagen: {exc}") from exc

            self._safe_on_raw_line("TX", command)

            try:
                reply = self._immediate_replies.get(timeout=effective_timeout)
            except queue.Empty:
                self._mark_desynced_if_open()
                raise PeristalticTimeoutError(
                    f"Keine Antwort auf '{command}' innerhalb von {effective_timeout}s."
                )

        if reply is _DISCONNECTED:
            raise PeristalticConnectionError(f"Verbindung während '{command}' verloren.")

        if reply.kind is LineKind.UNKNOWN:
            raise PeristalticProtocolError(f"Unerwartete/unbekannte Antwort auf '{command}': {reply.raw!r}")

        if reply.kind not in expected_kinds:
            if reply.kind in _ERR_KINDS:
                raise PeristalticCommandError(
                    f"Firmware meldete Fehler auf '{command}': {reply.raw!r}",
                    pump=reply.pump or reply.pump_token,
                    kind=reply.kind,
                    raw=reply.raw,
                )
            raise PeristalticProtocolError(
                f"Unerwartete Antwortart auf '{command}': {reply.raw!r} "
                f"(erwartet: {sorted(k.name for k in expected_kinds)})"
            )

        return reply

    def drain_boot_messages(self) -> list[str]:
        """Wartet eine kurze Quiet-Phase ab und sammelt dabei
        BOOT READY / Pn DRV_STATUS-Zeilen, falls die MCU beim
        Verbindungsaufbau (DTR-Toggle) neu gestartet ist."""
        poll_interval = 0.02
        deadline = time.monotonic() + self._config.boot_drain_max_wait_s
        last_count = len(self._diagnostics)
        last_change_time = time.monotonic()

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            current_count = len(self._diagnostics)
            now = time.monotonic()
            if current_count != last_count:
                last_count = current_count
                last_change_time = now
            elif current_count > 0 and (now - last_change_time) >= self._config.boot_drain_quiet_period_s:
                break

        return list(self._diagnostics)

    def ping(self) -> ParsedLine:
        return self._send_command("PING", expected_kinds=frozenset({LineKind.PING_OK}))

    def status(self) -> dict[str, str]:
        reply = self._send_command("STATUS", expected_kinds=frozenset({LineKind.STATUS_OK}))
        assert reply.statuses is not None
        return dict(reply.statuses)

    def start_dose(self, pump: str, ml: float) -> ParsedLine:
        return self._send_command(
            format_dose_command(pump, ml),
            expected_kinds=frozenset({LineKind.DOSE_STARTED}),
        )

    def wait_for_dose(self, pump: str, timeout_s: float | None = None) -> ParsedLine:
        if self._state == ConnectionState.DESYNCED:
            raise PeristalticDesyncError("Client ist desynchronisiert - Reconnect erforderlich.")
        if self._state != ConnectionState.OPEN:
            raise PeristalticConnectionError(f"wait_for_dose() im Zustand {self._state.name} nicht erlaubt.")

        effective_timeout = self._config.dose_wait_timeout_s if timeout_s is None else timeout_s
        pump_queue = self._pump_queues[pump]
        deadline = time.monotonic() + effective_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._mark_desynced_if_open()
                raise PeristalticTimeoutError(
                    f"Keine DONE/TIMEOUT-Antwort für {pump} innerhalb von {effective_timeout}s."
                )

            try:
                item = pump_queue.get(timeout=remaining)
            except queue.Empty:
                self._mark_desynced_if_open()
                raise PeristalticTimeoutError(
                    f"Keine DONE/TIMEOUT-Antwort für {pump} innerhalb von {effective_timeout}s."
                )

            if item is _DISCONNECTED:
                raise PeristalticConnectionError(f"Verbindung während Dosis auf {pump} verloren.")

            if item.kind is LineKind.DONE:
                return item

            if item.kind is LineKind.ERR_TIMEOUT:
                raise PeristalticCommandError(
                    f"Firmware-Timeout während Dosis auf {pump}: {item.raw!r}",
                    pump=pump,
                    kind=item.kind,
                    raw=item.raw,
                )

            if item.kind is LineKind.RUNNING:
                continue  # kein Endzustand, weiter warten

            raise PeristalticProtocolError(f"Unerwartete Antwort während Dosis auf {pump}: {item.raw!r}")

    def dose(self, pump: str, ml: float, timeout_s: float | None = None) -> ParsedLine:
        self.start_dose(pump, ml)
        return self.wait_for_dose(pump, timeout_s=timeout_s)

    def poll_pump_event(self, pump: str, timeout: float = 0.1) -> ParsedLine | None:
        """Nicht-blockierender/kurzer Check der Pumpen-Queue - Baustein für
        die Mehrpumpen-Überwachung in pair-test/all-four-test. None
        bedeutet "innerhalb von `timeout` kein Ereignis" - das ist der
        Normalfall während eine Pumpe noch läuft, kein Fehler."""
        pump_queue = self._pump_queues[pump]
        try:
            item = pump_queue.get(timeout=timeout)
        except queue.Empty:
            return None

        if item is _DISCONNECTED:
            raise PeristalticConnectionError(f"Verbindung während Überwachung von {pump} verloren.")

        return item

    def stop(self, pump: str) -> ParsedLine:
        return self._send_command(format_stop_command(pump), expected_kinds=frozenset({LineKind.STOP_OK}))

    def stop_all(self) -> ParsedLine:
        return self._send_command("STOPALL", expected_kinds=frozenset({LineKind.STOPALL_OK}))
