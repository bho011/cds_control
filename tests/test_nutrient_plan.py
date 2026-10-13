"""AP-05 Stufe 1: domain/recipe/nutrient_plan.py - reiner Dosierplan.

Keine Hardware, kein Netzwerk, keine Dateien. Gewichte und Mengen sind
Testdaten, keine Prozessparameter oder Freigaben.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from domain.recipe import nutrient_plan as np_mod
from domain.recipe.models import RunConfigSnapshot, RunOptions, StoredRecipe
from domain.recipe.nutrient_plan import (
    ABSOLUTE_MAX_CHUNK_ML,
    PUMP_ROLES,
    DoseChunk,
    NutrientDosingPlan,
    NutrientPlanError,
    PlanErrorCode,
    build_nutrient_dosing_plan,
    plan_chunks_ul,
    split_pair_ul,
)
from domain.recipe.volume_balance import expected_target_total_l
from domain.recipe_limits import RecipeValidationError
from services.peristaltic.models import default_mapping
from services.peristaltic.protocol import FIRMWARE_MAX_SINGLE_DOSE_ML, format_dose_command

EQUAL_WEIGHTS = {"P1": 0.0002, "P2": 0.0002, "P3": 0.0002, "P4": 0.0002}
# Testdaten in der Groessenordnung der gespeicherten MCU_B-Profilwerte; keine Freigabe.
INDIVIDUAL_WEIGHTS = {
    "P1": 0.000192624768,
    "P2": 0.000191096,
    "P3": 0.000175235032,
    "P4": 0.000197402168,
}
BAD_NUMBERS = [True, False, "1", None, float("nan"), float("inf"), -float("inf"), [1]]


def make_snapshot(**recipe_overrides) -> RunConfigSnapshot:
    values = dict(
        slot=1,
        recipe_name="Testrezept",
        target_ro_water_l=100.0,
        target_ec_ms_cm=1.5,
        ec_mixing_time_seconds=300,
        nutrients_ml_per_100_l=500.0,
        nutrient_a_percent=50.0,
        ec_adjustment_factor=1.0,
        nutrient_dosing_enabled=True,
    )
    values.update(recipe_overrides)
    return RunConfigSnapshot.build(StoredRecipe(**values), {"max_mixer_liters": 185.0}, RunOptions())


def params(**overrides):
    values = dict(
        pump_roles=dict(PUMP_ROLES),
        pump_weights=dict(EQUAL_WEIGHTS),
        profile_id="test_profile",
        max_chunk_ml=10.0,
        prefill_target_l=30.0,
    )
    values.update(overrides)
    return values


def build(snapshot=None, **overrides) -> NutrientDosingPlan:
    return build_nutrient_dosing_plan(snapshot if snapshot is not None else make_snapshot(), **params(**overrides))


def code_of(call) -> PlanErrorCode:
    with pytest.raises(NutrientPlanError) as exc:
        call()
    return exc.value.code


def assert_invariants(plan: NutrientDosingPlan) -> None:
    targets = {t.pump: t for t in plan.pump_targets}
    assert plan.a_ul + plan.b_ul == plan.total_ul
    assert targets["P1"].target_ul + targets["P3"].target_ul == plan.a_ul
    assert targets["P2"].target_ul + targets["P4"].target_ul == plan.b_ul
    assert sum(t.target_ul for t in plan.pump_targets) == plan.total_ul
    max_ul = round(plan.max_chunk_ml * 1000)
    for target in plan.pump_targets:
        assert sum(target.chunks_ul) == target.target_ul
        assert all(1 <= chunk <= max_ul for chunk in target.chunks_ul)
        assert (target.target_ul == 0) == (target.chunks_ul == ())
    assert plan.verify_hash()


# --- Verteilung -----------------------------------------------------------------------------


def test_50_50_distribution_with_equal_weights():
    plan = build()
    assert (plan.total_ul, plan.a_ul, plan.b_ul) == (500_000, 250_000, 250_000)
    assert [(t.pump, t.target_ul) for t in plan.pump_targets] == [
        ("P1", 125_000),
        ("P2", 125_000),
        ("P3", 125_000),
        ("P4", 125_000),
    ]
    assert_invariants(plan)


def test_40_60_distribution():
    plan = build(make_snapshot(nutrient_a_percent=40.0))
    assert (plan.a_ul, plan.b_ul) == (200_000, 300_000)
    assert plan.pump_target("P1").target_ul + plan.pump_target("P3").target_ul == 200_000
    assert plan.pump_target("P2").target_ul + plan.pump_target("P4").target_ul == 300_000
    assert_invariants(plan)


@pytest.mark.parametrize("a_percent,a_ul,b_ul", [(100.0, 500_000, 0), (0.0, 0, 500_000)])
def test_one_sided_recipes_create_no_chunks_for_the_empty_solution(a_percent, a_ul, b_ul):
    plan = build(make_snapshot(nutrient_a_percent=a_percent))
    assert (plan.a_ul, plan.b_ul) == (a_ul, b_ul)
    empty = ("P2", "P4") if b_ul == 0 else ("P1", "P3")
    for pump in empty:
        assert plan.pump_target(pump).target_ul == 0
        assert plan.pump_target(pump).chunks_ul == ()
        assert plan.pump_target(pump).chunk_count == 0
    used = {chunk.pump for round_ in plan.rounds() for chunk in round_}
    assert used.isdisjoint(empty)
    assert_invariants(plan)


def test_pump_assignment_is_fixed_and_matches_the_stored_mapping():
    assert PUMP_ROLES == default_mapping().controllers["MCU_B"].pumps
    plan = build()
    assert {t.pump: (t.role, t.solution) for t in plan.pump_targets} == {
        "P1": ("nutrient_a_1", "A"),
        "P3": ("nutrient_a_2", "A"),
        "P2": ("nutrient_b_1", "B"),
        "P4": ("nutrient_b_2", "B"),
    }
    assert plan.controller == "MCU_B"


def test_individual_profile_weights_are_used_and_remembered():
    plan = build(pump_weights=dict(INDIVIDUAL_WEIGHTS))
    assert [(t.pump, t.target_ul) for t in plan.pump_targets] == [
        ("P1", 130_909),
        ("P2", 122_971),
        ("P3", 119_091),
        ("P4", 127_029),
    ]
    assert {t.pump: t.weight_ml_per_step for t in plan.pump_targets} == INDIVIDUAL_WEIGHTS
    assert plan.profile_id == "test_profile"
    assert_invariants(plan)


def test_odd_microliter_amount_goes_to_the_first_pump_with_equal_weights():
    plan = build(make_snapshot(nutrients_ml_per_100_l=0.003, nutrient_a_percent=100.0))
    assert plan.total_ul == 3 and plan.a_ul == 3
    assert (plan.pump_target("P1").target_ul, plan.pump_target("P3").target_ul) == (2, 1)
    assert_invariants(plan)


def test_split_pair_rounding_rule_and_exactness():
    assert split_pair_ul(3, 1.0, 1.0) == (2, 1)
    assert split_pair_ul(1, 1.0, 1.0) == (1, 0)
    assert split_pair_ul(0, 1.0, 2.0) == (0, 0)
    assert split_pair_ul(300, 1, 2) == (100, 200)
    for total in list(range(0, 200)) + [99_999, 250_001, 85_000_000]:
        for weights in ((1.0, 1.0), (0.000192624768, 0.000175235032), (1.0, 1e-6), (3, 7)):
            first, second = split_pair_ul(total, *weights)
            assert first + second == total and first >= 0 and second >= 0


@pytest.mark.parametrize("bad", [0, -1.0, True, "1", None, float("nan"), float("inf")])
def test_split_pair_rejects_invalid_weights(bad):
    assert code_of(lambda: split_pair_ul(10, bad, 1.0)) in (PlanErrorCode.INVALID_WEIGHTS, PlanErrorCode.INVALID_TYPES)
    assert code_of(lambda: split_pair_ul(10, 1.0, bad)) in (PlanErrorCode.INVALID_WEIGHTS, PlanErrorCode.INVALID_TYPES)


@pytest.mark.parametrize("bad", [-1, 1.0, True, "1", None])
def test_split_pair_rejects_invalid_total(bad):
    assert code_of(lambda: split_pair_ul(bad, 1.0, 1.0)) is PlanErrorCode.INVALID_AMOUNT


@pytest.mark.parametrize(
    "overrides",
    [
        dict(),
        dict(nutrient_a_percent=40.0),
        dict(nutrient_a_percent=33.3, nutrients_ml_per_100_l=777.7),
        dict(target_ro_water_l=57.3, nutrients_ml_per_100_l=123.456, ec_adjustment_factor=0.37),
        dict(target_ro_water_l=180.0, nutrients_ml_per_100_l=1000.0),
    ],
)
@pytest.mark.parametrize("weights", [EQUAL_WEIGHTS, INDIVIDUAL_WEIGHTS])
@pytest.mark.parametrize("max_chunk", [10.0, 7.5, 1.0, 50.0])
def test_all_sums_are_exact_for_many_recipes(overrides, weights, max_chunk):
    plan = build(make_snapshot(**overrides), pump_weights=dict(weights), max_chunk_ml=max_chunk)
    assert_invariants(plan)


# --- Chunking -----------------------------------------------------------------------------------


def test_chunks_are_equal_sized_and_sum_exactly():
    assert plan_chunks_ul(25_000, 10.0) == (8334, 8333, 8333)
    assert plan_chunks_ul(20_000, 10.0) == (10_000, 10_000)
    assert plan_chunks_ul(10_001, 10.0) == (5001, 5000)
    assert plan_chunks_ul(7, 10.0) == (7,)
    assert plan_chunks_ul(1, 10.0) == (1,)
    assert plan_chunks_ul(10_000, 10.0) == (10_000,)


def test_zero_amount_creates_no_chunks():
    assert plan_chunks_ul(0, 10.0) == ()


def test_chunk_property_holds_for_a_dense_range_of_targets():
    for max_ml in (10.0, 3.3, 0.001, 50.0):
        max_ul = round(max_ml * 1000)
        targets = list(range(0, 400)) + ([] if max_ml == 0.001 else [9_999, 10_000, 10_001, 49_999, 123_457])
        for target in targets:
            chunks = plan_chunks_ul(target, max_ml)
            assert sum(chunks) == target
            assert all(1 <= c <= max_ul for c in chunks)
            if chunks:
                assert max(chunks) - min(chunks) <= 1


@pytest.mark.parametrize("bad", [0, 0.0, -1.0, True, "10", None, float("nan"), float("inf"), 50.001, 60.0, 0.0005])
def test_invalid_max_chunk_is_rejected_without_correction(bad):
    assert code_of(lambda: plan_chunks_ul(1000, bad)) is PlanErrorCode.INVALID_CHUNK_PARAMETER


@pytest.mark.parametrize("bad", [-1, 1.0, True, "5", None])
def test_invalid_chunk_target_is_rejected(bad):
    assert code_of(lambda: plan_chunks_ul(bad, 10.0)) is PlanErrorCode.INVALID_AMOUNT


def test_min_chunk_is_enforced_and_validated():
    assert plan_chunks_ul(10_001, 10.0, 5.0) == (5001, 5000)
    assert code_of(lambda: plan_chunks_ul(10_001, 10.0, 6.0)) is PlanErrorCode.CHUNK_BELOW_MINIMUM
    assert plan_chunks_ul(0, 10.0, 6.0) == ()  # Nullmenge ist kein Mindestmengenfehler
    for bad in (0, -1.0, 11.0, True, float("nan"), "1"):
        assert code_of(lambda: plan_chunks_ul(5000, 10.0, bad)) is PlanErrorCode.INVALID_CHUNK_PARAMETER


def test_plan_rejects_chunk_below_minimum_without_redistribution():
    assert code_of(lambda: build(max_chunk_ml=10.0, min_chunk_ml=200.0)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    snapshot = make_snapshot(nutrients_ml_per_100_l=10.0, nutrient_a_percent=100.0)  # 10 ml A
    assert code_of(lambda: build(snapshot, max_chunk_ml=10.0, min_chunk_ml=6.0)) is PlanErrorCode.CHUNK_BELOW_MINIMUM


def test_firmware_limit_reference_matches_the_protocol_constant():
    assert ABSOLUTE_MAX_CHUNK_ML == FIRMWARE_MAX_SINGLE_DOSE_ML


def test_rounds_are_pure_data_and_match_the_dose_protocol_format():
    plan = build(make_snapshot(nutrient_a_percent=100.0, nutrients_ml_per_100_l=25.0), pump_weights=dict(INDIVIDUAL_WEIGHTS))
    rounds = plan.rounds()
    assert plan.round_count == len(rounds) > 0
    flat = [chunk for round_ in rounds for chunk in round_]
    assert all(isinstance(chunk, DoseChunk) for chunk in flat)
    for round_ in rounds:
        assert [c.pump for c in round_] == sorted({c.pump for c in round_})  # je Pumpe hoechstens ein Chunk pro Runde
    for chunk in flat:
        assert format_dose_command(chunk.pump, chunk.amount_ml) == f"DOSE {chunk.pump} {chunk.dose_ml_text}"
    for pump in ("P1", "P3"):
        assert sum(c.amount_ul for c in flat if c.pump == pump) == plan.pump_target(pump).target_ul
    assert DoseChunk("P1", 1, 5).dose_ml_text == "0.005"
    assert DoseChunk("P1", 1, 10_000).dose_ml_text == "10.000"
    assert DoseChunk("P1", 1, 8_334).dose_ml_text == "8.334"


# --- deaktiviert / 0 ml / Legacy --------------------------------------------------------------------


def test_disabled_dosing_creates_no_work_and_needs_no_hardware_parameters():
    snapshot = make_snapshot(nutrient_dosing_enabled=False)
    plan = build_nutrient_dosing_plan(snapshot)
    assert plan.dosing_enabled is False and plan.has_dosing_work is False
    assert (plan.total_ul, plan.a_ul, plan.b_ul) == (0, 0, 0)
    assert plan.pump_targets == () and plan.rounds() == () and plan.round_count == 0
    assert plan.profile_id is None and plan.max_chunk_ml is None
    assert plan.volume.dosing_planned is False and plan.volume.prefill_target_l is None
    assert expected_target_total_l(plan.volume) == snapshot.target_ro_water_l  # heutiges Verhalten
    # Dosierparameter werden bei deaktivierter Dosierung nicht ausgewertet
    junk = build_nutrient_dosing_plan(snapshot, pump_weights={}, pump_roles=None, max_chunk_ml="x", prefill_target_l=-5)
    assert junk.pump_targets == () and junk.plan_hash == plan.plan_hash


def test_disabled_dosing_with_stored_dose_and_legacy_flag_still_creates_no_work():
    snapshot = make_snapshot(nutrient_dosing_enabled=False, nutrients_ml_per_100_l=900.0, legacy_dosing_needs_review=True)
    plan = build_nutrient_dosing_plan(snapshot)
    assert plan.has_dosing_work is False and plan.total_ul == 0


def test_disabled_snapshot_with_amounts_is_a_contradiction():
    snapshot = dataclasses.replace(make_snapshot(nutrient_dosing_enabled=False), calculated_total_nutrients_ml=10.0)
    assert code_of(lambda: build_nutrient_dosing_plan(snapshot)) is PlanErrorCode.DISABLED_BUT_AMOUNT


def test_enabled_with_zero_amount_is_rejected():
    zero = make_snapshot(nutrients_ml_per_100_l=0.0)
    assert zero.calculated_total_nutrients_ml == 0.0
    assert code_of(lambda: build(zero)) is PlanErrorCode.ENABLED_ZERO_AMOUNT


def test_legacy_recipe_without_release_cannot_be_planned():
    snapshot = dataclasses.replace(make_snapshot(), legacy_dosing_needs_review=True)
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.RECIPE_INVALID


# --- Widerspruch Snapshot <-> Berechnung ---------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        dict(calculated_nutrient_a_ml=1.0),
        dict(calculated_nutrient_b_ml=251.0),
        dict(calculated_total_nutrients_ml=501.0),
        dict(nutrient_b_percent=51.0),
        dict(estimated_process_volume_l=101.0),
        dict(calculated_nutrient_a_ml=250.001, calculated_nutrient_b_ml=250.001),
    ],
)
def test_snapshot_values_contradicting_the_calculation_are_rejected(changes):
    snapshot = dataclasses.replace(make_snapshot(), **changes)
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.SNAPSHOT_MISMATCH


@pytest.mark.parametrize("percent", [-0.1, 100.1, 120.0])
def test_invalid_percentages_are_rejected(percent):
    snapshot = dataclasses.replace(make_snapshot(), nutrient_a_percent=percent)
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.RECIPE_INVALID


def test_negative_dose_and_invalid_factor_are_rejected():
    assert code_of(lambda: build(dataclasses.replace(make_snapshot(), nutrients_ml_per_100_l=-1.0))) is PlanErrorCode.RECIPE_INVALID
    assert code_of(lambda: build(dataclasses.replace(make_snapshot(), ec_adjustment_factor=1.5))) is PlanErrorCode.RECIPE_INVALID


# --- Typen und nicht endliche Werte -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "target_ro_water_l",
        "max_mixer_liters",
        "nutrients_ml_per_100_l",
        "nutrient_a_percent",
        "ec_adjustment_factor",
        "calculated_total_nutrients_ml",
        "calculated_nutrient_a_ml",
        "calculated_nutrient_b_ml",
        "nutrient_b_percent",
        "estimated_process_volume_l",
        "target_ec_ms_cm",
    ],
)
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_invalid_numeric_snapshot_fields_are_rejected(field, bad):
    snapshot = dataclasses.replace(make_snapshot(), **{field: bad})
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.INVALID_TYPES


@pytest.mark.parametrize("field", ["nutrient_dosing_enabled", "legacy_dosing_needs_review"])
@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None, [True]])
def test_boolean_snapshot_fields_must_be_real_booleans(field, bad):
    snapshot = dataclasses.replace(make_snapshot(), **{field: bad})
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.INVALID_TYPES


def test_other_invalid_snapshot_field_types_are_rejected():
    base = make_snapshot()
    for changes in (
        dict(ec_mixing_time_seconds=300.5),
        dict(ec_mixing_time_seconds=True),
        dict(schema_version=4.0),
        dict(recipe_id=5),
        dict(recipe_id=""),
        dict(created_at=None),
        dict(recipe_name=None),
    ):
        assert code_of(lambda: build(dataclasses.replace(base, **changes))) is PlanErrorCode.INVALID_TYPES


def test_snapshot_input_form_is_validated():
    snapshot = make_snapshot()
    from_dict = build(snapshot.to_dict())
    assert from_dict.plan_hash == build(snapshot).plan_hash
    data = snapshot.to_dict()
    missing = dict(data)
    del missing["nutrient_a_percent"]
    assert code_of(lambda: build(missing)) is PlanErrorCode.INVALID_SNAPSHOT
    assert code_of(lambda: build(dict(data, surprise=1))) is PlanErrorCode.INVALID_SNAPSHOT
    for bad in (None, "snapshot", 5, [snapshot]):
        assert code_of(lambda: build_nutrient_dosing_plan(bad, **params())) is PlanErrorCode.INVALID_SNAPSHOT  # type: ignore[arg-type]


# --- Volumengrenzen ----------------------------------------------------------------------------------------


def test_180_l_ro_is_allowed_and_planned():
    snapshot = make_snapshot(target_ro_water_l=180.0, nutrients_ml_per_100_l=1000.0)  # 1800 ml
    plan = build(snapshot)
    assert plan.total_ul == 1_800_000
    assert plan.volume.target_ro_water_l == 180.0 and plan.volume.estimated_process_volume_l == 181.8
    assert_invariants(plan)


def test_ro_above_180_l_is_rejected():
    snapshot = dataclasses.replace(make_snapshot(), target_ro_water_l=180.01)
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.RECIPE_INVALID
    with pytest.raises(RecipeValidationError):
        make_snapshot(target_ro_water_l=180.01)


def test_process_volume_of_exactly_185_l_is_allowed():
    snapshot = make_snapshot(nutrients_ml_per_100_l=85_000.0)  # 100 l RO + 85 l Naehrstoffloesung
    assert snapshot.estimated_process_volume_l == 185.0
    plan = build(snapshot, max_chunk_ml=50.0)
    assert plan.total_ul == 85_000_000 and plan.volume.estimated_process_volume_l == 185.0
    assert_invariants(plan)


def test_process_volume_one_ml_above_185_l_is_rejected():
    with pytest.raises(RecipeValidationError):
        make_snapshot(nutrients_ml_per_100_l=85_001.0)
    snapshot = dataclasses.replace(
        make_snapshot(),
        nutrients_ml_per_100_l=85_001.0,
        calculated_total_nutrients_ml=85_001.0,
        calculated_nutrient_a_ml=42_500.5,
        calculated_nutrient_b_ml=42_500.5,
        estimated_process_volume_l=185.001,
    )
    assert code_of(lambda: build(snapshot, max_chunk_ml=50.0)) is PlanErrorCode.RECIPE_INVALID


def test_operative_mixer_limit_below_the_recipe_volume_is_rejected():
    snapshot = dataclasses.replace(make_snapshot(), max_mixer_liters=100.4)
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.VOLUME_INVALID


@pytest.mark.parametrize("prefill", [None, 0.0, -1.0, 100.001, True, float("nan"), float("inf"), "30"])
def test_prefill_target_must_be_positive_and_not_above_ro(prefill):
    assert code_of(lambda: build(prefill_target_l=prefill)) is PlanErrorCode.VOLUME_INVALID


def test_prefill_equal_to_ro_is_accepted():
    assert build(prefill_target_l=100.0).volume.prefill_target_l == 100.0


def test_plan_volume_model_is_consistent_with_the_snapshot():
    snapshot = make_snapshot()
    plan = build(snapshot)
    assert plan.volume.target_ro_water_l == snapshot.target_ro_water_l
    assert plan.volume.estimated_process_volume_l == snapshot.estimated_process_volume_l
    assert plan.volume.planned_nutrient_ml == snapshot.calculated_total_nutrients_ml
    assert plan.volume.max_mixer_l == snapshot.max_mixer_liters
    assert expected_target_total_l(plan.volume) == snapshot.estimated_process_volume_l


# --- Parameter ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "roles",
    [
        None,
        {},
        {"P1": "nutrient_a_1", "P2": "nutrient_b_1", "P3": "nutrient_a_2"},
        {"P1": "nutrient_a_1", "P2": "nutrient_a_2", "P3": "nutrient_b_1", "P4": "nutrient_b_2"},
        {"P1": "nutrient_b_1", "P2": "nutrient_a_1", "P3": "nutrient_b_2", "P4": "nutrient_a_2"},
        {"P1": "nutrient_a_1", "P2": "nutrient_b_1", "P3": "nutrient_a_2", "P4": "unassigned"},
        {**PUMP_ROLES, "P5": "x"},
        "MCU_B",
    ],
)
def test_wrong_or_missing_pump_assignment_is_rejected(roles):
    assert code_of(lambda: build(pump_roles=roles)) is PlanErrorCode.INVALID_PUMP_ROLES


@pytest.mark.parametrize(
    "weights",
    [
        None,
        {},
        {"P1": 1.0, "P2": 1.0, "P3": 1.0},  # P4 fehlt
        {"P1": 1.0, "P2": 1.0, "P3": 1.0, "P4": 1.0, "P5": 1.0},
        {"P1": 1.0, "P2": 1.0, "P3": 0.0, "P4": 1.0},
        {"P1": 1.0, "P2": -1.0, "P3": 1.0, "P4": 1.0},
        {"P1": float("nan"), "P2": 1.0, "P3": 1.0, "P4": 1.0},
        {"P1": float("inf"), "P2": 1.0, "P3": 1.0, "P4": 1.0},
        {"P1": True, "P2": 1.0, "P3": 1.0, "P4": 1.0},
        {"P1": "0.0002", "P2": 1.0, "P3": 1.0, "P4": 1.0},
        {"P1": None, "P2": 1.0, "P3": 1.0, "P4": 1.0},
    ],
)
def test_missing_or_invalid_profile_weights_are_rejected_without_fallback(weights):
    assert code_of(lambda: build(pump_weights=weights)) is PlanErrorCode.INVALID_WEIGHTS


def test_weights_are_required_even_when_one_solution_is_zero():
    snapshot = make_snapshot(nutrient_a_percent=100.0)
    assert code_of(lambda: build(snapshot, pump_weights={"P1": 1.0, "P3": 1.0})) is PlanErrorCode.INVALID_WEIGHTS


@pytest.mark.parametrize("profile_id", [None, "", "   ", 5, True])
def test_profile_id_is_required(profile_id):
    assert code_of(lambda: build(profile_id=profile_id)) is PlanErrorCode.INVALID_PROFILE_ID


@pytest.mark.parametrize("bad", [None, 0, -1.0, True, "10", float("nan"), float("inf"), 50.001, 0.0005])
def test_invalid_max_chunk_parameter_is_rejected_by_the_builder(bad):
    assert code_of(lambda: build(max_chunk_ml=bad)) is PlanErrorCode.INVALID_CHUNK_PARAMETER


# --- Unveraenderlichkeit --------------------------------------------------------------------------------------


def test_plan_is_frozen_and_uses_tuples():
    plan = build()
    for target in (plan, plan.pump_targets[0], plan.volume):
        with pytest.raises(dataclasses.FrozenInstanceError):
            target.total_ul = 1  # type: ignore[misc]
    assert isinstance(plan.pump_targets, tuple) and isinstance(plan.pump_targets[0].chunks_ul, tuple)
    with pytest.raises(AttributeError):
        plan.pump_targets.append(None)  # type: ignore[attr-defined]


def test_later_changes_to_inputs_do_not_change_the_plan():
    weights = dict(INDIVIDUAL_WEIGHTS)
    roles = dict(PUMP_ROLES)
    snapshot_dict = make_snapshot().to_dict()
    plan = build(snapshot_dict, pump_weights=weights, pump_roles=roles)
    before = plan.to_dict()

    weights["P1"] = 99.0
    roles["P1"] = "unassigned"
    snapshot_dict["nutrient_a_percent"] = 1.0
    snapshot_dict["target_ro_water_l"] = 1.0

    assert plan.to_dict() == before
    assert plan.verify_hash()
    assert {t.pump: t.weight_ml_per_step for t in plan.pump_targets} == INDIVIDUAL_WEIGHTS


def test_to_dict_returns_fresh_copies():
    plan = build()
    data = plan.to_dict()
    data["pump_targets"][0]["chunks_ul"].append(1)
    data["total_ul"] = 1
    assert plan.to_dict()["total_ul"] == 500_000
    assert sum(plan.pump_targets[0].chunks_ul) == 125_000


def test_plan_hash_is_deterministic_and_sensitive():
    snapshot = make_snapshot()
    assert build(snapshot).plan_hash == build(snapshot).plan_hash
    assert build(snapshot).plan_hash != build(snapshot, pump_weights=dict(INDIVIDUAL_WEIGHTS)).plan_hash
    assert build(snapshot).plan_hash != build(snapshot, max_chunk_ml=5.0).plan_hash
    assert build(snapshot).plan_hash != build(snapshot, profile_id="other").plan_hash
    tampered = dataclasses.replace(build(snapshot), total_ul=1)
    assert tampered.verify_hash() is False


def test_plan_carries_the_snapshot_reference_and_limits():
    snapshot = make_snapshot()
    plan = build(snapshot)
    assert plan.recipe_id == snapshot.recipe_id == "slot-1"
    assert plan.recipe_name == "Testrezept"
    assert plan.snapshot_created_at == snapshot.created_at
    assert plan.snapshot_schema_version == snapshot.schema_version
    assert plan.max_chunk_ml == 10.0 and plan.profile_id == "test_profile"
    assert plan.has_dosing_work is True
    assert (plan.total_ml, plan.a_ml, plan.b_ml) == (500.0, 250.0, 250.0)


# --- Reinheit ---------------------------------------------------------------------------------------------------


def test_module_has_no_io_hardware_time_or_thread_imports():
    forbidden = {
        "os", "sys", "socket", "serial", "threading", "time", "datetime", "pathlib", "subprocess",
        "random", "gpiozero", "paho", "services", "hardware", "nicegui_dashboard", "process", "statemachine",
    }
    source = Path(np_mod.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not (imported & forbidden), imported & forbidden


# --- Review-Korrektur B2: kanonische Pumpenrollen sind unveraenderlich ---------------------------------------

CANONICAL_ROLES = {"P1": "nutrient_a_1", "P2": "nutrient_b_1", "P3": "nutrient_a_2", "P4": "nutrient_b_2"}
TAMPERED_ROLES = {**CANONICAL_ROLES, "P1": "ph_acid"}


def test_public_role_and_pair_constants_cannot_be_modified():
    with pytest.raises(TypeError):
        PUMP_ROLES["P1"] = "ph_acid"  # type: ignore[index]
    with pytest.raises(TypeError):
        del PUMP_ROLES["P2"]  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        np_mod.SOLUTION_PAIRS["A"] = ("P1", "P2")  # type: ignore[index]
    with pytest.raises(AttributeError):
        PUMP_ROLES.update({"P1": "ph_acid"})  # type: ignore[attr-defined]
    assert isinstance(np_mod.PUMP_ORDER, tuple) and isinstance(np_mod.SOLUTION_PAIRS["A"], tuple)
    assert dict(PUMP_ROLES) == CANONICAL_ROLES
    assert dict(np_mod.SOLUTION_PAIRS) == {"A": ("P1", "P3"), "B": ("P2", "P4")}


def test_wrong_external_mapping_is_rejected_after_modification_attempts():
    with pytest.raises(TypeError):
        PUMP_ROLES["P1"] = "ph_acid"  # type: ignore[index]
    assert code_of(lambda: build(pump_roles=dict(TAMPERED_ROLES))) is PlanErrorCode.INVALID_PUMP_ROLES
    plan = build(pump_roles=dict(CANONICAL_ROLES))
    assert {t.pump: t.role for t in plan.pump_targets} == CANONICAL_ROLES


def test_rebinding_the_public_module_names_does_not_change_the_accepted_assignment(monkeypatch):
    monkeypatch.setattr(np_mod, "PUMP_ROLES", dict(TAMPERED_ROLES))
    monkeypatch.setattr(np_mod, "SOLUTION_PAIRS", {"A": ("P1", "P2"), "B": ("P3", "P4")})
    # die manipulierte Belegung wird NICHT zur Sollbelegung ...
    assert code_of(lambda: build(pump_roles=dict(TAMPERED_ROLES))) is PlanErrorCode.INVALID_PUMP_ROLES
    # ... und die kanonische bleibt gueltig, Zuordnung und Paare unveraendert
    plan = build(pump_roles=dict(CANONICAL_ROLES))
    assert {t.pump: (t.role, t.solution) for t in plan.pump_targets} == {
        "P1": ("nutrient_a_1", "A"),
        "P2": ("nutrient_b_1", "B"),
        "P3": ("nutrient_a_2", "A"),
        "P4": ("nutrient_b_2", "B"),
    }
    assert plan.pump_target("P1").target_ul + plan.pump_target("P3").target_ul == plan.a_ul
    assert plan.pump_target("P2").target_ul + plan.pump_target("P4").target_ul == plan.b_ul


def test_distribution_is_unchanged_by_the_immutability_change():
    plan = build(pump_weights=dict(INDIVIDUAL_WEIGHTS))
    assert [(t.pump, t.target_ul) for t in plan.pump_targets] == [("P1", 130_909), ("P2", 122_971), ("P3", 119_091), ("P4", 127_029)]


# --- Review-Korrektur B3: Begrenzung der Chunk-Allokation ----------------------------------------------------


def test_chunk_budget_is_checked_before_any_allocation():
    # 10**400 uL wuerden ohne Vorabpruefung den Speicher sprengen; hier: sofortiger kontrollierter Fehler
    assert code_of(lambda: plan_chunks_ul(10**400, 10.0)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    assert code_of(lambda: plan_chunks_ul(10**12, 10.0)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    assert code_of(lambda: plan_chunks_ul(185_000_000, 0.001)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED


def test_chunk_budget_boundary_is_exact_and_values_are_never_changed():
    limit = np_mod.MAX_CHUNKS_PER_PUMP
    assert limit == 20_000
    at_limit = plan_chunks_ul(limit * 10_000, 10.0)
    assert len(at_limit) == limit and set(at_limit) == {10_000}
    assert code_of(lambda: plan_chunks_ul(limit * 10_000 + 1, 10.0)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    # kein stilles Vergroessern: gueltige Auftraege behalten ihre Verteilung
    assert plan_chunks_ul(25_000, 10.0) == (8334, 8333, 8333)


def test_chunk_budget_parameter_is_validated_and_configurable_downwards():
    assert plan_chunks_ul(30_000, 10.0, max_chunk_count=3) == (10_000, 10_000, 10_000)
    assert code_of(lambda: plan_chunks_ul(30_000, 10.0, max_chunk_count=2)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    for bad in (0, -1, True, 1.5, "3", None):
        assert code_of(lambda: plan_chunks_ul(30_000, 10.0, max_chunk_count=bad)) is PlanErrorCode.INVALID_CHUNK_PARAMETER  # type: ignore[arg-type]


def test_plan_with_too_small_chunks_is_rejected_not_adjusted():
    snapshot = make_snapshot(nutrients_ml_per_100_l=85_000.0)  # 85 l -> ~21 l je Pumpe
    assert code_of(lambda: build(snapshot, max_chunk_ml=0.5)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    ok = build(snapshot, max_chunk_ml=10.0)
    assert max(max(t.chunks_ul) for t in ok.pump_targets) <= 10_000
    assert_invariants(ok)


def test_rounds_can_be_consumed_lazily_and_stay_equivalent():
    plan = build(make_snapshot(nutrients_ml_per_100_l=85_000.0), max_chunk_ml=10.0)
    iterator = plan.iter_rounds()
    assert iter(iterator) is iterator and not isinstance(iterator, (tuple, list))
    first = next(iterator)
    assert [c.index for c in first] == [1] * len(first) and {c.pump for c in first} == {"P1", "P2", "P3", "P4"}
    assert plan.rounds() == tuple(plan.iter_rounds())
    assert len(plan.rounds()) == plan.round_count
    assert sum(len(r) for r in plan.iter_rounds()) == sum(t.chunk_count for t in plan.pump_targets)


def test_largest_possible_plan_stays_within_the_documented_chunk_budget():
    plan = build(make_snapshot(nutrients_ml_per_100_l=85_000.0), max_chunk_ml=10.0)
    assert sum(t.chunk_count for t in plan.pump_targets) <= 4 * np_mod.MAX_CHUNKS_PER_PUMP


def test_dose_chunk_is_slotted_and_immutable():
    chunk = DoseChunk("P1", 1, 5)
    assert not hasattr(chunk, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.amount_ul = 6  # type: ignore[misc]


# --- Review-Korrektur B4: grosse Ganzzahlen ------------------------------------------------------------------

HUGE = [10**400, -(10**400)]


@pytest.mark.parametrize("huge", HUGE)
@pytest.mark.parametrize(
    "field",
    [
        "target_ro_water_l",
        "max_mixer_liters",
        "target_ec_ms_cm",
        "nutrients_ml_per_100_l",
        "ec_adjustment_factor",
        "nutrient_a_percent",
        "nutrient_b_percent",
        "calculated_total_nutrients_ml",
        "calculated_nutrient_a_ml",
        "calculated_nutrient_b_ml",
        "estimated_process_volume_l",
        "ec_mixing_time_seconds",
    ],
)
def test_huge_integers_in_snapshot_fields_raise_only_nutrient_plan_error(field, huge):
    snapshot = dataclasses.replace(make_snapshot(), **{field: huge})
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.INVALID_TYPES


@pytest.mark.parametrize("huge", HUGE)
def test_huge_integers_in_plan_parameters_are_rejected_controlled(huge):
    assert code_of(lambda: build(pump_weights={**EQUAL_WEIGHTS, "P3": huge})) is PlanErrorCode.INVALID_WEIGHTS
    assert code_of(lambda: build(max_chunk_ml=huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: build(min_chunk_ml=huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: build(prefill_target_l=huge)) is PlanErrorCode.VOLUME_INVALID
    assert code_of(lambda: split_pair_ul(10, huge, 1.0)) is PlanErrorCode.INVALID_TYPES
    assert code_of(lambda: split_pair_ul(10, 1.0, huge)) is PlanErrorCode.INVALID_TYPES
    assert code_of(lambda: plan_chunks_ul(1000, huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER


def test_huge_integer_amounts_in_pure_helpers_do_not_overflow():
    first, second = split_pair_ul(10**400, 1.0, 1.0)
    assert first + second == 10**400
    assert code_of(lambda: plan_chunks_ul(-(10**400), 10.0)) is PlanErrorCode.INVALID_AMOUNT


def test_huge_but_representable_values_are_rejected_by_the_domain_limits_not_by_overflow():
    snapshot = dataclasses.replace(make_snapshot(), target_ro_water_l=10**300)
    assert code_of(lambda: build(snapshot)) is PlanErrorCode.RECIPE_INVALID
    # ein riesiges, aber darstellbares Gewicht ist ein gueltiges relatives Gewicht: exakte Summen, kein Overflow
    plan = build(pump_weights={**EQUAL_WEIGHTS, "P1": 10**300})
    assert plan.pump_target("P3").target_ul == 0 and plan.pump_target("P1").target_ul == plan.a_ul
    assert_invariants(plan)


# --- Review R3, B3.1: max_chunk_count darf das feste Budget nicht erhoehen ---------------------------------------


def test_chunk_count_parameter_cannot_raise_the_technical_budget():
    limit = np_mod.MAX_CHUNKS_PER_PUMP
    # Reproduktion aus dem Review: Anforderung ueber dem Budget wird VOR der Allokation abgelehnt
    assert code_of(lambda: plan_chunks_ul(20_001, 0.001, max_chunk_count=20_001)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: plan_chunks_ul(20_001, 0.001, max_chunk_count=limit + 1)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: plan_chunks_ul(20_001, 0.001)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    assert code_of(lambda: plan_chunks_ul(20_001, 0.001, max_chunk_count=limit)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED
    # genau am Budget ist erlaubt (kleine Liste: 3 Chunks, Budget nur nach unten begrenzt)
    assert plan_chunks_ul(3, 0.001, max_chunk_count=limit) == (1, 1, 1)
    assert plan_chunks_ul(3, 0.001, max_chunk_count=3) == (1, 1, 1)
    assert code_of(lambda: plan_chunks_ul(3, 0.001, max_chunk_count=2)) is PlanErrorCode.CHUNK_BUDGET_EXCEEDED


@pytest.mark.parametrize(
    "bad",
    [0, -1, 20_001, 10**6, True, False, 1.5, 20_000.0, "3", None, 10**5000],
    ids=lambda v: f"{type(v).__name__}-{np_mod.safe_repr(v)[:24]}",
)
@pytest.mark.parametrize("target", [0, 3, 10_000])
def test_invalid_chunk_count_is_rejected_even_for_zero_target(bad, target):
    assert code_of(lambda: plan_chunks_ul(target, 10.0, max_chunk_count=bad)) is PlanErrorCode.INVALID_CHUNK_PARAMETER  # type: ignore[arg-type]


def test_chunk_count_at_both_bounds_is_accepted():
    assert plan_chunks_ul(0, 10.0, max_chunk_count=1) == ()
    assert plan_chunks_ul(0, 10.0, max_chunk_count=np_mod.MAX_CHUNKS_PER_PUMP) == ()
    assert plan_chunks_ul(5_000, 10.0, max_chunk_count=1) == (5_000,)


# --- Review R3, B4.1: extrem grosse Ganzzahlen (> sys.get_int_max_str_digits) -------------------------------------

import sys

HUGE5000 = [10**5000, -(10**5000)]
SNAPSHOT_FIELDS_5000 = [
    "target_ro_water_l", "max_mixer_liters", "target_ec_ms_cm", "nutrients_ml_per_100_l", "ec_adjustment_factor",
    "nutrient_a_percent", "nutrient_b_percent", "calculated_total_nutrients_ml", "calculated_nutrient_a_ml",
    "calculated_nutrient_b_ml", "estimated_process_volume_l", "ec_mixing_time_seconds", "schema_version",
    "nutrient_dosing_enabled", "legacy_dosing_needs_review", "recipe_id", "recipe_name", "created_at",
]


def test_python_int_string_conversion_limit_is_still_active():
    assert sys.get_int_max_str_digits() != 0  # nie global deaktiviert
    with pytest.raises(ValueError):
        str(10**5000)


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
@pytest.mark.parametrize("field", SNAPSHOT_FIELDS_5000)
def test_extremely_large_integers_in_snapshot_fields_raise_only_nutrient_plan_error(field, huge):
    snapshot = dataclasses.replace(make_snapshot(), **{field: huge})
    with pytest.raises(NutrientPlanError) as exc:
        build(snapshot)
    assert exc.value.code is PlanErrorCode.INVALID_TYPES
    assert len(str(exc.value)) < 2000  # keine ausgeschriebene Dezimaldarstellung


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
def test_extremely_large_integers_in_plan_parameters_and_helpers(huge):
    assert code_of(lambda: build(pump_weights={**EQUAL_WEIGHTS, "P3": huge})) is PlanErrorCode.INVALID_WEIGHTS
    assert code_of(lambda: build(pump_weights=huge)) is PlanErrorCode.INVALID_WEIGHTS
    assert code_of(lambda: build(pump_roles={**CANONICAL_ROLES, "P1": huge})) is PlanErrorCode.INVALID_PUMP_ROLES
    assert code_of(lambda: build(pump_roles=huge)) is PlanErrorCode.INVALID_PUMP_ROLES
    assert code_of(lambda: build(profile_id=huge)) is PlanErrorCode.INVALID_PROFILE_ID
    assert code_of(lambda: build(max_chunk_ml=huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: build(min_chunk_ml=huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: build(prefill_target_l=huge)) is PlanErrorCode.VOLUME_INVALID
    assert code_of(lambda: split_pair_ul(10, huge, 1.0)) is PlanErrorCode.INVALID_TYPES
    assert code_of(lambda: split_pair_ul(10, 1.0, huge)) is PlanErrorCode.INVALID_TYPES
    assert code_of(lambda: plan_chunks_ul(1000, huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: plan_chunks_ul(1000, 10.0, huge)) is PlanErrorCode.INVALID_CHUNK_PARAMETER
    assert code_of(lambda: plan_chunks_ul(huge, 10.0)) in (PlanErrorCode.INVALID_AMOUNT, PlanErrorCode.CHUNK_BUDGET_EXCEEDED)


@pytest.mark.parametrize("huge", HUGE5000, ids=["pos5000", "neg5000"])
def test_extremely_large_integers_as_snapshot_container_content(huge):
    data = make_snapshot().to_dict()
    assert code_of(lambda: build_nutrient_dosing_plan({**data, huge: 1}, **params())) is PlanErrorCode.INVALID_SNAPSHOT
    assert code_of(lambda: build_nutrient_dosing_plan(huge, **params())) is PlanErrorCode.INVALID_SNAPSHOT


def test_extremely_large_integer_amount_in_split_does_not_overflow_or_stringify():
    first, second = split_pair_ul(10**5000, 1.0, 1.0)
    assert first + second == 10**5000


def test_schema_version_is_bounded_so_the_plan_hash_never_needs_huge_integers():
    for bad in (0, -1, 5, 10**5000):
        snapshot = dataclasses.replace(make_snapshot(), schema_version=bad)
        assert code_of(lambda: build(snapshot)) is PlanErrorCode.INVALID_TYPES


def test_error_messages_never_contain_a_full_decimal_expansion():
    with pytest.raises(NutrientPlanError) as exc:
        build(dataclasses.replace(make_snapshot(), target_ro_water_l=10**5000))
    assert "Bit" in str(exc.value) and len(str(exc.value)) < 2000
