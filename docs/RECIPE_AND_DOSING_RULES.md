# Recipe, Tank Volume & EC Nutrient Dosing Rules

Single source of truth for the business rules implemented in
`domain/recipe_limits.py` and `domain/recipe_model.py`. If this document and
the code ever disagree, the code (and its tests in `tests/test_recipe_domain.py`,
`tests/test_recipe_run_config.py`, `tests/test_recipe_store.py`) is authoritative -
please update this file to match, not the other way around.

This describes the CDS project's recipe/dosing domain logic in general. It
does not name any specific operating company or site.

## 1. Tank capacity vs. usable recipe/process volume

Three distinct numbers, deliberately not interchangeable:

| Constant                     | Value    | Meaning |
|-------------------------------|---------:|---------|
| `TANK_PHYSICAL_CAPACITY_L`    | 200.0 l  | Approximate physical Mixing Tank capacity up to the rim. **Never** a usable recipe or process limit. |
| `MAX_RECIPE_RO_WATER_L`       | 180.0 l  | Maximum allowed amount of pure RO water in a recipe (`target_ro_water_l`). |
| `MAX_PROCESS_VOLUME_L`        | 185.0 l  | Maximum allowed estimated/measured recipe/process volume (RO water + calculated nutrients). |

Nutrient solutions come **on top of** the RO water - they are not deducted
from the 180 l RO water budget, they consume the remaining headroom up to
185 l total. Addon 1/Addon 2 no longer exist in the active recipe model (see
Section 5.1) - the real CDS process only has Nutrient Solution A/B.

Every operational fill target across the project is now capped at the same
185 l limit, not the 200 l physical rim capacity:

- `max_mixer_liters` in `config/process_settings.json`, `config/water_cycle_settings.json`,
  and `config/tank_cleaning_settings.json` is `185.0`.
- `target_total_liters` (`config/process_settings.json`) and
  `target_fill_total_liters` (`config/water_cycle_settings.json`,
  `config/tank_cleaning_settings.json`) are schema-capped at `185.0` too -
  the former `200.0` defaults are gone.

All of these are enforced as hard schema ceilings
(`max_value=MAX_PROCESS_VOLUME_L` on the respective `SettingField`s in
`nicegui_dashboard/process_controller.py` and `process/common.py`) - a
config edit that raises one back above 185 fails loudly at load time, it is
not just a convention. `config/system_config.json`'s
`mixer_level_calibration.volume_liters = 200.0` is unrelated - that is the
physical calibration reference volume, not an operational limit, and is
deliberately untouched.

## 2. EC nutrient solutions A and B

The two EC nutrient stock solutions are called **Nährstofflösung A** and
**Nährstofflösung B**. Together they always make up exactly 100 %.

- Only the A share (`nutrient_a_percent`) is stored/edited. Default: **50 %**.
- B is always calculated: `nutrient_b_percent = 100.0 - nutrient_a_percent`.
  It is displayed as a read-only, automatically-updated value and can never
  be edited independently - this is what guarantees A + B == 100 by
  construction, not by a secondary check.
- Example: entering A = 40 % immediately shows B = 60 %.
- The percentage range is the mathematical 0-100 - no narrower chemical
  bounds are enforced, because none have been fachlich confirmed yet (see
  `docs/OPEN_RECIPE_DECISIONS.md`).

**`nutrient_dosing_enabled`** (default `false`) gates whether dosing is
actually active for a recipe. When `false`: the calculated nutrient amounts
(`calculated_total_nutrients_ml`/`calculated_nutrient_a_ml`/
`calculated_nutrient_b_ml`) are forced to `0.0` everywhere (GUI preview,
estimated recipe volume, and the `RunConfigSnapshot`) - nothing will
actually be dosed, so nothing should be counted as if it would be. The
recipe's own stored `nutrients_ml_per_100_l` value stays visible/unchanged
regardless - only the *calculated, would-actually-happen* amounts are forced
to zero. The EC adjustment factor's `0.01`-`1.0` bound (see Section 6) is
only checked while dosing is enabled - an inert factor value is not an
error. A recipe still flagged `legacy_dosing_needs_review = true` (see
Section 5) cannot set `nutrient_dosing_enabled = true` - this is enforced,
not just a UI hint.

