"""Brücke Rezept -> GUI-Vorschau bzw. RunConfig für den Prozessstart."""

from __future__ import annotations

from typing import Any

from domain.recipe_model import RecipePreview, RunConfigSnapshot, RunOptions, StoredRecipe


def get_recipe_preview(recipe: dict[str, Any]) -> RecipePreview:
    """Live, always-recomputable preview for the GUI - does not raise on
    out-of-range input, see domain/recipe_model.py::RecipePreview."""
    return RecipePreview.from_recipe(StoredRecipe.from_dict(recipe))


def build_run_config(
    base_settings: dict[str, Any],
    recipe: dict[str, Any],
    run_options: RunOptions | None = None,
) -> dict[str, Any]:
    """
    Projects the active recipe onto a process_settings.json snapshot to build
    the RunConfig actually used for a Fill-and-Measure run.

    Builds an immutable RunConfigSnapshot from StoredRecipe + base_settings
    (ProcessSettings) + run_options first (raises RecipeValidationError/
    ValueError on any problem - fail closed, nothing is clamped). If
    run_options is omitted, RunConfigSnapshot.build() defaults to
    RunOptions() (everything off) - the safe, fail-closed choice, never an
    implicit "whatever the recipe used to say".

    Only target_ro_water_l and sensor_circulation_enabled have a real
    execution-side consumer today (statemachine/fill_and_measure_state_machine.py);
    the full snapshot is attached under "recipe_run_config_snapshot" for
    display/audit purposes - see domain/recipe_model.py::RunConfigSnapshot
    docstring for the exact remaining integration point. Every value used
    below is read exclusively from the snapshot, never again from the raw
    recipe dict/StoredRecipe/run_options.
    """
    stored_recipe = StoredRecipe.from_dict(recipe)
    snapshot = RunConfigSnapshot.build(stored_recipe, base_settings, run_options)

    run_config = dict(base_settings)
    run_config["fill_mode"] = "absolute"
    run_config["target_total_liters"] = snapshot.target_ro_water_l
    run_config["enable_sensor_circulation"] = bool(snapshot.sensor_circulation_enabled)
    run_config["recipe_run_config_snapshot"] = snapshot.to_dict()

    return run_config
