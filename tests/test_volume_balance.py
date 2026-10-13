"""AP-05 Stufe 1: domain/recipe/volume_balance.py - reines Volumenmodell.

Keine Hardware, kein Netzwerk, keine Dateien. Zahlen sind Testdaten, keine
Prozessparameter.
"""

from __future__ import annotations

import ast
import dataclasses
import math
from fractions import Fraction
from pathlib import Path

import pytest

from domain.recipe import volume_balance as vb
from domain.recipe.volume_balance import (
    Evidence,
    FrozenPhaseTarget,
    VolumeEvaluation,
    VolumeModelError,
    VolumePlan,
    VolumeReason,
    VolumeState,
    dosing_progress,
    dynamic_phase_target_l,
    evaluate_final_volume,
    evaluate_ro_resume,
    evaluate_target_confirmation,
    evaluate_volume_state,
    expected_target_total_l,
    freeze_phase_target,
    ml_to_ul,
    prefill_phase_target_l,
)

BAD_NUMBERS = [True, False, "1", None, float("nan"), float("inf"), -float("inf"), [1]]


def make_plan(**overrides) -> VolumePlan:
    values = dict(
        target_ro_water_l=100.0,
        estimated_process_volume_l=100.5,
        planned_nutrient_ml=500.0,
        max_mixer_l=185.0,
        prefill_target_l=30.0,
    )
    values.update(overrides)
    return VolumePlan.create(**values)


def state(mixer=0.0, confirmed=0.0, in_flight=0.0, uncertain=0.0) -> VolumeState:
    return VolumeState(
        mixer_measured_l=mixer,
        confirmed_nutrient_ml=confirmed,
        in_flight_nutrient_ml=in_flight,
        uncertain_nutrient_ml=uncertain,
    )


# --- Helfer ml -> uL ----------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [(0, 0), (0.0, 0), (1, 1000), (12.345, 12345), (0.001, 1), (12.345000000000001, 12345)])
def test_ml_to_ul_accepts_microliter_representable_values(value, expected):
    assert ml_to_ul(value) == expected


@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_ml_to_ul_rejects_invalid_types_and_non_finite(value):
    with pytest.raises(VolumeModelError) as exc:
        ml_to_ul(value)
    assert exc.value.reason is VolumeReason.INVALID_INPUT


def test_ml_to_ul_rejects_negative_and_non_quantizable():
    with pytest.raises(VolumeModelError) as neg:
        ml_to_ul(-0.001)
    assert neg.value.reason is VolumeReason.NEGATIVE_VALUE
    with pytest.raises(VolumeModelError) as frac:
        ml_to_ul(0.0005)
    assert frac.value.reason is VolumeReason.NOT_QUANTIZABLE


# --- VolumePlan: Grenzen ------------------------------------------------------------------


def test_valid_plan_is_created_and_frozen():
    plan = make_plan()
    assert plan.dosing_planned is True
    assert plan.planned_nutrient_ul == 500_000
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.target_ro_water_l = 1.0  # type: ignore[misc]


def test_180_l_ro_is_the_boundary():
    make_plan(target_ro_water_l=180.0, estimated_process_volume_l=180.9, planned_nutrient_ml=900.0)
    with pytest.raises(VolumeModelError) as exc:
        make_plan(target_ro_water_l=180.01, estimated_process_volume_l=180.51, planned_nutrient_ml=500.0)
    assert exc.value.reason is VolumeReason.RO_TARGET_ABOVE_LIMIT


def test_185_l_process_volume_is_the_boundary_and_1_ml_over_is_rejected():
    ok = make_plan(target_ro_water_l=180.0, estimated_process_volume_l=185.0, planned_nutrient_ml=5000.0)
    assert ok.limit_l == 185.0
    with pytest.raises(VolumeModelError) as exc:
        make_plan(target_ro_water_l=180.0, estimated_process_volume_l=185.001, planned_nutrient_ml=5001.0)
    assert exc.value.reason is VolumeReason.PROCESS_VOLUME_ABOVE_LIMIT


def test_physical_tank_capacity_200_l_is_not_an_operating_limit():
    with pytest.raises(VolumeModelError) as exc:
        make_plan(max_mixer_l=200.0)
    assert exc.value.reason is VolumeReason.MAX_MIXER_INVALID


def test_estimate_above_operative_mixer_limit_is_rejected():
    with pytest.raises(VolumeModelError) as exc:
        make_plan(max_mixer_l=100.4)
    assert exc.value.reason is VolumeReason.PROCESS_VOLUME_ABOVE_LIMIT


def test_estimate_must_equal_ro_plus_nutrients():
    with pytest.raises(VolumeModelError) as exc:
        make_plan(estimated_process_volume_l=100.6)
    assert exc.value.reason is VolumeReason.ESTIMATE_MISMATCH


@pytest.mark.parametrize("prefill", [0.0, -1.0, 100.001, True, float("nan"), float("inf"), "30"])
def test_prefill_target_must_be_positive_and_not_above_ro(prefill):
    with pytest.raises(VolumeModelError) as exc:
        make_plan(prefill_target_l=prefill)
    assert exc.value.reason is VolumeReason.PREFILL_TARGET_INVALID