### Dose calculation

```
total_nutrients_ml = target_ro_water_l / 100.0 * nutrients_ml_per_100_l * ec_adjustment_factor
nutrient_a_ml       = total_nutrients_ml * nutrient_a_percent / 100.0
nutrient_b_ml       = total_nutrients_ml - nutrient_a_ml     # remainder, not independent
```

B is always the *remainder* of the (already rounded) total, never computed
independently - this is what keeps A + B equal to the total even after
rounding (see `domain/recipe_limits.py::compute_nutrient_split`).

**Rounding strategy**: every calculated millilitre or litre value is rounded
to **3 decimal places** (`ML_ROUNDING_DECIMALS` / `L_ROUNDING_DECIMALS` in
`domain/recipe_limits.py`), applied consistently everywhere a value is
derived. Raw user input (e.g. `target_ec_ms_cm`, `target_ro_water_l`) is
compared directly, without rounding or quantizing, against its own limit -
the Recipe Editor's EC stepper (`step=0.1`) is a UI convenience only; a
value like `2.35` is accepted and stored exactly as entered.

`nutrients_ml_per_100_l` has **no invented default** - existing recipes
migrated from the old absolute-stock-volume model default to `0.0` (no dose)
until a confirmed value is entered; see Section 5 below and
`docs/OPEN_RECIPE_DECISIONS.md`.

## 3. Estimated recipe volume

```
estimated_process_volume_l =
    target_ro_water_l
    + total_nutrients_ml / 1000.0
```

