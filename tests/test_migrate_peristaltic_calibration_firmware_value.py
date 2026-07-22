"""
scripts/migrate_peristaltic_calibration_firmware_value.py: einmalige,
gezielte Korrektur von firmware_ml_per_step_used/candidate_ml_per_step für
die MCU_B/P1-Trials, die noch mit dem veralteten globalen Python-Client-
Defaultwert (0.000095548) statt dem tatsächlich geflashten Firmwarewert
(0.000191096, siehe config/peristaltic_firmware_profiles.json) protokolliert
wurden.

Das Skript liegt unter scripts/ (kein Package, kein __init__.py) und wird
daher wie scripts/peristaltic_calibration_cli.py nicht importiert, sondern
per importlib direkt aus der Datei geladen - dasselbe Muster, mit dem auch
das Skript selbst services/ importiert (sys.path.insert auf den
Projekt-Root).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from services.peristaltic.calibration import default_calibration_data, load_calibration_data

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_peristaltic_calibration_firmware_value.py"
_REPO_CALIBRATION_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "calibration_data" / "peristaltic_calibration.json"
)

OLD_VALUE = 0.000095548
NEW_VALUE = 0.000191096


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_peristaltic_calibration_firmware_value", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrate():
    return _load_module()


def _trial(
    timestamp_utc: str,
    requested_ml: float,
    measured_ml: float,
    firmware_ml_per_step_used: float,
    candidate_ml_per_step: float | None = None,
    measurement_method: str | None = "mass_approx",
    water_temperature_c: float | None = None,
) -> dict:
    if candidate_ml_per_step is None:
        candidate_ml_per_step = firmware_ml_per_step_used * measured_ml / requested_ml
    return {
        "timestamp_utc": timestamp_utc,
        "requested_ml": requested_ml,
        "measured_ml": measured_ml,
        "measurement_method": measurement_method,
        "water_temperature_c": water_temperature_c,
        "firmware_ml_per_step_used": firmware_ml_per_step_used,
        "candidate_ml_per_step": candidate_ml_per_step,
    }


def _data_with_mixed_trials() -> dict:
    """Enthält absichtlich Trials, die JEDES Auswahlkriterium einzeln
    verfehlen, plus welche, die alle erfüllen - damit ein Test, der nur
    prüft "es wurde IRGENDETWAS migriert", einen zu weiten Filter nicht
    übersehen würde."""
    data = default_calibration_data()

    # Passt: MCU_B/P1, alter Firmwarewert, im Zeitfenster.
    data["controllers"]["MCU_B"]["P1"]["trials"] = [
        _trial("2026-07-22T09:10:02+00:00", 5.0, 5.04, OLD_VALUE),  # untere Grenze inklusive
        _trial("2026-07-22T09:13:39+00:00", 10.0, 10.08, OLD_VALUE),
        _trial("2026-07-22T09:16:02+00:00", 10.0, 10.08, OLD_VALUE),  # obere Grenze inklusive
        # Falscher Firmwarewert (kein alter Default) - darf nicht angefasst werden.
        _trial("2026-07-22T09:12:00+00:00", 5.0, 5.0, 0.0001900),
        # Außerhalb des Zeitfensters (davor/danach) - darf nicht angefasst werden.
        _trial("2026-07-22T09:10:01+00:00", 5.0, 5.0, OLD_VALUE),
        _trial("2026-07-22T09:16:03+00:00", 5.0, 5.0, OLD_VALUE),
    ]
    data["controllers"]["MCU_B"]["P1"]["status"] = "candidate"

    # Falscher Pump (P2 statt P1) am selben Controller, sonst identische Werte - darf nicht angefasst werden.
    data["controllers"]["MCU_B"]["P2"]["trials"] = [
        _trial("2026-07-22T09:11:00+00:00", 5.0, 5.04, OLD_VALUE),
    ]
    data["controllers"]["MCU_B"]["P2"]["status"] = "candidate"

    # Falscher Controller (MCU_A statt MCU_B) - darf nicht angefasst werden.
    data["controllers"]["MCU_A"]["P1"]["trials"] = [
        _trial("2026-07-22T09:11:00+00:00", 5.0, 5.04, OLD_VALUE),
    ]
    data["controllers"]["MCU_A"]["P1"]["status"] = "candidate"

    return data


# --- find_migration_plan() -------------------------------------------------------


def test_plan_selects_exactly_the_matching_trials(migrate):
    data = _data_with_mixed_trials()
    plan = migrate.find_migration_plan(data)
    matched_timestamps = {change["timestamp_utc"] for change in plan}
    assert matched_timestamps == {
        "2026-07-22T09:10:02+00:00",
        "2026-07-22T09:13:39+00:00",
        "2026-07-22T09:16:02+00:00",
    }


def test_plan_is_empty_when_nothing_matches(migrate):
    data = default_calibration_data()
    assert migrate.find_migration_plan(data) == []


def test_plan_recomputes_candidate_with_new_firmware_value(migrate):
    data = _data_with_mixed_trials()
    plan = migrate.find_migration_plan(data)
    for change in plan:
        expected = NEW_VALUE * change["measured_ml"] / change["requested_ml"]
        assert change["new_candidate_ml_per_step"] == pytest.approx(expected)
        assert change["new_firmware_ml_per_step_used"] == NEW_VALUE


# --- apply_migration_plan() / find_migration_plan() zusammen --------------------


def test_apply_changes_only_the_planned_trials_and_preserves_measurements(migrate):
    data = _data_with_mixed_trials()
    before = copy.deepcopy(data)
    plan = migrate.find_migration_plan(data)

    migrate.apply_migration_plan(data, plan)

    p1_trials_before = before["controllers"]["MCU_B"]["P1"]["trials"]
    p1_trials_after = data["controllers"]["MCU_B"]["P1"]["trials"]
    assert len(p1_trials_before) == len(p1_trials_after)

    changed_timestamps = {change["timestamp_utc"] for change in plan}

    for trial_before, trial_after in zip(p1_trials_before, p1_trials_after):
        # Diese Felder duerfen sich NIE aendern, egal ob der Trial migriert wurde:
        assert trial_after["timestamp_utc"] == trial_before["timestamp_utc"]
        assert trial_after["requested_ml"] == trial_before["requested_ml"]
        assert trial_after["measured_ml"] == trial_before["measured_ml"]
        assert trial_after["measurement_method"] == trial_before["measurement_method"]
        assert trial_after["water_temperature_c"] == trial_before["water_temperature_c"]

        if trial_before["timestamp_utc"] in changed_timestamps:
            assert trial_after["firmware_ml_per_step_used"] == NEW_VALUE
            expected_candidate = NEW_VALUE * trial_after["measured_ml"] / trial_after["requested_ml"]
            assert trial_after["candidate_ml_per_step"] == pytest.approx(expected_candidate)
        else:
            assert trial_after["firmware_ml_per_step_used"] == trial_before["firmware_ml_per_step_used"]
            assert trial_after["candidate_ml_per_step"] == trial_before["candidate_ml_per_step"]


def test_apply_does_not_touch_other_pumps_or_controllers(migrate):
    data = _data_with_mixed_trials()
    before = copy.deepcopy(data)
    plan = migrate.find_migration_plan(data)
    migrate.apply_migration_plan(data, plan)

    assert data["controllers"]["MCU_B"]["P2"] == before["controllers"]["MCU_B"]["P2"]
    assert data["controllers"]["MCU_B"]["P3"] == before["controllers"]["MCU_B"]["P3"]
    assert data["controllers"]["MCU_B"]["P4"] == before["controllers"]["MCU_B"]["P4"]
    assert data["controllers"]["MCU_A"] == before["controllers"]["MCU_A"]


def test_apply_refreshes_pump_head_candidate_from_last_evaluated_group(migrate):
    """Eigene, unverfaelschte Trial-Liste (nicht _data_with_mixed_trials()):
    dort ist der chronologisch letzte Trial absichtlich EIN Trial, der
    knapp AUSSERHALB des Migrationsfensters liegt (Grenzfall-Test fuer den
    Fensterfilter) - fuer diesen Test wuerde das die Aussage verfaelschen,
    welche Gruppe "zuletzt ausgewertet" ist."""
    data = default_calibration_data()
    data["controllers"]["MCU_B"]["P1"]["trials"] = [
        _trial("2026-07-22T09:10:02+00:00", 5.0, 5.04, OLD_VALUE),
        _trial("2026-07-22T09:13:39+00:00", 10.0, 10.08, OLD_VALUE),
        _trial("2026-07-22T09:16:02+00:00", 10.0, 10.08, OLD_VALUE),
    ]

    plan = migrate.find_migration_plan(data)
    migrate.apply_migration_plan(data, plan)

    pump_entry = data["controllers"]["MCU_B"]["P1"]
    # letzter Trial (09:16:02) ist requested_ml=10.0 -> Kandidat muss aus der
    # 10ml-Gruppe bei NEW_VALUE stammen (Median von 10.08 und 10.08 -> exakt
    # der Einzelwert, da beide 10ml-Trials denselben measured_ml haben).
    expected = NEW_VALUE * 10.08 / 10.0
    assert pump_entry["candidate_ml_per_step"] == pytest.approx(expected)


def test_apply_does_not_touch_status_verified_fields_or_last_updated(migrate):
    data = _data_with_mixed_trials()
    before_head = copy.deepcopy(data["controllers"]["MCU_B"]["P1"])
    plan = migrate.find_migration_plan(data)
    migrate.apply_migration_plan(data, plan)

    after_head = data["controllers"]["MCU_B"]["P1"]
    assert after_head["status"] == before_head["status"]
    assert after_head["verified_ml_per_step"] == before_head["verified_ml_per_step"]
    assert after_head["verified_at_ml"] == before_head["verified_at_ml"]
    assert after_head["last_updated"] == before_head["last_updated"]


# --- run(): dry-run / apply / Backup / Idempotenz --------------------------------


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_dry_run_changes_nothing_on_disk(migrate, tmp_path, capsys):
    path = tmp_path / "peristaltic_calibration.json"
    _write(path, _data_with_mixed_trials())
    before_bytes = path.read_bytes()

    exit_code = migrate.run(path, apply=False)

    assert exit_code == 0
    assert path.read_bytes() == before_bytes
    assert list(tmp_path.iterdir()) == [path]  # kein Backup, keine weitere Datei
    output = capsys.readouterr().out
    assert "Dry-Run" in output
    assert "3 Trial(s)" in output


def test_apply_creates_a_dedicated_backup_before_writing(migrate, tmp_path):
    path = tmp_path / "peristaltic_calibration.json"
    original_data = _data_with_mixed_trials()
    _write(path, original_data)
    original_bytes = path.read_bytes()

    exit_code = migrate.run(path, apply=True)
    assert exit_code == 0

    backups = [p for p in tmp_path.iterdir() if p.name.startswith("peristaltic_calibration_before_migration_")]
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes


def test_apply_actually_writes_the_recomputed_values(migrate, tmp_path):
    path = tmp_path / "peristaltic_calibration.json"
    _write(path, _data_with_mixed_trials())

    migrate.run(path, apply=True)

    saved = load_calibration_data(path)
    p1_trials = saved["controllers"]["MCU_B"]["P1"]["trials"]
    migrated = [t for t in p1_trials if t["timestamp_utc"] in {
        "2026-07-22T09:10:02+00:00", "2026-07-22T09:13:39+00:00", "2026-07-22T09:16:02+00:00",
    }]
    assert len(migrated) == 3
    for trial in migrated:
        assert trial["firmware_ml_per_step_used"] == NEW_VALUE


def test_second_apply_run_is_a_no_op(migrate, tmp_path):
    path = tmp_path / "peristaltic_calibration.json"
    _write(path, _data_with_mixed_trials())

    migrate.run(path, apply=True)
    after_first = path.read_bytes()

    exit_code_second = migrate.run(path, apply=True)
    after_second = path.read_bytes()

    assert exit_code_second == 0
    assert after_second == after_first

    backups = [p for p in tmp_path.iterdir() if p.name.startswith("peristaltic_calibration_before_migration_")]
    assert len(backups) == 1  # zweiter Lauf hat KEIN weiteres Backup erzeugt


def test_dry_run_after_apply_reports_no_further_changes(migrate, tmp_path, capsys):
    path = tmp_path / "peristaltic_calibration.json"
    _write(path, _data_with_mixed_trials())
    migrate.run(path, apply=True)

    capsys.readouterr()
    exit_code = migrate.run(path, apply=False)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Keine passenden Trials gefunden" in output


# --- Regressionstest gegen die reale, ausgelieferte Kalibrierdatei --------------


def test_real_repo_calibration_file_migration_yields_the_expected_candidate(migrate, tmp_path):
    """Kopiert die tatsaechliche calibration_data/peristaltic_calibration.json
    in ein tmp_path (das Original bleibt unangetastet) und prueft den vom
    Auftraggeber vorgegebenen Erwartungswert 0.000192624768 fuer den
    Pumpenkopf MCU_B/P1 nach der Migration."""
    real_data = load_calibration_data(_REPO_CALIBRATION_DATA_PATH)
    path = tmp_path / "peristaltic_calibration.json"
    _write(path, real_data)

    plan = migrate.find_migration_plan(real_data)
    assert len(plan) == 7  # alle 7 vorhandenen MCU_B/P1-Trials liegen im Zeitfenster

    exit_code = migrate.run(path, apply=True)
    assert exit_code == 0

    migrated = load_calibration_data(path)
    pump_entry = migrated["controllers"]["MCU_B"]["P1"]
    assert pump_entry["candidate_ml_per_step"] == pytest.approx(0.000192624768, rel=1e-9)
    assert pump_entry["status"] == "candidate"
    assert pump_entry["verified_ml_per_step"] is None

    for trial in pump_entry["trials"]:
        assert trial["firmware_ml_per_step_used"] == NEW_VALUE
