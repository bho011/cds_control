# Central Dosing System – Python Control Layer

*This file is bilingual: English first, [German version below](#central-dosing-system--python-control-layer-deutsch). / Diese Datei ist zweisprachig: Englisch zuerst, deutsche Version weiter unten.*

Status: 2026-07-17
Project status: development and validation phase
Goal: safe, traceable, and modularly extensible control logic for the water-based CDS process.

> **Safety status in one sentence:** Hardware execution is disabled by default in this repository (`hardware_execution_enabled: false`); of the 15 configured GPIO outputs, only **three** are currently validated on the real physical system (see Section 4). All other outputs are software-addressable but without confirmed real-world effect.

---

## 1. Overview

This repository contains the Python implementation of the control and visualization layer for a Central Dosing System (Raspberry Pi, GPIO relays, OPC-UA sensors, MQTT/Node-RED). The current focus is the safe, water-only base process:

```text
RO water → Mixing Tank → Sensor box circulation → Drain
```

Chemical dosing and peristaltic pump control are not yet active. These functions will be added after further hardware, sensor, and safety validation. Recipe execution is so far only meaningfully connected to the actual process start for the pure water target value (fill volume, sensor circulation) — see Section 15. The EC/pH/dosing values in the recipe remain purely descriptive, with no consumer in the code.

The control logic was migrated from individual test scripts into a modular structure. `main.py` is the central entry point for preflight, water cycle, dashboard, calibration, and status checks.

**Target platform:** Raspberry Pi (Raspberry Pi OS), Python 3.11, `.venv`-based setup. GPIO control runs via `gpiozero` with the `lgpio` pin-factory backend.

---

## 2. Architecture / data flow

```text
                     ┌──────────────────────┐
                     │   OPC-UA-Server      │
                     │ (RO/Mixing-Tank,     │
                     │  pH, EC, Temp, DO)   │
                     └──────────┬───────────┘
                                │ OPC-UA read (timeout-protected)
                                ▼
                     mqtt_sensor_bridge.py  ──► MQTT: cds/status/sensors
                                                        │
gpio_config.py ──► hardware/ ──► process/ ──► statemachine/            │
   (Pin-Mapping)   (DigitalOutput,  (refill/drain/   (FillAndMeasure)  │
                    ActuatorManager) sensor_circulation/               │
                                     auto_circulation/                 │
                                     manual_drain_jog/                 │
                                     tank_cleaning)                    │
        │                 │                │                           │
        └─────────────────┴────────────────┴──► MQTT: cds/status/process
                                                        │
                                          ┌─────────────┴──────────────┐
                                          ▼                            ▼
                                  Node-RED Dashboard         NiceGUI Dashboard
                                  (existing HMI)             (nicegui_dashboard/)
```

`main.py` is the only supported entry point for all productive workflows (preflight, water cycle, dashboard, calibration, safe drain, status checks). Manual Drain Jog and Tank Cleaning are started exclusively via the NiceGUI dashboard, not via `main.py`.

---

## 3. Current feature status

Currently implemented:

- MQTT sensor bridge for OPC-UA sensor data (read timeout + QoS-1 publish with acknowledgment)
- NiceGUI dashboard for visualization and process control (revised 4-column layout, see Section 14)
- modular Water Cycle process (Refill → sensor box circulation → Drain)
- automatic circulation control (`process/auto_circulation.py`) – level-dependent on/off switching of the circulation pumps, reused by Water Cycle **and** Tank Cleaning
- Manual Drain Jog – dashboard-controlled manual draining with a server-side 30-second watchdog
- Tank Cleaning – automated cleaning cycle (Fill → Hold → Drain), see Section 13
- central preflight check including a GPIO conflict check against the Node-RED GPIO helper, via CLI **and** via the "Run Preflight Check" button in the dashboard (Dev Info)
- recipe editor in the dashboard with three favorites and JSON storage
- mixing tank calibration with pump-driven fill/drain segments, a per-second trace log, and an offline-recomputable formula (`--analyze`), see Section 16
- central system configuration for OPC-UA, MQTT, and mixer-level calibration (`config/system_config.json`), now also used by the dashboard MQTT layer and the state-machine calibration (see Section 7)
- central, race-free process locking for starting/stopping Fill-and-Measure, Manual Drain Jog, and Tank Cleaning, plus a cross-process hardware lock (`fcntl.flock`, `hardware/actuator_manager.py`) and a non-blocking, asynchronous Emergency Stop (see Section 8)
- unified watchdog/progress checking for all fill/drain phases (`process/watchdog.py`) and settings schema validation with collected errors instead of aborting on the first hit (`services/settings_validation.py`), both with `pytest` test coverage (see Section 9)
- recipe editor actually connected to process start: the target volume and sensor circulation from the active recipe now genuinely drive the next Fill-and-Measure run instead of only the display (see Section 15)
- typed recipe/dosing domain model (`domain/recipe_limits.py`, `domain/recipe_model.py`): central 180 L/185 L tank-volume limits, EC nutrient Solution A/B split (B always calculated as `100 - A`), an immutable, validated `RunConfigSnapshot`, and a `recipes/dashboard_recipes.json` schema migration with atomic/locked writes — see Section 15 and `docs/RECIPE_AND_DOSING_RULES.md`
- recipe/RunOptions data model hardened: `StoredRecipe` now holds only fachliche Rezeptwerte (a fixed RO correction is no longer a recipe value at all), per-run-only choices (sensor circulation, drain after process) moved to a separate `RunOptions` type that is never persisted and always defaults to off, strict `type(value) is bool`/finite-number/non-integral-float validation closes several silent-type-coercion gaps, recipe-book metadata (`schema_version`/`active_slot`/`favorite.slot`) is now fail-closed validated, and every operational fill target is capped at 185 L (no more 200 L defaults) — see Section 15 and `docs/RECIPE_AND_DOSING_RULES.md`
- favorites made genuinely persistent and interactive: F1/F2/F3 are now real buttons that activate a favorite slot (never start a process, never touch hardware), the recipe-book metadata contract is fully fail-closed (unknown future schema versions, duplicate slots, out-of-range slots are rejected instead of silently self-healed), Addon 1/Addon 2 were removed from the active recipe model entirely (the real CDS process only has Nutrient Solution A/B — old values are kept for audit under `legacy_recipe_values`), and a running process's status now keeps showing the recipe it was actually started with even if a different favorite is activated while it runs — see Section 15 and `docs/RECIPE_AND_DOSING_RULES.md`

The modular Water Cycle starts correctly on the software side. A full real hardware run after the latest refactor is still outstanding. Manual Drain Jog and Tank Cleaning are likewise not yet validated on real hardware (see Section 17).

---

## 4. Hardware validation status (important!)

Only the following outputs have been tested on the real physical system with confirmed effect:

| Function              | Config key              | GPIO (BCM) | Physical pin | Status              |
|------------------------|------------------------|:----------:|:---------------:|----------------------|
| Mixer Refill Pump       | `mixer_refill_pump`    | 20         | 38               | validated            |
| Supply/Test Valve 6     | `test_supply_valve_6`  | 6          | 31               | validated            |
| Drain Valve 0           | `valve_0_drain`        | 21         | 40               | validated            |

All other entries in `gpio_config.py` (`contactor_0/5`, `transfer_pump`, `mixing_circulation_pump`, `sensor_circulation_pump`, `valve_1`–`valve_9`) are addressable in code but **not** confirmed by a real hardware test. In particular:

- **Transfer Pump** (`transfer_pump`, GPIO26) is already used in `calibration_mixing_tank.py`, `main_safe_drain.py`, and now also in `process/tank_cleaning.py`, but its electrical wiring is, per the project status, not yet conclusively confirmed.
- **Valve 5** is noted in a `gpio_config.py` comment as not reliably sealing.
- **Tank Cleaning** additionally drives `mixing_circulation_pump` and `sensor_circulation_pump` – also without a real hardware test yet. The settings file (`config/tank_cleaning_settings.json`) is therefore deliberately set to a reduced **40-liter test volume** instead of the later 200-liter target, so the first hardware test can be run without major water consumption/risk.

Before a new output is used productively: switch it on the real system, confirm the effect visually/physically, only then include it in an automated sequence.

---

## 5. Central entry points

All main functions should be started via `main.py`.

```bash
python main.py --help
python main.py preflight
python main.py gpio-check
python main.py water-cycle
python main.py calibrate-mixer
python main.py dashboard
python main.py sensor-bridge-check
python main.py safe-drain
```

```bash
cd ~/cds_control
source .venv/bin/activate
python main.py preflight
```

The combined preflight must succeed before every hardware test.

Manual Drain Jog and Tank Cleaning have no dedicated `main.py` command – they are started exclusively via the corresponding buttons in the NiceGUI dashboard (`python main.py dashboard`).

---

## 6. Project structure

```text
cds_control/
├── main.py
├── config/
│   ├── system_config.json
│   ├── water_cycle_settings.json
│   ├── calibration_settings.json
│   ├── process_settings.json
│   └── tank_cleaning_settings.json
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   ├── water_cycle.py
│   ├── auto_circulation.py
│   ├── manual_drain_jog.py
│   ├── tank_cleaning.py
│   ├── watchdog.py
│   └── background_process.py
├── services/
│   ├── system_config.py
│   ├── mqtt_publisher.py
│   ├── process_run_logger.py
│   ├── sensor_snapshot.py
│   └── settings_validation.py
├── hardware/
│   ├── digital_output.py
│   └── actuator_manager.py
├── nicegui_dashboard/
│   ├── app.py
│   ├── cds_controller.py
│   ├── process_controller.py
│   ├── recipe_store.py
│   ├── mqtt_topic_reader.py
│   ├── pages/
│   └── static/
├── scripts/
│   └── check_gpio_conflicts.py
├── recipes/
│   └── dashboard_recipes.json
├── tests/
├── logs/
├── calibration_data/
├── mqtt_sensor_bridge.py
├── calibration_mixing_tank.py
├── main_safe_drain.py
├── preflight_check.py
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
```

---

## 7. Configuration

Central technical system values live in `config/system_config.json`:

```json
{
  "opcua": {
    "endpoint": "opc.tcp://10.8.0.62:14840",
    "read_timeout_seconds": 3.0
  },
  "mqtt": {
    "host": "localhost",
    "port": 1883,
    "sensor_topic": "cds/status/sensors",
    "process_topic": "cds/status/process",
    "qos": 1,
    "publish_timeout_seconds": 2.0
  },
  "mixer_level_calibration": {
    "volume_liters": 200.0,
    "factor": 0.610566,
    "offset": -26.093067,
    "status": "fitted_ge10L_20260703_20260709_sessions",
    "fit_r2": 0.996399,
    "fit_max_abs_error_liters": 5.698,
    "fit_source": "calibration_mixing_tank.py --analyze ... (see Section 16)"
  }
}
```

> Note: this file is now the single source of truth for the OPC-UA endpoint, MQTT connection, and mixer calibration — `mqtt_sensor_bridge.py`, `calibration_mixing_tank.py`, `preflight_check.py`, as well as the dashboard MQTT layer (`services/mqtt_publisher.py`, `nicegui_dashboard/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py`) and the calibration-factor fallback in `statemachine/fill_and_measure_state_machine.py` now all read from `get_mqtt_config()`/`get_mixer_level_calibration()` instead of their own hardcoded values.

Further settings files, one per process:

- `config/water_cycle_settings.json` – Water Cycle
- `config/calibration_settings.json` – mixing tank calibration
- `config/process_settings.json` – Fill-and-Measure
- `config/tank_cleaning_settings.json` – Tank Cleaning (target volume, hold time, auto-circulation thresholds, drain parameters, its own `hardware_execution_enabled` switch)

---

## 8. Safety principles

- Hardware execution is disabled by default in the repository (`hardware_execution_enabled: false`).
- Must be deliberately set to `true` locally for real tests — never commit with `true`.
- Every process settings file (Water Cycle, Fill-and-Measure, calibration, Tank Cleaning) has its **own** `hardware_execution_enabled` switch and confirmation text.
- `python main.py preflight` must succeed before every hardware run.
- Node-RED must not block any GPIOs that Python needs for the CDS process.
- Chemical dosing and peristaltic pump control are not currently active.
- Sensor values are not blindly trusted as ground truth (filtering, plausibility checks, confirm samples).
- Actuators are switched off via central safe-shutdown paths (`ActuatorManager.safe_shutdown_all()`); errors during shutdown are logged, not swallowed.
- Manual calibration drain functions have a safety timeout (`calibration_drain_max_seconds`).
- `KeyboardInterrupt` is handled uniformly across all Water Cycle phases and aborts the rest of the run in a controlled way.
- Manual Drain Jog and Tank Cleaning run as background threads whose loops check a `threading.Event` roughly every 0.5s — an Emergency Stop from the dashboard therefore interrupts them within under a second, instead of waiting for the current phase to finish.
- Start/stop of Fill-and-Measure, Manual Drain Jog, and Tank Cleaning are atomically guarded by the internal `ProcessController` lock: two concurrent start calls (a dashboard click plus a script started in parallel) can no longer both get through — previously a TOCTOU race between the state check and the actual start.
- Additionally, a cross-process file lock (`fcntl.flock`, `hardware/actuator_manager.py`, `logs/.hardware.lock`) prevents parallel hardware access by two independent Python *processes* (not just threads within the same process) — e.g. the dashboard plus a directly started calibration or safe-drain script.
- Emergency Stop is implemented asynchronously (`ProcessController.emergency_stop()`): signalling (state-machine abort, `request_stop()`, actuator shutdown) happens immediately under the lock; waiting for the thread join then runs outside the lock in a worker thread — the dashboard stays responsive for other connected clients in the meantime, instead of freezing for up to ~7s.

Default state in the configs:

```json
"hardware_execution_enabled": false
```

This means no GPIO outputs are initialized or switched as long as hardware execution is not explicitly enabled.

---

## 9. Preflight and GPIO conflict check

The combined preflight checks: project files, Python syntax, GPIO configuration, duplicate GPIO assignments (**blocks the Water Cycle start**, no longer just a warning), active system services, MQTT reachability, OPC-UA readability, the current sensor MQTT payload structure, Node-RED GPIO conflicts.

```bash
python main.py preflight
python main.py gpio-check
```

Also triggerable from the dashboard: the "Run Preflight Check" button in Dev Info writes the full result to the process log (runs in a worker thread, does not block the dashboard for other users while running).

Critical CDS GPIOs for Python:

```text
GPIO20 = mixer_refill_pump
GPIO21 = valve_0_drain
GPIO22 = sensor_circulation_pump
GPIO26 = transfer_pump
```

Node-RED must not block these pins. If Node-RED only operates independent functions such as the RO machine, that is fine as long as no CDS GPIOs are blocked.

### Troubleshooting

| Preflight error | Likely cause | Next step |
|---|---|---|
| MQTT unreachable | broker not started | `systemctl status mosquitto` |
| OPC-UA not readable | server unreachable / wrong endpoint | check `config/system_config.json`, check network |
| GPIO conflict with Node-RED | Node-RED flow uses the same pin | check the Node-RED flow's pin assignment, `scripts/check_gpio_conflicts.py` |
| Sensor payload invalid | `cds-sensor-bridge.service` not running/hung | `journalctl -u cds-sensor-bridge.service -f` |

### Automated tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers locking (`nicegui_dashboard/process_controller.py`, the cross-process
lock in `hardware/actuator_manager.py`), watchdog/progress checks
(`process/watchdog.py`), and settings validation (`services/settings_validation.py`,
including all four real `config/*.json` files) — no GPIO/OPC-UA is touched in
the process. `requirements-dev.txt` (just `pytest` on top of
`requirements.txt`) is deliberately kept separate from the production
`requirements.txt`.