This is the recipe's whole estimated volume and the value validated against
the 185 l limit - **a fixed RO correction is not part of a recipe at all
anymore** (see Section 4): there is nothing else to add. If this volume
exceeds 185 l, the recipe is invalid; no clamping, only rejection with a
concrete message (e.g. "Das geschätzte Rezeptvolumen von 185.4 l
überschreitet das Limit von 185.0 l.").

pH correction (`volume_acid_1_ml`, `volume_acid_2_ml`, `volume_base_ml`) is
**deliberately excluded** - the actual pH correction amount needed is not
reliably known at recipe time, and estimating it with an invented flat
amount would produce a false sense of precision. The volume known at recipe
time must stay at or under 185 l on its own; any later runtime pH/EC
correction has to be checked against whatever capacity is *then* still
available - the architecture supports this check (Section 4) but no runtime
consumer exists yet.

**No silent clamping.** A recipe whose `target_ro_water_l` or estimated
volume exceeds its limit is *rejected* with a concrete error message (e.g.
"RO-Wassermenge darf maximal 180.0 l betragen (war: 190.0 l)."), never
silently reduced to the limit and saved anyway.

## 4. RO correction is not a recipe value

A fixed RO correction amount is **not stored in a recipe and not part of a
`RunConfigSnapshot`** - there is no `requested_ro_correction_l` field
anywhere anymore. A real, demand-driven EC correction (measure, then correct
by exactly as much as needed) is a planned future feature, not implemented
today, and the old fixed-amount-per-recipe model had no real hardware
consumer anyway.

What the Recipe Editor's "Volumenvorschau" section shows is purely
informational - how much correction capacity *would* technically still be
available later, given this recipe's own estimated volume:

```
remaining_capacity_l = max(0.0, MAX_PROCESS_VOLUME_L - estimated_process_volume_l)
max_possible_ro_correction_l = min(LEGACY_MAX_RO_CORRECTION_L, remaining_capacity_l)
```

The legacy static cap (`LEGACY_MAX_RO_CORRECTION_L = 50.0`, confirmed from
the old Node-RED `EC_config.maxvol3`) is why this number is capped at 50 l
even when far more process-volume headroom remains - e.g. at an estimated
volume of 182 l, at most 3 l of later correction would be possible even
though the static cap is 50 l.

## 5. Stored recipe, preview, run options, and the RunConfig snapshot

Four distinct types (`domain/recipe_model.py`):

1. **`StoredRecipe`** - the editable, persisted recipe (mutable dataclass,
   mirrors `recipes/dashboard_recipes.json`). Contains only fachliche
   Rezeptwerte: recipe name/tank, RO water amount, EC/pH setpoints and
   recipe-specific mixing times, nutrient configuration (Nutrient Solution
   A/B - see Section 5.1), notes, and legacy status. It does **not** contain
   any per-run/technical choice - see RunOptions below.
2. **`RecipePreview`** - calculated preview values for the GUI. Always
   recomputable, never itself persisted, and deliberately does **not** raise
   on out-of-range input - the GUI needs live numbers (including "too high")
   so it can show a concrete error message while the user is still typing.
3. **`RunOptions`** - typed, per-run-only choices (`sensor_circulation_enabled`,
   `drain_after_process`), both defaulting to `False`. Never persisted in a
   recipe and always freshly constructed at the start of every run -
   **loading or selecting a recipe must never by itself activate sensor
   circulation or a post-process drain**. Only an explicit, per-run choice
   (a toggle in the Process Control panel's confirmation area, see
   `nicegui_dashboard/pages/dashboard_page.py`) may turn `sensor_circulation_enabled`
   on - and only because that one is genuinely wired to real hardware (the
   `sensor_circulation_pump` GPIO actuator via
   `statemachine/fill_and_measure_state_machine.py`). `drain_after_process`
   has **no hardware consumer at all** today and is deliberately **not**
   shown as a GUI toggle - exposing a control for a function that does
   nothing would misrepresent it as functional.
4. **`RunConfigSnapshot`** - a frozen (immutable) snapshot built **exclusively**
   from a validated `StoredRecipe` + the active `ProcessSettings` dict + a
   `RunOptions` instance (`RunConfigSnapshot.build(recipe, process_settings,
   run_options)`), built once when a process run is actually started. Raises
   `RecipeValidationError`/`ValueError` instead of being built from an
   invalid recipe or a `max_mixer_liters` violation - fail closed, nothing
   is ever clamped-and-run. Once this snapshot exists, nothing downstream
   may read an "effective" value from the UI, `StoredRecipe`, or
   `RunOptions` again -
   `nicegui_dashboard/recipe_store.py::build_run_config()` sources every
   value it acts on exclusively from the snapshot. **`ProcessController`
   stores the exact `RunConfigSnapshot` (merged into the run's full settings
   dict) a run was actually started with, and `get_status()` shows *that*
   stored snapshot - never a freshly recomputed one - for as long as the
   process is running or tearing down.** This matters because the active
   recipe can change while a process is in flight (e.g. a different favorite
   is activated from the Recipe/Setpoints card): a running process's status
   must keep showing the recipe it actually started with, unaffected by any
   later favorite change - only the *next* run picks up the newly active
   recipe. Once the run finishes, `get_status()` reverts to a live preview
   of the currently active recipe again.

### 5.1 Addon 1/Addon 2 removed from the active recipe model

Addon 1 and Addon 2 (`addon_1_ml`/`addon_2_ml`) were originally modeled as
independent additions, but the real CDS process only has Nutrient Solution
A/B, the total dose, and the A/B ratio - already fully covered by Section 2
above. They have been **removed from `StoredRecipe`, `RecipePreview`, and
`RunConfigSnapshot` entirely**, and from the Recipe Editor's form (the
"Addons" section no longer exists - the editor is now Grunddaten / EC / pH /
Volumenvorschau / Notiz).

Old addon values are not discarded: the schema_version 3 -> 4 migration
(`nicegui_dashboard/recipe_store.py::_migrate_v3_to_v4`) moves them into a
quarantined `legacy_recipe_values` dict on the recipe (e.g.
`{"addon_1_ml": 50.0, "addon_2_ml": 50.0}`), kept only for audit/traceability
- exactly the same pattern already used for `legacy_process_values` (see
Section 5 above / `docs/OPEN_RECIPE_DECISIONS.md` Section 5). This data is
**never shown in the Recipe Editor, never affects a volume calculation,
never reaches a `RunConfigSnapshot`, and never triggers dosing or any
hardware action.**

