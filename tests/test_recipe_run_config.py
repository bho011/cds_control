"""
Phase 5 regression tests: nicegui_dashboard/recipe_store.py::build_run_config().
Pure logic (a dict merge + one cross-field check), no GPIO/OPC-UA/I-O beyond
what the caller already provides.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nicegui_dashboard.process_controller import PROCESS_SETTINGS_SCHEMA
from nicegui_dashboard.recipe_store import DEFAULT_RECIPE, build_run_config
from services.settings_validation import validate_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_SETTINGS = {
    "fill_mode": "delta",
    "target_add_liters": 1.0,
    "target_total_liters": 1.0,
    "max_mixer_liters": 200.0,
}


def test_happy_path_overrides_only_the_connected_fields():
    recipe = dict(DEFAULT_RECIPE, target_fill_total_liters=75.0, sensor_circulation_enabled=False)

    run_config = build_run_config(BASE_SETTINGS, recipe)

    assert run_config["fill_mode"] == "absolute"
    assert run_config["target_total_liters"] == 75.0
    assert run_config["enable_sensor_circulation"] is False
    # untouched passthrough keys
    assert run_config["max_mixer_liters"] == 200.0
    assert run_config["target_add_liters"] == 1.0


def test_original_settings_dict_is_not_mutated():
    original = dict(BASE_SETTINGS)
    build_run_config(BASE_SETTINGS, dict(DEFAULT_RECIPE, target_fill_total_liters=10.0))
    assert BASE_SETTINGS == original


def test_target_exceeding_max_mixer_liters_is_rejected():
    recipe = dict(DEFAULT_RECIPE, target_fill_total_liters=250.0)
    with pytest.raises(ValueError, match="max_mixer_liters"):
        build_run_config(BASE_SETTINGS, recipe)


def test_missing_target_fill_total_liters_is_rejected():
    recipe = dict(DEFAULT_RECIPE)
    del recipe["target_fill_total_liters"]
    with pytest.raises(KeyError):
        build_run_config(BASE_SETTINGS, recipe)


def test_missing_sensor_circulation_enabled_defaults_to_true():
    recipe = dict(DEFAULT_RECIPE, target_fill_total_liters=10.0)
    del recipe["sensor_circulation_enabled"]

    run_config = build_run_config(BASE_SETTINGS, recipe)

    assert run_config["enable_sensor_circulation"] is True


def test_real_recipe_merged_onto_real_settings_passes_schema():
    with (REPO_ROOT / "config/process_settings.json").open(encoding="utf-8") as file:
        real_settings = json.load(file)

    with (REPO_ROOT / "recipes/dashboard_recipes.json").open(encoding="utf-8") as file:
        recipe_book = json.load(file)
    real_recipe = recipe_book["favorites"][0]

    run_config = build_run_config(real_settings, real_recipe)

    validate_settings(run_config, PROCESS_SETTINGS_SCHEMA, "RunConfig (real files)")  # raises on failure
