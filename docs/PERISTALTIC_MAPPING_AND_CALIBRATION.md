# Peristaltic Mapping, Testing & Calibration Tool

Describes the standalone, terminal-only tool under `services/peristaltic/`
and `scripts/peristaltic_calibration_cli.py`: discovering serial ports,
assigning them to the two peristaltic controllers, running water-only pump
tests, and deriving calibration candidates. This does not name any specific
operating company or site.

**Not part of this tool:** no Dashboard integration, no `ProcessController`/
state-machine integration, no automatic chemical dosing, no firmware
changes, no integration into the mixing process. It is a separate,
manually-run diagnostic/calibration utility for the two Arduino Mega
peristaltic controllers described in `~/central_dosing_sys_peristaltic`
(read-only reference from this repo's point of view - never modified,
built, or flashed here).

## 1. MCU-A / MCU-B mapping

Two identical Arduino Mega 2560 controllers (RAMPS 1.6, TMC2130, 4 pumps
each, addressed locally as `P1`-`P4`) are assigned two different jobs on
the Python/host side - the firmware itself has no concept of this split
(see `docs/PERISTALTIC_PROFILE_PLAN.md` in this repo for the deeper
firmware-side design discussion):

| Controller | Role | P1 | P2 | P3 | P4 |
|---|---|---|---|---|---|
| `MCU_A` | pH | `ph_acid` | `ph_base` | `unassigned` | `unassigned` |
| `MCU_B` | EC | `nutrient_a_1` | `nutrient_a_2` | `nutrient_b_1` | `nutrient_b_2` |

Stored in `config/peristaltic_mapping.json` (schema version 1, validated by
`services/peristaltic/models.py::validate_mapping_dict`): exactly the
controllers `MCU_A`/`MCU_B`, exactly the pumps `P1`-`P4` per controller, no
chemical role used twice except the literal string `"unassigned"` (which
may repeat freely), `port` is `null` or a string, `baudrate` must be
exactly `115200`. The real serial ports are unknown at the time of writing
and are **never invented** - both ports start as `null` and are filled in
only through the `map` subcommand, after the operator has physically
verified which cable goes where.

### Why MCU-B uses pump pairs

MCU-B doses the two EC nutrient stock solutions (Nutrient Solution A and
B), each split across two physical pumps (`nutrient_a_1`/`nutrient_a_2` and
`nutrient_b_1`/`nutrient_b_2`). Testing and eventually dosing both pumps of
one nutrient solution simultaneously is a real, expected operating mode -
that is exactly what `pair-test` exists for.

### Why MCU-A never gets a double assignment

MCU-A doses pH correction (acid/base), always in a closed-loop,
step-mix-remeasure pattern with small quantities. Acid and base must never
run at the same time - dosing both simultaneously could actively fight each
other and waste chemical, or in a real (non-water) run, create an unsafe
mixing situation. This is why `pair-test`/`all-four-test` reject **every**
pump combination on `MCU_A` outright (see Section 5) - only `test` and
`calibrate` (strictly one pump at a time) are available there.

### No automatic MCU identity

The firmware's `PING`/`STATUS` responses do not currently include any MCU
identity string (see `docs/PERISTALTIC_PROFILE_PLAN.md`, Section 3.3 - a
`MCU_ID` field is proposed there but not implemented). This means the `map`
subcommand's successful `PING`/`STATUS` exchange with a given port only
confirms that *some* peristaltic controller answers on that port - it can
**never** automatically confirm whether that is really `MCU_A` or `MCU_B`.
The tool prints an explicit disclaimer every time and requires the operator
to verify the physical wiring (e.g. unplugging one controller at a time).

## 2. Serial protocol summary

115200 baud, 8N1, line-based. Verified against `docs/SERIAL_PROTOCOL.md`
**and** the real firmware source (`src/main.cpp`) in the separate,
read-only firmware repository:

- `STATUS` reports each pump as `IDLE` or `BUSY` (never `RUNNING`).
- `Pn RUNNING ... ml remaining` and `DONE Pn ...` are unsolicited - they can
  arrive at any time, independent of whatever command was just sent
  (pumps run in parallel, there is no global busy lock).
