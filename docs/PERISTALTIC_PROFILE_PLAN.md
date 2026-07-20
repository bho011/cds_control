# Peristaltic Pump Profile Plan (MCU-A / MCU-B)

**Status: design only.** No firmware was changed, no MCU was flashed, no
pump was moved for this document. It lives in `cds_control` (the Python/
NiceGUI side) because it documents the integration point the recipe/dosing
domain model (`domain/recipe_model.py::RunConfigSnapshot`) will eventually
need on the peristaltic-pump side - the actual firmware lives in the
separate `~/central_dosing_sys_peristaltic` repository (PlatformIO, Arduino
Mega 2560 / RAMPS 1.6 / TMC2130) and is out of scope to modify here.

## 1. Current, real state (verified against the firmware repo, not assumed)

- Two Arduino Mega 2560 MCUs, RAMPS 1.6, TMC2130 drivers, `AccelStepper`.
  4 pumps per MCU (`AMOUNT_OF_PUMPS = 4`), addressed locally as P1-P4.
- **Both MCUs currently run the identical compiled firmware image**
  (`src/main.cpp` docstring: "Both Controllers get the same script (MCU A,
  MCU B)"). There is one PlatformIO environment, `[env:megaatmega2560]`.
- Firmware-side hardening (branch `harden-serial-protocol`) is complete and
  was verified on real hardware for both MCUs: drivers are disabled by
  default at boot and only enabled for the duration of an active `DOSE`
  command (`DRIVER_ENABLE_LEVEL`/`DRIVER_DISABLE_LEVEL`), a line-based
  `PING` / `STATUS` / `DOSE Pn <ml>` / `STOP Pn` / `STOPALL` protocol exists
  at 115200 baud (see `docs/SERIAL_PROTOCOL.md` in that repo), and every
  dose is bounded by two **shared, global** safety constants:
  `MAX_SINGLE_DOSE_ML = 50.0` and `MAX_RUNTIME_MS = 30000`, explicitly
  commented as provisional ("must be re-validated during real water-only
  operation before any chemical use").
- Per-pump calibration exists structurally (`ML_PER_STEP[AMOUNT_OF_PUMPS]`,
  a `PumpState` struct per pump) but all four pumps currently share the
  *same* calibration value and the *same* global `DEFAULT_SPEED`/
  `DEFAULT_ACCEL` - i.e. the array exists, but no pump has been individually
  calibrated yet.
- There is **no MCU identity** in the protocol yet (a `PING`/`STATUS`
  response cannot currently tell a Python client which physical MCU it is
  talking to) and **no Python serial client** exists anywhere in
  `cds_control` yet - this whole integration point is unbuilt.
- Pump-to-chemical assignment (which physical pump is stock/acid/base/...)
  is explicitly not decided yet, in either repository - the firmware's own
  docstring says this mapping happens later, on the Python/host side.

## 2. Why two profiles instead of one

The recipe domain model already distinguishes two very different dosing
regimes (see `domain/recipe_model.py::RunConfigSnapshot` and
`docs/RECIPE_AND_DOSING_RULES.md`):

- **pH correction** (`volume_acid_1_ml`, `volume_acid_2_ml`, `volume_base_ml`):
  small quantities (few drops to a few ml), dosed in a closed-loop
  step-mix-remeasure pattern, tight single-dose limits.
- **EC nutrient dosing** (`nutrients_ml_per_100_l`, nutrient solutions A/B):
  larger quantities - the recipe domain's own docstring notes "typically
  around 500 ml per type per 100 l" as the expected order of magnitude for
  this dosing regime, dosed in fewer, longer pumping runs.

A single shared `MAX_SINGLE_DOSE_ML = 50.0` cannot safely serve both: too
loose for a pH drop-wise correction, and too tight to ever complete an EC
nutrient dose in one `DOSE` command. Splitting into two named profiles lets
each MCU's limits match its actual job, without pretending one number fits
both.

## 3. Proposed design

### 3.1 Shared firmware code, two PlatformIO environments

Keep one `src/main.cpp` (the actual pump-control logic, serial protocol
parser, and safety watchdog do not differ between the two jobs). Add a
compile-time profile selection via `platformio.ini` build flags, e.g.:

```ini
[env:mcu_a_ph]
extends = env:megaatmega2560
build_flags = -D PROFILE_MCU_A_PH

[env:mcu_b_ec]
extends = env:megaatmega2560
build_flags = -D PROFILE_MCU_B_EC
```

`main.cpp` then selects a profile's constants at compile time (`#ifdef
PROFILE_MCU_A_PH` / `#ifdef PROFILE_MCU_B_EC`), instead of maintaining two
divergent copies of the pump/serial logic. This directly replaces today's
"both controllers get the same script" model with two distinct, explicitly
named build targets - while the actual control code stays a single shared
implementation.

### 3.2 Per-pump configuration, generalized from what already exists

The existing `PumpState pump_state[AMOUNT_OF_PUMPS]` and
`ML_PER_STEP[AMOUNT_OF_PUMPS]` arrays are already structurally per-pump -
this only needs to be *populated* with distinct, profile-appropriate values
per pump instead of four identical entries, plus two more per-pump fields
that are currently global constants:

```cpp
struct PumpConfig {
    float ml_per_step;        // already exists as ML_PER_STEP[i], per-pump today only in name
    float speed_rpm;          // currently DEFAULT_SPEED, global for all 4 pumps
    float max_single_dose_ml; // currently MAX_SINGLE_DOSE_ML, global for all 4 pumps
    unsigned long max_runtime_ms; // currently MAX_RUNTIME_MS, global for all 4 pumps
};

constexpr PumpConfig PUMP_CONFIG[AMOUNT_OF_PUMPS] = { /* profile-specific, TBD via calibration */ };
```

No concrete numbers are proposed here for MCU-A (pH) vs. MCU-B (EC) pumps -
those require real calibration runs (see Section 5) and must not be
invented.

### 3.3 MCU identification

Add a compile-time identity string (e.g. `MCU_ID = "MCU_A_PH"` /
`"MCU_B_EC"`, set via the same build flag used for the profile) and include
it in the `PING`/`STATUS` response, e.g. `OK READY MCU_A_PH` instead of
today's plain `OK READY`. This is what lets a future Python client verify it
opened the serial port to the MCU it expected, rather than assuming the port
enumeration order is stable.

### 3.4 Later Python serial client (not built yet)

A future `services/peristaltic_client.py` (name illustrative, not decided)
would need at minimum:

- open the configured serial port(s), verify MCU identity via `PING`
  against the expected `MCU_ID` before sending any `DOSE` command,
- translate a `RunConfigSnapshot`'s calculated amounts
  (`calculated_nutrient_a_ml`, `calculated_nutrient_b_ml`, and the
  pH-side amounts once that model exists) into `DOSE Pn <ml>` commands
  using a pump-assignment table that does not exist yet (Section 1),
  raising rather than guessing if a chemical has no assigned pump,
- treat `ERR Pn LIMIT_EXCEEDED` / `ERR Pn BUSY` / `ERR Pn TIMEOUT`
  responses as hard stops, not retried automatically,
- **hook into `ProcessController.emergency_stop()`**
  (`nicegui_dashboard/process_controller.py`) so a dashboard Emergency Stop
  also sends `STOPALL` to every connected MCU - today `emergency_stop()`
  only knows about the Raspberry Pi's own GPIO actuators
  (`ActuatorManager.safe_shutdown_all()`); the peristaltic MCUs are a
  second, independent hardware surface this method does not reach yet.

This client is explicitly **not** implemented as part of this task - no
serial port is opened, no command is sent, no pump moves.

## 4. What stays firmware-side, unaffected by this document

- The existing `DOSE`/`STOP`/`STOPALL` command parsing, the per-command
  bounds check, and the runtime watchdog (`main.cpp`) are unaffected -
  Section 3.2 only asks them to read from a `PumpConfig[i]` instead of a
  single global constant.
- `docs/SERIAL_PROTOCOL.md` (in the firmware repo) remains the authority
  for the wire protocol; this document does not redefine it, only proposes
  the `MCU_ID` addition to `PING`/`STATUS`.

## 5. Calibrations still needed before any of this can run for real

All of the following are currently unknown and must be measured, not
assumed, before MCU-A/MCU-B profiles can be given real numbers:

- Per-pump `ml_per_step` for each of the 8 physical pumps individually
  (today: one shared value, copied 4x, `central_dosing_sys_peristaltic`
  README/comment: "same physical calibration value for all 4 pumps today").
- Confirmed, safe per-profile `max_single_dose_ml` and `max_runtime_ms` -
  today's `50.0` ml / `30000` ms are explicitly commented as provisional
  and validated only for whatever pump they were last tested against, not
  per-chemical or per-target-viscosity.
- The actual pump-to-chemical assignment (which physical P1-P4 on which MCU
  is stock/acid/base/addon) - explicitly not decided anywhere yet.
- Real-liquid (not just water) flow-rate/back-pressure behaviour, if
  nutrient/acid/base viscosity differs meaningfully from water.

None of these are guessed at in this document, per the task's explicit
instruction not to invent hardware/calibration parameters.
