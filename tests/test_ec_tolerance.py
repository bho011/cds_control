"""
EC-Toleranz der Endbewertung (statemachine/measurement_evaluation.py), hardwarefrei.

ec_tolerance_ms_cm = 0.1 ist der vorlaeufige historische Referenzwert (bisheriger Parameter maxdelta der
frueheren Steuerung), kein hardwarevalidierter Qualitaetswert. Geprueft wird hier:

* die Konfigurationskette: echte config/process_settings.json -> Schema -> RunConfig -> Endbewertung,
* der exakte Dezimalvergleich an der Toleranzgrenze (inklusive Grenzen, keine Zusatztoleranz),
* EC wird nur bei Rezeptlaeufen MIT Naehrstoffdosierung bewertet (Wasserlauf: NOT_EVALUATED, kein Fehler),
* die Vorabpruefung der Toleranz mit derselben Regel wie die Endbewertung: bei einem Lauf MIT Naehrstoffdosierung
  blockiert ein fehlender Schluessel ebenso wie ein ungueltiger Wert den Start; ein Wasserlauf braucht den
  Schluessel nicht.

Die uebrigen Faelle (innerhalb/ausserhalb, fehlende Messwerte, ungueltige Toleranz in der Endbewertung) stehen in
tests/test_fill_and_measure_state_machine.py; das Verhalten nach einem EC-Fehler im Gesamtablauf (keine
Nachregelung) in tests/test_mixing_overlap.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_fill_and_measure_state_machine import _base_settings, _run_to_measure

import nicegui_dashboard.recipe_store as recipe_store
from domain.recipe_model import RunOptions
from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA
from nicegui_dashboard.recipe_store import build_run_config
from services.settings_validation import validate_settings
from statemachine.fill_and_measure_state_machine import FillAndMeasureState
from statemachine.measurement_evaluation import (
    EC_TOLERANCE_KEY,
    STATUS_FAILED,
    STATUS_NOT_EVALUATED,
    STATUS_OK,
    ec_tolerance_problem,
    evaluate_final_values,
    overall_status,
    valid_tolerance,
)
from statemachine.start_validation import validate_dosing_run_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(target: float = 1.5, tolerance=0.1, dosing: bool = True, **extra) -> dict:
    return {
        EC_TOLERANCE_KEY: tolerance,
        "recipe_run_config_snapshot": {"target_ec_ms_cm": target, "nutrient_dosing_enabled": dosing},
        **extra,
    }


def _evaluate_ec(actual, **kwargs):
    return evaluate_final_values(_settings(**kwargs), {"water_values": {"ec_ms_cm": actual, "ph": 6.0}})


# --- Konfigurationskette mit der echten Datei ------------------------------------------------------


def test_configured_ec_tolerance_reaches_the_final_evaluation_and_decides():
    with (REPO_ROOT / "config/process_settings.json").open(encoding="utf-8") as file:
        raw = json.load(file)
    assert raw[EC_TOLERANCE_KEY] == 0.1  # Wert aus der bisherigen Steuerung (maxdelta), vorlaeufig
    assert raw["nutrient_ro_resume_progress"] == 0.5

    # wie ProcessController.load_settings() und start_fill_and_measure(): Schema, RunConfig, Schema
    settings = validate_settings(raw, PROCESS_SETTINGS_SCHEMA, "config/process_settings.json")
    recipe = dict(
        recipe_store.DEFAULT_RECIPE, recipe_name="EC", target_ro_water_l=10.0, target_ec_ms_cm=1.5,
        nutrients_ml_per_100_l=200.0, nutrient_dosing_enabled=True,
    )
    run_config = build_run_config(settings, recipe, RunOptions(tank_emptied_confirmed=True))
    run_config = validate_settings(run_config, PROCESS_SETTINGS_SCHEMA, "RunConfig")
    assert run_config[EC_TOLERANCE_KEY] == 0.1  # unveraendert bis in die Zustandsmaschine
    assert run_config["recipe_run_config_snapshot"]["target_ec_ms_cm"] == 1.5
    assert validate_dosing_run_settings(run_config) is None  # die echte Konfiguration besteht die Vorabpruefung
    without_key = {key: value for key, value in run_config.items() if key != EC_TOLERANCE_KEY}
    problem = validate_dosing_run_settings(without_key)  # ohne den Schluessel wuerde derselbe Lauf blockiert
    assert problem is not None and problem[0] == "tolerance_invalid" and EC_TOLERANCE_KEY in problem[1]

    def evaluate(ec: float):
        return evaluate_final_values(run_config, {"water_values": {"ec_ms_cm": ec, "ph": 6.0}})

    ok, _message, _reason, details = evaluate(1.58)  # +0.08: innerhalb
    assert ok and details["EC"]["status"] == "ok" and details["EC"]["tolerance"] == 0.1
    assert overall_status(ok, details) == STATUS_OK  # pH-Band 5.0 bis 8.9 der Konfiguration enthaelt 6.0

    ok, message, reason, details = evaluate(1.65)  # +0.15: ausserhalb der konfigurierten Toleranz
    assert not ok and reason == "ec_out_of_tolerance" and "0.1" in message
    assert overall_status(ok, details) == STATUS_FAILED


# --- exakter Dezimalvergleich an der Grenze (E2) -----------------------------------------------------


@pytest.mark.parametrize("actual,expected_ok", [(1.400, True), (1.500, True), (1.600, True), (1.399, False), (1.601, False)])
def test_boundary_values_around_target_1_5_with_tolerance_0_1(actual, expected_ok):
    ok, _message, reason, details = _evaluate_ec(actual)
    assert ok is expected_ok
    assert details["EC"]["status"] == ("ok" if expected_ok else "out_of_tolerance")
    assert reason == (None if expected_ok else "ec_out_of_tolerance")


@pytest.mark.parametrize("target,actual", [(1.5, 1.6), (1.5, 1.4), (1.0, 1.1), (2.0, 2.1), (1.2, 1.3), (0.5, 0.6), (2.4, 2.5)])
def test_float_critical_edge_pairs_are_inside(target, actual):
    # abs(1.6 - 1.5) ist in Gleitkommazahlen 0.10000000000000009: ohne Dezimalvergleich waere das "ausserhalb".
    ok, *_ = _evaluate_ec(actual, target=target)
    assert ok, (target, actual)


def test_every_target_in_the_allowed_range_accepts_exactly_the_tolerance_and_rejects_a_hair_more():
    for hundredths in range(50, 251):  # Sollwertbereich 0,50 bis 2,50 mS/cm in 0,01-Schritten
        target = hundredths / 100
        for actual in (round(target + 0.1, 2), round(target - 0.1, 2)):
            assert _evaluate_ec(actual, target=target)[0], (target, actual)
        for actual in (round(target + 0.101, 3), round(target - 0.101, 3)):
            ok, _m, reason, _d = _evaluate_ec(actual, target=target)
            assert not ok and reason == "ec_out_of_tolerance", (target, actual)


def test_the_comparison_adds_no_hidden_tolerance():
    # Ein Tausendstel mehr als die Toleranz ist bereits ausserhalb, auch bei einer anderen Toleranz.
    assert _evaluate_ec(1.75, tolerance=0.25)[0]
    assert not _evaluate_ec(1.751, tolerance=0.25)[0]
    assert _evaluate_ec(1.25, tolerance=0.25)[0]
    assert not _evaluate_ec(1.249, tolerance=0.25)[0]


def test_the_reported_deviation_is_the_decimal_difference():
    _ok, _m, _r, details = _evaluate_ec(1.6)
    assert details["EC"]["deviation"] == 0.1  # nicht 0.10000000000000009


# --- Wasserlauf: EC NOT_EVALUATED (E1) -----------------------------------------------------------------


def _water_run_settings(**overrides):
    values = {
        "ec_tolerance_ms_cm": 0.1,
        "recipe_run_config_snapshot": {"target_ec_ms_cm": 1.5, "target_ph": 6.0, "nutrient_dosing_enabled": False},
    }
    values.update(overrides)
    return _base_settings(**values)


def test_water_run_with_a_far_off_ec_does_not_fail_but_is_not_confirmed():
    sm, pump = _run_to_measure(_water_run_settings(), ec=0.02, ph=6.0)  # RO-Wasser: weit unter dem EC-Sollwert
    assert sm.state == FillAndMeasureState.FINISHED and sm.error_message is None
    assert sm.final_evaluation["EC"] == {"status": STATUS_NOT_EVALUATED, "reason": "no_nutrient_dosing"}
    assert sm.final_evaluation_status == STATUS_NOT_EVALUATED  # keine bestaetigte Sollwerterfuellung
    assert pump.is_on is False


def test_water_run_still_evaluates_ph_unchanged():
    settings = _water_run_settings(ph_min=6.0, ph_max=7.0)
    sm, _ = _run_to_measure(settings, ec=0.02, ph=6.5)
    assert sm.state == FillAndMeasureState.FINISHED
    assert sm.final_evaluation["pH"]["status"] == "ok"
    assert sm.final_evaluation_status == STATUS_NOT_EVALUATED  # EC fehlt, daher nie OK

    sm, pump = _run_to_measure(settings, ec=0.02, ph=7.5)  # pH ausserhalb des Bands bleibt ein Fehler
    assert sm.state == FillAndMeasureState.ERROR and sm.stop_reason == "ph_out_of_tolerance"
    assert pump.is_on is False


@pytest.mark.parametrize("flag", [None, False, "true", 1, 0, "yes"])
def test_only_a_strict_true_dosing_flag_counts_as_a_dosing_run(flag):
    settings = _water_run_settings()
    settings["recipe_run_config_snapshot"]["nutrient_dosing_enabled"] = flag
    sm, _ = _run_to_measure(settings, ec=9.9)
    assert sm.state == FillAndMeasureState.FINISHED
    assert sm.final_evaluation["EC"]["status"] == STATUS_NOT_EVALUATED
    assert sm.final_evaluation_status == STATUS_NOT_EVALUATED


def test_dosing_run_with_ec_out_of_tolerance_is_still_an_error():
    settings = _water_run_settings()
    settings["recipe_run_config_snapshot"]["nutrient_dosing_enabled"] = True
    sm, pump = _run_to_measure(settings, ec=0.02)
    assert sm.state == FillAndMeasureState.ERROR and sm.stop_reason == "ec_out_of_tolerance"
    assert sm.final_evaluation_status == STATUS_FAILED and pump.is_on is False


def test_water_run_ignores_the_tolerance_value_because_ec_is_not_evaluated():
    sm, _ = _run_to_measure(_water_run_settings(ec_tolerance_ms_cm=0), ec=0.02)
    assert sm.state == FillAndMeasureState.FINISHED
    assert sm.final_evaluation["EC"]["reason"] == "no_nutrient_dosing"


def test_water_run_without_the_tolerance_key_is_not_blocked_and_ec_stays_not_evaluated():
    settings = _water_run_settings()
    del settings[EC_TOLERANCE_KEY]  # die Pflicht gilt nur fuer Laeufe MIT Naehrstoffdosierung
    sm, pump = _run_to_measure(settings, ec=0.02, ph=6.0)
    assert sm.state == FillAndMeasureState.FINISHED and sm.error_message is None
    assert sm.final_evaluation["EC"]["status"] == STATUS_NOT_EVALUATED
    assert sm.final_evaluation_status == STATUS_NOT_EVALUATED
    assert pump.is_on is False


# --- gemeinsame Toleranzpruefung (E3) --------------------------------------------------------------------

_INVALID = [0, 0.0, -0.1, None, "0.1", "", float("nan"), float("inf"), float("-inf"), True, False, [0.1], {}]


@pytest.mark.parametrize("bad", _INVALID)
def test_invalid_tolerance_values_are_rejected_by_the_one_shared_rule(bad):
    assert valid_tolerance(bad) is None
    message = ec_tolerance_problem({EC_TOLERANCE_KEY: bad})
    assert message is not None and EC_TOLERANCE_KEY in message
    problem = validate_dosing_run_settings({EC_TOLERANCE_KEY: bad, "nutrient_ro_resume_progress": 0.5})
    assert problem is not None and problem[0] == "tolerance_invalid"


@pytest.mark.parametrize("good", [0.1, 0.05, 1, 2.5, 1e-6])
def test_valid_tolerance_values_pass(good):
    assert valid_tolerance(good) == float(good)
    assert ec_tolerance_problem({EC_TOLERANCE_KEY: good}) is None


@pytest.mark.parametrize("settings", [{}, {"ph_min": 5.0, "ph_max": 8.9}, "kein dict", None])
def test_missing_ec_tolerance_is_reported_for_a_dosing_run(settings):
    message = ec_tolerance_problem(settings)
    assert message is not None and EC_TOLERANCE_KEY in message and "fehlt" in message


def test_dosing_run_settings_without_ec_tolerance_are_rejected_with_the_tolerance_reason():
    problem = validate_dosing_run_settings({"nutrient_ro_resume_progress": 0.5})  # ec_tolerance_ms_cm fehlt
    assert problem is not None and problem[0] == "tolerance_invalid"
    assert EC_TOLERANCE_KEY in problem[1] and "fehlt" in problem[1]
    assert validate_dosing_run_settings({"nutrient_ro_resume_progress": 0.5, EC_TOLERANCE_KEY: 0.1}) is None


@pytest.mark.parametrize("bad", [None, 0, -0.5, 1.0000001, 2, "0.5", True, float("nan"), float("inf")])
def test_invalid_resume_progress_is_rejected_before_any_dosing(bad):
    problem = validate_dosing_run_settings({"nutrient_ro_resume_progress": bad, EC_TOLERANCE_KEY: 0.1})
    assert problem is not None and problem[0] == "resume_progress_invalid"
    assert "nutrient_ro_resume_progress" in problem[1]


def test_missing_resume_progress_is_rejected_for_a_dosing_run():
    problem = validate_dosing_run_settings({EC_TOLERANCE_KEY: 0.1})
    assert problem is not None and problem[0] == "resume_progress_invalid"
    assert validate_dosing_run_settings("kein dict")[0] == "resume_progress_invalid"


@pytest.mark.parametrize("good", [0.5, 1, 1.0, 0.0001, 0.999])
def test_valid_resume_progress_values_pass(good):
    assert validate_dosing_run_settings({"nutrient_ro_resume_progress": good, EC_TOLERANCE_KEY: 0.1}) is None


def test_the_ec_tolerance_is_deliberately_not_in_the_settings_schema():
    # Ein Schema-Eintrag wuerde bei fehlendem Schluessel einen Standardwert einsetzen. Es gibt aber bewusst keine
    # Standardtoleranz: ohne Schluessel blockiert ein Dosierlauf (validate_dosing_run_settings), und ein Wasserlauf
    # bewertet EC nicht.
    assert EC_TOLERANCE_KEY not in {field.name for field in PROCESS_SETTINGS_SCHEMA}
    validated = validate_settings({**json.loads((REPO_ROOT / "config/process_settings.json").read_text("utf-8"))},
                                  PROCESS_SETTINGS_SCHEMA, "x")
    assert validated[EC_TOLERANCE_KEY] == 0.1
    without = json.loads((REPO_ROOT / "config/process_settings.json").read_text("utf-8"))
    del without[EC_TOLERANCE_KEY]
    assert EC_TOLERANCE_KEY not in validate_settings(without, PROCESS_SETTINGS_SCHEMA, "x")