- `ERR Pn BUSY` / `ERR Pn LIMIT_EXCEEDED` / `ERR <token> INVALID_PUMP` /
  `ERR INVALID_COMMAND` are always synchronous replies to the command just
  sent. `ERR Pn TIMEOUT` is always asynchronous (the firmware's own
  30-second runtime watchdog).
- `STOP`/`STOPALL` never produce a `DONE` for the pump(s) they stop
  (`hard_stop_pump()` has no `Serial.println` at all) - the client and CLI
  never wait for `DONE` on a pump after it has been stopped.

Current firmware limits (provisional, defined in the firmware, **not**
changed by this tool): `MAX_SINGLE_DOSE_ML = 50.0`. Beyond that, `MCU_A`
and `MCU_B` are **not assumed to share one firmware profile** - see
Section 2a for what is actually confirmed to be flashed on each controller
today, and Section 7 for how `ML_PER_STEP` per pump is tracked.

## 2a. Firmware profiles vs. calibration data

Two related but distinct concerns, tracked in two separate files:

- **What was actually flashed** - `config/peristaltic_firmware_profiles.json`
  (schema version 1, validated by
  `services/peristaltic/firmware_profiles.py::validate_firmware_profiles_dict`),
  one entry per controller: `status` (`"confirmed"`/`"unconfirmed"`),
  `profile_id`, `microsteps`, `speed_rpm`, `acceleration_steps_per_s2`,
  `max_runtime_ms`, and a per-pump `firmware_ml_per_step`. `MCU_A` and
  `MCU_B` may hold **different** profiles here - nothing assumes they were
  built or flashed identically (see `docs/PERISTALTIC_PROFILE_PLAN.md` for
  the separate, still-undone firmware-side build-target split this file
  does not depend on).
- **What trials were run** - `calibration_data/peristaltic_calibration.json`
  (Section 8), which records, per trial, which `firmware_ml_per_step_used`
  was assumed to be active *at the time of that trial*.

Current, real state (as of the 8-microstep re-flash of `MCU_B`):

| Controller | `status` | `profile_id` | `microsteps` | `speed_rpm` | `acceleration_steps_per_s2` | `max_runtime_ms` | `firmware_ml_per_step` (all 4 pumps) |
|---|---|---|---|---|---|---|---|
| `MCU_A` | `unconfirmed` | `null` | `null` | `null` | `null` | `null` | `null` |
| `MCU_B` | `confirmed` | `mcu_b_ec_8_microsteps_v1` | `8` | `120` | `2000` | `30000` | `0.000191096` |

`MCU_A`'s profile is **currently unconfirmed** - no firmware change has
been verified for it, and none of its values are guessed here.

`scripts/peristaltic_calibration_cli.py::cmd_calibrate` resolves
`firmware_ml_per_step_used` for a new trial **exclusively** from this file
(`services/peristaltic/firmware_profiles.py::resolve_firmware_ml_per_step`),
before doing anything else - no `--current-ml-per-step` override exists
anymore and no global default is used. It aborts with a clear message and
**no pump movement at all** if the controller's profile is missing,
`unconfirmed`, or the specific pump's `firmware_ml_per_step` is `null`
(this is checked per pump, not just per controller - a `confirmed`
controller with one still-unmeasured pump still blocks calibration for
that one pump).

A `candidate_ml_per_step` computed from calibration trials (Section 7) is
**never** automatically written into this profile file or treated as a
confirmed/flashed firmware value - only a person who has verified a real
firmware change may edit `config/peristaltic_firmware_profiles.json`
(there is currently no CLI subcommand that writes it).

## 3. Connection lifecycle and fail-closed desynchronization

`services/peristaltic/serial_client.py::PeristalticSerialClient` models an
explicit state machine: `CLOSED -> OPENING -> OPEN -> DESYNCED`/`CLOSING ->
CLOSED`. `open()` always runs, in this exact order: open the port with
`exclusive=True`, `reset_input_buffer()`, reset internal queues, start the
background reader thread, drain any boot/diagnostic lines, then run its own
`STOPALL` -> `PING` -> `STATUS` handshake. A failed handshake goes back to
`CLOSED`, never `DESYNCED` - a connection that was never successfully
synced cannot "lose" sync.

