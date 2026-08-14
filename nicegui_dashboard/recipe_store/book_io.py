"""Rezeptbuch laden/speichern: Pfade, Defaults, atomares Schreiben, Fail-Closed-Parsing."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from domain.recipe_model import RECIPE_SCHEMA_VERSION, StoredRecipe


class RecipeBookCorruptedError(Exception):
    """Raised when recipes/dashboard_recipes.json exists but cannot be
    parsed, or contains metadata (schema_version/active_slot/favorite.slot)
    of the wrong type. Deliberately NOT auto-repaired: overwriting a broken
    recipe file with defaults could silently discard real, hand-tuned
    recipe data."""


RECIPE_BOOK_PATH = Path("recipes/dashboard_recipes.json")
FAVORITE_SLOTS = (1, 2, 3)

# RLock (not Lock): save_recipe_to_slot()/set_active_slot() (queries.py) hold
# this lock across their whole load-modify-save sequence (closing the
# read-modify-write race window), and internally call load_recipe_book()/
# save_recipe_book() again - both of which also take this same lock. A
# plain Lock would deadlock on that reentrant acquisition from the same
# thread; RLock allows it.
_SAVE_LOCK = threading.RLock()


DEFAULT_RECIPE: dict[str, Any] = StoredRecipe(
    slot=1,
    recipe_name="RO Water Test 50L",
    target_tank="Ch 1 - T1",
    target_ro_water_l=50.0,
    target_ec_ms_cm=2.4,
    ec_mixing_time_seconds=300,
    nutrients_ml_per_100_l=0.0,
    nutrient_a_percent=50.0,
    ec_adjustment_factor=1.0,
    target_ph=6.0,
    volume_acid_1_ml=21.0,
    volume_acid_2_ml=15.0,
    volume_base_ml=11.0,
    ph_mixing_time_seconds=20,
    ph_adjustment_factor=0.2,
    notes=(
        "Visual recipe draft. nutrients_ml_per_100_l defaults to 0.0 (no dose) "
        "until a confirmed value is entered - no peristaltic pump control yet."
    ),
).to_dict()


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
        "schema_version": RECIPE_SCHEMA_VERSION,
        "active_slot": 1,
        "favorites": recipes,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _require_int_metadata(value: Any, field_name: str) -> int:
    """Fail-closed parsing for recipe-book metadata (schema_version,
    active_slot, favorite.slot): a hand-edited or corrupted file with a
    wrong-typed value here (a string, a float, a bool, None, a list, ...)
    must raise a controlled RecipeBookCorruptedError, never a raw
    TypeError/ValueError from int(value). bool is rejected explicitly since
    isinstance(True, int) is True in Python."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeBookCorruptedError(
            f"{RECIPE_BOOK_PATH}: '{field_name}' muss eine Ganzzahl sein "
            f"(war: {value!r}, Typ {type(value).__name__})."
        )
    return value


def ensure_recipe_book() -> None:
    if RECIPE_BOOK_PATH.exists():
        return

    RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_recipe_book(default_recipe_book())


