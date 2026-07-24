"""
scripts/peristaltic_calibration_cli.py::cmd_prime - separater, sicherer
Priming-Modus (Entlüften) für die Peristaltikpumpen. Testet den vollen
Ablauf hardwarefrei über tests/fake_mcu.py (FakeMcu/FakeSerialPort), nicht
nur die Validierungsfunktionen (die sind bereits in
tests/test_peristaltic_calibration.py::test_validate_priming_request_*/
test_compute_priming_chunks_* abgedeckt).

Kein echter serieller Port wird je geöffnet - PeristalticSerialClient wird
in jedem hardwareberührenden Test über serial_factory=make_client_factory(...)
an einen FakeSerialPort/FakeMcu angeschlossen. Session-Logs werden über
einen eigenen log_dir (tmp_path) umgeleitet, damit kein Testlauf echte
Dateien unter logs/peristaltic/ hinterlässt.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from fake_mcu import AutoCompletingFakeMcu, FakeMcu, FakeSerialPort, make_client_factory
from services.peristaltic.firmware_profiles import (
    ControllerFirmwareProfile,
    FirmwareProfiles,
    PumpFirmwareEntry,
    default_firmware_profiles,
    save_firmware_profiles,
)
from services.peristaltic.models import default_mapping, save_mapping
from services.peristaltic.serial_client import ClientConfig, PeristalticSerialClient
from services.peristaltic.session_log import PeristalticSessionLogger as RealSessionLogger

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "peristaltic_calibration_cli.py"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("peristaltic_calibration_cli_prime_tests", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli() -> ModuleType:
    return _load_cli_module()


class _HardwareTouchedError(AssertionError):
    """Wird geworfen, wenn ein Validierungs-/Bestätigungstest doch bis zur
    Hardware-Client-Konstruktion durchgereicht wird."""


def _forbid_hardware(cli_module: ModuleType) -> None:
    def _boom(*args, **kwargs):
        raise _HardwareTouchedError("_make_client() darf hier nicht aufgerufen werden.")

    cli_module._make_client = _boom


def _forbid_input(prompt: str) -> str:
    raise _HardwareTouchedError(f"input_func darf hier nicht aufgerufen werden (prompt={prompt!r}).")


def _confirmed_profile(controller: str = "MCU_B") -> FirmwareProfiles:
    profiles = default_firmware_profiles()
    profiles.controllers[controller] = ControllerFirmwareProfile(
        status="confirmed",
        profile_id="test-profile",
        microsteps=8,
        speed_rpm=120,
        acceleration_steps_per_s2=2000,
        max_runtime_ms=30000,
        pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=0.000191096) for pump in ("P1", "P2", "P3", "P4")},
    )
    return profiles


def _setup_confirmed_profile_and_mapping(cli_module: ModuleType, tmp_path: Path, controller: str = "MCU_B") -> None:
    profiles_path = tmp_path / "peristaltic_firmware_profiles.json"
    save_firmware_profiles(profiles_path, _confirmed_profile(controller))
    cli_module.FIRMWARE_PROFILES_PATH = profiles_path

    mapping_path = tmp_path / "peristaltic_mapping.json"
    mapping = default_mapping()
    mapping.controllers[controller].port = "fake-port"
    save_mapping(mapping_path, mapping)
    cli_module.MAPPING_PATH = mapping_path


def _redirect_session_logs(cli_module: ModuleType, tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"

    def _factory(command: str) -> RealSessionLogger:
        return RealSessionLogger(command, log_dir=log_dir)

    cli_module.PeristalticSessionLogger = _factory


def _fast_config(**overrides) -> ClientConfig:
    defaults = dict(
        boot_drain_max_wait_s=0.05,
        boot_drain_quiet_period_s=0.01,
        command_timeout_s=1.0,
        dose_wait_timeout_s=1.0,
    )
    defaults.update(overrides)
    return ClientConfig(**defaults)


def _install_fake_client(cli_module: ModuleType, port: FakeSerialPort, *, interrupt_after_n_doses: int | None = None) -> None:
    """Ersetzt cli._make_client() so, dass der zurückgegebene Client an
    einen FakeSerialPort/FakeMcu angeschlossen ist statt an einen echten
    seriellen Port. `interrupt_after_n_doses`: wenn gesetzt, wirft der
    N-te client.dose()-Aufruf KeyboardInterrupt (simuliert Ctrl+C mitten
    im Priming), ohne tatsächlich zu senden."""

    def _make_client(entry, session_logger) -> PeristalticSerialClient:
        client = PeristalticSerialClient(
            entry.port,
            config=_fast_config(baudrate=entry.baudrate),
            serial_factory=make_client_factory(port),
            on_raw_line=session_logger.log_raw,
        )
        if interrupt_after_n_doses is not None:
            original_dose = client.dose
            call_count = {"n": 0}

            def _dose_with_interrupt(pump, ml, timeout_s=None):
                call_count["n"] += 1
                if call_count["n"] == interrupt_after_n_doses:
                    raise KeyboardInterrupt()
                return original_dose(pump, ml, timeout_s=timeout_s)

            client.dose = _dose_with_interrupt
        return client

    cli_module._make_client = _make_client


_AutoCompletingFakeMcu = AutoCompletingFakeMcu  # jetzt in tests/fake_mcu.py, gemeinsam mit Dashboard-Tests genutzt


class _McuFailingOnNthDose(FakeMcu):
    """DOSE-Aufträge 1..N-1 schließen normal (sofort) mit DONE ab, der
    N-te wird von der Firmware mit ERR ... LIMIT_EXCEEDED abgelehnt -
    simuliert einen Fehler mitten in einer Teilauftragsserie."""

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


class _McuThatMisreportsBusyAfterOneDose(FakeMcu):
    """DOSE schließt normal mit DONE ab (Pumpe wird intern wieder IDLE),
    aber der Fake meldet die Pumpe beim nächsten STATUS-Aufruf trotzdem
    fälschlich als BUSY weiter - testet den 'STATUS nach DONE prüfen'
    Sicherheitscheck in cmd_prime unabhängig von einer echten
    Firmware-Inkonsistenz."""

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
                    self.busy[pump_token] = True  # Firmware "lügt" beim naechsten STATUS
            return lines

        return super().handle_command(line)


def _written_commands(port: FakeSerialPort) -> list[str]:
    return [line.split(" ", 1)[0] for line in port.written_lines]


def _args(controller="MCU_B", pump="P2", max_ml=180.0, chunk_ml=10.0) -> argparse.Namespace:
    return argparse.Namespace(controller=controller, pump=pump, max_ml=max_ml, chunk_ml=chunk_ml)


def _confirm(controller: str, pump: str, max_ml: float):
    required = f"PRIME {controller} {pump} {int(max_ml) if float(max_ml).is_integer() else max_ml}"

    def _input(prompt: str) -> str:
        return required

    return _input


# --- Grenzen: max-ml/chunk-ml, kein Hardwarezugriff bei Ablehnung ---------------


def test_max_ml_above_200_is_rejected_without_touching_hardware(cli):
    _forbid_hardware(cli)
    result = cli.cmd_prime(_args(max_ml=200.1), input_func=_forbid_input)
    assert result == 1


def test_chunk_ml_above_max_initial_test_dose_is_rejected_without_touching_hardware(cli):
    _forbid_hardware(cli)
    result = cli.cmd_prime(_args(chunk_ml=10.1), input_func=_forbid_input)
    assert result == 1


def test_existing_max_initial_test_dose_ml_is_unchanged():
    from services.peristaltic.calibration import MAX_INITIAL_TEST_DOSE_ML

    assert MAX_INITIAL_TEST_DOSE_ML == 10.0


def test_test_and_calibrate_subparsers_have_no_priming_arguments(cli):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["test", "--controller", "MCU_B", "--pump", "P1", "--ml", "5", "--max-ml", "180"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["calibrate", "--controller", "MCU_B", "--pump", "P1", "--requested-ml", "5", "--chunk-ml", "10"]
        )


def test_only_a_single_pump_can_be_selected_for_prime(cli):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["prime", "--controller", "MCU_B", "--pump", "P1", "P2", "--max-ml", "180"])


# --- Bestätigung -------------------------------------------------------------


def test_wrong_confirmation_causes_no_movement(cli, tmp_path):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _forbid_hardware(cli)

    result = cli.cmd_prime(_args(), input_func=lambda prompt: "conf")  # falsche Phrase fuer prime
    assert result == 1


def test_confirmation_phrase_matches_the_task_example(cli, tmp_path, capsys):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)  # PeristalticSessionLogger() wird schon vor _make_client() gebaut

    _forbid_hardware(cli)  # Bestaetigung allein bewegt noch keine Pumpe -> anschliessend wird verlangt
    with pytest.raises(_HardwareTouchedError):
        cli.cmd_prime(
            _args(controller="MCU_B", pump="P2", max_ml=180.0),
            input_func=lambda prompt: "PRIME MCU_B P2 180",
        )
    output = capsys.readouterr().out
    assert "Zum Fortfahren exakt eingeben: PRIME MCU_B P2 180" in output


# --- Voller Ablauf: STATUS->STOPALL->STATUS, Teilauftraege, Fortschritt --------


def test_150_ml_are_split_into_15_chunks_of_10_and_all_are_sent(cli, tmp_path, capsys):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    port = FakeSerialPort(_AutoCompletingFakeMcu())
    _install_fake_client(cli, port)

    result = cli.cmd_prime(_args(max_ml=150.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 150.0))

    assert result == 0
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 15
    assert all(line == "DOSE P2 10.000" for line in dose_lines)

    output = capsys.readouterr().out
    assert "Priming-Fortschritt: 40.0 / 150.0 ml" in output
    assert "Priming abgeschlossen: 150.0 / 150.0 ml." in output


def test_155_ml_are_split_into_15_of_10_plus_1_of_5(cli, tmp_path):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    port = FakeSerialPort(_AutoCompletingFakeMcu())
    _install_fake_client(cli, port)

    result = cli.cmd_prime(_args(max_ml=155.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 155.0))

    assert result == 0
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 16
    assert dose_lines[:15] == ["DOSE P2 10.000"] * 15
    assert dose_lines[15] == "DOSE P2 5.000"


def test_status_stopall_status_runs_before_the_first_chunk(cli, tmp_path):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    port = FakeSerialPort(_AutoCompletingFakeMcu())
    _install_fake_client(cli, port)

    cli.cmd_prime(_args(max_ml=20.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 20.0))

    commands = _written_commands(port)
    # open()-Handshake: STOPALL, PING, STATUS - danach die explizite
    # Sicherstellung vor dem ersten Teilauftrag: STATUS, STOPALL, STATUS.
    assert commands[:6] == ["STOPALL", "PING", "STATUS", "STATUS", "STOPALL", "STATUS"]
    assert commands[6] == "DOSE"


def test_next_chunk_only_starts_after_done_and_idle_status_check(cli, tmp_path):
    """FakeMcu meldet nach dem ersten (ansonsten erfolgreichen) DOSE die
    Pumpe faelschlich weiter als BUSY - cmd_prime muss daraufhin sofort
    abbrechen, KEINEN zweiten Teilauftrag senden."""
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    mcu = _McuThatMisreportsBusyAfterOneDose(misreport_after_dose_number=1)
    port = FakeSerialPort(mcu)
    _install_fake_client(cli, port)

    result = cli.cmd_prime(_args(max_ml=50.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 50.0))

    assert result == 1
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 1  # kein zweiter Teilauftrag


def test_error_in_one_chunk_prevents_all_further_chunks(cli, tmp_path):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    mcu = _McuFailingOnNthDose(fail_on_dose_number=4)
    port = FakeSerialPort(mcu)
    _install_fake_client(cli, port)

    result = cli.cmd_prime(_args(max_ml=150.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 150.0))

    assert result == 1
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 4  # 3 erfolgreiche + der fehlgeschlagene 4., danach Schluss
    # best-effort STOPALL nach dem Fehler:
    assert "STOPALL" in _written_commands(port)[-3:]


def test_ctrl_c_triggers_best_effort_stopall_and_reports_progress(cli, tmp_path, capsys):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    port = FakeSerialPort(_AutoCompletingFakeMcu())
    _install_fake_client(cli, port, interrupt_after_n_doses=4)

    result = cli.cmd_prime(_args(max_ml=150.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 150.0))

    assert result == 130
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 3  # der 4. dose()-Aufruf wurde von KeyboardInterrupt abgefangen, nie gesendet
    assert "STOPALL" in _written_commands(port)[-3:]

    output = capsys.readouterr().out
    assert "30.0" in output  # bereits angeforderte Gesamtmenge (3 x 10 ml) wird angezeigt
    assert "ABBRUCH" in output


# --- Datenhaltung: keine Kalibrierdatei, kein candidate_ml_per_step -------------


def test_prime_does_not_touch_the_calibration_file(cli, tmp_path):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    _redirect_session_logs(cli, tmp_path)
    calibration_path = tmp_path / "peristaltic_calibration.json"
    cli.CALIBRATION_DATA_PATH = calibration_path
    port = FakeSerialPort(_AutoCompletingFakeMcu())
    _install_fake_client(cli, port)

    result = cli.cmd_prime(_args(max_ml=20.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 20.0))

    assert result == 0
    assert not calibration_path.exists()


def test_prime_log_rows_are_marked_as_priming_not_calibration(cli, tmp_path):
    _setup_confirmed_profile_and_mapping(cli, tmp_path)
    log_dir = tmp_path / "logs"

    captured: dict[str, RealSessionLogger] = {}

    def _factory(command: str) -> RealSessionLogger:
        logger = RealSessionLogger(command, log_dir=log_dir)
        captured["logger"] = logger
        return logger

    cli.PeristalticSessionLogger = _factory
    port = FakeSerialPort(_AutoCompletingFakeMcu())
    _install_fake_client(cli, port)

    result = cli.cmd_prime(_args(max_ml=20.0, chunk_ml=10.0), input_func=_confirm("MCU_B", "P2", 20.0))
    assert result == 0

    logger = captured["logger"]
    with logger.csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 2  # zwei Teilauftraege (2 x 10 ml)
    for row in rows:
        assert row["operation"] == "prime"
        assert row["requested_max_ml"] == "20.0"
        assert row["chunk_ml"] == "10.0"
        assert row["candidate_ml_per_step"] == ""
        assert row["firmware_ml_per_step_used"] == ""

    assert rows[0]["completion_reason"] == ""       # nur der letzte Teilauftrag traegt den Abschlussgrund
    assert rows[-1]["completion_reason"] == "completed"
    assert rows[-1]["completed_ml"] == "20.0"
