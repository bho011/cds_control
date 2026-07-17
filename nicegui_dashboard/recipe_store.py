from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


RECIPE_BOOK_PATH = Path("recipes/dashboard_recipes.json")
FAVORITE_SLOTS = (1, 2, 3)


DEFAULT_RECIPE: dict[str, Any] = {
    "slot": 1,
    "recipe_name": "RO Water Test 50L",
    "target_tank": "Ch 1 - T1",
    "target_fill_total_liters": 50.0,

    "target_ec_ms_cm": 2.4,
    "ec_mixing_time_seconds": 300,
    "volume_stock_1_ml": 101.0,
    "volume_stock_2_ml": 19.0,
    "volume_ro_correction_liters": 19.0,
    "ec_adjustment_factor": 1.0,

    "target_ph": 6.0,
    "volume_acid_1_ml": 21.0,
    "volume_acid_2_ml": 15.0,
    "volume_base_ml": 11.0,
    "ph_mixing_time_seconds": 20,
    "ph_adjustment_factor": 0.2,

    "addon_1_ml": 50.0,
    "addon_2_ml": 50.0,

    "sensor_circulation_enabled": True,
    "sensor_pump_seconds": 200,
    "drain_after_process": False,
    "notes": "Visual recipe draft. No peristaltic pump control yet.",
    "updated_at": None,
}


def _default_recipe(slot: int) -> dict[str, Any]:
    recipe = deepcopy(DEFAULT_RECIPE)
    recipe["slot"] = slot
    recipe["recipe_name"] = f"Favorit {slot}"
    recipe["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return recipe


def default_recipe_book() -> dict[str, Any]:
    recipes = [_default_recipe(slot) for slot in FAVORITE_SLOTS]
    recipes[0]["recipe_name"] = "RO Water Test 50L"

    return {
        "schema_version": 1,
        "active_slot": 1,
        "favorites": recipes,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def ensure_recipe_book() -> None:
    if RECIPE_BOOK_PATH.exists():
        return

    RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_recipe_book(default_recipe_book())


def load_recipe_book() -> dict[str, Any]:
    ensure_recipe_book()

    try:
        with RECIPE_BOOK_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        data = default_recipe_book()
        save_recipe_book(data)
        return data

    changed = False

    if "favorites" not in data or not isinstance(data["favorites"], list):
        data["favorites"] = []
        changed = True

    existing_by_slot = {
        int(recipe.get("slot", 0)): recipe
        for recipe in data["favorites"]
        if isinstance(recipe, dict)
    }

    favorites = []
    for slot in FAVORITE_SLOTS:
        recipe = existing_by_slot.get(slot)
        if recipe is None:
            recipe = _default_recipe(slot)
            changed = True
        favorites.append(recipe)

    data["favorites"] = favorites

    if int(data.get("active_slot", 1)) not in FAVORITE_SLOTS:
        data["active_slot"] = 1
        changed = True

    if changed:
        save_recipe_book(data)

    return data


def save_recipe_book(data: dict[str, Any]) -> None:
    RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")

    with RECIPE_BOOK_PATH.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")


def get_recipe_by_slot(recipe_book: dict[str, Any], slot: int) -> dict[str, Any]:
    for recipe in recipe_book.get("favorites", []):
        if int(recipe.get("slot", 0)) == int(slot):
            return recipe

    return _default_recipe(slot)


def get_active_recipe(recipe_book: dict[str, Any]) -> dict[str, Any]:
    active_slot = int(recipe_book.get("active_slot", 1))
    return get_recipe_by_slot(recipe_book, active_slot)


def save_recipe_to_slot(slot: int, recipe: dict[str, Any], make_active: bool = True) -> dict[str, Any]:
    slot = int(slot)
    if slot not in FAVORITE_SLOTS:
        raise ValueError(f"Invalid recipe slot: {slot}")

    recipe_book = load_recipe_book()
    recipe = dict(recipe)
    recipe["slot"] = slot
    recipe["updated_at"] = datetime.now().isoformat(timespec="seconds")

    favorites = []
    replaced = False

    for existing in recipe_book.get("favorites", []):
        if int(existing.get("slot", 0)) == slot:
            favorites.append(recipe)
            replaced = True
        else:
            favorites.append(existing)

    if not replaced:
        favorites.append(recipe)

    favorites = sorted(favorites, key=lambda item: int(item.get("slot", 0)))[:3]
    recipe_book["favorites"] = favorites

    if make_active:
        recipe_book["active_slot"] = slot

    save_recipe_book(recipe_book)
    return recipe_book


def build_run_config(base_settings: dict[str, Any], recipe: dict[str, Any]) -> dict[str, Any]:
    """Projects the active recipe onto a process_settings.json snapshot to build
    the RunConfig actually used for a Fill-and-Measure run. Only fields with a
    real execution-side consumer are connected today (target_fill_total_liters,
    sensor_circulation_enabled) - the recipe's EC/pH/dosing fields have no
    consumer yet, since chemical dosing is not implemented."""
    run_config = dict(base_settings)
    run_config["fill_mode"] = "absolute"
    run_config["target_total_liters"] = float(recipe["target_fill_total_liters"])
    run_config["enable_sensor_circulation"] = bool(recipe.get("sensor_circulation_enabled", True))

    if run_config["target_total_liters"] > float(run_config["max_mixer_liters"]):
        raise ValueError(
            f"Rezept-Zielvolumen {run_config['target_total_liters']} L überschreitet "
            f"max_mixer_liters ({run_config['max_mixer_liters']} L)."
        )

    return run_config


def set_active_slot(slot: int) -> dict[str, Any]:
    slot = int(slot)
    if slot not in FAVORITE_SLOTS:
        raise ValueError(f"Invalid recipe slot: {slot}")

    recipe_book = load_recipe_book()
    recipe_book["active_slot"] = slot
    save_recipe_book(recipe_book)
    return recipe_book
