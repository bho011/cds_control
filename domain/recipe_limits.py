"""
Single source of truth for recipe/tank-volume/EC-nutrient business rules.

Referenced from nicegui_dashboard/recipe_store.py, nicegui_dashboard/pages/dashboard_page.py
and nicegui_dashboard/process_controller.py so the constants below are never
duplicated (and cannot drift) across the GUI, the recipe store, and the
process controller.

Values below were confirmed against the legacy Node-RED system
(~/.node-red/projects/Central_Dosing_System/flows.json, "EC Configuration"
function node) where noted - everything else is a fresh decision for this
project, documented in docs/RECIPE_AND_DOSING_RULES.md.
"""

from __future__ import annotations

import math
from typing import Any


class RecipeValidationError(ValueError):
    """Raised with every problem found in a recipe, not just the first."""


# --- Tank / process volume limits -------------------------------------------------
#
# 200 l is only the approximate physical tank capacity up to the rim - it must
# never be used as an allowed recipe/process quantity. 180 l is the maximum
# allowed amount of pure RO water in a recipe. Nutrient solutions and addons
# come on top of the RO water. The estimated/measured total process volume
# must never exceed 185 l.
TANK_PHYSICAL_CAPACITY_L = 200.0
MAX_RECIPE_RO_WATER_L = 180.0
MAX_PROCESS_VOLUME_L = 185.0

# Legacy Node-RED "EC_config.maxvol3" (RO/dilution correction) - a static cap
# on any *future* runtime RO correction (not a stored recipe value - see
# compute_max_possible_ro_correction_l below and docs/RECIPE_AND_DOSING_RULES.md
# Section 4; the actual demand-driven EC correction is not implemented yet).
LEGACY_MAX_RO_CORRECTION_L = 50.0

# Legacy Node-RED "EC_config.maxfactor" - confirmed upper bound for the EC
# adjustment factor.
EC_ADJUSTMENT_FACTOR_MIN = 0.0
EC_ADJUSTMENT_FACTOR_MAX = 1.0

NUTRIENT_A_PERCENT_MIN = 0.0
NUTRIENT_A_PERCENT_MAX = 100.0

# Confirmed against the legacy Node-RED EC_config/addon_config (see
# docs/OPEN_RECIPE_DECISIONS.md). "Zero or in-range" fields: not dosing at
# all (0) is always allowed, but a nonzero dose must be a real, physically
# sensible amount - a dribble of 0.3 ml is almost certainly a typo, not an
# intentional micro-dose.
EC_SETPOINT_MIN_MS_CM = 0.5
EC_SETPOINT_MAX_MS_CM = 2.5
EC_MIXING_TIME_MIN_SECONDS = 300
EC_MIXING_TIME_MAX_SECONDS = 1200
EC_ADJUSTMENT_FACTOR_ENABLED_MIN = 0.01

# Central rounding strategy for millilitre and litre quantities, applied
# consistently everywhere a value is calculated (not to raw user input).
ML_ROUNDING_DECIMALS = 3
L_ROUNDING_DECIMALS = 3


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


def validate_numeric_field(name: str, value: Any, expected_type: type) -> str | None:
    """
    Rejects everything that is not a genuine, finite number of the expected
    type - returns a clear message instead of letting a bad value reach
    arithmetic further down and raise a raw TypeError/blow up json.dumps.

    bool is deliberately rejected even for expected_type=int:
    isinstance(True, int) is True in Python, so a naive isinstance() check
    would silently accept a checkbox-shaped value as a valid number.

    expected_type=int additionally rejects a non-integral float (e.g. 300.5
    for a mixing-time-in-seconds field) - a whole-number float (300.0) is
    accepted, since JSON does not distinguish "300" from "300.0".
    """
    if isinstance(value, bool):
        return f"'{name}' darf kein Wahrheitswert (true/false) sein (war: {value!r})."

    if not isinstance(value, (int, float)):
        return f"'{name}' muss eine Zahl sein (war: {value!r}, Typ {type(value).__name__})."

    if not math.isfinite(value):
        return f"'{name}' muss eine endliche Zahl sein (war: {value!r})."

    if expected_type is int and isinstance(value, float) and not value.is_integer():
        return f"'{name}' muss eine Ganzzahl sein, keine Kommazahl (war: {value!r})."

    return None


def validate_bool_field(name: str, value: Any) -> str | None:
    """
    Strict boolean check: type(value) is bool, not isinstance() - Python's
    bool is a subtype of int, and isinstance(1, bool) is False but
    isinstance(True, int) is True, so isinstance() alone is asymmetric and
    error-prone here. Never normalizes with bool(value) - a string "false",
    a nonzero number, None, a list, or a dict must all be rejected outright,
    never silently coerced to a truthy/falsy Python bool.
    """
    if type(value) is bool:
        return None

    return f"'{name}' muss ein Wahrheitswert (true/false) sein (war: {value!r}, Typ {type(value).__name__})."


