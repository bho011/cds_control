"""Compatibility shim: domain/recipe_model.py moved into domain/recipe/.

# Re-exports: hält "from domain.recipe_model import Y" nach dem
# Package-Split funktionsfähig. Der eigentliche Inhalt lebt jetzt in
# domain/recipe/models.py (siehe Modularisierungs-Plan, Phase 1).
"""

from __future__ import annotations

from domain.recipe.limits import RecipeValidationError
from domain.recipe.models import RECIPE_SCHEMA_VERSION, RecipePreview, RunConfigSnapshot, RunOptions, StoredRecipe

# RecipeValidationError is re-exported here too: the original recipe_model.py
# imported it from recipe_limits.py at module level, which made it
# importable from either module - tests/test_recipe_domain.py relies on
# `from domain.recipe_model import RecipeValidationError` specifically.
__all__ = [
    "RECIPE_SCHEMA_VERSION",
    "StoredRecipe",
    "RecipePreview",
    "RunOptions",
    "RunConfigSnapshot",
    "RecipeValidationError",
]
