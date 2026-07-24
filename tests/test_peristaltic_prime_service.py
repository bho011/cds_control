"""
services/peristaltic/prime.py::prime_single_pump - der aus
scripts/peristaltic_calibration_cli.py::cmd_prime extrahierte, wieder-
verwendbare Priming-Kern (Aufrufer öffnet/schließt Client und Session-
Logger selbst, ruft ensure_all_idle_before_dose() einmal pro MCU vorher
auf). Hardwarefrei über tests/fake_mcu.py (FakeMcu/FakeSerialPort).
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

from fake_mcu import AutoCompletingFakeMcu, FakeMcu, FakeSerialPort, make_client_factory
from services.peristaltic.prime import PrimeChunkResult, prime_single_pump
from services.peristaltic.safety import SafetyCheckFailed, ensure_all_idle_before_dose
from services.peristaltic.serial_client import ClientConfig, PeristalticSerialClient
from services.peristaltic.session_log import PeristalticSessionLogger


def _fast_config(**overrides) -> ClientConfig:
    defaults = dict(
        boot_drain_max_wait_s=0.05,
        boot_drain_quiet_period_s=0.01,
        command_timeout_s=1.0,
        dose_wait_timeout_s=1.0,
    )
    defaults.update(overrides)
    return ClientConfig(**defaults)


def _open_client(mcu: FakeMcu, session_logger: PeristalticSessionLogger) -> tuple[PeristalticSerialClient, FakeSerialPort]:
    port = FakeSerialPort(mcu)
    client = PeristalticSerialClient(
        "fake-port",
        config=_fast_config(),
        serial_factory=make_client_factory(port),
        on_raw_line=session_logger.log_raw,
    )
    client.open()
    return client, port


def _written_commands(port: FakeSerialPort) -> list[str]:
    return [line.split(" ", 1)[0] for line in port.written_lines]


class _McuThatMisreportsBusyAfterOneDose(FakeMcu):
    """DOSE schließt normal mit DONE ab, aber der Fake meldet die Pumpe
    beim nächsten STATUS-Aufruf trotzdem fälschlich als BUSY weiter -
    testet den 'STATUS nach DONE prüfen' Sicherheitscheck."""

    def __init__(self, *args, misreport_after_dose_number: int = 1, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._misreport_after_dose_number = misreport_after_dose_number
        self._dose_count = 0

    def handle_command(self, line: str) -> list[str]:
        text = line.strip()
        parts = text.split(" ", 1)
        cmd = parts[0].upper() if parts else ""

        if cmd == "DOSE":
            self._dose_count += 1
            lines = super().handle_command(line)
            if lines and lines[0].startswith("OK START"):
                pump_token = parts[1].split(" ", 1)[0]
                lines.extend(self.complete_dose(pump_token))
                if self._dose_count == self._misreport_after_dose_number:
                    self.busy[pump_token] = True
            return lines

        return super().handle_command(line)


class _McuFailingOnNthDose(FakeMcu):
    """DOSE-Aufträge 1..N-1 schließen normal ab, der N-te wird mit
    ERR ... LIMIT_EXCEEDED abgelehnt."""

    def __init__(self, *args, fail_on_dose_number: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fail_on_dose_number = fail_on_dose_number
        self._dose_count = 0

    def handle_command(self, line: str) -> list[str]:
        text = line.strip()
        parts = text.split(" ", 1)
        cmd = parts[0].upper() if parts else ""

        if cmd == "DOSE":
            self._dose_count += 1
            if self._dose_count == self._fail_on_dose_number:
                dose_parts = parts[1].split(" ", 1)
                pump_token = dose_parts[0]
                return [f"ERR {pump_token} LIMIT_EXCEEDED"]

            lines = super().handle_command(line)
            if lines and lines[0].startswith("OK START"):
                pump_token = parts[1].split(" ", 1)[0]
                lines.extend(self.complete_dose(pump_token))
            return lines

        return super().handle_command(line)


def test_150_ml_are_split_into_15_chunks_and_all_complete(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=150.0, chunk_ml=10.0,
    )

    assert outcome.completion_reason == "completed"
    assert outcome.chunks_total == 15
    assert outcome.chunks_completed == 15
    assert outcome.completed_ml == 150.0

    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 15
    assert all(line == "DOSE P2 10.000" for line in dose_lines)

    client.close()
    session_logger.close()


def test_155_ml_are_split_into_15_of_10_plus_1_of_5(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=155.0, chunk_ml=10.0,
    )

    assert outcome.completion_reason == "completed"
    assert outcome.completed_ml == 155.0

    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 16
    assert dose_lines[:15] == ["DOSE P2 10.000"] * 15
    assert dose_lines[15] == "DOSE P2 5.000"

    client.close()
    session_logger.close()


def test_the_150_ml_are_never_split_across_pumps_only_within_one_pump(tmp_path: Path) -> None:
    """Jede Pumpe bekommt ihre eigenen vollen 150 ml - prime_single_pump
    kennt nur eine Pumpe pro Aufruf, es gibt keinen Mechanismus, der die
    Menge auf mehrere Pumpen aufteilen könnte."""
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    outcome_p1 = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P1", role="nutrient_a_1", max_ml=150.0, chunk_ml=10.0,
    )
    outcome_p3 = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P3", role="nutrient_a_2", max_ml=150.0, chunk_ml=10.0,
    )

    assert outcome_p1.completed_ml == 150.0
    assert outcome_p3.completed_ml == 150.0

    client.close()
    session_logger.close()


def test_stop_event_set_before_first_chunk_aborts_immediately(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    stop_event = threading.Event()
    stop_event.set()

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=150.0, chunk_ml=10.0,
        stop_event=stop_event,
    )

    assert outcome.completion_reason == "user_abort"
    assert outcome.chunks_completed == 0
    assert outcome.completed_ml == 0.0
    assert not any(line.startswith("DOSE") for line in port.written_lines)

    csv_path = session_logger.csv_path
    client.close()
    session_logger.close()

    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 1  # terminaler Eintrag auch bei 0 abgeschlossenen Chunks
    assert rows[0]["completion_reason"] == "user_abort"
    assert rows[0]["completed_ml"] == "0.0"


def test_stop_event_set_before_first_chunk_uses_custom_abort_reason(tmp_path: Path) -> None:
    """Der Aufrufer (PumpPrimeController) kann emergency_stop statt
    user_abort angeben - prime_single_pump kennt den Unterschied selbst
    nicht, gibt aber den übergebenen Grund unverändert durch."""
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    stop_event = threading.Event()
    stop_event.set()

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=150.0, chunk_ml=10.0,
        stop_event=stop_event, abort_completion_reason="emergency_stop",
    )

    assert outcome.completion_reason == "emergency_stop"

    client.close()
    session_logger.close()


def test_stop_event_set_between_chunk_1_and_2_aborts_after_one_chunk(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    stop_event = threading.Event()

    def _on_chunk_complete(chunk: PrimeChunkResult) -> None:
        if chunk.index == 1:
            stop_event.set()

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=150.0, chunk_ml=10.0,
        stop_event=stop_event, on_chunk_complete=_on_chunk_complete,
    )

    assert outcome.completion_reason == "user_abort"
    assert outcome.chunks_completed == 1
    assert outcome.completed_ml == 10.0

    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 1

    client.close()
    session_logger.close()


def test_busy_after_one_dose_stops_further_chunks_and_sends_stopall(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    mcu = _McuThatMisreportsBusyAfterOneDose(misreport_after_dose_number=1)
    client, port = _open_client(mcu, session_logger)
    ensure_all_idle_before_dose(client)

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=50.0, chunk_ml=10.0,
    )

    assert outcome.completion_reason == "safety_check_failed"
    assert outcome.error_code == SafetyCheckFailed.__name__
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 1  # kein zweiter Teilauftrag
    assert "STOPALL" in _written_commands(port)[-3:]

    client.close()
    session_logger.close()


def test_error_on_nth_dose_prevents_all_further_chunks(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    mcu = _McuFailingOnNthDose(fail_on_dose_number=4)
    client, port = _open_client(mcu, session_logger)
    ensure_all_idle_before_dose(client)

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=150.0, chunk_ml=10.0,
    )

    assert outcome.completion_reason == "error"
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 4  # 3 erfolgreiche + der fehlgeschlagene 4., danach Schluss
    assert "STOPALL" in _written_commands(port)[-3:]

    client.close()
    session_logger.close()


def test_on_chunk_complete_reports_correct_running_totals(tmp_path: Path) -> None:
    session_logger = PeristalticSessionLogger("prime", log_dir=tmp_path / "logs")
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    seen: list[PrimeChunkResult] = []

    outcome = prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=30.0, chunk_ml=10.0,
        on_chunk_complete=seen.append,
    )

    assert outcome.completion_reason == "completed"
    assert [chunk.index for chunk in seen] == [1, 2, 3]
    assert [chunk.completed_total_ml for chunk in seen] == [10.0, 20.0, 30.0]
    assert all(chunk.total == 3 for chunk in seen)

    client.close()
    session_logger.close()


def test_csv_rows_are_marked_as_priming_with_completion_reason_only_on_last_row(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    session_logger = PeristalticSessionLogger("prime", log_dir=log_dir)
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=20.0, chunk_ml=10.0,
    )

    csv_path = session_logger.csv_path
    client.close()
    session_logger.close()

    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 2
    for row in rows:
        assert row["operation"] == "prime"
        assert row["command"] == "prime"
        assert row["requested_max_ml"] == "20.0"
        assert row["chunk_ml"] == "10.0"
        assert row["candidate_ml_per_step"] == ""
        assert row["firmware_ml_per_step_used"] == ""
    assert rows[0]["completion_reason"] == ""
    assert rows[-1]["completion_reason"] == "completed"
    assert rows[-1]["completed_ml"] == "20.0"


def test_prime_single_pump_passes_through_the_callers_session_id(tmp_path: Path) -> None:
    """prime_single_pump() setzt session_id nie selbst - eine vom Aufrufer
    (z.B. PumpPrimeController, das mehrere MCU-Logger mit derselben
    session_id ausstattet) vorgegebene session_id muss unverändert in
    jeder Zeile erscheinen."""
    session_logger = PeristalticSessionLogger(
        "prime", log_dir=tmp_path / "logs", session_id="prime_shared_test_id"
    )
    client, port = _open_client(AutoCompletingFakeMcu(), session_logger)
    ensure_all_idle_before_dose(client)

    prime_single_pump(
        client, session_logger,
        controller_id="MCU_B", controller_role="EC", port="fake-port",
        pump="P2", role="nutrient_b_1", max_ml=20.0, chunk_ml=10.0,
    )

    csv_path = session_logger.csv_path
    client.close()
    session_logger.close()

    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 2
    assert all(row["session_id"] == "prime_shared_test_id" for row in rows)