Once `OPEN`, three things mark the client `DESYNCED` and trigger a
best-effort internal `STOPALL` (write-only, no reply awaited, never the
strict public `stop_all()`): a command timeout, a connection error detected
by the reader thread, or a stale/unmatched reply found in the queue right
before a new command would be sent. From that point on, **every** further
command is rejected immediately (no I/O attempted at all) until the caller
explicitly closes and reopens the connection - a late reply belonging to an
old, already-timed-out command can therefore never be misread as the reply
to a new one.

## 4. Water-only safety rules

`MAX_INITIAL_TEST_DOSE_ML = 10.0` (`services/peristaltic/calibration.py`) -
deliberately far below the firmware's own 50 ml limit. Allowed: 0.1-10.0 ml
per pump per request. Blocked, with a clear message and no silent
clamping: `0`, negative values, `NaN`/`Infinity`, anything above 10 ml, and
any pump outside `P1`-`P4`. There is no `--force`/`--unsafe` flag anywhere
in this tool.

Before every pump movement, the CLI prints controller, port, local pump,
chemical role, requested amount, and an explicit "WATER TEST" notice, then
requires the operator to type the exact phrase `WATER TEST CONFIRMED` - a
plain `y`/`n` is accepted only for the non-movement `map` save
confirmation.

Before every `DOSE`: query `STATUS` (logged), send `STOPALL`
unconditionally, query `STATUS` again, and only proceed if every pump on
that controller is now `IDLE`. On any `ERR`, timeout, disconnect, or
unexpected response during an attempt: immediate best-effort `STOPALL`, the
attempt is logged as failed, and there is no automatic retry.

## 5. Parallel-test restriction

`pair-test` and `all-four-test` never touch `MCU_A` (see Section 1). On
`MCU_B`, only three pump selections are accepted:
`{P1,P2}`, `{P3,P4}` (both `pair-test`), or all four (`all-four-test`) -
enforced by `services/peristaltic/calibration.py::validate_parallel_pump_selection`.
Any other combination (e.g. `{P1,P3}`) is rejected. Single-pump `test`/
`calibrate` remain available for every pump on both controllers regardless
of this restriction. Cross-MCU pair tests (one pump per controller) are out
of scope - each CLI invocation only ever talks to one controller/one serial
connection.

`pair-test`/`all-four-test` are implemented and hardware-free tested in
this task, but are only meant to be run **after** the individual pumps
involved have already been calibrated at least once (see the CLI's own help
text) - that is a documented recommendation, not a programmatically
enforced gate.

## 6. Workflow: discover -> map -> check -> test -> calibrate -> (verify, prepared)

```bash
python scripts/peristaltic_calibration_cli.py discover
python scripts/peristaltic_calibration_cli.py map
python scripts/peristaltic_calibration_cli.py check --controller MCU_B
python scripts/peristaltic_calibration_cli.py stop-all --controller MCU_B
python scripts/peristaltic_calibration_cli.py test --controller MCU_B --pump P1 --ml 5
python scripts/peristaltic_calibration_cli.py calibrate --controller MCU_B --pump P1 --requested-ml 5
python scripts/peristaltic_calibration_cli.py pair-test --controller MCU_B --pumps P1 P2 --ml-each 5
python scripts/peristaltic_calibration_cli.py all-four-test --controller MCU_B --ml-each 2
```

- `discover`: lists serial ports, prefers `/dev/serial/by-id/...` stable
  paths. Opens nothing, moves nothing.
- `map`: lets the operator assign a discovered (or manually typed) port to
  each controller, runs `PING`/`STATUS` to confirm *a* connection works
  (with the no-automatic-identity disclaimer from Section 1), saves only
  after an explicit y/n confirmation. No pump movement.
- `check` / `stop-all`: connection/status diagnostics and an explicit
  `STOPALL`. No pump movement.
- `test`: one safe, confirmed, logged single-pump water dose.
- `calibrate`: one `test`-equivalent dose, then interactively records the
  operator's real measured amount (+ optional measurement method / water
  temperature) and computes a calibration candidate (Section 7).
- `pair-test` / `all-four-test`: the restricted parallel tests from
  Section 5.

