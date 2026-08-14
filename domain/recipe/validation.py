"""Recipe field-name tables and business-rule validation."""

from __future__ import annotations

from typing import Any

from .calculations import compute_base_volume_before_ro_correction_l, compute_total_nutrients_ml, round_l
from .field_validators import validate_bool_field, validate_numeric_field, validate_strict_int_field
from .limits import (
    EC_ADJUSTMENT_FACTOR_ENABLED_MIN,
    EC_ADJUSTMENT_FACTOR_MAX,
    EC_ADJUSTMENT_FACTOR_MIN,
    EC_MIXING_TIME_MAX_SECONDS,
    EC_MIXING_TIME_MIN_SECONDS,
    EC_SETPOINT_MAX_MS_CM,
    EC_SETPOINT_MIN_MS_CM,
    MAX_PROCESS_VOLUME_L,
    MAX_RECIPE_RO_WATER_L,
    NUTRIENT_A_PERCENT_MAX,
    NUTRIENT_A_PERCENT_MIN,
)

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
# tolerance (see field_validators.py::validate_strict_int_field above) -
# never user-typed values, always produced by the code itself.
RECIPE_STRICT_INT_FIELDS: tuple[str, ...] = ("schema_version", "slot")

# Every boolean field of StoredRecipe. sensor_circulation_enabled and
# drain_after_process are NOT here - they moved to RunOptions (see
# domain/recipe/models.py::RunOptions.validate(), same strict-bool rule
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