def test_prefill_equal_to_ro_is_allowed():
    assert make_plan(prefill_target_l=100.0).prefill_target_l == 100.0


def test_prefill_is_required_when_dosing_is_planned_but_not_otherwise():
    with pytest.raises(VolumeModelError) as exc:
        make_plan(prefill_target_l=None)
    assert exc.value.reason is VolumeReason.PREFILL_TARGET_MISSING
    plan = make_plan(planned_nutrient_ml=0.0, estimated_process_volume_l=100.0, prefill_target_l=None)
    assert plan.dosing_planned is False


@pytest.mark.parametrize("planned", [-1.0, 0.0005, True, float("nan"), "5"])
def test_planned_nutrient_amount_is_validated(planned):
    with pytest.raises(VolumeModelError):
        make_plan(planned_nutrient_ml=planned)


@pytest.mark.parametrize("ro", [0.0, -5.0, True, float("nan"), "100"])
def test_ro_target_must_be_positive_and_finite(ro):
    with pytest.raises(VolumeModelError) as exc:
        make_plan(target_ro_water_l=ro)
    assert exc.value.reason is VolumeReason.RO_TARGET_INVALID


# --- expected_target_total_l ----------------------------------------------------------------


def test_expected_target_total_is_recipe_volume_with_dosing_and_raw_ro_without():
    assert expected_target_total_l(make_plan()) == 100.5
    raw = make_plan(target_ro_water_l=50.1234, estimated_process_volume_l=50.123, planned_nutrient_ml=0.0, prefill_target_l=None)
    assert expected_target_total_l(raw) == 50.1234  # heutiges Verhalten ohne Dosierung bleibt


# --- evaluate_volume_state ------------------------------------------------------------------


def test_start_state_has_full_conservative_capacity():
    ev = evaluate_volume_state(make_plan(), state(mixer=0.0))
    assert ev.ok and ev.reason is None
    assert ev.remaining_capacity_ul == 184_500_000
    assert ev.remaining_capacity_l == 184.5
    assert ev.not_started_nutrient_ml == 500.0
    assert ev.still_possible_nutrient_ml == 500.0
    assert ev.indirect_ro_estimate_l == 0.0


def test_four_evidence_classes_are_kept_apart():
    assert vb.VOLUME_FIELD_EVIDENCE["target_ro_water_l"] is Evidence.PLANNED
    assert vb.VOLUME_FIELD_EVIDENCE["confirmed_nutrient_ml"] is Evidence.PROTOCOL_CONFIRMED
    assert vb.VOLUME_FIELD_EVIDENCE["mixer_measured_l"] is Evidence.SENSOR_MEASURED
    assert vb.VOLUME_FIELD_EVIDENCE["in_flight_nutrient_ml"] is Evidence.UNCONFIRMED
    assert vb.VOLUME_FIELD_EVIDENCE["uncertain_nutrient_ml"] is Evidence.UNCONFIRMED
    # kein Anspruch auf einen RO-Mengennachweis
    assert vb.VOLUME_FIELD_EVIDENCE["indirect_ro_estimate_l"] is Evidence.UNCONFIRMED
    assert vb.RO_AMOUNT_BASIS == "indirect_level_minus_confirmed"


def test_confirmed_amount_is_booked_and_reduces_capacity():
    ev = evaluate_volume_state(make_plan(), state(mixer=100.0, confirmed=100.0))
    assert ev.ok
    assert ev.still_possible_nutrient_ml == 400.0
    assert ev.remaining_capacity_ul == 84_600_000
    assert ev.indirect_ro_estimate_l == pytest.approx(99.9)


def test_in_flight_is_counted_conservatively_and_never_releases_capacity():
    base = evaluate_volume_state(make_plan(), state(mixer=100.0, confirmed=100.0))
    flying = evaluate_volume_state(make_plan(), state(mixer=100.0, confirmed=100.0, in_flight=40.0))
    assert flying.ok
    assert flying.in_flight_nutrient_ml == 40.0
    # in flight steckt bereits in "geplant - bestaetigt": keine Doppelbuchung, keine Mehr-Kapazitaet
    assert flying.remaining_capacity_ul == base.remaining_capacity_ul
    assert flying.not_started_nutrient_ml == 360.0


def test_uncertain_amount_is_kept_separate_and_still_counts_against_capacity():
    ev = evaluate_volume_state(make_plan(), state(mixer=100.0, confirmed=100.0, uncertain=10.0))
    assert ev.ok
    assert ev.uncertain_nutrient_ml == 10.0
    assert ev.still_possible_nutrient_ml == 400.0
    assert ev.remaining_capacity_ul == 84_600_000


