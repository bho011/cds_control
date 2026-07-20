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

from domain.recipe_limits import RecipeValidationError
from domain.recipe_model import (
    RECIPE_SCHEMA_VERSION,
    RecipePreview,
    RunConfigSnapshot,
    RunOptions,
    StoredRecipe,
)

__all__ = [
    "RecipeBookCorruptedError",
    "RecipeValidationError",
    "RECIPE_BOOK_PATH",
    "FAVORITE_SLOTS",
    "DEFAULT_RECIPE",
    "default_recipe_book",
    "ensure_recipe_book",
    "load_recipe_book",
    "save_recipe_book",
    "get_recipe_by_slot",
    "get_active_recipe",
    "save_recipe_to_slot",
    "set_active_slot",
    "get_recipe_preview",
    "build_run_config",
]


class RecipeBookCorruptedError(Exception):
    """Raised when recipes/dashboard_recipes.json exists but cannot be
    parsed, or contains metadata (schema_version/active_slot/favorite.slot)
    of the wrong type. Deliberately NOT auto-repaired: overwriting a broken
    recipe file with defaults could silently discard real, hand-tuned
    recipe data."""


RECIPE_BOOK_PATH = Path("recipes/dashboard_recipes.json")
FAVORITE_SLOTS = (1, 2, 3)

# RLock (not Lock): save_recipe_to_slot()/set_active_slot() hold this lock
# across their whole load-modify-save sequence (closing the read-modify-
# write race window), and internally call load_recipe_book()/
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
            f"{RECIPE_BOOK_PATH}: '{field_name}' muss ein Objekt (dict) oder nicht "
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
            f"{RECIPE_BOOK_PATH}: unbekannte zukünftige favorite.schema_version "
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


def _require_valid_slot(slot: Any) -> int:
    """Strict slot check for the public slot-taking functions below: only a
    genuine Python int in FAVORITE_SLOTS is accepted - no int(slot)
    normalization, so a float (1.0), a bool, or a numeric string ("1") is
    rejected outright rather than silently coerced."""
    if type(slot) is not int or slot not in FAVORITE_SLOTS:
        raise ValueError(f"Invalid recipe slot: {slot!r}")
    return slot


def get_recipe_by_slot(recipe_book: dict[str, Any], slot: int) -> dict[str, Any]:
    slot = _require_valid_slot(slot)
    for recipe in recipe_book.get("favorites", []):
        if _require_int_metadata(recipe.get("slot", 0), "favorite.slot") == slot:
            return recipe

    return _default_recipe(slot)


def get_active_recipe(recipe_book: dict[str, Any]) -> dict[str, Any]:
    active_slot = _require_int_metadata(recipe_book.get("active_slot", 1), "active_slot")
    return get_recipe_by_slot(recipe_book, active_slot)


# Audit-only fields that the Recipe Editor never shows/edits (see
# domain/recipe_model.py::StoredRecipe field docstrings) - save_recipe_to_slot()
# inherits these from the slot's existing recipe whenever the caller's dict
# does not explicitly supply them, so saving a recipe from a form that has
# no notion of these fields at all can never silently wipe them.
_LEGACY_AUDIT_FIELDS: tuple[str, ...] = (
    "legacy_volume_stock_1_ml",
    "legacy_volume_stock_2_ml",
    "legacy_process_values",
    "legacy_recipe_values",
)


def save_recipe_to_slot(slot: int, recipe: dict[str, Any], make_active: bool = True) -> dict[str, Any]:
    slot = _require_valid_slot(slot)

    recipe = dict(recipe)
    recipe["slot"] = slot
    recipe["schema_version"] = RECIPE_SCHEMA_VERSION
    recipe["updated_at"] = datetime.now().isoformat(timespec="seconds")

    # The whole load-merge-validate-save sequence is one critical section:
    # without this, two concurrent save_recipe_to_slot() calls for different
    # slots could both load the same pre-change book, each apply only their
    # own slot's change, and whichever writes last would silently discard
    # the other's change. _SAVE_LOCK is an RLock, so the nested
    # load_recipe_book()/save_recipe_book() calls below (which also take
    # this lock) do not deadlock.
    with _SAVE_LOCK:
        # Merging in this slot's existing legacy audit fields needs to read
        # the current book, but calling load_recipe_book() unconditionally
        # here would itself create a fresh default file as a side effect
        # (via ensure_recipe_book()) even if this save later fails
        # validation - only read it if a book genuinely already exists.
        recipe_book = load_recipe_book() if RECIPE_BOOK_PATH.exists() else None

        existing_recipe: dict[str, Any] | None = None
        if recipe_book is not None:
            for candidate in recipe_book.get("favorites", []):
                if _require_int_metadata(candidate.get("slot", 0), "favorite.slot") == slot:
                    existing_recipe = candidate
                    break

        # Merge-safe: a legacy audit field is only ever inherited from the
        # slot's previous value when the incoming dict does not explicitly
        # contain that key at all - never overwritten if genuinely present
        # (even if explicitly None/empty).
        if existing_recipe is not None:
            for field_name in _LEGACY_AUDIT_FIELDS:
                if field_name not in recipe:
                    recipe[field_name] = existing_recipe.get(field_name)

        stored_recipe = StoredRecipe.from_dict(recipe)
        errors = stored_recipe.validate()
        if errors:
            formatted = "\n".join(f"  - {message}" for message in errors)
            raise RecipeValidationError(
                f"Rezept '{recipe.get('recipe_name', '?')}' (Slot {slot}) kann nicht gespeichert "
                f"werden, {len(errors)} Fehler:\n{formatted}"
            )

        # Only the canonical StoredRecipe representation is ever persisted,
        # never the raw incoming dict - StoredRecipe.from_dict() already
        # drops any unknown/removed field (e.g. a reintroduced addon_1_ml),
        # and .to_dict() reflects every __post_init__ normalization (e.g.
        # a whole-number float ec_mixing_time_seconds coerced to a real int)
        # exactly as it will be validated and used from now on.
        recipe = stored_recipe.to_dict()

        if recipe_book is None:
            recipe_book = load_recipe_book()

        favorites = []
        replaced = False

        for existing in recipe_book.get("favorites", []):
            if _require_int_metadata(existing.get("slot", 0), "favorite.slot") == slot:
                favorites.append(recipe)
                replaced = True
            else:
                favorites.append(existing)

        if not replaced:
            favorites.append(recipe)

        favorites = sorted(
            favorites, key=lambda item: _require_int_metadata(item.get("slot", 0), "favorite.slot")
        )[:3]
        recipe_book["favorites"] = favorites

        if make_active:
            recipe_book["active_slot"] = slot

        save_recipe_book(recipe_book)
        return recipe_book


def set_active_slot(slot: int) -> dict[str, Any]:
    slot = _require_valid_slot(slot)

    with _SAVE_LOCK:
        recipe_book = load_recipe_book()
        recipe_book["active_slot"] = slot
        save_recipe_book(recipe_book)
        return recipe_book


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
