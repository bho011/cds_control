"""Pure calculation helpers for recipe/EC-nutrient values, no validation."""

from __future__ import annotations

from .limits import LEGACY_MAX_RO_CORRECTION_L, L_ROUNDING_DECIMALS, MAX_PROCESS_VOLUME_L, ML_ROUNDING_DECIMALS


def round_ml(value: float) -> float:
    return round(float(value), ML_ROUNDING_DECIMALS)


def round_l(value: float) -> float:
    return round(float(value), L_ROUNDING_DECIMALS)


def nutrient_b_percent(nutrient_a_percent: float) -> float:
    """B is always the remainder of A, never an independently stored value -
    this is what guarantees A + B == 100 by construction."""
    return round(100.0 - float(nutrient_a_percent), 6)


def compute_total_nutrients_ml(
    target_ro_water_l: float,
    nutrients_ml_per_100_l: float,
    ec_adjustment_factor: float,
) -> float:
    raw = (
        float(target_ro_water_l)
        / 100.0
        * float(nutrients_ml_per_100_l)
        * float(ec_adjustment_factor)
    )
    return round_ml(raw)


def compute_nutrient_split(
    total_nutrients_ml: float,
    nutrient_a_percent: float,
) -> tuple[float, float]:
    """B is calculated as the remainder of the (already rounded) total, not
    independently - so A + B always reproduces total_nutrients_ml exactly,
    even after rounding."""
    nutrient_a_ml = round_ml(float(total_nutrients_ml) * float(nutrient_a_percent) / 100.0)
    nutrient_b_ml = round_ml(float(total_nutrients_ml) - nutrient_a_ml)
    return nutrient_a_ml, nutrient_b_ml


def compute_base_volume_before_ro_correction_l(
    target_ro_water_l: float,
    total_nutrients_ml: float,
) -> float:
    """The recipe's whole estimated volume: RO water + calculated nutrients.
    Addon 1/Addon 2 are no longer part of the active recipe model (the real
    CDS process only has Nutrient Solution A/B - see
    docs/RECIPE_AND_DOSING_RULES.md). A fixed RO correction is also no
    longer part of a recipe (see docs/RECIPE_AND_DOSING_RULES.md Section 4)
    - this IS "the estimated recipe volume" now, not just an intermediate
    baseline."""
    return float(target_ro_water_l) + float(total_nutrients_ml) / 1000.0


def compute_available_process_capacity_l(current_or_estimated_volume_l: float) -> float:
    return round_l(max(0.0, MAX_PROCESS_VOLUME_L - float(current_or_estimated_volume_l)))


def compute_max_possible_ro_correction_l(current_or_estimated_volume_l: float) -> float:
    """The technically possible RO correction *some later run* could still
    apply, given the recipe's own estimated volume - purely informational
    (see docs/RECIPE_AND_DOSING_RULES.md Section 4). Not a stored recipe
    value and not part of RunConfigSnapshot: no fixed correction amount is
    requested or applied by this system today, a real demand-driven EC
    correction is a future feature."""
    available = compute_available_process_capacity_l(current_or_estimated_volume_l)
    return round_l(min(LEGACY_MAX_RO_CORRECTION_L, available))