def test_confirmed_above_planned_is_rejected():
    ev = evaluate_volume_state(make_plan(), state(mixer=1.0, confirmed=500.001))
    assert not ev.ok and ev.reason is VolumeReason.CONFIRMED_EXCEEDS_PLANNED
    assert ev.remaining_capacity_ul is None


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(confirmed=400.0, in_flight=100.001),
        dict(confirmed=300.0, in_flight=100.0, uncertain=100.001),
        dict(in_flight=500.001),
        dict(uncertain=500.001),
    ],
)
def test_double_booking_is_rejected(kwargs):
    ev = evaluate_volume_state(make_plan(), state(mixer=1.0, **kwargs))
    assert not ev.ok and ev.reason is VolumeReason.NUTRIENT_ACCOUNTING_INCONSISTENT


def test_full_booking_without_overlap_is_allowed():
    ev = evaluate_volume_state(make_plan(), state(mixer=50.0, confirmed=200.0, in_flight=200.0, uncertain=100.0))
    assert ev.ok and ev.not_started_nutrient_ml == 0.0


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_invalid_mixer_value_gives_machine_readable_reason(bad):
    ev = evaluate_volume_state(make_plan(), state(mixer=bad))
    assert not ev.ok and ev.reason is VolumeReason.INVALID_INPUT


@pytest.mark.parametrize("field", ["confirmed", "in_flight", "uncertain"])
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_invalid_nutrient_values_are_rejected(field, bad):
    ev = evaluate_volume_state(make_plan(), state(mixer=1.0, **{field: bad}))
    assert not ev.ok and ev.reason is VolumeReason.INVALID_INPUT


def test_negative_values_are_rejected():
    assert evaluate_volume_state(make_plan(), state(mixer=-0.1)).reason is VolumeReason.NEGATIVE_VALUE
    assert evaluate_volume_state(make_plan(), state(mixer=1.0, in_flight=-1.0)).reason is VolumeReason.NEGATIVE_VALUE


def test_mixer_above_limit_even_without_open_nutrients():
    plan = make_plan(target_ro_water_l=180.0, estimated_process_volume_l=185.0, planned_nutrient_ml=5000.0)
    assert evaluate_volume_state(plan, state(mixer=185.0, confirmed=5000.0)).ok
    ev = evaluate_volume_state(plan, state(mixer=185.001, confirmed=5000.0))
    assert ev.reason is VolumeReason.MIXER_ABOVE_LIMIT
    assert evaluate_volume_state(plan, state(mixer=190.0, confirmed=5000.0)).reason is VolumeReason.MIXER_ABOVE_LIMIT


def test_capacity_near_185_l_counts_open_nutrient_pumping():
    plan = make_plan(target_ro_water_l=180.0, estimated_process_volume_l=185.0, planned_nutrient_ml=5000.0)
    exactly = evaluate_volume_state(plan, state(mixer=180.0, confirmed=0.0, in_flight=40.0))
    assert exactly.ok and exactly.remaining_capacity_ul == 0
    over = evaluate_volume_state(plan, state(mixer=180.001, confirmed=0.0, in_flight=40.0))
    assert not over.ok and over.reason is VolumeReason.CAPACITY_EXCEEDED
    # nach vollstaendiger Bestaetigung bleibt der Stand bis 185.0 zulaessig
    assert evaluate_volume_state(plan, state(mixer=185.0, confirmed=5000.0)).remaining_capacity_ul == 0


def test_operative_mixer_limit_below_185_is_used():
    plan = make_plan(max_mixer_l=150.0)
    ev = evaluate_volume_state(plan, state(mixer=150.001))
    assert ev.reason is VolumeReason.MIXER_ABOVE_LIMIT


def test_capacity_is_floored_to_microliters_and_never_overestimated():
    plan = make_plan()
    mixer = 0.1 + 0.2  # 0.30000000000000004
    ev = evaluate_volume_state(plan, state(mixer=mixer))
    exact_margin_ul = (Fraction(185) - Fraction(mixer)) * 1_000_000 - 500_000
    assert ev.remaining_capacity_ul <= exact_margin_ul
    assert ev.remaining_capacity_ul > exact_margin_ul - 1


def test_invalid_plan_is_reported_not_raised():
    bad = VolumePlan(target_ro_water_l=190.0, estimated_process_volume_l=190.0, planned_nutrient_ml=0.0, max_mixer_l=185.0)
    ev = evaluate_volume_state(bad, state(mixer=1.0))
    assert not ev.ok and ev.reason is VolumeReason.RO_TARGET_ABOVE_LIMIT


@pytest.mark.parametrize("target", [0.0, -1.0, 100.501, float("nan"), True, "100"])
def test_invalid_phase_target_is_rejected(target):
    ev = evaluate_volume_state(make_plan(), state(mixer=1.0), phase_target_l=target)
    assert not ev.ok and ev.reason is VolumeReason.PHASE_TARGET_INVALID


def test_target_reached_is_exact_at_the_boundary():
    plan = make_plan()
    assert evaluate_volume_state(plan, state(mixer=30.0), phase_target_l=30.0).target_reached is True
    assert evaluate_volume_state(plan, state(mixer=29.999999999), phase_target_l=30.0).target_reached is False
    assert evaluate_volume_state(plan, state(mixer=1.0)).target_reached is None


