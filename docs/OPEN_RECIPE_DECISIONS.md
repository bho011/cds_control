# Open Recipe/Dosing Decisions

Points that could not be safely resolved from the current `cds_control`
repository alone, plus what was actually confirmed by inspecting the legacy
Node-RED project (`~/.node-red/projects/Central_Dosing_System/flows.json`,
function node "EC Configuration" around line 3415, and the surrounding
tank-configuration flow). Nothing below was guessed; every "confirmed" value
is a direct quote from that file.

**Update 1**: a follow-up correction pass enforces EC setpoint, EC mixing
time, addon amounts, and a dosing-enabled-conditional EC adjustment factor
minimum - all previously listed below as "confirmed but not enforced". See
`domain/recipe_limits.py::validate_recipe_values()` and
`docs/RECIPE_AND_DOSING_RULES.md` Section 6 for the current, authoritative
enforcement.

**Update 2**: a second correction pass removed the fixed RO correction
concept from the recipe model entirely (`requested_ro_correction_l` no
longer exists anywhere - see `docs/RECIPE_AND_DOSING_RULES.md` Section 4),
and separated `sensor_circulation_enabled`/`sensor_pump_seconds`/
`drain_after_process` out of the recipe into `RunOptions` (per-run only) or
removed them as dead fields entirely. A `recipes/dashboard_recipes.json`
schema_version 2 -> 3 migration quarantines the old values of all four
fields under `legacy_process_values` for audit (see Section 5 below) -
`RunOptions` itself is never persisted anywhere.

**Update 3**: a third correction pass (a) fixed the root cause of a
favorites-loss bug report - the previously committed `load_recipe_book()`
silently overwrote a corrupted file with fresh defaults instead of raising;
the currently committed code already fails closed with
`RecipeBookCorruptedError` and writes atomically, and the Recipe/Setpoints
card's F1/F2/F3 favorite badges (previously non-interactive labels) are now
real buttons that activate a slot; (b) removed Addon 1/Addon 2 from the
active recipe model entirely - see Section 6 below and
`docs/RECIPE_AND_DOSING_RULES.md` Section 5.1 - since the real CDS process
only has Nutrient Solution A/B; (c) made the recipe-book metadata contract
fully fail-closed (unknown future `schema_version`, duplicate slots,
out-of-range `favorite.slot`/`active_slot`, non-dict favorites are now all
rejected, none silently self-healed); (d) gave `schema_version`/`slot` a
strict, zero-float-tolerance integer contract, separate from the
lenient whole-number-float tolerance kept for `ec_mixing_time_seconds`/
`ph_mixing_time_seconds`; (e) made `ProcessController` store the exact
`RunConfigSnapshot` a run was actually started with, so `get_status()` keeps
showing that snapshot - not a freshly recomputed one - for as long as the
process is running.

Only the pH-related bounds and the legacy stock-volume/legacy-process-values/
legacy-recipe-values conversion questions (Section 2/3/5/6 below) remain
genuinely open.

## 1. Values confirmed from the legacy system, and their current status here

