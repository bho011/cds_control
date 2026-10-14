"""Hardwarefreie Tests fuer scripts/ap05_hardware_test.py (nur Fakes, keine GPIO/Serial/MQTT)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import ap05_hardware_test as t
from services.peristaltic.protocol import parse_line

GOOD_SENSORS = {
    "mixer": {"volume_liters_calc": 20.0, "volume_liters_raw": 60.0},
    "ro": {"volume_liters_calc": 300.0},
    "water_values": {"ec_ms_cm": 0.05, "ph": 7.0, "water_temperature": 20.0, "dissolved_oxygen": 8.0},
}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    def now(self) -> datetime:
        return datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))) + timedelta(seconds=self.t)


class FakeOutput:
    def __init__(self, name: str, pin: int, events: list) -> None:
        self.name, self.gpio_pin, self.is_active, self._events = name, pin, False, events

    def on(self) -> None:
        self.is_active = True
        self._events.append(("on", self.name))

    def off(self) -> None:
        self.is_active = False
        self._events.append(("off", self.name))


class FakeActuators:
    def __init__(self) -> None:
        self.events: list = []
        self.outputs: dict[str, FakeOutput] = {}
        self.closed = False

    def add(self, name: str, pin: int) -> FakeOutput:
        self.outputs[name] = FakeOutput(name, pin, self.events)
        return self.outputs[name]

    def get(self, name: str) -> FakeOutput:
        return self.outputs[name]

    def status_payload(self) -> dict[str, bool]:
        return {n: o.is_active for n, o in self.outputs.items()}

    def safe_shutdown_all(self) -> list[str]:
        for o in self.outputs.values():
            o.off()
        return []

    def close_all(self) -> list[str]:
        self.closed = True
        return []


class FakeClient:
    def __init__(self, clock: FakeClock, never_done: bool = False, done_ml: float = 1.0) -> None:
        self.clock, self.never_done, self.done_ml = clock, never_done, done_ml
        self.sent: list[str] = []
        self.pending: dict[str, list] = {}

    def open(self) -> None:
        self.sent.append("OPEN")

    def close(self) -> None:
        self.sent.append("CLOSE")

    def ping(self):
        self.sent.append("PING")
        return parse_line("OK READY")

    def status(self) -> dict[str, str]:
        self.sent.append("STATUS")
        return {p: "IDLE" for p in ("P1", "P2", "P3", "P4")}

    def stop_all(self):
        self.sent.append("STOPALL")

    def stop(self, pump: str):
        self.sent.append(f"STOP {pump}")

    def start_dose(self, pump: str, ml: float):
        self.sent.append(f"DOSE {pump} {ml:.3f}")
        return parse_line(f"OK START {pump} {ml:.3f}")

    def poll_pump_event(self, pump: str, timeout: float = 0.1):
        self.clock.t += 1.6 if not self.never_done else timeout
        return None if self.never_done else parse_line(f"DONE {pump} {self.done_ml:.3f}")


def make_env(tmp_path: Path, answers: list[str], *, sensors=GOOD_SENSORS, enabled=True, never_done=False,
             estimate: float = 1.7, done_ml: float = 1.0):
    clock = FakeClock()
    actuators = FakeActuators()
    client = FakeClient(clock, never_done=never_done, done_ml=done_ml)
    queue = list(answers)
    printed: list[str] = []
    mcu = {"port": "/dev/fake-mcu-b", "baudrate": 115200, "roles": dict(t.EXPECTED_MCU_B_ROLES),
           "estimated_runtime_s": {p: estimate for p in t.EXPECTED_MCU_B_ROLES}}
    env = t.Env(
        input_fn=lambda prompt: queue.pop(0) if queue else "abort",
        print_fn=printed.append,
        now=clock.now, mono=clock.mono, sleep=clock.sleep,
        load_settings=lambda: {"hardware_execution_enabled": enabled},
        git_commit=lambda: "abc1234+dirty",
        sensor_fn=lambda: sensors,
        actuators_factory=lambda: actuators,
        client_factory=lambda port, baud, raw: client,
        load_mcu_b=lambda: mcu,
        path_exists=lambda p: True,
    )
    return env, actuators, client, printed


def all_go(step_ids=None, observation="j"):
    answers = []
    for step in t.STEPS:
        answers.append(f"go {step.step_id}")
        if step.kind != "sensor_check":
            answers += [observation, "ok"]
    return answers


def read_csv(tmp_path: Path):
    files = sorted(tmp_path.glob("ap05_hardware_test_*.csv"))
    assert len(files) == 1
    with files[0].open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_summary(tmp_path: Path):
    files = sorted(tmp_path.glob("ap05_hardware_test_*_summary.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_gate_blocks_without_hardware_execution_enabled(tmp_path):
    env, actuators, client, printed = make_env(tmp_path, all_go(), enabled=False)
    assert t.run(env, tmp_path) == 2
    assert not actuators.events and not client.sent
    assert list(tmp_path.glob("ap05_*")) == []


def test_plan_only_default_touches_nothing(capsys):
    assert t.main([]) == 0
    assert "PLAN" in capsys.readouterr().out


def test_happy_path_logs_everything_and_shuts_down(tmp_path):
    env, actuators, client, _ = make_env(tmp_path, all_go())
    assert t.run(env, tmp_path) == 0
    summary = read_summary(tmp_path)
    assert summary["run_status"] == "COMPLETED"
    assert summary["passed"] == len(t.STEPS) and summary["failed"] == 0 and summary["skipped"] == 0
    assert summary["shutdown"]["ok"] is True
    assert not any(actuators.status_payload().values()) and actuators.closed
    rows = read_csv(tmp_path)
    assert set(t.CSV_FIELDS) == set(rows[0].keys())
    assert {r["run_id"] for r in rows} == {summary["run_id"]}
    assert all(r["git_commit"] == "abc1234+dirty" and r["timestamp"].endswith("+02:00") for r in rows)
    gpio_off = [r for r in rows if r["event"] == "output_off"]
    assert {r["device"] for r in gpio_off} == {"mixer_refill_pump", "mixing_circulation_pump", "sensor_circulation_pump"}
    assert "supply_valve_6" not in actuators.outputs
    assert gpio_off and all(0 < float(r["runtime_s"]) <= t.MAX_ON_SECONDS + 0.1 for r in gpio_off)
    assert {r["device"] for r in rows if r["event"] == "dose_command"} == {f"MCU_B/P{i}" for i in range(1, 5)}
    assert [c for c in client.sent if c.startswith("DOSE")] == [f"DOSE P{i} 1.000" for i in range(1, 5)]
    assert rows[-1]["event"] == "final_shutdown"


def test_command_without_operator_confirmation_is_not_passed(tmp_path):
    env, *_ = make_env(tmp_path, all_go(observation="u"))
    t.run(env, tmp_path)
    summary = read_summary(tmp_path)
    by_id = {s["step_id"]: s["result"] for s in summary["steps"]}
    assert by_id["1_ro_prefill"] == t.UNCONFIRMED
    assert by_id["5b_sensor_check"] == t.PASSED          # reine Softwarepruefung ohne Ausgabe
    assert summary["passed"] == 1 and summary["unconfirmed_command_only"] == len(t.STEPS) - 1
    rows = read_csv(tmp_path)
    assert not any(r["evidence_level"] == "OPERATOR_CONFIRMED" for r in rows)


def test_operator_negative_stops_run_and_skips_rest(tmp_path):
    answers = ["go 1_ro_prefill", "j", "", "go 2a_mixing_circulation", "n", "Pumpe lief nicht"]
    env, actuators, client, _ = make_env(tmp_path, answers)
    assert t.run(env, tmp_path) == 1
    summary = read_summary(tmp_path)
    by_id = {s["step_id"]: s["result"] for s in summary["steps"]}
    assert by_id["2a_mixing_circulation"] == t.FAILED
    assert by_id["2b_sensor_circulation"] == t.SKIPPED and by_id["4_ro_refill"] == t.SKIPPED
    assert summary["run_status"] == "STOPPED_ON_ERROR"
    assert not any(c.startswith("DOSE") for c in client.sent)
    assert not any(actuators.status_payload().values())


def test_missing_release_aborts_without_switching(tmp_path):
    env, actuators, client, _ = make_env(tmp_path, ["go wrong_step"])
    t.run(env, tmp_path)
    summary = read_summary(tmp_path)
    assert summary["run_status"] == "ABORTED"
    assert not actuators.events or all(kind == "off" for kind, _ in actuators.events)
    assert not any(c.startswith("DOSE") for c in client.sent)


def test_low_tank_blocks_circulation_and_never_switches(tmp_path):
    low = {**GOOD_SENSORS, "mixer": {"volume_liters_calc": 2.0}}
    env, actuators, _, _ = make_env(tmp_path, ["skip"] * 10, sensors=low)
    t.run(env, tmp_path)
    rows = read_csv(tmp_path)
    blocked = [r for r in rows if r["event"] == "step_result" and r["step_id"] == "2a_mixing_circulation"]
    assert blocked[0]["result"] == t.SKIPPED and "Trockenlauf" in blocked[0]["error_or_reason"]
    assert ("on", "mixing_circulation_pump") not in actuators.events


def test_mcu_watchdog_stops_pump_after_3_seconds(tmp_path):
    answers = ["skip"] * 3 + ["go 3a_mcu_b_p1"]
    env, actuators, client, _ = make_env(tmp_path, answers, never_done=True)
    assert t.run(env, tmp_path) == 1
    assert "STOP P1" in client.sent and client.sent.count("STOPALL") >= 2
    rows = read_csv(tmp_path)
    end = [r for r in rows if r["event"] == "dose_end"][0]
    assert t.MAX_ON_SECONDS <= float(end["runtime_s"]) < t.MAX_ON_SECONDS + 0.3
    assert read_summary(tmp_path)["failed"] == 1


def test_mcu_step_blocked_when_runtime_not_reliably_bounded(tmp_path):
    env, _, client, _ = make_env(tmp_path, ["skip"] * 3 + ["skip"] * 10, estimate=2.6)
    t.run(env, tmp_path)
    assert not any(c.startswith("DOSE") for c in client.sent)
    rows = read_csv(tmp_path)
    reasons = [r["error_or_reason"] for r in rows if r["event"] == "step_result" and r["step_id"].startswith("3")]
    assert len(reasons) == 4 and all("nicht verlaesslich" in r for r in reasons)


def test_wrong_done_amount_fails(tmp_path):
    env, *_ = make_env(tmp_path, ["skip"] * 3 + ["go 3a_mcu_b_p1"], done_ml=0.5)
    t.run(env, tmp_path)
    assert read_summary(tmp_path)["failed"] == 1


def test_implausible_sensor_values_are_warning_not_failure_nor_success(tmp_path):
    bad = {**GOOD_SENSORS, "water_values": {**GOOD_SENSORS["water_values"], "ph": 14.4, "water_temperature": 51.6}}
    env, *_ = make_env(tmp_path, ["skip"] * 9 + ["go 5b_sensor_check"], sensors=bad)
    t.run(env, tmp_path)
    by_id = {s["step_id"]: s["result"] for s in read_summary(tmp_path)["steps"]}
    assert by_id["5b_sensor_check"] == t.WARNING
    summary = read_summary(tmp_path)
    assert summary["warnings"] == 1 and summary["failed"] == 0
    rows = read_csv(tmp_path)
    check = [r for r in rows if r["event"] == "step_result" and r["step_id"] == "5b_sensor_check"][0]
    assert "pH 14.4" in check["error_or_reason"] and check["error_or_reason"].startswith("WARNUNG")


def test_ctrl_c_during_run_still_shuts_down(tmp_path):
    env, actuators, client, _ = make_env(tmp_path, [])

    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    env.input_fn = interrupt
    t.run(env, tmp_path)
    assert read_summary(tmp_path)["run_status"] == "ABORTED"
    assert not any(actuators.status_payload().values())


def test_pump_never_driven_longer_than_cap_even_if_requested(tmp_path):
    env, actuators, *_ = make_env(tmp_path, [])
    runner = t.Runner(env, t.TestLog(tmp_path, "r", "g", env.now))
    runner.actuators = actuators
    for name, pin in (("mixing_circulation_pump", 23),):
        actuators.add(name, pin)
    runtimes = runner._timed_gpio(t.STEPS[1], ("mixing_circulation_pump",), 60.0)
    assert runtimes["mixing_circulation_pump"] <= t.MAX_ON_SECONDS + 0.1
    runner.log.close()
