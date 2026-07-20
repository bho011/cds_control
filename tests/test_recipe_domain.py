"""
Domain/recipe_limits.py + domain/recipe_model.py: pure calculation +
validation rules for tank volume and EC nutrient dosing. No GPIO/MQTT/OPC-UA,
no I/O.
"""

from __future__ import annotations

import pytest

from domain.recipe_limits import (
    EC_ADJUSTMENT_FACTOR_ENABLED_MIN,
    EC_MIXING_TIME_MAX_SECONDS,
    EC_MIXING_TIME_MIN_SECONDS,
    EC_SETPOINT_MAX_MS_CM,
    EC_SETPOINT_MIN_MS_CM,
    LEGACY_MAX_RO_CORRECTION_L,
    MAX_PROCESS_VOLUME_L,
    MAX_RECIPE_RO_WATER_L,
    compute_base_volume_before_ro_correction_l,
    compute_max_possible_ro_correction_l,
    compute_nutrient_split,
    compute_total_nutrients_ml,
    nutrient_b_percent,
    validate_bool_field,
    validate_numeric_field,
    validate_recipe_values,
    validate_strict_int_field,
)
from domain.recipe_model import RecipePreview, RecipeValidationError, RunConfigSnapshot, RunOptions, StoredRecipe


def _validate(**overrides) -> list[str]:
    values = dict(
        target_ro_water_l=50.0,
        target_ec_ms_cm=1.5,
        ec_mixing_time_seconds=600,
        nutrients_ml_per_100_l=0.0,
        nutrient_a_percent=50.0,
        ec_adjustment_factor=1.0,
        nutrient_dosing_enabled=True,
        legacy_dosing_needs_review=False,
    )
    values.update(overrides)
    return validate_recipe_values(**values)


# --- RO water / total process volume limits ---------------------------------------


def test_ro_water_180l_is_allowed_when_total_stays_under_185():
    assert _validate(target_ro_water_l=180.0) == []


def test_ro_water_180_01l_is_rejected():
    errors = _validate(target_ro_water_l=180.01)
    assert any("180" in message for message in errors)


def test_ro_water_200l_is_rejected():
    assert _validate(target_ro_water_l=200.0) != []


def test_ro_water_must_be_positive():
    assert _validate(target_ro_water_l=0.0) != []


def test_physical_tank_capacity_is_not_a_usable_recipe_limit():
    # 200 l is only ever the physical rim capacity - both recipe-level limits
    # must be strictly below it.
    assert MAX_RECIPE_RO_WATER_L < 200.0
    assert MAX_PROCESS_VOLUME_L < 200.0


def test_estimated_recipe_volume_of_exactly_185l_is_allowed():
    # 100 l RO water + 85 l of nutrients (85000 ml) = 185.0 l exactly. A
    # fixed RO correction is not part of a recipe anymore (see Section 4),
    # so the only way to reach the limit is via RO water + nutrients.
    total_nutrients_ml = compute_total_nutrients_ml(
        target_ro_water_l=100.0, nutrients_ml_per_100_l=85000.0, ec_adjustment_factor=1.0
    )
    assert compute_base_volume_before_ro_correction_l(100.0, total_nutrients_ml) == 185.0

    errors = _validate(
        target_ro_water_l=100.0,
        nutrients_ml_per_100_l=85000.0,
        ec_adjustment_factor=1.0,
    )
    assert errors == []


def test_recipe_volume_over_185l_is_rejected():
    # A large nutrient dose alone (well within the 0-no-max
    # nutrients_ml_per_100_l business rule) pushes the recipe's own estimated
    # volume over 185 l without any RO correction being involved at all.
    errors = _validate(
        target_ro_water_l=180.0,
        nutrients_ml_per_100_l=3000.0,
        ec_adjustment_factor=1.0,
    )
    assert any("Rezeptvolumen" in message and "185" in message for message in errors)


# --- No fixed RO correction anywhere in a recipe (item 4 removal) ------------------


