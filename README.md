# Central Dosing System – Python Control Layer

*This file is bilingual: English first, [German version below](#central-dosing-system--python-control-layer-deutsch). / Diese Datei ist zweisprachig: Englisch zuerst, deutsche Version weiter unten.*

Status: 2026-10-14 (see [Development status](#development-status-2026-10-14))
Project status: development and validation phase
Goal: safe, traceable, and modularly extensible control logic for the water-based CDS process.

> **Safety status in one sentence:** Hardware execution is disabled by default in this repository (`hardware_execution_enabled: false`); the plant owner has confirmed that all CDS hardware components were function-tested individually and work (repeated on 2026-10-14; Section 4). What is still outstanding are the **integrated process runs** (Water Cycle, Tank Cleaning, AP-08 transfer, dosing), not the individual components.

---

## Development status (2026-10-14)

Basis: `main` and `origin/main` (GitHub, verified with `git ls-remote origin` on 2026-10-14) both at `f463a0f`. Detailed results: Sections 21–24.

| Category | Content |
|---|---|
| Implemented, integrated into `main`, and published on GitHub (`f463a0f`) | AP-00 to AP-04 (up to `6125b1f`): test isolation, run-option forwarding, sensor/settings hardening, recipe start validation, fill safety, shared sensor readings and hardware start gates, drain safety, Water Cycle completion |
| AP-05 Stage 1 (integrated, published) | Pure domain logic `domain/recipe/nutrient_plan.py` and `domain/recipe/volume_balance.py` (commit `f463a0f`, fully integrated into `main` and published on GitHub). Integrated as code only: no process, hardware, or MQTT integration |
| Verified in software | Hardware-free test suite on `main` (`f463a0f`): 1874 passed, **1 failed** (known Recipe Editor test, Section 23); AP-05 Stage 1: 565 tests passed (`tests/test_nutrient_plan.py`, `tests/test_volume_balance.py`) |
| Integrated runs **not** yet validated | Individual components are function-tested (Section 4). For AP-00 to AP-05 no integrated hardware validation is documented: no MCU_B dosing, no chemical dosing, no real run of the hardened fill/drain/Water Cycle paths, no integrated AP-08 transfer |
| Planned next steps | all pending: AP-05 Stages 2–6 (Stage 2: hardware/profile gates, see Section 22), AP-06 to AP-09 (table below) |

### Work-package overview AP-00 to AP-09

The assignment of changes to work packages (AP-00 to AP-05) is taken from branch names (`feature/ap0x-*`) and the AP references in module and test docstrings; commit titles alone were not used. The roadmap AP-06 to AP-09 is the project's defined scope; no code for it exists yet and nothing in it is implemented.

| AP | Goal | Realized scope | Status | Main components | Open items |
|---|---|---|---|---|---|
| AP-00 | Hardware-free tests | Central fail-closed guard blocks GPIO, serial, lock file, writes to `logs/ config/ recipes/ calibration_data/`, and network connects during `pytest` | integrated | `tests/_hardware_guard.py`, `tests/_guard_site/sitecustomize.py`, `tests/conftest.py` | Guard reporting gaps for never-awaited child processes (documented in the module) |
| AP-01 | Dashboard run options | `RunOptions` chosen in the dashboard are forwarded to the process controller | integrated | `nicegui_dashboard/cds_controller.py` | none known |
| AP-02A | Sensor and settings hardening | Strict number reading (no bool/NaN/inf/overflow, no masking by fallback), stricter settings validation | integrated | `statemachine/sensor_reading.py`, `services/settings_validation.py` | none known |
| AP-02B | Safe recipe start | Pure start release for recipe runs (RO sufficient, mixer plausible, tank-empty confirmation with mixer veto) | integrated | `statemachine/start_validation.py`, `process/fill_and_measure.py` | Operator confirmation "tank empty" is not a measurement |
| AP-02C | Fill safety | Fresh-measurement check right before pump start, runtime RO/mixer checks, pump-off-first target confirmation, final error latch | integrated | `statemachine/fill_and_measure_state_machine.py`, `services/sensor_stamp.py`, `services/mqtt_topic_reader.py` | Freshness window 5 s is not hardware-validated |
| AP-03A | Shared sensor readings | Strict mixer reading in `process/common.py`; invalid values do not enter the history | integrated | `process/common.py` | none known |
| AP-03B | Hardware start gates | `BackgroundHardwareProcess` starts only with `hardware_execution_enabled is True`; sensor read errors block in a controlled way | integrated | `process/background_process.py` | none known |
| AP-04A | Drain safety | One sensor watchdog and one stop sequence for the three drain paths | integrated | `process/drain_safety.py`, `process/drain.py`, `process/manual_drain_jog.py`, `process/tank_cleaning/` | O1, provisional parameters (Section 24) |
| AP-04 | Water Cycle completion | Final outcome and cleanup are collected; final MQTT statuses derived only after cleanup | integrated | `process/water_cycle.py` | No guaranteed MQTT delivery |
| AP-05 Stage 1 | Dosing plan and volume accounting (pure logic) | Immutable plan for MCU_B, µL arithmetic, conservative volume accounting | integrated, published | `domain/recipe/nutrient_plan.py`, `domain/recipe/volume_balance.py` | Not wired into any process |
| AP-05 Stages 2–6 | Further MCU_B dosing steps (Stage 2 = hardware/profile gates, per module docstring); Stages 3–6 are not detailed in the repository | AP-specific implementation not yet started | pending | – | MCU_B identity, calibration, profile, firmware gates |
| AP-06 | EC control and correction logic | AP-specific implementation not yet started | pending | – | Whole scope |
| AP-07 | MCU_A integration and pH control | AP-specific implementation not yet started | pending | – | Whole scope |
| AP-08 | Transfer into the target container | Controlled transfer of the Mixing Tank content into exactly one Solution Tank (1-4): operator-selected tank, read-off tank level and capacity confirmation checked against the 300 L nominal volume and the mixer amount at transfer start, separate acknowledgement for `NOT_EVALUATED`, release bound to the last finished mixing run and consumed only by a successfully completed transfer (retry after stop/failure with a new release, fresh read-off level and fresh mixer measurement), valve open, settle, stop re-check, then pump; pump off before valve close (failed pump-off: valve stays open, critical error, physical emergency stop required); first plausible 0 L ends the transfer; drain-style source monitoring; Emergency Stop, process interlock, dashboard status and process log. Software only, integrated hardware run pending | implemented (hardware-free tests; components individually function-tested) | `process/target_transfer.py`, `process/drain_safety.py`, `nicegui_dashboard/process_controller.py` | No target tank sensors: no overflow protection, safety reserve rests on the operator confirmation; first supervised water test (Section 24) |
| AP-09 | Overall integration, hardware validation, and system tests | AP-specific implementation not yet started | pending | – | All hardware validation items (Section 24) |

---

## 1. Overview

This repository contains the Python implementation of the control and visualization layer for a Central Dosing System (Raspberry Pi, GPIO relays, OPC-UA sensors, MQTT/Node-RED). The current focus is the safe, water-only base process:

```text
RO water → Mixing Tank → Sensor box circulation → Drain
```

Chemical dosing and peristaltic pump control are not yet active. These functions will be added after further hardware, sensor, and safety validation. Recipe execution is so far only meaningfully connected to the actual process start for the pure water target value (fill volume, sensor circulation) — see Section 15. The EC/pH/dosing values in the recipe remain purely descriptive, with no consumer in the process code. Since AP-05 Stage 1 a pure, hardware-free dosing plan and volume accounting exist as domain logic (Section 22); they are not connected to any process or hardware.

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
- hardware-free test isolation (AP-00), strict sensor/settings handling and recipe start validation (AP-02A/B), fill runtime safety (AP-02C), shared sensor readings and strict hardware start gates (AP-03), drain safety for all three drain paths (AP-04A), and Water Cycle outcome/cleanup handling with final MQTT status (AP-04) — software only, see Section 21
- pure domain logic for an immutable MCU_B nutrient dosing plan and a conservative volume balance (`domain/recipe/nutrient_plan.py`, `domain/recipe/volume_balance.py`, AP-05 Stage 1, integrated in `main` and published on GitHub) — no process, dashboard, or hardware integration, see Section 22

The modular Water Cycle starts correctly on the software side. A full real hardware run after the latest refactor and after the AP-02 to AP-04 hardening is still outstanding. For Manual Drain Jog and Tank Cleaning the components are function-tested individually, but their integrated runs are likewise not yet validated (see Sections 17 and 24).

---

## 4. Hardware validation status (important!)

**Individual component function tests:** the plant owner confirms that all CDS hardware components (transfer pump, target valves 1-4, further valves and pumps, existing sensors, and peristaltic components) were each tested individually and found functional; the function test was repeated on 2026-10-14. This is a statement about the individual components, not about integrated process sequences. The software documents it as a confirmation by the plant owner; no test protocol is stored in the repository.

**Not covered by this:** integrated process runs (Water Cycle, Tank Cleaning, Manual Drain, AP-08 target transfer, dosing) have not yet been run on the real plant after the AP-02 to AP-08 changes.

The following outputs were additionally validated earlier within real process runs:

| Function              | Config key              | GPIO (BCM) | Physical pin | Status              |
|------------------------|------------------------|:----------:|:---------------:|----------------------|
| Mixer Refill Pump       | `mixer_refill_pump`    | 20         | 38               | validated            |
| Supply/Test Valve 6     | `test_supply_valve_6`  | 6          | 31               | validated            |
| Drain Valve 0           | `valve_0_drain`        | 21         | 40               | validated            |

All other entries in `gpio_config.py` (`contactor_0/5`, `transfer_pump`, `mixing_circulation_pump`, `sensor_circulation_pump`, `valve_1`–`valve_9`) are covered by the individual function test above, but have **not** been validated within an integrated process run documented here. In particular:

- **Transfer Pump** (`transfer_pump`, GPIO26) is used in `calibration_mixing_tank.py`, `main_safe_drain.py`, `process/tank_cleaning/`, and the AP-08 transfer. It is switched through a GPIO-controlled contactor without feedback; the individual function test is confirmed, the integrated AP-08 sequence is still pending.
- **Valve 5** is noted in a `gpio_config.py` comment as not reliably sealing.
- **Tank Cleaning** additionally drives `mixing_circulation_pump` and `sensor_circulation_pump` – individually function-tested, integrated run pending. The settings file (`config/tank_cleaning_settings.json`) is therefore deliberately set to a reduced **40-liter test volume** instead of the later 200-liter target, so the first hardware test can be run without major water consumption/risk.

Before a new output is used productively: switch it on the real system, confirm the effect visually/physically, only then include it in an automated sequence.

The AP-00 to AP-05 changes (Sections 21 and 22) did not change this table: they are software-verified only. The MCU_B peristaltic outputs (P1–P4) are not part of it and are not released for dosing (Section 24).

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
├── gpio_config.py
├── config/
│   ├── system_config.json
│   ├── water_cycle_settings.json
│   ├── calibration_settings.json
│   ├── process_settings.json
│   └── tank_cleaning_settings.json
├── domain/
│   ├── recipe/                       # limits.py, calculations.py, field_validators.py, validation.py, models.py,
│   │                                 # nutrient_plan.py, volume_balance.py (AP-05 Stage 1, pure logic, not wired in)
│   ├── recipe_limits.py              # compat wrapper -> domain/recipe/
│   └── recipe_model.py               # compat wrapper -> domain/recipe/models.py
├── statemachine/
│   ├── fill_and_measure_state_machine.py
│   ├── sensor_reading.py             # strict mixer/RO liter reading (AP-02A)
│   └── start_validation.py           # pure recipe start release / runtime check (AP-02B/C)
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   ├── drain_safety.py               # drain sensor watchdog, safe stop sequence (AP-04A)
│   ├── water_cycle.py
│   ├── auto_circulation.py
│   ├── manual_drain_jog.py
│   ├── fill_and_measure.py           # FillAndMeasureController
│   ├── pump_prime.py
│   ├── tank_cleaning/                # controller.py, phases.py, fill/hold/drain_phase.py, status_publisher.py
│   ├── watchdog.py
│   └── background_process.py         # BackgroundHardwareProcess base class
├── services/
│   ├── system_config.py
│   ├── mqtt_publisher.py
│   ├── mqtt_topic_reader.py          # unified MQTT single-topic reader
│   ├── process_run_logger.py
│   ├── sensor_stamp.py               # measurement identity for freshness checks (AP-02C)
│   ├── settings_validation.py
│   └── peristaltic/                  # protocol/safety/models/firmware_profiles/prime/session_log/
│                                      # serial_client/dose_limits/parallel_pump_safety/calibration_*.py
├── hardware/
│   ├── digital_output.py
│   └── actuator_manager.py
├── calibration/                      # config/models/cli_input/sensor_reading/measurement_csv/
│                                      # pump_segments/analysis/session/cli.py
├── preflight/                        # report/files_and_syntax/disk_space/gpio/services/mqtt/opcua/run.py
├── nicegui_dashboard/
│   ├── app.py
│   ├── cds_controller.py
│   ├── process_controller.py         # coordinator: exclusivity, status aggregation, recipe merge
│   ├── components/                   # formatting.py, status_widgets.py
│   ├── recipe_store/                 # book_io.py, migrations.py, queries.py, run_config.py
│   ├── pages/
│   │   ├── dashboard_page.py         # thin scaffold, ~190 lines
│   │   └── dashboard/                # one file per dashboard section (recipe_text, status_actuators,
│   │                                  # maintenance, recipe, process_control, prime, sensor_tanks)
│   └── static/
├── scripts/
│   ├── check_gpio_conflicts.py
│   ├── peristaltic_calibration_cli.py
│   ├── peristaltic/                  # discover.py
│   └── manual_tests/
├── recipes/
│   └── dashboard_recipes.json
├── tests/                            # incl. _hardware_guard.py, _guard_site/ (AP-00 hardware isolation)
├── logs/
├── calibration_data/
├── mqtt_sensor_bridge.py
├── calibration_mixing_tank.py        # compat wrapper -> calibration/cli.py
├── main_safe_drain.py
├── main_refill_and_drain_test.py
├── preflight_check.py                # compat wrapper -> preflight/run.py
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

> Note: this file is now the single source of truth for the OPC-UA endpoint, MQTT connection, and mixer calibration — `mqtt_sensor_bridge.py`, `calibration_mixing_tank.py`, `preflight_check.py`, as well as the dashboard MQTT layer (`services/mqtt_publisher.py`, `services/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py`) and the calibration-factor fallback in `statemachine/fill_and_measure_state_machine.py` now all read from `get_mqtt_config()`/`get_mixer_level_calibration()` instead of their own hardcoded values.

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
- Chemical dosing and peristaltic pump control are not currently active. The AP-05 Stage 1 dosing plan is pure logic and releases nothing.
- Sensor values are not blindly trusted as ground truth (filtering, plausibility checks, confirm samples).
- Actuators are switched off via central safe-shutdown paths (`ActuatorManager.safe_shutdown_all()`); errors during shutdown are logged, not swallowed.
- Manual calibration drain functions have a safety timeout (`calibration_drain_max_seconds`).
- `KeyboardInterrupt` is handled uniformly across all Water Cycle phases and aborts the rest of the run in a controlled way.
- Manual Drain Jog and Tank Cleaning run as background threads whose loops check a `threading.Event` roughly every 0.5s — an Emergency Stop from the dashboard therefore interrupts them within under a second, instead of waiting for the current phase to finish.
- Start/stop of Fill-and-Measure, Manual Drain Jog, and Tank Cleaning are atomically guarded by the internal `ProcessController` lock: two concurrent start calls (a dashboard click plus a script started in parallel) can no longer both get through — previously a TOCTOU race between the state check and the actual start.
- Additionally, a cross-process file lock (`fcntl.flock`, `hardware/actuator_manager.py`, `logs/.hardware.lock`) prevents parallel hardware access by two independent Python *processes* (not just threads within the same process) — e.g. the dashboard plus a directly started calibration or safe-drain script.
- Emergency Stop is implemented asynchronously (`ProcessController.emergency_stop()`): signalling (state-machine abort, `request_stop()`, actuator shutdown) happens immediately under the lock; waiting for the thread join then runs outside the lock in a worker thread — the dashboard stays responsive for other connected clients in the meantime, instead of freezing for up to ~7s.
- Since AP-03B, hardware start is released only for `hardware_execution_enabled is True` (strict type, no coercion). Since AP-02B/C, recipe runs need a fail-closed start release and fresh sensor measurements; since AP-04A, all drain paths are bounded by a sensor watchdog; since AP-04, the Water Cycle reports cleanup errors instead of success (Section 21).
- Switch-off is a software call without physical feedback; this limit is documented, not solved (Section 24).

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
(`process/watchdog.py`), settings validation (`services/settings_validation.py`,
including all four real `config/*.json` files), and since AP-00 to AP-05 also
start validation, fill/drain safety, Water Cycle outcome, and the dosing plan/volume
logic. A central guard (`tests/_hardware_guard.py`) blocks GPIO, serial, network, and
writes to production data directories, so no GPIO/OPC-UA/MQTT is touched in the
process. Current result and known failures: Section 23. `requirements-dev.txt` (just `pytest` on top of
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

> The modular Water Cycle still needs one full real hardware test after the latest refactor. Since AP-04 it collects the final outcome and cleanup errors and sends `WATER_CYCLE_FINISHED` only after a successful cleanup (Section 21); MQTT delivery of status messages is not guaranteed.

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
process/tank_cleaning/         → TankCleaningController
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

EC nutrient dosing uses a single total dose (`nutrients_ml_per_100_l`) split between Nutrient Solution A and B by a percentage (`nutrient_a_percent`, default 50 %); B is always calculated as `100 - A`, never edited independently. `nutrient_dosing_enabled` (default off) gates whether a dose is actually calculated anywhere — while off, every calculated amount is forced to 0 ml, including in the estimated volume. A recipe still flagged for legacy review (`legacy_dosing_needs_review`) cannot enable dosing until the editor's "Legacy review completed" switch explicitly clears that flag. The EC setpoint field uses a stepper (`0.05` in `nicegui_dashboard/pages/dashboard/recipe_section.py`; a test still expects `0.1`, see Section 23) as a UI convenience only — arbitrary in-range values (e.g. `2.35`) are accepted and stored unchanged. No claim is made that real chemical dosing has been tested — this logic is fully implemented and unit-tested as pure domain logic, but no real peristaltic pump is connected yet (see below).

**Actually effective** (`nicegui_dashboard/recipe_store/run_config.py::build_run_config()`, called from `ProcessController.start_fill_and_measure()`): `target_ro_water_l` determines the real fill target volume (forces `fill_mode="absolute"`); `sensor_circulation_enabled`, sourced from a fresh `RunOptions` chosen in the Process Control panel's confirmation area (not the recipe, not persisted, always defaults to off, and only reset back to off after a start is actually accepted — a blocked start leaves the choice untouched so the user can correct and retry), determines whether the sensor-box circulation pump is driven during the run. Both are checked against `max_mixer_liters` and the `process_settings.json` schema — a recipe target volume above the mixer's capacity blocks the start with a clear message, instead of starting the process and only aborting mid-run via the watchdog. `RunConfigSnapshot.build(recipe, process_settings, run_options)` is the only place these are combined; everything downstream reads exclusively from that immutable snapshot. `ProcessController` stores the exact snapshot a run was actually started with, and the dashboard's status keeps showing *that* snapshot — not a freshly recomputed one — for as long as the process is running, even if a different favorite is activated in the meantime; only the next run picks up the newly active recipe.

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
6. ~~Unify the MQTT staleness/payload readers.~~ Done: `services/sensor_snapshot.py` (`SensorSnapshotReader`) and the old `nicegui_dashboard/mqtt_topic_reader.py` (`MqttTopicReader`) were merged into one class in `services/mqtt_topic_reader.py`, with locking, error tracking, and a sentinel-based `get_latest(max_age_seconds=...)` — a deliberate, small, documented behavior change (see the commit message on that change for the exact deltas), not a pure move.
7. No CI, no linting/mypy despite consistent type annotations (a hardware-free `pytest` suite exists, see Sections 9 and 23).
8. ~~Connect recipe values to Water Cycle settings in a controlled way.~~ Done: `nicegui_dashboard/recipe_store/run_config.py::build_run_config()` connects the two recipe fields that have a real consumer (`target_ro_water_l` → `fill_mode="absolute"`/`target_total_liters`, `sensor_circulation_enabled` → `enable_sensor_circulation`) when starting Fill-and-Measure, including a `max_mixer_liters` bound check. The recipe's EC nutrient dosing fields are now fully typed, calculated, and validated (see Section 15, `docs/RECIPE_AND_DOSING_RULES.md`) and attached as `recipe_run_config_snapshot`, but stay without a hardware consumer until chemical dosing is implemented (item 9 below). pH correction fields are unchanged/out of scope for that work.
9. **Peristaltic pump control** — firmware-side hardening is complete in the separate `central_dosing_sys_peristaltic` repository (branch `harden-serial-protocol`) and verified on both Arduino MCUs on real hardware: drivers disabled by default, dose/runtime limits (`MAX_SINGLE_DOSE_ML`, `MAX_RUNTIME_MS`), a machine-readable `PING`/`STATUS`/`DOSE`/`STOP`/`STOPALL` protocol (see `docs/SERIAL_PROTOCOL.md` there). `cds_control` now has a standalone, hardware-free-tested Python serial client and terminal CLI for this protocol (`services/peristaltic/`, `scripts/peristaltic_calibration_cli.py` — discover/map/check/stop-all/test/calibrate/pair-test/all-four-test, water-only, max 10 ml per test dose, see `docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md`), but it is deliberately **not** wired into the recipe logic, the NiceGUI dashboard, or the state machine, and **no real peristaltic pumps are connected** — that integration, and real per-pump calibration/verification, remain separate, not-yet-started steps. A design-only plan for splitting the shared firmware into an `MCU_A_PH`/`MCU_B_EC` profile pair also exists at `docs/PERISTALTIC_PROFILE_PLAN.md` (no firmware/behavior changed by it).
10. **pH dosing bounds not yet enforced** — pH setpoint, pH mixing-time, and pH stock-volume bounds were confirmed against the legacy Node-RED configuration (see `docs/OPEN_RECIPE_DECISIONS.md`) but intentionally not added as hard validation, since pH dosing has no consumer yet and they were outside the explicitly requested rule set. (EC setpoint/mixing-time bounds and the EC adjustment factor bound *are* enforced — see Section 15. Addon 1/Addon 2 bounds no longer apply at all, since Addon 1/Addon 2 were removed from the active recipe model entirely.) Whether/how to enforce the remaining pH bounds is a product decision, not yet made.
11. **AP-05 Stage 1 integrated but not wired in** — commit `f463a0f` is in `main` and on GitHub; no process, dashboard, or hardware code uses `domain/recipe/nutrient_plan.py` / `volume_balance.py` (Section 22). Stages 2–6 and AP-06 to AP-09 are pending.
12. **Known test failure** — Recipe Editor step test (Section 23).
13. **Open safety and hardware questions** (O1, switch-off feedback, level < ~10 L, dry running, provisional drain parameters, kill/SIGTERM limits, `DigitalOutput` cleanup, MQTT delivery, MCU_B identity/calibration gates) — Section 24.

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

## 21. Work-package results AP-00 to AP-04

All items below are software results, verified by hardware-free tests (Section 23). None of them has been run on the real system.

**AP-00 – Isolated automated tests.** `tests/_hardware_guard.py` is installed from `tests/conftest.py` before collection and, for child processes, through `tests/_guard_site/sitecustomize.py`. It blocks GPIO (`gpiozero` pin factory, `lgpio`), serial devices, network connects (MQTT/OPC-UA), and writes to `logs/`, `config/`, `recipes/`, and `calibration_data/`; the hardware lock file is redirected to a temporary path. Every blocked attempt is recorded and fails the test, even if the caller swallows the exception. No production code was changed for this. Known gap: the exit code of a child process that is never awaited is not seen if its report fails (documented in the module).

**AP-01 – Run options.** `RunOptions` selected in the dashboard are forwarded to the process controller (`nicegui_dashboard/cds_controller.py`).

**AP-02 – Recipe and runtime validation, sensor checks, fill safety.**

- AP-02A: `statemachine/sensor_reading.py` accepts only real finite numbers (no `bool`, NaN, inf, or integers too large for a float); an invalid preferred value is never replaced by a less reliable fallback. `services/settings_validation.py` validates settings more strictly.
- AP-02B: `statemachine/start_validation.py` is a pure, fail-closed release check for recipe runs. Block reasons: `run_config_invalid`, `sensor_invalid`, `mixer_invalid`, `mixer_implausible`, `mixer_at_target`, `ro_invalid`, `ro_capacity_invalid`, `ro_insufficient`, `tank_not_confirmed`, `empty_veto`. The operator confirmation "mixing tank emptied" is not a measurement; the mixer value is only a veto against a contradiction, because the calibration is not reliable below about 10 L.
- AP-02C: in recipe-run mode the fill state machine repeats the release check on a new measurement immediately before the pump is switched on, requires a new MQTT message with a later bridge timestamp (`services/sensor_stamp.py`) within 5 s, checks RO and mixer values unfiltered on every tick, switches the pump off first on reaching the target and confirms the target only with the pump off, and latches the first error as final (a concurrent emergency stop cannot be overwritten or followed by another switch-on). The 5 s window is not hardware-validated.

**AP-03 – Hardware start gates and exclusion.**

- AP-03A: `process/common.py` reads the mixer level strictly; the finite check happens before clamping to zero, and invalid readings never enter the history.
- AP-03B: `BackgroundHardwareProcess` releases hardware only if `hardware_execution_enabled is True` (no `bool()` coercion: strings, `1`, `None`, missing key, or non-dict settings block); an exception while reading the sensor snapshot blocks the start in a controlled way.
- Mutual exclusion of Fill-and-Measure, Manual Drain Jog, and Tank Cleaning (`ProcessController` lock) and the cross-process `fcntl.flock` lock are older mechanisms (Section 8), not new in AP-03.

**AP-04 – Drain safety, process errors, Water Cycle completion.**

- AP-04A: `process/drain_safety.py` provides, for `process/drain.py`, `process/tank_cleaning/drain_phase.py`, and `process/manual_drain_jog.py`, a `DrainSensorWatchdog` (plausibility, sensor loss, no-progress, unexpected rise on the unfiltered mixer level) and one stop sequence `safe_stop_drain` (transfer pump off, then drain valve off; both always attempted, errors collected). Drain settings are validated before any actuator is touched; the Manual Drain Jog has a binding maximum of 30 s. A failed switch-off is never reported as success.
- AP-04: `process/water_cycle.py` collects the outcome (`finished`, `aborted`, `interrupted`, `startup_failed`, `error`; default fail-closed `error`) and derives exit code and final status only after the complete cleanup. Each cleanup step is isolated; its exception is recorded, not propagated.
- Final MQTT statuses: `WATER_CYCLE_FINISHED` is sent only if cleanup had no error; the final status is `SAFE_SHUTDOWN` or `SAFE_SHUTDOWN_FAILED` and carries `cleanup_errors`, `physical_switch_off_confirmed: false`, and `status_publish_failures`.
- **MQTT delivery is not guaranteed.** QoS 1 with a short `wait_for_publish` timeout (`services/mqtt_publisher.py`). A status publish never raises into the cleanup. Failure handling in `process/water_cycle.py`:
  - `publish_json() == False` or an exception is counted in `status_publish_failures` and logged, but does not by itself change the exit code.
  - A `KeyboardInterrupt` during `_publish()` is registered as a cleanup error; the final exit code can therefore become 1.
  - One corrective final MQTT status is attempted exactly once if and only if the preceding final publish registered a new cleanup error, for example through `KeyboardInterrupt`. With an ordinary `publish_json() == False` or a caught regular exception without a new cleanup error, there is no retry.
  - `mqtt_publisher.close()` runs after the final status message. An error there can lead to local exit code 1 without being contained in the last published MQTT status.

## 22. AP-05 Stage 1 – dosing plan and volume accounting (pure domain logic)

`domain/recipe/nutrient_plan.py` and `domain/recipe/volume_balance.py` do no I/O: no hardware, no MQTT, no sensor, no file, no clock. Nothing in `process/`, `statemachine/`, or `nicegui_dashboard/` imports them yet. **The existence of a dosing plan is not a release of real MCU_B dosing and is not part of any process.**

- Input is only the validated `RunConfigSnapshot`; totals, A/B split, and volume are recomputed with the existing functions and compared against the snapshot; any contradiction is a `NutrientPlanError` with a machine-readable code.
- `NutrientDosingPlan` is immutable (frozen dataclasses, read-only mappings, plan hash).
- Pump roles MCU_B: P1 and P3 for solution A, P2 and P4 for solution B (canonical private tuples, public views read-only).
- Exact arithmetic in integer microliters. Per solution, the first pump (P1/P2) receives `floor(amount * w1/(w1+w2) + 1/2)`, the second pump (P3/P4) the remainder, so the sum is exact. Weights must be passed explicitly (profile `firmware_ml_per_step`); there is no default weighting and no 50/50 fallback.
- Bounded chunking: `n = ceil(target / max_chunk)`, equal chunks, no zero chunks, no tiny remainder chunk; reference limit 50 ml (firmware limit, checked by a test); at most 20 000 chunks per pump, otherwise the plan is rejected (memory protection; amounts are never altered).
- `volume_balance.py` separates four evidence classes that are never mixed: planned, protocol-confirmed (a `DONE` is a protocol event, not proof of delivered volume), sensor-measured (total volume only), and unconfirmed (in flight / uncertain). There is no independent RO flow measurement; the RO share is only an indirect estimate and is never used as a limit.
- Limits: RO water 180 L (`MAX_RECIPE_RO_WATER_L`), process volume 185 L (`MAX_PROCESS_VOLUME_L`); comparisons are exact (`fractions.Fraction`), capacity is rounded down to whole µL.
- In-flight and uncertain nutrient amounts block target confirmation, final-volume evaluation, and RO resume; evaluation functions return a reason code instead of raising.
- Invalid numbers (bool, NaN, inf, huge integers, wrong types) end in controlled errors; the chunk budget prevents excessive allocation.
- Reference test state: 565 tests passed (`tests/test_nutrient_plan.py` 373, `tests/test_volume_balance.py` 192), independently reviewed.
- Open for Stage 2: MCU identity, firmware/profile, and calibration gates; transfer into a process.

## 23. Test status and limits

Strategy: `pytest` with the AP-00 guard; all tests are hardware-free. Process, state-machine, and MQTT tests use fakes; GPIO, serial, and network are blocked structurally, not only by convention.

| Run | Result |
|---|---|
| Full suite, main worktree (`f463a0f`, 2026-10-14) | 1874 passed, **1 failed** |
| AP-05 Stage 1 tests only | 565 passed |
| Full suite, separate worktree `../cds_control_ap05_s1` | 1873 passed, 2 failed, 3 errors (the known failure plus the missing-file effects below) |

- **Known failure:** `tests/test_dashboard_page_recipe_editor.py::test_recipe_editor_number_fields_have_field_appropriate_step` expects an EC setpoint step of `0.1`; `nicegui_dashboard/pages/dashboard/recipe_section.py` sets `0.05`. Which value is intended is not decided; this README does not claim either. It is unrelated to AP-00 to AP-05.
- **Separate worktree failures (environmental):** the file `recipes/dashboard_recipes.json` is git-ignored runtime data and missing in that worktree. Tests that read the real recipe book (`tests/test_recipe_run_config.py::test_real_active_recipe_merged_onto_real_settings_passes_schema` and two tests in `tests/test_process_locking.py`) fail there because the code tries to create `recipes/` and the AP-00 guard correctly blocks that (`os.mkdir('recipes')`). This is a missing local file, not a product defect; the same file is present in the main worktree.
- The suite is therefore **not** fully green; no statement "all tests pass" is made. Passing tests do not prove hardware behavior.

## 24. Open safety and hardware questions

| Topic | Description | Assigned to |
|---|---|---|
| No physical switch-off feedback | Switch-off is a software call; `physical_switch_off_confirmed` is always `false` | AP-09 |
| O1 | Drain paths: `safe_stop_drain()` (`process/drain_safety.py`) still attempts to close the drain valve after a failed `transfer_pump.off()`. **AP-08 transfer decides differently:** if `pump.off()` fails, the target valve is deliberately **not** closed (not against a possibly running pump), the actuators stay held, the error is critical, the operator must press the physical emergency stop (independent of the Raspberry Pi, not software-controllable), and no further transfer is possible until the fault is fixed and acknowledged (Reset / Ack). Without contactor feedback software cannot prove that the pump stopped. O1 for the drain paths remains an open decision | AP-09 |
| Level below about 10 L | The mixer calibration is fitted only from about 10 L. Plant owner confirmation (AP-08): when draining, the first displayed 0 L means the Mixing Tank is actually empty; afterwards the values may fluctuate again. The transfer therefore ends on the first fresh, valid 0 L value that fits the drain course (previous plausible value already at or below 10 L); a transfer start below 10 L is blocked. The generic drain paths are unchanged and still require several empty samples | AP-09 |
| Dry running | Nothing in software can prevent transfer pump dry running; it only bounds the run time on sensor loss/no progress | AP-09 |
| Provisional drain parameters | Sensor-loss limit 5 s and the defaults for pump flow (16 L/min), timeout buffer (180 s), valve settle time (1 s), and no-progress/rise limits in `DrainSafetyLimits`: not hardware-validated | AP-09 |
| SIGTERM / kill | Cleanup runs only if the process can execute it; after `SIGKILL` or a hard crash no software cleanup is possible, outputs may stay in their last state | AP-09 |
| `DigitalOutput` cleanup | `hardware/digital_output.py`: `off()` does nothing if the software state is inactive; `close()` does not reach `device.close()` if `off()` raises. A confirmed software defect must be fixed before any affected hardware release; AP-09 remains responsible for hardware and system validation | software fix first, then AP-09 |
| MQTT delivery | Status messages are not guaranteed to arrive (Section 21) | accepted limitation |
| MCU_B identity and calibration | Identity check, firmware/profile match, and per-pump calibration releases are not completed | AP-05 Stage 2 |
| Chemical dosing | Not released. No chemical dosing before all hardware gates have passed | AP-05 Stage 2 / AP-09 |
| Target tank overflow (AP-08) | The four Solution Tanks (300 L nominal, manually readable level scale) have no sensors. The software only checks the operator's read-off level against the 300 L nominal volume and the mixer amount at transfer start; it defines no safety reserve and does not include the mixer sensor uncertainty. Both rest on the explicit operator confirmation and supervision. An automatic overflow protection is not possible at present and remains a documented later extension | later extension |
| Transfer sequence (AP-08) | `valve_1`-`valve_4` and `transfer_pump` are function-tested individually (plant owner). The integrated AP-08 sequence (valve, settle, pump, monitoring, stop) has not yet been run on the real plant; pump rate (16 L/min) and the transfer timeout derive from the provisional drain parameters. A transfer that was stopped or failed does not consume the mixing run's release (new release, new read-off level and new mixer measurement required; after a fault or emergency stop only after acknowledgement); only a successfully completed transfer does | AP-09 |

The assignment of open items to AP-05 Stage 2 and AP-09 follows the roadmap (AP-09: overall integration, hardware validation, system tests); the individual assignment is the author's reading of that scope.

---
---

# Central Dosing System – Python Control Layer (Deutsch)

*Deutsche Version. [English version above](#central-dosing-system--python-control-layer).*

Stand: 14.10.2026 (siehe [Entwicklungsstand](#entwicklungsstand-2026-10-14))
Projektstatus: Entwicklungs- und Validierungsphase
Ziel: sichere, nachvollziehbare und modular erweiterbare Steuerungslogik für den wasserbasierten CDS-Prozess.

> **Sicherheitsstatus in einem Satz:** Hardwareausführung ist im Repository standardmäßig deaktiviert (`hardware_execution_enabled: false`); der Anlagenverantwortliche hat bestätigt, dass sämtliche CDS-Hardwarekomponenten bereits einzeln getestet wurden und funktionsfähig sind (heute, 14.10.2026, erneut geprüft; Abschnitt 4). Ausstehend sind die **integrierten Prozessläufe** (Water Cycle, Tank Cleaning, AP-08-Transfer, Dosierung), nicht die einzelnen Komponenten.
---

## Entwicklungsstand (2026-10-14)

Grundlage: `main` und `origin/main` (GitHub, am 2026-10-14 per `git ls-remote origin` geprüft) stehen beide auf `f463a0f`. Detailergebnisse: Abschnitte 21–24.

| Kategorie | Inhalt |
|---|---|
| Implementiert, in `main` integriert und auf GitHub veröffentlicht (`f463a0f`) | AP-00 bis AP-04 (bis `6125b1f`): Testisolierung, Weitergabe der Laufoptionen, Sensor-/Settings-Härtung, Rezeptstartvalidierung, Fill-Sicherheit, gemeinsame Sensorauswertung und Hardware-Start-Gates, Drain-Sicherheit, Water-Cycle-Abschluss |
| AP-05 Stufe 1 (integriert, veröffentlicht) | Reine Fachlogik `domain/recipe/nutrient_plan.py` und `domain/recipe/volume_balance.py` (Commit `f463a0f`, vollständig in `main` integriert und auf GitHub veröffentlicht). Nur als Code integriert: keine Prozess-, Hardware- oder MQTT-Anbindung |
| Softwareseitig geprüft | Hardwarefreie Testsuite auf `main` (`f463a0f`): 1874 bestanden, **1 fehlgeschlagen** (bekannter Recipe-Editor-Test, Abschnitt 23); AP-05 Stufe 1: 565 Tests bestanden (`tests/test_nutrient_plan.py`, `tests/test_volume_balance.py`) |
| Integrierte Läufe **nicht** validiert | Die Einzelkomponenten sind funktionsgeprüft (Abschnitt 4). Für AP-00 bis AP-05 ist keine integrierte Hardwarevalidierung dokumentiert: keine MCU_B-Dosierung, keine chemische Dosierung, kein realer Lauf der gehärteten Fill-/Drain-/Water-Cycle-Pfade, kein integrierter AP-08-Transfer |
| Geplante nächste Schritte | alle ausstehend: AP-05 Stufen 2–6 (Stufe 2: Hardware-/Profil-Gates, siehe Abschnitt 22), AP-06 bis AP-09 (Tabelle unten) |

### Arbeitspaket-Übersicht AP-00 bis AP-09

Die Zuordnung der Änderungen zu Arbeitspaketen (AP-00 bis AP-05) stammt aus den Branchnamen (`feature/ap0x-*`) und den AP-Verweisen in Modul- und Test-Docstrings; Commit-Titel allein wurden nicht verwendet. Die Roadmap AP-06 bis AP-09 ist der definierte Projektumfang; dafür existiert noch kein Code, nichts davon ist implementiert.

| AP | Ziel | Realisierter Umfang | Status | Wesentliche Komponenten | Offene Punkte |
|---|---|---|---|---|---|
| AP-00 | Hardwarefreie Tests | Zentraler fail-closed Guard blockiert in `pytest` GPIO, Seriell, Lock-Datei, Schreibzugriffe auf `logs/ config/ recipes/ calibration_data/` und Netzwerkverbindungen | integriert | `tests/_hardware_guard.py`, `tests/_guard_site/sitecustomize.py`, `tests/conftest.py` | Meldelücken bei nie abgewarteten Kindprozessen (im Modul dokumentiert) |
| AP-01 | Dashboard-Laufoptionen | Im Dashboard gewählte `RunOptions` werden an den Prozess-Controller weitergegeben | integriert | `nicegui_dashboard/cds_controller.py` | keine bekannt |
| AP-02A | Sensor- und Settings-Härtung | Strikte Zahlenauswertung (kein bool/NaN/inf/Überlauf, keine Maskierung durch Fallback), strengere Settings-Validierung | integriert | `statemachine/sensor_reading.py`, `services/settings_validation.py` | keine bekannt |
| AP-02B | Sicherer Rezeptstart | Reine Startfreigabe für Rezeptläufe (RO ausreichend, Mixer plausibel, Bestätigung „Tank leer“ mit Mixer-Veto) | integriert | `statemachine/start_validation.py`, `process/fill_and_measure.py` | Operator-Bestätigung „Tank leer“ ist keine Messung |
| AP-02C | Fill-Sicherheit | Aktualitätsprüfung unmittelbar vor Pumpe-an, laufende RO-/Mixer-Prüfung, Pumpe-aus-zuerst-Zielbestätigung, endgültiger Fehlerzustand | integriert | `statemachine/fill_and_measure_state_machine.py`, `services/sensor_stamp.py`, `services/mqtt_topic_reader.py` | Aktualitätsfenster 5 s nicht hardwarevalidiert |
| AP-03A | Gemeinsame Sensorauswertung | Strikte Mixer-Auswertung in `process/common.py`; ungültige Werte gelangen nicht in die Historie | integriert | `process/common.py` | keine bekannt |
| AP-03B | Hardware-Start-Gates | `BackgroundHardwareProcess` startet nur bei `hardware_execution_enabled is True`; Sensor-Lesefehler blockieren kontrolliert | integriert | `process/background_process.py` | keine bekannt |
| AP-04A | Drain-Sicherheit | Ein Sensor-Watchdog und eine Stopp-Sequenz für die drei Drain-Pfade | integriert | `process/drain_safety.py`, `process/drain.py`, `process/manual_drain_jog.py`, `process/tank_cleaning/` | O1, vorläufige Parameter (Abschnitt 24) |
| AP-04 | Water-Cycle-Abschluss | Endergebnis und Cleanup werden gesammelt; finale MQTT-Status erst nach dem Cleanup abgeleitet | integriert | `process/water_cycle.py` | Keine garantierte MQTT-Zustellung |
| AP-05 Stufe 1 | Dosierplan und Volumenbilanz (reine Logik) | Unveränderlicher Plan für MCU_B, µL-Arithmetik, konservative Volumenbilanz | integriert, veröffentlicht | `domain/recipe/nutrient_plan.py`, `domain/recipe/volume_balance.py` | In keinen Prozess eingebunden |
| AP-05 Stufen 2–6 | Weitere MCU_B-Dosierschritte (Stufe 2 = Hardware-/Profil-Gates, laut Modul-Docstring); Stufen 3–6 sind im Repository nicht detailliert | AP-spezifische Umsetzung noch nicht begonnen | ausstehend | – | MCU_B-Identität, Kalibrierung, Profil, Firmware-Gates |
| AP-06 | EC-Regelung und Korrekturlogik | AP-spezifische Umsetzung noch nicht begonnen | ausstehend | – | Gesamter Umfang |
| AP-07 | MCU_A-Integration und pH-Regelung | AP-spezifische Umsetzung noch nicht begonnen | ausstehend | – | Gesamter Umfang |
| AP-08 | Transfer in den Zielbehälter | Kontrollierter Transfer des Mixing-Tank-Inhalts in genau einen Solution Tank (1-4): vom Bediener gewählter Tank, abgelesener Füllstand und Kapazitätsbestätigung, geprüft gegen das 300-L-Nennvolumen und die beim Transferstart erfasste Mixer-Menge, gesonderte Bestätigung bei `NOT_EVALUATED`, Freigabe an den zuletzt abgeschlossenen Mischlauf gebunden und nur durch einen erfolgreich abgeschlossenen Transfer verbraucht (Wiederholung nach Stopp/Fehler mit neuer Freigabe, neu abgelesenem Füllstand und neuer Mixer-Messung), Ventil auf, Settle, Stop-Recheck, dann Pumpe; Pumpe aus vor Ventil zu (Pumpe-aus fehlgeschlagen: Ventil bleibt offen, kritischer Fehler, physischer Not-Aus nötig); erste plausible 0 L beendet den Transfer; Quellseiten-Überwachung wie beim Drain; Emergency Stop, Prozessverriegelung, Dashboard-Status und Prozesslog. Nur Software, integrierter Hardwarelauf steht aus | umgesetzt (hardwarefreie Tests; Komponenten einzeln funktionsgeprüft) | `process/target_transfer.py`, `process/drain_safety.py`, `nicegui_dashboard/process_controller.py` | Keine Zieltank-Sensoren: keine Überlaufsicherung, Sicherheitsreserve liegt in der Bedienerbestätigung; erster begleiteter Wassertest (Abschnitt 24) |
| AP-09 | Gesamtintegration, Hardwarevalidierung und Systemtests | AP-spezifische Umsetzung noch nicht begonnen | ausstehend | – | Alle Hardwarevalidierungspunkte (Abschnitt 24) |

---

## 1. Kurzüberblick

Dieses Repository enthält die Python-Implementierung für die Steuerungs- und Visualisierungsebene eines Central-Dosing-Systems (Raspberry Pi, GPIO-Relais, OPC-UA-Sensorik, MQTT/Node-RED). Der aktuelle Fokus liegt auf dem sicheren wasserbasierten Grundprozess:

```text
RO-Wasser → Mixing Tank → Sensorbox-Zirkulation → Drain
```

Chemikaliendosierung und Peristaltikpumpensteuerung sind noch nicht aktiv. Diese Funktionen werden erst nach weiterer Hardware-, Sensor- und Sicherheitsvalidierung ergänzt. Die Rezeptausführung ist bisher nur für den reinen Wasser-Zielwert (Füllvolumen, Sensorzirkulation) wirksam mit dem tatsächlichen Prozessstart verbunden (siehe Abschnitt 15) — die EC-/pH-/Dosierwerte im Rezept sind weiterhin rein deskriptiv, ohne Konsument im Prozesscode. Seit AP-05 Stufe 1 existieren ein reiner, hardwarefreier Dosierplan und eine Volumenbilanz als Fachlogik (Abschnitt 22); sie sind mit keinem Prozess und keiner Hardware verbunden.

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
- hardwarefreie Testisolierung (AP-00), strikte Sensor-/Settings-Auswertung und Rezeptstartvalidierung (AP-02A/B), Fill-Laufzeitsicherheit (AP-02C), gemeinsame Sensorauswertung und strikte Hardware-Start-Gates (AP-03), Drain-Sicherheit für alle drei Drain-Pfade (AP-04A) sowie Water-Cycle-Abschluss-/Cleanup-Behandlung mit finalem MQTT-Status (AP-04) — nur Software, siehe Abschnitt 21
- reine Fachlogik für einen unveränderlichen MCU_B-Nährstoffdosierplan und eine konservative Volumenbilanz (`domain/recipe/nutrient_plan.py`, `domain/recipe/volume_balance.py`, AP-05 Stufe 1, in `main` integriert und auf GitHub veröffentlicht) — keine Prozess-, Dashboard- oder Hardwareanbindung, siehe Abschnitt 22

Der modulare Water-Cycle startet softwareseitig korrekt. Ein vollständiger realer Hardwarelauf nach dem letzten Refactor und nach der Härtung AP-02 bis AP-04 steht noch aus. Bei Manual Drain Jog und Tank Cleaning sind die Komponenten einzeln funktionsgeprüft, die integrierten Läufe aber ebenfalls noch nicht validiert (siehe Abschnitte 17 und 24).

---

## 4. Hardware-Validierungsstatus (wichtig!)

**Funktionsprüfung der Einzelkomponenten:** Der Anlagenverantwortliche bestätigt, dass sämtliche CDS-Hardwarekomponenten (Transferpumpe, Zielventile 1–4, weitere Ventile und Pumpen, vorhandene Sensorik und Peristaltik-Komponenten) jeweils einzeln getestet und als funktionsfähig bestätigt wurden; die Funktionsprüfung wurde am 14.10.2026 erneut durchgeführt. Das ist eine Aussage über die einzelnen Komponenten, nicht über integrierte Prozessabläufe. Die Dokumentation hält das als Bestätigung des Anlagenverantwortlichen fest; ein Prüfprotokoll liegt nicht im Repository.

**Davon nicht abgedeckt:** Integrierte Prozessläufe (Water Cycle, Tank Cleaning, Manual Drain, AP-08-Zieltanktransfer, Dosierung) wurden nach den Änderungen AP-02 bis AP-08 noch nicht an der realen Anlage durchgeführt.

Folgende Ausgänge wurden zusätzlich bereits früher innerhalb realer Prozessläufe validiert:

| Funktion              | Config-Key             | GPIO (BCM) | Physischer Pin | Status              |
|------------------------|------------------------|:----------:|:---------------:|----------------------|
| Mixer Refill Pump       | `mixer_refill_pump`    | 20         | 38               | validiert            |
| Supply/Test Valve 6     | `test_supply_valve_6`  | 6          | 31               | validiert            |
| Drain Valve 0           | `valve_0_drain`        | 21         | 40               | validiert            |

Alle übrigen Einträge in `gpio_config.py` (`contactor_0/5`, `transfer_pump`, `mixing_circulation_pump`, `sensor_circulation_pump`, `valve_1`–`valve_9`) sind durch die obige Einzelfunktionsprüfung abgedeckt, aber **nicht** innerhalb eines hier dokumentierten integrierten Prozesslaufs validiert. Insbesondere:

- **Transfer Pump** (`transfer_pump`, GPIO26) wird in `calibration_mixing_tank.py`, `main_safe_drain.py`, `process/tank_cleaning/` und im AP-08-Transfer verwendet. Sie wird über einen GPIO-gesteuerten Contactor ohne Rückmeldung geschaltet; die Einzelfunktionsprüfung ist bestätigt, der integrierte AP-08-Ablauf steht noch aus.
- **Valve 5** ist laut Kommentar in `gpio_config.py` nicht zuverlässig dicht.
- **Tank Cleaning** steuert zusätzlich `mixing_circulation_pump` und `sensor_circulation_pump` an – einzeln funktionsgeprüft, integrierter Lauf steht aus. Die Settings-Datei (`config/tank_cleaning_settings.json`) ist deshalb bewusst auf ein reduziertes **40-Liter-Testvolumen** statt des späteren 200-Liter-Zielwerts gestellt, um den ersten Hardwaretest ohne größeren Wasserverbrauch/-risiko durchzuführen.

Bevor ein neuer Ausgang produktiv verwendet wird: real am System schalten, Wirkung visuell/physisch bestätigen, erst danach in eine automatisierte Sequenz aufnehmen.

Die Änderungen AP-00 bis AP-05 (Abschnitte 21 und 22) haben diese Tabelle nicht verändert: sie sind nur softwareseitig geprüft. Die MCU_B-Peristaltikausgänge (P1–P4) sind nicht Teil der Tabelle und nicht für Dosierung freigegeben (Abschnitt 24).

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
├── gpio_config.py
├── config/
│   ├── system_config.json
│   ├── water_cycle_settings.json
│   ├── calibration_settings.json
│   ├── process_settings.json
│   └── tank_cleaning_settings.json
├── domain/
│   ├── recipe/                       # limits.py, calculations.py, field_validators.py, validation.py, models.py,
│   │                                 # nutrient_plan.py, volume_balance.py (AP-05 Stufe 1, reine Logik, nicht eingebunden)
│   ├── recipe_limits.py              # Compat-Wrapper -> domain/recipe/
│   └── recipe_model.py               # Compat-Wrapper -> domain/recipe/models.py
├── statemachine/
│   ├── fill_and_measure_state_machine.py
│   ├── sensor_reading.py             # strikte Mixer-/RO-Liter-Auswertung (AP-02A)
│   └── start_validation.py           # reine Rezept-Startfreigabe / Laufzeitprüfung (AP-02B/C)
├── process/
│   ├── common.py
│   ├── refill.py
│   ├── sensor_circulation.py
│   ├── drain.py
│   ├── drain_safety.py               # Drain-Sensor-Watchdog, sichere Stopp-Sequenz (AP-04A)
│   ├── water_cycle.py
│   ├── auto_circulation.py
│   ├── manual_drain_jog.py
│   ├── fill_and_measure.py           # FillAndMeasureController
│   ├── pump_prime.py
│   ├── tank_cleaning/                # controller.py, phases.py, fill/hold/drain_phase.py, status_publisher.py
│   ├── watchdog.py
│   └── background_process.py         # Basisklasse BackgroundHardwareProcess
├── services/
│   ├── system_config.py
│   ├── mqtt_publisher.py
│   ├── mqtt_topic_reader.py          # vereinheitlichter MQTT-Einzeltopic-Reader
│   ├── process_run_logger.py
│   ├── sensor_stamp.py               # Messungsidentität für Aktualitätsprüfung (AP-02C)
│   ├── settings_validation.py
│   └── peristaltic/                  # protocol/safety/models/firmware_profiles/prime/session_log/
│                                      # serial_client/dose_limits/parallel_pump_safety/calibration_*.py
├── hardware/
│   ├── digital_output.py
│   └── actuator_manager.py
├── calibration/                      # config/models/cli_input/sensor_reading/measurement_csv/
│                                      # pump_segments/analysis/session/cli.py
├── preflight/                        # report/files_and_syntax/disk_space/gpio/services/mqtt/opcua/run.py
├── nicegui_dashboard/
│   ├── app.py
│   ├── cds_controller.py
│   ├── process_controller.py         # Koordinator: Exklusivität, Status-Aggregation, Rezept-Merge
│   ├── components/                   # formatting.py, status_widgets.py
│   ├── recipe_store/                 # book_io.py, migrations.py, queries.py, run_config.py
│   ├── pages/
│   │   ├── dashboard_page.py         # dünnes Gerüst, ~190 Zeilen
│   │   └── dashboard/                # je eine Datei pro Dashboard-Bereich (recipe_text, status_actuators,
│   │                                  # maintenance, recipe, process_control, prime, sensor_tanks)
│   └── static/
├── scripts/
│   ├── check_gpio_conflicts.py
│   ├── peristaltic_calibration_cli.py
│   ├── peristaltic/                  # discover.py
│   └── manual_tests/
├── recipes/
│   └── dashboard_recipes.json
├── tests/                            # inkl. _hardware_guard.py, _guard_site/ (AP-00 Hardware-Isolation)
├── logs/
├── calibration_data/
├── mqtt_sensor_bridge.py
├── calibration_mixing_tank.py        # Compat-Wrapper -> calibration/cli.py
├── main_safe_drain.py
├── main_refill_and_drain_test.py
├── preflight_check.py                # Compat-Wrapper -> preflight/run.py
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

> Hinweis: Diese Datei ist inzwischen die alleinige Quelle für OPC-UA-Endpoint, MQTT-Verbindung und Mixer-Kalibrierung — sowohl `mqtt_sensor_bridge.py`, `calibration_mixing_tank.py`, `preflight_check.py` als auch der Dashboard-MQTT-Layer (`services/mqtt_publisher.py`, `services/mqtt_topic_reader.py`, `nicegui_dashboard/cds_controller.py`) und der Kalibrierfaktor-Fallback in `statemachine/fill_and_measure_state_machine.py` lesen jetzt aus `get_mqtt_config()`/`get_mixer_level_calibration()` statt aus eigenen hartcodierten Werten.

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
- Chemikaliendosierung und Peristaltikpumpensteuerung sind aktuell nicht aktiv. Der AP-05-Stufe-1-Dosierplan ist reine Logik und gibt nichts frei.
- Sensorwerte werden nicht blind als zuverlässige Wahrheit angenommen (Filterung, Plausibilitätsprüfung, Confirm-Samples).
- Aktoren werden über zentrale Safe-Shutdown-Pfade ausgeschaltet (`ActuatorManager.safe_shutdown_all()`), Fehler dabei werden geloggt statt verschluckt.
- Manuelle Kalibrier-Drain-Funktionen besitzen ein Safety-Timeout (`calibration_drain_max_seconds`).
- `KeyboardInterrupt` wird in allen Water-Cycle-Phasen einheitlich behandelt und bricht den Rest des Ablaufs kontrolliert ab.
- Manual Drain Jog und Tank Cleaning laufen als Hintergrund-Threads, deren Schleifen alle ~0,5s auf ein `threading.Event` prüfen — ein Emergency Stop aus dem Dashboard unterbricht sie dadurch binnen einer knappen Sekunde, statt bis zum Ende der laufenden Phase zu warten.
- Start/Stop von Fill-and-Measure, Manual Drain Jog und Tank Cleaning sind atomar gegen den internen `ProcessController`-Lock abgesichert: zwei gleichzeitige Start-Aufrufe (Dashboard-Klick + parallel gestartetes Skript) können nicht mehr beide durchkommen — vormals ein TOCTOU-Race zwischen Zustandsprüfung und tatsächlichem Start.
- Zusätzlich verhindert eine prozessübergreifende Datei-Sperre (`fcntl.flock`, `hardware/actuator_manager.py`, `logs/.hardware.lock`) parallele Hardware-Ansteuerung durch zwei unabhängige Python-*Prozesse* (nicht nur Threads im selben Prozess) — z. B. Dashboard plus ein direkt gestartetes Kalibrier- oder Safe-Drain-Skript.
- Emergency Stop ist asynchron implementiert (`ProcessController.emergency_stop()`): das Signalisieren (State-Machine-Abbruch, `request_stop()`, Aktor-Shutdown) passiert sofort unter Lock, das Warten auf den Thread-Join läuft danach außerhalb des Locks in einem Worker-Thread — das Dashboard bleibt für andere verbundene Clients währenddessen reaktionsfähig, statt bis zu ~7s einzufrieren.
- Seit AP-03B wird der Hardwarestart nur für `hardware_execution_enabled is True` freigegeben (strikter Typ, keine Umwandlung). Seit AP-02B/C benötigen Rezeptläufe eine fail-closed Startfreigabe und aktuelle Sensormessungen; seit AP-04A sind alle Drain-Pfade durch einen Sensor-Watchdog begrenzt; seit AP-04 meldet der Water-Cycle Cleanup-Fehler statt Erfolg (Abschnitt 21).
- Abschalten ist ein Softwareaufruf ohne physische Rückmeldung; diese Grenze ist dokumentiert, nicht gelöst (Abschnitt 24).

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
(`process/watchdog.py`), Settings-Validierung (`services/settings_validation.py`,
inklusive aller vier echten `config/*.json`-Dateien) sowie seit AP-00 bis AP-05
Startvalidierung, Fill-/Drain-Sicherheit, Water-Cycle-Abschluss und Dosierplan-/Volumenlogik ab.
Ein zentraler Guard (`tests/_hardware_guard.py`) blockiert GPIO, Seriell, Netzwerk und
Schreibzugriffe auf Produktivdatenverzeichnisse — es wird also kein GPIO/OPC-UA/MQTT
angefasst. Aktuelles Ergebnis und bekannte Fehlschläge: Abschnitt 23. `requirements-dev.txt` (nur `pytest` zusätzlich zu
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

> Der modulare Water-Cycle muss nach dem letzten Refactor noch einmal vollständig real mit Hardware getestet werden. Seit AP-04 sammelt er Endergebnis und Cleanup-Fehler und sendet `WATER_CYCLE_FINISHED` nur nach erfolgreichem Cleanup (Abschnitt 21); die MQTT-Zustellung von Statusmeldungen ist nicht garantiert.

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
process/tank_cleaning/         → TankCleaningController
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

Die EC-Nährstoffdosierung nutzt eine Gesamtdosis (`nutrients_ml_per_100_l`), aufgeteilt zwischen Nährstofflösung A und B über einen Prozentanteil (`nutrient_a_percent`, Standard 50 %); B wird stets als `100 - A` berechnet, nie unabhängig editiert. `nutrient_dosing_enabled` (Standard aus) steuert, ob überhaupt irgendwo eine Dosis berechnet wird — solange aus, werden alle berechneten Mengen auf 0 ml erzwungen, auch im geschätzten Volumen. Ein noch als Legacy-prüfungsbedürftig markiertes Rezept (`legacy_dosing_needs_review`) kann die Dosierung erst aktivieren, nachdem der "Legacy review completed"-Schalter im Editor dieses Flag ausdrücklich aufgehoben hat. Das EC-Sollwert-Feld nutzt eine Schrittweite (`0.05` in `nicegui_dashboard/pages/dashboard/recipe_section.py`; ein Test erwartet noch `0.1`, siehe Abschnitt 23) nur als Bedienhilfe — beliebige Zwischenwerte im gültigen Bereich (z. B. `2,35`) werden unverändert akzeptiert und gespeichert. Es wird nicht behauptet, dass eine echte Chemikaliendosierung bereits getestet wurde — diese Logik ist als reine Domänenlogik vollständig implementiert und getestet, aber es ist noch keine echte Peristaltikpumpe angeschlossen (siehe unten).

**Tatsächlich wirksam** (`nicegui_dashboard/recipe_store/run_config.py::build_run_config()`, aufgerufen von `ProcessController.start_fill_and_measure()`): `target_ro_water_l` bestimmt das reale Füll-Zielvolumen (erzwingt `fill_mode="absolute"`); `sensor_circulation_enabled`, aus einem frisch im Bestätigungsbereich von Process Control gewählten `RunOptions` (nicht aus dem Rezept, nicht gespeichert, startet immer auf „aus" und wird erst nach einem tatsächlich akzeptierten Start wieder auf „aus" zurückgesetzt — ein blockierter Start lässt die Wahl unverändert, damit ein erneuter Versuch möglich ist), bestimmt, ob die Sensorbox-Zirkulationspumpe beim Lauf mit angesteuert wird. Beide werden gegen `max_mixer_liters` und das `process_settings.json`-Schema geprüft — ein Rezept-Zielvolumen über der Mixer-Kapazität blockiert den Start mit klarer Meldung, statt den Prozess zu starten und erst per Watchdog mittendrin abzubrechen. `RunConfigSnapshot.build(recipe, process_settings, run_options)` ist die einzige Stelle, an der diese drei Quellen zusammengeführt werden; alles Nachgelagerte liest ausschließlich aus diesem unveränderlichen Snapshot. `ProcessController` speichert den genauen Snapshot, mit dem ein Lauf tatsächlich gestartet wurde, und der Status im Dashboard zeigt weiterhin genau diesen Snapshot — nicht neu berechnet — solange der Prozess läuft, selbst wenn währenddessen ein anderer Favorit aktiviert wird; erst der nächste Lauf verwendet das neu aktive Rezept.

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
6. ~~MQTT-Staleness-/Payload-Reader vereinheitlichen.~~ Erledigt: `services/sensor_snapshot.py` (`SensorSnapshotReader`) und das alte `nicegui_dashboard/mqtt_topic_reader.py` (`MqttTopicReader`) wurden zu einer Klasse in `services/mqtt_topic_reader.py` zusammengeführt, mit Locking, Fehler-Tracking und einem Sentinel-basierten `get_latest(max_age_seconds=...)` — eine bewusste, kleine, dokumentierte Verhaltensänderung (siehe die Commit-Nachricht dieser Änderung für die genauen Deltas), kein reines Verschieben.
7. Kein CI, kein Linting/mypy trotz durchgängiger Typannotationen (eine hardwarefreie `pytest`-Suite existiert, siehe Abschnitte 9 und 23).
8. ~~Rezeptwerte kontrolliert mit Water-Cycle-Settings verbinden.~~ Erledigt: `nicegui_dashboard/recipe_store/run_config.py::build_run_config()` verbindet die zwei Rezeptfelder mit einem echten Konsumenten (`target_ro_water_l` → `fill_mode="absolute"`/`target_total_liters`, `sensor_circulation_enabled` → `enable_sensor_circulation`) beim Start von Fill-and-Measure, inklusive `max_mixer_liters`-Grenzprüfung. Die EC-Nährstoffdosierfelder des Rezepts sind jetzt vollständig typisiert, berechnet und validiert (siehe Abschnitt 15, `docs/RECIPE_AND_DOSING_RULES.md`) und als `recipe_run_config_snapshot` angehängt, bleiben aber ohne Hardware-Konsument, solange Chemikaliendosierung nicht implementiert ist (Punkt 9 unten). Die pH-Korrekturfelder sind unverändert/außerhalb des Umfangs dieser Arbeit.
9. **Peristaltikpumpensteuerung** — Firmware-seitige Härtung ist im separaten Repository `central_dosing_sys_peristaltic` (Branch `harden-serial-protocol`) abgeschlossen und auf beiden Arduino-MCUs real verifiziert: Treiber standardmäßig deaktiviert, Dosis-/Laufzeitlimits (`MAX_SINGLE_DOSE_ML`, `MAX_RUNTIME_MS`), maschinenlesbares `PING`/`STATUS`/`DOSE`/`STOP`/`STOPALL`-Protokoll (siehe dortige `docs/SERIAL_PROTOCOL.md`). `cds_control` hat jetzt einen eigenständigen, hardwarefrei getesteten Python-Serial-Client und ein Terminal-CLI dafür (`services/peristaltic/`, `scripts/peristaltic_calibration_cli.py` — discover/map/check/stop-all/test/calibrate/pair-test/all-four-test, ausschließlich Wasser, max. 10 ml pro Testdosis, siehe `docs/PERISTALTIC_MAPPING_AND_CALIBRATION.md`), das aber bewusst **nicht** an Rezeptlogik, NiceGUI-Dashboard oder State Machine angebunden ist, und es sind **noch keine echten Peristaltikpumpen angeschlossen** — diese Anbindung sowie eine reale Pumpen-Kalibrierung/-Verifikation bleiben separate, noch nicht begonnene Schritte. Ein reiner Entwurf für die Aufteilung der gemeinsamen Firmware in ein `MCU_A_PH`/`MCU_B_EC`-Profilpaar liegt außerdem unter `docs/PERISTALTIC_PROFILE_PLAN.md` vor (keine Firmware-/Verhaltensänderung dadurch).
10. **pH-Dosierungsgrenzen noch nicht durchgesetzt** — pH-Sollwert, pH-Mischzeit und pH-Stockvolumengrenzen wurden gegen die alte Node-RED-Konfiguration bestätigt (siehe `docs/OPEN_RECIPE_DECISIONS.md`), aber bewusst nicht als harte Validierung ergänzt, da die pH-Dosierung noch keinen Konsumenten hat und sie außerhalb des explizit angeforderten Regelsatzes lagen. (EC-Sollwert-/Mischzeitgrenzen und die Grenze des EC-Anpassungsfaktors *sind* durchgesetzt — siehe Abschnitt 15. Addon-1/Addon-2-Grenzen entfallen vollständig, da Addon 1/Addon 2 vollständig aus dem aktiven Rezeptmodell entfernt wurden.) Ob/wie die verbleibenden pH-Grenzen durchgesetzt werden sollen, ist eine noch offene fachliche Entscheidung.
11. **AP-05 Stufe 1 integriert, aber nicht eingebunden** — Commit `f463a0f` ist in `main` und auf GitHub; kein Prozess-, Dashboard- oder Hardwarecode nutzt `domain/recipe/nutrient_plan.py` / `volume_balance.py` (Abschnitt 22). Stufen 2–6 und AP-06 bis AP-09 sind ausstehend.
12. **Bekannter Testfehler** — Recipe-Editor-Schrittweitentest (Abschnitt 23).
13. **Offene Sicherheits- und Hardwarefragen** (O1, Abschaltrückmeldung, Füllstand < ca. 10 L, Trockenlauf, vorläufige Drain-Parameter, Kill-/SIGTERM-Grenzen, `DigitalOutput`-Cleanup, MQTT-Zustellung, MCU_B-Identitäts-/Kalibrierungs-Gates) — Abschnitt 24.

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

---

## 21. Arbeitspaket-Ergebnisse AP-00 bis AP-04

Alle folgenden Punkte sind Softwareergebnisse, abgesichert durch hardwarefreie Tests (Abschnitt 23). Keiner davon wurde am realen System ausgeführt.

**AP-00 – Isolierte automatisierte Tests.** `tests/_hardware_guard.py` wird von `tests/conftest.py` vor der Collection und für Kindprozesse über `tests/_guard_site/sitecustomize.py` installiert. Er blockiert GPIO (`gpiozero`-Pin-Factory, `lgpio`), serielle Geräte, Netzwerkverbindungen (MQTT/OPC-UA) und Schreibzugriffe auf `logs/`, `config/`, `recipes/` und `calibration_data/`; die Hardware-Lock-Datei wird auf einen temporären Pfad umgeleitet. Jeder blockierte Versuch wird protokolliert und lässt den Test scheitern, auch wenn der Aufrufer die Exception verschluckt. Dafür wurde kein Produktivcode geändert. Bekannte Lücke: Der Exit-Code eines nie abgewarteten Kindprozesses wird bei fehlgeschlagener Meldung nicht gesehen (im Modul dokumentiert).

**AP-01 – Laufoptionen.** Im Dashboard gewählte `RunOptions` werden an den Prozess-Controller weitergegeben (`nicegui_dashboard/cds_controller.py`).

**AP-02 – Rezept-/Laufzeitvalidierung, Sensorprüfung, Fill-Sicherheit.**

- AP-02A: `statemachine/sensor_reading.py` akzeptiert nur echte, endliche Zahlen (kein `bool`, NaN, inf, keine für `float` zu großen Ganzzahlen); ein ungültiger bevorzugter Wert wird nie durch einen weniger verlässlichen Fallback ersetzt. `services/settings_validation.py` validiert Settings strenger.
- AP-02B: `statemachine/start_validation.py` ist eine reine, fail-closed Startfreigabe für Rezeptläufe. Blockgründe: `run_config_invalid`, `sensor_invalid`, `mixer_invalid`, `mixer_implausible`, `mixer_at_target`, `ro_invalid`, `ro_capacity_invalid`, `ro_insufficient`, `tank_not_confirmed`, `empty_veto`. Die Operator-Bestätigung „Mischtank entleert“ ist keine Messung; der Mixerwert dient nur als Veto gegen einen Widerspruch, da die Kalibrierung unterhalb von etwa 10 L nicht belastbar ist.
- AP-02C: Im Rezeptlauf-Modus wiederholt die Fill-State-Machine die Startfreigabe unmittelbar vor Pumpe-an auf einer neuen Messung, verlangt innerhalb von 5 s eine neue MQTT-Nachricht mit späterem Bridge-Zeitstempel (`services/sensor_stamp.py`), prüft RO- und Mixerwerte in jedem Takt ungefiltert, schaltet beim Erreichen des Ziels die Pumpe zuerst aus und bestätigt das Ziel nur bei ausgeschalteter Pumpe und hält den ersten Fehler endgültig fest (ein paralleler Emergency Stop kann weder überschrieben noch von einem erneuten Einschalten gefolgt werden). Das 5-s-Fenster ist nicht hardwarevalidiert.

**AP-03 – Hardware-Start-Gates und Ausschluss.**

- AP-03A: `process/common.py` liest den Mixerstand strikt; die Endlichkeitsprüfung erfolgt vor dem Begrenzen auf null, ungültige Werte gelangen nie in die Historie.
- AP-03B: `BackgroundHardwareProcess` gibt Hardware nur frei, wenn `hardware_execution_enabled is True` (keine `bool()`-Umwandlung: Strings, `1`, `None`, fehlender Schlüssel oder Nicht-dict-Settings blockieren); eine Exception beim Lesen des Sensor-Snapshots blockiert den Start kontrolliert.
- Der gegenseitige Ausschluss von Fill-and-Measure, Manual Drain Jog und Tank Cleaning (`ProcessController`-Lock) und die prozessübergreifende `fcntl.flock`-Sperre sind ältere Mechanismen (Abschnitt 8), nicht neu in AP-03.

**AP-04 – Drain-Sicherheit, Prozessfehler, Water-Cycle-Abschluss.**

- AP-04A: `process/drain_safety.py` liefert für `process/drain.py`, `process/tank_cleaning/drain_phase.py` und `process/manual_drain_jog.py` einen `DrainSensorWatchdog` (Plausibilität, Sensorverlust, kein Fortschritt, unerwarteter Anstieg auf dem ungefilterten Mixerstand) und eine gemeinsame Stopp-Sequenz `safe_stop_drain` (Transferpumpe aus, danach Drain-Ventil aus; beides wird immer versucht, Fehler werden gesammelt). Drain-Settings werden validiert, bevor ein Aktor berührt wird; der Manual Drain Jog hat ein verbindliches Maximum von 30 s. Ein fehlgeschlagenes Abschalten wird nie als Erfolg gemeldet.
- AP-04: `process/water_cycle.py` sammelt das Ergebnis (`finished`, `aborted`, `interrupted`, `startup_failed`, `error`; Standard fail-closed `error`) und leitet Exit-Code und finalen Status erst nach vollständigem Cleanup ab. Jeder Cleanup-Schritt läuft isoliert; seine Exception wird protokolliert, nicht weitergereicht.
- Finale MQTT-Status: `WATER_CYCLE_FINISHED` wird nur gesendet, wenn das Cleanup fehlerfrei war; der Endstatus ist `SAFE_SHUTDOWN` oder `SAFE_SHUTDOWN_FAILED` und enthält `cleanup_errors`, `physical_switch_off_confirmed: false` und `status_publish_failures`.
- **MQTT-Zustellung ist nicht garantiert.** QoS 1 mit kurzem `wait_for_publish`-Timeout (`services/mqtt_publisher.py`). Ein Status-Publish wirft nie in das Cleanup hinein. Fehlerbehandlung in `process/water_cycle.py`:
  - `publish_json() == False` oder eine Exception wird in `status_publish_failures` gezählt und protokolliert, ändert aber für sich genommen nicht den Exit-Code.
  - Ein `KeyboardInterrupt` während `_publish()` wird als Cleanup-Fehler registriert; der finale Exit-Code kann dadurch 1 werden.
  - Ein korrigierender finaler MQTT-Status wird genau dann einmalig versucht, wenn der vorherige finale Publish einen neuen Cleanup-Fehler registriert hat, beispielsweise durch `KeyboardInterrupt`. Bei einem gewöhnlichen `publish_json() == False` oder einer abgefangenen regulären Exception ohne neuen Cleanup-Fehler erfolgt kein Retry.
  - `mqtt_publisher.close()` erfolgt nach der finalen Statusmeldung. Ein Fehler dabei kann lokal zu Exit-Code 1 führen, ohne im zuletzt veröffentlichten MQTT-Status enthalten zu sein.

## 22. AP-05 Stufe 1 – Dosierplan und Volumenbilanz (reine Fachlogik)

`domain/recipe/nutrient_plan.py` und `domain/recipe/volume_balance.py` führen kein I/O aus: keine Hardware, kein MQTT, kein Sensor, keine Datei, keine Uhr. Nichts in `process/`, `statemachine/` oder `nicegui_dashboard/` importiert sie bisher. **Die Existenz eines Dosierplans ist keine Freigabe realer MCU_B-Dosierung und nicht Teil eines Prozesses.**

- Eingabe ist ausschließlich der validierte `RunConfigSnapshot`; Summen, A/B-Aufteilung und Volumen werden mit den vorhandenen Funktionen nachgerechnet und gegen den Snapshot abgeglichen; jeder Widerspruch ist ein `NutrientPlanError` mit maschinenlesbarem Code.
- `NutrientDosingPlan` ist unveränderlich (frozen Dataclasses, schreibgeschützte Mappings, Plan-Hash).
- Pumpenrollen MCU_B: P1 und P3 für Lösung A, P2 und P4 für Lösung B (kanonische private Tupel, öffentliche Sichten schreibgeschützt).
- Exakte Arithmetik in ganzzahligen Mikrolitern. Je Lösung erhält die erste Pumpe (P1/P2) `floor(Menge * w1/(w1+w2) + 1/2)`, die zweite (P3/P4) den Rest; die Summe ist damit exakt. Gewichte müssen ausdrücklich übergeben werden (Profil `firmware_ml_per_step`); es gibt keine Standardgewichtung und keinen 50/50-Fallback.
- Begrenztes Chunking: `n = ceil(Ziel / max_chunk)`, gleich große Chunks, keine Null-Chunks, kein winziger Restchunk; Referenzlimit 50 ml (Firmware-Limit, per Test geprüft); höchstens 20 000 Chunks je Pumpe, sonst wird der Plan abgelehnt (Speicherschutz; Mengen werden nie verändert).
- `volume_balance.py` trennt vier Evidenzklassen, die nie vermischt werden: geplant, protokollbestätigt (ein `DONE` ist ein Protokollereignis, kein Nachweis der geförderten Menge), sensorgemessen (nur Gesamtvolumen) und unbestätigt (in flight / unsicher). Es gibt keine unabhängige RO-Durchflussmessung; der RO-Anteil ist nur indirekt schätzbar und wird nie als Grenzwert verwendet.
- Grenzen: RO-Wasser 180 L (`MAX_RECIPE_RO_WATER_L`), Prozessvolumen 185 L (`MAX_PROCESS_VOLUME_L`); Vergleiche sind exakt (`fractions.Fraction`), die Kapazität wird auf ganze µL abgerundet.
- In-flight- und unsichere Nährstoffmengen sperren Zielbestätigung, Endvolumenbewertung und RO-Wiederaufnahme; Bewertungsfunktionen liefern einen Fehlergrund statt zu werfen.
- Ungültige Zahlen (bool, NaN, inf, riesige Ganzzahlen, falsche Typen) enden in kontrollierten Fehlern; das Chunk-Budget verhindert übermäßige Speicherallokation.
- Referenz-Teststand: 565 Tests bestanden (`tests/test_nutrient_plan.py` 373, `tests/test_volume_balance.py` 192), unabhängig geprüft.
- Offen für Stufe 2: MCU-Identitäts-, Firmware-/Profil- und Kalibrierungs-Gates; Übernahme in einen Prozess.

## 23. Teststatus und Grenzen

Strategie: `pytest` mit dem AP-00-Guard; alle Tests sind hardwarefrei. Prozess-, State-Machine- und MQTT-Tests verwenden Fakes; GPIO, Seriell und Netzwerk sind strukturell blockiert, nicht nur per Konvention.

| Lauf | Ergebnis |
|---|---|
| Vollständige Suite, Haupt-Worktree (`f463a0f`, 2026-10-14) | 1874 bestanden, **1 fehlgeschlagen** |
| Nur AP-05-Stufe-1-Tests | 565 bestanden |
| Vollständige Suite, separater Worktree `../cds_control_ap05_s1` | 1873 bestanden, 2 fehlgeschlagen, 3 Fehler (der bekannte Fehlschlag plus die Folgen der fehlenden Datei, siehe unten) |

- **Bekannter Fehlschlag:** `tests/test_dashboard_page_recipe_editor.py::test_recipe_editor_number_fields_have_field_appropriate_step` erwartet für den EC-Sollwert die Schrittweite `0.1`; `nicegui_dashboard/pages/dashboard/recipe_section.py` setzt `0.05`. Welcher Wert gewollt ist, ist nicht entschieden; diese README behauptet keinen von beiden. Kein Bezug zu AP-00 bis AP-05.
- **Fehler im separaten Worktree (umgebungsbedingt):** Die Datei `recipes/dashboard_recipes.json` ist gitignorierte Laufzeitdatei und fehlt in diesem Worktree. Tests, die das echte Rezeptbuch lesen (`tests/test_recipe_run_config.py::test_real_active_recipe_merged_onto_real_settings_passes_schema` und zwei Tests in `tests/test_process_locking.py`), scheitern dort, weil der Code `recipes/` anlegen will und der AP-00-Guard dies korrekt blockiert (`os.mkdir('recipes')`). Das ist eine fehlende lokale Datei, kein Produktfehler; im Haupt-Worktree ist die Datei vorhanden.
- Die Suite ist daher **nicht** vollständig grün; die Aussage „alle Tests erfolgreich“ wird nicht getroffen. Bestandene Tests belegen kein Hardwareverhalten.

## 24. Offene Sicherheits- und Hardwarefragen

| Thema | Beschreibung | Zugeordnet zu |
|---|---|---|
| Keine physische Abschaltrückmeldung | Abschalten ist ein Softwareaufruf; `physical_switch_off_confirmed` ist immer `false` | AP-09 |
| O1 | Drain-Pfade: `safe_stop_drain()` (`process/drain_safety.py`) versucht nach einem fehlgeschlagenen `transfer_pump.off()` weiterhin, das Drain-Ventil zu schließen. **Der AP-08-Transfer entscheidet anders:** Schlägt `pump.off()` fehl, wird das Zielventil bewusst **nicht** geschlossen (nicht gegen eine möglicherweise laufende Pumpe), die Aktoren bleiben gehalten, der Fehler ist kritisch, der Bediener muss den physischen Not-Aus betätigen (unabhängig vom Raspberry Pi, softwareseitig nicht ansteuerbar), und bis zur Behebung und Quittierung (Reset / Ack) ist kein weiterer Transfer möglich. Ohne Contactor-Rückmeldung kann die Software keinen Pumpenstillstand beweisen. O1 für die Drain-Pfade bleibt eine offene Entscheidung | AP-09 |
| Füllstand unter etwa 10 L | Die Mixer-Kalibrierung ist nur ab etwa 10 L gefittet. Bestätigung des Anlagenverantwortlichen (AP-08): Beim Entleeren bedeutet der erste angezeigte Wert von 0 L, dass der Mixing Tank tatsächlich leer ist; danach können die Werte wieder schwanken. Der Transfer endet deshalb mit dem ersten frischen, gültigen 0-L-Wert, der zum Entleerverlauf passt (vorheriger plausibler Wert bereits bei höchstens 10 L); ein Transferstart unter 10 L wird blockiert. Die allgemeinen Drain-Pfade sind unverändert und verlangen weiterhin mehrere Leer-Samples | AP-09 |
| Trockenlauf | Software kann Trockenlauf der Transferpumpe nicht verhindern; sie begrenzt nur die Laufzeit bei Sensorverlust/fehlendem Fortschritt | AP-09 |
| Vorläufige Drain-Parameter | Sensorverlust-Grenze 5 s sowie die Standardwerte für Pumpenförderrate (16 L/min), Timeout-Puffer (180 s), Ventil-Settle-Zeit (1 s) und Fortschritts-/Anstiegsgrenzen in `DrainSafetyLimits`: nicht hardwarevalidiert | AP-09 |
| SIGTERM / Kill | Cleanup läuft nur, wenn der Prozess es ausführen kann; nach `SIGKILL` oder hartem Absturz ist keine Software-Bereinigung möglich, Ausgänge können im letzten Zustand bleiben | AP-09 |
| `DigitalOutput`-Cleanup | `hardware/digital_output.py`: `off()` tut nichts, wenn der Softwarezustand „inaktiv“ ist; `close()` erreicht `device.close()` nicht, wenn `off()` eine Exception wirft. Ein nachgewiesener Softwarefehler muss vor einer betroffenen Hardwarefreigabe korrigiert werden; AP-09 bleibt für Hardware- und Systemvalidierung zuständig | zuerst Softwarekorrektur, dann AP-09 |
| MQTT-Zustellung | Statusmeldungen sind nicht garantiert zugestellt (Abschnitt 21) | akzeptierte Einschränkung |
| MCU_B-Identität und Kalibrierung | Identitätsprüfung, Firmware-/Profil-Abgleich und Freigaben der Einzelpumpenkalibrierung sind nicht abgeschlossen | AP-05 Stufe 2 |
| Chemische Dosierung | Nicht freigegeben. Keine chemische Dosierung, bevor alle Hardware-Gates bestanden sind | AP-05 Stufe 2 / AP-09 |
| Zieltank-Überlauf (AP-08) | Die vier Solution Tanks (300 L Nennvolumen, manuell ablesbare Füllstandsskala) haben keine Sensoren. Die Software prüft nur den vom Bediener abgelesenen Füllstand gegen das 300-L-Nennvolumen und die Mixer-Menge beim Transferstart; sie legt keine Sicherheitsreserve fest und berücksichtigt die Unsicherheit des Mixer-Sensors nicht. Beides liegt in der ausdrücklichen Bedienerbestätigung und Beaufsichtigung. Ein automatischer Überlaufschutz ist derzeit nicht möglich und bleibt eine dokumentierte spätere Erweiterung | spätere Erweiterung |
| Transferablauf (AP-08) | `valve_1` bis `valve_4` und `transfer_pump` sind einzeln funktionsgeprüft (Anlagenverantwortlicher). Der integrierte AP-08-Ablauf (Ventil, Settle, Pumpe, Überwachung, Stopp) wurde noch nicht an der realen Anlage durchgeführt; Pumpenförderrate (16 L/min) und Transfer-Timeout folgen den vorläufigen Drain-Parametern. Ein gestoppter oder fehlgeschlagener Transfer verbraucht die Freigabe des Mischlaufs nicht (neue Freigabe, neu abgelesener Füllstand und neue Mixer-Messung nötig; nach Störung oder Emergency Stop erst nach Quittierung); nur ein erfolgreich abgeschlossener Transfer verbraucht sie | AP-09 |

Die Zuordnung offener Punkte zu AP-05 Stufe 2 und AP-09 folgt der Roadmap (AP-09: Gesamtintegration, Hardwarevalidierung, Systemtests); die einzelne Zuordnung ist die Lesart des Autors.
