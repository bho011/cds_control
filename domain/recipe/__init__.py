"""Recipe domain model package - re-exports the public API of its submodules.

# Re-exports: hält "from X import Y" nach dem Package-Split funktionsfähig,
# hier zusätzlich als bequemer Einstiegspunkt "from domain.recipe import Y".
"""

from __future__ import annotations

from .calculations import (
    compute_available_process_capacity_l,
    compute_base_volume_before_ro_correction_l,
    compute_max_possible_ro_correction_l,
    compute_nutrient_split,
    compute_total_nutrients_ml,
    nutrient_b_percent,
    round_l,
    round_ml,
)
from .field_validators import validate_bool_field, validate_numeric_field, validate_strict_int_field
from .limits import (
    EC_ADJUSTMENT_FACTOR_ENABLED_MIN,
    EC_ADJUSTMENT_FACTOR_MAX,
    EC_ADJUSTMENT_FACTOR_MIN,
    EC_MIXING_TIME_MAX_SECONDS,
    EC_MIXING_TIME_MIN_SECONDS,
    EC_SETPOINT_MAX_MS_CM,
    EC_SETPOINT_MIN_MS_CM,
    LEGACY_MAX_RO_CORRECTION_L,
    L_ROUNDING_DECIMALS,
    MAX_PROCESS_VOLUME_L,
    MAX_RECIPE_RO_WATER_L,
    ML_ROUNDING_DECIMALS,
    NUTRIENT_A_PERCENT_MAX,
    NUTRIENT_A_PERCENT_MIN,
    TANK_PHYSICAL_CAPACITY_L,
    RecipeValidationError,
)
from .models import RECIPE_SCHEMA_VERSION, RecipePreview, RunConfigSnapshot, RunOptions, StoredRecipe
from .validation import (
    RECIPE_BOOLEAN_FIELDS,
    RECIPE_NUMERIC_FIELDS,
    RECIPE_STRICT_INT_FIELDS,
    validate_recipe_field_types,
    validate_recipe_values,
)

__all__ = [
    "RecipeValidationError",
    "TANK_PHYSICAL_CAPACITY_L",
    "MAX_RECIPE_RO_WATER_L",
    "MAX_PROCESS_VOLUME_L",
    "LEGACY_MAX_RO_CORRECTION_L",
    "EC_ADJUSTMENT_FACTOR_MIN",
    "EC_ADJUSTMENT_FACTOR_MAX",
    "NUTRIENT_A_PERCENT_MIN",
    "NUTRIENT_A_PERCENT_MAX",
    "EC_SETPOINT_MIN_MS_CM",
    "EC_SETPOINT_MAX_MS_CM",
    "EC_MIXING_TIME_MIN_SECONDS",
    "EC_MIXING_TIME_MAX_SECONDS",
    "EC_ADJUSTMENT_FACTOR_ENABLED_MIN",
    "ML_ROUNDING_DECIMALS",
    "L_ROUNDING_DECIMALS",
    "round_ml",
    "round_l",
    "nutrient_b_percent",
    "compute_total_nutrients_ml",
    "compute_nutrient_split",
    "compute_base_volume_before_ro_correction_l",
    "compute_available_process_capacity_l",
    "compute_max_possible_ro_correction_l",
    "validate_numeric_field",
    "validate_bool_field",
    "validate_strict_int_field",
    "RECIPE_NUMERIC_FIELDS",
    "RECIPE_STRICT_INT_FIELDS",
    "RECIPE_BOOLEAN_FIELDS",
    "validate_recipe_field_types",
    "validate_recipe_values",
    "RECIPE_SCHEMA_VERSION",
    "StoredRecipe",
    "RecipePreview",
    "RunOptions",
    "RunConfigSnapshot",
]
