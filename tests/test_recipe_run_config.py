"""
nicegui_dashboard/recipe_store.py::build_run_config() and
domain/recipe_model.py::RunConfigSnapshot - the integration between a stored
recipe, process_settings.json, a RunOptions instance, and the immutable
snapshot actually handed to a Fill-and-Measure run. Pure logic (a dict
merge/validation), no GPIO/OPC-UA/I-O.
"""

from __future__ import annotations

import dataclasses

import pytest

from domain.recipe_limits import RecipeValidationError
from domain.recipe_model import RunConfigSnapshot, RunOptions, StoredRecipe
from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA
from nicegui_dashboard.recipe_store import DEFAULT_RECIPE, build_run_config, get_active_recipe, load_recipe_book
from services.settings_validation import validate_settings

BASE_SETTINGS = {
    "fill_mode": "delta",
    "target_add_liters": 1.0,
    "target_total_liters": 1.0,
    "max_mixer_liters": 185.0,
}


def test_happy_path_overrides_only_the_connected_fields():
    recipe = dict(DEFAULT_RECIPE, target_ro_water_l=75.0)
    run_options = RunOptions(sensor_circulation_enabled=False)

    run_config = build_run_config(BASE_SETTINGS, recipe, run_options)

    assert run_config["fill_mode"] == "absolute"
    assert run_config["target_total_liters"] == 75.0
    assert run_config["enable_sensor_circulation"] is False
    # untouched passthrough keys
    assert run_config["max_mixer_liters"] == 185.0
    assert run_config["target_add_liters"] == 1.0
    # the validated EC/nutrient snapshot is attached for display/audit, even
    # though it has no hardware consumer yet
    assert run_config["recipe_run_config_snapshot"]["recipe_name"] == recipe["recipe_name"]
    # enable_sensor_circulation must be sourced exclusively from the
    # immutable snapshot (built from RunOptions, never the recipe) - both
    # views of the same value must always agree.
    assert (
        run_config["enable_sensor_circulation"]
        == run_config["recipe_run_config_snapshot"]["sensor_circulation_enabled"]
    )


def test_sensor_circulation_comes_from_run_options_not_the_recipe():
    # The recipe itself no longer has a sensor_circulation_enabled field at
    # all - only run_options controls it.
    recipe = dict(DEFAULT_RECIPE, target_ro_water_l=10.0)
    assert "sensor_circulation_enabled" not in recipe

    run_config_off = build_run_config(BASE_SETTINGS, recipe, RunOptions(sensor_circulation_enabled=False))
    run_config_on = build_run_config(BASE_SETTINGS, recipe, RunOptions(sensor_circulation_enabled=True))

    assert run_config_off["enable_sensor_circulation"] is False
    assert run_config_on["enable_sensor_circulation"] is True


def test_omitting_run_options_defaults_to_everything_off():
    # Fail-closed default: a caller that forgets to pass run_options must
    # never accidentally activate sensor circulation.
    recipe = dict(DEFAULT_RECIPE, target_ro_water_l=10.0)
    run_config = build_run_config(BASE_SETTINGS, recipe)
    assert run_config["enable_sensor_circulation"] is False


def test_original_settings_dict_is_not_mutated():
    original = dict(BASE_SETTINGS)
    build_run_config(BASE_SETTINGS, dict(DEFAULT_RECIPE, target_ro_water_l=10.0))
    assert BASE_SETTINGS == original


def test_ro_water_above_180_is_rejected_before_reaching_max_mixer_liters():
    recipe = dict(DEFAULT_RECIPE, target_ro_water_l=190.0)
    with pytest.raises(RecipeValidationError, match="180"):
        build_run_config(BASE_SETTINGS, recipe)


def test_target_exceeding_max_mixer_liters_is_still_rejected_as_a_secondary_check():
    # 75 l is a perfectly valid recipe (well under the 180 l recipe limit),
    # but process_settings.json's own max_mixer_liters is a separate,
    # independent physical-capacity guard that must still apply.
    recipe = dict(DEFAULT_RECIPE, target_ro_water_l=75.0)
    tight_settings = dict(BASE_SETTINGS, max_mixer_liters=60.0)

    with pytest.raises(ValueError, match="max_mixer_liters"):
        build_run_config(tight_settings, recipe)


def test_missing_target_ro_water_l_is_rejected_via_validation_not_a_raw_keyerror():
    recipe = dict(DEFAULT_RECIPE)
    del recipe["target_ro_water_l"]

    # StoredRecipe.from_dict() defaults the missing field to 0.0, which then
    # fails validation with a clear message instead of a raw KeyError.
    with pytest.raises(RecipeValidationError):
        build_run_config(BASE_SETTINGS, recipe)


def test_start_is_blocked_for_an_invalid_active_recipe():
    invalid_recipe = dict(DEFAULT_RECIPE, target_ro_water_l=0.0)
    with pytest.raises(RecipeValidationError):
        build_run_config(BASE_SETTINGS, invalid_recipe)


def test_real_active_recipe_merged_onto_real_settings_passes_schema():
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent

    with (repo_root / "config/process_settings.json").open(encoding="utf-8") as file:
        real_settings = json.load(file)

    # Goes through the real loader (not a raw json.load) so the schema
    # migration chain is applied exactly as it would be for the live
    # dashboard.
    real_recipe = get_active_recipe(load_recipe_book())

    run_config = build_run_config(real_settings, real_recipe)

    validate_settings(run_config, PROCESS_SETTINGS_SCHEMA, "RunConfig (real files)")  # raises on failure


# --- RunOptions are never persisted / never come from the recipe -----------------