def validate_strict_int_field(name: str, value: Any) -> str | None:
    """
    Strict integer check for structural identifiers (schema_version, slot):
    type(value) is int only, zero float tolerance. Unlike
    validate_numeric_field(..., int), a whole-number float such as 1.0 or
    3.0 is deliberately REJECTED here, not accepted - these two fields are
    never meant to be user-edited/typed-in numbers, only real Python ints
    produced by the code itself. bool is rejected too (bool is a subtype of
    int in Python), and so is any non-numeric type.
    """
    if type(value) is int:
        return None

    return f"'{name}' muss eine echte Ganzzahl sein, ohne Kommastelle (war: {value!r}, Typ {type(value).__name__})."


# Every numeric field of domain.recipe_model.StoredRecipe that is editable
# via the recipe editor. legacy_volume_stock_1_ml/legacy_volume_stock_2_ml
# are intentionally excluded here - they are Optional[float] legacy display
# data (see docs/OPEN_RECIPE_DECISIONS.md), checked separately in
# validate_recipe_field_types() only when not None.
RECIPE_NUMERIC_FIELDS: tuple[tuple[str, type], ...] = (
    ("target_ro_water_l", float),
    ("target_ec_ms_cm", float),
    ("ec_mixing_time_seconds", int),
    ("nutrients_ml_per_100_l", float),
    ("nutrient_a_percent", float),
    ("ec_adjustment_factor", float),
    ("target_ph", float),
    ("volume_acid_1_ml", float),
    ("volume_acid_2_ml", float),
    ("volume_base_ml", float),
    ("ph_mixing_time_seconds", int),
    ("ph_adjustment_factor", float),
)

# Structural identifiers that must be a genuine Python int, with zero float
# tolerance (see validate_strict_int_field above) - never user-typed values,
# always produced by the code itself.
RECIPE_STRICT_INT_FIELDS: tuple[str, ...] = ("schema_version", "slot")

# Every boolean field of StoredRecipe. sensor_circulation_enabled and
# drain_after_process are NOT here - they moved to RunOptions (see
# domain/recipe_model.py::RunOptions.validate(), same strict-bool rule
# applies there).
RECIPE_BOOLEAN_FIELDS: tuple[str, ...] = (
    "nutrient_dosing_enabled",
    "legacy_dosing_needs_review",
)


def validate_recipe_field_types(recipe: Any) -> list[str]:
    """Type/finiteness pass over every numeric and boolean StoredRecipe
    field, run before any business-rule arithmetic (see
    validate_recipe_values) - a string/None/NaN/"false" must produce a
    RecipeValidationError, not a TypeError deep inside a comparison or a
    subsequent json.dumps(allow_nan=False)."""
    errors: list[str] = []

    for name in RECIPE_STRICT_INT_FIELDS:
        value = getattr(recipe, name)
        message = validate_strict_int_field(name, value)
        if message:
            errors.append(message)

    for name, expected_type in RECIPE_NUMERIC_FIELDS:
        value = getattr(recipe, name)
        message = validate_numeric_field(name, value, expected_type)
        if message:
            errors.append(message)

    for name in RECIPE_BOOLEAN_FIELDS:
        value = getattr(recipe, name)
        message = validate_bool_field(name, value)
        if message:
            errors.append(message)

    for name in ("legacy_volume_stock_1_ml", "legacy_volume_stock_2_ml"):
        value = getattr(recipe, name, None)
        if value is not None:
            message = validate_numeric_field(name, value, float)
            if message:
                errors.append(message)

    for name in ("legacy_process_values", "legacy_recipe_values"):
        value = getattr(recipe, name, None)
        if value is not None and not isinstance(value, dict):
            errors.append(
                f"'{name}' muss ein Objekt (dict) oder nicht gesetzt sein "
                f"(war: {value!r}, Typ {type(value).__name__})."
            )

    return errors