def test_stored_recipe_has_no_requested_ro_correction_field():
    recipe = StoredRecipe()
    assert not hasattr(recipe, "requested_ro_correction_l")


def test_run_config_snapshot_has_no_ro_correction_fields():
    snapshot_fields = {f for f in RunConfigSnapshot.__dataclass_fields__}
    assert "requested_ro_correction_l" not in snapshot_fields
    assert "allowed_ro_correction_l" not in snapshot_fields


def test_stored_recipe_has_no_run_option_or_technical_process_fields():
    """sensor_circulation_enabled/drain_after_process moved to RunOptions
    (per-run only, never persisted). sensor_pump_seconds is a technical/
    ProcessSettings-shaped value, not a recipe value at all - it has no
    execution-side consumer in the recipe/Fill-and-Measure path and must be
    sourced from ProcessSettings if it is ever needed, never from a
    recipe."""
    recipe_fields = {f for f in StoredRecipe.__dataclass_fields__}
    for removed_field in (
        "sensor_circulation_enabled",
        "drain_after_process",
        "sensor_pump_seconds",
        "requested_ro_correction_l",
    ):
        assert removed_field not in recipe_fields


def test_max_possible_ro_correction_is_purely_informational():
    # Pure domain function, unrelated to any stored/requested amount -
    # matches the new preview formula exactly:
    # base_volume = RO water + nutrients
    # remaining_capacity = max(0, 185 - base_volume)
    # max_available_ro_correction = min(50, remaining_capacity)
    assert compute_max_possible_ro_correction_l(100.0) == 50.0
    assert compute_max_possible_ro_correction_l(182.0) == 3.0
    assert compute_max_possible_ro_correction_l(185.0) == 0.0
    assert compute_max_possible_ro_correction_l(170.0) == 15.0


def test_base_volume_formula_matches_ro_water_plus_nutrients():
    total_nutrients_ml = compute_total_nutrients_ml(
        target_ro_water_l=100.0, nutrients_ml_per_100_l=200.0, ec_adjustment_factor=1.0
    )
    base_volume = compute_base_volume_before_ro_correction_l(
        target_ro_water_l=100.0, total_nutrients_ml=total_nutrients_ml
    )
    assert base_volume == 100.0 + 200.0 / 1000.0


# --- Nutrient A/B split -------------------------------------------------------------


def test_default_50_50_split_is_computed_correctly():
    total = compute_total_nutrients_ml(target_ro_water_l=100.0, nutrients_ml_per_100_l=200.0, ec_adjustment_factor=1.0)
    a_ml, b_ml = compute_nutrient_split(total, nutrient_a_percent=50.0)
    assert a_ml == b_ml == 100.0


def test_40_60_split_is_computed_correctly():
    assert nutrient_b_percent(40.0) == 60.0


def test_1800ml_total_at_40_percent_a_splits_to_720_and_1080():
    a_ml, b_ml = compute_nutrient_split(1800.0, nutrient_a_percent=40.0)
    assert a_ml == 720.0
    assert b_ml == 1080.0


def test_a_zero_percent_gives_b_100_percent():
    assert nutrient_b_percent(0.0) == 100.0


def test_a_100_percent_gives_b_zero_percent():
    assert nutrient_b_percent(100.0) == 0.0


def test_a_below_zero_is_rejected():
    assert _validate(nutrient_a_percent=-0.01) != []


def test_a_above_100_is_rejected():
    assert _validate(nutrient_a_percent=100.01) != []


def test_a_plus_b_is_always_100_by_construction():
    for a in (0.0, 12.5, 40.0, 50.0, 73.3, 100.0):
        assert round(a + nutrient_b_percent(a), 6) == 100.0


def test_split_sums_back_to_the_rounded_total_even_after_rounding():
    total = compute_total_nutrients_ml(target_ro_water_l=33.0, nutrients_ml_per_100_l=17.0, ec_adjustment_factor=0.7)
    a_ml, b_ml = compute_nutrient_split(total, nutrient_a_percent=37.0)
    assert round(a_ml + b_ml, 3) == total


