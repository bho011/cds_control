"""Schema-Migration der Rezeptbuch-Favoriten (schema_version 1 -> 4)."""

from __future__ import annotations

from typing import Any

from domain.recipe_model import RECIPE_SCHEMA_VERSION

from . import book_io
from .book_io import RecipeBookCorruptedError, _require_int_metadata

# book_io.RECIPE_BOOK_PATH read through the module, not as a bare name -
# see queries.py for why (only used in error-message text here, but kept
# consistent with the one place where this actually mattered for real
# file I/O).


def _require_legacy_dict_or_none(value: Any, field_name: str) -> dict[str, Any]:
    """Fail-closed guard for the legacy_process_values/legacy_recipe_values
    quarantine dicts read from a favorite during migration: a wrong-typed
    value (e.g. a string) must raise a controlled RecipeBookCorruptedError,
    never a raw ValueError/TypeError from dict(value) - Python's dict()
    tries to interpret a string as an iterable of key/value pairs and blows
    up with a confusing, low-level error otherwise."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RecipeBookCorruptedError(
            f"{book_io.RECIPE_BOOK_PATH}: '{field_name}' muss ein Objekt (dict) oder nicht "
            f"gesetzt sein (war: {value!r}, Typ {type(value).__name__})."
        )
    return dict(value)


def _migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """
    schema_version 1 -> 2:

    - target_fill_total_liters -> target_ro_water_l (unambiguous 1:1 rename:
      this was already the only value that actually drove the RO fill target).
    - volume_ro_correction_liters -> requested_ro_correction_l (unambiguous
      1:1 rename, same unit/meaning - itself quarantined again by the v2 -> v3
      step below, see _migrate_v2_to_v3).
    - volume_stock_1_ml / volume_stock_2_ml -> legacy_volume_stock_1_ml /
      legacy_volume_stock_2_ml. NOT auto-converted into nutrients_ml_per_100_l:
      the reference water amount those absolute doses were sized for is not
      reliably known (see docs/OPEN_RECIPE_DECISIONS.md). Values are kept for
      manual review, nutrients_ml_per_100_l stays at the safe, explicit
      default of 0.0 (no dose) until someone confirms a real value.
    - ec_adjustment_factor, addon_1_ml/addon_2_ml, all pH/EC-setpoint,
      sensor/process fields: unchanged, direct 1:1 fields already.
    """
    recipe = dict(raw)

    if "target_ro_water_l" not in recipe and "target_fill_total_liters" in recipe:
        recipe["target_ro_water_l"] = recipe.pop("target_fill_total_liters")
    else:
        recipe.pop("target_fill_total_liters", None)

    if "requested_ro_correction_l" not in recipe and "volume_ro_correction_liters" in recipe:
        recipe["requested_ro_correction_l"] = recipe.pop("volume_ro_correction_liters")
    else:
        recipe.pop("volume_ro_correction_liters", None)

    legacy_stock_1 = recipe.pop("volume_stock_1_ml", None)
    legacy_stock_2 = recipe.pop("volume_stock_2_ml", None)
    if "legacy_volume_stock_1_ml" not in recipe and legacy_stock_1 is not None:
        recipe["legacy_volume_stock_1_ml"] = legacy_stock_1
    if "legacy_volume_stock_2_ml" not in recipe and legacy_stock_2 is not None:
        recipe["legacy_volume_stock_2_ml"] = legacy_stock_2

    if recipe.get("legacy_volume_stock_1_ml") or recipe.get("legacy_volume_stock_2_ml"):
        recipe["legacy_dosing_needs_review"] = True

    recipe.setdefault("nutrients_ml_per_100_l", 0.0)
    recipe.setdefault("nutrient_a_percent", 50.0)
    recipe.setdefault("nutrient_dosing_enabled", False)
    recipe["schema_version"] = 2

    return recipe


def _migrate_v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    """
    schema_version 2 -> 3: separates fachliche Rezeptwerte from
    process/runtime-only concepts (see domain/recipe_model.py module
    docstring).

    - requested_ro_correction_l: a fixed RO correction is no longer a recipe
      value at all - a real, demand-driven EC correction is a future
      feature, not implemented, and had no hardware consumer anyway (see
      docs/RECIPE_AND_DOSING_RULES.md Section 4).
    - sensor_circulation_enabled: DOES have a real hardware consumer, but
      must be a fresh per-run RunOptions choice from now on, never a
      recipe-stored default that would silently re-activate it on load.
    - sensor_pump_seconds: a technical/ProcessSettings-shaped value, not a
      recipe value, and has no execution-side consumer in the recipe/
      Fill-and-Measure path today.
    - drain_after_process: has no execution-side consumer anywhere today;
      kept only for audit, not shown as a functional GUI control anymore.

    All four are removed from the recipe proper and quarantined under
    legacy_process_values purely for audit - they must never again activate
    a RunOption or reach RunConfig automatically, and trigger no hardware
    action by themselves.
    """
    recipe = dict(raw)
    legacy_process_values = _require_legacy_dict_or_none(
        recipe.get("legacy_process_values"), "legacy_process_values"
    )

    for key in (
        "requested_ro_correction_l",
        "sensor_circulation_enabled",
        "sensor_pump_seconds",
        "drain_after_process",
    ):
        if key in recipe:
            legacy_process_values.setdefault(key, recipe.pop(key))

    if legacy_process_values:
        recipe["legacy_process_values"] = legacy_process_values

    recipe["schema_version"] = 3
    return recipe


def _migrate_v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    """
    schema_version 3 -> 4: removes Addon 1/Addon 2 from the active recipe
    model entirely. They were meant as independent additions, but the real
    CDS process only has Nutrient Solution A/B, total dose, and the A/B
    ratio - already fully covered by the EC section (see
    domain/recipe_model.py module docstring, docs/RECIPE_AND_DOSING_RULES.md).

    addon_1_ml/addon_2_ml are moved into legacy_recipe_values, merge-safe:
    if legacy_recipe_values already has entries (e.g. from a hand-edited
    file, or a second migration run), those existing entries are preserved
    untouched - only addon_1_ml/addon_2_ml are added, via setdefault, never
    overwriting an existing key. This also makes the migration idempotent:
    running it again on an already-migrated recipe (no more addon_1_ml/
    addon_2_ml keys at the top level) is a no-op for legacy_recipe_values.
    """
    recipe = dict(raw)
    legacy_recipe_values = _require_legacy_dict_or_none(
        recipe.get("legacy_recipe_values"), "legacy_recipe_values"
    )

    for key in ("addon_1_ml", "addon_2_ml"):
        if key in recipe:
            legacy_recipe_values.setdefault(key, recipe.pop(key))

    if legacy_recipe_values:
        recipe["legacy_recipe_values"] = legacy_recipe_values

    recipe["schema_version"] = 4
    return recipe


def migrate_favorite_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Chains the per-favorite migration steps needed to reach
    RECIPE_SCHEMA_VERSION, based on the favorite's own schema_version (not
    just the book-level one) - so a book with partially-migrated favorites
    (e.g. after a manual edit) is still handled correctly. Called for EVERY
    favorite on every load_recipe_book() call, independent of the book-level
    schema_version - a book already at RECIPE_SCHEMA_VERSION can still
    contain an individual favorite left at an older version."""
    recipe = dict(raw)
    version = _require_int_metadata(recipe.get("schema_version", 1), "favorite.schema_version")

    if version > RECIPE_SCHEMA_VERSION:
        raise RecipeBookCorruptedError(
            f"{book_io.RECIPE_BOOK_PATH}: unbekannte zukünftige favorite.schema_version "
            f"{version} (unterstützt wird höchstens {RECIPE_SCHEMA_VERSION}). "
            "Die Datei wurde NICHT automatisch überschrieben."
        )

    if version < 2:
        recipe = _migrate_v1_to_v2(recipe)
        version = 2

    if version < 3:
        recipe = _migrate_v2_to_v3(recipe)
        version = 3

    if version < 4:
        recipe = _migrate_v3_to_v4(recipe)
        version = 4

    return recipe