| Field | Legacy value (Node-RED `EC_config`/`pH_config`/`addon_config`) | Status in this implementation |
|---|---|---|
| EC setpoint | min 0.5, max 2.5 mS/cm | **Confirmed and enforced**: `EC_SETPOINT_MIN_MS_CM = 0.5` / `EC_SETPOINT_MAX_MS_CM = 2.5` in `domain/recipe_limits.py`. |
| EC mixing time | min 5, max 20 | Unit resolved: legacy code computes `mixingTimeMs = (cfg.EC.mixing_time ?? 10) * 60 * 1000` - **the legacy unit is minutes**, i.e. 5-20 minutes = 300-1200 seconds. **Confirmed and enforced** on the seconds-based `ec_mixing_time_seconds` field: `EC_MIXING_TIME_MIN_SECONDS = 300` / `EC_MIXING_TIME_MAX_SECONDS = 1200`. |
| Stock solution 1 (legacy absolute dosing) | min 100, max 1000 ml | Superseded by `nutrients_ml_per_100_l` + `nutrient_a_percent`. The legacy amount is preserved as `legacy_volume_stock_1_ml` (quarantined, not auto-converted - see Section 2). Still not enforced as a bound - the field is legacy-only display data now, not a live input. |
| Stock solution 2 (legacy absolute dosing) | min 100, max 1000 ml | Same as above - preserved as `legacy_volume_stock_2_ml`, not enforced. |
| RO correction / dilution (upper bound) | max 50 l | **Confirmed, but no longer a stored recipe value at all** (see `docs/RECIPE_AND_DOSING_RULES.md` Section 4): `LEGACY_MAX_RO_CORRECTION_L = 50.0` in `domain/recipe_limits.py` now only bounds the purely informational `compute_max_possible_ro_correction_l()` preview - there is no `requested_ro_correction_l` field to apply it to anymore. A real, demand-driven EC correction is a planned future feature. |
| EC adjustment factor | min 0.01, max 1.0 | **Confirmed and enforced, conditionally**: `EC_ADJUSTMENT_FACTOR_MAX = 1.0` always applies. `EC_ADJUSTMENT_FACTOR_ENABLED_MIN = 0.01` applies only while `nutrient_dosing_enabled = true` - when dosing is disabled the factor is inert (no dose is calculated regardless of its value) and is not range-checked, resolving the previously open "is 0.0 a legitimate disabled state" question: yes, via the explicit `nutrient_dosing_enabled` flag rather than an implicit factor value of `0.0`. |
| Addon 1 | min 10, max 100 ml | **No longer part of the active recipe model at all** (Update 3): the real CDS process only has Nutrient Solution A/B (see Section 2 above), already fully covering this. `addon_1_ml` is quarantined under `legacy_recipe_values` by the schema_version 3 -> 4 migration - see Section 6 below. |
| Addon 2 | min 10, max 100 ml | Same as Addon 1. |

## 2. Why `volume_stock_1_ml`/`volume_stock_2_ml` were not auto-converted

The new model needs `nutrients_ml_per_100_l` (a dose *per 100 l of RO
water*). The legacy absolute stock amounts (e.g. 101 ml / 19 ml in the
existing `recipes/dashboard_recipes.json`) only convert into that unit if the
reference RO-water amount they were actually dosed against is known with
certainty. The same JSON record also happens to contain
`target_fill_total_liters: 50.0`, which is tempting to use as that reference
- but nothing in the code confirms that the legacy stock amounts were
*defined relative to* that particular fill volume rather than being a fixed
absolute dose regardless of tank fill level. Silently assuming so would risk
inventing a wrong nutrient dose for a system that doses real chemicals.

Per the task's own instruction ("nicht raten ... eine sichere bzw.
fail-closed Lösung verwenden"), the migration instead:

- keeps the legacy amounts verbatim under `legacy_volume_stock_1_ml` /
  `legacy_volume_stock_2_ml` (nothing is discarded),
- sets `legacy_dosing_needs_review = true` on any migrated recipe that had
  a non-zero legacy stock amount (surfaced as a visible warning in the
  Recipe Editor dialog),
- defaults `nutrients_ml_per_100_l` to `0.0` (an explicit "no dose" state,
  not an invented number) until someone enters and confirms a real value.

This also affects the current, real `recipes/dashboard_recipes.json`: after
migration, all three saved favorites have `legacy_dosing_needs_review: true`
and `nutrients_ml_per_100_l: 0.0` - i.e. **EC nutrient dosing is currently
off** for all three saved recipes until manually reviewed and set.

## 3. Additional legacy findings, for context (not acted on)

- `global.set("VolumeMixer", 200)` in the legacy tank config matches
  `TANK_PHYSICAL_CAPACITY_L = 200.0` used here - consistent.
- `global.set("safetyvolumemixer", 20)` matches the existing, independent
  `config/process_settings.json::min_ro_liters_required = 20.0` - consistent,
  unrelated to this task's changes.
- pH setpoint: min 5.0, max 6.5. pH mixing time: min 5, max 20 (minutes, same
  code pattern as EC). pH stock volumes (acid 1/2, base): min 10, max 50 ml
  each. None of these are enforced here - pH dosing has no consumer yet and
  was out of scope.
- The legacy `default_recipe` global (a *different*, separate block from
  `EC_config`/`pH_config`) sets `EC.adjustment_factor: 5` and
  `pH.adjustment_factor: 5` - both **exceed** the same file's own confirmed
  `maxfactor: 1` limit. This is an internal inconsistency in the legacy
  system, not a reliable value - it was **not** used as a default here.