def test_no_mutation_of_inputs_or_results():
    plan, st = make_plan(), state(mixer=10.0, confirmed=10.0)
    before = (dataclasses.astuple(plan), dataclasses.astuple(st))
    ev = evaluate_volume_state(plan, st)
    assert (dataclasses.astuple(plan), dataclasses.astuple(st)) == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.ok = False  # type: ignore[misc]
    assert isinstance(ev, VolumeEvaluation)


def test_state_from_ul_is_strict():
    st = VolumeState.from_ul(mixer_measured_l=5.0, confirmed_ul=12_345, in_flight_ul=1, uncertain_ul=0)
    assert st.confirmed_nutrient_ml == 12.345 and st.in_flight_nutrient_ml == 0.001
    for bad in (-1, 1.0, True, "1"):
        with pytest.raises(VolumeModelError):
            VolumeState.from_ul(mixer_measured_l=5.0, confirmed_ul=bad)  # type: ignore[arg-type]


# --- Phasenziele -------------------------------------------------------------------------


def test_prefill_target_is_returned_and_missing_prefill_raises():
    assert prefill_phase_target_l(make_plan()) == 30.0
    with pytest.raises(VolumeModelError):
        prefill_phase_target_l(make_plan(planned_nutrient_ml=0.0, estimated_process_volume_l=100.0, prefill_target_l=None))


def test_dynamic_target_rises_with_confirmed_amount_up_to_the_recipe_volume():
    plan = make_plan()
    assert dynamic_phase_target_l(plan, 0.0) == 100.0
    assert dynamic_phase_target_l(plan, 250.0) == 100.25
    assert dynamic_phase_target_l(plan, 500.0) == plan.estimated_process_volume_l
    targets = [dynamic_phase_target_l(plan, ml) for ml in (0.0, 1.0, 10.0, 250.0, 500.0)]
    assert targets == sorted(targets)


def test_dynamic_target_rejects_more_than_planned_and_invalid_values():
    plan = make_plan()
    with pytest.raises(VolumeModelError) as exc:
        dynamic_phase_target_l(plan, 500.001)
    assert exc.value.reason is VolumeReason.CONFIRMED_EXCEEDS_PLANNED
    for bad in BAD_NUMBERS:
        with pytest.raises(VolumeModelError):
            dynamic_phase_target_l(plan, bad)


def test_dynamic_target_equals_estimate_for_realistic_recipe_amounts():
    plan = make_plan(target_ro_water_l=50.1234, estimated_process_volume_l=50.623, planned_nutrient_ml=500.0, prefill_target_l=10.0)
    assert dynamic_phase_target_l(plan, 500.0) == plan.estimated_process_volume_l


# --- eingefrorenes Ziel / Bestaetigung ---------------------------------------------------------


def test_frozen_target_is_not_moved_by_later_confirmations():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 250.0)
    assert frozen == FrozenPhaseTarget(target_l=100.25, confirmed_nutrient_ml_at_freeze=250.0)

    # waehrend der Bestaetigung steigt die bestaetigte Menge (Chunk endet)
    later = state(mixer=100.25, confirmed=300.0, in_flight=0.0)
    assert dynamic_phase_target_l(plan, 300.0) == 100.3  # das dynamische Ziel waere hoeher
    result = evaluate_target_confirmation(plan, frozen, later)
    assert result.ok and result.reached is True and result.quiescent is True and result.confirmable is True
    assert result.target_l == 100.25


def test_confirmation_requires_quiescence():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 250.0)
    result = evaluate_target_confirmation(plan, frozen, state(mixer=100.3, confirmed=250.0, in_flight=10.0))
    assert result.ok and result.reached is True
    assert result.quiescent is False and result.confirmable is False


def test_confirmation_below_frozen_target_is_not_confirmable_but_valid_input():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 250.0)
    result = evaluate_target_confirmation(plan, frozen, state(mixer=100.249, confirmed=250.0))
    assert result.ok and result.reached is False and result.confirmable is False


def test_confirmed_amount_must_not_decrease_after_freeze():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 250.0)
    result = evaluate_target_confirmation(plan, frozen, state(mixer=101.0, confirmed=249.0))
    assert not result.ok and result.reason is VolumeReason.CONFIRMED_DECREASED and not result.confirmable


def test_tampered_or_invalid_frozen_target_fails_closed():
    plan = make_plan()
    tampered = FrozenPhaseTarget(target_l=100.0, confirmed_nutrient_ml_at_freeze=250.0)
    assert evaluate_target_confirmation(plan, tampered, state(mixer=101.0, confirmed=250.0)).reason is VolumeReason.FROZEN_TARGET_MISMATCH
    assert evaluate_target_confirmation(plan, "x", state(mixer=1.0)).reason is VolumeReason.INVALID_INPUT  # type: ignore[arg-type]
    over = FrozenPhaseTarget(target_l=100.5, confirmed_nutrient_ml_at_freeze=600.0)
    assert not evaluate_target_confirmation(plan, over, state(mixer=1.0)).ok


