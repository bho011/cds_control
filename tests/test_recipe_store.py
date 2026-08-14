"""
nicegui_dashboard/recipe_store.py: atomic/locked JSON persistence, the
fail-closed corrupted-file behaviour, and the schema_version 1 -> 2 -> 3
migration chain. No GPIO/OPC-UA, only tmp-path JSON files - RECIPE_BOOK_PATH
is monkeypatched per test so the real recipes/dashboard_recipes.json is
never touched.
"""

from __future__ import annotations

import json
import threading

import pytest

import nicegui_dashboard.recipe_store as recipe_store
from domain.recipe_limits import RecipeValidationError
from nicegui_dashboard.recipe_store import (
    RECIPE_SCHEMA_VERSION,
    RecipeBookCorruptedError,
    load_recipe_book,
    migrate_favorite_dict,
    save_recipe_to_slot,
    set_active_slot,
)


@pytest.fixture(autouse=True)
def _isolated_recipe_book(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_store.book_io, "RECIPE_BOOK_PATH", tmp_path / "dashboard_recipes.json")
    return tmp_path


def _valid_recipe(**overrides) -> dict:
    recipe = dict(recipe_store.DEFAULT_RECIPE)
    recipe.update(overrides)
    return recipe


# --- Save: validation gate + atomic write ------------------------------------------


def test_invalid_recipe_is_not_saved():
    with pytest.raises(RecipeValidationError):
        save_recipe_to_slot(slot=1, recipe=_valid_recipe(target_ro_water_l=0.0))

    assert not recipe_store.book_io.RECIPE_BOOK_PATH.exists()


def test_valid_recipe_is_saved_and_reloadable():
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Test Recipe", target_ro_water_l=42.0))

    reloaded = load_recipe_book()
    saved = next(r for r in reloaded["favorites"] if r["slot"] == 2)
    assert saved["recipe_name"] == "Test Recipe"
    assert saved["target_ro_water_l"] == 42.0


def test_save_is_atomic_no_tmp_file_left_behind():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe())

    leftover_tmp_files = list(recipe_store.book_io.RECIPE_BOOK_PATH.parent.glob("*.tmp"))
    assert leftover_tmp_files == []
    assert recipe_store.book_io.RECIPE_BOOK_PATH.exists()


def test_second_save_creates_a_backup_of_the_previous_file():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="First"))
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Second"))

    backup_path = recipe_store.book_io.RECIPE_BOOK_PATH.with_suffix(".json.bak")
    assert backup_path.exists()
    with backup_path.open(encoding="utf-8") as file:
        backup_data = json.load(file)
    assert backup_data["favorites"][0]["recipe_name"] == "First"


# --- Corrupted file: fail closed, never auto-overwritten ---------------------------


