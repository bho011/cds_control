"""
Hardwarefreier Test-Doppelgänger für die Peristaltik-Firmware: eine kleine,
reaktive FakeMcu (führt echten Busy/IDLE-Zustand je Pumpe, damit BUSY/
STOPALL/parallele Dosierung realistisch nachgebildet werden können) plus
ein FakeSerialPort, der nur die von PeristalticSerialClient tatsächlich
genutzte pyserial-Teilmenge implementiert (write/readline/
reset_input_buffer/close) und über `serial_factory` injiziert wird - nie
ein echter Port.

Kein eigenes Testmodul (kein test_*.py-Präfix), wird von den
test_peristaltic_*.py-Dateien importiert.
"""

from __future__ import annotations

import queue
from typing import Iterable


class FakeMcu:
    """Minimal-Nachbildung von src/main.cpp::handle_command() - nur so viel
    Zustand wie für die Tests nötig (Busy/IDLE je Pumpe). RUNNING/DONE/
    TIMEOUT werden NICHT automatisch nach einer echten Wartezeit erzeugt
    (das wäre für Tests zu langsam/nichtdeterministisch) - der Test ruft
    stattdessen explizit complete_dose()/timeout_dose() auf und speist die
    zurückgegebenen Zeilen über FakeSerialPort.inject_line() ein."""

    def __init__(self, pumps: tuple[str, ...] = ("P1", "P2", "P3", "P4"), boot_banner: bool = True) -> None:
        self.pumps = pumps
        self.boot_banner = boot_banner
        self.busy = {pump: False for pump in pumps}
        self._pending_ml: dict[str, float] = {}

    def boot_lines(self) -> list[str]:
        if not self.boot_banner:
            return []
        lines = ["BOOT READY"]
        for pump in self.pumps:
            lines.append(f"{pump} DRV_STATUS 0x1")
        return lines

    def handle_command(self, line: str) -> list[str]:
        text = line.strip()
        if not text:
            return []

        parts = text.split(" ", 1)
        cmd = parts[0].upper()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "PING":
            return ["OK READY"]

        if cmd == "STATUS":
            states = " ".join(f"{pump}={'BUSY' if self.busy[pump] else 'IDLE'}" for pump in self.pumps)
            return [f"OK STATUS {states}"]

        if cmd == "STOPALL":
            for pump in self.pumps:
                self.busy[pump] = False
            return ["OK STOPALL"]

        if cmd == "STOP":
            token = rest.strip()
            if token not in self.pumps:
                return [f"ERR {token} INVALID_PUMP"]
            self.busy[token] = False
            return [f"OK STOP {token}"]

        if cmd == "DOSE":
            dose_parts = rest.split(" ", 1)
            if len(dose_parts) != 2:
                return ["ERR INVALID_COMMAND"]
            pump_token, ml_token = dose_parts[0], dose_parts[1].strip()

            if pump_token not in self.pumps:
                return [f"ERR {pump_token} INVALID_PUMP"]

            try:
                ml = float(ml_token)
            except ValueError:
                ml = 0.0

            if ml <= 0 or ml > 50.0:
                return [f"ERR {pump_token} LIMIT_EXCEEDED"]

            if self.busy[pump_token]:
                return [f"ERR {pump_token} BUSY"]

            self.busy[pump_token] = True
            self._pending_ml[pump_token] = ml
            return [f"OK START {pump_token} {ml:.3f}"]

        return ["ERR INVALID_COMMAND"]

    def complete_dose(self, pump: str, running_samples: Iterable[float] = ()) -> list[str]:
        """Simuliert das erfolgreiche Ende einer laufenden Dosis: optionale
        RUNNING-Fortschrittszeilen, dann DONE. Setzt die Pumpe auf IDLE."""
        ml = self._pending_ml.get(pump, 0.0)
        lines = [f"{pump} RUNNING {remaining:.3f} ml remaining" for remaining in running_samples]
        self.busy[pump] = False
        lines.append(f"DONE {pump} {ml:.3f}")
        return lines

    def timeout_dose(self, pump: str) -> list[str]:
        """Simuliert den firmwareseitigen Laufzeit-Watchdog: die Pumpe wird
        zwangsgestoppt, ERR Pn TIMEOUT wird gemeldet (immer unsolicited)."""
        self.busy[pump] = False
        return [f"ERR {pump} TIMEOUT"]