---

## 10. Sensor bridge

`mqtt_sensor_bridge.py` reads sensor data via OPC-UA and publishes it over MQTT on `cds/status/sensors` (RO/mixing-tank level, EC, pH, water temperature, dissolved oxygen, bridge/error status). OPC-UA reads are timeout-protected; MQTT publishing runs with QoS 1 and acknowledgment.

The bridge now also detects an OPC-UA session that died mid-flight (several consecutive cycles where all sensors fail at once) and automatically reconnects, with backoff (5s, doubling up to 30s). Previously a dead session went unnoticed — the process would then keep running indefinitely, publishing only errors/`null` values, without ever recovering on its own.

```bash
systemctl status cds-sensor-bridge.service
sudo systemctl restart cds-sensor-bridge.service
journalctl -u cds-sensor-bridge.service -f
mosquitto_sub -h localhost -t cds/status/sensors -v
```

---

## 11. Water Cycle process

```text
process/water_cycle.py         → orchestrator
process/refill.py              → fill the mixing tank
process/sensor_circulation.py  → circulate through the sensor box
process/drain.py               → drain the mixing tank
process/auto_circulation.py    → level-dependent circulation pump control
process/common.py              → shared helper functions
```

```bash
python main.py water-cycle
```

The Water Cycle automatically runs preflight first; if it fails, the process does not start.

```text
1. safety confirmation
2. check sensor payload
3. refill to target (circulation pumps switch on automatically above auto_circulation_start_liters)
4. optional sensor box circulation
5. optional drain phase (circulation pumps switch off automatically below auto_circulation_stop_liters)
6. safe shutdown
```

> The modular Water Cycle still needs one full real hardware test after the latest refactor.

---

## 12. Manual Drain Jog

Dashboard maintenance function for controlled manual draining, independent of the Water Cycle.

```text
process/manual_drain_jog.py → ManualDrainJog
```

Flow:

- "Hold to Drain" button in the dashboard: the drain valve opens, a brief valve settle time, then the transfer pump turns on.
- The pump runs as long as the button is held.
- Releasing stops it immediately (dead-man principle).
- A server-side watchdog additionally stops it after 30 seconds at the latest, even if the connection to the browser/websocket hangs.

Runs as its own background thread with `threading.Event` control, so an Emergency Stop reliably takes effect at any time. Mutually exclusive with Fill-and-Measure and Tank Cleaning – only one of the three processes can be active at a time.

---

## 13. Tank Cleaning

Automated cleaning cycle for the mixing tank, e.g. after a mixing run.

```text
process/tank_cleaning.py       → TankCleaningController
config/tank_cleaning_settings.json → settings
```

Flow (three phases, all visible via MQTT on `cds/status/process`):

```text
1. FILL    Mixer Refill Pump fills to the target volume (target_fill_total_liters)
2. HOLD    fixed hold time (cleaning_hold_seconds, default 300 s)
3. DRAIN   Transfer Pump + Drain Valve drain until the sensor confirms "empty"
```

The same `AutoCirculationController` logic as in the Water Cycle runs throughout FILL, HOLD, and DRAIN: the circulation pumps (`mixing_circulation_pump`, `sensor_circulation_pump`) switch on automatically once the tank exceeds `auto_circulation_start_liters` (default 30 L), and off again below `auto_circulation_stop_liters` (default 25 L) – even during the drain. The 300-second hold time itself only starts once the target volume is sensor-confirmed as reached, not already when the pumps switch on at 30 L.

Safety:

- Its own `hardware_execution_enabled` switch + confirmation text in `config/tank_cleaning_settings.json`, independent of the other processes.
- Every phase loop checks a `threading.Event` every 0.5s – the Stop button or Emergency Stop therefore take effect almost immediately, not only at the end of the current phase.
- Fill phase uses the same safety checks as `process/refill.py` (timeout, progress monitoring, max tank level, minimum RO amount).
- Drain phase uses the same computed timeout as `process/drain.py`.
- `safe_shutdown` switches off all involved actuators on every exit path (success, abort, error).

**Current test state:** `config/tank_cleaning_settings.json` is set to a reduced **test volume of 40 L** (instead of the later 200 L), so the first real hardware test can be run without major water consumption. `hardware_execution_enabled` is `false` and must be deliberately set to `true` only after a physical check of the water path, before a real run.

Dashboard: "Start Tank Cleaning" button in the "Process Control → Tank Cleaning" section, uses the same confirmation field as "Start Process". Mutually exclusive with Fill-and-Measure and Manual Drain Jog.

---

## 14. NiceGUI dashboard

The dashboard was rebuilt into a 4-column layout that needs no scrolling:

