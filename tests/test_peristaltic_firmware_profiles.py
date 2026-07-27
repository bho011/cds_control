"""
services/peristaltic/firmware_profiles.py: Firmwareprofil-Schema-Validierung
(validate_firmware_profiles_dict), die default_firmware_profiles()-Factory,
die atomare JSON-Persistenz (load_firmware_profiles/save_firmware_profiles)
und die fail-closed Auflösung (resolve_firmware_ml_per_step) für
scripts/peristaltic_calibration_cli.py::cmd_calibrate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.peristaltic.calibration import FIRMWARE_DEFAULT_ML_PER_STEP
from services.peristaltic.firmware_profiles import (
    ControllerFirmwareProfile,
    FirmwareProfileError,
    FirmwareProfiles,
    PumpFirmwareEntry,
    default_firmware_profiles,
    firmware_profiles_to_dict,
    load_firmware_profiles,
    resolve_firmware_ml_per_step,
    save_firmware_profiles,
    validate_firmware_profiles_dict,
)

REPO_FIRMWARE_PROFILES_PATH = Path(__file__).resolve().parent.parent / "config" / "peristaltic_firmware_profiles.json"


def _valid_dict() -> dict:
    return firmware_profiles_to_dict(default_firmware_profiles())


def _confirmed_mcu_b_dict(ml_per_step: float = 0.000191096) -> dict:
    data = _valid_dict()
    data["controllers"]["MCU_B"] = {
        "status": "confirmed",
        "profile_id": "mcu_b_ec_8_microsteps_v1",
        "microsteps": 8,
        "speed_rpm": 120,
        "acceleration_steps_per_s2": 2000,
        "max_runtime_ms": 30000,
        "pumps": {pump: {"firmware_ml_per_step": ml_per_step} for pump in ("P1", "P2", "P3", "P4")},
    }
    return data


# --- default_firmware_profiles() -----------------------------------------------


def test_default_firmware_profiles_are_unconfirmed_with_null_values():
    profiles = default_firmware_profiles()
    for controller_id in ("MCU_A", "MCU_B"):
        controller = profiles.controllers[controller_id]
        assert controller.status == "unconfirmed"
        assert controller.profile_id is None
        assert controller.microsteps is None
        assert controller.speed_rpm is None
        assert controller.acceleration_steps_per_s2 is None
        assert controller.max_runtime_ms is None
        for pump in ("P1", "P2", "P3", "P4"):
            assert controller.pumps[pump].firmware_ml_per_step is None


def test_default_firmware_profiles_is_not_a_shared_mutable_singleton():
    first = default_firmware_profiles()
    first.controllers["MCU_A"].status = "confirmed"
    first.controllers["MCU_A"].pumps["P1"].firmware_ml_per_step = 0.5

    second = default_firmware_profiles()
    assert second.controllers["MCU_A"].status == "unconfirmed"
    assert second.controllers["MCU_A"].pumps["P1"].firmware_ml_per_step is None


# --- validate_firmware_profiles_dict() ------------------------------------------


def test_default_dict_round_trip_is_valid():
    assert validate_firmware_profiles_dict(_valid_dict()) == []


def test_confirmed_mcu_b_example_from_task_is_valid():
    assert validate_firmware_profiles_dict(_confirmed_mcu_b_dict()) == []


def test_non_dict_payload_is_rejected():
    assert validate_firmware_profiles_dict("not a dict") != []
    assert validate_firmware_profiles_dict(None) != []
    assert validate_firmware_profiles_dict([1, 2, 3]) != []


def test_bad_schema_version_type_is_rejected():
    data = _valid_dict()
    data["schema_version"] = "1"
    errors = validate_firmware_profiles_dict(data)
    assert any("schema_version" in message for message in errors)


def test_schema_version_true_is_rejected_not_treated_as_1():
    data = _valid_dict()
    data["schema_version"] = True
    errors = validate_firmware_profiles_dict(data)
    assert any("schema_version" in message for message in errors)


def test_future_schema_version_is_rejected():
    data = _valid_dict()
    data["schema_version"] = 2
    errors = validate_firmware_profiles_dict(data)
    assert any("schema_version" in message and "2" in message for message in errors)


def test_missing_controller_is_rejected():
    data = _valid_dict()
    del data["controllers"]["MCU_A"]
    errors = validate_firmware_profiles_dict(data)
    assert any("Fehlende Controller" in message for message in errors)


def test_unknown_controller_is_rejected():
    data = _valid_dict()
    data["controllers"]["MCU_C"] = data["controllers"]["MCU_A"]
    errors = validate_firmware_profiles_dict(data)
    assert any("Unbekannte Controller" in message for message in errors)


@pytest.mark.parametrize("bad_status", ["Confirmed", "CONFIRMED", "", None, 1, True])
def test_bad_status_value_is_rejected(bad_status):
    data = _valid_dict()
    data["controllers"]["MCU_A"]["status"] = bad_status
    errors = validate_firmware_profiles_dict(data)
    assert any("status" in message for message in errors)


@pytest.mark.parametrize("field", ["microsteps", "speed_rpm", "acceleration_steps_per_s2", "max_runtime_ms"])
def test_negative_or_zero_numeric_field_is_rejected(field):
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"][field] = 0
    errors = validate_firmware_profiles_dict(data)
    assert any(field in message for message in errors)

    data2 = _confirmed_mcu_b_dict()
    data2["controllers"]["MCU_B"][field] = -5
    errors2 = validate_firmware_profiles_dict(data2)
    assert any(field in message for message in errors2)


@pytest.mark.parametrize("field", ["microsteps", "speed_rpm", "acceleration_steps_per_s2", "max_runtime_ms"])
def test_float_numeric_field_is_rejected_even_as_whole_number(field):
    """Strikt wie baudrate in models.py::validate_mapping_dict - type(x) is
    int, kein bool, keine "eigentlich ganzzahligen" Floats."""
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"][field] = 8.0
    errors = validate_firmware_profiles_dict(data)
    assert any(field in message for message in errors)


@pytest.mark.parametrize("field", ["microsteps", "speed_rpm", "acceleration_steps_per_s2", "max_runtime_ms"])
def test_bool_numeric_field_is_rejected(field):
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"][field] = True
    errors = validate_firmware_profiles_dict(data)
    assert any(field in message for message in errors)


def test_null_numeric_fields_are_valid_for_unconfirmed_controller():
    assert validate_firmware_profiles_dict(_valid_dict()) == []


def test_empty_profile_id_is_rejected():
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"]["profile_id"] = ""
    errors = validate_firmware_profiles_dict(data)
    assert any("profile_id" in message for message in errors)


def test_missing_pump_is_rejected():
    data = _confirmed_mcu_b_dict()
    del data["controllers"]["MCU_B"]["pumps"]["P4"]
    errors = validate_firmware_profiles_dict(data)
    assert any("fehlende Pumpen" in message for message in errors)


def test_unknown_pump_key_is_rejected():
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"]["pumps"]["P5"] = {"firmware_ml_per_step": 0.0001}
    errors = validate_firmware_profiles_dict(data)
    assert any("unbekannte Pumpennummern" in message for message in errors)


def test_missing_firmware_ml_per_step_key_is_rejected():
    data = _confirmed_mcu_b_dict()
    del data["controllers"]["MCU_B"]["pumps"]["P1"]["firmware_ml_per_step"]
    errors = validate_firmware_profiles_dict(data)
    assert any("firmware_ml_per_step" in message for message in errors)


@pytest.mark.parametrize("bad_value", [0, -0.000191096, "0.000191096", True, float("nan"), float("inf")])
def test_bad_firmware_ml_per_step_value_is_rejected(bad_value):
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"]["pumps"]["P1"]["firmware_ml_per_step"] = bad_value
    errors = validate_firmware_profiles_dict(data)
    assert any("firmware_ml_per_step" in message for message in errors)


def test_confirmed_controller_with_some_null_pump_values_is_structurally_valid():
    """status=='confirmed' zwingt NICHT, dass bereits alle 4 Pumpen einen
    Wert haben - ob eine einzelne Pumpe fehlt, ist eine Auflösungsfrage
    (resolve_firmware_ml_per_step), keine Strukturverletzung."""
    data = _confirmed_mcu_b_dict()
    data["controllers"]["MCU_B"]["pumps"]["P4"]["firmware_ml_per_step"] = None
    assert validate_firmware_profiles_dict(data) == []


def test_all_errors_are_collected_not_just_the_first():
    data = _valid_dict()
    del data["controllers"]["MCU_A"]
    data["controllers"]["MCU_B"]["status"] = "wrong"
    data["controllers"]["MCU_B"]["microsteps"] = -1
    errors = validate_firmware_profiles_dict(data)
    assert len(errors) >= 3


# --- Persistenz -------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "peristaltic_firmware_profiles.json"
    profiles = default_firmware_profiles()
    profiles.controllers["MCU_B"] = ControllerFirmwareProfile(
        status="confirmed",
        profile_id="mcu_b_ec_8_microsteps_v1",
        microsteps=8,
        speed_rpm=120,
        acceleration_steps_per_s2=2000,
        max_runtime_ms=30000,
        pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=0.000191096) for pump in ("P1", "P2", "P3", "P4")},
    )
    save_firmware_profiles(path, profiles)

    loaded = load_firmware_profiles(path)
    assert loaded == profiles


def test_save_creates_backup_on_second_save(tmp_path):
    path = tmp_path / "peristaltic_firmware_profiles.json"
    save_firmware_profiles(path, default_firmware_profiles())

    second = default_firmware_profiles()
    second.controllers["MCU_A"].status = "confirmed"
    save_firmware_profiles(path, second)

    backup_path = path.with_suffix(path.suffix + ".bak")
    assert backup_path.exists()


def test_save_never_writes_an_invalid_profile(tmp_path):
    path = tmp_path / "peristaltic_firmware_profiles.json"
    invalid = default_firmware_profiles()
    invalid.controllers["MCU_A"].status = "not-a-real-status"
    with pytest.raises(FirmwareProfileError):
        save_firmware_profiles(path, invalid)
    assert not path.exists()


def test_load_missing_file_raises_fail_closed():
    with pytest.raises(FirmwareProfileError):
        load_firmware_profiles(Path("/nonexistent/peristaltic_firmware_profiles.json"))


def test_load_corrupted_json_fails_closed(tmp_path):
    path = tmp_path / "peristaltic_firmware_profiles.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(FirmwareProfileError):
        load_firmware_profiles(path)


def test_load_structurally_invalid_profile_fails_closed(tmp_path):
    path = tmp_path / "peristaltic_firmware_profiles.json"
    data = _valid_dict()
    del data["controllers"]["MCU_A"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FirmwareProfileError):
        load_firmware_profiles(path)


# --- resolve_firmware_ml_per_step() ---------------------------------------------


def test_resolve_returns_confirmed_value_for_confirmed_controller_and_pump():
    profiles = FirmwareProfiles(
        schema_version=1,
        controllers={
            "MCU_A": default_firmware_profiles().controllers["MCU_A"],
            "MCU_B": ControllerFirmwareProfile(
                status="confirmed",
                profile_id="mcu_b_ec_8_microsteps_v1",
                microsteps=8,
                speed_rpm=120,
                acceleration_steps_per_s2=2000,
                max_runtime_ms=30000,
                pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=0.000191096) for pump in ("P1", "P2", "P3", "P4")},
            ),
        },
    )
    value = resolve_firmware_ml_per_step(profiles, "MCU_B", "P1")
    assert value == 0.000191096
    assert value != FIRMWARE_DEFAULT_ML_PER_STEP


def test_resolve_rejects_unconfirmed_controller():
    profiles = default_firmware_profiles()  # both unconfirmed
    with pytest.raises(FirmwareProfileError):
        resolve_firmware_ml_per_step(profiles, "MCU_A", "P1")


def test_resolve_rejects_null_pump_value_even_when_controller_confirmed():
    profiles = FirmwareProfiles(
        schema_version=1,
        controllers={
            "MCU_A": default_firmware_profiles().controllers["MCU_A"],
            "MCU_B": ControllerFirmwareProfile(
                status="confirmed",
                profile_id="mcu_b_ec_8_microsteps_v1",
                microsteps=8,
                speed_rpm=120,
                acceleration_steps_per_s2=2000,
                max_runtime_ms=30000,
                pumps={
                    "P1": PumpFirmwareEntry(firmware_ml_per_step=0.000191096),
                    "P2": PumpFirmwareEntry(firmware_ml_per_step=0.000191096),
                    "P3": PumpFirmwareEntry(firmware_ml_per_step=0.000191096),
                    "P4": PumpFirmwareEntry(firmware_ml_per_step=None),  # noch nicht einzeln vermessen
                },
            ),
        },
    )
    with pytest.raises(FirmwareProfileError):
        resolve_firmware_ml_per_step(profiles, "MCU_B", "P4")
    # andere Pumpen desselben (confirmed) Controllers bleiben unbetroffen:
    assert resolve_firmware_ml_per_step(profiles, "MCU_B", "P1") == 0.000191096


def test_resolve_rejects_missing_controller():
    profiles = FirmwareProfiles(schema_version=1, controllers={})
    with pytest.raises(FirmwareProfileError):
        resolve_firmware_ml_per_step(profiles, "MCU_B", "P1")


def test_resolve_rejects_missing_pump_entry():
    profiles = FirmwareProfiles(
        schema_version=1,
        controllers={
            "MCU_B": ControllerFirmwareProfile(
                status="confirmed",
                profile_id="x",
                microsteps=8,
                speed_rpm=120,
                acceleration_steps_per_s2=2000,
                max_runtime_ms=30000,
                pumps={},
            ),
        },
    )
    with pytest.raises(FirmwareProfileError):
        resolve_firmware_ml_per_step(profiles, "MCU_B", "P1")


# --- Regressionstest gegen die echte, ausgelieferte Profildatei -----------------


def test_real_repo_profile_resolves_mcu_b_p1_to_the_confirmed_firmware_value():
    profiles = load_firmware_profiles(REPO_FIRMWARE_PROFILES_PATH)
    value = resolve_firmware_ml_per_step(profiles, "MCU_B", "P1")
    assert value == 0.000192624768
    assert value != FIRMWARE_DEFAULT_ML_PER_STEP


def test_real_repo_profile_blocks_mcu_a_because_it_is_unconfirmed():
    profiles = load_firmware_profiles(REPO_FIRMWARE_PROFILES_PATH)
    assert profiles.controllers["MCU_A"].status == "unconfirmed"
    with pytest.raises(FirmwareProfileError):
        resolve_firmware_ml_per_step(profiles, "MCU_A", "P1")