def test_negative_total_dose_is_rejected():
    assert _validate(nutrients_ml_per_100_l=-1.0) != []


# --- Disabled dosing forces everything to 0 ml (item 2) ----------------------------


def test_disabled_dosing_forces_calculated_amounts_to_zero_in_preview():
    recipe = StoredRecipe(
        target_ro_water_l=100.0,
        nutrient_dosing_enabled=False,
        nutrients_ml_per_100_l=500.0,
        nutrient_a_percent=40.0,
    )
    preview = RecipePreview.from_recipe(recipe)
    assert preview.total_nutrients_ml == 0.0
    assert preview.nutrient_a_ml == 0.0
    assert preview.nutrient_b_ml == 0.0
    # the recipe's own stored dose value stays visible/unchanged
    assert recipe.nutrients_ml_per_100_l == 500.0


def test_disabled_dosing_does_not_count_nutrients_towards_estimated_volume():
    recipe = StoredRecipe(target_ro_water_l=100.0, nutrient_dosing_enabled=False, nutrients_ml_per_100_l=5000.0)
    preview = RecipePreview.from_recipe(recipe)
    assert preview.estimated_process_volume_l == 100.0


def test_disabled_dosing_still_forbids_invalid_types_and_negative_values():
    assert _validate(nutrient_dosing_enabled=False, nutrients_ml_per_100_l=-1.0) != []


# --- Review finding: disabled dosing must not affect validation's own
# estimated-volume check either (validate_recipe_values recomputes
# total_nutrients_ml independently of RecipePreview/RunConfigSnapshot - both
# must force it to 0.0 identically while dosing is disabled) --------------


def test_ro_180l_disabled_with_a_huge_stored_dose_stays_valid_and_is_ignored_in_the_volume_check():
    # 180 l RO water is already at the recipe's own max; a dose this large
    # (100000 ml/100l) would push the estimated volume far over 185 l if it
    # were counted - but with dosing disabled nothing will actually be
    # dosed, so it must not be counted at all.
    errors = _validate(
        target_ro_water_l=180.0,
        nutrient_dosing_enabled=False,
        nutrients_ml_per_100_l=100000.0,
    )
    assert errors == []


def test_ec_adjustment_factor_negative_is_rejected_even_when_dosing_disabled():
    errors = _validate(nutrient_dosing_enabled=False, ec_adjustment_factor=-1.0)
    assert errors != []
    assert any("EC-Anpassungsfaktor" in message for message in errors)


def test_ec_adjustment_factor_100_is_rejected_even_when_dosing_disabled():
    errors = _validate(nutrient_dosing_enabled=False, ec_adjustment_factor=100.0)
    assert errors != []
    assert any("EC-Anpassungsfaktor" in message for message in errors)


# --- EC setpoint / mixing time (confirmed via legacy Node-RED) --------------------


@pytest.mark.parametrize(
    "value,expect_valid",
    [
        (EC_SETPOINT_MIN_MS_CM - 0.01, False),
        (EC_SETPOINT_MIN_MS_CM, True),
        (1.5, True),
        (2.35, True),
        (EC_SETPOINT_MAX_MS_CM, True),
        (EC_SETPOINT_MAX_MS_CM + 0.01, False),
    ],
)
def test_ec_setpoint_bounds(value, expect_valid):
    errors = _validate(target_ec_ms_cm=value)
    assert (errors == []) is expect_valid


def test_ec_setpoint_2_35_is_not_rounded_or_quantized():
    # The GUI stepper (step=0.1) is a UI convenience only - the domain layer
    # must accept and preserve an arbitrary in-range value unchanged.
    recipe = StoredRecipe(
        target_ro_water_l=50.0,
        target_ec_ms_cm=2.35,
        ec_mixing_time_seconds=600,
        nutrient_dosing_enabled=False,
    )
    assert recipe.validate() == []
    assert recipe.target_ec_ms_cm == 2.35
    round_tripped = StoredRecipe.from_dict(recipe.to_dict())
    assert round_tripped.target_ec_ms_cm == 2.35