```text
┌─────────────┬───────────────────┬──────────────────────┬────────────────┐
│ System      │ Current Process   │ Process Control      │ Sensor Values  │
│ Status      │ State             │  - Safety            │                │
│             │                   │    Confirmation      │ Tanks / Levels │
│ Actuators / │ Recipe /          │  - Maintenance       │ (RO + Mixing,  │
│ Outputs     │ Setpoints         │    (Manual Drain Jog)│  untereinander │
│             │                   │  - Tank Cleaning     │  gestapelt)    │
└─────────────┴───────────────────┴──────────────────────┴────────────────┘
```

(The last column's German labels mean "stacked vertically" — the RO and Mixing tank gauges sit one above the other, see the bullet below.)

- **System Status / Actuators**: only the outputs actually in use (Mixer Refill Pump, Supply Valve 6, Drain Valve 0, Transfer Pump, Mixing/Sensor Circulation Pump, Valve 1–5). Unused pins (`contactor_0/5`, `valve_7/8/9`) have been removed from the display so the card isn't cluttered unnecessarily.
- **Process Control**: Safety Confirmation (one shared confirmation field for Start Process **and** Tank Cleaning) → Emergency Stop → Maintenance (Manual Drain Jog) → Tank Cleaning.
- **Dev Info**: the status badges that used to be permanently visible (RASPI LIVE / PYTHON CORE / Update) and the process log are now hidden behind a "Dev Info" button in the header and open as a dialog – diagnostic info that's unnecessary for the end user no longer clutters the main view.
- **Sensor Values / Tanks**: its own right-hand column; the RO and Mixing tank gauges are stacked vertically (no longer side by side) and have a stable width independent of how many decimal places the fill level shows.

```bash
sudo systemctl restart cds-nicegui-dashboard.service
systemctl status cds-nicegui-dashboard.service --no-pager
python main.py dashboard   # manual start
```

The UI is an HMI/visualization layer plus start/stop control for Fill-and-Measure, Manual Drain Jog, and Tank Cleaning. The actual process logic stays in the Python process modules (`process/`, `statemachine/`), not in the UI code.

---

## 15. Recipe editor

Recipe editor with **three permanently stored, independent favorites** under `recipes/dashboard_recipes.json` (schema version 4). Typed domain model in `domain/recipe_limits.py` (central constants + calculation/validation rules) and `domain/recipe_model.py` (`StoredRecipe`, `RecipePreview`, `RunOptions`, immutable `RunConfigSnapshot`) — see `docs/RECIPE_AND_DOSING_RULES.md` for the full business rules.

The Recipe/Setpoints card shows three real **F1/F2/F3 buttons**, one per favorite slot, each labelled with that slot's current recipe name and highlighting the currently active one. Clicking a button only calls `set_active_slot()` — it loads the recipe book fresh, validates it, atomically saves the new active slot, and immediately refreshes the card from that freshly-loaded book. **A favorite-button click never starts a process and never touches hardware.** Saving a recipe only ever changes the slot it was saved to — the other two favorites are left byte-for-byte unchanged (the whole load-modify-save sequence runs under one lock).

The Recipe Editor is organized into four sections: Grunddaten (name/tank/RO water), EC, pH, and a read-only Volumenvorschau. `StoredRecipe` contains only fachliche Rezeptwerte — recipe name/tank, RO water amount, EC/pH setpoints and mixing times, nutrient configuration (Nutrient Solution A/B), notes, and legacy status. It deliberately does **not** contain any per-run/technical choice (sensor circulation, drain, sensor-pump timing) — those live in a separate `RunOptions` type instead, so **loading or selecting a recipe can never by itself activate hardware**. Addon 1/Addon 2 no longer exist in the active recipe model — the real CDS process only has Nutrient Solution A/B (already fully covered by the EC section); old addon values from migrated recipes are kept only for audit under `legacy_recipe_values`, never shown in the editor, never affecting a calculation, never reaching a `RunConfigSnapshot`, never triggering dosing/hardware.

Tank volume limits are centrally defined and enforced: `target_ro_water_l` (RO water only) is capped at 180 L, the estimated recipe volume (RO water + calculated EC nutrients) at 185 L. The 200 L physical tank rim capacity is documented but never used as a usable limit — every operational fill target across `config/process_settings.json`/`config/water_cycle_settings.json`/`config/tank_cleaning_settings.json` is now schema-capped at 185 L too. Invalid values are rejected with a concrete message on save, never silently clamped. A fixed RO correction is **not** a recipe value at all and is **not yet implemented** as a real feature — the editor shows a purely informational estimate of how much correction capacity would technically remain (`max_possible_ro_correction_l`); a real, demand-driven EC correction is a planned future feature only.

EC nutrient dosing uses a single total dose (`nutrients_ml_per_100_l`) split between Nutrient Solution A and B by a percentage (`nutrient_a_percent`, default 50 %); B is always calculated as `100 - A`, never edited independently. `nutrient_dosing_enabled` (default off) gates whether a dose is actually calculated anywhere — while off, every calculated amount is forced to 0 ml, including in the estimated volume. A recipe still flagged for legacy review (`legacy_dosing_needs_review`) cannot enable dosing until the editor's "Legacy review completed" switch explicitly clears that flag. The EC setpoint field uses a `0.1` stepper as a UI convenience only — arbitrary in-range values (e.g. `2.35`) are accepted and stored unchanged. No claim is made that real chemical dosing has been tested — this logic is fully implemented and unit-tested as pure domain logic, but no real peristaltic pump is connected yet (see below).

**Actually effective** (`nicegui_dashboard/recipe_store.py::build_run_config()`, called from `ProcessController.start_fill_and_measure()`): `target_ro_water_l` determines the real fill target volume (forces `fill_mode="absolute"`); `sensor_circulation_enabled`, sourced from a fresh `RunOptions` chosen in the Process Control panel's confirmation area (not the recipe, not persisted, always defaults to off, and only reset back to off after a start is actually accepted — a blocked start leaves the choice untouched so the user can correct and retry), determines whether the sensor-box circulation pump is driven during the run. Both are checked against `max_mixer_liters` and the `process_settings.json` schema — a recipe target volume above the mixer's capacity blocks the start with a clear message, instead of starting the process and only aborting mid-run via the watchdog. `RunConfigSnapshot.build(recipe, process_settings, run_options)` is the only place these are combined; everything downstream reads exclusively from that immutable snapshot. `ProcessController` stores the exact snapshot a run was actually started with, and the dashboard's status keeps showing *that* snapshot — not a freshly recomputed one — for as long as the process is running, even if a different favorite is activated in the meantime; only the next run picks up the newly active recipe.

Not yet active (no consumer in the code): the EC nutrient dosing fields and pH correction — the validated `RunConfigSnapshot` calculates and carries them (attached to the run's settings as `recipe_run_config_snapshot`), but there is still no peristaltic-pump control to actually dose them, and **no real peristaltic pumps are connected**. `drain_after_process` exists as a typed `RunOptions` field but has no hardware consumer at all and is deliberately **not** shown as a GUI toggle, so it is never presented as functional. See `docs/RECIPE_AND_DOSING_RULES.md` Section 5 and `docs/PERISTALTIC_PROFILE_PLAN.md` for the exact remaining integration point.

Recipes saved before this change are migrated automatically on first load, chained per-recipe through schema_version 1 → 2 → 3 → 4: unambiguous fields are renamed, the old absolute EC stock amounts are preserved under `legacy_volume_stock_1_ml`/`legacy_volume_stock_2_ml` (flagged `legacy_dosing_needs_review`, shown as a warning in the editor) rather than guess-converted, the old `requested_ro_correction_l`/`sensor_circulation_enabled`/`sensor_pump_seconds`/`drain_after_process` values are quarantined under `legacy_process_values` for audit, and the old `addon_1_ml`/`addon_2_ml` values are quarantined under `legacy_recipe_values` (merge-safe: existing entries are never overwritten, and re-running the migration is a no-op) — see `docs/OPEN_RECIPE_DECISIONS.md`. The recipe file is written atomically (temp file + fsync + `Path.replace()`, with a `.bak` backup of the previous version) under a single re-entrant lock held across each whole load-modify-save sequence; a JSON parse failure, wrong-typed metadata, an unknown future `schema_version`, duplicate favorite slots, or an out-of-range `active_slot`/`favorite.slot` all raise a controlled `RecipeBookCorruptedError` instead of silently overwriting the broken file with defaults, silently self-healing, or crashing with a raw exception.

---

## 16. Calibration

```bash
python main.py calibrate-mixer
```

Reads raw OPC-UA values, stores measurement points as CSV under `calibration_data/`.

**Flow of a live session:**

1. **0–30 L manual**: add water via bucket/can, enter the amount, averaged measurement point (settle time + N samples) as before.
2. **30–200 L pump-driven**: the mixer refill pump fills automatically, once to 20 L (rounded to the first tank marking), then in 25 L steps. Each segment logs the raw value **every second** into a separate `..._trace.csv`. Enter stops the pump once the marking is reached; a per-segment safety timeout (`calibration_fill_max_seconds`, default 300s) protects the pump in case that's forgotten.
3. **200–0 L pump-driven**: transfer pump + drain valve, same principle in 25 L steps down to 0 L, likewise with a trace log and its own segment timeout (`calibration_drain_max_seconds`, default 180s).

**Safety:** the previously typed confirmation phrases (`required_confirmation_text` etc.) and the `hardware_execution_enabled` check were removed from this script at explicit request — it's only ever started manually, with an operator present at the tank anyway. The pure pump protection (segment timeout) was deliberately kept.

**Recompute offline, without hardware:**

```bash
python calibration_mixing_tank.py --analyze calibration_data/*.csv
```

Loads any number of already-saved CSVs, pools them, normalizes zero points per session, warns on non-monotonic measurement points, and computes several fit variants (including one with a `min_reference_volume_l` filter, since the sensor is known to be unreliable below ~10 L). Guaranteed not to touch any GPIO/OPC-UA — pure CSV analysis.

**Current formula** (`config/system_config.json` → `mixer_level_calibration`):

```text
liters_real = 0.610566 * sensor_raw + -26.093067
R² = 0.996, max error ≈ 5.7 L, range 10-175 L
```

Derived from all calibration sessions so far (zero-normalized, fill phase only, ≥10 L). One single outlier measurement point (the first pump checkpoint of the latest session, likely an imprecisely read marking) was deliberately marked as `valid_for_fit=False`, see `invalid_reason` in the respective CSV. **175–200 L remains unconfirmed** — there is no reliable tank marking there to cross-check against.

---

## 17. Currently open items

1. **Tank Cleaning at test volume** — `config/tank_cleaning_settings.json` is currently set to 40 L (test run) instead of the planned 200 L; set back to the target volume after a successful first hardware test.
2. **Validate Tank Cleaning and Manual Drain Jog on real hardware** — both functions have so far only been tested on the software side; `hardware_execution_enabled` is `false` in both associated settings files.
3. **Secure the 175–200 L calibration range** — no reliable tank marking at the upper end to cross-check against yet; the production formula extrapolates slightly beyond the confirmed range there.
4. Test the modular Water Cycle on real hardware.
5. Gradually migrate logging from `print()` to `logging` (so far only `actuator_manager.py`, `cds_controller.py`).
6. Unify the MQTT staleness/payload readers (`services/sensor_snapshot.py`, `nicegui_dashboard/mqtt_topic_reader.py`).
7. No CI, no linting/mypy despite consistent type annotations (`tests/` with `pytest` for locking/watchdogs/config validation now exists, see Section 9).
8. ~~Connect recipe values to Water Cycle settings in a controlled way.~~ Done: `nicegui_dashboard/recipe_store.py::build_run_config()` connects the two recipe fields that have a real consumer (`target_ro_water_l` → `fill_mode="absolute"`/`target_total_liters`, `sensor_circulation_enabled` → `enable_sensor_circulation`) when starting Fill-and-Measure, including a `max_mixer_liters` bound check. The recipe's EC nutrient dosing fields are now fully typed, calculated, and validated (see Section 15, `docs/RECIPE_AND_DOSING_RULES.md`) and attached as `recipe_run_config_snapshot`, but stay without a hardware consumer until chemical dosing is implemented (item 9 below). pH correction fields are unchanged/out of scope for that work.
9. **Peristaltic pump control** — firmware-side hardening is complete in the separate `central_dosing_sys_peristaltic` repository (branch `harden-serial-protocol`) and verified on both Arduino MCUs on real hardware: drivers disabled by default, dose/runtime limits (`MAX_SINGLE_DOSE_ML`, `MAX_RUNTIME_MS`), a machine-readable `PING`/`STATUS`/`DOSE`/`STOP`/`STOPALL` protocol (see `docs/SERIAL_PROTOCOL.md` there). `cds_control` now has a standalone, hardware-free-tested Python serial client and terminal CLI for this protocol (`services/peristaltic/`, `scripts/peristaltic_calibration_cli.py` — discover/map/check/stop-all/test/calibrate/pair-test/all-four-test, water-only, max 10 ml per test dose, see `docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md`), but it is deliberately **not** wired into the recipe logic, the NiceGUI dashboard, or the state machine, and **no real peristaltic pumps are connected** — that integration, and real per-pump calibration/verification, remain separate, not-yet-started steps. A design-only plan for splitting the shared firmware into an `MCU_A_PH`/`MCU_B_EC` profile pair also exists at `docs/PERISTALTIC_PROFILE_PLAN.md` (no firmware/behavior changed by it).
10. **pH dosing bounds not yet enforced** — pH setpoint, pH mixing-time, and pH stock-volume bounds were confirmed against the legacy Node-RED configuration (see `docs/OPEN_RECIPE_DECISIONS.md`) but intentionally not added as hard validation, since pH dosing has no consumer yet and they were outside the explicitly requested rule set. (EC setpoint/mixing-time bounds and the EC adjustment factor bound *are* enforced — see Section 15. Addon 1/Addon 2 bounds no longer apply at all, since Addon 1/Addon 2 were removed from the active recipe model entirely.) Whether/how to enforce the remaining pH bounds is a product decision, not yet made.

---

## 18. Git and repo hygiene

Not included in the repository:

```text
__pycache__/
*.pyc
.venv/
logs/*
*.log
*.out
*.bak_*
calibration_data/*.csv
archive/
```

```text
Git history replaces local backup files.
```

Check before commits:

```bash
git status
git diff --check
python main.py preflight
```

---

## 19. Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 20. Development rule

```text
Safe first.
Then traceable.
Then automated.
Then production.
```

New hardware functions are only added once:

- the real hardware assignment has been verified,
- the water path is traceably safe,
- preflight succeeds,
- a safe shutdown exists,
- a timeout or plausible abort path exists,
- and the flow has first been tested with water.

---
---

# Central Dosing System – Python Control Layer (Deutsch)

*Deutsche Version. [English version above](#central-dosing-system--python-control-layer).*

Stand: 17.07.2026
Projektstatus: Entwicklungs- und Validierungsphase
Ziel: sichere, nachvollziehbare und modular erweiterbare Steuerungslogik für den wasserbasierten CDS-Prozess.

> **Sicherheitsstatus in einem Satz:** Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`); von den 15 konfigurierten GPIO-Ausgängen sind aktuell nur **drei** real am physischen System validiert (siehe Abschnitt 4). Alle anderen Ausgänge sind softwareseitig ansteuerbar, aber ohne bestätigte reale Wirkung.

---

## 1. Kurzüberblick

Dieses Repository enthält die Python-Implementierung für die Steuerungs- und Visualisierungsebene eines Central-Dosing-Systems (Raspberry Pi, GPIO-Relais, OPC-UA-Sensorik, MQTT/Node-RED). Der aktuelle Fokus liegt auf dem sicheren wasserbasierten Grundprozess:

```text
RO-Wasser → Mixing Tank → Sensorbox-Zirkulation → Drain
```

Chemikaliendosierung und Peristaltikpumpensteuerung sind noch nicht aktiv. Diese Funktionen werden erst nach weiterer Hardware-, Sensor- und Sicherheitsvalidierung ergänzt. Die Rezeptausführung ist bisher nur für den reinen Wasser-Zielwert (Füllvolumen, Sensorzirkulation) wirksam mit dem tatsächlichen Prozessstart verbunden (siehe Abschnitt 15) — die EC-/pH-/Dosierwerte im Rezept sind weiterhin rein deskriptiv, ohne Konsument im Code.

Die Steuerungslogik wurde von einzelnen Testskripten in eine modulare Struktur überführt. `main.py` ist der zentrale Einstiegspunkt für Preflight, Water-Cycle, Dashboard, Kalibrierung und Statusprüfungen.

**Zielplattform:** Raspberry Pi (Raspberry Pi OS), Python 3.11, `.venv`-basiertes Setup. Die GPIO-Ansteuerung läuft über `gpiozero` mit dem `lgpio`-Pin-Factory-Backend.

---

## 2. Architektur / Datenfluss

```text
                     ┌──────────────────────┐
                     │   OPC-UA-Server      │
                     │ (RO/Mixing-Tank,     │
                     │  pH, EC, Temp, DO)   │
                     └──────────┬───────────┘
                                │ OPC-UA read (Timeout-abgesichert)
                                ▼
                     mqtt_sensor_bridge.py  ──► MQTT: cds/status/sensors
                                                        │
gpio_config.py ──► hardware/ ──► process/ ──► statemachine/            │
   (Pin-Mapping)   (DigitalOutput,  (refill/drain/   (FillAndMeasure)  │
                    ActuatorManager) sensor_circulation/               │
                                     auto_circulation/                 │
                                     manual_drain_jog/                 │
                                     tank_cleaning)                    │
        │                 │                │                           │
        └─────────────────┴────────────────┴──► MQTT: cds/status/process
                                                        │
                                          ┌─────────────┴──────────────┐
                                          ▼                            ▼
                                  Node-RED Dashboard         NiceGUI Dashboard
                                  (bestehende HMI)           (nicegui_dashboard/)
```

`main.py` ist der einzige unterstützte Einstiegspunkt für alle produktiven Abläufe (Preflight, Water-Cycle, Dashboard, Kalibrierung, Safe-Drain, Statuschecks). Manual Drain Jog und Tank Cleaning werden ausschließlich über das NiceGUI-Dashboard gestartet, nicht über `main.py`.

---

## 3. Aktueller Funktionsstand

Aktuell umgesetzt:

- MQTT-Sensor-Bridge für OPC-UA-Sensordaten (Read-Timeout + QoS-1-Publish mit Bestätigung)
- NiceGUI-Dashboard zur Visualisierung und Prozesssteuerung (überarbeitetes 4-Spalten-Layout, siehe Abschnitt 14)
- modularer Water-Cycle-Prozess (Refill → Sensorbox-Zirkulation → Drain)
- automatische Zirkulationssteuerung (`process/auto_circulation.py`) – füllstandsabhängiges Ein-/Ausschalten der Zirkulationspumpen, wiederverwendet von Water-Cycle **und** Tank Cleaning
- Manual Drain Jog – dashboardgesteuerte manuelle Entleerung mit Server-seitigem 30-Sekunden-Watchdog
- Tank Cleaning – automatischer Reinigungszyklus (Fill → Hold → Drain), siehe Abschnitt 13
- zentraler Preflight-Check inkl. GPIO-Konfliktprüfung gegen Node-RED-GPIO-Helper, per CLI **und** per "Run Preflight Check"-Button im Dashboard (Dev Info)
- Recipe-Editor im Dashboard mit drei Favoriten und JSON-Ablage
- Mixing-Tank-Kalibrierung mit pumpengesteuerten Fill-/Drain-Segmenten, sekündlichem Trace-Log und offline nachrechenbarer Formel (`--analyze`), siehe Abschnitt 16
- zentrale Systemkonfiguration für OPC-UA, MQTT und Mixer-Level-Kalibrierung (`config/system_config.json`), inzwischen auch vom Dashboard-MQTT-Layer und der State-Machine-Kalibrierung genutzt (siehe Abschnitt 7)
- zentrale, race-freie Prozessverriegelung für Start/Stop von Fill-and-Measure, Manual Drain Jog und Tank Cleaning, plus prozessübergreifende Hardware-Sperre (`fcntl.flock`, `hardware/actuator_manager.py`) und nicht-blockierender, asynchroner Emergency Stop (siehe Abschnitt 8)
- einheitliche Watchdog-/Fortschrittsprüfung für alle Fill-/Drain-Phasen (`process/watchdog.py`) und Settings-Schema-Validierung mit gesammelten Fehlern statt Abbruch beim ersten Treffer (`services/settings_validation.py`), beide mit `pytest`-Testabdeckung (siehe Abschnitt 9)
- Recipe-Editor tatsächlich mit dem Prozessstart verbunden: Zielvolumen und Sensorzirkulation aus dem aktiven Rezept bestimmen jetzt real den nächsten Fill-and-Measure-Lauf statt nur die Anzeige (siehe Abschnitt 15)
- typisiertes Rezept-/Dosier-Domänenmodell (`domain/recipe_limits.py`, `domain/recipe_model.py`): zentrale 180-l-/185-l-Tankvolumengrenzen, EC-Nährstofflösung-A/B-Aufteilung (B stets berechnet als `100 - A`), ein unveränderlicher, validierter `RunConfigSnapshot`, sowie eine `recipes/dashboard_recipes.json`-Schema-Migration mit atomarem, gesperrtem Schreiben — siehe Abschnitt 15 und `docs/RECIPE_AND_DOSING_RULES.md`
- Rezept-/RunOptions-Datenmodell gehärtet: `StoredRecipe` enthält jetzt nur noch fachliche Rezeptwerte (eine feste RO-Korrektur ist kein Rezeptwert mehr), Lauf-spezifische Einstellungen (Sensorzirkulation, Drain nach Prozess) wurden in einen eigenen, nie gespeicherten `RunOptions`-Typ mit Standard „aus“ ausgelagert, strikte `type(value) is bool`-/Endlichkeits-/Nicht-Ganzzahl-Float-Prüfung schließt mehrere stille Typ-Umwandlungslücken, Rezeptbuch-Metadaten (`schema_version`/`active_slot`/`favorite.slot`) werden jetzt fail-closed validiert, und jedes operative Füllziel ist auf 185 l begrenzt (keine 200-l-Defaults mehr) — siehe Abschnitt 15 und `docs/RECIPE_AND_DOSING_RULES.md`
- Favoriten dauerhaft und interaktiv gemacht: F1/F2/F3 sind jetzt echte Buttons, die einen Favoritenslot aktivieren (starten nie einen Prozess, aktivieren nie Hardware), der Rezeptbuch-Metadatenvertrag ist vollständig fail-closed (unbekannte zukünftige Schema-Versionen, doppelte Slots, Slots außerhalb des gültigen Bereichs werden abgelehnt statt still selbstheilend korrigiert), Addon 1/Addon 2 wurden vollständig aus dem aktiven Rezeptmodell entfernt (der reale CDS-Prozess kennt nur Nährstofflösung A/B — alte Werte bleiben zur Nachvollziehbarkeit unter `legacy_recipe_values` erhalten), und der Status eines laufenden Prozesses zeigt jetzt weiterhin das Rezept, mit dem tatsächlich gestartet wurde, selbst wenn währenddessen ein anderer Favorit aktiviert wird — siehe Abschnitt 15 und `docs/RECIPE_AND_DOSING_RULES.md`

Der modulare Water-Cycle startet softwareseitig korrekt. Ein vollständiger realer Hardwarelauf nach dem letzten Refactor steht noch aus. Manual Drain Jog und Tank Cleaning sind ebenfalls noch nicht real mit Hardware validiert (siehe Abschnitt 17).

---

## 4. Hardware-Validierungsstatus (wichtig!)

Nur die folgenden Ausgänge wurden real am physischen System getestet und ihre Wirkung bestätigt:

| Funktion              | Config-Key             | GPIO (BCM) | Physischer Pin | Status              |
|------------------------|------------------------|:----------:|:---------------:|----------------------|
| Mixer Refill Pump       | `mixer_refill_pump`    | 20         | 38               | validiert            |
| Supply/Test Valve 6     | `test_supply_valve_6`  | 6          | 31               | validiert            |
| Drain Valve 0           | `valve_0_drain`        | 21         | 40               | validiert            |

Alle übrigen Einträge in `gpio_config.py` (`contactor_0/5`, `transfer_pump`, `mixing_circulation_pump`, `sensor_circulation_pump`, `valve_1`–`valve_9`) sind im Code ansteuerbar, aber **nicht** durch einen realen Hardwaretest bestätigt. Insbesondere:

- **Transfer Pump** (`transfer_pump`, GPIO26) wird bereits in `calibration_mixing_tank.py`, `main_safe_drain.py` und jetzt auch in `process/tank_cleaning.py` verwendet, ihre elektrische Anbindung ist aber laut Projektstand noch nicht abschließend geklärt.
- **Valve 5** ist laut Kommentar in `gpio_config.py` nicht zuverlässig dicht.
- **Tank Cleaning** steuert zusätzlich `mixing_circulation_pump` und `sensor_circulation_pump` an – ebenfalls noch ohne realen Hardwaretest. Die Settings-Datei (`config/tank_cleaning_settings.json`) ist deshalb bewusst auf ein reduziertes **40-Liter-Testvolumen** statt des späteren 200-Liter-Zielwerts gestellt, um den ersten Hardwaretest ohne größeren Wasserverbrauch/-risiko durchzuführen.

Bevor ein neuer Ausgang produktiv verwendet wird: real am System schalten, Wirkung visuell/physisch bestätigen, erst danach in eine automatisierte Sequenz aufnehmen.

---

## 5. Zentrale Einstiegspunkte

Alle Hauptfunktionen sollen über `main.py` gestartet werden.

```bash
python main.py --help
python main.py preflight
python main.py gpio-check
python main.py water-cycle
python main.py calibrate-mixer
python main.py dashboard
python main.py sensor-bridge-check
python main.py safe-drain
```

```bash
cd ~/cds_control
source .venv/bin/activate
python main.py preflight
```

Vor jedem Hardwaretest muss der kombinierte Preflight erfolgreich sein.

Manual Drain Jog und Tank Cleaning haben keinen eigenen `main.py`-Befehl – sie werden ausschließlich über die entsprechenden Buttons im NiceGUI-Dashboard gestartet (`python main.py dashboard`).

---

## 6. Projektstruktur

```text
cds_control/
├── main.py
├── config/
│   ├── system_config.json
│   ├── water_cycle_settings.json
│   ├── calibration_settings.json
│   ├── process_settings.json
│   └── tank_cleaning_settings.json
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   ├── water_cycle.py
│   ├── auto_circulation.py
│   ├── manual_drain_jog.py
│   ├── tank_cleaning.py
│   ├── watchdog.py
│   └── background_process.py
├── services/
│   ├── system_config.py
│   ├── mqtt_publisher.py
│   ├── process_run_logger.py
│   ├── sensor_snapshot.py
│   └── settings_validation.py
├── hardware/
│   ├── digital_output.py
│   └── actuator_manager.py
├── nicegui_dashboard/
│   ├── app.py
│   ├── cds_controller.py
│   ├── process_controller.py
│   ├── recipe_store.py
│   ├── mqtt_topic_reader.py
│   ├── pages/
│   └── static/
├── scripts/
│   └── check_gpio_conflicts.py
├── recipes/
│   └── dashboard_recipes.json
├── tests/
├── logs/
├── calibration_data/
├── mqtt_sensor_bridge.py
├── calibration_mixing_tank.py
├── main_safe_drain.py
├── preflight_check.py
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
```

---

## 7. Konfiguration

Zentrale technische Systemwerte liegen in `config/system_config.json`:

```json
{
  "opcua": {
    "endpoint": "opc.tcp://10.8.0.62:14840",
    "read_timeout_seconds": 3.0
  },
  "mqtt": {
    "host": "localhost",
    "port": 1883,
    "sensor_topic": "cds/status/sensors",
    "process_topic": "cds/status/process",
    "qos": 1,
    "publish_timeout_seconds": 2.0
  },
  "mixer_level_calibration": {
    "volume_liters": 200.0,
    "factor": 0.610566,
    "offset": -26.093067,
    "status": "fitted_ge10L_20260703_20260709_sessions",
    "fit_r2": 0.996399,
    "fit_max_abs_error_liters": 5.698,
    "fit_source": "calibration_mixing_tank.py --analyze ... (siehe Abschnitt 16)"
  }
}
```

> Hinweis: Diese Datei ist inzwischen die alleinige Quelle für OPC-UA-Endpoint, MQTT-Verbindung und Mixer-Kalibrierung — sowohl `mqtt_sensor_bridge.py`, `calibration_mixing_tank.py`, `preflight_check.py` als auch der Dashboard-MQTT-Layer (`services/mqtt_publisher.py`, `nicegui_dashboard/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py`) und der Kalibrierfaktor-Fallback in `statemachine/fill_and_measure_state_machine.py` lesen jetzt aus `get_mqtt_config()`/`get_mixer_level_calibration()` statt aus eigenen hartcodierten Werten.

Weitere Settings-Dateien, je Prozess eine eigene:

- `config/water_cycle_settings.json` – Water-Cycle
- `config/calibration_settings.json` – Mixing-Tank-Kalibrierung
- `config/process_settings.json` – Fill-and-Measure
- `config/tank_cleaning_settings.json` – Tank Cleaning (Zielvolumen, Haltezeit, Auto-Zirkulations-Schwellen, Drain-Parameter, eigener `hardware_execution_enabled`-Schalter)

---

## 8. Sicherheitsgrundsätze

- Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`).
- Muss für reale Tests bewusst lokal auf `true` gesetzt werden — nie mit `true` committen.
- Jede Prozess-Settings-Datei (Water-Cycle, Fill-and-Measure, Kalibrierung, Tank Cleaning) hat ihren **eigenen** `hardware_execution_enabled`-Schalter und Bestätigungstext.
- Vor jedem Hardwarelauf muss `python main.py preflight` erfolgreich sein.
- Node-RED darf keine GPIOs blockieren, die Python für den CDS-Prozess benötigt.
- Chemikaliendosierung und Peristaltikpumpensteuerung sind aktuell nicht aktiv.
- Sensorwerte werden nicht blind als zuverlässige Wahrheit angenommen (Filterung, Plausibilitätsprüfung, Confirm-Samples).
- Aktoren werden über zentrale Safe-Shutdown-Pfade ausgeschaltet (`ActuatorManager.safe_shutdown_all()`), Fehler dabei werden geloggt statt verschluckt.
- Manuelle Kalibrier-Drain-Funktionen besitzen ein Safety-Timeout (`calibration_drain_max_seconds`).
- `KeyboardInterrupt` wird in allen Water-Cycle-Phasen einheitlich behandelt und bricht den Rest des Ablaufs kontrolliert ab.
- Manual Drain Jog und Tank Cleaning laufen als Hintergrund-Threads, deren Schleifen alle ~0,5s auf ein `threading.Event` prüfen — ein Emergency Stop aus dem Dashboard unterbricht sie dadurch binnen einer knappen Sekunde, statt bis zum Ende der laufenden Phase zu warten.
- Start/Stop von Fill-and-Measure, Manual Drain Jog und Tank Cleaning sind atomar gegen den internen `ProcessController`-Lock abgesichert: zwei gleichzeitige Start-Aufrufe (Dashboard-Klick + parallel gestartetes Skript) können nicht mehr beide durchkommen — vormals ein TOCTOU-Race zwischen Zustandsprüfung und tatsächlichem Start.
- Zusätzlich verhindert eine prozessübergreifende Datei-Sperre (`fcntl.flock`, `hardware/actuator_manager.py`, `logs/.hardware.lock`) parallele Hardware-Ansteuerung durch zwei unabhängige Python-*Prozesse* (nicht nur Threads im selben Prozess) — z. B. Dashboard plus ein direkt gestartetes Kalibrier- oder Safe-Drain-Skript.
- Emergency Stop ist asynchron implementiert (`ProcessController.emergency_stop()`): das Signalisieren (State-Machine-Abbruch, `request_stop()`, Aktor-Shutdown) passiert sofort unter Lock, das Warten auf den Thread-Join läuft danach außerhalb des Locks in einem Worker-Thread — das Dashboard bleibt für andere verbundene Clients währenddessen reaktionsfähig, statt bis zu ~7s einzufrieren.

Standardzustand in den Configs:

```json
"hardware_execution_enabled": false
```

Dadurch werden keine GPIO-Ausgänge initialisiert oder geschaltet, solange die Hardwareausführung nicht explizit aktiviert wird.

---

## 9. Preflight und GPIO-Konfliktprüfung

Der kombinierte Preflight prüft: Projektdateien, Python-Syntax, GPIO-Konfiguration, doppelte GPIO-Zuordnungen (**blockiert den Water-Cycle-Start**, kein reines Warnen mehr), aktive Systemdienste, MQTT-Erreichbarkeit, OPC-UA-Lesbarkeit, aktuelle Sensor-MQTT-Payload-Struktur, Node-RED-GPIO-Konflikte.

```bash
python main.py preflight
python main.py gpio-check
```

Zusätzlich per Dashboard auslösbar: Button "Run Preflight Check" in Dev Info schreibt das komplette Ergebnis ins Process Log (läuft in einem Worker-Thread, blockiert das Dashboard für andere Nutzer währenddessen nicht).

Kritische CDS-GPIOs für Python:

```text
GPIO20 = mixer_refill_pump
GPIO21 = valve_0_drain
GPIO22 = sensor_circulation_pump
GPIO26 = transfer_pump
```

Node-RED darf diese Pins nicht blockieren. Falls Node-RED nur unabhängige Funktionen wie RO-Machine betreibt, ist das zulässig, solange keine CDS-GPIOs blockiert werden.

### Troubleshooting

| Preflight-Fehler | Wahrscheinliche Ursache | Nächster Schritt |
|---|---|---|
| MQTT nicht erreichbar | Broker nicht gestartet | `systemctl status mosquitto` |
| OPC-UA nicht lesbar | Server nicht erreichbar / falscher Endpoint | `config/system_config.json` prüfen, Netzwerk prüfen |
| GPIO-Konflikt mit Node-RED | Node-RED-Flow nutzt denselben Pin | Node-RED-Flow-Zuordnung prüfen, `scripts/check_gpio_conflicts.py` |
| Sensor-Payload ungültig | `cds-sensor-bridge.service` läuft nicht/hängt | `journalctl -u cds-sensor-bridge.service -f` |

### Automatisierte Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Deckt Locking (`nicegui_dashboard/process_controller.py`, prozessübergreifende
Sperre in `hardware/actuator_manager.py`), Watchdog-/Fortschrittsprüfungen
(`process/watchdog.py`) und Settings-Validierung (`services/settings_validation.py`,
inklusive aller vier echten `config/*.json`-Dateien) ab — kein GPIO/OPC-UA
wird dabei angefasst. `requirements-dev.txt` (nur `pytest` zusätzlich zu
`requirements.txt`) ist bewusst getrennt von der Produktions-`requirements.txt`.

---

## 10. Sensor-Bridge

`mqtt_sensor_bridge.py` liest Sensordaten über OPC-UA und veröffentlicht sie per MQTT auf `cds/status/sensors` (RO-/Mixing-Tank-Füllstand, EC, pH, Wassertemperatur, Dissolved Oxygen, Bridge-/Fehlerstatus). OPC-UA-Reads sind mit Timeout abgesichert, MQTT-Publish läuft mit QoS 1 und Bestätigung.

Die Bridge erkennt jetzt auch eine mittendrin gestorbene OPC-UA-Session (mehrere Zyklen in Folge, in denen alle Sensoren gleichzeitig fehlschlagen) und verbindet sich automatisch neu, mit Backoff (5s, verdoppelnd bis 30s). Vorher blieb eine tote Session unbemerkt bestehen — der Prozess lief dann beliebig lange weiter und veröffentlichte nur noch Fehler/`null`-Werte, ohne sich je selbst zu erholen.

```bash
systemctl status cds-sensor-bridge.service
sudo systemctl restart cds-sensor-bridge.service
journalctl -u cds-sensor-bridge.service -f
mosquitto_sub -h localhost -t cds/status/sensors -v
```

---

## 11. Water-Cycle-Prozess

```text
process/water_cycle.py         → Orchestrator
process/refill.py              → Mixing Tank befüllen
process/sensor_circulation.py  → Sensorbox durchströmen
process/drain.py               → Mixing Tank entleeren
process/auto_circulation.py    → füllstandsabhängige Zirkulationspumpen-Steuerung
process/common.py              → gemeinsame Hilfsfunktionen
```

```bash
python main.py water-cycle
```

Der Water-Cycle führt automatisch zuerst den Preflight aus; schlägt dieser fehl, startet der Prozess nicht.

```text
1. Sicherheitsabfrage
2. Sensor-Payload prüfen
3. Refill bis Zielwert (Zirkulationspumpen schalten automatisch ab auto_circulation_start_liters zu)
4. optionale Sensorbox-Zirkulation
5. optionale Drain-Phase (Zirkulationspumpen schalten automatisch unter auto_circulation_stop_liters ab)
6. Safe-Shutdown
```

> Der modulare Water-Cycle muss nach dem letzten Refactor noch einmal vollständig real mit Hardware getestet werden.

---

## 12. Manual Drain Jog

Dashboard-Wartungsfunktion für kontrolliertes manuelles Entleeren, unabhängig vom Water-Cycle.

```text
process/manual_drain_jog.py → ManualDrainJog
```

Ablauf:

- "Hold to Drain"-Button im Dashboard: Drain-Ventil öffnet, kurze Ventil-Settle-Zeit, dann Transferpumpe an.
- Solange der Button gehalten wird, läuft die Pumpe.
- Loslassen stoppt sofort (dead-man-Prinzip).
- Server-seitiger Watchdog stoppt zusätzlich spätestens nach 30 Sekunden, auch falls die Verbindung zum Browser/Websocket hängt.

Läuft als eigener Hintergrund-Thread mit `threading.Event`-Steuerung, damit ein Emergency Stop jederzeit zuverlässig durchgreift. Wechselseitig ausgeschlossen mit Fill-and-Measure und Tank Cleaning – es kann immer nur einer der drei Prozesse gleichzeitig aktiv sein.

---

## 13. Tank Cleaning

Automatisierter Reinigungszyklus für den Mixing Tank, z. B. nach einem Mischvorgang.

```text
process/tank_cleaning.py       → TankCleaningController
config/tank_cleaning_settings.json → Einstellungen
```

Ablauf (drei Phasen, alle über MQTT auf `cds/status/process` sichtbar):

```text
1. FILL    Mixer Refill Pump füllt bis zum Zielvolumen (target_fill_total_liters)
2. HOLD    feste Haltezeit (cleaning_hold_seconds, Default 300 s)
3. DRAIN   Transfer Pump + Drain Valve entleeren bis Sensor "leer" bestätigt
```

Während FILL, HOLD und DRAIN läuft dieselbe `AutoCirculationController`-Logik wie im Water-Cycle mit: Zirkulationspumpen (`mixing_circulation_pump`, `sensor_circulation_pump`) schalten automatisch ein, sobald der Tank `auto_circulation_start_liters` (Default 30 L) überschreitet, und wieder aus unter `auto_circulation_stop_liters` (Default 25 L) – auch während des Drains. Die 300-Sekunden-Haltezeit selbst beginnt erst, sobald das Zielvolumen sensorbestätigt erreicht ist, nicht schon beim Einschalten der Pumpen bei 30 L.

Sicherheit:

- Eigener `hardware_execution_enabled`-Schalter + Bestätigungstext in `config/tank_cleaning_settings.json`, unabhängig von den anderen Prozessen.
- Jede Phasen-Schleife prüft alle 0,5 s ein `threading.Event` – Stop-Button oder Emergency Stop wirken dadurch nahezu sofort, nicht erst am Ende der laufenden Phase.
- Fill-Phase mit denselben Sicherheitsprüfungen wie `process/refill.py` (Timeout, Fortschrittskontrolle, max. Tankfüllstand, RO-Mindestmenge).
- Drain-Phase mit demselben berechneten Timeout wie `process/drain.py`.
- `safe_shutdown` schaltet auf jedem Ausstiegspfad (Erfolg, Abbruch, Fehler) alle beteiligten Aktoren ab.

**Aktueller Testzustand:** `config/tank_cleaning_settings.json` ist auf ein reduziertes **Testvolumen von 40 L** gestellt (statt der späteren 200 L), damit der erste reale Hardwaretest ohne großen Wasserverbrauch durchgeführt werden kann. `hardware_execution_enabled` steht auf `false` und muss vor einem echten Lauf bewusst und nur nach physischer Prüfung des Wasserwegs auf `true` gesetzt werden.

Dashboard: Button "Start Tank Cleaning" im Bereich "Process Control → Tank Cleaning", nutzt dasselbe Bestätigungsfeld wie "Start Process". Wechselseitig ausgeschlossen mit Fill-and-Measure und Manual Drain Jog.

---

## 14. NiceGUI-Dashboard

Das Dashboard wurde auf ein 4-Spalten-Layout umgebaut, das ohne Scrollen auskommt:

```text
┌─────────────┬───────────────────┬──────────────────────┬────────────────┐
│ System      │ Current Process   │ Process Control      │ Sensor Values  │
│ Status      │ State             │  - Safety            │                │
│             │                   │    Confirmation      │ Tanks / Levels │
│ Actuators / │ Recipe /          │  - Maintenance       │ (RO + Mixing,  │
│ Outputs     │ Setpoints         │    (Manual Drain Jog)│  untereinander │
│             │                   │  - Tank Cleaning     │  gestapelt)    │
└─────────────┴───────────────────┴──────────────────────┴────────────────┘
```

- **System Status / Actuators**: nur noch die tatsächlich genutzten Ausgänge (Mixer Refill Pump, Supply Valve 6, Drain Valve 0, Transfer Pump, Mixing/Sensor Circulation Pump, Valve 1–5). Unbenutzte Pins (`contactor_0/5`, `valve_7/8/9`) sind aus der Anzeige entfernt, um die Karte nicht unnötig zu füllen.
- **Process Control**: Safety Confirmation (ein gemeinsames Bestätigungsfeld für Start Process **und** Tank Cleaning) → Emergency Stop → Maintenance (Manual Drain Jog) → Tank Cleaning.
- **Dev Info**: Die früher permanent sichtbaren Status-Badges (RASPI LIVE / PYTHON CORE / Update) und das Prozess-Log sind hinter einem "Dev Info"-Button im Kopfbereich versteckt und öffnen sich als Dialog – für den Endanwender unnötige Diagnoseinfos stören dadurch nicht mehr die Hauptansicht.
- **Sensor Values / Tanks**: eigene rechte Spalte, RO- und Mixing-Tank-Gauge stehen untereinander (nicht mehr nebeneinander) und haben eine stabile Breite unabhängig von der Anzahl der Nachkommastellen des Füllstands.

```bash
sudo systemctl restart cds-nicegui-dashboard.service
systemctl status cds-nicegui-dashboard.service --no-pager
python main.py dashboard   # manueller Start
```

Die Oberfläche ist HMI/Visualisierung plus Start/Stop-Steuerung für Fill-and-Measure, Manual Drain Jog und Tank Cleaning. Die eigentliche Prozesslogik bleibt in den Python-Prozessmodulen (`process/`, `statemachine/`), nicht im UI-Code.

---

## 15. Recipe-Editor

Recipe-Editor mit **drei dauerhaft gespeicherten, unabhängigen Favoriten** unter `recipes/dashboard_recipes.json` (Schema-Version 4). Typisiertes Domänenmodell in `domain/recipe_limits.py` (zentrale Konstanten + Berechnungs-/Validierungsregeln) und `domain/recipe_model.py` (`StoredRecipe`, `RecipePreview`, `RunOptions`, unveränderlicher `RunConfigSnapshot`) — siehe `docs/RECIPE_AND_DOSING_RULES.md` für die vollständigen fachlichen Regeln.

Die Recipe-/Setpoints-Karte zeigt drei echte **F1/F2/F3-Buttons**, je einen pro Favoritenslot, jeweils mit dem aktuellen Rezeptnamen dieses Slots beschriftet und mit Hervorhebung des aktiven Slots. Ein Klick ruft ausschließlich `set_active_slot()` auf — dies lädt das Rezeptbuch frisch, validiert es, speichert den neuen aktiven Slot atomar und aktualisiert die Karte sofort aus diesem frisch geladenen Stand. **Ein Klick auf einen Favoritenbutton startet niemals einen Prozess und aktiviert niemals Hardware.** Das Speichern eines Rezepts ändert immer nur den Slot, in den gespeichert wurde — die anderen beiden Favoriten bleiben Byte für Byte unverändert (die gesamte Lade-Ändern-Speichern-Sequenz läuft unter einer Sperre).

Der Recipe-Editor ist in vier Bereiche gegliedert: Grunddaten (Name/Tank/RO-Wasser), EC, pH und eine reine Volumenvorschau. `StoredRecipe` enthält nur fachliche Rezeptwerte — Rezeptname/Tank, RO-Wassermenge, EC-/pH-Sollwerte und Mischzeiten, Nährstoffkonfiguration (Nährstofflösung A/B), Notiz und Legacy-Status. Es enthält bewusst **keine** Lauf-/technische Einstellung (Sensorzirkulation, Drain, Sensorpumpenzeit) mehr — diese leben stattdessen in einem eigenen `RunOptions`-Typ, sodass **das Laden oder Auswählen eines Rezepts niemals von selbst Hardware aktivieren kann**. Addon 1/Addon 2 existieren nicht mehr im aktiven Rezeptmodell — der reale CDS-Prozess kennt nur Nährstofflösung A/B (bereits vollständig durch den EC-Bereich abgedeckt); alte Addon-Werte migrierter Rezepte bleiben nur zur Nachvollziehbarkeit unter `legacy_recipe_values` erhalten, werden nie im Editor angezeigt, beeinflussen keine Berechnung, erreichen keinen `RunConfigSnapshot` und lösen keine Dosierung/Hardware aus.

Tankvolumengrenzen sind zentral definiert und werden durchgesetzt: `target_ro_water_l` (reines RO-Wasser) ist auf 180 l begrenzt, das geschätzte Rezeptvolumen (RO-Wasser + berechnete EC-Nährstoffe) auf 185 l. Die 200-l-physische Tankkapazität am Rand ist dokumentiert, wird aber nie als nutzbare Grenze verwendet — auch jedes operative Füllziel in `config/process_settings.json`/`config/water_cycle_settings.json`/`config/tank_cleaning_settings.json` ist jetzt schema-seitig auf 185 l begrenzt. Ungültige Werte werden beim Speichern mit konkreter Meldung abgelehnt, nie stillschweigend geclampt. Eine feste RO-Korrektur ist **kein** Rezeptwert mehr und **noch nicht als echte Funktion implementiert** — der Editor zeigt nur eine rein informative Schätzung, wie viel Korrekturkapazität später technisch noch verfügbar wäre (`max_possible_ro_correction_l`); eine echte, bedarfsabhängige EC-Korrektur ist ausschließlich eine geplante Zukunftsfunktion.

Die EC-Nährstoffdosierung nutzt eine Gesamtdosis (`nutrients_ml_per_100_l`), aufgeteilt zwischen Nährstofflösung A und B über einen Prozentanteil (`nutrient_a_percent`, Standard 50 %); B wird stets als `100 - A` berechnet, nie unabhängig editiert. `nutrient_dosing_enabled` (Standard aus) steuert, ob überhaupt irgendwo eine Dosis berechnet wird — solange aus, werden alle berechneten Mengen auf 0 ml erzwungen, auch im geschätzten Volumen. Ein noch als Legacy-prüfungsbedürftig markiertes Rezept (`legacy_dosing_needs_review`) kann die Dosierung erst aktivieren, nachdem der "Legacy review completed"-Schalter im Editor dieses Flag ausdrücklich aufgehoben hat. Das EC-Sollwert-Feld nutzt eine `0,1`-Schrittweite nur als Bedienhilfe — beliebige Zwischenwerte im gültigen Bereich (z. B. `2,35`) werden unverändert akzeptiert und gespeichert. Es wird nicht behauptet, dass eine echte Chemikaliendosierung bereits getestet wurde — diese Logik ist als reine Domänenlogik vollständig implementiert und getestet, aber es ist noch keine echte Peristaltikpumpe angeschlossen (siehe unten).

**Tatsächlich wirksam** (`nicegui_dashboard/recipe_store.py::build_run_config()`, aufgerufen von `ProcessController.start_fill_and_measure()`): `target_ro_water_l` bestimmt das reale Füll-Zielvolumen (erzwingt `fill_mode="absolute"`); `sensor_circulation_enabled`, aus einem frisch im Bestätigungsbereich von Process Control gewählten `RunOptions` (nicht aus dem Rezept, nicht gespeichert, startet immer auf „aus" und wird erst nach einem tatsächlich akzeptierten Start wieder auf „aus" zurückgesetzt — ein blockierter Start lässt die Wahl unverändert, damit ein erneuter Versuch möglich ist), bestimmt, ob die Sensorbox-Zirkulationspumpe beim Lauf mit angesteuert wird. Beide werden gegen `max_mixer_liters` und das `process_settings.json`-Schema geprüft — ein Rezept-Zielvolumen über der Mixer-Kapazität blockiert den Start mit klarer Meldung, statt den Prozess zu starten und erst per Watchdog mittendrin abzubrechen. `RunConfigSnapshot.build(recipe, process_settings, run_options)` ist die einzige Stelle, an der diese drei Quellen zusammengeführt werden; alles Nachgelagerte liest ausschließlich aus diesem unveränderlichen Snapshot. `ProcessController` speichert den genauen Snapshot, mit dem ein Lauf tatsächlich gestartet wurde, und der Status im Dashboard zeigt weiterhin genau diesen Snapshot — nicht neu berechnet — solange der Prozess läuft, selbst wenn währenddessen ein anderer Favorit aktiviert wird; erst der nächste Lauf verwendet das neu aktive Rezept.

Noch nicht aktiv (kein Konsument im Code): die EC-Nährstoffdosierfelder und die pH-Korrektur — der validierte `RunConfigSnapshot` berechnet und führt sie mit (als `recipe_run_config_snapshot` an die Lauf-Settings angehängt), aber es gibt noch keine Peristaltikpumpensteuerung, die sie tatsächlich dosiert, und **es sind noch keine echten Peristaltikpumpen angeschlossen**. `drain_after_process` existiert als typisiertes `RunOptions`-Feld, hat aber keinerlei Hardware-Konsumenten und wird bewusst **nicht** als GUI-Schalter angezeigt, damit nichts Nicht-Implementiertes als funktionsfähig dargestellt wird. Siehe `docs/RECIPE_AND_DOSING_RULES.md` Abschnitt 5 und `docs/PERISTALTIC_PROFILE_PLAN.md` für den genauen verbleibenden Integrationspunkt.

Vor dieser Änderung gespeicherte Rezepte werden beim ersten Laden automatisch migriert, pro Rezept verkettet über Schema-Version 1 → 2 → 3 → 4: eindeutige Felder werden umbenannt, die alten absoluten EC-Stockmengen bleiben unter `legacy_volume_stock_1_ml`/`legacy_volume_stock_2_ml` erhalten (markiert mit `legacy_dosing_needs_review`, als Warnung im Editor angezeigt) statt geraten-konvertiert zu werden, die alten Werte von `requested_ro_correction_l`/`sensor_circulation_enabled`/`sensor_pump_seconds`/`drain_after_process` werden unter `legacy_process_values` zur Prüfung quarantäniert, und die alten Werte von `addon_1_ml`/`addon_2_ml` werden unter `legacy_recipe_values` quarantäniert (merge-sicher: bestehende Einträge werden nie überschrieben, ein erneuter Migrationslauf ist ein No-op) — siehe `docs/OPEN_RECIPE_DECISIONS.md`. Die Rezeptdatei wird atomar geschrieben (temporäre Datei + fsync + `Path.replace()`, mit `.bak`-Backup der vorherigen Version) unter einer einzigen wiedereintrittsfähigen Sperre über die gesamte Lade-Ändern-Speichern-Sequenz; ein JSON-Parse-Fehler, falsch typisierte Metadaten, eine unbekannte zukünftige `schema_version`, doppelte Favoritenslots oder ein außerhalb des gültigen Bereichs liegender `active_slot`/`favorite.slot` lösen allesamt einen kontrollierten `RecipeBookCorruptedError` aus, statt die beschädigte Datei stillschweigend mit Defaults zu überschreiben, sich still selbst zu heilen oder mit einer rohen Exception abzustürzen.

---

## 16. Kalibrierung

```bash
python main.py calibrate-mixer
```

Liest OPC-UA-Rohwerte, speichert Messpunkte als CSV unter `calibration_data/`.

**Ablauf einer Live-Session:**

1. **0–30 L manuell**: Wasser per Eimer/Kanne zuführen, Menge eingeben, gemittelter Messpunkt (Settle-Zeit + N Samples) wie gehabt.
2. **30–200 L pumpengesteuert**: Die Mixer-Refill-Pumpe füllt automatisch, einmal 20 L (Rundung auf die erste Tankmarkierung), danach in 25-L-Schritten. Jedes Segment loggt den Rohwert **sekündlich** in eine separate `..._trace.csv`. Enter stoppt die Pumpe, sobald die Markierung erreicht ist; ein Safety-Timeout pro Segment (`calibration_fill_max_seconds`, Default 300s) schützt die Pumpe, falls das mal vergessen wird.
3. **200–0 L pumpengesteuert**: Transferpumpe + Drainventil, gleiches Prinzip in 25-L-Schritten runter bis 0 L, ebenfalls mit Trace-Log und eigenem Segment-Timeout (`calibration_drain_max_seconds`, Default 180s).

**Sicherheit:** Die früheren getippten Bestätigungsphrasen (`required_confirmation_text` etc.) und die `hardware_execution_enabled`-Abfrage wurden auf ausdrücklichen Wunsch aus diesem Skript entfernt — es wird ohnehin nur manuell, mit anwesendem Bediener am Tank, gestartet. Der reine Pumpenschutz (Segment-Timeout) blieb bewusst bestehen.

**Offline nachrechnen, ohne Hardware:**

```bash
python calibration_mixing_tank.py --analyze calibration_data/*.csv
```

Lädt beliebig viele bereits gespeicherte CSVs, poolt sie, normalisiert Nullpunkte pro Session, warnt bei nicht-monotonen Messpunkten, und rechnet mehrere Fit-Varianten (u. a. mit einem `min_reference_volume_l`-Filter, da der Sensor unter ~10 L bekannt unzuverlässig ist). Rührt garantiert kein GPIO/OPC-UA an — reine CSV-Auswertung.

**Aktuelle Formel** (`config/system_config.json` → `mixer_level_calibration`):

```text
Liter_real = 0.610566 * sensor_raw + -26.093067
R² = 0.996, max. Fehler ≈ 5.7 L, Bereich 10-175 L
```

Abgeleitet aus allen bisherigen Kalibrierungssessions (zero-normalisiert, nur Fill-Phase, ≥10 L). Ein einzelner Ausreißer-Messpunkt (erster Pump-Checkpoint der letzten Session, vermutlich ungenau abgelesene Markierung) wurde dabei bewusst als `valid_for_fit=False` markiert, siehe `invalid_reason` in der jeweiligen CSV. **175–200 L bleibt unbestätigt** — dort gibt es keine verlässliche Tankmarkierung zum Gegenchecken.

---

## 17. Aktuell offene Punkte

1. **Tank Cleaning auf Testvolumen** — `config/tank_cleaning_settings.json` ist aktuell auf 40 L (Testlauf) statt der geplanten 200 L gestellt; nach erfolgreichem erstem Hardwaretest zurück auf das Zielvolumen setzen.
2. **Tank Cleaning und Manual Drain Jog real mit Hardware validieren** — beide Funktionen sind bisher nur softwareseitig getestet, `hardware_execution_enabled` steht in beiden zugehörigen Settings-Dateien auf `false`.
3. **Kalibrierbereich 175–200 L absichern** — bisher keine verlässliche Tankmarkierung am oberen Ende zum Gegenchecken; die produktive Formel extrapoliert dort leicht über den bestätigten Bereich hinaus.
4. Modularen Water-Cycle real mit Hardware testen.
5. Logging schrittweise von `print()` auf `logging` umstellen (bisher nur `actuator_manager.py`, `cds_controller.py`).
6. MQTT-Staleness-/Payload-Reader (`services/sensor_snapshot.py`, `nicegui_dashboard/mqtt_topic_reader.py`) vereinheitlichen.
7. Kein CI, kein Linting/mypy trotz durchgängiger Typannotationen (`tests/` mit `pytest` für Locking/Watchdogs/Config-Validierung existiert inzwischen, siehe Abschnitt 9).
8. ~~Rezeptwerte kontrolliert mit Water-Cycle-Settings verbinden.~~ Erledigt: `nicegui_dashboard/recipe_store.py::build_run_config()` verbindet die zwei Rezeptfelder mit einem echten Konsumenten (`target_ro_water_l` → `fill_mode="absolute"`/`target_total_liters`, `sensor_circulation_enabled` → `enable_sensor_circulation`) beim Start von Fill-and-Measure, inklusive `max_mixer_liters`-Grenzprüfung. Die EC-Nährstoffdosierfelder des Rezepts sind jetzt vollständig typisiert, berechnet und validiert (siehe Abschnitt 15, `docs/RECIPE_AND_DOSING_RULES.md`) und als `recipe_run_config_snapshot` angehängt, bleiben aber ohne Hardware-Konsument, solange Chemikaliendosierung nicht implementiert ist (Punkt 9 unten). Die pH-Korrekturfelder sind unverändert/außerhalb des Umfangs dieser Arbeit.
9. **Peristaltikpumpensteuerung** — Firmware-seitige Härtung ist im separaten Repository `central_dosing_sys_peristaltic` (Branch `harden-serial-protocol`) abgeschlossen und auf beiden Arduino-MCUs real verifiziert: Treiber standardmäßig deaktiviert, Dosis-/Laufzeitlimits (`MAX_SINGLE_DOSE_ML`, `MAX_RUNTIME_MS`), maschinenlesbares `PING`/`STATUS`/`DOSE`/`STOP`/`STOPALL`-Protokoll (siehe dortige `docs/SERIAL_PROTOCOL.md`). `cds_control` hat jetzt einen eigenständigen, hardwarefrei getesteten Python-Serial-Client und ein Terminal-CLI dafür (`services/peristaltic/`, `scripts/peristaltic_calibration_cli.py` — discover/map/check/stop-all/test/calibrate/pair-test/all-four-test, ausschließlich Wasser, max. 10 ml pro Testdosis, siehe `docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md`), das aber bewusst **nicht** an Rezeptlogik, NiceGUI-Dashboard oder State Machine angebunden ist, und es sind **noch keine echten Peristaltikpumpen angeschlossen** — diese Anbindung sowie eine reale Pumpen-Kalibrierung/-Verifikation bleiben separate, noch nicht begonnene Schritte. Ein reiner Entwurf für die Aufteilung der gemeinsamen Firmware in ein `MCU_A_PH`/`MCU_B_EC`-Profilpaar liegt außerdem unter `docs/PERISTALTIC_PROFILE_PLAN.md` vor (keine Firmware-/Verhaltensänderung dadurch).
10. **pH-Dosierungsgrenzen noch nicht durchgesetzt** — pH-Sollwert, pH-Mischzeit und pH-Stockvolumengrenzen wurden gegen die alte Node-RED-Konfiguration bestätigt (siehe `docs/OPEN_RECIPE_DECISIONS.md`), aber bewusst nicht als harte Validierung ergänzt, da die pH-Dosierung noch keinen Konsumenten hat und sie außerhalb des explizit angeforderten Regelsatzes lagen. (EC-Sollwert-/Mischzeitgrenzen und die Grenze des EC-Anpassungsfaktors *sind* durchgesetzt — siehe Abschnitt 15. Addon-1/Addon-2-Grenzen entfallen vollständig, da Addon 1/Addon 2 vollständig aus dem aktiven Rezeptmodell entfernt wurden.) Ob/wie die verbleibenden pH-Grenzen durchgesetzt werden sollen, ist eine noch offene fachliche Entscheidung.

---

## 18. Git- und Repo-Hygiene

Nicht ins Repository sind:

```text
__pycache__/
*.pyc
.venv/
logs/*
*.log
*.out
*.bak_*
calibration_data/*.csv
archive/
```

```text
Git-History ersetzt lokale Backup-Dateien.
```

Vor Commits prüfen:

```bash
git status
git diff --check
python main.py preflight
```

---

## 19. Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 20. Entwicklungsregel

```text
Erst sicher.
Dann nachvollziehbar.
Dann automatisiert.
Dann produktiv.
```

Neue Hardwarefunktionen werden nur aufgenommen, wenn:

- die reale Hardwarezuordnung geprüft wurde,
- der Wasserpfad nachvollziehbar sicher ist,
- Preflight erfolgreich ist,
- ein Safe-Shutdown vorhanden ist,
- ein Timeout oder plausibler Abbruchpfad existiert,
- und der Ablauf zunächst mit Wasser getestet wurde.
