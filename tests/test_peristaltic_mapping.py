"""
services/peristaltic/models.py: Mapping-Schema-Validierung
(validate_mapping_dict), die default_mapping()-Factory und die
atomare JSON-Persistenz (load_mapping/save_mapping).
"""

from __future__ import annotations

import json

import pytest

from services.peristaltic.models import (
    REQUIRED_BAUDRATE,
    MappingValidationError,
    default_mapping,
    load_mapping,
    mapping_to_dict,
    save_mapping,
    validate_mapping_dict,
)


def _valid_dict() -> dict:
    return mapping_to_dict(default_mapping())


# --- default_mapping() ---------------------------------------------------------


def test_default_mapping_has_null_ports_and_required_baudrate():
    mapping = default_mapping()
    for controller in ("MCU_A", "MCU_B"):
        entry = mapping.controllers[controller]
        assert entry.port is None
        assert entry.baudrate == REQUIRED_BAUDRATE


def test_default_mapping_matches_target_chemical_roles():
    mapping = default_mapping()
    assert mapping.controllers["MCU_A"].role == "PH"
    assert mapping.controllers["MCU_A"].pumps == {
        "P1": "ph_acid", "P2": "ph_base", "P3": "unassigned", "P4": "unassigned"
    }
    assert mapping.controllers["MCU_B"].role == "EC"
    assert mapping.controllers["MCU_B"].pumps == {
        "P1": "nutrient_a_1", "P2": "nutrient_a_2", "P3": "nutrient_b_1", "P4": "nutrient_b_2"
    }


def test_default_mapping_is_not_a_shared_mutable_singleton():
    """Regressionstest (Zusatzregel 2 bei Freigabe): default_mapping() muss
    bei jedem Aufruf frische, unabhaengige verschachtelte Dicts liefern."""
    first = default_mapping()
    first.controllers["MCU_A"].pumps["P1"] = "mutated_value"
    first.controllers["MCU_A"].port = "/dev/ttyFAKE"

    second = default_mapping()
    assert second.controllers["MCU_A"].pumps["P1"] == "ph_acid"
    assert second.controllers["MCU_A"].port is None


def test_default_mapping_passes_its_own_validation():
    assert validate_mapping_dict(_valid_dict()) == []


# --- validate_mapping_dict(): Struktur ------------------------------------------


def test_missing_controller_is_rejected():
    data = _valid_dict()
    del data["controllers"]["MCU_A"]
    errors = validate_mapping_dict(data)
    assert any("MCU_A" in e for e in errors)


def test_unknown_extra_controller_is_rejected():
    data = _valid_dict()
    data["controllers"]["MCU_C"] = data["controllers"]["MCU_B"]
    errors = validate_mapping_dict(data)
    assert any("MCU_C" in e for e in errors)


def test_missing_pump_key_is_rejected():
    data = _valid_dict()
    del data["controllers"]["MCU_A"]["pumps"]["P3"]
    errors = validate_mapping_dict(data)
    assert any("P3" in e for e in errors)


def test_unexpected_pump_key_is_rejected():
    data = _valid_dict()
    data["controllers"]["MCU_A"]["pumps"]["P5"] = "unassigned"
    errors = validate_mapping_dict(data)
    assert any("P5" in e for e in errors)


def test_duplicate_chemical_role_across_controllers_is_rejected():
    data = _valid_dict()
    data["controllers"]["MCU_A"]["pumps"]["P3"] = "nutrient_a_1"  # bereits auf MCU_B.P1 vergeben
    errors = validate_mapping_dict(data)
    assert any("nutrient_a_1" in e for e in errors)


def test_unassigned_role_may_repeat_freely():
    data = _valid_dict()
    data["controllers"]["MCU_B"]["pumps"]["P3"] = "unassigned"
    data["controllers"]["MCU_B"]["pumps"]["P4"] = "unassigned"
    # MCU_A hat P3/P4 ohnehin schon "unassigned" - insgesamt 4x "unassigned", kein Fehler
    errors = validate_mapping_dict(data)
    assert errors == []


def test_port_must_be_null_or_string():
    data = _valid_dict()
    data["controllers"]["MCU_A"]["port"] = 42
    errors = validate_mapping_dict(data)
    assert any("port" in e for e in errors)


def test_port_string_is_accepted():
    data = _valid_dict()
    data["controllers"]["MCU_A"]["port"] = "/dev/serial/by-id/usb-Arduino_x-if00"
    assert validate_mapping_dict(data) == []


@pytest.mark.parametrize("bad_baudrate", [True, False, "115200", 115200.0, 9600, 0])
def test_baudrate_must_be_exactly_required_value(bad_baudrate):
    data = _valid_dict()
    data["controllers"]["MCU_B"]["baudrate"] = bad_baudrate
    errors = validate_mapping_dict(data)
    assert any("baudrate" in e for e in errors)


def test_multiple_violations_are_all_collected_together():
    data = _valid_dict()
    del data["controllers"]["MCU_A"]["pumps"]["P3"]
    data["controllers"]["MCU_B"]["baudrate"] = 9600
    data["controllers"]["MCU_A"]["port"] = 3.14
    errors = validate_mapping_dict(data)
    assert len(errors) >= 3


def test_non_dict_input_is_rejected():
    assert validate_mapping_dict("not a dict") != []
    assert validate_mapping_dict(None) != []


# --- Persistenz -----------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "peristaltic_mapping.json"
    mapping = default_mapping()
    mapping.controllers["MCU_A"].port = "/dev/serial/by-id/usb-Arduino_A"
    save_mapping(path, mapping)

    loaded = load_mapping(path)
    assert loaded.controllers["MCU_A"].port == "/dev/serial/by-id/usb-Arduino_A"
    assert loaded.controllers["MCU_B"].pumps == mapping.controllers["MCU_B"].pumps


def test_save_creates_backup_on_second_save(tmp_path):
    path = tmp_path / "peristaltic_mapping.json"
    save_mapping(path, default_mapping())

    updated = default_mapping()
    updated.controllers["MCU_B"].port = "/dev/serial/by-id/usb-Arduino_B"
    save_mapping(path, updated)

    backup_path = path.with_suffix(path.suffix + ".bak")
    assert backup_path.exists()


def test_save_rejects_invalid_mapping(tmp_path):
    path = tmp_path / "peristaltic_mapping.json"
    mapping = default_mapping()
    mapping.controllers["MCU_A"].baudrate = 9600
    with pytest.raises(MappingValidationError):
        save_mapping(path, mapping)
    assert not path.exists()


def test_load_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(MappingValidationError):
        load_mapping(tmp_path / "does_not_exist.json")


def test_load_corrupted_json_fails_closed(tmp_path):
    path = tmp_path / "peristaltic_mapping.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(MappingValidationError):
        load_mapping(path)


def test_load_invalid_mapping_fails_closed(tmp_path):
    path = tmp_path / "peristaltic_mapping.json"
    data = _valid_dict()
    del data["controllers"]["MCU_B"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MappingValidationError):
        load_mapping(path)
