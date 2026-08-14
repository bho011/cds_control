"""Slot-CRUD: einzelne Rezepte lesen, speichern, aktivieren."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from domain.recipe_limits import RecipeValidationError
from domain.recipe_model import RECIPE_SCHEMA_VERSION, StoredRecipe

from . import book_io
from .book_io import (
    _SAVE_LOCK,
    FAVORITE_SLOTS,
    _default_recipe,
    _require_int_metadata,
    load_recipe_book,
    save_recipe_book,
)

# book_io.RECIPE_BOOK_PATH is deliberately read through the module (not
# imported as a bare name) - tests monkeypatch RECIPE_BOOK_PATH on the
# book_io module itself, and a captured "from .book_io import RECIPE_BOOK_PATH"
# would silently stop observing that patch (a real incident during this
# refactor: a stale bare-name copy caused a test to write to the real
# recipes/dashboard_recipes.json instead of a tmp path - see plan Phase 7a).


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
    # the other's change. book_io._SAVE_LOCK is an RLock (held internally by
    # load_recipe_book()/save_recipe_book()), so those nested calls below do
    # not deadlock.
    with _SAVE_LOCK:
        # Merging in this slot's existing legacy audit fields needs to read
        # the current book, but calling load_recipe_book() unconditionally
        # here would itself create a fresh default file as a side effect
        # (via ensure_recipe_book()) even if this save later fails
        # validation - only read it if a book genuinely already exists.
        recipe_book = load_recipe_book() if book_io.RECIPE_BOOK_PATH.exists() else None

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