def validate_recipe_values(
    *,
    target_ro_water_l: float,
    target_ec_ms_cm: float,
    ec_mixing_time_seconds: float,
    nutrients_ml_per_100_l: float,
    nutrient_a_percent: float,
    ec_adjustment_factor: float,
    nutrient_dosing_enabled: bool,
    legacy_dosing_needs_review: bool,
) -> list[str]:
    """
    Pure validation, no side effects. Returns every problem found (not just
    the first) so a recipe with several issues can be fixed in one pass -
    same style as services/settings_validation.py::validate_settings.

    Deliberately does NOT clamp any value to a limit - an invalid recipe must
    be rejected with a clear message, never silently corrected and saved.

    Callers must run validate_recipe_field_types() first and only call this
    function if that returned no errors - the comparisons/arithmetic below
    assume every value is already a finite int/float or a genuine bool.
    """
    errors: list[str] = []

    if target_ro_water_l <= 0:
        errors.append("RO-Wassermenge muss größer als 0 l sein.")
    elif target_ro_water_l > MAX_RECIPE_RO_WATER_L:
        errors.append(
            f"RO-Wassermenge darf maximal {MAX_RECIPE_RO_WATER_L} l betragen "
            f"(war: {target_ro_water_l} l)."
        )

    if not (EC_SETPOINT_MIN_MS_CM <= target_ec_ms_cm <= EC_SETPOINT_MAX_MS_CM):
        errors.append(
            f"EC-Sollwert muss zwischen {EC_SETPOINT_MIN_MS_CM} und {EC_SETPOINT_MAX_MS_CM} "
            f"mS/cm liegen (war: {target_ec_ms_cm} mS/cm). Bestätigt durch die alte "
            "Node-RED-Konfiguration (EC_config)."
        )

    if not (EC_MIXING_TIME_MIN_SECONDS <= ec_mixing_time_seconds <= EC_MIXING_TIME_MAX_SECONDS):
        errors.append(
            f"EC-Mischzeit muss zwischen {EC_MIXING_TIME_MIN_SECONDS} und "
            f"{EC_MIXING_TIME_MAX_SECONDS} s liegen (war: {ec_mixing_time_seconds} s). "
            "Bestätigt durch die alte Node-RED-Konfiguration (EC_config, dort in Minuten)."
        )

    if not (NUTRIENT_A_PERCENT_MIN <= nutrient_a_percent <= NUTRIENT_A_PERCENT_MAX):
        errors.append(
            f"Anteil Nährstofflösung A muss zwischen {NUTRIENT_A_PERCENT_MIN} und "
            f"{NUTRIENT_A_PERCENT_MAX} % liegen (war: {nutrient_a_percent} %)."
        )

    if nutrients_ml_per_100_l < 0:
        errors.append(
            f"Nährstoff-Gesamtdosis darf nicht negativ sein (war: {nutrients_ml_per_100_l} ml/100l)."
        )

    # Unconditional, regardless of nutrient_dosing_enabled: the factor is a
    # stored recipe value and must always be a physically sane number, even
    # while dosing is off and the value is otherwise inert.
    if not (EC_ADJUSTMENT_FACTOR_MIN <= ec_adjustment_factor <= EC_ADJUSTMENT_FACTOR_MAX):
        errors.append(
            f"EC-Anpassungsfaktor muss zwischen {EC_ADJUSTMENT_FACTOR_MIN} und "
            f"{EC_ADJUSTMENT_FACTOR_MAX} liegen (war: {ec_adjustment_factor}). Bestätigt durch "
            "die alte Node-RED-Konfiguration (EC_config.maxfactor)."
        )
    elif nutrient_dosing_enabled and ec_adjustment_factor < EC_ADJUSTMENT_FACTOR_ENABLED_MIN:
        # Additional, stricter minimum only while dosing is actually enabled.
        errors.append(
            f"EC-Anpassungsfaktor muss bei aktivierter Nährstoffdosierung mindestens "
            f"{EC_ADJUSTMENT_FACTOR_ENABLED_MIN} betragen (war: {ec_adjustment_factor})."
        )

    if legacy_dosing_needs_review and nutrient_dosing_enabled:
        errors.append(
            "Nährstoffdosierung kann nicht aktiviert werden, solange die Legacy-Prüfung "
            "dieses Rezepts nicht abgeschlossen ist (siehe 'Legacy review completed' "
            "im Recipe Editor)."
        )

    # Only computed if the inputs above are individually sane - an
    # out-of-range percentage or negative dose would otherwise produce a
    # confusing, secondary "process volume" error on top of the real one.
    if not errors:
        # Mirrors RecipePreview.from_recipe()/RunConfigSnapshot exactly: while
        # dosing is disabled nothing will actually be dosed, so a large
        # stored-but-inert nutrients_ml_per_100_l value must not count
        # towards the estimated process volume at all - a recipe with dosing
        # off must never be rejected for a dose that will never happen.
        if nutrient_dosing_enabled:
            total_nutrients_ml = compute_total_nutrients_ml(
                target_ro_water_l, nutrients_ml_per_100_l, ec_adjustment_factor
            )
        else:
            total_nutrients_ml = 0.0
        estimated_volume_l = compute_base_volume_before_ro_correction_l(
            target_ro_water_l, total_nutrients_ml
        )
        if estimated_volume_l > MAX_PROCESS_VOLUME_L:
            errors.append(
                f"Das geschätzte Rezeptvolumen von {round_l(estimated_volume_l)} l "
                f"überschreitet das Limit von {MAX_PROCESS_VOLUME_L} l."
            )

    return errors