Recommended first real-hardware sequence (documented here only, **never**
executed automatically by this tool): fill each line separately with water
first; 3 trials at 5 ml per pump, candidate from the median of those
trials; then 3 verification trials at 10 ml; only then attempt a
`pair-test`.

## 7. Calibration formula

For a single trial, `requested_ml` is what was asked of the firmware and
`measured_ml` is what the operator actually measured:

```text
candidate_ml_per_step = firmware_ml_per_step_used * measured_ml / requested_ml
```

Example: 5.0 ml requested, 4.5 ml measured, current firmware value
`0.000095548` -> candidate `= 0.000095548 * 4.5 / 5.0`.

Three distinct values are tracked per pump and never conflated:

- `firmware_ml_per_step_used` - what the trial *assumed* was actually
  flashed into the firmware at that time. For `calibrate`, this is
  resolved exclusively from the confirmed controller/pump entry in
  `config/peristaltic_firmware_profiles.json` (Section 2a) - **never**
  automatically replaced by a previously computed candidate, and never a
  silent global default. `FIRMWARE_DEFAULT_ML_PER_STEP = 0.000095548` in
  `services/peristaltic/calibration.py` still exists only as a fallback for
  direct `add_trial()` calls without an explicit value (e.g. in tests) - it
  is no longer read by the `calibrate` command.
- `candidate_ml_per_step` - the computed suggestion. Never claimed to be
  final or correct.
- `verified_ml_per_step` - stays `null` in this task; no `verify` workflow
  is implemented yet, only prepared (see Section 8).

Per pump, across all trials sharing the *same* `requested_ml` **and** the
same `firmware_ml_per_step_used` (trials for a different requested amount,
e.g. later 10 ml verification runs, or trials logged against a different
firmware value, are never pooled together):
count, mean and median of `measured_ml`, standard deviation (`None` below
two trials), mean absolute deviation, and `mean_absolute_relative_error_percent`
(`mean(|measured - requested| / requested * 100)` across trials) - see
`services/peristaltic/calibration.py::compute_pump_stats`.

## 8. Calibration state and file

`calibration_data/peristaltic_calibration.json` (schema version 1) holds,
per controller/pump: `role`, `status`, `candidate_ml_per_step`,
`verified_ml_per_step`, `verified_at_ml` (volumes a verification has
succeeded at - empty for now), `last_updated`, and the full `trials`
history (an additive extension beyond the task's illustrative example
schema, needed so statistics can be recomputed across multiple `calibrate`
runs).

States: `not_calibrated -> candidate -> verified`. A single `calibrate` run
can only ever move a pump to `candidate`, **never** `verified` - there is
no `verify` CLI subcommand in this task; the state exists in the schema and
is validated, but nothing sets it automatically. Verification (a defined
sequence of confirmatory trials, e.g. at 10 ml after calibrating at 5 ml)
is future work.

## 9. Logging

Every run of any hardware-touching subcommand creates three files under
`logs/peristaltic/YYYYMMDD/` (never overwriting an existing file - a
numeric suffix is appended on collision): a CSV with one row per
pump/attempt, a raw text log with every transmitted and received line
timestamped (`TX PING`, `RX OK READY`, ...), and a JSON run summary.
CSV columns: `timestamp_utc, session_id, controller, controller_role,
serial_port, pump, pump_role, command, requested_ml, measured_ml,
measurement_method, water_temperature_c, started_at, finished_at,
elapsed_seconds, result, firmware_reported_ml, firmware_ml_per_step_used,
candidate_ml_per_step, error_code, notes, raw_log_file` (the last two
firmware-value columns are deliberately named to match Section 7's
`firmware_ml_per_step_used`/`candidate_ml_per_step` split, not the more
ambiguous "current" wording).

## 10. Explicitly not done here

`config/peristaltic_firmware_profiles.json` (Section 2a) only records,
Python-side, which firmware values are confirmed to be flashed per
controller/pump - it does **not** implement the separate, still-undone
firmware-side build-target split (`MCU_A_PH`/`MCU_B_EC` PlatformIO
environments with compile-time profile selection - see
`docs/PERISTALTIC_PROFILE_PLAN.md`). Also not done here: no real chemical
dosing, no Dashboard/recipe/state-machine integration, no verification
workflow with real numbers, no invented port paths or calibration values.