def test_recipe_dict_has_no_run_option_fields():
    for key in ("sensor_circulation_enabled", "drain_after_process", "sensor_pump_seconds"):
        assert key not in DEFAULT_RECIPE


def test_loading_a_recipe_does_not_activate_drain():
    recipe = dict(DEFAULT_RECIPE, target_ro_water_l=10.0)
    run_config = build_run_config(BASE_SETTINGS, recipe)
    snapshot = run_config["recipe_run_config_snapshot"]
    assert snapshot["drain_after_process"] is False


# --- RunConfigSnapshot: immutability + fidelity ------------------------------------


def _valid_stored_recipe(**overrides) -> StoredRecipe:
    return StoredRecipe.from_dict(dict(DEFAULT_RECIPE, target_ro_water_l=120.0, **overrides))


def test_run_config_snapshot_is_immutable():
    snapshot = RunConfigSnapshot.build(_valid_stored_recipe(), BASE_SETTINGS)

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.target_ro_water_l = 1.0  # type: ignore[misc]


def test_run_config_snapshot_matches_the_recipe_in_every_business_value():
    recipe = _valid_stored_recipe(
        nutrient_dosing_enabled=True,
        nutrients_ml_per_100_l=40.0,
        nutrient_a_percent=40.0,
        ec_adjustment_factor=0.8,
    )
    run_options = RunOptions(sensor_circulation_enabled=True)
    snapshot = RunConfigSnapshot.build(recipe, BASE_SETTINGS, run_options)

    assert snapshot.target_ro_water_l == recipe.target_ro_water_l
    assert snapshot.target_ec_ms_cm == recipe.target_ec_ms_cm
    assert snapshot.ec_mixing_time_seconds == recipe.ec_mixing_time_seconds
    assert snapshot.nutrient_dosing_enabled == recipe.nutrient_dosing_enabled
    assert snapshot.legacy_dosing_needs_review == recipe.legacy_dosing_needs_review
    assert snapshot.nutrients_ml_per_100_l == recipe.nutrients_ml_per_100_l
    assert snapshot.ec_adjustment_factor == recipe.ec_adjustment_factor
    assert snapshot.nutrient_a_percent == recipe.nutrient_a_percent
    assert snapshot.nutrient_b_percent == 60.0
    # sourced from run_options, not the recipe (the recipe has no such field)
    assert snapshot.sensor_circulation_enabled is True
    assert snapshot.drain_after_process is False
    # calculated fields are internally consistent, and nonzero since dosing
    # is actually enabled here
    assert snapshot.calculated_total_nutrients_ml > 0
    assert round(snapshot.calculated_nutrient_a_ml + snapshot.calculated_nutrient_b_ml, 3) == (
        snapshot.calculated_total_nutrients_ml
    )


def test_run_config_snapshot_raises_for_invalid_recipe_instead_of_being_built():
    invalid_recipe = _valid_stored_recipe(nutrient_a_percent=150.0)
    with pytest.raises(RecipeValidationError):
        RunConfigSnapshot.build(invalid_recipe, BASE_SETTINGS)


def test_run_config_snapshot_raises_for_invalid_run_options():
    with pytest.raises(RecipeValidationError):
        RunConfigSnapshot.build(_valid_stored_recipe(), BASE_SETTINGS, RunOptions(drain_after_process="false"))  # type: ignore[arg-type]


# --- Legacy dosing gate, at the RunConfigSnapshot/build_run_config level ----------


def test_legacy_recipe_with_dosing_disabled_still_builds_a_valid_run_config():
    # The normal RO fill must keep working even for a recipe still flagged
    # for legacy dosing review - only nutrient dosing itself is blocked.
    legacy_recipe = dict(
        DEFAULT_RECIPE,
        target_ro_water_l=90.0,
        legacy_dosing_needs_review=True,
        nutrient_dosing_enabled=False,
        nutrients_ml_per_100_l=0.0,
    )

    run_config = build_run_config(BASE_SETTINGS, legacy_recipe)

    assert run_config["target_total_liters"] == 90.0
    snapshot = run_config["recipe_run_config_snapshot"]
    assert snapshot["legacy_dosing_needs_review"] is True
    assert snapshot["nutrient_dosing_enabled"] is False
    assert snapshot["calculated_total_nutrients_ml"] == 0.0


def test_legacy_recipe_cannot_enable_nutrient_dosing():
    legacy_recipe = dict(
        DEFAULT_RECIPE,
        target_ro_water_l=90.0,
        legacy_dosing_needs_review=True,
        nutrient_dosing_enabled=True,
    )

    with pytest.raises(RecipeValidationError, match="Legacy"):
        build_run_config(BASE_SETTINGS, legacy_recipe)


def test_dosing_disabled_forces_calculated_amounts_to_zero_even_with_a_dose_set():
    # nutrients_ml_per_100_l stays visible/unchanged in the recipe itself,
    # but nothing will actually be dosed - the snapshot must reflect reality
    # (0), not a hypothetical "if it were enabled" number.
    recipe = _valid_stored_recipe(
        nutrient_dosing_enabled=False,
        nutrients_ml_per_100_l=500.0,
        nutrient_a_percent=40.0,
    )
    snapshot = RunConfigSnapshot.build(recipe, BASE_SETTINGS)

    assert snapshot.nutrients_ml_per_100_l == 500.0  # unchanged, informational
    assert snapshot.calculated_total_nutrients_ml == 0.0
    assert snapshot.calculated_nutrient_a_ml == 0.0
    assert snapshot.calculated_nutrient_b_ml == 0.0
