"""AP-04: Water-Cycle-Abschluss (process/water_cycle.py).

Vollstaendig hardwarefrei (AP-00-Isolation aus conftest): ActuatorManager,
Sensor-Reader, MQTT-Publisher, Logger, Auto-Zirkulation sowie Fill-/Drain-Phase
sind Fakes. Alle Fakes schreiben in EIN gemeinsames Ereignisprotokoll.

O1 (offene Hardwareentscheidung) bleibt unberuehrt: Es wird nur geprueft, dass
Fehler der Abschaltung sichtbar werden und alle Cleanup-Schritte laufen.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import process.water_cycle as wc
from process.common import PhaseResult

REPO_ROOT = Path(__file__).resolve().parent.parent

OK_FILL = PhaseResult(True, "target_reached_by_sensor", 10.0, 200.0, 190.0)
BAD_FILL = PhaseResult(False, "fill_timeout", 10.0, 50.0, 40.0)
OK_DRAIN = PhaseResult(True, "empty_by_sensor", 200.0, 0.0, 200.0)
BAD_DRAIN = PhaseResult(False, "no_drain_progress", 200.0, 150.0, 50.0)
BAD_DRAIN_CLEANUP = PhaseResult(False, "drain_cleanup_failed", 200.0, 0.0, 200.0)
INTERRUPTED_FILL = PhaseResult(False, "keyboard_interrupt", 10.0, 20.0, 10.0)


class FakeOutput:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def on(self):
        self.events.append(f"{self.name}:on")

    def off(self):
        self.events.append(f"{self.name}:off")


class Env:
    """Scenario + shared event log; installs all fakes into process.water_cycle."""

    def __init__(self):
        self.events: list[str] = []
        self.payloads: list[dict] = []
        self.constructed: list[str] = []

        self.settings = {"hardware_execution_enabled": True}
        self.answers = ["ja"]
        self.hardware_confirmed = True
        self.drain_confirmed = True
        self.fill_result = OK_FILL
        self.drain_result = OK_DRAIN
        self.fill_exc: BaseException | None = None
        self.drain_exc: BaseException | None = None

        self.reader_error = None
        self.reader_wait = True
        self.reader_close_exc = None
        self.logger_close_exc = None
        self.publisher_close_exc = None
        self.publish_result = True
        self.publish_exc = None
        self.publish_exc_on: dict[str, BaseException] = {}
        self.auto_stop_exc = None
        self.shutdown_result: list[str] = []
        self.shutdown_exc = None
        self.close_result: list[str] = []
        self.close_exc = None
        self.actuator_ctor_exc = None

    # ---- helpers ----
    def states(self):
        return [p["process_state"] for p in self.payloads]

    def last(self):
        return self.payloads[-1]

    def any_on(self):
        return [e for e in self.events if e.endswith(":on")]


@pytest.fixture
def env(monkeypatch):
    e = Env()

    class FakeReader:
        def __init__(self, host, port, topic):
            e.constructed.append("reader")

        def start(self):
            e.events.append("reader.start")

        def get_error(self):
            return e.reader_error

        def wait_for_first_snapshot(self, timeout_seconds):
            return e.reader_wait

        def close(self):
            e.events.append("reader.close")
            if e.reader_close_exc:
                raise e.reader_close_exc

    class FakePublisher:
        def __init__(self):
            e.constructed.append("publisher")

        def publish_json(self, payload):
            e.events.append(f"publish:{payload['process_state']}")
            if e.publish_exc:
                raise e.publish_exc
            if payload["process_state"] in e.publish_exc_on:
                raise e.publish_exc_on[payload["process_state"]]
            e.payloads.append(payload)
            return e.publish_result

        def close(self):
            e.events.append("publisher.close")
            if e.publisher_close_exc:
                raise e.publisher_close_exc

    class FakeLogger:
        def __init__(self, process_name):
            e.constructed.append("logger")

        def close(self):
            e.events.append("logger.close")
            if e.logger_close_exc:
                raise e.logger_close_exc

    class FakeActuators:
        def __init__(self, active_low):
            if e.actuator_ctor_exc:
                raise e.actuator_ctor_exc
            e.constructed.append("actuators")
            self._outputs = {}

        def add(self, name, gpio_pin):
            self._outputs[name] = FakeOutput(name, e.events)
            return self._outputs[name]

        def status_payload(self):
            return {name: False for name in self._outputs}

        def safe_shutdown_all(self):
            e.events.append("safe_shutdown_all")
            if e.shutdown_exc:
                raise e.shutdown_exc
            return list(e.shutdown_result)

        def close_all(self):
            e.events.append("close_all")
            if e.close_exc:
                raise e.close_exc
            return list(e.close_result)

    class FakeAuto:
        def __init__(self, actuators, config):
            pass

        def stop(self):
            e.events.append("auto.stop")
            if e.auto_stop_exc:
                raise e.auto_stop_exc

    def fake_fill(**kwargs):
        e.events.append("fill")
        if e.fill_exc:
            raise e.fill_exc
        return e.fill_result

    def fake_drain(**kwargs):
        e.events.append("drain")
        if e.drain_exc:
            raise e.drain_exc
        return e.drain_result

    def fake_input(prompt=""):
        if not e.answers:
            raise AssertionError("unexpected extra input()")
        return e.answers.pop(0)

    monkeypatch.setattr(wc, "load_settings", lambda: dict(e.settings))
    monkeypatch.setattr(wc, "require_hardware_confirmation", lambda s: e.hardware_confirmed)
    monkeypatch.setattr(wc, "confirm_drain", lambda s: e.drain_confirmed)
    monkeypatch.setattr(wc, "MqttTopicReader", FakeReader)
    monkeypatch.setattr(wc, "MqttPublisher", FakePublisher)
    monkeypatch.setattr(wc, "ProcessRunLogger", FakeLogger)
    monkeypatch.setattr(wc, "ActuatorManager", FakeActuators)
    monkeypatch.setattr(wc, "AutoCirculationController", FakeAuto)
    monkeypatch.setattr(wc, "run_fill_phase", fake_fill)
    monkeypatch.setattr(wc, "run_drain_phase", fake_drain)
    monkeypatch.setattr("builtins.input", fake_input)
    return e


def run(env):
    return wc.main()


# ---- Gesamtergebnis (W1) ----------------------------------------------------

def test_fill_and_drain_success(env):
    assert run(env) == 0
    assert env.states() == ["WATER_CYCLE_FINISHED", "SAFE_SHUTDOWN"]
    final = env.last()
    assert final["error"] is None
    assert final["process_details"]["final_outcome"] == "finished"
    assert final["process_details"]["drain_success"] is True
    assert final["process_details"]["physical_switch_off_confirmed"] is False


def test_fill_success_drain_not_confirmed_is_success(env):
    env.drain_confirmed = False
    assert run(env) == 0
    assert "drain" not in env.events
    assert env.states() == ["WATER_CYCLE_FINISHED", "SAFE_SHUTDOWN"]
    assert env.payloads[0]["process_details"]["drain_success"] is None
    assert env.last()["error"] is None


def test_drain_failure_is_aborted_not_finished(env):
    env.drain_result = BAD_DRAIN
    assert run(env) == 1
    assert "WATER_CYCLE_FINISHED" not in env.states()
    assert env.states() == ["WATER_CYCLE_ABORTED", "SAFE_SHUTDOWN"]
    aborted = env.payloads[0]
    assert aborted["error"] == "no_drain_progress"
    assert aborted["process_details"]["abort_phase"] == "drain"
    assert aborted["process_details"]["fill_success"] is True
    assert aborted["process_details"]["drain_success"] is False
    assert aborted["process_details"]["drain_stop_reason"] == "no_drain_progress"


def test_original_error_survives_in_final_status(env):
    env.drain_result = BAD_DRAIN
    run(env)
    final = env.last()
    assert final["process_state"] == "SAFE_SHUTDOWN"
    assert final["error"] == "no_drain_progress"
    assert final["process_details"]["final_outcome"] == "aborted"
    assert final["process_details"]["abort_phase"] == "drain"
    assert final["process_details"]["drain_stop_reason"] == "no_drain_progress"


def test_drain_cleanup_failed_reason_is_visible(env):
    env.drain_result = BAD_DRAIN_CLEANUP
    assert run(env) == 1
    assert env.payloads[0]["error"] == "drain_cleanup_failed"
    assert env.last()["error"] == "drain_cleanup_failed"


def test_fill_failure_never_starts_drain(env):
    env.fill_result = BAD_FILL
    assert run(env) == 1
    assert "drain" not in env.events
    assert env.states() == ["WATER_CYCLE_ABORTED", "SAFE_SHUTDOWN"]
    assert env.payloads[0]["process_details"]["abort_phase"] == "fill"
    assert env.last()["error"] == "fill_timeout"
    assert env.any_on() == []


# ---- frueher Abbruch / Start (W4) --------------------------------------------

def test_user_declines_before_start_is_exit_0_and_touches_nothing(env):
    env.answers = ["nein"]
    assert run(env) == 0
    assert env.constructed == []
    assert env.events == []


def test_invalid_hardware_confirmation_is_exit_1_and_touches_nothing(env):
    env.hardware_confirmed = False
    assert run(env) == 1
    assert env.constructed == []
    assert env.events == []


def test_settings_load_failure_is_exit_1(env, monkeypatch):
    def boom():
        raise ValueError("kaputt")

    monkeypatch.setattr(wc, "load_settings", boom)
    assert run(env) == 1
    assert env.constructed == []


def test_sensor_connection_error_is_exit_1_with_cleanup_and_status(env):
    env.reader_error = "connection refused"
    assert run(env) == 1
    assert "fill" not in env.events
    assert env.states() == ["SAFE_SHUTDOWN"]
    final = env.last()
    assert "connection refused" in final["error"]
    assert final["process_details"]["abort_phase"] == "startup"
    assert final["process_details"]["final_outcome"] == "startup_failed"
    assert "close_all" in env.events and "reader.close" in env.events


def test_missing_sensor_payload_is_exit_1(env):
    env.reader_wait = False
    assert run(env) == 1
    assert "fill" not in env.events
    assert env.last()["error"] == "no_sensor_payload_received"


def test_hardware_lock_failure_is_startup_failure_and_exit_1(env):
    env.actuator_ctor_exc = RuntimeError("Hardware wird bereits verwendet")
    assert run(env) == 1
    assert "fill" not in env.events
    # Kein Aktor-Objekt vorhanden: es gibt keinen Status (kein Aktorzustand behauptbar),
    # aber die vorhandenen Ressourcen werden geschlossen.
    assert env.payloads == []
    assert "reader.close" in env.events
    assert "logger.close" in env.events
    assert "publisher.close" in env.events


# ---- Exceptions in laufender Phase (W3/W7) -----------------------------------

def test_exception_during_fill_is_not_reported_as_start_failure(env, capsys):
    env.fill_exc = RuntimeError("pump gpio broke")
    assert run(env) == 1
    out = capsys.readouterr().out
    assert "konnte nicht gestartet werden" not in out
    assert "Phase 'fill'" in out
    assert env.states() == ["ERROR", "SAFE_SHUTDOWN"]
    assert env.payloads[0]["error"] == "pump gpio broke"
    assert env.last()["error"] == "pump gpio broke"
    assert env.last()["process_details"]["abort_phase"] == "fill"
    assert env.last()["process_details"]["final_outcome"] == "error"
    assert "drain" not in env.events


def test_exception_during_drain(env):
    env.drain_exc = RuntimeError("log write failed")
    assert run(env) == 1
    assert env.last()["process_details"]["abort_phase"] == "drain"
    assert env.last()["error"] == "log write failed"
    assert "WATER_CYCLE_FINISHED" not in env.states()


# ---- Cleanup (W3) --------------------------------------------------------------

ALL_STEPS = ["auto.stop", "safe_shutdown_all", "close_all", "reader.close", "logger.close", "publisher.close"]


def assert_all_cleanup_steps_ran(env):
    for step in ALL_STEPS:
        assert step in env.events, f"{step} missing in {env.events}"


def test_auto_circulation_stop_raises_does_not_skip_cleanup(env):
    env.auto_stop_exc = RuntimeError("gpio off failed")
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    final = env.last()
    assert final["process_state"] == "SAFE_SHUTDOWN_FAILED"
    assert "auto circulation stop failed" in final["error"]
    assert final["process_details"]["cleanup_ok"] is False
    assert final["process_details"]["final_outcome"] == "cleanup_failed"


def test_safe_shutdown_all_returns_errors(env):
    env.shutdown_result = ["Could not switch off actuator 'transfer_pump': boom"]
    assert run(env) == 1
    final = env.last()
    assert final["process_state"] == "SAFE_SHUTDOWN_FAILED"
    assert "transfer_pump" in final["error"]
    assert final["process_details"]["cleanup_errors"] == env.shutdown_result
    assert final["process_details"]["physical_switch_off_confirmed"] is False
    assert_all_cleanup_steps_ran(env)


def test_safe_shutdown_all_raises_close_all_still_runs(env):
    env.shutdown_exc = RuntimeError("shutdown exploded")
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    assert env.last()["process_state"] == "SAFE_SHUTDOWN_FAILED"
    assert "shutdown exploded" in env.last()["error"]


def test_close_all_errors_show_up_in_final_status(env):
    env.close_result = ["Could not close actuator 'drain_valve_0': busy"]
    assert run(env) == 1
    final = env.last()
    assert final["process_state"] == "SAFE_SHUTDOWN_FAILED"
    assert "drain_valve_0" in final["error"]
    assert_all_cleanup_steps_ran(env)


def test_close_all_raises_still_closes_resources(env):
    env.close_exc = RuntimeError("close exploded")
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    assert "close exploded" in env.last()["error"]


def test_publish_raises_does_not_block_cleanup_or_exit_code(env):
    env.publish_exc = RuntimeError("mqtt down")
    assert run(env) == 0  # Cleanup fehlerfrei, Prozess erfolgreich; Status nur Telemetrie
    assert_all_cleanup_steps_ran(env)
    assert env.payloads == []


def test_publish_raises_with_failed_drain_still_exit_1(env):
    env.publish_exc = RuntimeError("mqtt down")
    env.drain_result = BAD_DRAIN
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)


def test_publish_returns_false_is_counted_and_not_fatal(env, capsys):
    env.publish_result = False
    assert run(env) == 0
    assert_all_cleanup_steps_ran(env)
    assert "was not delivered" in capsys.readouterr().out
    # Der Endstatus weist die verlorenen Statusmeldungen aus (der Zaehler wird vor dem Senden gebildet).
    assert env.last()["process_details"]["status_publish_failures"] == 1


def test_publish_returns_false_with_failed_drain_keeps_exit_1(env):
    env.publish_result = False
    env.drain_result = BAD_DRAIN
    assert run(env) == 1


@pytest.mark.parametrize(
    "attribute, label",
    [
        ("reader_close_exc", "sensor reader close"),
        ("logger_close_exc", "logger close"),
        ("publisher_close_exc", "mqtt publisher close"),
    ],
)
def test_resource_close_failures_are_exit_1_and_do_not_skip_steps(env, attribute, label):
    setattr(env, attribute, OSError("close failed"))
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    if attribute != "publisher_close_exc":  # Publisher-Close kommt nach dem Endstatus
        assert label in env.last()["error"]
        assert env.last()["process_state"] == "SAFE_SHUTDOWN_FAILED"


def test_multiple_cleanup_errors_are_all_kept_and_deduplicated(env):
    env.auto_stop_exc = RuntimeError("auto broke")
    env.shutdown_result = ["Could not switch off actuator 'transfer_pump': x"]
    env.close_result = [
        "Could not switch off actuator 'transfer_pump': x",  # close_all wiederholt den Versuch
        "Could not close actuator 'drain_valve_0': y",
    ]
    env.reader_close_exc = OSError("reader broke")
    env.drain_result = BAD_DRAIN
    assert run(env) == 1
    errors = env.last()["process_details"]["cleanup_errors"]
    assert len(errors) == 4
    assert len(set(errors)) == 4
    assert env.last()["process_details"]["abort_phase"] == "drain"
    # Ursprungsfehler bleibt vorn, Cleanup-Fehler folgen.
    assert env.last()["error"].startswith("no_drain_progress; cleanup_failed: ")
    assert_all_cleanup_steps_ran(env)


def test_cleanup_failure_after_success_is_never_reported_as_success(env):
    env.shutdown_result = ["Could not switch off actuator 'mixer_refill_pump': x"]
    assert run(env) == 1
    assert env.last()["process_details"]["final_outcome"] == "cleanup_failed"


def test_no_actuator_start_after_abort_or_failure(env):
    env.fill_result = BAD_FILL
    run(env)
    assert env.any_on() == []
    env.events.clear()
    env.payloads.clear()
    env.fill_result = OK_FILL
    env.drain_result = BAD_DRAIN
    env.answers = ["ja"]
    run(env)
    assert env.any_on() == []
    # Nach dem Fehler kommt nur noch Cleanup, kein weiterer Phasenaufruf.
    assert env.events.index("drain") < env.events.index("auto.stop")
    assert env.events.count("fill") == 1 and env.events.count("drain") == 1


# ---- KeyboardInterrupt (W4) -------------------------------------------------------

def test_keyboard_interrupt_in_phase_is_130_and_cleans_up(env):
    env.fill_exc = KeyboardInterrupt()
    assert run(env) == 130
    assert_all_cleanup_steps_ran(env)
    assert env.states() == ["ERROR", "SAFE_SHUTDOWN"]
    assert env.last()["error"] == "KeyboardInterrupt"
    assert env.last()["process_details"]["final_outcome"] == "interrupted"


def test_keyboard_interrupt_reported_by_phase_result_is_130(env):
    env.fill_result = INTERRUPTED_FILL
    assert run(env) == 130
    assert env.last()["process_details"]["final_outcome"] == "interrupted"
    assert "drain" not in env.events


def test_keyboard_interrupt_at_prompt_is_130_and_touches_nothing(env, monkeypatch):
    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert run(env) == 130
    assert env.constructed == []


def test_cleanup_failure_overrides_interrupt_exit_code(env):
    env.fill_exc = KeyboardInterrupt()
    env.close_exc = RuntimeError("x")
    assert run(env) == 1


def test_second_interrupt_during_cleanup_does_not_skip_steps(env):
    env.auto_stop_exc = KeyboardInterrupt()
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)


# ---- Einstiegspunkte (isolierter Kindprozess, Fakes statt Hardware) --------------

_CHILD = textwrap.dedent(
    """
    import builtins, runpy, sys
    sys.path.insert(0, ".")
    scenario, entry = sys.argv[1], sys.argv[2]

    class Out:
        def __init__(self, name, **k): pass
        def on(self): raise SystemExit(99)   # darf nie aufgerufen werden
        def off(self): pass

    class Act:
        def __init__(self, active_low): self.o = {}
        def add(self, name, gpio_pin): self.o[name] = Out(name); return self.o[name]
        def status_payload(self): return {n: False for n in self.o}
        def safe_shutdown_all(self): return []
        def close_all(self): return ["close boom"] if scenario == "close_fail" else []

    class Pub:
        def publish_json(self, payload): return True
        def close(self): pass

    class Rd:
        def __init__(self, **k): pass
        def start(self): pass
        def get_error(self): return None
        def wait_for_first_snapshot(self, timeout_seconds): return True
        def close(self): pass

    class Lg:
        def __init__(self, process_name): pass
        def close(self): pass

    class Auto:
        def __init__(self, actuators, config): pass
        def stop(self): pass

    from process.common import PhaseResult
    import process.common, process.refill, process.drain, process.auto_circulation
    import hardware.actuator_manager, services.mqtt_publisher, services.mqtt_topic_reader, services.process_run_logger

    def fill(**k):
        if scenario == "interrupt":
            raise KeyboardInterrupt
        return PhaseResult(True, "target_reached_by_sensor", 1.0, 2.0, 1.0)

    def drain(**k):
        ok = scenario != "drain_fail"
        return PhaseResult(ok, "empty_by_sensor" if ok else "no_drain_progress", 2.0, 0.0, 2.0)

    process.common.load_settings = lambda: {"hardware_execution_enabled": True}
    process.common.require_hardware_confirmation = lambda s: True
    process.common.confirm_drain = lambda s: True
    process.refill.run_fill_phase = fill
    process.drain.run_drain_phase = drain
    process.auto_circulation.AutoCirculationController = Auto
    hardware.actuator_manager.ActuatorManager = Act
    services.mqtt_publisher.MqttPublisher = Pub
    services.mqtt_topic_reader.MqttTopicReader = Rd
    services.process_run_logger.ProcessRunLogger = Lg
    builtins.input = lambda prompt="": "ja"

    if entry == "module":
        runpy.run_module("process.water_cycle", run_name="__main__")
    else:
        runpy.run_path("main_refill_and_drain_test.py", run_name="__main__")
    """
)


def _run_entry(scenario: str, entry: str) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, scenario, entry],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 99, proc.stdout + proc.stderr
    return proc.returncode


@pytest.mark.parametrize("entry", ["module", "wrapper"])
@pytest.mark.parametrize(
    "scenario, expected",
    [("ok", 0), ("drain_fail", 1), ("close_fail", 1), ("interrupt", 130)],
)
def test_entry_points_propagate_exit_code(entry, scenario, expected):
    assert _run_entry(scenario, entry) == expected


# ---- Review-Korrekturen B1 / B2 ----------------------------------------------

def test_b2_finished_is_published_only_after_all_cleanup(env):
    assert run(env) == 0
    ev = env.events
    finished = ev.index("publish:WATER_CYCLE_FINISHED")
    for step in ("auto.stop", "safe_shutdown_all", "close_all", "reader.close", "logger.close"):
        assert ev.index(step) < finished, ev
    assert finished < ev.index("publish:SAFE_SHUTDOWN") < ev.index("publisher.close")
    assert env.states() == ["WATER_CYCLE_FINISHED", "SAFE_SHUTDOWN"]


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("shutdown_result", ["Could not switch off actuator 'transfer_pump': x"]),
        ("close_result", ["Could not close actuator 'drain_valve_0': y"]),
        ("shutdown_exc", RuntimeError("boom")),
        ("close_exc", RuntimeError("boom")),
        ("auto_stop_exc", RuntimeError("boom")),
        ("reader_close_exc", OSError("boom")),
        ("logger_close_exc", OSError("boom")),
    ],
)
def test_b2_cleanup_failure_never_emits_finished(env, attribute, value):
    setattr(env, attribute, value)
    assert run(env) == 1
    assert "WATER_CYCLE_FINISHED" not in env.states()
    assert "publish:WATER_CYCLE_FINISHED" not in env.events
    assert env.states() == ["SAFE_SHUTDOWN_FAILED"]
    assert env.last()["process_details"]["final_outcome"] == "cleanup_failed"


def test_b2_publisher_close_failure_is_exit_1_and_visible_locally(env, capsys):
    env.publisher_close_exc = OSError("close failed")
    assert run(env) == 1
    assert "mqtt publisher close failed" in capsys.readouterr().out


def test_b2_drain_failure_keeps_aborted_and_exit_1(env):
    env.drain_result = BAD_DRAIN
    assert run(env) == 1
    assert env.states() == ["WATER_CYCLE_ABORTED", "SAFE_SHUTDOWN"]
    assert "WATER_CYCLE_FINISHED" not in env.states()


def test_b1_interrupt_during_first_status_publish(env, capsys):
    env.drain_result = BAD_DRAIN
    env.publish_exc_on["WATER_CYCLE_ABORTED"] = KeyboardInterrupt()
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    assert "KeyboardInterrupt" in capsys.readouterr().out
    final = env.last()
    assert final["process_state"] == "SAFE_SHUTDOWN_FAILED"
    # Ursprungsfehler bleibt erhalten, der Interrupt kommt dazu.
    assert final["error"].startswith("no_drain_progress; cleanup_failed: ")
    assert "KeyboardInterrupt" in final["error"]


def test_b1_interrupt_during_final_status_publish(env, capsys):
    env.publish_exc_on["SAFE_SHUTDOWN"] = KeyboardInterrupt()
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    assert env.events[-1] == "publisher.close"
    assert "publish failed: KeyboardInterrupt" in capsys.readouterr().out


def test_b1_interrupt_during_finished_publish_ends_with_failed_status(env):
    env.publish_exc_on["WATER_CYCLE_FINISHED"] = KeyboardInterrupt()
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    assert env.last()["process_state"] == "SAFE_SHUTDOWN_FAILED"
    assert env.last()["process_details"]["final_outcome"] == "cleanup_failed"


def test_b1_interrupt_in_every_publish_still_runs_all_cleanup(env):
    env.publish_exc = KeyboardInterrupt()
    assert run(env) == 1
    assert_all_cleanup_steps_ran(env)
    assert env.events.index("close_all") < env.events.index("publisher.close")


# ---- B3: korrigierender Status nach Interrupt im finalen Publish --------------

class _InterruptOnce:
    """Raises KeyboardInterrupt on the first SAFE_SHUTDOWN publish only."""

    def __init__(self, env, always=False):
        self.env, self.always, self.fired = env, always, 0

    def arm(self, monkeypatch):
        env = self.env
        original = env.publish_exc_on

        class Dict(dict):
            def __contains__(d, key):
                return key in ("SAFE_SHUTDOWN", "SAFE_SHUTDOWN_FAILED") and (self.always or self.fired == 0 and key == "SAFE_SHUTDOWN")

            def __getitem__(d, key):
                self.fired += 1
                return KeyboardInterrupt()

        env.publish_exc_on = Dict()
        return original


def test_b3_interrupt_in_final_publish_triggers_one_corrective_failed_status(env, monkeypatch, capsys):
    _InterruptOnce(env).arm(monkeypatch)
    assert run(env) == 1
    # FINISHED ging raus, SAFE_SHUTDOWN wurde unterbrochen, danach genau EIN korrigierender Status.
    assert env.states() == ["WATER_CYCLE_FINISHED", "SAFE_SHUTDOWN_FAILED"]
    final = env.last()
    assert "publish failed: KeyboardInterrupt" in final["error"]
    assert final["process_details"]["cleanup_ok"] is False
    assert final["process_details"]["final_outcome"] == "cleanup_failed"
    assert final["process_details"]["physical_switch_off_confirmed"] is False
    assert env.events.count("publish:SAFE_SHUTDOWN") == 1
    assert env.events.count("publish:SAFE_SHUTDOWN_FAILED") == 1
    assert env.events[-1] == "publisher.close"
    assert_all_cleanup_steps_ran(env)


def test_b3_corrective_status_keeps_original_error(env, monkeypatch):
    env.drain_result = BAD_DRAIN
    _InterruptOnce(env).arm(monkeypatch)
    assert run(env) == 1
    final = env.last()
    assert final["process_state"] == "SAFE_SHUTDOWN_FAILED"
    assert final["error"].startswith("no_drain_progress; cleanup_failed: ")
    assert final["process_details"]["final_outcome"] == "aborted"
    assert final["process_details"]["abort_phase"] == "drain"


def test_b3_no_unbounded_retry_and_publisher_close_still_attempted(env, monkeypatch):
    _InterruptOnce(env, always=True).arm(monkeypatch)
    assert run(env) == 1
    publishes = [e for e in env.events if e.startswith("publish:SAFE_SHUTDOWN")]
    assert publishes == ["publish:SAFE_SHUTDOWN", "publish:SAFE_SHUTDOWN_FAILED"]
    assert env.events[-1] == "publisher.close"
    assert_all_cleanup_steps_ran(env)


def test_b3_publisher_close_failure_after_corrective_status_is_exit_1(env, monkeypatch):
    _InterruptOnce(env, always=True).arm(monkeypatch)
    env.publisher_close_exc = OSError("close failed")
    assert run(env) == 1
    assert env.events[-1] == "publisher.close"


def test_b3_delivery_false_does_not_trigger_retry(env):
    env.publish_result = False
    assert run(env) == 0
    assert env.events.count("publish:SAFE_SHUTDOWN") == 1