- `maxdelta: 0.1` (EC) / `maxdelta: 0.05` (used elsewhere in the same file
  for pH) is a legacy *tolerance band* around the setpoint (used to decide
  whether a correction is needed at all), not a recipe input field - not
  applicable to the new dosing model.
- `global.set("mixertankvol", {...})` / `maxtankvol` (300 l per numbered
  target tank 1-9) describes the separate downstream *target* tanks, not the
  Mixing Tank this task's limits apply to - not relevant here.

## 4. Suggested next step

If pH setpoint/mixing-time/stock-volume bounds should also be enforced going
forward, that is the same small, additive change to
`domain/recipe_limits.py::validate_recipe_values()` the EC bounds already
received (the legacy values are already known, see Section 3 above) - but pH
dosing has no consumer yet (see `docs/RECIPE_AND_DOSING_RULES.md` Section
5), so it remains a follow-up confirmation rather than being added silently
here.

Separately, whether/how the quarantined `legacy_volume_stock_1_ml`/
`legacy_volume_stock_2_ml` fields (Section 2 above) should ever be converted
into a `nutrients_ml_per_100_l` value - or simply stay permanent audit data -
is still an open product decision, not a code question.

## 5. schema_version 2 -> 3: `legacy_process_values` quarantine

A second migration step (`nicegui_dashboard/recipe_store.py::_migrate_v2_to_v3`)
removes four fields from every recipe that isn't a fachlicher Rezeptwert:

| Field (schema_version 2) | Why it was removed |
|---|---|
| `requested_ro_correction_l` | Not a recipe value anymore at all - see Section 1 above and `docs/RECIPE_AND_DOSING_RULES.md` Section 4. Had no hardware consumer even before removal. |
| `sensor_circulation_enabled` | Has a real hardware consumer, but must be a fresh per-run `RunOptions` choice from now on - a recipe-stored default would silently re-activate the sensor-circulation pump just by loading a recipe. |
| `sensor_pump_seconds` | A technical/ProcessSettings-shaped value, not a recipe value - confirmed (repo-wide search) to have zero execution-side consumers in the recipe/Fill-and-Measure path; an unrelated, also-unused `sensor_pump_seconds` key exists in `config/water_cycle_settings.json` for a different, dead code path (`process/sensor_circulation.py::run_sensor_pump_phase`, never called from `process/water_cycle.py::main`). |
| `drain_after_process` | Confirmed (repo-wide search, full read of `statemachine/fill_and_measure_state_machine.py`) to have **zero** consumers anywhere - not in the state machine, not in `ProcessController`. `process/tank_cleaning.py`'s own drain phase is a separate, unrelated maintenance workflow with no connection to this field. |

All four values are preserved verbatim under a new `legacy_process_values`
dict on the recipe (nothing is silently discarded), but are guaranteed to
never again activate a `RunOption`, never reach a `RunConfigSnapshot`, and
never trigger any hardware action - they are audit-only. Whether this data
is ever useful (e.g. to pre-fill a new "Sensor circulation for this run"
default the first time a migrated recipe's Process Control panel is opened)
is an open product question, not decided here - today the panel always
starts at the same safe `RunOptions()` default (`sensor_circulation_enabled=false`,
`drain_after_process=false`) regardless of `legacy_process_values`.

## 6. schema_version 3 -> 4: `legacy_recipe_values` quarantine (Addon 1/2 removal)

A third migration step (`nicegui_dashboard/recipe_store.py::_migrate_v3_to_v4`)
removes `addon_1_ml`/`addon_2_ml` from every recipe - they no longer belong
to the active recipe model at all (see Section 1 above and
`docs/RECIPE_AND_DOSING_RULES.md` Section 5.1). Both values are preserved
verbatim under a `legacy_recipe_values` dict on the recipe (e.g.
`{"addon_1_ml": 50.0, "addon_2_ml": 50.0}`), merge-safe: if
`legacy_recipe_values` already has other entries (e.g. from a hand-edited
file, or a second migration run), those are kept untouched and the addon
values are only added via `setdefault`, never overwriting an existing key -
running the migration a second time on an already-migrated recipe is a
no-op.

This data is guaranteed to never be shown in the Recipe Editor, never affect
a volume calculation, never reach a `RunConfigSnapshot`, and never trigger
dosing or any hardware action - purely audit-only, exactly like
`legacy_process_values` above. Whether it should ever be converted back into
a live value (there is currently no plan to reintroduce Addon 1/2 in any
form) is not decided here.
