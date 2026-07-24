"""
process/pump_prime.py::PumpPrimeController - erste Dashboard-Anbindung der
Peristaltik-Prime-Funktion. Hardwarefrei über tests/fake_mcu.py; Mapping/
Firmwareprofil-/Kalibrierdateien UND Log-Verzeichnis liegen für jeden Test
in tmp_path (nie die echten config/*.json, calibration_data/*.json oder
logs/peristaltic/ - siehe Projektkonvention "Migrationstests von
Live-Daten entkoppeln").

PumpPrimeController baut auf BackgroundHardwareProcess auf (wie
ManualDrainJog/TankCleaningController) - is_active()/request_stop()/
wait_stopped() kommen von dort und sind bereits an anderer Stelle getestet;
hier geht es um die Prime-eigene Logik: die vier unabhängigen
Sicherheitsgates (Mapping/Verbindung/Firmwareprofil/Kalibrierung),
Mehrpumpen-/Mehr-MCU-Sequenzierung, Fehlerpfade, Session-ID, garantierte
Abschluss-Logeinträge, Status-Form.
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

from fake_mcu import AutoCompletingFakeMcu, FakeSerialPort
from process import pump_prime
from process.pump_prime import PrimePhase, PumpPrimeController, describe_pump_options
from services.peristaltic.calibration import default_calibration_data, load_calibration_data, save_calibration_data
from services.peristaltic.firmware_profiles import (
    ControllerFirmwareProfile,
    PumpFirmwareEntry,
    default_firmware_profiles,
    save_firmware_profiles,
)
from services.peristaltic.models import UNASSIGNED, default_mapping, load_mapping, save_mapping


def _confirmed_controller_profile(pumps=("P1", "P2", "P3", "P4")) -> ControllerFirmwareProfile:
    return ControllerFirmwareProfile(
        status="confirmed",
        profile_id="test-profile",
        microsteps=8,
        speed_rpm=120,
        acceleration_steps_per_s2=2000,
        max_runtime_ms=30000,
        pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=0.000191096) for pump in pumps},
    )


def _setup(
    monkeypatch,
    tmp_path: Path,
    *,
    confirmed_controllers=("MCU_B",),
    calibrated_controllers=None,
    calibrated_status="candidate",
) -> dict[str, str]:
    """
    Bereitet Mapping/Firmwareprofil/Kalibrierdaten in tmp_path vor und
    monkeypatcht die drei Pfad-Konstanten in process.pump_prime. Portpfade
    sind ECHTE (leere) Dateien in tmp_path - die Verbindungsverfügbarkeits-
    Prüfung (Path(entry.port).exists()) ist ein reiner Dateisystem-Stat,
    keine Fake-Bezeichner wie "fake-mcu-a" mehr, die nie als Pfad existieren
    würden. calibrated_controllers default: identisch zu confirmed_controllers
    (in den meisten Tests soll "bestätigtes Profil" auch "kalibriert"
    bedeuten - Tests, die die beiden Gates bewusst auseinanderziehen wollen,
    übergeben calibrated_controllers explizit).

    Rückgabe: {"MCU_A": "<realer Pfad>", "MCU_B": "<realer Pfad>"} - von den
    Aufrufern für serial_factory-Zuordnungen verwendet.
    """
    if calibrated_controllers is None:
        calibrated_controllers = confirmed_controllers

    port_names = {
        "MCU_A": str(tmp_path / "fake-mcu-a"),
        "MCU_B": str(tmp_path / "fake-mcu-b"),
    }
    for port_path in port_names.values():
        Path(port_path).touch()

    profiles = default_firmware_profiles()
    for controller in confirmed_controllers:
        profiles.controllers[controller] = _confirmed_controller_profile()
    profiles_path = tmp_path / "peristaltic_firmware_profiles.json"
    save_firmware_profiles(profiles_path, profiles)

    mapping = default_mapping()
    for controller, port_path in port_names.items():
        mapping.controllers[controller].port = port_path
    mapping_path = tmp_path / "peristaltic_mapping.json"
    save_mapping(mapping_path, mapping)

    calibration_data = default_calibration_data()
    for controller in calibrated_controllers:
        for pump_entry in calibration_data["controllers"][controller].values():
            if pump_entry["role"] != UNASSIGNED:
                pump_entry["status"] = calibrated_status
    calibration_path = tmp_path / "peristaltic_calibration.json"
    save_calibration_data(calibration_path, calibration_data)

    monkeypatch.setattr(pump_prime, "MAPPING_PATH", mapping_path)
    monkeypatch.setattr(pump_prime, "FIRMWARE_PROFILES_PATH", profiles_path)
    monkeypatch.setattr(pump_prime, "CALIBRATION_DATA_PATH", calibration_path)

    return port_names


def _make_controller(
    tmp_path: Path,
    serial_factory,
) -> PumpPrimeController:
    return PumpPrimeController(
        get_sensor_snapshot=lambda: None,
        serial_factory=serial_factory,
        session_log_dir=tmp_path / "logs",
    )


def _make_multi_port_factory(ports_by_name: dict[str, FakeSerialPort], open_order: list[str] | None = None):
    def factory(port_name: str, baudrate: int, timeout: float) -> FakeSerialPort:
        if open_order is not None:
            open_order.append(port_name)
        return ports_by_name[port_name]

    return factory


def _wait_until_finished(controller: PumpPrimeController, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while controller.is_active() and time.monotonic() < deadline:
        time.sleep(0.02)


class _SlowFirstDoseFakeMcu(AutoCompletingFakeMcu):
    """Wie AutoCompletingFakeMcu, aber der N-te DOSE-Aufruf setzt zuerst ein
    started_event (der DOSE-Befehl wurde bereits geschrieben, siehe
    FakeSerialPort.write()) und blockiert dann kurz, bevor er (weiterhin
    erfolgreich) mit DONE abschließt. Der Test wartet deterministisch auf
    started_event, statt sich auf eine geratene time.sleep()-Dauer zu
    verlassen, bevor request_stop() aufgerufen wird - so ist garantiert,
    dass der Hintergrund-Thread nachweislich noch mitten im ersten,
    bereits gesendeten Chunk hängt."""

    def __init__(
        self,
        *args,
        slow_on_dose_number: int = 1,
        delay_seconds: float = 0.3,
        started_event: threading.Event | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._slow_on_dose_number = slow_on_dose_number
        self._delay_seconds = delay_seconds
        self._started_event = started_event
        self._dose_count = 0

    def handle_command(self, line: str) -> list[str]:
        text = line.strip()
        cmd = text.split(" ", 1)[0].upper() if text else ""
        if cmd == "DOSE":
            self._dose_count += 1
            if self._dose_count == self._slow_on_dose_number:
                if self._started_event is not None:
                    self._started_event.set()
                time.sleep(self._delay_seconds)
        return super().handle_command(line)


class _McuFailingOnNthDose(AutoCompletingFakeMcu):
    """DOSE-Aufträge 1..N-1 schließen normal ab, der N-te wird mit
    ERR ... LIMIT_EXCEEDED abgelehnt - simuliert einen Fehler mitten in
    einer Mehrpumpen-Serie."""

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

        return super().handle_command(line)


class _McuThatNeverGoesIdle(AutoCompletingFakeMcu):
    """STOPALL meldet OK, aber P1 bleibt danach laut STATUS weiterhin BUSY -
    simuliert eine Firmware, die die STATUS->STOPALL->STATUS-Sicherstellung
    (ensure_all_idle_before_dose) nicht erfüllt. Muss den Lauf VOR jeder
    einzelnen Pumpenansteuerung mit safety_check_failed abbrechen."""

    def handle_command(self, line: str) -> list[str]:
        text = line.strip()
        cmd = text.split(" ", 1)[0].upper() if text else ""
        lines = super().handle_command(line)
        if cmd == "STOPALL":
            self.busy["P1"] = True
        return lines


# --- Sicherheitsgates (_check_start_preconditions_locked / start) -------------


def test_start_refuses_without_any_pump_selected(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path)
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {}})

    assert result["success"] is False
    assert "Pumpe auswählen" in result["message"]
    assert not controller.is_active()


def test_start_refuses_on_missing_mapping_file(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pump_prime, "MAPPING_PATH", tmp_path / "does_not_exist.json")
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_B": ["P1"]}})

    assert result["success"] is False
    assert "Mapping" in result["message"]
    assert not controller.is_active()


def test_start_refuses_on_invalid_firmware_profiles_file(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pump_prime, "FIRMWARE_PROFILES_PATH", tmp_path / "does_not_exist.json")
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_B": ["P1"]}})

    assert result["success"] is False
    assert "Firmware" in result["message"]
    assert not controller.is_active()


def test_start_refuses_on_invalid_calibration_data_file(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pump_prime, "CALIBRATION_DATA_PATH", tmp_path / "does_not_exist.json")
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_B": ["P1"]}})

    assert result["success"] is False
    assert "Kalibrierdaten" in result["message"]
    assert not controller.is_active()


def test_start_refuses_for_pump_with_unconfirmed_firmware_profile(tmp_path: Path, monkeypatch) -> None:
    """MCU_A bleibt per default_firmware_profiles() unconfirmed - entspricht
    dem heutigen echten Zustand von config/peristaltic_firmware_profiles.json."""
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",), calibrated_controllers=("MCU_A", "MCU_B"))
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_A": ["P1"]}})

    assert result["success"] is False
    assert "MCU_A / P1" in result["message"]
    assert "Firmware" in result["message"]
    assert not controller.is_active()


def test_start_refuses_for_pump_without_volumetric_calibration(tmp_path: Path, monkeypatch) -> None:
    """Das Firmwareprofil-Gate und das Kalibrierungs-Gate sind unabhängig:
    MCU_A hier mit bestätigtem Firmwareprofil, aber OHNE Kalibrierung
    (calibrated_controllers bewusst leer gelassen) - muss trotzdem
    blockieren, mit einer eindeutig als Kalibrierungsproblem erkennbaren
    Meldung, nicht mit der Firmwareprofil-Meldung."""
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"), calibrated_controllers=())
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_A": ["P1"]}})

    assert result["success"] is False
    assert "MCU_A / P1" in result["message"]
    assert "volumetrische Kalibrierung" in result["message"]
    assert not controller.is_active()


def test_start_succeeds_when_mcu_a_is_fully_confirmed_and_calibrated(tmp_path: Path, monkeypatch) -> None:
    """Beweist, dass keines der vier Gates MCU-hartkodiert ist - sobald
    MCU_A Mapping+Verbindung+bestätigtes Profil+Kalibrierung hat, wird es
    genauso freigegeben wie MCU_B."""
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))
    port = FakeSerialPort(AutoCompletingFakeMcu())
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_A"]: port})
    )

    result = controller.start({"pumps": {"MCU_A": ["P1"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0})
    assert result["success"] is True

    _wait_until_finished(controller)
    status = controller.get_status()
    assert status["phase"] == PrimePhase.FINISHED
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 2


def test_start_refuses_for_unassigned_pump(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_A": ["P3"]}})  # P3/P4 sind per default_mapping() UNASSIGNED

    assert result["success"] is False
    assert "MCU_A / P3" in result["message"]
    assert not controller.is_active()


def test_start_refuses_when_port_path_does_not_exist(tmp_path: Path, monkeypatch) -> None:
    """Verbindungsverfügbarkeit ist ein eigenständiges Gate, unabhängig
    von einem strukturell gültigen Mapping: der Port ist zugeordnet, aber
    die Gerätedatei existiert (gerade) nicht."""
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    mapping = load_mapping(pump_prime.MAPPING_PATH)
    mapping.controllers["MCU_B"].port = str(tmp_path / "not-actually-plugged-in")
    save_mapping(pump_prime.MAPPING_PATH, mapping)

    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    result = controller.start({"pumps": {"MCU_B": ["P1"]}})

    assert result["success"] is False
    assert "Verbindung zu MCU_B" in result["message"]
    assert not controller.is_active()


# --- Mehrpumpen- / Mehr-MCU-Sequenzierung -------------------------------------


def test_full_run_all_four_mcu_b_pumps_in_order(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    port = FakeSerialPort(AutoCompletingFakeMcu())
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    result = controller.start(
        {"pumps": {"MCU_B": ["P1", "P2", "P3", "P4"]}, "max_ml_per_pump": 150.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    _wait_until_finished(controller)
    status = controller.get_status()

    assert status["phase"] == PrimePhase.FINISHED
    assert len(status["pump_results"]) == 4
    assert [r["pump"] for r in status["pump_results"]] == ["P1", "P2", "P3", "P4"]
    assert all(r["completion_reason"] == "completed" for r in status["pump_results"])
    assert all(r["completed_ml"] == 150.0 for r in status["pump_results"])

    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 60  # 4 Pumpen x 15 Teilaufträge
    for pump in ("P1", "P2", "P3", "P4"):
        assert dose_lines.count(f"DOSE {pump} 10.000") == 15


def test_two_mcu_run_processes_mcu_a_completely_before_mcu_b(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))
    port_a = FakeSerialPort(AutoCompletingFakeMcu())
    port_b = FakeSerialPort(AutoCompletingFakeMcu())
    open_order: list[str] = []
    controller = _make_controller(
        tmp_path,
        _make_multi_port_factory(
            {port_names["MCU_A"]: port_a, port_names["MCU_B"]: port_b}, open_order=open_order
        ),
    )

    result = controller.start(
        {"pumps": {"MCU_A": ["P1"], "MCU_B": ["P2"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    _wait_until_finished(controller)
    status = controller.get_status()

    assert status["phase"] == PrimePhase.FINISHED
    assert open_order == [port_names["MCU_A"], port_names["MCU_B"]]
    assert [r["controller"] for r in status["pump_results"]] == ["MCU_A", "MCU_B"]

    assert len([line for line in port_a.written_lines if line.startswith("DOSE")]) == 2
    assert len([line for line in port_b.written_lines if line.startswith("DOSE")]) == 2


def test_request_stop_mid_run_halts_quickly_without_error_phase(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    dose_in_flight = threading.Event()
    mcu = _SlowFirstDoseFakeMcu(slow_on_dose_number=1, delay_seconds=0.5, started_event=dose_in_flight)
    port = FakeSerialPort(mcu)
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    result = controller.start(
        {"pumps": {"MCU_B": ["P1", "P2"]}, "max_ml_per_pump": 150.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    # Deterministisch warten, bis der erste DOSE-Befehl nachweislich bereits
    # geschrieben wurde und der Hintergrund-Thread noch mitten in dessen
    # (künstlich verzögerter) Bearbeitung hängt - keine geratene Wartezeit.
    assert dose_in_flight.wait(timeout=2.0)
    controller.request_stop(reason="test_stop")
    stop_result = controller.wait_stopped(timeout=3.0)

    assert stop_result["success"] is True
    assert not controller.is_active()

    status = controller.get_status()
    assert status["phase"] != PrimePhase.ERROR
    assert status["stop_reason"] == "test_stop"

    # Kein weiterer Chunk und keine weitere Pumpe nach der Stop-Anforderung:
    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 1  # gestoppt direkt nach dem ersten (bereits laufenden) Chunk
    assert all(line == "DOSE P1 10.000" for line in dose_lines)  # P2 nie angesteuert
    assert "STOPALL" in port.written_lines[-3:]


def test_request_stop_prevents_opening_the_next_mcu(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))
    dose_in_flight = threading.Event()
    mcu_a = _SlowFirstDoseFakeMcu(slow_on_dose_number=1, delay_seconds=0.5, started_event=dose_in_flight)
    port_a = FakeSerialPort(mcu_a)
    port_b = FakeSerialPort(AutoCompletingFakeMcu())
    open_order: list[str] = []
    controller = _make_controller(
        tmp_path,
        _make_multi_port_factory(
            {port_names["MCU_A"]: port_a, port_names["MCU_B"]: port_b}, open_order=open_order
        ),
    )

    result = controller.start(
        {"pumps": {"MCU_A": ["P1"], "MCU_B": ["P2"]}, "max_ml_per_pump": 150.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    assert dose_in_flight.wait(timeout=2.0)
    controller.request_stop(reason="test_stop")
    controller.wait_stopped(timeout=3.0)

    assert open_order == [port_names["MCU_A"]]  # MCU_B wurde nie geöffnet
    assert not any(line.startswith("DOSE") for line in port_b.written_lines)


def test_failure_on_second_pump_stops_remaining_pumps_and_sets_error_phase(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    # 150 ml / 10 ml chunk_ml = 15 Teilauftraege je Pumpe - der 16. DOSE-
    # Aufruf ist damit exakt der erste Teilauftrag der zweiten Pumpe (P2).
    mcu = _McuFailingOnNthDose(fail_on_dose_number=16)
    port = FakeSerialPort(mcu)
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    result = controller.start(
        {"pumps": {"MCU_B": ["P1", "P2", "P3", "P4"]}, "max_ml_per_pump": 150.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    _wait_until_finished(controller)
    status = controller.get_status()

    assert status["phase"] == PrimePhase.ERROR
    assert status["last_error"]

    dose_lines = [line for line in port.written_lines if line.startswith("DOSE")]
    assert len(dose_lines) == 16  # 15 fuer P1 + der fehlgeschlagene erste Versuch fuer P2
    assert len(status["pump_results"]) == 2  # P1 (completed) + P2 (error) - P3/P4 nie versucht
    assert status["pump_results"][0]["completion_reason"] == "completed"
    assert status["pump_results"][1]["completion_reason"] == "error"


# --- Garantierte Abschluss-Logeinträge (auch ohne abgeschlossenen Chunk) ------


def test_client_open_failure_logs_connection_error_for_every_selected_pump(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    port = FakeSerialPort(AutoCompletingFakeMcu())
    port.fail_next_write()  # laesst den allerersten Schreibversuch in client.open() fehlschlagen
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    result = controller.start(
        {"pumps": {"MCU_B": ["P1", "P2"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    _wait_until_finished(controller)
    status = controller.get_status()

    assert status["phase"] == PrimePhase.ERROR
    assert len(status["pump_results"]) == 2  # beide ausgewählten Pumpen bekommen einen terminalen Eintrag
    assert {r["pump"] for r in status["pump_results"]} == {"P1", "P2"}
    assert all(r["completion_reason"] == "connection_error" for r in status["pump_results"])
    assert not any(line.startswith("DOSE") for line in port.written_lines)


def test_ensure_all_idle_before_dose_failure_logs_safety_check_failed(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    port = FakeSerialPort(_McuThatNeverGoesIdle())
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    result = controller.start(
        {"pumps": {"MCU_B": ["P1"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True

    _wait_until_finished(controller)
    status = controller.get_status()

    assert status["phase"] == PrimePhase.ERROR
    assert len(status["pump_results"]) == 1
    assert status["pump_results"][0]["pump"] == "P1"
    assert status["pump_results"][0]["completion_reason"] == "safety_check_failed"
    # Keine Pumpe wurde je angesteuert - der Fehlschlag geschah in
    # ensure_all_idle_before_dose(), vor jedem DOSE-Aufruf.
    assert not any(line.startswith("DOSE") for line in port.written_lines)


def test_emergency_stop_reason_logs_as_emergency_stop_not_user_abort(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    dose_in_flight = threading.Event()
    mcu = _SlowFirstDoseFakeMcu(slow_on_dose_number=1, delay_seconds=0.5, started_event=dose_in_flight)
    port = FakeSerialPort(mcu)
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    result = controller.start({"pumps": {"MCU_B": ["P1"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0})
    assert result["success"] is True

    assert dose_in_flight.wait(timeout=2.0)
    controller.request_stop(reason="emergency_stop")
    controller.wait_stopped(timeout=3.0)

    status = controller.get_status()
    assert status["phase"] != PrimePhase.ERROR
    assert status["stop_reason"] == "emergency_stop"
    assert len(status["pump_results"]) == 1
    assert status["pump_results"][0]["completion_reason"] == "emergency_stop"


# --- Session-ID ---------------------------------------------------------------


def test_shared_session_id_across_all_mcu_loggers_of_one_run(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))
    port_a = FakeSerialPort(AutoCompletingFakeMcu())
    port_b = FakeSerialPort(AutoCompletingFakeMcu())
    controller = _make_controller(
        tmp_path,
        _make_multi_port_factory({port_names["MCU_A"]: port_a, port_names["MCU_B"]: port_b}),
    )

    result = controller.start(
        {"pumps": {"MCU_A": ["P1"], "MCU_B": ["P2"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0}
    )
    assert result["success"] is True
    _wait_until_finished(controller)

    status = controller.get_status()
    assert status["phase"] == PrimePhase.FINISHED
    session_id = status["session_id"]
    assert session_id is not None

    csv_files = sorted((tmp_path / "logs").rglob("prime_*.csv"))
    assert len(csv_files) == 2

    for csv_path in csv_files:
        with csv_path.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        assert rows
        assert all(row["session_id"] == session_id for row in rows)


def test_two_consecutive_runs_get_different_session_ids(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    port = FakeSerialPort(AutoCompletingFakeMcu())
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    controller.start({"pumps": {"MCU_B": ["P1"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0})
    _wait_until_finished(controller)
    first_session_id = controller.get_status()["session_id"]

    controller.start({"pumps": {"MCU_B": ["P1"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0})
    _wait_until_finished(controller)
    second_session_id = controller.get_status()["session_id"]

    assert first_session_id is not None
    assert second_session_id is not None
    assert first_session_id != second_session_id


def test_get_status_shape(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path)
    controller = _make_controller(tmp_path, pump_prime.default_serial_factory)

    status = controller.get_status()

    for key in (
        "is_active", "phase", "session_id", "current_controller", "current_pump",
        "current_pump_role", "pumps_plan", "pump_results", "max_ml_per_pump",
        "chunk_ml", "started_at", "stop_reason", "last_error", "last_message",
    ):
        assert key in status

    assert status["is_active"] is False
    assert status["phase"] == PrimePhase.IDLE
    assert status["session_id"] is None
    assert status["pump_results"] == []


def test_double_start_while_active_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    port_names = _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    mcu = _SlowFirstDoseFakeMcu(slow_on_dose_number=1, delay_seconds=0.3)
    port = FakeSerialPort(mcu)
    controller = _make_controller(
        tmp_path, _make_multi_port_factory({port_names["MCU_B"]: port})
    )

    first = controller.start({"pumps": {"MCU_B": ["P1"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0})
    assert first["success"] is True

    second = controller.start({"pumps": {"MCU_B": ["P2"]}, "max_ml_per_pump": 20.0, "chunk_ml": 10.0})
    assert second["success"] is True
    assert "bereits" in second["message"]

    controller.request_stop(reason="test_cleanup")
    controller.wait_stopped(timeout=3.0)

    # Die zweite start()-Anforderung (P2) darf nie angenommen worden sein -
    # nur P1s Plan wurde tatsaechlich verwendet.
    assert controller.get_status()["pumps_plan"] == {"MCU_B": ["P1"]}


# --- describe_pump_options() --------------------------------------------------


def test_describe_pump_options_hides_unassigned_and_reports_both_gates_separately(
    tmp_path: Path, monkeypatch
) -> None:
    """MCU_A ist heute real weder kalibriert noch firmwarebestätigt - beide
    Gründe müssen unabhängig als eigene Einträge in blocked_reasons
    erscheinen, nicht gegenseitig ersetzt werden."""
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",), calibrated_controllers=("MCU_B",))

    options = describe_pump_options()

    assert options[("MCU_A", "P3")]["hidden"] is True  # UNASSIGNED per default_mapping()
    assert options[("MCU_A", "P4")]["hidden"] is True

    assert options[("MCU_A", "P1")]["hidden"] is False
    assert options[("MCU_A", "P1")]["blocked"] is True
    reasons = options[("MCU_A", "P1")]["blocked_reasons"]
    assert any("Firmware" in reason for reason in reasons)
    assert any("volumetrische Kalibrierung" in reason for reason in reasons)

    for pump in ("P1", "P2", "P3", "P4"):
        assert options[("MCU_B", pump)]["hidden"] is False
        assert options[("MCU_B", pump)]["blocked"] is False
        assert options[("MCU_B", pump)]["blocked_reasons"] == []


def test_describe_pump_options_never_raises_on_broken_mapping_file(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pump_prime, "MAPPING_PATH", tmp_path / "does_not_exist.json")

    options = describe_pump_options()

    for (controller_id, pump), option in options.items():
        assert option["blocked"] is True
        assert option["blocked_reasons"]


def test_describe_pump_options_reflects_confirmed_and_calibrated_mcu_a(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))

    options = describe_pump_options()

    assert options[("MCU_A", "P1")]["blocked"] is False
    assert options[("MCU_A", "P2")]["blocked"] is False


def test_describe_pump_options_blocks_on_unknown_calibration_status(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",), calibrated_controllers=("MCU_B",))
    calibration_data = load_calibration_data(pump_prime.CALIBRATION_DATA_PATH)
    calibration_data["controllers"]["MCU_B"]["P1"]["status"] = "some_bogus_value"
    save_calibration_data(pump_prime.CALIBRATION_DATA_PATH, calibration_data)

    options = describe_pump_options()

    assert options[("MCU_B", "P1")]["blocked"] is True
    assert any("volumetrische Kalibrierung" in reason for reason in options[("MCU_B", "P1")]["blocked_reasons"])
    # Andere Pumpen an MCU_B bleiben unbeeinflusst:
    assert options[("MCU_B", "P2")]["blocked"] is False


def test_describe_pump_options_accepts_verified_status_too(tmp_path: Path, monkeypatch) -> None:
    _setup(
        monkeypatch, tmp_path, confirmed_controllers=("MCU_B",),
        calibrated_controllers=("MCU_B",), calibrated_status="verified",
    )

    options = describe_pump_options()

    assert options[("MCU_B", "P1")]["blocked"] is False


def test_describe_pump_options_blocks_port_that_does_not_exist(tmp_path: Path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_B",))
    mapping = load_mapping(pump_prime.MAPPING_PATH)
    mapping.controllers["MCU_B"].port = str(tmp_path / "unplugged-device")
    save_mapping(pump_prime.MAPPING_PATH, mapping)

    options = describe_pump_options()

    assert options[("MCU_B", "P1")]["blocked"] is True
    assert any("Verbindung" in reason for reason in options[("MCU_B", "P1")]["blocked_reasons"])


def test_describe_pump_options_never_constructs_a_serial_client(tmp_path: Path, monkeypatch) -> None:
    """UI-Polling (refresh()/update_prime_pump_checklist()) darf nie eine
    serielle Verbindung öffnen - describe_pump_options() ist die dafür
    verwendete Funktion und darf PeristalticSerialClient nirgends
    instanziieren."""
    _setup(monkeypatch, tmp_path, confirmed_controllers=("MCU_A", "MCU_B"))

    def _boom(*args, **kwargs):
        raise AssertionError("describe_pump_options() darf keinen PeristalticSerialClient erzeugen.")

    monkeypatch.setattr(pump_prime, "PeristalticSerialClient", _boom)

    for _ in range(5):
        describe_pump_options()  # muss durchlaufen, ohne die Fake-Klasse je aufzurufen