def test_confirmation_passes_through_invalid_volume_states():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 0.0)
    result = evaluate_target_confirmation(plan, frozen, state(mixer=186.0))
    assert not result.ok and result.reason is VolumeReason.MIXER_ABOVE_LIMIT and result.confirmable is False


# --- Abschluss -----------------------------------------------------------------------------------


def test_final_volume_ok_when_everything_is_confirmed_and_level_reached():
    plan = make_plan()
    ev = evaluate_final_volume(plan, state(mixer=100.5, confirmed=500.0))
    assert ev.ok and ev.target_reached is True


def test_final_volume_rejects_low_level_incomplete_and_open_amounts():
    plan = make_plan()
    assert evaluate_final_volume(plan, state(mixer=100.499, confirmed=500.0)).reason is VolumeReason.FINAL_LEVEL_BELOW_TARGET
    assert evaluate_final_volume(plan, state(mixer=100.5, confirmed=499.0)).reason is VolumeReason.DOSING_INCOMPLETE
    assert evaluate_final_volume(plan, state(mixer=100.5, confirmed=490.0, in_flight=10.0)).reason is VolumeReason.DOSING_INCOMPLETE
    assert evaluate_final_volume(plan, state(mixer=100.5, confirmed=490.0, uncertain=10.0)).reason is VolumeReason.DOSING_INCOMPLETE
    assert evaluate_final_volume(plan, state(mixer=100.5, confirmed=600.0)).reason is VolumeReason.CONFIRMED_EXCEEDS_PLANNED


# --- Fortschritt und RO-Wiederaufnahme ---------------------------------------------------------------


def resume(st, **overrides):
    params = dict(progress_threshold=0.5, phase_baseline_l=30.0, max_negative_drift_l=2.0, ro_on_count=1)
    params.update(overrides)
    return evaluate_ro_resume(make_plan(), st, **params)


def test_dosing_progress_counts_only_confirmed_amounts():
    plan = make_plan()
    assert dosing_progress(plan, state(mixer=30.0, confirmed=250.0, in_flight=100.0)) == 0.5
    assert dosing_progress(plan, state(mixer=30.0)) == 0.0
    assert dosing_progress(plan, state(mixer=-1.0)) is None
    assert dosing_progress(make_plan(planned_nutrient_ml=0.0, estimated_process_volume_l=100.0, prefill_target_l=None), state(mixer=1.0)) is None


def test_resume_allowed_at_threshold_and_not_below():
    ok = resume(state(mixer=30.0, confirmed=250.0))
    assert ok.allowed and ok.progress == 0.5
    low = resume(state(mixer=30.0, confirmed=249.999))
    assert not low.allowed and low.reason is VolumeReason.RESUME_PROGRESS_BELOW_THRESHOLD


def test_resume_only_after_exactly_one_ro_start():
    for count in (0, 2, 3):
        assert resume(state(mixer=30.0, confirmed=250.0), ro_on_count=count).reason is VolumeReason.RESUME_RO_ON_COUNT_INVALID


def test_resume_blocked_by_uncertain_amount_and_level_below_baseline():
    assert resume(state(mixer=30.0, confirmed=250.0, uncertain=1.0)).reason is VolumeReason.RESUME_UNCERTAIN_NUTRIENT
    assert resume(state(mixer=27.999, confirmed=250.0)).reason is VolumeReason.RESUME_LEVEL_BELOW_BASELINE
    assert resume(state(mixer=28.0, confirmed=250.0)).allowed


def test_resume_allows_in_flight_but_passes_capacity_failure_through():
    assert resume(state(mixer=30.0, confirmed=250.0, in_flight=50.0)).allowed
    plan = make_plan(target_ro_water_l=180.0, estimated_process_volume_l=185.0, planned_nutrient_ml=5000.0)
    decision = evaluate_ro_resume(
        plan, state(mixer=183.0, confirmed=2500.0), progress_threshold=0.5, phase_baseline_l=30.0, max_negative_drift_l=2.0, ro_on_count=1
    )
    assert not decision.allowed and decision.reason is VolumeReason.CAPACITY_EXCEEDED


def test_resume_without_planned_dosing_and_with_invalid_parameters():
    no_dosing = make_plan(planned_nutrient_ml=0.0, estimated_process_volume_l=100.0, prefill_target_l=None)
    d = evaluate_ro_resume(no_dosing, state(mixer=1.0), progress_threshold=0.5, phase_baseline_l=0.0, max_negative_drift_l=0.0, ro_on_count=1)
    assert d.reason is VolumeReason.NO_DOSING_PLANNED
    for name, bad in [
        ("progress_threshold", 0.0),
        ("progress_threshold", 1.01),
        ("progress_threshold", True),
        ("progress_threshold", float("nan")),
        ("phase_baseline_l", "30"),
        ("max_negative_drift_l", float("inf")),
        ("ro_on_count", True),
        ("ro_on_count", 1.0),
    ]:
        assert resume(state(mixer=30.0, confirmed=250.0), **{name: bad}).reason is VolumeReason.INVALID_INPUT
    assert resume(state(mixer=30.0, confirmed=250.0), phase_baseline_l=-1.0).reason is VolumeReason.NEGATIVE_VALUE


