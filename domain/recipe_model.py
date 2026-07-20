"""
Typed recipe domain model, built on top of domain/recipe_limits.py.

Four distinct types:

1. StoredRecipe    - the editable, persisted recipe (plain mutable dataclass,
                      mirrors what recipe_store.py reads/writes as JSON).
                      Contains only fachliche Rezeptwerte - recipe name/tank,
                      RO water amount, EC/pH setpoints and recipe-specific
                      mixing times, nutrient configuration (Nutrient
                      Solution A/B), notes, and legacy status. It does NOT
                      contain any
                      per-run/technical choice (sensor circulation, drain,
                      sensor pump timing) - those live in RunOptions/
                      ProcessSettings instead, so loading a recipe can never
                      by itself activate hardware.
2. RecipePreview   - calculated preview values for the GUI. Always
                      recomputable from a StoredRecipe, never itself stored.
                      Deliberately does not raise on out-of-range input - the
                      GUI needs live numbers (including "too high") to show a
                      concrete error message; only building a RunConfigSnapshot
                      is a hard gate.
3. RunOptions      - typed, per-run-only choices (sensor circulation, drain
                      after process). Never persisted in a recipe, always
                      freshly constructed (defaulting to the safest/most
                      inactive state) at the start of every run - see
                      docs/RECIPE_AND_DOSING_RULES.md Section 5.
4. RunConfigSnapshot - frozen (immutable) snapshot built exclusively from a
                      *validated* StoredRecipe + the active ProcessSettings +
                      a RunOptions instance, at process-start time. Raises
                      RecipeValidationError instead of being constructed from
                      an invalid recipe (fail-closed). Nothing downstream may
                      read an effective value from the UI or StoredRecipe
                      again once this snapshot exists.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any

from domain.recipe_limits import (
    MAX_PROCESS_VOLUME_L,
    RecipeValidationError,
    compute_base_volume_before_ro_correction_l,
    compute_max_possible_ro_correction_l,
    compute_nutrient_split,
    compute_total_nutrients_ml,
    nutrient_b_percent,
    round_l,
    validate_bool_field,
    validate_recipe_field_types,
    validate_recipe_values,
)

RECIPE_SCHEMA_VERSION = 4

# Editable time fields (not structural identifiers like schema_version/slot)
# that may have a whole-number float such as 300.0 controlled-normalized to
# a real int in __post_init__ below - see domain/recipe_limits.py
# ::validate_strict_int_field for why schema_version/slot get none of this
# tolerance.
_INT_COERCIBLE_TIME_FIELDS = ("ec_mixing_time_seconds", "ph_mixing_time_seconds")


def _coerce_whole_number_float_to_int(value: Any) -> Any:
    """Converts value to int ONLY if it is a whole-number float
    (math.isfinite and .is_integer()) and not a bool - any other value
    (non-integral float, str, None, bool, already-int) passes through
    unchanged so StoredRecipe.validate() can report the real offending
    value instead of a silently coerced one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