def test_corrupted_json_raises_and_is_not_overwritten():
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()

    # the broken file must still be exactly as broken as before - no silent
    # "reset to defaults" write.
    assert recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8") == "{not valid json"


def test_non_object_json_top_level_is_treated_as_corrupted():
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


def test_favorites_as_a_string_is_rejected_and_file_stays_byte_identical():
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    original_text = json.dumps(
        {"schema_version": RECIPE_SCHEMA_VERSION, "active_slot": 1, "favorites": "oops"}
    )
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(original_text, encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()

    assert recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8") == original_text


def test_missing_favorites_key_is_rejected_and_file_stays_byte_identical():
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    original_text = json.dumps({"schema_version": RECIPE_SCHEMA_VERSION, "active_slot": 1})
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(original_text, encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()

    assert recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8") == original_text


# --- schema_version 1 -> 2 -> 3 migration chain ---------------------------------


def test_migrate_favorite_dict_renames_unambiguous_fields():
    legacy = {
        "slot": 1,
        "recipe_name": "Legacy",
        "target_fill_total_liters": 50.0,
        "volume_ro_correction_liters": 19.0,
        "ec_adjustment_factor": 1.0,
    }

    migrated = migrate_favorite_dict(legacy)

    assert migrated["target_ro_water_l"] == 50.0
    assert "target_fill_total_liters" not in migrated
    assert "volume_ro_correction_liters" not in migrated
    assert migrated["schema_version"] == RECIPE_SCHEMA_VERSION
    # a fixed RO correction is no longer a recipe value at all (v2 -> v3) -
    # it is quarantined for audit, not exposed as a top-level field.
    assert "requested_ro_correction_l" not in migrated
    assert migrated["legacy_process_values"]["requested_ro_correction_l"] == 19.0


def test_migrate_favorite_dict_quarantines_ambiguous_stock_volumes():
    legacy = {
        "slot": 1,
        "target_fill_total_liters": 50.0,
        "volume_stock_1_ml": 101.0,
        "volume_stock_2_ml": 19.0,
    }

    migrated = migrate_favorite_dict(legacy)

    # not silently converted into a nutrients-per-100l dose
    assert migrated["nutrients_ml_per_100_l"] == 0.0
    assert migrated["nutrient_a_percent"] == 50.0
    # but not discarded either - kept for manual review
    assert migrated["legacy_volume_stock_1_ml"] == 101.0
    assert migrated["legacy_volume_stock_2_ml"] == 19.0
    assert migrated["legacy_dosing_needs_review"] is True
    assert "volume_stock_1_ml" not in migrated
    assert "volume_stock_2_ml" not in migrated


def test_migrate_favorite_dict_without_legacy_stock_needs_no_review():
    migrated = migrate_favorite_dict({"slot": 1, "target_fill_total_liters": 50.0})
    assert migrated.get("legacy_dosing_needs_review", False) is False


def test_migrate_favorite_dict_defaults_nutrient_dosing_to_disabled():
    migrated = migrate_favorite_dict({"slot": 1, "target_fill_total_liters": 50.0})
    assert migrated["nutrient_dosing_enabled"] is False


def test_migrate_v2_to_v3_quarantines_process_runtime_fields():
    from nicegui_dashboard.recipe_store import _migrate_v2_to_v3

    v2_recipe = {
        "slot": 1,
        "schema_version": 2,
        "recipe_name": "V2 Recipe",
        "target_ro_water_l": 50.0,
        "requested_ro_correction_l": 19.0,
        "sensor_circulation_enabled": True,
        "sensor_pump_seconds": 200,
        "drain_after_process": False,
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
        "nutrients_ml_per_100_l": 0.0,
        "nutrient_a_percent": 50.0,
        "nutrient_dosing_enabled": False,
    }

    # Calling the v2->v3 migration step in isolation (not the full chain via
    # migrate_favorite_dict) to check exactly this step's own behaviour,
    # unaffected by the later v3->v4 addon-removal step.
    migrated = _migrate_v2_to_v3(v2_recipe)

    assert migrated["schema_version"] == 3
    # quarantined, not top-level, not silently discarded
    for key in (
        "requested_ro_correction_l",
        "sensor_circulation_enabled",
        "sensor_pump_seconds",
        "drain_after_process",
    ):
        assert key not in migrated
    assert migrated["legacy_process_values"] == {
        "requested_ro_correction_l": 19.0,
        "sensor_circulation_enabled": True,
        "sensor_pump_seconds": 200,
        "drain_after_process": False,
    }
    # fachliche Rezeptwerte (addons) are untouched by this migration step -
    # they are only removed one step later, by _migrate_v3_to_v4.
    assert migrated["addon_1_ml"] == 50.0
    assert migrated["addon_2_ml"] == 30.0


def test_older_recipe_with_bad_legacy_process_values_type_is_rejected_not_a_raw_valueerror():
    # dict("oops") raises a raw, confusing ValueError (Python tries to treat
    # the string as an iterable of key/value pairs) - this must instead
    # surface as a controlled RecipeBookCorruptedError, and the file must be
    # left untouched.
    book = {
        "schema_version": 2,
        "active_slot": 1,
        "favorites": [
            dict(
                recipe_store.DEFAULT_RECIPE,
                slot=1,
                schema_version=2,
                legacy_process_values="oops",
            ),
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    original_text = json.dumps(book)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(original_text, encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()

    assert recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8") == original_text


# --- schema_version 3 -> 4 migration: Addon 1/Addon 2 removal ---------------------


def test_migrate_v3_to_v4_moves_addons_into_legacy_recipe_values():
    from nicegui_dashboard.recipe_store import _migrate_v3_to_v4

    v3_recipe = {
        "slot": 1,
        "schema_version": 3,
        "recipe_name": "V3 Recipe",
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
    }

    migrated = _migrate_v3_to_v4(v3_recipe)

    assert migrated["schema_version"] == 4
    assert "addon_1_ml" not in migrated
    assert "addon_2_ml" not in migrated
    assert migrated["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 30.0}


def test_migrate_v3_to_v4_preserves_existing_legacy_recipe_values():
    from nicegui_dashboard.recipe_store import _migrate_v3_to_v4

    v3_recipe = {
        "slot": 1,
        "schema_version": 3,
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
        "legacy_recipe_values": {"some_other_key": "x"},
    }

    migrated = _migrate_v3_to_v4(v3_recipe)

    assert migrated["legacy_recipe_values"] == {
        "some_other_key": "x",
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
    }


def test_migrate_v3_to_v4_is_idempotent_on_a_second_run():
    from nicegui_dashboard.recipe_store import _migrate_v3_to_v4

    v3_recipe = {
        "slot": 1,
        "schema_version": 3,
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
        "legacy_recipe_values": {"some_other_key": "x"},
    }

    once = _migrate_v3_to_v4(v3_recipe)
    twice = _migrate_v3_to_v4(once)

    assert twice["legacy_recipe_values"] == once["legacy_recipe_values"]
    assert "addon_1_ml" not in twice
    assert "addon_2_ml" not in twice
    assert twice["schema_version"] == 4


def test_older_recipe_with_bad_legacy_recipe_values_type_is_rejected_not_a_raw_valueerror():
    book = {
        "schema_version": 3,
        "active_slot": 1,
        "favorites": [
            dict(
                recipe_store.DEFAULT_RECIPE,
                slot=1,
                schema_version=3,
                legacy_recipe_values="oops",
            ),
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    original_text = json.dumps(book)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(original_text, encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()

    assert recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8") == original_text


def test_migrate_favorite_dict_full_chain_moves_addons_into_legacy_recipe_values():
    v2_recipe = {
        "slot": 1,
        "schema_version": 2,
        "recipe_name": "V2 Recipe",
        "target_ro_water_l": 50.0,
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
        "nutrients_ml_per_100_l": 0.0,
        "nutrient_a_percent": 50.0,
        "nutrient_dosing_enabled": False,
    }

    migrated = migrate_favorite_dict(v2_recipe)

    assert migrated["schema_version"] == RECIPE_SCHEMA_VERSION
    assert "addon_1_ml" not in migrated
    assert "addon_2_ml" not in migrated
    assert migrated["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 30.0}


def test_load_recipe_book_migrates_a_v1_file_on_disk_and_resaves_it():
    v1_book = {
        "schema_version": 1,
        "active_slot": 1,
        "favorites": [
            {
                "slot": 1,
                "recipe_name": "Old Recipe",
                "target_fill_total_liters": 50.0,
                "volume_stock_1_ml": 101.0,
                "volume_stock_2_ml": 19.0,
                "volume_ro_correction_liters": 19.0,
                "ec_adjustment_factor": 1.0,
                "addon_1_ml": 50.0,
                "addon_2_ml": 50.0,
            },
            # A file that already exists on disk must contain all three
            # favorite slots (see the missing-slot fail-closed tests below) -
            # slots 2/3 just need to be valid enough to migrate cleanly.
            {"slot": 2, "recipe_name": "Old Recipe 2", "target_fill_total_liters": 40.0},
            {"slot": 3, "recipe_name": "Old Recipe 3", "target_fill_total_liters": 30.0},
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(v1_book), encoding="utf-8")

    loaded = load_recipe_book()

    assert loaded["schema_version"] == RECIPE_SCHEMA_VERSION
    migrated_recipe = next(r for r in loaded["favorites"] if r["slot"] == 1)
    assert migrated_recipe["target_ro_water_l"] == 50.0
    assert migrated_recipe["legacy_volume_stock_1_ml"] == 101.0
    assert migrated_recipe["nutrients_ml_per_100_l"] == 0.0
    # addons no longer belong to the active recipe model - quarantined under
    # legacy_recipe_values by the v3 -> v4 step, not discarded, not top-level.
    assert "addon_1_ml" not in migrated_recipe
    assert "addon_2_ml" not in migrated_recipe
    assert migrated_recipe["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 50.0}
    # the old RO correction is quarantined, not a live recipe value anymore
    assert "requested_ro_correction_l" not in migrated_recipe
    assert migrated_recipe["legacy_process_values"]["requested_ro_correction_l"] == 19.0

    # persisted back to disk, not just returned in memory
    with recipe_store.book_io.RECIPE_BOOK_PATH.open(encoding="utf-8") as file:
        on_disk = json.load(file)
    assert on_disk["schema_version"] == RECIPE_SCHEMA_VERSION


# --- Fail-closed recipe-book metadata validation ---------------------------------


@pytest.mark.parametrize(
    "bad_schema_version",
    ["two", 2.5, True, None, [2], {"v": 2}],
)
def test_bad_schema_version_type_raises_recipe_book_corrupted_error(bad_schema_version):
    book = {"schema_version": bad_schema_version, "active_slot": 1, "favorites": []}
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


@pytest.mark.parametrize(
    "bad_active_slot",
    ["one", 1.5, True, [1], {"slot": 1}],
)
def test_bad_active_slot_type_raises_recipe_book_corrupted_error(bad_active_slot):
    # All three slots must be present so this genuinely exercises the
    # active_slot check itself, not the (separate) missing-slot check.
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": bad_active_slot,
        "favorites": [
            dict(recipe_store.DEFAULT_RECIPE, slot=1),
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


@pytest.mark.parametrize(
    "bad_slot",
    ["one", 1.5, True, [1], {"slot": 1}],
)
def test_bad_favorite_slot_type_raises_recipe_book_corrupted_error(bad_slot):
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": [dict(recipe_store.DEFAULT_RECIPE, slot=bad_slot)],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


@pytest.mark.parametrize("out_of_range_slot", [0, 4, -1, 99])
def test_favorite_slot_out_of_range_raises_recipe_book_corrupted_error(out_of_range_slot):
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": [dict(recipe_store.DEFAULT_RECIPE, slot=out_of_range_slot)],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


@pytest.mark.parametrize("out_of_range_active_slot", [0, 4, -1, 99])
def test_active_slot_out_of_range_raises_recipe_book_corrupted_error(out_of_range_active_slot):
    # All three slots must be present so this genuinely exercises the
    # active_slot check itself, not the (separate) missing-slot check.
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": out_of_range_active_slot,
        "favorites": [
            dict(recipe_store.DEFAULT_RECIPE, slot=1),
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


def test_existing_file_with_only_slot_1_is_rejected_and_file_stays_byte_identical():
    # A file that already exists must contain exactly Slot 1/2/3 - a missing
    # slot must no longer be silently filled with a default (that
    # self-healing could mask a genuinely lost/corrupted favorite). Only
    # ensure_recipe_book(), for a file that does not exist at all yet, may
    # still create a fresh default_recipe_book() with three defaults.
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": [dict(recipe_store.DEFAULT_RECIPE, slot=1)],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    original_text = json.dumps(book)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(original_text, encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()

    assert recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8") == original_text


def test_duplicate_slots_raise_recipe_book_corrupted_error():
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": [
            dict(recipe_store.DEFAULT_RECIPE, slot=1, recipe_name="First"),
            dict(recipe_store.DEFAULT_RECIPE, slot=1, recipe_name="Duplicate"),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


def test_non_dict_favorite_raises_recipe_book_corrupted_error():
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": ["not a dict", dict(recipe_store.DEFAULT_RECIPE, slot=2)],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


def test_unknown_future_schema_version_raises_recipe_book_corrupted_error():
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION + 1,
        "active_slot": 1,
        "favorites": [dict(recipe_store.DEFAULT_RECIPE, slot=1)],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


def test_mixed_schema_version_favorites_are_each_migrated_from_their_own_version():
    # A book with a book-level schema_version already at RECIPE_SCHEMA_VERSION
    # but one favorite still hand-edited back down to an older version (e.g.
    # after a manual JSON edit) - migrate_favorite_dict() must still catch
    # and migrate that one favorite based on ITS OWN schema_version, not the
    # book-level one, and doing so must also bump the book-level version.
    book = {
        "schema_version": 3,
        "active_slot": 1,
        "favorites": [
            {
                "slot": 1,
                "schema_version": 1,
                "recipe_name": "Old",
                "target_fill_total_liters": 50.0,
                "addon_1_ml": 10.0,
            },
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    loaded = load_recipe_book()

    assert loaded["schema_version"] == RECIPE_SCHEMA_VERSION
    slot_1 = next(r for r in loaded["favorites"] if r["slot"] == 1)
    assert slot_1["schema_version"] == RECIPE_SCHEMA_VERSION
    assert slot_1["target_ro_water_l"] == 50.0
    assert "addon_1_ml" not in slot_1
    assert slot_1["legacy_recipe_values"] == {"addon_1_ml": 10.0}


def test_book_already_at_v4_with_one_favorite_still_at_v3_is_still_migrated():
    # The specific bug this closes: migration used to be gated ONLY on the
    # book-level schema_version (`if schema_version < RECIPE_SCHEMA_VERSION`)
    # - with the book-level version already AT RECIPE_SCHEMA_VERSION, that
    # whole branch was skipped entirely, so an individual favorite left
    # behind at an older version (e.g. after a hand edit, or a partial
    # write) was silently never migrated - its addon_1_ml/addon_2_ml would
    # stay at the top level forever instead of being quarantined. Every
    # favorite must now be migrated based on its OWN schema_version,
    # regardless of the book-level one.
    v3_favorite = {
        "slot": 1,
        "schema_version": 3,
        "recipe_name": "Still V3",
        "target_ro_water_l": 50.0,
        "target_ec_ms_cm": 1.5,
        "ec_mixing_time_seconds": 600,
        "nutrients_ml_per_100_l": 0.0,
        "nutrient_a_percent": 50.0,
        "ec_adjustment_factor": 1.0,
        "nutrient_dosing_enabled": False,
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
    }
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": [
            v3_favorite,
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    loaded = load_recipe_book()

    slot_1 = next(r for r in loaded["favorites"] if r["slot"] == 1)
    assert slot_1["schema_version"] == RECIPE_SCHEMA_VERSION
    assert "addon_1_ml" not in slot_1
    assert "addon_2_ml" not in slot_1
    assert slot_1["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 30.0}


def test_future_favorite_schema_version_is_rejected_even_if_book_level_version_is_current():
    book = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": [
            dict(recipe_store.DEFAULT_RECIPE, slot=1, schema_version=RECIPE_SCHEMA_VERSION + 1),
            dict(recipe_store.DEFAULT_RECIPE, slot=2),
            dict(recipe_store.DEFAULT_RECIPE, slot=3),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    with pytest.raises(RecipeBookCorruptedError):
        load_recipe_book()


def test_reloading_an_already_current_book_is_a_no_op():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Stable"))
    first_load = load_recipe_book()

    on_disk_before = recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8")
    second_load = load_recipe_book()
    on_disk_after = recipe_store.book_io.RECIPE_BOOK_PATH.read_text(encoding="utf-8")

    assert first_load == second_load
    assert on_disk_before == on_disk_after


# --- Strict slot typing: no int(slot) normalization -------------------------------


@pytest.mark.parametrize("bad_slot", [1.0, 3.0, True, False, "1", None, [1]])
def test_save_recipe_to_slot_rejects_non_strict_int_slot(bad_slot):
    with pytest.raises(ValueError):
        save_recipe_to_slot(slot=bad_slot, recipe=_valid_recipe())


@pytest.mark.parametrize("bad_slot", [1.0, 3.0, True, False, "1", None, [1]])
def test_set_active_slot_rejects_non_strict_int_slot(bad_slot):
    with pytest.raises(ValueError):
        set_active_slot(bad_slot)


@pytest.mark.parametrize("bad_slot", [1.0, 3.0, True, False, "1", None, [1]])
def test_get_recipe_by_slot_rejects_non_strict_int_slot(bad_slot):
    book = load_recipe_book()
    with pytest.raises(ValueError):
        recipe_store.get_recipe_by_slot(book, bad_slot)


def test_save_recipe_to_slot_accepts_genuine_int_slot():
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Real Int"))
    reloaded = load_recipe_book()
    saved = next(r for r in reloaded["favorites"] if r["slot"] == 2)
    assert saved["recipe_name"] == "Real Int"


# --- Favorite save semantics: only the target slot changes, others survive -------


def test_saving_three_slots_then_resaving_one_leaves_the_others_byte_for_byte_unchanged():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Favorite 1"))
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Favorite 2"))
    save_recipe_to_slot(slot=3, recipe=_valid_recipe(recipe_name="Favorite 3"))

    before = load_recipe_book()
    slot_1_before = next(r for r in before["favorites"] if r["slot"] == 1)
    slot_3_before = next(r for r in before["favorites"] if r["slot"] == 3)

    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Favorite 2 Updated"))

    after = load_recipe_book()
    slot_1_after = next(r for r in after["favorites"] if r["slot"] == 1)
    slot_2_after = next(r for r in after["favorites"] if r["slot"] == 2)
    slot_3_after = next(r for r in after["favorites"] if r["slot"] == 3)

    assert slot_1_after == slot_1_before
    assert slot_3_after == slot_3_before
    assert slot_2_after["recipe_name"] == "Favorite 2 Updated"

    # freshly reloaded from disk, all three still present
    fresh = load_recipe_book()
    assert {r["slot"] for r in fresh["favorites"]} == {1, 2, 3}


def test_make_active_false_leaves_active_slot_unchanged():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Favorite 1"), make_active=True)
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Favorite 2"), make_active=False)

    reloaded = load_recipe_book()
    assert reloaded["active_slot"] == 1


def test_make_active_true_sets_active_slot():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Favorite 1"), make_active=True)
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Favorite 2"), make_active=True)

    reloaded = load_recipe_book()
    assert reloaded["active_slot"] == 2


# --- Legacy audit data must survive a Recipe-Editor-style save -------------------


def test_saving_a_migrated_recipe_via_the_editor_preserves_legacy_audit_fields():
    """
    The bug this closes: nicegui_dashboard/pages/dashboard_page.py's
    build_recipe_dict_from_form() never includes legacy_process_values/
    legacy_recipe_values at all (the editor never shows or edits them) - so
    a plain save_recipe_to_slot(slot, form_dict) used to silently wipe both
    fields on every single edit, discarding real audit data. They must now
    be inherited from the slot's existing recipe whenever the incoming dict
    doesn't explicitly contain the key.
    """
    v3_favorite_1 = {
        "slot": 1,
        "schema_version": 3,
        "recipe_name": "Migrated Recipe",
        "target_tank": "Ch 1 - T1",
        "target_ro_water_l": 50.0,
        "target_ec_ms_cm": 1.5,
        "ec_mixing_time_seconds": 600,
        "nutrients_ml_per_100_l": 0.0,
        "nutrient_a_percent": 50.0,
        "ec_adjustment_factor": 1.0,
        "nutrient_dosing_enabled": False,
        "target_ph": 6.0,
        "volume_acid_1_ml": 21.0,
        "volume_acid_2_ml": 15.0,
        "volume_base_ml": 11.0,
        "ph_mixing_time_seconds": 20,
        "ph_adjustment_factor": 0.2,
        "addon_1_ml": 50.0,
        "addon_2_ml": 30.0,
        "legacy_process_values": {"sensor_circulation_enabled": True},
        "notes": "",
    }
    book = {
        "schema_version": 3,
        "active_slot": 1,
        "favorites": [
            v3_favorite_1,
            dict(recipe_store.DEFAULT_RECIPE, slot=2, recipe_name="Favorite 2"),
            dict(recipe_store.DEFAULT_RECIPE, slot=3, recipe_name="Favorite 3"),
        ],
    }
    recipe_store.book_io.RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    recipe_store.book_io.RECIPE_BOOK_PATH.write_text(json.dumps(book), encoding="utf-8")

    # v3 -> v4 migration on load: addon_1_ml/addon_2_ml quarantined into
    # legacy_recipe_values, legacy_process_values untouched.
    migrated = load_recipe_book()
    slot_1_after_migration = next(r for r in migrated["favorites"] if r["slot"] == 1)
    slot_2_after_migration = next(r for r in migrated["favorites"] if r["slot"] == 2)
    slot_3_after_migration = next(r for r in migrated["favorites"] if r["slot"] == 3)
    assert slot_1_after_migration["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 30.0}
    assert slot_1_after_migration["legacy_process_values"] == {"sensor_circulation_enabled": True}

    # Simulate a Recipe Editor save: a fresh form dict built the same way
    # build_recipe_dict_from_form() does - it never includes
    # legacy_process_values/legacy_recipe_values at all.
    form_dict = dict(slot_1_after_migration)
    form_dict.pop("legacy_process_values", None)
    form_dict.pop("legacy_recipe_values", None)
    form_dict["recipe_name"] = "Edited via editor"

    save_recipe_to_slot(slot=1, recipe=form_dict, make_active=True)

    final = load_recipe_book()
    slot_1_final = next(r for r in final["favorites"] if r["slot"] == 1)
    slot_2_final = next(r for r in final["favorites"] if r["slot"] == 2)
    slot_3_final = next(r for r in final["favorites"] if r["slot"] == 3)

    assert slot_1_final["recipe_name"] == "Edited via editor"
    assert slot_1_final["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 30.0}
    assert slot_1_final["legacy_process_values"] == {"sensor_circulation_enabled": True}

    # the other two slots are completely untouched by editing slot 1
    assert slot_2_final == slot_2_after_migration
    assert slot_3_final == slot_3_after_migration


def test_saving_a_recipe_that_explicitly_sets_legacy_fields_does_not_inherit_old_values():
    save_recipe_to_slot(
        slot=1,
        recipe=_valid_recipe(recipe_name="First", legacy_process_values={"a": 1}),
    )

    # This save explicitly provides legacy_process_values=None - since the
    # key IS present (even though its value is None), it must NOT be
    # silently replaced by the previous slot's {"a": 1}.
    result = save_recipe_to_slot(
        slot=1,
        recipe=_valid_recipe(recipe_name="Second", legacy_process_values=None),
    )

    slot_1 = next(r for r in result["favorites"] if r["slot"] == 1)
    assert slot_1["legacy_process_values"] is None


# --- Only canonical StoredRecipe data is ever persisted ---------------------------


def test_unknown_field_in_incoming_dict_is_not_persisted():
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Favorite 2"))
    save_recipe_to_slot(slot=3, recipe=_valid_recipe(recipe_name="Favorite 3"))
    slot_2_before = next(r for r in load_recipe_book()["favorites"] if r["slot"] == 2)
    slot_3_before = next(r for r in load_recipe_book()["favorites"] if r["slot"] == 3)

    incoming = _valid_recipe(recipe_name="Favorite 1")
    incoming["some_unknown_field"] = "should not persist"

    result = save_recipe_to_slot(slot=1, recipe=incoming)

    slot_1 = next(r for r in result["favorites"] if r["slot"] == 1)
    assert "some_unknown_field" not in slot_1
    # only the target slot is touched
    slot_2_after = next(r for r in result["favorites"] if r["slot"] == 2)
    slot_3_after = next(r for r in result["favorites"] if r["slot"] == 3)
    assert slot_2_after == slot_2_before
    assert slot_3_after == slot_3_before


def test_addon_fields_in_a_new_v4_save_are_not_persisted():
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Favorite 2"))
    save_recipe_to_slot(slot=3, recipe=_valid_recipe(recipe_name="Favorite 3"))
    slot_2_before = next(r for r in load_recipe_book()["favorites"] if r["slot"] == 2)
    slot_3_before = next(r for r in load_recipe_book()["favorites"] if r["slot"] == 3)

    # A stale caller (or a hand-edited form dict) that still supplies
    # addon_1_ml/addon_2_ml must not be able to reintroduce them into a
    # schema v4 save - StoredRecipe no longer has these fields at all.
    incoming = _valid_recipe(recipe_name="Favorite 1")
    incoming["addon_1_ml"] = 50.0
    incoming["addon_2_ml"] = 30.0

    result = save_recipe_to_slot(slot=1, recipe=incoming)

    slot_1 = next(r for r in result["favorites"] if r["slot"] == 1)
    assert "addon_1_ml" not in slot_1
    assert "addon_2_ml" not in slot_1
    slot_2_after = next(r for r in result["favorites"] if r["slot"] == 2)
    slot_3_after = next(r for r in result["favorites"] if r["slot"] == 3)
    assert slot_2_after == slot_2_before
    assert slot_3_after == slot_3_before


def test_canonicalized_save_still_preserves_existing_legacy_recipe_values():
    # First save establishes legacy_recipe_values on slot 1 (simulating a
    # post-migration state) via an explicit override.
    save_recipe_to_slot(
        slot=1,
        recipe=_valid_recipe(
            recipe_name="First", legacy_recipe_values={"addon_1_ml": 50.0, "addon_2_ml": 30.0}
        ),
    )

    # A Recipe-Editor-style save that never mentions legacy_recipe_values at
    # all - it must still survive the merge -> StoredRecipe -> to_dict()
    # round trip unchanged.
    editor_dict = _valid_recipe(recipe_name="Edited")
    del editor_dict["legacy_recipe_values"]

    result = save_recipe_to_slot(slot=1, recipe=editor_dict)

    slot_1 = next(r for r in result["favorites"] if r["slot"] == 1)
    assert slot_1["recipe_name"] == "Edited"
    assert slot_1["legacy_recipe_values"] == {"addon_1_ml": 50.0, "addon_2_ml": 30.0}


def test_controlled_normalized_int_actually_lands_in_the_saved_data():
    # ec_mixing_time_seconds=300.0 (a whole-number float) is controlled-
    # normalized to a real int by StoredRecipe.__post_init__ - since only
    # stored_recipe.to_dict() is ever persisted now (not the raw incoming
    # dict), the saved/reloaded value must be the normalized int, not the
    # original float.
    result = save_recipe_to_slot(
        slot=1, recipe=_valid_recipe(recipe_name="Favorite 1", ec_mixing_time_seconds=300.0)
    )

    slot_1 = next(r for r in result["favorites"] if r["slot"] == 1)
    assert slot_1["ec_mixing_time_seconds"] == 300
    assert type(slot_1["ec_mixing_time_seconds"]) is int

    reloaded = load_recipe_book()
    slot_1_reloaded = next(r for r in reloaded["favorites"] if r["slot"] == 1)
    assert type(slot_1_reloaded["ec_mixing_time_seconds"]) is int


# --- Read-modify-write locking (item 3) ---------------------------------------------


def test_concurrent_saves_to_different_slots_do_not_clobber_each_other():
    """
    Read-modify-write race this closes: save_recipe_to_slot() used to call
    load_recipe_book() and save_recipe_book() as two separate steps with no
    lock held across the whole sequence - two concurrent calls for different
    slots could each load the same pre-change book, apply only their own
    slot's change, and whichever wrote last would silently discard the
    other's change. _SAVE_LOCK is now held across the full load-modify-save
    sequence in save_recipe_to_slot() (an RLock, so the nested
    load_recipe_book()/save_recipe_book() calls inside it do not deadlock).
    """
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Baseline 1"))
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Baseline 2"))

    for iteration in range(20):
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def save_slot(slot: int, name: str) -> None:
            try:
                barrier.wait()
                save_recipe_to_slot(slot=slot, recipe=_valid_recipe(recipe_name=name))
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                errors.append(exc)

        name_1 = f"Updated 1 (iteration {iteration})"
        name_2 = f"Updated 2 (iteration {iteration})"

        t1 = threading.Thread(target=save_slot, args=(1, name_1))
        t2 = threading.Thread(target=save_slot, args=(2, name_2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []

        final = load_recipe_book()
        names_by_slot = {recipe["slot"]: recipe["recipe_name"] for recipe in final["favorites"]}
        assert names_by_slot[1] == name_1
        assert names_by_slot[2] == name_2


def test_concurrent_set_active_slot_and_save_recipe_to_slot_do_not_clobber_each_other():
    save_recipe_to_slot(slot=1, recipe=_valid_recipe(recipe_name="Baseline 1"))
    save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Baseline 2"))

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def do_save() -> None:
        try:
            barrier.wait()
            save_recipe_to_slot(slot=2, recipe=_valid_recipe(recipe_name="Renamed 2"), make_active=False)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_activate() -> None:
        try:
            barrier.wait()
            set_active_slot(1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=do_save)
    t2 = threading.Thread(target=do_activate)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []

    final = load_recipe_book()
    names_by_slot = {recipe["slot"]: recipe["recipe_name"] for recipe in final["favorites"]}
    assert names_by_slot[2] == "Renamed 2"
    assert final["active_slot"] == 1
