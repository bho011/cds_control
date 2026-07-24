"""
services/peristaltic/protocol.py (parse_line) und
services/peristaltic/serial_client.py (PeristalticSerialClient): Wire-Format-
Parsing, Verbindungs-Lebenszyklus (CLOSED/OPENING/OPEN/DESYNCED/CLOSING),
Fail-closed-Desynchronisation, Queue-Routing (Immediate-Reply vs. je-Pumpe),
best-effort STOPALL-Cleanup. Ausschließlich gegen FakeSerialPort/FakeMcu -
kein echter serieller Port, keine echte Wartezeit über Sekunden hinaus.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from fake_mcu import FakeMcu, FakeSerialPort, make_client_factory

from services.peristaltic.models import LineKind, ParsedLine
from services.peristaltic.protocol import parse_line
from services.peristaltic.serial_client import (
    ClientConfig,
    ConnectionState,
    PeristalticCommandError,
    PeristalticConnectionError,
    PeristalticDesyncError,
    PeristalticProtocolError,
    PeristalticSerialClient,
    PeristalticTimeoutError,
)


def _fast_config(**overrides: Any) -> ClientConfig:
    defaults: dict[str, Any] = dict(
        boot_drain_max_wait_s=0.05,
        boot_drain_quiet_period_s=0.01,
        command_timeout_s=1.0,
        dose_wait_timeout_s=1.0,
    )
    defaults.update(overrides)
    return ClientConfig(**defaults)


def _open_client(mcu: FakeMcu | None = None, **config_overrides: Any) -> tuple[PeristalticSerialClient, FakeSerialPort]:
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient(
        "fake-port", config=_fast_config(**config_overrides), serial_factory=make_client_factory(port)
    )
    client.open()
    return client, port


# --- parse_line() ------------------------------------------------------------


def test_parse_line_ping_ok():
    assert parse_line("OK READY").kind is LineKind.PING_OK


def test_parse_line_status_ok():
    result = parse_line("OK STATUS P1=IDLE P2=BUSY P3=IDLE P4=IDLE")
    assert result.kind is LineKind.STATUS_OK
    assert result.statuses == {"P1": "IDLE", "P2": "BUSY", "P3": "IDLE", "P4": "IDLE"}


def test_parse_line_dose_started():
    result = parse_line("OK START P1 5.000")
    assert result.kind is LineKind.DOSE_STARTED
    assert result.pump == "P1"
    assert result.ml == pytest.approx(5.0)


def test_parse_line_running():
    result = parse_line("P1 RUNNING 3.120 ml remaining")
    assert result.kind is LineKind.RUNNING
    assert result.pump == "P1"
    assert result.ml == pytest.approx(3.120)


def test_parse_line_done():
    result = parse_line("DONE P1 5.000")
    assert result.kind is LineKind.DONE
    assert result.pump == "P1"
    assert result.ml == pytest.approx(5.0)


def test_parse_line_err_busy():
    result = parse_line("ERR P1 BUSY")
    assert result.kind is LineKind.ERR_BUSY
    assert result.pump == "P1"


def test_parse_line_err_limit_exceeded():
    assert parse_line("ERR P2 LIMIT_EXCEEDED").kind is LineKind.ERR_LIMIT_EXCEEDED


def test_parse_line_err_timeout():
    assert parse_line("ERR P3 TIMEOUT").kind is LineKind.ERR_TIMEOUT


def test_parse_line_err_invalid_pump_echoes_arbitrary_token():
    result = parse_line("ERR P9 INVALID_PUMP")
    assert result.kind is LineKind.ERR_INVALID_PUMP
    assert result.pump_token == "P9"

    result2 = parse_line("ERR XYZ INVALID_PUMP")
    assert result2.kind is LineKind.ERR_INVALID_PUMP
    assert result2.pump_token == "XYZ"


def test_parse_line_err_invalid_command():
    assert parse_line("ERR INVALID_COMMAND").kind is LineKind.ERR_INVALID_COMMAND


def test_parse_line_stop_ok():
    result = parse_line("OK STOP P2")
    assert result.kind is LineKind.STOP_OK
    assert result.pump == "P2"


def test_parse_line_stopall_ok():
    assert parse_line("OK STOPALL").kind is LineKind.STOPALL_OK


def test_parse_line_boot():
    assert parse_line("BOOT READY").kind is LineKind.BOOT


def test_parse_line_diagnostic():
    result = parse_line("P1 DRV_STATUS 0x1A")
    assert result.kind is LineKind.DIAGNOSTIC
    assert result.pump == "P1"


def test_parse_line_unknown_for_garbage():
    assert parse_line("this is not a real protocol line").kind is LineKind.UNKNOWN


def test_parse_line_fuzz_never_raises():
    import random

    random.seed(42)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .=_\n\r\t"
    for _ in range(500):
        length = random.randint(0, 40)
        garbage = "".join(random.choice(alphabet) for _ in range(length))
        result = parse_line(garbage)
        assert isinstance(result, ParsedLine)


# --- Verbindungsaufbau / Handshake -------------------------------------------


def test_boot_messages_drained_before_ping_and_open_succeeds():
    mcu = FakeMcu(boot_banner=True)
    client, port = _open_client(mcu)
    try:
        assert any("BOOT READY" in line for line in client.diagnostics)
        assert client.state == ConnectionState.OPEN
    finally:
        client.close()


def test_open_sends_stopall_ping_status_in_order():
    mcu = FakeMcu()
    client, port = _open_client(mcu)
    try:
        assert port.written_lines == ["STOPALL", "PING", "STATUS"]
    finally:
        client.close()


def test_reader_thread_not_started_before_reset_input_buffer():
    mcu = FakeMcu()
    port = FakeSerialPort(mcu)
    events: list[str] = []

    original_reset = port.reset_input_buffer
    original_readline = port.readline

    def tracked_reset() -> None:
        events.append("reset_input_buffer")
        original_reset()

    def tracked_readline() -> bytes:
        if "readline" not in events:
            events.append("readline")
        return original_readline()

    port.reset_input_buffer = tracked_reset  # type: ignore[method-assign]
    port.readline = tracked_readline  # type: ignore[method-assign]

    client = PeristalticSerialClient("fake-port", config=_fast_config(), serial_factory=make_client_factory(port))
    client.open()
    try:
        assert "reset_input_buffer" in events
        assert "readline" in events
        assert events.index("reset_input_buffer") < events.index("readline")
    finally:
        client.close()


def test_open_only_allowed_from_closed():
    client, port = _open_client(FakeMcu())
    try:
        with pytest.raises(PeristalticConnectionError):
            client.open()
    finally:
        client.close()


def test_open_failure_on_dead_link_leaves_state_closed_not_desynced():
    port = FakeSerialPort(mcu=None)  # niemand antwortet je
    client = PeristalticSerialClient(
        "fake-port", config=_fast_config(command_timeout_s=0.05), serial_factory=make_client_factory(port)
    )
    with pytest.raises(PeristalticConnectionError):
        client.open()
    assert client.state == ConnectionState.CLOSED


def test_close_is_idempotent():
    client, port = _open_client(FakeMcu())
    client.close()
    client.close()
    assert client.state == ConnectionState.CLOSED


def test_close_without_prior_open_is_a_noop():
    client = PeristalticSerialClient(
        "fake-port", config=_fast_config(), serial_factory=make_client_factory(FakeSerialPort())
    )
    client.close()
    assert client.state == ConnectionState.CLOSED


# --- PING / STATUS ------------------------------------------------------------


def test_ping_ok():
    client, port = _open_client(FakeMcu())
    try:
        result = client.ping()
        assert result.kind is LineKind.PING_OK
    finally:
        client.close()


def test_status_all_idle():
    client, port = _open_client(FakeMcu())
    try:
        assert client.status() == {"P1": "IDLE", "P2": "IDLE", "P3": "IDLE", "P4": "IDLE"}
    finally:
        client.close()


def test_status_mixed_busy_idle():
    mcu = FakeMcu()
    client, port = _open_client(mcu)
    try:
        client.start_dose("P2", 2.0)
        status = client.status()
        assert status["P2"] == "BUSY"
        assert status["P1"] == "IDLE"
        assert status["P3"] == "IDLE"
        assert status["P4"] == "IDLE"
    finally:
        client.close()


# --- DOSE / RUNNING / DONE / Fehler -------------------------------------------


def test_single_dose_completes_with_running_and_done():
    events: list[ParsedLine] = []
    mcu = FakeMcu()
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient(
        "fake-port", config=_fast_config(), serial_factory=make_client_factory(port), on_event=events.append
    )
    client.open()
    try:
        client.start_dose("P1", 5.0)
        for line in mcu.complete_dose("P1", running_samples=[3.120, 1.050]):
            port.inject_line(line)

        result = client.wait_for_dose("P1", timeout_s=2.0)
        assert result.kind is LineKind.DONE
        assert result.ml == pytest.approx(5.0)

        kinds = [event.kind for event in events]
        assert LineKind.RUNNING in kinds
        assert LineKind.DONE in kinds
    finally:
        client.close()


def test_dose_on_busy_pump_raises_command_error():
    client, port = _open_client(FakeMcu())
    try:
        client.start_dose("P1", 5.0)
        with pytest.raises(PeristalticCommandError) as exc_info:
            client.start_dose("P1", 3.0)
        assert exc_info.value.kind is LineKind.ERR_BUSY
    finally:
        client.close()


def test_dose_over_firmware_limit_raises_command_error():
    client, port = _open_client(FakeMcu())
    try:
        with pytest.raises(PeristalticCommandError) as exc_info:
            client.start_dose("P1", 999.0)
        assert exc_info.value.kind is LineKind.ERR_LIMIT_EXCEEDED
    finally:
        client.close()


def test_dose_firmware_timeout_raises_command_error():
    mcu = FakeMcu()
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient("fake-port", config=_fast_config(), serial_factory=make_client_factory(port))
    client.open()
    try:
        client.start_dose("P1", 5.0)
        for line in mcu.timeout_dose("P1"):
            port.inject_line(line)
        with pytest.raises(PeristalticCommandError) as exc_info:
            client.wait_for_dose("P1", timeout_s=1.0)
        assert exc_info.value.kind is LineKind.ERR_TIMEOUT
    finally:
        client.close()


def test_dose_with_invalid_pump_token_echoes_exact_token():
    client, port = _open_client(FakeMcu())
    try:
        with pytest.raises(PeristalticCommandError) as exc_info:
            client._send_command("DOSE P9 5.000", expected_kinds=frozenset({LineKind.DOSE_STARTED}))
        assert exc_info.value.kind is LineKind.ERR_INVALID_PUMP
        assert exc_info.value.pump == "P9"
    finally:
        client.close()


def test_invalid_command_raises_command_error():
    client, port = _open_client(FakeMcu())
    try:
        with pytest.raises(PeristalticCommandError) as exc_info:
            client._send_command("BOGUS", expected_kinds=frozenset({LineKind.PING_OK}))
        assert exc_info.value.kind is LineKind.ERR_INVALID_COMMAND
    finally:
        client.close()


def test_stop_idempotent_on_idle_pump():
    client, port = _open_client(FakeMcu())
    try:
        result = client.stop("P1")
        assert result.kind is LineKind.STOP_OK
        assert result.pump == "P1"
    finally:
        client.close()


def test_stopall_never_produces_done_for_a_running_pump():
    """hard_stop_pump() in der Firmware gibt kein DONE aus - nach
    STOP/STOPALL darf nie mehr auf DONE für die betroffene Pumpe gewartet
    werden (Kernannahme hinter monitor_parallel_doses() in der CLI)."""
    client, port = _open_client(FakeMcu())
    try:
        client.start_dose("P1", 5.0)
        client.stop_all()
        event = client.poll_pump_event("P1", timeout=0.05)
        assert event is None
    finally:
        client.close()


def test_two_parallel_doses_route_done_correctly_despite_interleaved_running():
    mcu = FakeMcu()
    client, port = _open_client(mcu)
    try:
        client.start_dose("P1", 5.0)
        client.start_dose("P2", 3.0)

        port.inject_line("P1 RUNNING 3.000 ml remaining")
        port.inject_line("P2 RUNNING 1.500 ml remaining")
        port.inject_line("P1 RUNNING 1.000 ml remaining")
        for line in mcu.complete_dose("P2"):
            port.inject_line(line)
        for line in mcu.complete_dose("P1"):
            port.inject_line(line)

        result_p2 = client.wait_for_dose("P2", timeout_s=2.0)
        result_p1 = client.wait_for_dose("P1", timeout_s=2.0)
        assert result_p1.pump == "P1" and result_p1.kind is LineKind.DONE
        assert result_p2.pump == "P2" and result_p2.kind is LineKind.DONE
    finally:
        client.close()


# --- Unbekannte/unerwartete Antworten -----------------------------------------


def test_immediate_unknown_reply_raises_protocol_error_and_is_logged():
    mcu = FakeMcu()
    client, port = _open_client(mcu)
    try:
        mcu.handle_command = lambda line: ["THIS IS NOT A VALID RESPONSE"]
        with pytest.raises(PeristalticProtocolError):
            client.ping()
        assert any("THIS IS NOT A VALID RESPONSE" in message for message in client.protocol_errors)
    finally:
        client.close()


def test_stale_unsolicited_reply_before_next_command_triggers_desync():
    client, port = _open_client(FakeMcu())
    try:
        port.inject_line("OK READY")  # verirrte Zeile, ohne dass ein Befehl darauf wartet
        time.sleep(0.05)

        with pytest.raises(PeristalticDesyncError):
            client.status()
        assert client.state == ConnectionState.DESYNCED
    finally:
        client.close()


def test_public_stop_all_rejected_when_closed():
    client = PeristalticSerialClient(
        "fake-port", config=_fast_config(), serial_factory=make_client_factory(FakeSerialPort())
    )
    with pytest.raises(PeristalticConnectionError):
        client.stop_all()


# --- Desynchronisation (Timeout / Disconnect) --------------------------------


def test_command_timeout_marks_client_desynced():
    mcu = FakeMcu()
    client, port = _open_client(mcu, command_timeout_s=0.05)
    try:
        mcu.handle_command = lambda line: []  # ab jetzt: keine Antwort mehr
        with pytest.raises(PeristalticTimeoutError):
            client.ping()
        assert client.state == ConnectionState.DESYNCED
    finally:
        client.close()


def test_late_reply_after_timeout_desyncs_client_until_reconnect():
    """Regressionstest (explizit gefordert): Befehl A läuft in Timeout,
    seine Antwort trifft verspätet ein, Befehl B wird ohne Reconnect
    abgelehnt (und schreibt nichts), nach Close/Open funktioniert B
    wieder."""
    mcu = FakeMcu()
    client, port = _open_client(mcu, command_timeout_s=0.05)

    original_handle_command = mcu.handle_command
    mcu.handle_command = lambda line: []  # Befehl A (PING) bekommt keine Antwort -> Timeout

    with pytest.raises(PeristalticTimeoutError):
        client.ping()
    assert client.state == ConnectionState.DESYNCED

    written_before = list(port.written_lines)
    port.inject_line("OK READY")  # A's Antwort trifft jetzt verspätet ein
    time.sleep(0.05)

    with pytest.raises(PeristalticDesyncError):
        client.status()  # Befehl B: ohne Reconnect abgelehnt
    assert port.written_lines == written_before  # B hat NICHTS geschrieben

    mcu.handle_command = original_handle_command
    client.close()

    client.open()  # Reconnect
    try:
        status = client.status()  # B funktioniert jetzt wieder
        assert status == {"P1": "IDLE", "P2": "IDLE", "P3": "IDLE", "P4": "IDLE"}
    finally:
        client.close()


def test_reader_disconnect_wakes_waiters_immediately_not_after_full_timeout():
    client, port = _open_client(FakeMcu(), command_timeout_s=5.0)
    try:
        # mcu abhaengen: ab jetzt erzeugt write() keine automatische
        # Antwort mehr - sonst gaebe es ein Zeitfenster, in dem die
        # ohnehin schon geschriebene PING-Antwort noch regulaer beim
        # Readerthread ankommen koennte, bevor dessen naechster
        # readline()-Aufruf den simulierten Abbruch bemerkt.
        port.mcu = None
        port.simulate_disconnect()
        started = time.monotonic()
        with pytest.raises(PeristalticConnectionError):
            client.ping()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert client.state == ConnectionState.DESYNCED
    finally:
        client.close()


def test_write_failure_marks_client_desynced():
    client, port = _open_client(FakeMcu())
    try:
        port.fail_next_write()
        with pytest.raises(PeristalticConnectionError):
            client.ping()
        assert client.state == ConnectionState.DESYNCED
    finally:
        client.close()


# --- best-effort STOPALL / Cleanup (Zusatzregel 3 bei Freigabe) --------------


def test_best_effort_stopall_is_logged_even_though_no_reply_is_awaited():
    tx_log: list[tuple[str, str]] = []
    mcu = FakeMcu()
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient(
        "fake-port",
        config=_fast_config(),
        serial_factory=make_client_factory(port),
        on_raw_line=lambda direction, line: tx_log.append((direction, line)),
    )
    client.open()
    client.close()
    assert ("TX", "STOPALL") in tx_log


def test_broken_raw_log_callback_does_not_block_best_effort_cleanup():
    def failing_on_raw_line(direction: str, line: str) -> None:
        raise RuntimeError("logger kaputt")

    mcu = FakeMcu()
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient(
        "fake-port", config=_fast_config(), serial_factory=make_client_factory(port), on_raw_line=failing_on_raw_line
    )
    client.open()
    client.close()  # darf NICHT wegen des kaputten Loggers scheitern
    assert client.state == ConnectionState.CLOSED
    assert port.written_lines[-1] == "STOPALL"  # Schreibversuch fand trotzdem statt


def test_close_sends_best_effort_stopall():
    client, port = _open_client(FakeMcu())
    client.close()
    assert "STOPALL" in port.written_lines


def test_keyboard_interrupt_during_context_triggers_close_stopall():
    mcu = FakeMcu()
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient("fake-port", config=_fast_config(), serial_factory=make_client_factory(port))

    with pytest.raises(KeyboardInterrupt):
        with client:
            raise KeyboardInterrupt()

    assert port.written_lines[-1] == "STOPALL"
    assert client.state == ConnectionState.CLOSED