### 5.2 Three permanently stored favorite slots

`recipes/dashboard_recipes.json` permanently stores exactly **three
independent favorite recipes** (slots 1/2/3, `FAVORITE_SLOTS = (1, 2, 3)`).
They are genuinely independent: saving a recipe to one slot only ever
changes that slot - the other two are left byte-for-byte unchanged (the
whole load-modify-save sequence runs under one lock, see "Concurrent saves"
below). One slot is additionally marked `active_slot` - this is the recipe a
new process run actually uses.

The Recipe/Setpoints card shows three real **F1/F2/F3 buttons** (not static
labels), each labelled with that slot's current recipe name. Clicking one
only calls `set_active_slot(slot)` - it loads the book fresh, validates it,
atomically saves the new `active_slot`, and immediately refreshes the
Recipe/Setpoints card and the button highlighting from that freshly-loaded
book. **A favorite-button click never starts a process and never touches
hardware** - it only changes which recipe is considered "active" for the
*next* run. Opening the Recipe Editor afterward shows exactly the slot that
was last clicked/selected.

**Remaining integration point** (documented explicitly, not hidden): today
only `target_ro_water_l` (from the recipe) and `sensor_circulation_enabled`
(from `RunOptions`) have a real hardware consumer
(`statemachine/fill_and_measure_state_machine.py` via
`nicegui_dashboard/recipe_store.py::build_run_config()` /
`ProcessController.start_fill_and_measure()`). The EC nutrient fields in
`RunConfigSnapshot` (`nutrients_ml_per_100_l`, `nutrient_a_percent/b_percent`,
`calculated_nutrient_a_ml/b_ml`, `ec_adjustment_factor`, ...) are fully
calculated and validated and attached to the run's settings dict under
`recipe_run_config_snapshot` for display/audit, but there is still **no**
peristaltic-pump consumer for them - see `docs/PERISTALTIC_PROFILE_PLAN.md`
and README.md Section 15/17. `drain_after_process` and the technical
`sensor_pump_seconds`-shaped concept have **no** consumer anywhere and no
GUI control claiming otherwise. No fake/no-op dosing or run behaviour was
introduced to paper over any of these gaps.

**Legacy dosing review gate**: a recipe migrated from the old absolute-stock
model (`legacy_dosing_needs_review = true`) cannot have
`nutrient_dosing_enabled = true` - `StoredRecipe.validate()` rejects that
combination outright. The Recipe Editor's "Legacy review completed" switch
is the *only* way to clear `legacy_dosing_needs_review` - it never clears
itself just because a number was typed. The normal RO fill
(`target_ro_water_l`) is unaffected either way - only nutrient dosing itself
is gated.