# --- Reinheit --------------------------------------------------------------------------------------


def test_module_has_no_io_hardware_time_or_thread_imports():
    forbidden = {"os", "sys", "socket", "serial", "threading", "time", "datetime", "pathlib", "subprocess", "random", "gpiozero", "paho"}
    source = Path(vb.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not (imported & forbidden), imported & forbidden
    assert math.isfinite(vb.UL_PER_L)


# --- Review-Korrektur B1: unsichere Menge sperrt die Zielbestaetigung -------------------------------------


def test_review_case_uncertain_amount_must_not_be_confirmable():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 100.0)
    assert frozen.target_l == 100.1
    result = evaluate_target_confirmation(plan, frozen, state(mixer=100.1, confirmed=100.0, in_flight=0.0, uncertain=10.0))
    assert result.confirmable is False
    assert result.ok is False and result.reason is VolumeReason.UNCERTAIN_NUTRIENT_PRESENT
    # Diagnose bleibt erhalten: Ziel erreicht und nichts in flight - trotzdem gesperrt
    assert result.reached is True and result.quiescent is True
    assert result.target_l == 100.1 and result.evaluation is not None and result.evaluation.ok


def test_same_state_without_uncertain_amount_is_confirmable():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 100.0)
    result = evaluate_target_confirmation(plan, frozen, state(mixer=100.1, confirmed=100.0))
    assert result.ok and result.reason is None
    assert result.confirmable is True


@pytest.mark.parametrize("uncertain", [0.001, 10.0, 300.0])
@pytest.mark.parametrize("in_flight", [0.0, 5.0])
@pytest.mark.parametrize("mixer", [100.0, 100.1, 150.0])
def test_confirmable_requires_all_four_conditions(uncertain, in_flight, mixer):
    plan = make_plan()
    frozen = freeze_phase_target(plan, 100.0)
    result = evaluate_target_confirmation(plan, frozen, state(mixer=mixer, confirmed=100.0, in_flight=in_flight, uncertain=uncertain))
    assert result.confirmable is False
    assert result.reason is VolumeReason.UNCERTAIN_NUTRIENT_PRESENT


def test_every_single_blocker_prevents_confirmation():
    plan = make_plan()
    frozen = freeze_phase_target(plan, 100.0)
    cases = {
        "invalid_state": state(mixer=186.0, confirmed=100.0),
        "target_not_reached": state(mixer=100.099, confirmed=100.0),
        "in_flight": state(mixer=100.1, confirmed=100.0, in_flight=1.0),
        "uncertain": state(mixer=100.1, confirmed=100.0, uncertain=1.0),
    }
    for name, st in cases.items():
        assert evaluate_target_confirmation(plan, frozen, st).confirmable is False, name
    assert evaluate_target_confirmation(plan, frozen, state(mixer=100.1, confirmed=100.0)).confirmable is True


# --- Review-Korrektur B4: grosse Ganzzahlen ----------------------------------------------------------------

HUGE = [10**400, -(10**400)]


@pytest.mark.parametrize("huge", HUGE)
def test_is_finite_number_rejects_non_representable_integers(huge):
    assert vb.is_finite_number(huge) is False
    assert vb.is_finite_number(10**300) is True
    assert vb.is_finite_number(True) is False and vb.is_finite_number(float("nan")) is False


@pytest.mark.parametrize("huge", HUGE)
def test_ml_to_ul_and_ul_to_ml_reject_huge_integers_controlled(huge):
    with pytest.raises(VolumeModelError) as exc:
        ml_to_ul(huge)
    assert exc.value.reason in (VolumeReason.INVALID_INPUT, VolumeReason.NEGATIVE_VALUE)
    with pytest.raises(VolumeModelError):
        vb.ul_to_ml(10**400)


@pytest.mark.parametrize("huge", HUGE)
@pytest.mark.parametrize("field", ["mixer", "confirmed", "in_flight", "uncertain"])
def test_evaluate_volume_state_is_total_for_huge_integers(field, huge):
    values = dict(mixer=1.0, confirmed=0.0, in_flight=0.0, uncertain=0.0)
    values[field] = huge
    ev = evaluate_volume_state(make_plan(), state(**values))
    assert not ev.ok and ev.reason in (VolumeReason.INVALID_INPUT, VolumeReason.NEGATIVE_VALUE)


def test_huge_but_representable_mixer_value_is_a_limit_violation_not_an_overflow():
    ev = evaluate_volume_state(make_plan(), state(mixer=10**300))
    assert not ev.ok and ev.reason is VolumeReason.MIXER_ABOVE_LIMIT
    ev2 = evaluate_volume_state(make_plan(), state(mixer=1.0, confirmed=10**300))
    assert not ev2.ok and ev2.reason is VolumeReason.CONFIRMED_EXCEEDS_PLANNED


