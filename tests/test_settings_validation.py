"""
Phase 3 regression tests: services/settings_validation.py, plus the critical
check that all four real, current config/*.json files pass their own
schema - the whole point of adding validation is to catch future mistakes,
not to reject what already works in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.settings_validation import SettingField, SettingsValidationError, validate_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_missing_required_key_is_rejected():
    schema = [SettingField("max_fill_seconds", float)]
    with pytest.raises(SettingsValidationError, match="max_fill_seconds"):
        validate_settings({}, schema, "test")


def test_wrong_type_for_list_field_is_rejected():
    schema = [SettingField("auto_circulation_outputs", list)]
    with pytest.raises(SettingsValidationError, match="Liste"):
        validate_settings({"auto_circulation_outputs": "not_a_list"}, schema, "test")


def test_non_coercible_value_is_rejected():
    schema = [SettingField("max_fill_seconds", float)]
    with pytest.raises(SettingsValidationError, match="float"):
        validate_settings({"max_fill_seconds": "not_a_number"}, schema, "test")


def test_value_out_of_range_is_rejected():
    """This is the 0-samples edge case: EmptyConfirmCounter would confirm
    'empty'/'target reached' on the very first reading if this were allowed."""
    schema = [SettingField("target_reached_confirm_samples", int, min_value=1)]
    with pytest.raises(SettingsValidationError, match=">= 1"):
        validate_settings({"target_reached_confirm_samples": 0}, schema, "test")


def test_disallowed_value_is_rejected():
    schema = [SettingField("fill_mode", str, required=False, default="delta", allowed_values={"delta", "absolute"})]
    with pytest.raises(SettingsValidationError, match="delta"):
        validate_settings({"fill_mode": "sideways"}, schema, "test")


def test_multiple_errors_are_reported_together():
    schema = [
        SettingField("max_fill_seconds", float),
        SettingField("target_reached_confirm_samples", int, min_value=1),
    ]
    with pytest.raises(SettingsValidationError) as exc_info:
        validate_settings({"target_reached_confirm_samples": 0}, schema, "test")

    message = str(exc_info.value)
    assert "max_fill_seconds" in message
    assert "target_reached_confirm_samples" in message
    assert "2 Fehler" in message


def test_valid_settings_pass_and_optional_defaults_are_filled_in():
    schema = [
        SettingField("max_fill_seconds", float, min_value=0.0),
        SettingField("valve_settle_seconds", float, required=False, default=1.0, min_value=0.0),
    ]
    result = validate_settings({"max_fill_seconds": 20.0}, schema, "test")
    assert result["max_fill_seconds"] == 20.0
    assert result["valve_settle_seconds"] == 1.0


def _real_settings_schemas():
    from nicegui_dashboard.process_controller import (
        PROCESS_SETTINGS_SCHEMA,
        TANK_CLEANING_SETTINGS_SCHEMA,
    )
    from process.common import WATER_CYCLE_SETTINGS_SCHEMA
    from calibration_mixing_tank import CALIBRATION_SETTINGS_SCHEMA

    return [
        ("config/process_settings.json", PROCESS_SETTINGS_SCHEMA),
        ("config/water_cycle_settings.json", WATER_CYCLE_SETTINGS_SCHEMA),
        ("config/tank_cleaning_settings.json", TANK_CLEANING_SETTINGS_SCHEMA),
        ("config/calibration_settings.json", CALIBRATION_SETTINGS_SCHEMA),
    ]


@pytest.mark.parametrize("path_str,schema", _real_settings_schemas(), ids=lambda v: v if isinstance(v, str) else "")
def test_real_production_settings_files_pass_their_schema(path_str, schema):
    path = REPO_ROOT / path_str
    with path.open(encoding="utf-8") as file:
        settings = json.load(file)

    validate_settings(settings, schema, path_str)  # raises on failure


def test_broken_copy_missing_key_is_rejected():
    from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA

    with (REPO_ROOT / "config/process_settings.json").open(encoding="utf-8") as file:
        real_settings = json.load(file)

    broken = dict(real_settings)
    del broken["no_fill_progress_timeout_seconds"]

    with pytest.raises(SettingsValidationError):
        validate_settings(broken, PROCESS_SETTINGS_SCHEMA, "config/process_settings.json (broken copy)")


def test_broken_copy_wrong_type_is_rejected():
    from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA

    with (REPO_ROOT / "config/process_settings.json").open(encoding="utf-8") as file:
        real_settings = json.load(file)

    broken = dict(real_settings)
    broken["max_fill_seconds"] = "zwanzig"

    with pytest.raises(SettingsValidationError):
        validate_settings(broken, PROCESS_SETTINGS_SCHEMA, "config/process_settings.json (broken copy)")
