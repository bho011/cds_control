#!/usr/bin/env python3
"""AP-05 erster Hardware-Funktionstest mit Wasser - gefuehrt, einzeln bestaetigt, protokolliert.

Interaktives Operator-Skript, KEIN Prozess und KEINE Aenderung der
Prozesssteuerung. Ohne ``--execute`` wird nur der Testplan ausgegeben (keine
Hardware-Imports, keine GPIO, kein Seriellzugriff).

Mit ``--execute`` (nur wenn ``hardware_execution_enabled`` in
config/process_settings.json vom Operator auf true gesetzt wurde):

* jeder Schritt wird zuerst beschrieben und laeuft erst nach ``go <schritt>``;
* Pumpen/Ausgaenge laufen hoechstens MAX_ON_SECONDS (3 s), Abschaltung immer in
  ``finally``; MCU_B-Pumpen nur als kleine DOSE (Schrittzahl bestimmt die Laufzeit)
  plus Software-Watchdog bei MAX_ON_SECONDS (STOP/STOPALL). Nicht verlaesslich
  begrenzbare MCU_B-Schritte werden gesperrt (SKIPPED);
* der erste Fehler beendet den Lauf (kein automatisches Weitermachen), danach
  immer: alle GPIO aus, STOPALL an MCU_B, Statuspruefung;
* jedes Ereignis wird sofort in die CSV geschrieben (flush + fsync), die
  Summary-JSON nach jedem Schritt atomar neu geschrieben.

Beweisniveaus (Spalte ``evidence_level``), bewusst getrennt:
  COMMAND_SENT        - Software hat den Befehl abgesetzt (GPIO gesetzt / Zeile gesendet)
  SOFTWARE_FEEDBACK   - Software hat eine Rueckmeldung erhalten (MCU_B OK START/DONE/STATUS;
                        bei GPIO nur der interne Softwarezustand, KEINE elektrische Rueckmeldung)
  OPERATOR_CONFIRMED  - der Bediener hat die physische Funktion bestaetigt
Nur OPERATOR_CONFIRMED ohne Fehler ergibt Ergebnis PASSED.

Der Dashboard-Emergency-Stop erreicht dieses separate Skript NICHT; Schutz sind
der physische Not-Aus, Strg+C (schaltet ab) und die 3-s-Begrenzung.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MAX_ON_SECONDS = 10.0
RUNTIME_TOLERANCE_S = 0.5          # Messtoleranz ueber 3 s, danach FAILED
PUMP_DOSE_ML = 1.0                 # ca. 1,6 s bei 120 rpm / 8 Mikroschritten
MCU_MAX_ESTIMATED_S = 2.0          # Schaetzung darueber -> Schritt gesperrt
MIN_CIRCULATION_LITERS = 15.0      # Trockenlaufschutz; unter ca. 10 L ist der Fuellstand nicht belastbar
MIN_RO_LITERS = 20.0
EXPECTED_MCU_B_ROLES = {"P1": "nutrient_a_1", "P2": "nutrient_b_1", "P3": "nutrient_a_2", "P4": "nutrient_b_2"}
SETTINGS_PATH = REPO_ROOT / "config" / "process_settings.json"
SYSTEM_CONFIG_PATH = REPO_ROOT / "config" / "system_config.json"

CSV_FIELDS = (
    "run_id", "timestamp", "git_commit", "step_id", "phase", "event", "device", "gpio_or_port",
    "requested_state", "command_sent", "t_on", "t_off", "runtime_s", "evidence_level",
    "software_feedback", "sensors", "result", "error_or_reason", "operator_confirmed", "operator_note",
)

PASSED, FAILED, SKIPPED, UNCONFIRMED, WARNING = "PASSED", "FAILED", "SKIPPED", "UNCONFIRMED", "WARNING"


@dataclass
class Step:
    step_id: str
    phase: str
    kind: str                       # gpio | ro | mcu | sensor_check
    devices: tuple[str, ...]
    description: str
    pump: str | None = None
    needs_level_l: float | None = None


STEPS: tuple[Step, ...] = (
    Step("1_ro_prefill", "RO-Vorbefuellung", "ro", ("mixer_refill_pump",),
         "RO-Refill-Pumpe max. 3 s (ohne Ventil, wie im Prozess). Schlauch RO -> Mixing Tank frei, Tank nicht voll."),
    Step("2a_mixing_circulation", "Zirkulation", "gpio", ("mixing_circulation_pump",),
         "Mixing-Zirkulationspumpe max. 3 s. Tank muss genug Wasser haben (Trockenlauf).",
         needs_level_l=MIN_CIRCULATION_LITERS),
    Step("2b_sensor_circulation", "Zirkulation", "gpio", ("sensor_circulation_pump",),
         "Sensor-Zirkulationspumpe max. 3 s. Sensorbox-Leitung frei, Tank mit Wasser.",
         needs_level_l=MIN_CIRCULATION_LITERS),
    *(Step(f"3{letter}_mcu_b_{pump.lower()}", "Nahrstoffpumpen (Wasser)", "mcu", ("MCU_B/" + pump,),
           f"MCU_B {pump} ({EXPECTED_MCU_B_ROLES[pump]}): DOSE {PUMP_DOSE_ML} ml WASSER, Saugschlauch in Wasser, "
           "Druckleitung in den Tank. Software-Watchdog 3 s.", pump=pump)
      for letter, pump in zip("abcd", ("P1", "P2", "P3", "P4"))),
    Step("4_ro_refill", "RO-Nachbefuellung", "ro", ("mixer_refill_pump",),
         "RO-Refill-Pumpe max. 3 s (wie Schritt 1, ohne Ventil)."),
    Step("5a_post_mix", "Nachmischen", "gpio", ("mixing_circulation_pump", "sensor_circulation_pump"),
         "Mixing- und Sensorzirkulation gemeinsam max. 3 s.", needs_level_l=MIN_CIRCULATION_LITERS),
    Step("5b_sensor_check", "Sensorwerte", "sensor_check", (),
         "Sensorwerte (Fuellstand, EC, pH, Temperatur, O2) lesen und auf Plausibilitaet pruefen; keine Ausgabe."),
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def sensor_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"available": False}
    mixer, ro, water = (snapshot.get(k) or {} for k in ("mixer", "ro", "water_values"))
    return {
        "available": True,
        "mixer_l": _finite(mixer.get("volume_liters_calc")),
        "mixer_l_raw": _finite(mixer.get("volume_liters_raw")),
        "ro_l": _finite(ro.get("volume_liters_calc")),
        "ec_ms_cm": _finite(water.get("ec_ms_cm")),
        "ph": _finite(water.get("ph")),
        "water_temperature_c": _finite(water.get("water_temperature")),
        "dissolved_oxygen": _finite(water.get("dissolved_oxygen")),
    }


def sensor_plausibility_problems(summary: dict[str, Any]) -> list[str]:
    if not summary.get("available"):
        return ["kein Sensor-Snapshot"]
    problems = [f"{k} fehlt/ungueltig" for k in ("mixer_l", "ro_l", "ec_ms_cm", "ph", "water_temperature_c", "dissolved_oxygen")
                if summary.get(k) is None]
    ph, temp, ec = summary.get("ph"), summary.get("water_temperature_c"), summary.get("ec_ms_cm")
    if ph is not None and not 0.0 <= ph < 14.0:
        problems.append(f"pH {ph} unplausibel")
    if temp is not None and not 0.0 <= temp <= 40.0:
        problems.append(f"Temperatur {temp} C unplausibel")
    if ec is not None and not 0.0 <= ec <= 10.0:
        problems.append(f"EC {ec} unplausibel")
    return problems


class TestLog:
    """CSV (je Ereignis flush+fsync) und Summary-JSON (atomar)."""

    def __init__(self, log_dir: Path, run_id: str, git_commit: str, now: Callable[[], datetime]) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = log_dir / f"ap05_hardware_test_{stamp}.csv"
        self.summary_path = log_dir / f"ap05_hardware_test_{stamp}_summary.json"
        self._run_id, self._git, self._now = run_id, git_commit, now
        self._file = self.csv_path.open("x", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._sync()

    def _sync(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())

    def row(self, **fields: Any) -> None:
        row = dict.fromkeys(CSV_FIELDS)
        row.update(run_id=self._run_id, timestamp=self._now().isoformat(timespec="milliseconds"), git_commit=self._git)
        for key, value in fields.items():
            if key not in CSV_FIELDS:
                raise KeyError(key)
            row[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        self._writer.writerow(row)
        self._sync()

    def write_summary(self, summary: dict[str, Any]) -> None:
        tmp = self.summary_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.summary_path)

    def close(self) -> None:
        self._file.close()


@dataclass
class Env:
    """Alle Aussenkontakte injizierbar (Tests laufen komplett ohne Hardware)."""
    input_fn: Callable[[str], str] = input
    print_fn: Callable[[str], None] = print
    now: Callable[[], datetime] = lambda: datetime.now().astimezone()
    mono: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    load_settings: Callable[[], dict[str, Any]] = lambda: json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    git_commit: Callable[[], str] = lambda: _git_commit()
    sensor_fn: Callable[[], dict[str, Any] | None] = lambda: None
    actuators_factory: Callable[[], Any] | None = None
    client_factory: Callable[[str, int, Callable[[str, str], None]], Any] | None = None
    load_mcu_b: Callable[[], dict[str, Any]] | None = None
    path_exists: Callable[[str], bool] = lambda p: Path(p).exists()


def _git_commit() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        return f"{head}{'+dirty' if dirty else ''}"
    except Exception as exc:
        return f"unknown ({type(exc).__name__})"


class StepFailed(Exception):
    pass


class AbortRun(Exception):
    pass


@dataclass
class Runner:
    env: Env
    log: TestLog
    min_circulation_liters: float = MIN_CIRCULATION_LITERS
    results: list[dict[str, Any]] = field(default_factory=list)
    actuators: Any = None
    client: Any = None
    mcu: dict[str, Any] | None = None
    shutdown_result: dict[str, Any] = field(default_factory=dict)
    raw_log_path: str | None = None
    _raw_sink: Callable[[str, str], None] | None = None

    # ---- Hilfen -------------------------------------------------------
    def say(self, text: str) -> None:
        self.env.print_fn(text)

    def ask(self, prompt: str) -> str:
        try:
            return self.env.input_fn(prompt).strip()
        except EOFError:
            return "abort"

    def _sensors(self) -> dict[str, Any]:
        try:
            return sensor_summary(self.env.sensor_fn())
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def _row(self, step: Step, event: str, **fields: Any) -> None:
        self.log.row(step_id=step.step_id, phase=step.phase, event=event, **fields)

    # ---- Schritte -------------------------------------------------------
    def _timed_gpio(self, step: Step, names: tuple[str, ...], seconds: float) -> dict[str, float]:
        """Alle ``names`` gleichzeitig einschalten, hoechstens ``seconds`` (<= MAX_ON_SECONDS), immer aus."""
        seconds = min(float(seconds), MAX_ON_SECONDS)
        outputs = [self.actuators.get(name) for name in names]
        runtimes: dict[str, float] = {}
        started: dict[str, tuple[float, str]] = {}
        try:
            for name, out in zip(names, outputs):
                out.on()
                started[name] = (self.env.mono(), self.env.now().isoformat(timespec="milliseconds"))
                self._row(step, "output_on", device=name, gpio_or_port=f"GPIO{out.gpio_pin}", requested_state="ON",
                          command_sent=True, t_on=started[name][1], evidence_level="COMMAND_SENT",
                          software_feedback=f"software_state_is_active={out.is_active} (kein elektrischer Readback)")
            first = min(t for t, _ in started.values())
            while self.env.mono() - first < seconds:
                self.env.sleep(min(0.05, max(0.0, seconds - (self.env.mono() - first))))
        finally:
            for name, out in reversed(list(zip(names, outputs))):
                if name not in started:
                    continue
                try:
                    out.off()
                except Exception as exc:
                    self._row(step, "output_off_error", device=name, requested_state="OFF", error_or_reason=f"{type(exc).__name__}: {exc}")
                    raise
                t_off_mono, t_off = self.env.mono(), self.env.now().isoformat(timespec="milliseconds")
                runtimes[name] = t_off_mono - started[name][0]
                self._row(step, "output_off", device=name, gpio_or_port=f"GPIO{out.gpio_pin}", requested_state="OFF",
                          command_sent=True, t_on=started[name][1], t_off=t_off, runtime_s=round(runtimes[name], 3),
                          evidence_level="COMMAND_SENT",
                          software_feedback=f"software_state_is_active={out.is_active} (kein elektrischer Readback)")
        return runtimes

    def _check_runtimes(self, runtimes: dict[str, float], pumps: tuple[str, ...]) -> None:
        for name in pumps:
            if runtimes.get(name, 0.0) > MAX_ON_SECONDS + RUNTIME_TOLERANCE_S:
                raise StepFailed(f"{name} lief {runtimes[name]:.2f} s (> {MAX_ON_SECONDS} s)")

    def _run_gpio(self, step: Step) -> None:
        runtimes = self._timed_gpio(step, step.devices, MAX_ON_SECONDS)
        self._check_runtimes(runtimes, step.devices)

    def _ensure_mcu_client(self, step: Step) -> Any:
        if self.client is not None:
            return self.client
        assert self.mcu is not None
        from services.peristaltic.safety import ensure_all_idle_before_dose
        self.client = self.env.client_factory(self.mcu["port"], self.mcu["baudrate"], self._raw)
        self.client.open()
        ping = self.client.ping()
        self._row(step, "mcu_connect", device="MCU_B", gpio_or_port=self.mcu["port"], command_sent=True,
                  evidence_level="SOFTWARE_FEEDBACK", software_feedback=f"PING: {ping.raw} (beweist die MCU-Identitaet NICHT)")
        statuses = ensure_all_idle_before_dose(self.client)
        self._row(step, "mcu_idle_check", device="MCU_B", gpio_or_port=self.mcu["port"], command_sent=True,
                  evidence_level="SOFTWARE_FEEDBACK", software_feedback=f"STATUS nach STOPALL: {statuses}")
        return self.client

    def _raw(self, direction: str, line: str) -> None:
        if self._raw_sink is not None:
            self._raw_sink(direction, line)

    def _run_mcu(self, step: Step) -> None:
        from services.peristaltic.protocol import LineKind, format_dose_command
        pump = step.pump
        client = self._ensure_mcu_client(step)
        command = format_dose_command(pump, PUMP_DOSE_ML)
        t_on = self.env.now().isoformat(timespec="milliseconds")
        t0 = self.env.mono()
        start = client.start_dose(pump, PUMP_DOSE_ML)
        self._row(step, "dose_command", device=f"MCU_B/{pump}", gpio_or_port=self.mcu["port"], requested_state=f"DOSE {PUMP_DOSE_ML} ml",
                  command_sent=command, t_on=t_on, evidence_level="SOFTWARE_FEEDBACK", software_feedback=f"Antwort: {start.raw}")
        done, reason = None, None
        while True:
            elapsed = self.env.mono() - t0
            if elapsed >= MAX_ON_SECONDS:
                reason = f"Watchdog: kein DONE nach {MAX_ON_SECONDS} s"
                break
            event = client.poll_pump_event(pump, 0.1)
            if event is None:
                continue
            if event.kind is LineKind.DONE:
                done = event
                break
            if event.kind is not LineKind.RUNNING:
                reason = f"Unerwartete Antwort: {event.raw}"
                break
        runtime = self.env.mono() - t0
        if done is None:
            try:
                client.stop(pump)
            finally:
                client.stop_all()
        t_off = self.env.now().isoformat(timespec="milliseconds")
        self._row(step, "dose_end", device=f"MCU_B/{pump}", gpio_or_port=self.mcu["port"], requested_state="IDLE", t_on=t_on, t_off=t_off,
                  runtime_s=round(runtime, 3), evidence_level="SOFTWARE_FEEDBACK",
                  software_feedback=f"Ende: {done.raw if done else 'STOP/STOPALL gesendet'}", error_or_reason=reason)
        if reason is not None:
            raise StepFailed(reason)
        if abs(done.ml - PUMP_DOSE_ML) > 0.001:
            raise StepFailed(f"DONE meldet {done.ml} ml statt {PUMP_DOSE_ML} ml")
        if runtime > MAX_ON_SECONDS + RUNTIME_TOLERANCE_S:
            raise StepFailed(f"Laufzeit {runtime:.2f} s > {MAX_ON_SECONDS} s")
        status = client.status()
        self._row(step, "mcu_status_after", device=f"MCU_B/{pump}", gpio_or_port=self.mcu["port"], command_sent="STATUS",
                  evidence_level="SOFTWARE_FEEDBACK", software_feedback=f"STATUS: {status}")
        if any(state != "IDLE" for state in status.values()):
            raise StepFailed(f"Nach Dosis nicht alle Pumpen IDLE: {status}")

    def _run_sensor_check(self, step: Step) -> list[str]:
        """Nur dokumentieren: auffaellige Werte sind eine WARNUNG, kein Hardwarefehler und kein Erfolg."""
        summary = self._sensors()
        problems = sensor_plausibility_problems(summary)
        self._row(step, "sensor_check", sensors=summary, evidence_level="SOFTWARE_FEEDBACK",
                  software_feedback="Sensorwerte gelesen", error_or_reason=("WARNUNG: " + "; ".join(problems)) if problems else None)
        return problems

    def _block_reason(self, step: Step, before: dict[str, Any]) -> str | None:
        if step.kind == "mcu":
            if self.mcu is None:
                return "MCU_B-Konfiguration nicht verfuegbar"
            if not self.env.path_exists(self.mcu["port"]):
                return f"MCU_B-Port {self.mcu['port']} nicht gefunden"
            if self.mcu["roles"].get(step.pump) != EXPECTED_MCU_B_ROLES[step.pump]:
                return f"Mapping {step.pump}={self.mcu['roles'].get(step.pump)!r} weicht von {EXPECTED_MCU_B_ROLES[step.pump]!r} ab"
            estimate = self.mcu["estimated_runtime_s"].get(step.pump)
            if estimate is None or estimate > MCU_MAX_ESTIMATED_S:
                return f"Laufzeit nicht verlaesslich auf {MAX_ON_SECONDS} s begrenzbar (Schaetzung {estimate})"
            return None
        if step.kind == "sensor_check":
            return None
        if not before.get("available") or before.get("mixer_l") is None:
            return "kein gueltiger Fuellstand (Sensor-Snapshot)"
        if step.kind == "ro":
            ro_l = before.get("ro_l")
            if ro_l is None or ro_l < MIN_RO_LITERS:
                return f"RO-Vorrat {ro_l} L unter {MIN_RO_LITERS} L"
        if step.needs_level_l is not None and before["mixer_l"] < self.min_circulation_liters:
            return (f"Fuellstand {before['mixer_l']:.1f} L < {self.min_circulation_liters:.1f} L "
                    "(Trockenlaufschutz): Tank vorher separat auf Wasserstand bringen")
        return None

    def run_step(self, step: Step) -> str:
        before = self._sensors()
        self.say(f"\n=== {step.step_id} | {step.phase} ===\n{step.description}\nAusgaenge/Geraete: {', '.join(step.devices) or '-'}"
                 f"\nSensoren vorher: {before}")
        self._row(step, "step_prepared", device=", ".join(step.devices), sensors=before, requested_state="prepared")
        block = self._block_reason(step, before)
        if block:
            self.say(f"GESPERRT: {block}")
            self._row(step, "step_result", result=SKIPPED, error_or_reason=f"gesperrt: {block}")
            return SKIPPED
        answer = self.ask(f"Wasserwege/Schlaeuche geprueft? Freigabe mit 'go {step.step_id}', 'skip' oder 'abort': ")
        self._row(step, "operator_release", operator_note=answer)
        if answer == "skip":
            self._row(step, "step_result", result=SKIPPED, error_or_reason="vom Bediener uebersprungen")
            return SKIPPED
        if answer != f"go {step.step_id}":
            raise AbortRun(f"Freigabe nicht erteilt (Eingabe: {answer!r})")
        try:
            outcome = {"gpio": self._run_gpio, "ro": self._run_gpio, "mcu": self._run_mcu, "sensor_check": self._run_sensor_check}[step.kind](step)
        except StepFailed as exc:
            self._row(step, "step_result", result=FAILED, error_or_reason=str(exc), sensors=self._sensors())
            self.say(f"FEHLER: {exc}")
            return FAILED
        except Exception as exc:
            self._row(step, "step_result", result=FAILED, error_or_reason=f"{type(exc).__name__}: {exc}", sensors=self._sensors())
            self.say(f"FEHLER: {type(exc).__name__}: {exc}")
            return FAILED
        after = self._sensors()
        if step.kind == "sensor_check":
            result = WARNING if outcome else PASSED
            self._row(step, "step_result", result=result, sensors=after, evidence_level="SOFTWARE_FEEDBACK",
                      software_feedback="Plausibilitaetspruefung der Software", error_or_reason=("WARNUNG: " + "; ".join(outcome)) if outcome else None)
            return result
        observed = self.ask("Physische Funktion beobachtet und bestaetigt? [j=ja / n=nein / u=unklar]: ").lower()
        note = self.ask("Beobachtung (Freitext, optional): ")
        confirmed = {"j": "yes", "n": "no"}.get(observed, "unknown")
        result = {"yes": PASSED, "no": FAILED}.get(confirmed, UNCONFIRMED)
        self._row(step, "operator_observation", operator_confirmed=confirmed, operator_note=note, sensors=after,
                  evidence_level="OPERATOR_CONFIRMED" if confirmed == "yes" else "COMMAND_SENT")
        self._row(step, "step_result", result=result, operator_confirmed=confirmed, operator_note=note, sensors=after,
                  error_or_reason="Bediener verneint Funktion" if result == FAILED else (
                      "Befehl gesendet, physische Funktion nicht bestaetigt" if result == UNCONFIRMED else None))
        return result

    # ---- Ablauf ---------------------------------------------------------
    def shutdown(self) -> dict[str, Any]:
        step = Step("99_final_shutdown", "Abschaltung", "gpio", (), "Alle Ausgaenge aus, STOPALL, Statuspruefung")
        result: dict[str, Any] = {"gpio_errors": [], "gpio_all_off": None, "mcu_stopall": None, "mcu_all_idle": None}
        if self.actuators is not None:
            try:
                result["gpio_errors"] = list(self.actuators.safe_shutdown_all())
                result["gpio_all_off"] = not any(self.actuators.status_payload().values())
            except Exception as exc:
                result["gpio_errors"].append(f"{type(exc).__name__}: {exc}")
                result["gpio_all_off"] = False
        if self.client is not None:
            try:
                self.client.stop_all()
                result["mcu_stopall"] = True
                result["mcu_all_idle"] = all(s == "IDLE" for s in self.client.status().values())
            except Exception as exc:
                result["mcu_stopall"] = False
                result["mcu_error"] = f"{type(exc).__name__}: {exc}"
        ok = result["gpio_all_off"] is True and not result["gpio_errors"] and result["mcu_all_idle"] in (True, None) and result["mcu_stopall"] in (True, None)
        result["ok"] = ok
        self._row(step, "final_shutdown", evidence_level="SOFTWARE_FEEDBACK", software_feedback=result,
                  result=PASSED if ok else FAILED, error_or_reason=None if ok else "Abschaltung nicht verifiziert")
        self.shutdown_result = result
        return result

    def summarize(self, run_id: str, started: str, git: str, status: str, abort_reason: str | None) -> dict[str, Any]:
        counts = {r: sum(1 for x in self.results if x["result"] == r) for r in (PASSED, FAILED, SKIPPED, UNCONFIRMED, WARNING)}
        executed = [x for x in self.results if x["result"] in (PASSED, FAILED, UNCONFIRMED, WARNING)]
        return {
            "run_id": run_id, "git_commit": git, "started": started, "finished": self.env.now().isoformat(timespec="seconds"),
            "run_status": status, "abort_reason": abort_reason,
            "components_total": len(STEPS), "components_executed": len(executed), "passed": counts[PASSED], "failed": counts[FAILED],
            "skipped": counts[SKIPPED], "unconfirmed_command_only": counts[UNCONFIRMED], "warnings": counts[WARNING],
            "steps": self.results, "shutdown": self.shutdown_result,
            "csv": str(self.log.csv_path), "summary": str(self.log.summary_path), "raw_serial_log": self.raw_log_path,
            "note": "PASSED nur mit Bedienerbestaetigung; UNCONFIRMED = Befehl gesendet, Funktion nicht bestaetigt. "
                    "GPIO-Rueckmeldung ist nur der Softwarezustand.",
        }


def print_plan(print_fn: Callable[[str], None] = print) -> None:
    print_fn("AP-05 Hardware-Funktionstest - PLAN (keine Hardware wird angesteuert)\n")
    for step in STEPS:
        print_fn(f"- {step.step_id}: {step.phase} | {', '.join(step.devices) or '-'} | {step.description}")
    print_fn(f"\nPumpen/Ausgaenge max. {MAX_ON_SECONDS} s. Start mit: python3 scripts/ap05_hardware_test.py --execute")


def run(env: Env, log_dir: Path, min_circulation_liters: float = MIN_CIRCULATION_LITERS) -> int:
    run_id = uuid.uuid4().hex[:12]
    started = env.now().isoformat(timespec="seconds")
    git = env.git_commit()

    # Fail-closed Gate VOR jedem Hardwarezugriff (wird nie vom Skript gesetzt).
    try:
        settings = env.load_settings()
    except Exception as exc:
        env.print_fn(f"BLOCKIERT: Settings nicht lesbar ({type(exc).__name__}: {exc}).")
        return 2
    if settings.get("hardware_execution_enabled") is not True:
        env.print_fn("BLOCKIERT: hardware_execution_enabled ist nicht true (vom Bediener zu setzen). Keine Hardware angesprochen.")
        return 2
    if env.actuators_factory is None or env.client_factory is None or env.load_mcu_b is None:
        env.print_fn("BLOCKIERT: Hardware-Anbindung nicht konfiguriert.")
        return 2

    log = TestLog(log_dir, run_id, git, env.now)
    runner = Runner(env, log, min_circulation_liters)
    status, abort_reason, raw_logger = "COMPLETED", None, None
    prepare = Step("00_preflight", "Vorbereitung", "gpio", (), "Preflight")
    log.row(step_id=prepare.step_id, phase=prepare.phase, event="run_start", result="started",
            software_feedback={"run_id": run_id, "git": git, "plan": [s.step_id for s in STEPS]})
    try:
        try:
            runner.mcu = env.load_mcu_b()
        except Exception as exc:
            runner.mcu = None
            log.row(step_id=prepare.step_id, phase=prepare.phase, event="mcu_config", error_or_reason=f"{type(exc).__name__}: {exc}")
        runner.actuators = env.actuators_factory()
        from gpio_config import OUTPUTS
        for name, key in (("mixer_refill_pump", "mixer_refill_pump"),
                          ("mixing_circulation_pump", "mixing_circulation_pump"), ("sensor_circulation_pump", "sensor_circulation_pump")):
            runner.actuators.add(name, OUTPUTS[key])
        if runner.mcu is not None:
            from services.peristaltic.session_log import PeristalticSessionLogger
            raw_logger = PeristalticSessionLogger("ap05_hardware_test", log_dir=log_dir / "peristaltic", session_id=run_id)
            runner._raw_sink = raw_logger.log_raw
            runner.raw_log_path = str(raw_logger.raw_log_path)
        for step in STEPS:
            result = runner.run_step(step)
            runner.results.append({"step_id": step.step_id, "phase": step.phase, "devices": list(step.devices), "result": result})
            log.write_summary(runner.summarize(run_id, started, git, "IN_PROGRESS", None))
            if result == FAILED:
                status, abort_reason = "STOPPED_ON_ERROR", f"Fehler in {step.step_id}"
                break
    except (AbortRun, KeyboardInterrupt) as exc:
        status, abort_reason = "ABORTED", str(exc) or "Strg+C/SIGTERM"
        log.row(step_id=prepare.step_id, phase=prepare.phase, event="abort", result=SKIPPED, error_or_reason=abort_reason)
    except Exception as exc:
        status, abort_reason = "ERROR", f"{type(exc).__name__}: {exc}"
        log.row(step_id=prepare.step_id, phase=prepare.phase, event="run_error", result=FAILED, error_or_reason=abort_reason)
    finally:
        runner.shutdown()
        done = {r["step_id"] for r in runner.results}
        for step in STEPS:
            if step.step_id not in done:
                runner.results.append({"step_id": step.step_id, "phase": step.phase, "devices": list(step.devices), "result": SKIPPED,
                                       "reason": abort_reason or "nicht erreicht"})
        summary = runner.summarize(run_id, started, git, status, abort_reason)
        log.write_summary(summary)
        if runner.client is not None:
            try:
                runner.client.close()
            except Exception:
                pass
        if raw_logger is not None:
            raw_logger.close()
        if runner.actuators is not None:
            try:
                runner.actuators.close_all()
            except Exception:
                pass
        log.close()
    env.print_fn("\n=== ERGEBNIS ===\n" + json.dumps({k: summary[k] for k in (
        "run_status", "components_total", "components_executed", "passed", "failed", "skipped", "unconfirmed_command_only", "warnings", "shutdown",
        "csv", "summary", "raw_serial_log")}, indent=2, ensure_ascii=False))
    return 0 if status == "COMPLETED" and summary["failed"] == 0 and runner.shutdown_result.get("ok") else 1


def _real_env() -> Env:
    from gpio_config import ACTIVE_LOW
    from services.mqtt_topic_reader import MqttTopicReader
    from services.peristaltic.calibration import has_volumetric_calibration, load_calibration_data
    from services.peristaltic.firmware_profiles import load_firmware_profiles, resolve_firmware_ml_per_step
    from services.peristaltic.models import load_mapping
    from services.peristaltic.serial_client import ClientConfig, PeristalticSerialClient
    from process.nutrient_dosing import (CALIBRATION_DATA_PATH, CONTROLLER, FIRMWARE_PROFILES_PATH, MAPPING_PATH,
                                         estimate_chunk_runtime_ms)

    system = json.loads(SYSTEM_CONFIG_PATH.read_text(encoding="utf-8"))["mqtt"]
    reader = MqttTopicReader(system["host"], system["port"], system["sensor_topic"], max_age_seconds=5.0)
    reader.start()
    reader.wait_for_first_snapshot(5.0)

    def load_mcu_b() -> dict[str, Any]:
        mapping = load_mapping(REPO_ROOT / MAPPING_PATH).controllers[CONTROLLER]
        profiles = load_firmware_profiles(REPO_ROOT / FIRMWARE_PROFILES_PATH)
        profile = profiles.controllers[CONTROLLER]
        calibration = load_calibration_data(REPO_ROOT / CALIBRATION_DATA_PATH)
        estimates = {}
        for pump in EXPECTED_MCU_B_ROLES:
            if not has_volumetric_calibration(calibration, CONTROLLER, pump):
                estimates[pump] = None
                continue
            ml_per_step = resolve_firmware_ml_per_step(profiles, CONTROLLER, pump)
            estimates[pump] = estimate_chunk_runtime_ms(PUMP_DOSE_ML, ml_per_step, profile) / 1000.0
        return {"port": mapping.port, "baudrate": mapping.baudrate, "roles": dict(mapping.pumps), "estimated_runtime_s": estimates}

    def actuators_factory() -> Any:
        from hardware.actuator_manager import ActuatorManager
        return ActuatorManager(active_low=ACTIVE_LOW)

    def client_factory(port: str, baudrate: int, on_raw: Callable[[str, str], None]) -> Any:
        return PeristalticSerialClient(port, config=ClientConfig(baudrate=baudrate), on_raw_line=on_raw)

    return Env(sensor_fn=reader.get_latest, actuators_factory=actuators_factory, client_factory=client_factory, load_mcu_b=load_mcu_b)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="Hardware-Test wirklich ausfuehren (sonst nur Plan)")
    parser.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    parser.add_argument("--min-circulation-liters", type=float, default=MIN_CIRCULATION_LITERS)
    args = parser.parse_args(argv)
    if not args.execute:
        print_plan()
        return 0
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    return run(_real_env(), Path(args.log_dir), args.min_circulation_liters)


if __name__ == "__main__":
    raise SystemExit(main())