@pytest.mark.parametrize("huge", HUGE)
def test_all_other_public_evaluations_handle_huge_integers(huge):
    plan = make_plan()
    assert evaluate_volume_state(plan, state(mixer=1.0), phase_target_l=huge).reason is VolumeReason.PHASE_TARGET_INVALID
    with pytest.raises(VolumeModelError):
        dynamic_phase_target_l(plan, huge)
    with pytest.raises(VolumeModelError):
        freeze_phase_target(plan, huge)
    assert evaluate_final_volume(plan, state(mixer=huge, confirmed=500.0)).ok is False
    assert evaluate_final_volume(plan, state(mixer=100.5, confirmed=huge)).ok is False
    assert dosing_progress(plan, state(mixer=huge)) is None
    frozen = freeze_phase_target(plan, 100.0)
    assert evaluate_target_confirmation(plan, frozen, state(mixer=huge, confirmed=100.0)).confirmable is False
    assert evaluate_target_confirmation(plan, FrozenPhaseTarget(huge, 100.0), state(mixer=1.0)).ok is False
    assert evaluate_target_confirmation(plan, FrozenPhaseTarget(100.1, huge), state(mixer=1.0)).ok is False
    for name in ("progress_threshold", "phase_baseline_l", "max_negative_drift_l"):
        assert resume(state(mixer=30.0, confirmed=250.0), **{name: huge}).allowed is False
    assert resume(state(mixer=30.0, confirmed=250.0), ro_on_count=10**400).allowed is False


@pytest.mark.parametrize("huge", HUGE)
@pytest.mark.parametrize(
    "field",
    ["target_ro_water_l", "estimated_process_volume_l", "planned_nutrient_ml", "max_mixer_l", "prefill_target_l"],
)
def test_volume_plan_creation_rejects_huge_integers_without_overflow(field, huge):
    with pytest.raises(VolumeModelError):
        make_plan(**{field: huge})
    bad = dataclasses.replace(make_plan(), **{field: huge})
    assert evaluate_volume_state(bad, state(mixer=1.0)).ok is False
    with pytest.raises(VolumeModelError):
        expected_target_total_l(bad)


@pytest.mark.parametrize("huge", HUGE)
def test_state_from_ul_rejects_huge_values_controlled(huge):
    with pytest.raises(VolumeModelError):
        VolumeState.from_ul(mixer_measured_l=1.0, confirmed_ul=abs(huge))


# --- Rundung dynamischer Ziele -----------------------------------------------------------------------------------


def test_dynamic_target_rounding_deviation_is_at_most_half_a_millilitre():
    from fractions import Fraction

    worst = Fraction(0)
    for ro in (100.0, 50.1234, 57.3, 179.9995, 12.0005):
        planned_ml = 1000.0 if ro < 150 else 2000.0
        est = round(ro + planned_ml / 1000, 3)
        plan = make_plan(target_ro_water_l=ro, estimated_process_volume_l=est, planned_nutrient_ml=planned_ml, prefill_target_l=1.0)
        previous = 0.0
        for confirmed_ul in list(range(0, 3001, 7)) + [int(planned_ml * 1000)]:
            confirmed_ml = confirmed_ul / 1000
            if confirmed_ml > planned_ml:
                continue
            target = dynamic_phase_target_l(plan, confirmed_ml)
            exact = Fraction(ro) + Fraction(confirmed_ul, 1_000_000)
            deviation = abs(Fraction(target) - exact)
            worst = max(worst, deviation)
            assert deviation <= Fraction(5, 10_000) + Fraction(1, 10**9)  # 0.5 ml + Gleitkomma-Rauschen
            assert target >= previous  # monoton
            assert target <= plan.estimated_process_volume_l
            previous = target
    assert worst > Fraction(2, 10_000)  # die Abweichung tritt tatsaechlich in der Groessenordnung auf


def test_dynamic_target_rounding_extremes_are_half_a_millilitre_in_both_directions():
    plan = make_plan()  # RO 100.0 l
    # exakt 100.000499 l -> 100.0: die RO-Pumpe stoppt bis zu ~0.5 ml frueher (Schlusspruefung laxer)
    assert dynamic_phase_target_l(plan, 0.499) == 100.0
    # exakt 100.0005 l -> 100.001: bis zu 0.5 ml strenger
    assert dynamic_phase_target_l(plan, 0.5) == 100.001


