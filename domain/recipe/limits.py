"""Central recipe/tank-volume/EC-nutrient limit constants.

Referenced from nicegui_dashboard/recipe_store.py, nicegui_dashboard/pages/dashboard_page.py
and nicegui_dashboard/process_controller.py (via the domain/recipe_limits.py
compatibility shim) so the constants below are never duplicated (and cannot
drift) across the GUI, the recipe store, and the process controller.

Values below were confirmed against the legacy Node-RED system
(~/.node-red/projects/Central_Dosing_System/flows.json, "EC Configuration"
function node) where noted - everything else is a fresh decision for this
project, documented in docs/RECIPE_AND_DOSING_RULES.md.
"""

from __future__ import annotations


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

# Capacity of the RO supply tank as configured in the sensor bridge
# (mqtt_sensor_bridge.py::RO_VOLUME_LITERS, published as
# ro.configured_max_liters). The bridge cannot import this module without
# changing a running service, so tests/test_start_validation.py asserts that
# both values stay identical. Used only to cross-check the reported capacity.
RO_TANK_CAPACITY_L = 1300.0

# "Empty veto" for starting a new recipe run: a mixer reading above this value
# contradicts the operator's "tank emptied" confirmation and blocks the start.
# Same value as the existing drain end criterion empty_threshold_liters
# (config/tank_cleaning_settings.json, config/water_cycle_settings.json).
# NOT a measured proof of emptiness: the mixer calibration is unreliable
# below ~10 L, so a reading at or below this value never replaces the
# operator confirmation.
MIXER_EMPTY_VETO_L = 0.3

# Unterhalb dieser Mixer-Menge ist der Fuellstandssensor nicht belastbar (Kalibrierung nur ab 10 L). Einzige Quelle:
# AP-08-Transferstart und AP-05-Dosierstart pruefen dagegen. KEIN Trockenlaufschutz: ein hardwareseitig sicherer
# Mindestfuellstand fuer die Zirkulationspumpen ist nicht validiert.
MIXER_SENSOR_RELIABLE_MIN_LITERS = 10.0

# Legacy Node-RED "EC_config.maxvol3" (RO/dilution correction) - a static cap
# on any *future* runtime RO correction (not a stored recipe value - see
# compute_max_possible_ro_correction_l in calculations.py and
# docs/RECIPE_AND_DOSING_RULES.md Section 4; the actual demand-driven EC
# correction is not implemented yet).
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