def load_recipe_book() -> dict[str, Any]:
    """
    Locked for its full body, not just the file read: this can itself
    trigger a self-repair write below (missing slots, schema migration),
    and holding the lock for that whole read-modify-write sequence is what
    prevents two concurrent callers from each repairing from the same stale
    snapshot and one silently clobbering the other's repair.
    """
    # Local import: migrations.py imports RECIPE_BOOK_PATH/RecipeBookCorruptedError/
    # _require_int_metadata from THIS module at its own top level, so a
    # top-level import here would be circular. Deferred until the function
    # actually runs, by which point both modules are fully loaded.
    from .migrations import migrate_favorite_dict

    with _SAVE_LOCK:
        ensure_recipe_book()

        try:
            with RECIPE_BOOK_PATH.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise RecipeBookCorruptedError(
                f"{RECIPE_BOOK_PATH} ist beschädigt oder nicht lesbar ({exc}). "
                "Die Datei wurde NICHT automatisch mit Standardwerten überschrieben - "
                "bitte manuell prüfen bzw. aus einem Backup (*.json.bak) wiederherstellen."
            ) from exc

        if not isinstance(data, dict):
            raise RecipeBookCorruptedError(
                f"{RECIPE_BOOK_PATH} enthält kein JSON-Objekt auf oberster Ebene. "
                "Die Datei wurde NICHT automatisch überschrieben."
            )

        changed = False

        if "favorites" not in data or not isinstance(data["favorites"], list):
            raise RecipeBookCorruptedError(
                f"{RECIPE_BOOK_PATH}: 'favorites' fehlt oder ist keine Liste. "
                "Die Datei wurde NICHT automatisch überschrieben."
            )

        schema_version = _require_int_metadata(data.get("schema_version", 1), "schema_version")
        if schema_version > RECIPE_SCHEMA_VERSION:
            raise RecipeBookCorruptedError(
                f"{RECIPE_BOOK_PATH}: unbekannte zukünftige schema_version "
                f"{schema_version} (unterstützt wird höchstens {RECIPE_SCHEMA_VERSION}). "
                "Die Datei wurde NICHT automatisch überschrieben."
            )
        if schema_version < RECIPE_SCHEMA_VERSION:
            data["schema_version"] = RECIPE_SCHEMA_VERSION
            changed = True

        existing_by_slot: dict[int, dict[str, Any]] = {}
        for recipe in data["favorites"]:
            if not isinstance(recipe, dict):
                raise RecipeBookCorruptedError(
                    f"{RECIPE_BOOK_PATH}: ein Favorit ist kein JSON-Objekt "
                    f"(war: {recipe!r}). Die Datei wurde NICHT automatisch überschrieben."
                )

            # Every favorite is migrated independently based on ITS OWN
            # schema_version (see migrate_favorite_dict docstring) - this
            # runs regardless of whether the book-level schema_version above
            # was already current, so a favorite left behind at an older
            # version (e.g. after a hand edit) is still brought up to date.
            migrated_recipe = migrate_favorite_dict(recipe)
            if migrated_recipe != recipe:
                changed = True
            recipe = migrated_recipe

            slot_value = _require_int_metadata(recipe.get("slot", 0), "favorite.slot")
            if slot_value not in FAVORITE_SLOTS:
                raise RecipeBookCorruptedError(
                    f"{RECIPE_BOOK_PATH}: 'favorite.slot' liegt außerhalb von "
                    f"{FAVORITE_SLOTS} (war: {slot_value!r}). Die Datei wurde NICHT "
                    "automatisch überschrieben."
                )
            if slot_value in existing_by_slot:
                raise RecipeBookCorruptedError(
                    f"{RECIPE_BOOK_PATH}: Slot {slot_value} ist mehrfach vorhanden. "
                    "Die Datei wurde NICHT automatisch überschrieben."
                )
            existing_by_slot[slot_value] = recipe

        # A file that already exists must contain exactly Slot 1/2/3 - a
        # missing slot is no longer silently filled with a default (that
        # self-healing could mask a genuinely lost/corrupted favorite).
        # default_recipe_book() with three defaults is only ever used by
        # ensure_recipe_book() for a file that does not exist yet at all.
        missing_slots = [slot for slot in FAVORITE_SLOTS if slot not in existing_by_slot]
        if missing_slots:
            raise RecipeBookCorruptedError(
                f"{RECIPE_BOOK_PATH}: Favoritenslot(s) {missing_slots} fehlen. Die Datei "
                f"muss genau einmal jeden der Slots {FAVORITE_SLOTS} enthalten. Die Datei "
                "wurde NICHT automatisch überschrieben."
            )

        data["favorites"] = [existing_by_slot[slot] for slot in FAVORITE_SLOTS]

        active_slot = _require_int_metadata(data.get("active_slot", 1), "active_slot")
        if active_slot not in FAVORITE_SLOTS:
            raise RecipeBookCorruptedError(
                f"{RECIPE_BOOK_PATH}: 'active_slot' liegt außerhalb von "
                f"{FAVORITE_SLOTS} (war: {active_slot!r}). Die Datei wurde NICHT "
                "automatisch überschrieben."
            )

        if changed:
            _save_recipe_book_unlocked(data)

        return data


def save_recipe_book(data: dict[str, Any]) -> None:
    """Public, locked entry point - see _save_recipe_book_unlocked() for the
    actual atomic-write mechanics."""
    with _SAVE_LOCK:
        _save_recipe_book_unlocked(data)


def _save_recipe_book_unlocked(data: dict[str, Any]) -> None:
    """
    Atomic write, assumes the caller already holds _SAVE_LOCK (called from
    save_recipe_book() and from within the locked sequences in
    load_recipe_book()/save_recipe_to_slot()/set_active_slot() - never call
    this directly without holding the lock). Validation has already happened
    by the time this is called (save_recipe_to_slot validates before
    invoking this). This function only has to get the bytes onto disk
    safely:

    1. serialize fully in memory first (a bad value here fails before any
       file I/O happens, the existing file is never touched) - allow_nan=False
       is a defense-in-depth backstop, real recipes should never reach here
       with a NaN/Inf since StoredRecipe.validate() rejects those first,
    2. write to a temp file in the same directory, flush + fsync,
    3. best-effort copy the previous file to a .bak backup,
    4. atomically replace the real file with Path.replace().
    """
    RECIPE_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)

    data = dict(data)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(RECIPE_BOOK_PATH.parent),
        prefix=f".{RECIPE_BOOK_PATH.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())

        # mkstemp() creates the file with mode 0600 (its security
        # default) - match the other config/*.json files' 0644 instead
        # of silently narrowing permissions on every save.
        os.chmod(tmp_path, 0o644)

        if RECIPE_BOOK_PATH.exists():
            backup_path = RECIPE_BOOK_PATH.with_suffix(RECIPE_BOOK_PATH.suffix + ".bak")
            try:
                shutil.copy2(RECIPE_BOOK_PATH, backup_path)
            except OSError:
                pass  # backup is best-effort, must not block a valid save

        tmp_path.replace(RECIPE_BOOK_PATH)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