def test_evaluation_functions_are_total_for_hostile_values():
    """evaluate_* duerfen fuer keine Eingabe eine Exception werfen."""
    hostile = [10**400, -(10**400), 10**308, 2**1024, 1e308, -1e308, float("nan"), True, "x", None]
    plan = make_plan()
    frozen = freeze_phase_target(plan, 100.0)
    for value in hostile:
        for field in ("mixer_measured_l", "confirmed_nutrient_ml", "in_flight_nutrient_ml", "uncertain_nutrient_ml"):
            st = dataclasses.replace(VolumeState(1.0, 100.0), **{field: value})
            assert evaluate_volume_state(plan, st, phase_target_l=value).ok in (True, False)
            assert evaluate_target_confirmation(plan, frozen, st).confirmable in (True, False)
            assert evaluate_final_volume(plan, st).ok in (True, False)
            assert evaluate_ro_resume(
                plan, st, progress_threshold=value, phase_baseline_l=value, max_negative_drift_l=value, ro_on_count=value
            ).allowed in (True, False)
        assert evaluate_target_confirmation(plan, value, VolumeState(1.0)).confirmable is False  # type: ignore[arg-type]
        assert evaluate_target_confirmation(plan, FrozenPhaseTarget(value, value), VolumeState(1.0)).confirmable is False  # type: ignore[arg-type]


# --- Review R3, B4.1: extrem grosse Ganzzahlen (> sys.get_int_max_str_digits) ----------------------------------------

import sys

HUGE5000 = [10**5000, -(10**5000)]


def test_int_string_conversion_limit_stays_enabled_and_safe_repr_does_not_need_it():
    assert sys.get_int_max_str_digits() != 0
    text = vb.safe_repr(10**5000)
    assert "Bit" in text and len(text) < 100
    assert vb.safe_repr(12345) == "12345" and vb.safe_repr(True) == "True" and vb.safe_repr(None) == "None"
    assert len(vb.safe_repr("x" * 10_000)) <= 83
    assert vb.safe_repr(object.__new__(type("Bad", (), {"__repr__": lambda self: 1 / 0}))) == "<Bad>"


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
def test_ml_to_ul_with_extremely_large_integers_raises_only_the_domain_error(huge):
    with pytest.raises(VolumeModelError) as exc:
        ml_to_ul(huge)
    assert exc.value.reason in (VolumeReason.INVALID_INPUT, VolumeReason.NEGATIVE_VALUE)
    assert len(str(exc.value)) < 500
    with pytest.raises(VolumeModelError):
        vb.ul_to_ml(10**5000)


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
@pytest.mark.parametrize("field", ["mixer", "confirmed", "in_flight", "uncertain"])
def test_evaluate_volume_state_returns_a_result_for_extremely_large_integers(field, huge):
    values = dict(mixer=1.0, confirmed=0.0, in_flight=0.0, uncertain=0.0)
    values[field] = huge
    ev = evaluate_volume_state(make_plan(), state(**values))
    assert not ev.ok and ev.reason in (VolumeReason.INVALID_INPUT, VolumeReason.NEGATIVE_VALUE)
    assert ev.message is not None and len(ev.message) < 500


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
@pytest.mark.parametrize(
    "field", ["target_ro_water_l", "estimated_process_volume_l", "planned_nutrient_ml", "max_mixer_l", "prefill_target_l"]
)
def test_volume_plan_with_extremely_large_integers_raises_only_the_domain_error(field, huge):
    with pytest.raises(VolumeModelError):
        make_plan(**{field: huge})
    bad = dataclasses.replace(make_plan(), **{field: huge})
    assert evaluate_volume_state(bad, state(mixer=1.0)).ok is False
    assert vb.check_volume_plan(bad) is not None
    with pytest.raises(VolumeModelError):
        expected_target_total_l(bad)
    with pytest.raises(VolumeModelError):
        prefill_phase_target_l(bad)


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
def test_other_functions_handle_extremely_large_integers(huge):
    plan = make_plan()
    frozen = freeze_phase_target(plan, 100.0)
    assert evaluate_volume_state(plan, state(mixer=1.0), phase_target_l=huge).reason is VolumeReason.PHASE_TARGET_INVALID
    with pytest.raises(VolumeModelError):
        dynamic_phase_target_l(plan, huge)
    with pytest.raises(VolumeModelError):
        freeze_phase_target(plan, huge)
    with pytest.raises(VolumeModelError):
        VolumeState.from_ul(mixer_measured_l=1.0, confirmed_ul=abs(huge))
    with pytest.raises(VolumeModelError):
        VolumeState.from_ul(mixer_measured_l=1.0, confirmed_ul=huge)
    assert dosing_progress(plan, state(mixer=huge)) is None
    assert evaluate_final_volume(plan, state(mixer=huge, confirmed=500.0)).ok is False
    assert evaluate_target_confirmation(plan, frozen, state(mixer=huge, confirmed=100.0)).confirmable is False
    assert evaluate_target_confirmation(plan, FrozenPhaseTarget(huge, huge), state(mixer=1.0)).ok is False
    assert evaluate_target_confirmation(plan, huge, state(mixer=1.0)).ok is False  # type: ignore[arg-type]
    for name in ("progress_threshold", "phase_baseline_l", "max_negative_drift_l"):
        assert resume(state(mixer=30.0, confirmed=250.0), **{name: huge}).allowed is False
    decision = resume(state(mixer=30.0, confirmed=250.0), ro_on_count=huge)
    assert decision.allowed is False and decision.reason is VolumeReason.RESUME_RO_ON_COUNT_INVALID
    assert decision.message is not None and len(decision.message) < 500