@dataclass
class StoredRecipe:
    """The editable recipe as saved to recipes/dashboard_recipes.json.
    Fachliche Rezeptwerte only - see module docstring."""

    schema_version: int = RECIPE_SCHEMA_VERSION
    slot: int = 1
    recipe_name: str = ""
    target_tank: str = ""

    target_ro_water_l: float = 0.0

    target_ec_ms_cm: float = 0.0
    ec_mixing_time_seconds: int = 0
    nutrients_ml_per_100_l: float = 0.0
    nutrient_a_percent: float = 50.0
    ec_adjustment_factor: float = 1.0
    nutrient_dosing_enabled: bool = False

    target_ph: float = 0.0
    volume_acid_1_ml: float = 0.0
    volume_acid_2_ml: float = 0.0
    volume_base_ml: float = 0.0
    ph_mixing_time_seconds: int = 0
    ph_adjustment_factor: float = 0.0

    notes: str = ""
    updated_at: str | None = None

    # Legacy fields from schema_version 1 (volume_stock_1_ml/volume_stock_2_ml),
    # preserved verbatim for manual review rather than guessed-converted into
    # nutrients_ml_per_100_l (their reference water amount is ambiguous - see
    # docs/OPEN_RECIPE_DECISIONS.md). None if the recipe was never migrated
    # from a legacy stock-volume model.
    legacy_volume_stock_1_ml: float | None = None
    legacy_volume_stock_2_ml: float | None = None
    legacy_dosing_needs_review: bool = False

    # Legacy process/runtime-only values from schema_version 2
    # (requested_ro_correction_l, sensor_circulation_enabled,
    # sensor_pump_seconds, drain_after_process) - quarantined here for audit
    # by the schema_version 2 -> 3 migration. Purely informational: never
    # read to activate a RunOption, never merged into a RunConfigSnapshot,
    # never triggers any hardware action. See
    # nicegui_dashboard/recipe_store.py::_migrate_v2_to_v3.
    legacy_process_values: dict[str, Any] | None = None

    # Legacy recipe values from schema_version 3 (addon_1_ml, addon_2_ml) -
    # quarantined here for audit by the schema_version 3 -> 4 migration.
    # Addon 1/Addon 2 were meant as independent additions, but the real CDS
    # process only has Nutrient Solution A/B (already fully covered by the
    # EC section above) - they no longer belong to the active recipe model.
    # Purely informational: never shown in the Recipe Editor, never affects
    # a volume calculation, never reaches a RunConfigSnapshot, never
    # triggers dosing/hardware. See
    # nicegui_dashboard/recipe_store.py::_migrate_v3_to_v4.
    legacy_recipe_values: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Controlled float->int normalization for editable time fields only
        # (see _INT_COERCIBLE_TIME_FIELDS docstring above). schema_version/
        # slot are deliberately NOT touched here - they are validated with
        # zero float tolerance in validate_recipe_field_types() instead.
        for name in _INT_COERCIBLE_TIME_FIELDS:
            setattr(self, name, _coerce_whole_number_float_to_int(getattr(self, name)))

    def validate(self) -> list[str]:
        # Type/finiteness check runs first and gates the business-rule pass
        # below: validate_recipe_values() does arithmetic/comparisons on
        # these values and would raise a raw TypeError on a string/None/NaN
        # instead of a clear RecipeValidationError.
        type_errors = validate_recipe_field_types(self)
        if type_errors:
            return type_errors

        return validate_recipe_values(
            target_ro_water_l=self.target_ro_water_l,
            target_ec_ms_cm=self.target_ec_ms_cm,
            ec_mixing_time_seconds=self.ec_mixing_time_seconds,
            nutrients_ml_per_100_l=self.nutrients_ml_per_100_l,
            nutrient_a_percent=self.nutrient_a_percent,
            ec_adjustment_factor=self.ec_adjustment_factor,
            nutrient_dosing_enabled=self.nutrient_dosing_enabled,
            legacy_dosing_needs_review=self.legacy_dosing_needs_review,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoredRecipe":
        known_names = {f.name for f in fields(cls)}
        kwargs = {key: value for key, value in data.items() if key in known_names}
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecipePreview:
    """Calculated, always-recomputable preview - never persisted itself."""

    nutrient_b_percent: float
    total_nutrients_ml: float
    nutrient_a_ml: float
    nutrient_b_ml: float
    estimated_process_volume_l: float
    remaining_capacity_l: float
    max_possible_ro_correction_l: float

    @classmethod
    def from_recipe(cls, recipe: StoredRecipe) -> "RecipePreview":
        if recipe.nutrient_dosing_enabled:
            total_nutrients_ml = compute_total_nutrients_ml(
                recipe.target_ro_water_l,
                recipe.nutrients_ml_per_100_l,
                recipe.ec_adjustment_factor,
            )
            nutrient_a_ml, nutrient_b_ml = compute_nutrient_split(
                total_nutrients_ml, recipe.nutrient_a_percent
            )
        else:
            # Dosing disabled: nothing will actually be pumped, so neither
            # the snapshot nor the volume estimate should count a dose that
            # will never happen - see docs/RECIPE_AND_DOSING_RULES.md.
            total_nutrients_ml = 0.0
            nutrient_a_ml, nutrient_b_ml = 0.0, 0.0

        estimated_process_volume_l = round_l(
            compute_base_volume_before_ro_correction_l(
                recipe.target_ro_water_l, total_nutrients_ml
            )
        )
        remaining_capacity_l = round_l(max(0.0, MAX_PROCESS_VOLUME_L - estimated_process_volume_l))
        max_possible_ro_correction_l = compute_max_possible_ro_correction_l(estimated_process_volume_l)

        return cls(
            nutrient_b_percent=nutrient_b_percent(recipe.nutrient_a_percent),
            total_nutrients_ml=total_nutrients_ml,
            nutrient_a_ml=nutrient_a_ml,
            nutrient_b_ml=nutrient_b_ml,
            estimated_process_volume_l=estimated_process_volume_l,
            remaining_capacity_l=remaining_capacity_l,
            max_possible_ro_correction_l=max_possible_ro_correction_l,
        )


@dataclass(frozen=True)
class RunOptions:
    """
    Per-run-only choices - never part of a recipe, never persisted, always
    freshly constructed at the start of every run (defaulting to the safest,
    most inactive state: everything off). Loading/selecting a recipe must
    never by itself activate sensor circulation or a post-process drain -
    only an explicit, per-run choice (e.g. a toggle in the Start/confirmation
    dialog) may do that, and only for a function that is genuinely wired to
    real hardware (see nicegui_dashboard/pages/dashboard_page.py).
    """

    sensor_circulation_enabled: bool = False
    drain_after_process: bool = False

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name, value in (
            ("sensor_circulation_enabled", self.sensor_circulation_enabled),
            ("drain_after_process", self.drain_after_process),
        ):
            message = validate_bool_field(name, value)
            if message:
                errors.append(message)
        return errors


@dataclass(frozen=True)
class RunConfigSnapshot:
    """
    Immutable snapshot built exclusively from a *validated* StoredRecipe, the
    active ProcessSettings dict, and a RunOptions instance - built once when
    a process run is actually started. This is the only object that should
    ever be handed to process execution code - never the mutable
    StoredRecipe/raw dict/RunOptions/UI state directly, and nothing
    downstream may re-read an "effective" value from any of those again
    once this snapshot exists.

    NOTE on integration status: today only target_ro_water_l and
    sensor_circulation_enabled have a real hardware consumer (see
    nicegui_dashboard/recipe_store.py::build_run_config and README.md
    Section 15/17). drain_after_process has no consumer at all yet (not
    shown as functional in the GUI - see dashboard_page.py). The EC nutrient
    fields below are fully calculated and validated, but there is no
    peristaltic-pump consumer yet - see docs/RECIPE_AND_DOSING_RULES.md
    "Remaining integration point".
    """

    recipe_id: str
    recipe_name: str
    schema_version: int
    created_at: str

    target_ro_water_l: float
    max_mixer_liters: float

    target_ec_ms_cm: float
    ec_mixing_time_seconds: int

    nutrient_dosing_enabled: bool
    legacy_dosing_needs_review: bool
    nutrients_ml_per_100_l: float
    ec_adjustment_factor: float
    nutrient_a_percent: float
    nutrient_b_percent: float
    calculated_total_nutrients_ml: float
    calculated_nutrient_a_ml: float
    calculated_nutrient_b_ml: float

    estimated_process_volume_l: float
    remaining_capacity_l: float
    max_possible_ro_correction_l: float

    # From RunOptions - a per-run choice, never from the recipe itself.
    sensor_circulation_enabled: bool
    drain_after_process: bool

    @classmethod
    def build(
        cls,
        recipe: StoredRecipe,
        process_settings: dict[str, Any],
        run_options: RunOptions | None = None,
    ) -> "RunConfigSnapshot":
        run_options = run_options or RunOptions()

        errors = recipe.validate() + run_options.validate()
        if errors:
            formatted = "\n".join(f"  - {message}" for message in errors)
            raise RecipeValidationError(
                f"Rezept '{recipe.recipe_name}' (Slot {recipe.slot}) ist ungültig, "
                f"{len(errors)} Fehler:\n{formatted}"
            )

        max_mixer_liters = float(process_settings.get("max_mixer_liters", MAX_PROCESS_VOLUME_L))
        if recipe.target_ro_water_l > max_mixer_liters:
            raise ValueError(
                f"Rezept-Zielvolumen {recipe.target_ro_water_l} L überschreitet "
                f"max_mixer_liters ({max_mixer_liters} L)."
            )

        preview = RecipePreview.from_recipe(recipe)

        return cls(
            recipe_id=f"slot-{recipe.slot}",
            recipe_name=recipe.recipe_name,
            schema_version=recipe.schema_version,
            created_at=datetime.now().isoformat(timespec="seconds"),
            target_ro_water_l=recipe.target_ro_water_l,
            max_mixer_liters=max_mixer_liters,
            target_ec_ms_cm=recipe.target_ec_ms_cm,
            ec_mixing_time_seconds=recipe.ec_mixing_time_seconds,
            nutrient_dosing_enabled=recipe.nutrient_dosing_enabled,
            legacy_dosing_needs_review=recipe.legacy_dosing_needs_review,
            nutrients_ml_per_100_l=recipe.nutrients_ml_per_100_l,
            ec_adjustment_factor=recipe.ec_adjustment_factor,
            nutrient_a_percent=recipe.nutrient_a_percent,
            nutrient_b_percent=preview.nutrient_b_percent,
            calculated_total_nutrients_ml=preview.total_nutrients_ml,
            calculated_nutrient_a_ml=preview.nutrient_a_ml,
            calculated_nutrient_b_ml=preview.nutrient_b_ml,
            estimated_process_volume_l=preview.estimated_process_volume_l,
            remaining_capacity_l=preview.remaining_capacity_l,
            max_possible_ro_correction_l=preview.max_possible_ro_correction_l,
            sensor_circulation_enabled=run_options.sensor_circulation_enabled,
            drain_after_process=run_options.drain_after_process,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
