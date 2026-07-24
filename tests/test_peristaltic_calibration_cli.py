"""
scripts/peristaltic_calibration_cli.py::cmd_calibrate - fail-closed
Auflösung des Firmware-Ist-Werts über config/peristaltic_firmware_profiles.json
(services/peristaltic/firmware_profiles.py). Kein Test des vollen
Hardware-Ablaufs (kein serieller Port wird je geöffnet) - jeder Test hier
beweist stattdessen, dass `cmd_calibrate` bei einem fehlenden/ungültigen
Firmwareprofil VOR jeder Pumpenbewegung abbricht: kein
`PeristalticSerialClient` wird konstruiert, `input_func` (die interaktive
Bestätigung) wird nicht aufgerufen.

Das Skript liegt unter scripts/ (kein Package) und wird daher wie
scripts/migrate_peristaltic_calibration_firmware_value.py per importlib
direkt aus der Datei geladen, nicht importiert.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from services.peristaltic.firmware_profiles import (
    ControllerFirmwareProfile,
    FirmwareProfiles,
    PumpFirmwareEntry,
    default_firmware_profiles,
    save_firmware_profiles,
)

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "peristaltic_calibration_cli.py"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("peristaltic_calibration_cli", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Muss vor exec_module() in sys.modules stehen: die CLI-Datei definiert
    # eigene @dataclass-Klassen (z.B. DiscoveredPort) mit
    # "from __future__ import annotations" - dataclasses löst deren
    # String-Annotationen über sys.modules[cls.__module__] auf.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli() -> ModuleType:
    return _load_cli_module()


class _HardwareTouchedError(AssertionError):
    """Wird geworfen, wenn ein Test einen Pfad erreicht, der nur nach einer
    erfolgreichen Firmwareprofil-Prüfung erreichbar sein dürfte (Hardware-
    Client-Konstruktion oder interaktive Bestätigung)."""


def _forbid_hardware(cli_module: ModuleType) -> None:
    def _boom(*args, **kwargs):
        raise _HardwareTouchedError(
            "PeristalticSerialClient darf hier nicht konstruiert werden - keine Pumpenbewegung erwartet."
        )

    cli_module.PeristalticSerialClient = _boom


def _forbid_input(prompt: str) -> str:
    raise _HardwareTouchedError(f"input_func darf hier nicht aufgerufen werden (prompt={prompt!r}).")


def _confirmed_mcu_b_profiles() -> FirmwareProfiles:
    profiles = default_firmware_profiles()  # MCU_A bleibt unconfirmed/null
    profiles.controllers["MCU_B"] = ControllerFirmwareProfile(
        status="confirmed",
        profile_id="mcu_b_ec_8_microsteps_v1",
        microsteps=8,
        speed_rpm=120,
        acceleration_steps_per_s2=2000,
        max_runtime_ms=30000,
        pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=0.000191096) for pump in ("P1", "P2", "P3", "P4")},
    )
    return profiles


# --- argparse: kein globaler Override-Parameter mehr -----------------------------


def test_calibrate_subcommand_no_longer_accepts_current_ml_per_step_override(cli):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "calibrate", "--controller", "MCU_B", "--pump", "P1",
                "--requested-ml", "5", "--current-ml-per-step", "0.0001",
            ]
        )


def test_firmware_default_ml_per_step_is_not_imported_into_the_cli_module(cli):
    assert not hasattr(cli, "FIRMWARE_DEFAULT_ML_PER_STEP")


# --- fail-closed: calibrate darf ohne bestaetigtes Profil keine Pumpe bewegen ----


def test_calibrate_aborts_before_any_hardware_when_profile_file_is_missing(cli, tmp_path):
    cli.FIRMWARE_PROFILES_PATH = tmp_path / "does_not_exist.json"
    cli.MAPPING_PATH = tmp_path / "also_missing_mapping.json"
    _forbid_hardware(cli)

    args = argparse.Namespace(controller="MCU_B", pump="P1", requested_ml=5.0)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_calibrate(args, input_func=_forbid_input)
    assert exc_info.value.code == 1


def test_calibrate_aborts_before_any_hardware_when_controller_is_unconfirmed(cli, tmp_path):
    profiles_path = tmp_path / "peristaltic_firmware_profiles.json"
    save_firmware_profiles(profiles_path, default_firmware_profiles())  # MCU_A unconfirmed
    cli.FIRMWARE_PROFILES_PATH = profiles_path
    _forbid_hardware(cli)

    args = argparse.Namespace(controller="MCU_A", pump="P1", requested_ml=5.0)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_calibrate(args, input_func=_forbid_input)
    assert exc_info.value.code == 1


def test_calibrate_aborts_before_any_hardware_when_specific_pump_value_is_null(cli, tmp_path):
    """Auch bei einem bestaetigten Controller-Profil darf eine einzelne
    Pumpe mit null-Wert keine Kalibrierung ausloesen - die Pruefung ist
    pro Pumpe, nicht nur pro Controller."""
    profiles = _confirmed_mcu_b_profiles()
    profiles.controllers["MCU_B"].pumps["P4"].firmware_ml_per_step = None
    profiles_path = tmp_path / "peristaltic_firmware_profiles.json"
    save_firmware_profiles(profiles_path, profiles)
    cli.FIRMWARE_PROFILES_PATH = profiles_path
    _forbid_hardware(cli)

    args = argparse.Namespace(controller="MCU_B", pump="P4", requested_ml=5.0)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_calibrate(args, input_func=_forbid_input)
    assert exc_info.value.code == 1


def test_calibrate_aborts_before_any_hardware_when_profile_file_is_corrupted(cli, tmp_path):
    profiles_path = tmp_path / "peristaltic_firmware_profiles.json"
    profiles_path.write_text("{not valid json", encoding="utf-8")
    cli.FIRMWARE_PROFILES_PATH = profiles_path
    _forbid_hardware(cli)

    args = argparse.Namespace(controller="MCU_B", pump="P1", requested_ml=5.0)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_calibrate(args, input_func=_forbid_input)
    assert exc_info.value.code == 1


def test_calibrate_resolves_the_confirmed_value_not_the_old_global_default(cli, tmp_path, capsys):
    """Beweist zwei Dinge in einem Test: (1) bei einem gueltigen,
    bestaetigten Profil wird der korrekte, profilbasierte Wert aufgeloest
    (0.000191096, NICHT der alte globale Default 0.000095548), und (2) die
    Pruefung laesst den Ablauf danach weiterlaufen bis zur naechsten
    fail-closed Stufe (Mapping) - erkennbar an deren eigener Fehlermeldung,
    ohne dass jemals Hardware beruehrt wurde."""
    profiles_path = tmp_path / "peristaltic_firmware_profiles.json"
    save_firmware_profiles(profiles_path, _confirmed_mcu_b_profiles())
    cli.FIRMWARE_PROFILES_PATH = profiles_path
    cli.MAPPING_PATH = tmp_path / "no_mapping_here.json"  # absichtlich fehlend
    _forbid_hardware(cli)

    args = argparse.Namespace(controller="MCU_B", pump="P1", requested_ml=5.0)
    with pytest.raises(SystemExit):
        cli.cmd_calibrate(args, input_func=_forbid_input)

    output = capsys.readouterr().out
    assert "0.000191096" in output
    assert "0.000095548" not in output
    assert "Mapping-Datei fehlt" in output