@pytest.mark.parametrize(
    "value,expect_valid",
    [
        (EC_MIXING_TIME_MIN_SECONDS - 1, False),
        (EC_MIXING_TIME_MIN_SECONDS, True),
        (600, True),
        (EC_MIXING_TIME_MAX_SECONDS, True),
        (EC_MIXING_TIME_MAX_SECONDS + 1, False),
    ],
)
def test_ec_mixing_time_bounds(value, expect_valid):
    errors = _validate(ec_mixing_time_seconds=value)
    assert (errors == []) is expect_valid


# --- Addon 1/Addon 2 fully removed from the active recipe model ------------------


def test_stored_recipe_has_no_addon_fields():
    recipe_fields = {f for f in StoredRecipe.__dataclass_fields__}
    assert "addon_1_ml" not in recipe_fields
    assert "addon_2_ml" not in recipe_fields


def test_recipe_preview_has_no_addon_fields():
    preview_fields = {f for f in RecipePreview.__dataclass_fields__}
    assert "addon_1_ml" not in preview_fields
    assert "addon_2_ml" not in preview_fields


def test_run_config_snapshot_has_no_addon_fields():
    snapshot_fields = {f for f in RunConfigSnapshot.__dataclass_fields__}
    assert "addon_1_ml" not in snapshot_fields
    assert "addon_2_ml" not in snapshot_fields


def test_stored_recipe_has_legacy_recipe_values_field():
    recipe = StoredRecipe()
    assert hasattr(recipe, "legacy_recipe_values")
    assert recipe.legacy_recipe_values is None


def test_legacy_recipe_values_must_be_a_dict_or_none():
    recipe = StoredRecipe(target_ro_water_l=50.0, nutrient_dosing_enabled=False)
    recipe.legacy_recipe_values = "not a dict"  # type: ignore[assignment]
    errors = recipe.validate()
    assert errors
    assert any("legacy_recipe_values" in message for message in errors)


def test_legacy_recipe_values_dict_is_accepted():
    assert (
        StoredRecipe(
            target_ro_water_l=50.0,
            target_ec_ms_cm=1.5,
            ec_mixing_time_seconds=600,
            nutrient_dosing_enabled=False,
            legacy_recipe_values={"addon_1_ml": 50.0, "addon_2_ml": 50.0},
        ).validate()
        == []
    )


# --- EC adjustment factor: range depends on nutrient_dosing_enabled ---------------


def test_ec_adjustment_factor_below_0_01_is_rejected_when_dosing_enabled():
    assert _validate(ec_adjustment_factor=0.005, nutrient_dosing_enabled=True) != []


def test_ec_adjustment_factor_at_0_01_is_allowed_when_dosing_enabled():
    assert _validate(ec_adjustment_factor=EC_ADJUSTMENT_FACTOR_ENABLED_MIN, nutrient_dosing_enabled=True) == []


def test_ec_adjustment_factor_of_exactly_1_is_allowed_when_dosing_enabled():
    assert _validate(ec_adjustment_factor=1.0, nutrient_dosing_enabled=True) == []


def test_ec_adjustment_factor_above_1_is_rejected_when_dosing_enabled():
    assert _validate(ec_adjustment_factor=1.01, nutrient_dosing_enabled=True) != []


def test_ec_adjustment_factor_enabled_only_minimum_is_skipped_when_dosing_disabled():
    # The stricter 0.01 minimum only applies while dosing is enabled - 0.0
    # is still within the unconditional [0.0, 1.0] bound, so it stays valid
    # while disabled (see the dedicated negative/100 rejection tests above,
    # which prove the unconditional bound itself is NOT skipped).
    assert _validate(ec_adjustment_factor=0.0, nutrient_dosing_enabled=False) == []


# --- max_possible_ro_correction_l (informational preview only) -------------------


def test_estimated_volume_182l_allows_at_most_3l_of_technically_possible_correction():
    assert compute_max_possible_ro_correction_l(182.0) == 3.0


def test_estimated_volume_185l_allows_zero_technically_possible_correction():
    assert compute_max_possible_ro_correction_l(185.0) == 0.0


