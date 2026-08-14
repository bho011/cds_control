"""Rezeptbuch-Speicher: Package-Re-Exports für "from nicegui_dashboard.recipe_store import Y".

# Re-exports: hält "from nicegui_dashboard.recipe_store import Y" nach dem
# Package-Split funktionsfähig (siehe Modularisierungs-Plan, Phase 7a).
"""

from __future__ import annotations

from domain.recipe_limits import RecipeValidationError
from domain.recipe_model import RECIPE_SCHEMA_VERSION

from .book_io import (
    DEFAULT_RECIPE,
    FAVORITE_SLOTS,
    RECIPE_BOOK_PATH,
    RecipeBookCorruptedError,
    default_recipe_book,
    ensure_recipe_book,
    load_recipe_book,
    save_recipe_book,
)
from .migrations import _migrate_v1_to_v2, _migrate_v2_to_v3, _migrate_v3_to_v4, migrate_favorite_dict
from .queries import get_active_recipe, get_recipe_by_slot, save_recipe_to_slot, set_active_slot
from .run_config import build_run_config, get_recipe_preview

__all__ = [
    "RecipeBookCorruptedError",
    "RecipeValidationError",
    "RECIPE_BOOK_PATH",
    "RECIPE_SCHEMA_VERSION",
    "FAVORITE_SLOTS",
    "DEFAULT_RECIPE",
    "default_recipe_book",
    "ensure_recipe_book",
    "load_recipe_book",
    "save_recipe_book",
    "migrate_favorite_dict",
    "get_recipe_by_slot",
    "get_active_recipe",
    "save_recipe_to_slot",
    "set_active_slot",
    "get_recipe_preview",
    "build_run_config",
]