class FakeSerialPort:
    """Implementiert nur write()/readline()/reset_input_buffer()/close(),
    genau die von PeristalticSerialClient genutzte pyserial-Teilmenge."""

    def __init__(self, mcu: FakeMcu | None = None, read_timeout: float = 0.02) -> None:
        self.mcu = mcu
        self.read_timeout = read_timeout
        self._rx_queue: "queue.Queue[bytes]" = queue.Queue()
        self.written_lines: list[str] = []
        self.is_open = True
        self._raise_on_read: Exception | None = None
        self._raise_on_write: Exception | None = None
        self._boot_lines_delivered = False

    def write(self, data: bytes) -> int:
        if self._raise_on_write is not None:
            raise self._raise_on_write

        text = data.decode("ascii").strip()
        self.written_lines.append(text)

        if self.mcu is not None:
            for response in self.mcu.handle_command(text):
                self._rx_queue.put((response + "\n").encode("utf-8"))

        return len(data)

    def readline(self) -> bytes:
        if self._raise_on_read is not None:
            raise self._raise_on_read
        try:
            return self._rx_queue.get(timeout=self.read_timeout)
        except queue.Empty:
            return b""

    def reset_input_buffer(self) -> None:
        """Wie beim echten Arduino: der Boot-Banner erscheint NACH dem
        (durch das Öffnen des Ports ausgelösten) Reset, nicht schon vorher
        im Puffer - deshalb liefert der Fake ihn erst hier aus, nicht schon
        im Konstruktor, damit reset_input_buffer() ihn nicht versehentlich
        selbst wegwirft."""
        while True:
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break

        if self.mcu is not None and not self._boot_lines_delivered:
            self._boot_lines_delivered = True
            for line in self.mcu.boot_lines():
                self._rx_queue.put((line + "\n").encode("utf-8"))

    def close(self) -> None:
        self.is_open = False

    def inject_line(self, text: str) -> None:
        """Speist eine RX-Zeile unabhängig vom TX-Zeitpunkt ein - z.B. für
        den Desync-Regressionstest (verspätete Antwort eines Befehls, der
        bereits in den Timeout gelaufen ist)."""
        self._rx_queue.put((text + "\n").encode("utf-8"))

    def simulate_disconnect(self, exc: Exception | None = None) -> None:
        self._raise_on_read = exc if exc is not None else OSError("simulierter Verbindungsabbruch")

    def fail_next_write(self, exc: Exception | None = None) -> None:
        self._raise_on_write = exc if exc is not None else OSError("simulierter Schreibfehler")


class AutoCompletingFakeMcu(FakeMcu):
    """Wie FakeMcu, aber DOSE schließt sofort synchron mit DONE ab (keine
    BUSY-Zwischenphase, kein zweiter Thread/manueller complete_dose()-
    Aufruf nötig) - für Tests, die viele aufeinanderfolgende
    prime-Teilaufträge auswerten wollen und bei denen die BUSY/RUNNING-
    Modellierung selbst nicht Testgegenstand ist (dafür existiert bereits
    tests/test_peristaltic_protocol.py)."""

    def handle_command(self, line: str) -> list[str]:
        text = line.strip()
        parts = text.split(" ", 1)
        cmd = parts[0].upper() if parts else ""

        lines = super().handle_command(line)
        if cmd == "DOSE" and lines and lines[0].startswith("OK START"):
            pump_token = parts[1].split(" ", 1)[0]
            lines.extend(self.complete_dose(pump_token))
        return lines


def make_client_factory(port: FakeSerialPort):
    """Liefert eine serial_factory-kompatible Closure, die immer denselben
    FakeSerialPort zurückgibt - unabhängig von den übergebenen
    port/baudrate/timeout-Argumenten (die der echte serial.Serial()
    bräuchte, der Fake ignoriert sie bewusst)."""

    def factory(port_name: str, baudrate: int, timeout: float) -> FakeSerialPort:
        return port

    return factory