def test_static_50l_cap_applies_when_plenty_of_capacity_remains():
    assert compute_max_possible_ro_correction_l(100.0) == LEGACY_MAX_RO_CORRECTION_L


# --- Legacy dosing gate -------------------------------------------------------------


def test_legacy_needs_review_blocks_nutrient_dosing_enabled():
    errors = _validate(nutrient_dosing_enabled=True, legacy_dosing_needs_review=True)
    assert any("Legacy" in message for message in errors)


def test_legacy_needs_review_allows_dosing_disabled():
    assert _validate(nutrient_dosing_enabled=False, legacy_dosing_needs_review=True) == []


# --- Strict boolean validation: never bool(value) to normalize ---------------------


@pytest.mark.parametrize("value", [True, False])
def test_validate_bool_field_accepts_genuine_booleans(value):
    assert validate_bool_field("nutrient_dosing_enabled", value) is None


@pytest.mark.parametrize(
    "value",
    ["false", "true", "False", 0, 1, 1.0, 0.0, None, [], {}, [True], {"x": True}],
)
def test_validate_bool_field_rejects_everything_else(value):
    message = validate_bool_field("nutrient_dosing_enabled", value)
    assert message is not None
    assert "nutrient_dosing_enabled" in message


def test_string_false_does_not_enable_nutrient_dosing():
    """The literal acceptance test from the task: a string "false" must not
    activate anything - type(value) is bool rejects it outright rather than
    falling back to Python truthiness (bool("false") is True!)."""
    recipe = StoredRecipe(target_ro_water_l=50.0, nutrient_dosing_enabled="false")  # type: ignore[arg-type]
    errors = recipe.validate()
    assert errors, "a string 'false' must be rejected, not silently truthy-coerced"
    assert any("nutrient_dosing_enabled" in message for message in errors)


def test_run_options_reject_string_false_too():
    run_options = RunOptions(sensor_circulation_enabled="false")  # type: ignore[arg-type]
    errors = run_options.validate()
    assert errors
    assert any("sensor_circulation_enabled" in message for message in errors)


def test_run_options_default_to_everything_off():
    run_options = RunOptions()
    assert run_options.sensor_circulation_enabled is False
    assert run_options.drain_after_process is False
    assert run_options.validate() == []


# --- Non-finite / wrong-type / non-integral numeric fields -------------------------


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "50", None, True, False, [], {}],
)
def test_validate_numeric_field_rejects_non_finite_and_wrong_types(value):
    message = validate_numeric_field("target_ro_water_l", value, float)
    assert message is not None
    assert "target_ro_water_l" in message


@pytest.mark.parametrize("value", [0.0, 1, 50.0, -5.0])
def test_validate_numeric_field_accepts_genuine_finite_numbers(value):
    assert validate_numeric_field("target_ro_water_l", value, float) is None


def test_validate_numeric_field_rejects_non_integral_float_for_int_fields():
    message = validate_numeric_field("ec_mixing_time_seconds", 300.5, int)
    assert message is not None
    assert "Ganzzahl" in message


def test_validate_numeric_field_accepts_whole_number_float_for_int_fields():
    # JSON does not distinguish 300 from 300.0 - a whole-number float must
    # still be accepted for an int-typed field.
    assert validate_numeric_field("ec_mixing_time_seconds", 300.0, int) is None


@pytest.mark.parametrize(
    "field_name,bad_value",
    [
        ("target_ro_water_l", float("nan")),
        ("target_ro_water_l", float("inf")),
        ("nutrients_ml_per_100_l", float("-inf")),
        ("nutrient_a_percent", "fifty"),
        ("ec_adjustment_factor", None),
        ("volume_acid_1_ml", True),
        ("ec_mixing_time_seconds", 300.5),
    ],
)
def test_stored_recipe_validate_rejects_bad_numeric_values_without_a_typeerror(field_name, bad_value):
    recipe = StoredRecipe(**{field_name: bad_value})
    errors = recipe.validate()
    assert errors, f"expected a validation error for {field_name}={bad_value!r}"
    assert any(field_name in message for message in errors)


