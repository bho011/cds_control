"""
services/peristaltic/calibration.py: Kalibrierformel, Dosis-Sicherheits-
validierung (MAX_INITIAL_TEST_DOSE_ML), Trial-/Statistikberechnung (mit
strikter Trennung nach requested_ml UND firmware_ml_per_step_used),
Firmware-Ist-Wert-vs.-Kandidat-Trennung, JSON-Atomic-Write-Persistenz und
die Parallel-Pumpen-Beschränkung für pair-test/all-four-test.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from services.peristaltic.calibration import (
    ALLOWED_PARALLEL_PUMP_GROUPS,
    DEFAULT_PRIMING_CHUNK_ML,
    FIRMWARE_DEFAULT_ML_PER_STEP,
    MAX_INITIAL_TEST_DOSE_ML,
    MAX_PRIMING_TOTAL_ML,
    CalibrationTrial,
    CalibrationValidationError,
    add_trial,
    candidate_ml_per_step,
    compute_pump_stats,
    compute_priming_chunks,
    default_calibration_data,
    load_calibration_data,
    save_calibration_data,
    validate_dose_ml,
    validate_parallel_pump_selection,
    validate_priming_request,
)


def _trial(requested_ml: float, measured_ml: float, firmware_value: float = FIRMWARE_DEFAULT_ML_PER_STEP) -> CalibrationTrial:
    return CalibrationTrial(
        timestamp_utc="2026-07-21T10:00:00+00:00",
        requested_ml=requested_ml,
        measured_ml=measured_ml,
        measurement_method="graduated_cylinder",
        water_temperature_c=21.0,
        firmware_ml_per_step_used=firmware_value,
        candidate_ml_per_step=candidate_ml_per_step(firmware_value, measured_ml, requested_ml),
    )


# --- Kalibrierformel ----------------------------------------------------------


def test_candidate_formula_matches_task_example():
    # Firmware soll 5.0 ml dosieren, gemessen werden 4.5 ml:
    # candidate = old_value * 4.5 / 5.0
    current = 0.000095548
    result = candidate_ml_per_step(current, measured_ml=4.5, requested_ml=5.0)
    assert result == pytest.approx(current * 4.5 / 5.0)


# --- validate_dose_ml ----------------------------------------------------------


@pytest.mark.parametrize("value", [0.1, 1.0, 5.0, 10.0])
def test_validate_dose_ml_accepts_in_range_values(value):
    assert validate_dose_ml(value) == []


@pytest.mark.parametrize("value", [0, 0.0, -1.0, -0.1])
def test_validate_dose_ml_rejects_zero_and_negative(value):
    assert validate_dose_ml(value) != []


def test_validate_dose_ml_rejects_above_max():
    errors = validate_dose_ml(10.1)
    assert errors != []
    assert str(MAX_INITIAL_TEST_DOSE_ML) in errors[0]


def test_validate_dose_ml_rejects_above_firmware_limit_too():
    assert validate_dose_ml(50.0) != []  # Firmware erlaubt das, das CLI nicht


def test_validate_dose_ml_rejects_nan_and_infinity():
    assert validate_dose_ml(float("nan")) != []
    assert validate_dose_ml(float("inf")) != []
    assert validate_dose_ml(float("-inf")) != []


def test_validate_dose_ml_rejects_bool():
    assert validate_dose_ml(True) != []
    assert validate_dose_ml(False) != []


def test_validate_dose_ml_rejects_non_numeric():
    assert validate_dose_ml("5.0") != []
    assert validate_dose_ml(None) != []


# --- Priming (Entlüften): validate_priming_request / compute_priming_chunks --


def test_validate_priming_request_accepts_the_task_example():
    assert validate_priming_request(180.0, 10.0) == []


def test_validate_priming_request_accepts_default_chunk_ml():
    assert validate_priming_request(150.0, DEFAULT_PRIMING_CHUNK_ML) == []


def test_validate_priming_request_rejects_max_ml_above_200():
    errors = validate_priming_request(200.1, 10.0)
    assert errors != []
    assert str(MAX_PRIMING_TOTAL_ML) in errors[0]


def test_validate_priming_request_accepts_max_ml_at_exactly_200():
    assert validate_priming_request(200.0, 10.0) == []


def test_validate_priming_request_rejects_chunk_ml_above_max_initial_test_dose():
    errors = validate_priming_request(150.0, 10.1)
    assert errors != []
    assert str(MAX_INITIAL_TEST_DOSE_ML) in errors[0]


def test_validate_priming_request_accepts_chunk_ml_at_exactly_the_test_limit():
    assert validate_priming_request(150.0, MAX_INITIAL_TEST_DOSE_ML) == []


def test_validate_priming_request_does_not_raise_max_initial_test_dose_ml_itself():
    """MAX_INITIAL_TEST_DOSE_ML (für test/calibrate) bleibt exakt bei 10.0 -
    validate_priming_request() erweitert nur eine eigene, separate Grenze
    (MAX_PRIMING_TOTAL_ML), verändert diese Konstante nicht."""
    assert MAX_INITIAL_TEST_DOSE_ML == 10.0


@pytest.mark.parametrize("value", [0, 0.0, -1.0])
def test_validate_priming_request_rejects_zero_and_negative_max_ml(value):
    assert validate_priming_request(value, 10.0) != []


@pytest.mark.parametrize("value", [0, 0.0, -1.0])
def test_validate_priming_request_rejects_zero_and_negative_chunk_ml(value):
    assert validate_priming_request(150.0, value) != []


def test_validate_priming_request_rejects_nan_and_infinity():
    assert validate_priming_request(float("nan"), 10.0) != []
    assert validate_priming_request(float("inf"), 10.0) != []
    assert validate_priming_request(150.0, float("nan")) != []
    assert validate_priming_request(150.0, float("inf")) != []


def test_validate_priming_request_rejects_bool():
    assert validate_priming_request(True, 10.0) != []
    assert validate_priming_request(150.0, True) != []


def test_validate_priming_request_rejects_non_numeric():
    assert validate_priming_request("150", 10.0) != []
    assert validate_priming_request(150.0, "10") != []


def test_validate_priming_request_collects_both_errors_at_once():
    errors = validate_priming_request(500.0, 50.0)
    assert len(errors) == 2


def test_compute_priming_chunks_150_ml_splits_into_15_chunks_of_10():
    chunks = compute_priming_chunks(150.0, 10.0)
    assert chunks == [10.0] * 15
    assert sum(chunks) == pytest.approx(150.0)


def test_compute_priming_chunks_155_ml_splits_into_15_of_10_plus_1_of_5():
    chunks = compute_priming_chunks(155.0, 10.0)
    assert chunks == [10.0] * 15 + [5.0]
    assert sum(chunks) == pytest.approx(155.0)


def test_compute_priming_chunks_last_chunk_is_never_larger_than_chunk_ml():
    for max_ml in (1.0, 9.9, 10.0, 10.1, 33.0, 180.0, 200.0):
        chunks = compute_priming_chunks(max_ml, 10.0)
        assert all(chunk <= 10.0 for chunk in chunks)
        assert sum(chunks) == pytest.approx(max_ml)


def test_compute_priming_chunks_exact_multiple_has_no_extra_small_chunk():
    chunks = compute_priming_chunks(180.0, 10.0)
    assert chunks == [10.0] * 18
    assert len(chunks) == 18


# --- Statistik / Trennung nach requested_ml UND firmware_ml_per_step_used ----


def test_compute_pump_stats_basic_numbers():
    trials = [_trial(5.0, 4.5), _trial(5.0, 4.6), _trial(5.0, 5.1)]
    stats = compute_pump_stats(trials, requested_ml=5.0, firmware_ml_per_step_used=FIRMWARE_DEFAULT_ML_PER_STEP)

    measured = [4.5, 4.6, 5.1]
    assert stats.count == 3
    assert stats.mean_measured_ml == pytest.approx(sum(measured) / 3)
    assert stats.median_measured_ml == pytest.approx(4.6)
    assert stats.stdev_measured_ml is not None
    expected_mad = sum(abs(x - stats.mean_measured_ml) for x in measured) / 3
    assert stats.mean_absolute_deviation_ml == pytest.approx(expected_mad)
    expected_percent = sum(abs(x - 5.0) / 5.0 * 100.0 for x in measured) / 3
    assert stats.mean_absolute_relative_error_percent == pytest.approx(expected_percent)
    assert stats.suggested_candidate_ml_per_step == pytest.approx(
        candidate_ml_per_step(FIRMWARE_DEFAULT_ML_PER_STEP, 4.6, 5.0)
    )


def test_compute_pump_stats_stdev_none_for_single_trial():
    stats = compute_pump_stats([_trial(5.0, 4.5)], requested_ml=5.0, firmware_ml_per_step_used=FIRMWARE_DEFAULT_ML_PER_STEP)
    assert stats.count == 1
    assert stats.stdev_measured_ml is None


def test_compute_pump_stats_raises_when_no_matching_trials():
    trials = [_trial(5.0, 4.5)]
    with pytest.raises(CalibrationValidationError):
        compute_pump_stats(trials, requested_ml=10.0, firmware_ml_per_step_used=FIRMWARE_DEFAULT_ML_PER_STEP)


def test_stats_do_not_mix_trials_with_different_requested_ml():
    """Kalibrier-Trials (5ml) und spaetere Verifikations-Trials (10ml)
    duerfen nie in dieselbe Statistik einfliessen."""
    trials = [_trial(5.0, 4.5), _trial(5.0, 4.6), _trial(10.0, 9.4)]
    stats_5 = compute_pump_stats(trials, requested_ml=5.0, firmware_ml_per_step_used=FIRMWARE_DEFAULT_ML_PER_STEP)
    assert stats_5.count == 2


def test_stats_do_not_mix_trials_with_different_firmware_values():
    """Regressionstest (Zusatzregel 1 bei Freigabe): zwei 5ml-Trials mit
    Firmwarewert A, ein 5ml-Trial mit Firmwarewert B - die Statistik fuer A
    beruecksichtigt ausschliesslich die beiden A-Trials, die fuer B nur den
    einen B-Trial, kein gemeinsamer Kandidat."""
    firmware_b = 0.0001900
    trials = [
        _trial(5.0, 4.5, firmware_value=FIRMWARE_DEFAULT_ML_PER_STEP),
        _trial(5.0, 4.6, firmware_value=FIRMWARE_DEFAULT_ML_PER_STEP),
        _trial(5.0, 9.0, firmware_value=firmware_b),
    ]

    stats_a = compute_pump_stats(trials, requested_ml=5.0, firmware_ml_per_step_used=FIRMWARE_DEFAULT_ML_PER_STEP)
    assert stats_a.count == 2

    stats_b = compute_pump_stats(trials, requested_ml=5.0, firmware_ml_per_step_used=firmware_b)
    assert stats_b.count == 1
    assert stats_a.suggested_candidate_ml_per_step != stats_b.suggested_candidate_ml_per_step


# --- Persistenz / Trial-Aufzeichnung -------------------------------------------


def test_default_calibration_data_matches_target_mapping_roles():
    data = default_calibration_data()
    assert data["schema_version"] == 1
    assert data["controllers"]["MCU_A"]["P1"]["role"] == "ph_acid"
    assert data["controllers"]["MCU_B"]["P3"]["role"] == "nutrient_a_2"
    for controller in ("MCU_A", "MCU_B"):
        for pump in ("P1", "P2", "P3", "P4"):
            entry = data["controllers"][controller][pump]
            assert entry["status"] == "not_calibrated"
            assert entry["candidate_ml_per_step"] is None
            assert entry["verified_ml_per_step"] is None
            assert entry["verified_at_ml"] == []
            assert entry["trials"] == []


def test_default_calibration_data_is_not_a_shared_mutable_singleton():
    first = default_calibration_data()
    first["controllers"]["MCU_A"]["P1"]["trials"].append({"fake": True})

    second = default_calibration_data()
    assert second["controllers"]["MCU_A"]["P1"]["trials"] == []


def test_add_trial_sets_candidate_status_never_verified():
    data = default_calibration_data()
    trial = add_trial(
        data,
        "MCU_B",
        "P1",
        requested_ml=5.0,
        measured_ml=4.5,
        measurement_method="graduated_cylinder",
        water_temperature_c=21.0,
    )
    entry = data["controllers"]["MCU_B"]["P1"]
    assert entry["status"] == "candidate"
    assert entry["status"] != "verified"
    assert entry["candidate_ml_per_step"] == pytest.approx(trial.candidate_ml_per_step)
    assert entry["verified_ml_per_step"] is None
    assert entry["last_updated"] == trial.timestamp_utc
    assert len(entry["trials"]) == 1


def test_add_trial_does_not_disturb_other_pumps():
    data = default_calibration_data()
    add_trial(
        data, "MCU_B", "P1", requested_ml=5.0, measured_ml=4.5,
        measurement_method=None, water_temperature_c=None,
    )
    assert data["controllers"]["MCU_B"]["P2"]["status"] == "not_calibrated"
    assert data["controllers"]["MCU_B"]["P2"]["trials"] == []
    assert data["controllers"]["MCU_A"]["P1"]["status"] == "not_calibrated"


def test_second_trial_does_not_auto_chain_candidate_as_firmware_value():
    """Regressionstest (Zusatzregel bei Freigabe zu Section 7): der zweite
    Trial verwendet ohne explizite Angabe weiterhin FIRMWARE_DEFAULT_ML_PER_STEP,
    NICHT den aus dem ersten Trial berechneten Kandidaten."""
    data = default_calibration_data()

    t1 = add_trial(
        data, "MCU_B", "P1", requested_ml=5.0, measured_ml=4.5,
        measurement_method="graduated_cylinder", water_temperature_c=21.0,
    )
    assert t1.firmware_ml_per_step_used == FIRMWARE_DEFAULT_ML_PER_STEP
    assert t1.candidate_ml_per_step != FIRMWARE_DEFAULT_ML_PER_STEP

    t2 = add_trial(
        data, "MCU_B", "P1", requested_ml=5.0, measured_ml=4.6,
        measurement_method="graduated_cylinder", water_temperature_c=21.0,
        # firmware_ml_per_step_used bewusst NICHT uebergeben
    )
    assert t2.firmware_ml_per_step_used == FIRMWARE_DEFAULT_ML_PER_STEP

    entry = data["controllers"]["MCU_B"]["P1"]
    assert entry["status"] == "candidate"
    assert entry["verified_ml_per_step"] is None


def test_save_and_load_calibration_data_roundtrip(tmp_path):
    path = tmp_path / "peristaltic_calibration.json"
    data = default_calibration_data()
    add_trial(
        data, "MCU_A", "P1", requested_ml=5.0, measured_ml=4.8,
        measurement_method="mass_approx", water_temperature_c=19.5,
    )
    save_calibration_data(path, data)

    loaded = load_calibration_data(path)
    assert loaded == data


def test_save_calibration_data_creates_backup_on_second_save(tmp_path):
    path = tmp_path / "peristaltic_calibration.json"
    save_calibration_data(path, default_calibration_data())

    data2 = default_calibration_data()
    add_trial(
        data2, "MCU_A", "P1", requested_ml=5.0, measured_ml=4.8,
        measurement_method=None, water_temperature_c=None,
    )
    save_calibration_data(path, data2)

    backup_path = path.with_suffix(path.suffix + ".bak")
    assert backup_path.exists()


def test_load_calibration_data_missing_file_raises():
    with pytest.raises(CalibrationValidationError):
        load_calibration_data(__import__("pathlib").Path("/nonexistent/peristaltic_calibration.json"))


def test_load_calibration_data_corrupted_json_fails_closed(tmp_path):
    path = tmp_path / "peristaltic_calibration.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CalibrationValidationError):
        load_calibration_data(path)


# --- Parallel-Pumpen-Beschraenkung (pair-test / all-four-test) ---------------


def test_mcu_a_never_allows_parallel_tests():
    assert ALLOWED_PARALLEL_PUMP_GROUPS["MCU_A"] == []
    assert validate_parallel_pump_selection("MCU_A", ["P1", "P2"]) != []
    assert validate_parallel_pump_selection("MCU_A", ["P3", "P4"]) != []
    assert validate_parallel_pump_selection("MCU_A", ["P1", "P2", "P3", "P4"]) != []


def test_mcu_b_allows_only_the_two_fixed_pairs_and_all_four():
    """Physische Hardwareanordnung: Naehrstoff A = P1+P3, Naehrstoff B =
    P2+P4 (siehe config/peristaltic_mapping.json)."""
    assert validate_parallel_pump_selection("MCU_B", ["P1", "P3"]) == []
    assert validate_parallel_pump_selection("MCU_B", ["P2", "P4"]) == []
    assert validate_parallel_pump_selection("MCU_B", ["P1", "P2", "P3", "P4"]) == []


@pytest.mark.parametrize("pumps", [["P1", "P2"], ["P3", "P4"], ["P1", "P4"], ["P2", "P3"]])
def test_mcu_b_rejects_cross_pair_combinations(pumps):
    assert validate_parallel_pump_selection("MCU_B", pumps) != []


def test_all_four_test_on_mcu_a_is_rejected():
    assert validate_parallel_pump_selection("MCU_A", ["P1", "P2", "P3", "P4"]) != []


def test_all_four_test_on_mcu_b_is_allowed():
    assert validate_parallel_pump_selection("MCU_B", ["P1", "P2", "P3", "P4"]) == []


# --- Regressionstest gegen die echte, ausgelieferte Kalibrierdatei --------------


def test_real_repo_calibration_file_roles_reflect_the_new_pairing_without_touching_trial_data():
    """Nur die role-Felder von MCU_B duerfen sich durch das Umpaaren
    geaendert haben - die vorhandenen P1-Messwerte/Kandidaten/Status/
    Zeitstempel muessen exakt erhalten geblieben sein."""
    path = Path(__file__).resolve().parent.parent / "calibration_data" / "peristaltic_calibration.json"
    data = load_calibration_data(path)
    mcu_b = data["controllers"]["MCU_B"]

    assert mcu_b["P1"]["role"] == "nutrient_a_1"
    assert mcu_b["P2"]["role"] == "nutrient_b_1"
    assert mcu_b["P3"]["role"] == "nutrient_a_2"
    assert mcu_b["P4"]["role"] == "nutrient_b_2"

    p1 = mcu_b["P1"]
    assert p1["status"] == "candidate"
    assert p1["verified_ml_per_step"] is None
    assert p1["last_updated"] == "2026-07-22T09:16:02+00:00"
    assert p1["candidate_ml_per_step"] == pytest.approx(0.000192624768, rel=1e-9)
    assert len(p1["trials"]) == 7
    for trial in p1["trials"]:
        assert trial["firmware_ml_per_step_used"] == 0.000191096