**Input robustness**: every numeric recipe field is checked for being a
genuine, finite number (`math.isfinite`) of the right type, and every
boolean field (`nutrient_dosing_enabled`, `legacy_dosing_needs_review`, and
`RunOptions`' two fields) is checked with `type(value) is bool` - never
`bool(value)`, which would silently treat a string `"false"`, a nonzero
number, or a non-empty list/dict as true. Both checks run before any
business-rule arithmetic
(`domain/recipe_limits.py::validate_recipe_field_types`). `NaN`, `+Inf`,
`-Inf`, strings, `null`, non-integral floats in integer fields (e.g. `300.5`
seconds), and anything but a genuine `bool` in a boolean field are all
rejected with a concrete `RecipeValidationError` naming the field - never a
raw `TypeError`/`ValueError` from arithmetic deep inside a comparison.

**Two-tier integer contract**: `schema_version`/`slot` are structural
identifiers, never user-typed values - `validate_strict_int_field` accepts
**only** a genuine `type(value) is int`, with zero float tolerance (`1.0`,
`3.0`, and `True`/`False` are all rejected, never silently normalized).
`ec_mixing_time_seconds`/`ph_mixing_time_seconds` are editable time fields,
where JSON doesn't distinguish `300` from `300.0` - `StoredRecipe.__post_init__`
controlled-normalizes *only* a whole-number float to a real `int` for these
two fields; a non-integral float (`300.5`) is left unchanged so `validate()`
reports the real offending value instead of masking it.

Recipe-book *metadata* (`schema_version`, `active_slot`, each
`favorite.slot`) gets the same fail-closed treatment one level up, in
`nicegui_dashboard/recipe_store.py::_require_int_metadata` - a wrong-typed
value there raises `RecipeBookCorruptedError`, never a raw `int(...)`
exception. This is now enforced regardless of the book's top-level
`schema_version`: an unknown/future `schema_version` (higher than the code
supports), duplicate favorite slots, a `favorite.slot`/`active_slot` outside
`1-3`, or a non-dict favorite all raise `RecipeBookCorruptedError` - none of
these are silently self-healed anymore (a previous version silently reset an
out-of-range `active_slot` back to `1`; that self-healing is gone in favour
of failing closed). `get_recipe_by_slot()`/`save_recipe_to_slot()`/
`set_active_slot()` require a genuine `type(slot) is int` too - they no
longer normalize via `int(slot)`, so a float, a bool, or a numeric string
is rejected outright rather than silently coerced. `nicegui_dashboard/recipe_store.py`
also serializes with `json.dumps(..., allow_nan=False)` as a defense-in-depth
backstop.

**Concurrent saves**: `save_recipe_to_slot()`/`set_active_slot()` hold a
single re-entrant lock (`threading.RLock`) across their entire
load-modify-save sequence, not just the final write - two concurrent saves
to different favorite slots can no longer silently discard one another.

## 6. Known vs. still-open limits

Confirmed and enforced in `domain/recipe_limits.py::validate_recipe_values()`:

- `MAX_RECIPE_RO_WATER_L = 180.0`, `MAX_PROCESS_VOLUME_L = 185.0`,
  `TANK_PHYSICAL_CAPACITY_L = 200.0` (never a usable limit) - explicit
  product decisions for this task.
- `LEGACY_MAX_RO_CORRECTION_L = 50.0` and `EC_ADJUSTMENT_FACTOR_MAX = 1.0` -
  confirmed against the old Node-RED `EC_config` (`maxvol3`, `maxfactor`).
  `EC_ADJUSTMENT_FACTOR_ENABLED_MIN = 0.01` applies only while
  `nutrient_dosing_enabled` is true (see Section 2/5).
- `EC_SETPOINT_MIN_MS_CM = 0.5` / `EC_SETPOINT_MAX_MS_CM = 2.5` and
  `EC_MIXING_TIME_MIN_SECONDS = 300` / `EC_MIXING_TIME_MAX_SECONDS = 1200` -
  confirmed against the old Node-RED `EC_config` (mixing time there is in
  minutes; 300-1200 s = 5-20 min).

Addon 1/Addon 2 no longer have a bound here at all - they were removed from
the active recipe model entirely (see Section 5.1), not just left
unenforced.

Still open (not enforced, see `docs/OPEN_RECIPE_DECISIONS.md` for detail):
pH setpoint/mixing-time/stock-volume bounds (pH dosing has no consumer yet),
and how (if ever) the quarantined legacy stock-volume fields
(`legacy_volume_stock_1_ml`/`legacy_volume_stock_2_ml`), the quarantined
legacy process values (`legacy_process_values`), or the quarantined legacy
recipe values (`legacy_recipe_values`, i.e. the old addon amounts - see
Section 5.1 and `docs/OPEN_RECIPE_DECISIONS.md`) should ever be converted
into a live value.

## 7. What is still not real

For clarity, explicitly restated here even though it follows from the
sections above: **no real RO correction is implemented yet** (Section 4 is
informational only), **no real peristaltic pumps are connected** (Section 5
"Remaining integration point"), and **no claim is made that real chemical
dosing has already been tested** - the EC nutrient calculation is fully
implemented and unit-tested as pure domain logic, but has never driven a
real pump.
