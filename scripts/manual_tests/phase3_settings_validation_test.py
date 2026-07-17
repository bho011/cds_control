"""
No-hardware regression test for the Phase 3 settings-validation layer (see the
"Architecture-Hardening-Roadmap" plan, Phase 3).

validate_settings() is pure logic - no threading, no GPIO, no I/O beyond
reading the real *.json settings files, which is safe. This confirms both
directions: the generic mechanism rejects bad input correctly, AND the real,
current production settings files still pass (the whole point of adding
validation is to catch future mistakes, not to break what already works).

Usage:
    cd ~/cds_control && .venv/bin/python3 scripts/manual_tests/phase3_settings_validation_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.settings_validation import SettingField, SettingsValidationError, validate_settings


def test_missing_required_key() -> None:
    print("[TEST] Fehlender Pflicht-Key ...")
    schema = [SettingField("max_fill_seconds", float)]
    try:
        validate_settings({}, schema, "test")
        raise AssertionError("hätte fehlschlagen müssen")
    except SettingsValidationError as exc:
        assert "max_fill_seconds" in str(exc)
        assert "fehlt" in str(exc)
    print("  [OK]")


def test_wrong_type() -> None:
    print("[TEST] Falscher Typ ...")
    schema = [SettingField("auto_circulation_outputs", list)]
    try:
        validate_settings({"auto_circulation_outputs": "not_a_list"}, schema, "test")
        raise AssertionError("hätte fehlschlagen müssen")
    except SettingsValidationError as exc:
        assert "Liste" in str(exc)
    print("  [OK]")


def test_type_coercion_failure() -> None:
    print("[TEST] Nicht-konvertierbarer Wert ...")
    schema = [SettingField("max_fill_seconds", float)]
    try:
        validate_settings({"max_fill_seconds": "not_a_number"}, schema, "test")
        raise AssertionError("hätte fehlschlagen müssen")
    except SettingsValidationError as exc:
        assert "float" in str(exc)
    print("  [OK]")


def test_out_of_range() -> None:
    print("[TEST] Wert außerhalb Min/Max ...")
    schema = [SettingField("target_reached_confirm_samples", int, min_value=1)]
    try:
        validate_settings({"target_reached_confirm_samples": 0}, schema, "test")
        raise AssertionError("hätte fehlschlagen müssen - der 0-Samples-Edge-Case-Bug")
    except SettingsValidationError as exc:
        assert ">= 1" in str(exc)
    print("  [OK]")


def test_allowed_values() -> None:
    print("[TEST] Ungültiger allowed_values-Wert ...")
    schema = [SettingField("fill_mode", str, required=False, default="delta", allowed_values={"delta", "absolute"})]
    try:
        validate_settings({"fill_mode": "sideways"}, schema, "test")
        raise AssertionError("hätte fehlschlagen müssen")
    except SettingsValidationError as exc:
        assert "delta" in str(exc) and "absolute" in str(exc)
    print("  [OK]")


def test_multiple_errors_reported_together() -> None:
    print("[TEST] Mehrere Fehler gleichzeitig in einer Meldung ...")
    schema = [
        SettingField("max_fill_seconds", float),
        SettingField("target_reached_confirm_samples", int, min_value=1),
    ]
    try:
        validate_settings({"target_reached_confirm_samples": 0}, schema, "test")
        raise AssertionError("hätte fehlschlagen müssen")
    except SettingsValidationError as exc:
        message = str(exc)
        assert "max_fill_seconds" in message  # missing
        assert "target_reached_confirm_samples" in message  # out of range
        assert "2 Fehler" in message
    print("  [OK]")


def test_valid_settings_pass_with_defaults_filled_in() -> None:
    print("[TEST] Gültige Settings laufen durch, optionale Defaults werden gefüllt ...")
    schema = [
        SettingField("max_fill_seconds", float, min_value=0.0),
        SettingField("valve_settle_seconds", float, required=False, default=1.0, min_value=0.0),
    ]
    result = validate_settings({"max_fill_seconds": 20.0}, schema, "test")
    assert result["max_fill_seconds"] == 20.0
    assert result["valve_settle_seconds"] == 1.0  # filled in from default
    print("  [OK]")


def test_real_settings_files_pass_their_schema() -> None:
    print("[TEST] Echte, aktuelle Settings-Dateien bestehen ihr jeweiliges Schema ...")

    from nicegui_dashboard.process_controller import (
        PROCESS_SETTINGS_SCHEMA,
        TANK_CLEANING_SETTINGS_SCHEMA,
    )
    from process.common import WATER_CYCLE_SETTINGS_SCHEMA
    from calibration_mixing_tank import CALIBRATION_SETTINGS_SCHEMA

    files_and_schemas = [
        ("config/process_settings.json", PROCESS_SETTINGS_SCHEMA),
        ("config/water_cycle_settings.json", WATER_CYCLE_SETTINGS_SCHEMA),
        ("config/tank_cleaning_settings.json", TANK_CLEANING_SETTINGS_SCHEMA),
        ("config/calibration_settings.json", CALIBRATION_SETTINGS_SCHEMA),
    ]

    for path_str, schema in files_and_schemas:
        path = Path(path_str)
        with path.open(encoding="utf-8") as file:
            settings = json.load(file)

        validate_settings(settings, schema, path_str)  # raises on failure
        print(f"  [OK] {path_str}")


def test_broken_copies_are_rejected() -> None:
    print("[TEST] Absichtlich kaputte Kopien der echten Dateien werden abgelehnt ...")

    from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA

    with Path("config/process_settings.json").open(encoding="utf-8") as file:
        real_settings = json.load(file)

    broken = dict(real_settings)
    del broken["no_fill_progress_timeout_seconds"]
    try:
        validate_settings(broken, PROCESS_SETTINGS_SCHEMA, "config/process_settings.json (broken copy)")
        raise AssertionError("hätte fehlschlagen müssen (Key entfernt)")
    except SettingsValidationError:
        pass

    broken_type = dict(real_settings)
    broken_type["max_fill_seconds"] = "zwanzig"
    try:
        validate_settings(broken_type, PROCESS_SETTINGS_SCHEMA, "config/process_settings.json (broken copy)")
        raise AssertionError("hätte fehlschlagen müssen (falscher Typ)")
    except SettingsValidationError:
        pass

    print("  [OK]")


def main() -> None:
    print("=" * 60)
    print("Phase 3 Settings-Validierung - Regressionstest (keine Hardware)")
    print("=" * 60)

    test_missing_required_key()
    test_wrong_type()
    test_type_coercion_failure()
    test_out_of_range()
    test_allowed_values()
    test_multiple_errors_reported_together()
    test_valid_settings_pass_with_defaults_filled_in()
    test_real_settings_files_pass_their_schema()
    test_broken_copies_are_rejected()

    print()
    print("[RESULT] Alle Phase-3-Validierungstests erfolgreich.")


if __name__ == "__main__":
    main()