def test_bad_numeric_type_surfaces_as_recipe_validation_error_not_typeerror():
    recipe = StoredRecipe(target_ro_water_l="not a number")
    with pytest.raises(RecipeValidationError):
        RunConfigSnapshot.build(recipe, {"max_mixer_liters": 185.0})


# --- Strict integer contract: schema_version/slot get zero float tolerance,
# only ec_mixing_time_seconds/ph_mixing_time_seconds may be coerced --------------


@pytest.mark.parametrize("value", [1, 3, 4])
def test_validate_strict_int_field_accepts_genuine_ints(value):
    assert validate_strict_int_field("slot", value) is None


@pytest.mark.parametrize("value", [1.0, 3.0, True, False, "1", None, float("nan")])
def test_validate_strict_int_field_rejects_everything_else(value):
    message = validate_strict_int_field("slot", value)
    assert message is not None
    assert "slot" in message


def test_stored_recipe_schema_version_as_whole_number_float_is_rejected_not_normalized():
    recipe = StoredRecipe(
        target_ro_water_l=50.0, nutrient_dosing_enabled=False, schema_version=1.0  # type: ignore[arg-type]
    )
    errors = recipe.validate()
    assert errors
    assert any("schema_version" in message for message in errors)
    # deliberately NOT normalized to int - the real offending value is
    # preserved for the error message, no silent __post_init__ coercion.
    assert recipe.schema_version == 1.0


def test_stored_recipe_slot_as_whole_number_float_is_rejected_not_normalized():
    recipe = StoredRecipe(
        target_ro_water_l=50.0, nutrient_dosing_enabled=False, slot=3.0  # type: ignore[arg-type]
    )
    errors = recipe.validate()
    assert errors
    assert any("slot" in message for message in errors)
    assert recipe.slot == 3.0


def test_stored_recipe_slot_true_is_rejected():
    recipe = StoredRecipe(
        target_ro_water_l=50.0, nutrient_dosing_enabled=False, slot=True  # type: ignore[arg-type]
    )
    errors = recipe.validate()
    assert errors
    assert any("slot" in message for message in errors)


def test_stored_recipe_schema_version_as_string_is_rejected():
    recipe = StoredRecipe(
        target_ro_water_l=50.0, nutrient_dosing_enabled=False, schema_version="1"  # type: ignore[arg-type]
    )
    errors = recipe.validate()
    assert errors
    assert any("schema_version" in message for message in errors)


def test_ec_mixing_time_whole_number_float_is_coerced_to_real_int():
    recipe = StoredRecipe(ec_mixing_time_seconds=300.0)
    assert recipe.ec_mixing_time_seconds == 300
    assert type(recipe.ec_mixing_time_seconds) is int


def test_ph_mixing_time_whole_number_float_is_coerced_to_real_int():
    recipe = StoredRecipe(ph_mixing_time_seconds=600.0)
    assert recipe.ph_mixing_time_seconds == 600
    assert type(recipe.ph_mixing_time_seconds) is int


def test_ec_mixing_time_non_integral_float_is_not_coerced_and_fails_validation():
    recipe = StoredRecipe(
        target_ro_water_l=50.0, nutrient_dosing_enabled=False, ec_mixing_time_seconds=300.5
    )
    assert recipe.ec_mixing_time_seconds == 300.5
    errors = recipe.validate()
    assert errors
    assert any("ec_mixing_time_seconds" in message for message in errors)


def test_run_config_snapshot_ec_mixing_time_is_a_real_int_for_valid_float_input():
    recipe = StoredRecipe(
        target_ro_water_l=50.0,
        target_ec_ms_cm=1.5,
        ec_mixing_time_seconds=300.0,
        nutrient_dosing_enabled=False,
    )
    snapshot = RunConfigSnapshot.build(recipe, {"max_mixer_liters": 185.0})
    assert type(snapshot.ec_mixing_time_seconds) is int
    assert snapshot.ec_mixing_time_seconds == 300
