from __future__ import annotations

from gpio_config import OUTPUTS, ACTIVE_LOW
from hardware.actuator_manager import ActuatorManager
from mqtt_sensor_bridge import MQTT_HOST, MQTT_PORT, MQTT_TOPIC
from services.mqtt_publisher import MqttPublisher
from services.mqtt_topic_reader import MqttTopicReader
from services.process_run_logger import ProcessRunLogger

from .auto_circulation import AutoCirculationController, load_auto_circulation_config
from .common import (
    load_settings,
    require_hardware_confirmation,
    confirm_drain,
    publish_process_status,
)
from .refill import run_fill_phase
from .drain import run_drain_phase
from .drain_safety import shutdown_errors


# Process exit codes (also used by the compatibility wrapper
# main_refill_and_drain_test.py and, via the subprocess return code, by main.py).
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130

STATE_SAFE_SHUTDOWN = "SAFE_SHUTDOWN"
STATE_SAFE_SHUTDOWN_FAILED = "SAFE_SHUTDOWN_FAILED"


class _Resources:
    """Everything the cleanup has to release; filled in as it gets created."""

    def __init__(self) -> None:
        self.sensor_reader = None
        self.mqtt_publisher = None
        self.logger = None
        self.actuators = None
        self.auto_circulation = None


class _CycleState:
    """
    Collected outcome of one water cycle. The exit code and the final MQTT
    status are derived from this ONLY AFTER the complete cleanup, so a
    cleanup error can never be hidden behind an earlier `return`.
    """

    def __init__(self) -> None:
        self.phase = "startup"  # startup | fill | drain_confirmation | drain
        # Fail closed: only a positively reached success sets these to success.
        self.outcome = "error"  # finished | aborted | interrupted | startup_failed | error
        self.exit_code = EXIT_FAILURE
        self.error: str | None = None
        self.abort_phase: str | None = None
        self.fill_result = None
        self.drain_result = None
        self.cleanup_errors: list[str] = []
        self.status_publish_failures = 0

    def add_cleanup_errors(self, errors) -> None:
        for error in errors:
            if error not in self.cleanup_errors:
                self.cleanup_errors.append(error)


class _RecordingPublisher:
    """
    Passes publish_json() through and remembers its return value.
    process.common.publish_process_status() discards it, but with
    fail_soft=True a False means the status was NOT delivered.
    """

    def __init__(self, publisher) -> None:
        self._publisher = publisher
        self.result = None

    def publish_json(self, payload):
        self.result = self._publisher.publish_json(payload)
        return self.result


def _result_details(state: _CycleState) -> dict:
    fill, drain = state.fill_result, state.drain_result
    return {
        "fill_success": fill.success if fill else None,
        "fill_stop_reason": fill.stop_reason if fill else None,
        "drain_success": drain.success if drain else None,
        "drain_stop_reason": drain.stop_reason if drain else None,
    }


def _publish(state: _CycleState, res: _Resources, phase: str, error=None, details=None) -> bool:
    """Publish a status. Never raises; a lost status never blocks cleanup or the exit code."""
    if res.mqtt_publisher is None or res.actuators is None:
        return False

    recorder = _RecordingPublisher(res.mqtt_publisher)
    try:
        publish_process_status(recorder, phase, res.actuators, error=error, details=details)
    except (Exception, KeyboardInterrupt) as exc:
        # A (second) Ctrl+C during a status publish must neither skip the
        # remaining cleanup nor go unnoticed: it is recorded as an error, which
        # also prevents a success exit code. Earlier errors are kept.
        state.status_publish_failures += 1
        message = f"status {phase} publish failed: {type(exc).__name__}: {exc}"
        print(f"[WARN] {message}")
        if isinstance(exc, KeyboardInterrupt):
            state.add_cleanup_errors([message])
        return False

    if recorder.result is not True:
        state.status_publish_failures += 1
        print(f"[WARN] Status {phase} was not delivered (publisher returned {recorder.result!r}).")
        return False

    return True


def _cleanup_step(state: _CycleState, label: str, step):
    """Run one cleanup step isolated; its exception is recorded, never propagated."""
    try:
        return step()
    except (Exception, KeyboardInterrupt) as exc:
        message = f"{label} failed: {type(exc).__name__}: {exc}"
        print(f"[ERROR] {message}")
        state.add_cleanup_errors([message])
        return None


def _cleanup_listed_step(state: _CycleState, label: str, step) -> None:
    """Like _cleanup_step, for ActuatorManager calls that RETURN an error list."""
    errors = shutdown_errors(_cleanup_step(state, label, step))
    if errors:
        print(f"[ERROR] {label} reported: {'; '.join(errors)}")
        state.add_cleanup_errors(errors)


def _cleanup(state: _CycleState, res: _Resources) -> None:
    """
    Every step runs, whatever happened before. Actuators are switched off and
    released first; the final status goes out afterwards so it can carry the
    complete cleanup result (including late close_all() errors).
    """
    print("[SAFE] Shutdown all actuators.")

    if res.auto_circulation is not None:
        _cleanup_step(state, "auto circulation stop", res.auto_circulation.stop)

    if res.actuators is not None:
        _cleanup_listed_step(state, "safe_shutdown_all", res.actuators.safe_shutdown_all)
        _cleanup_listed_step(state, "close_all", res.actuators.close_all)

    if res.sensor_reader is not None:
        _cleanup_step(state, "sensor reader close", res.sensor_reader.close)

    if res.logger is not None:
        _cleanup_step(state, "logger close", res.logger.close)

    # WATER_CYCLE_FINISHED is only sent once hardware and resource cleanup
    # succeeded; with any cleanup error only SAFE_SHUTDOWN_FAILED goes out.
    if state.outcome == "finished" and not state.cleanup_errors:
        _publish(state, res, "WATER_CYCLE_FINISHED", details=_result_details(state))

    _publish_final_status(state, res)

    if res.mqtt_publisher is not None:
        _cleanup_step(state, "mqtt publisher close", res.mqtt_publisher.close)


def _publish_final_status(state: _CycleState, res: _Resources) -> None:
    """
    Publish the final status. If the publish itself added a new error (an
    interrupt), ONE corrective attempt follows with the complete error list.
    No further retry; delivery is never guaranteed.
    """
    errors_before = len(state.cleanup_errors)
    _publish_final_status_once(state, res)

    if len(state.cleanup_errors) != errors_before:
        _publish_final_status_once(state, res)


def _publish_final_status_once(state: _CycleState, res: _Resources) -> None:
    cleanup_ok = not state.cleanup_errors
    final_outcome = state.outcome
    if not cleanup_ok and final_outcome == "finished":
        final_outcome = "cleanup_failed"

    error = state.error
    if not cleanup_ok:
        error = "; ".join(filter(None, [error, f"cleanup_failed: {'; '.join(state.cleanup_errors)}"]))

    details = {
        "final_outcome": final_outcome,
        "abort_phase": state.abort_phase,
        **_result_details(state),
        "cleanup_ok": cleanup_ok,
        "cleanup_errors": list(state.cleanup_errors),
        # Switch-off commands are software-side only; there is no feedback.
        "physical_switch_off_confirmed": False,
        "status_publish_failures": state.status_publish_failures,
    }

    _publish(
        state,
        res,
        STATE_SAFE_SHUTDOWN if cleanup_ok else STATE_SAFE_SHUTDOWN_FAILED,
        error=error,
        details=details,
    )


def _abort(state: _CycleState, res: _Resources, phase: str, result) -> None:
    """A phase returned an unsuccessful result: publish ABORTED and remember why."""
    interrupted = result.stop_reason == "keyboard_interrupt"
    state.outcome = "interrupted" if interrupted else "aborted"
    state.exit_code = EXIT_INTERRUPTED if interrupted else EXIT_FAILURE
    state.abort_phase = phase
    state.error = result.stop_reason

    _publish(
        state,
        res,
        "WATER_CYCLE_ABORTED",
        error=result.stop_reason,
        details={"abort_phase": phase, **_result_details(state)},
    )


def _execute(state: _CycleState, res: _Resources, settings: dict, auto_circulation_config) -> None:
    res.sensor_reader = MqttTopicReader(host=MQTT_HOST, port=MQTT_PORT, topic=MQTT_TOPIC)
    res.mqtt_publisher = MqttPublisher()
    res.logger = ProcessRunLogger(process_name="water_cycle")
    res.actuators = ActuatorManager(active_low=ACTIVE_LOW)
    actuators = res.actuators

    res.sensor_reader.start()
    # MqttTopicReader.start() sets get_error() on a connect failure
    # instead of raising (unlike the old SensorSnapshotReader.start()) -
    # check explicitly instead of relying on wait_for_first_snapshot()'s
    # generic timeout message below, see Modularisierungs-Plan Phase 3.
    if res.sensor_reader.get_error() is not None:
        state.outcome = "startup_failed"
        state.abort_phase = "startup"
        state.error = f"sensor_mqtt_connection_failed: {res.sensor_reader.get_error()}"
        print(f"[ERROR] Sensor-MQTT-Verbindung fehlgeschlagen: {res.sensor_reader.get_error()}")
        return

    print("[INFO] Warte auf erstes Sensor-MQTT-Payload...")
    if not res.sensor_reader.wait_for_first_snapshot(timeout_seconds=5.0):
        state.outcome = "startup_failed"
        state.abort_phase = "startup"
        state.error = "no_sensor_payload_received"
        print("[ERROR] Kein Sensor-Payload empfangen. Abbruch.")
        return

    print("[OK] Sensor-Payload empfangen.")

    actuators.add(
        name="mixer_refill_pump",
        gpio_pin=OUTPUTS["mixer_refill_pump"],
    )

    actuators.add(
        name="sensor_circulation_pump",
        gpio_pin=OUTPUTS["sensor_circulation_pump"],
    )

    actuators.add(
        name="mixing_circulation_pump",
        gpio_pin=OUTPUTS["mixing_circulation_pump"],
    )

    actuators.add(
        name="transfer_pump",
        gpio_pin=OUTPUTS["transfer_pump"],
    )

    actuators.add(
        name="drain_valve_0",
        gpio_pin=OUTPUTS["valve_0_drain"],
    )

    res.auto_circulation = AutoCirculationController(
        actuators=actuators,
        config=auto_circulation_config,
    )

    state.phase = "fill"
    state.fill_result = run_fill_phase(
        settings=settings,
        sensor_reader=res.sensor_reader,
        actuators=actuators,
        mqtt_publisher=res.mqtt_publisher,
        logger=res.logger,
        auto_circulation=res.auto_circulation,
    )
    fill_result = state.fill_result

    print()
    print(f"[FILL RESULT] success={fill_result.success}, reason={fill_result.stop_reason}")

    if not fill_result.success:
        print()
        print("[ABORT] Water-cycle wird nach fehlgeschlagener Fill-Phase beendet.")
        _abort(state, res, "fill", fill_result)
        return

    state.phase = "drain_confirmation"
    if confirm_drain(settings):
        state.phase = "drain"
        state.drain_result = run_drain_phase(
            settings=settings,
            sensor_reader=res.sensor_reader,
            actuators=actuators,
            mqtt_publisher=res.mqtt_publisher,
            logger=res.logger,
            auto_circulation=res.auto_circulation,
        )

        print()
        print(
            f"[DRAIN RESULT] success={state.drain_result.success}, "
            f"reason={state.drain_result.stop_reason}"
        )

        if not state.drain_result.success:
            print()
            print("[ABORT] Water-cycle nach fehlgeschlagener Drain-Phase nicht erfolgreich.")
            _abort(state, res, "drain", state.drain_result)
            return

    # An unconfirmed optional drain is not an error.
    state.outcome = "finished"
    state.exit_code = EXIT_OK


def main() -> int:
    print("CDS Water Cycle Process")
    print("=======================")
    print()
    print("Ablauf:")
    print("1. RO Refill Pump füllt den Mixing Tank bis zum absoluten Zielwert.")
    print("2. Ab auto_circulation_start_liters laufen die Zirkulationspumpen automatisch.")
    print("3. Unter auto_circulation_stop_liters werden die Zirkulationspumpen automatisch abgeschaltet.")
    print("4. Danach wird gefragt, ob über Transferpumpe + Valve_0_Drain geleert wird.")
    print()
    print("Wichtig:")
    print("- RO-Leitung muss direkt in den Mixing Tank führen.")
    print("- Sensor-Circulation-Pump wird automatisch füllstandsabhängig gesteuert.")
    print("- Mixing-Circulation-Pump wird automatisch füllstandsabhängig gesteuert.")
    print("- Transferpumpe hängt an transfer_pump.")
    print("- Drain läuft über Valve_0_Drain.")
    print("- Keine Chemie, kein Routing zu Solution Tanks.")
    print()

    # Before this point nothing hardware-related exists, so no cleanup is needed.
    try:
        try:
            settings = load_settings()
            auto_circulation_config = load_auto_circulation_config(settings)
        except Exception as exc:
            print(f"[BLOCKED] Settings konnten nicht geladen werden: {exc}")
            return EXIT_FAILURE

        print("Settings:")
        print(f"- target_fill_total_liters: {settings.get('target_fill_total_liters')}")
        print(f"- max_fill_seconds: {settings.get('max_fill_seconds')}")
        print(f"- transfer_pump_liters_per_minute: {settings.get('transfer_pump_liters_per_minute')}")
        print(f"- drain_timeout_buffer_seconds: {settings.get('drain_timeout_buffer_seconds')}")
        print(f"- empty_threshold_liters: {settings.get('empty_threshold_liters')}")
        print(f"- hardware_execution_enabled: {settings.get('hardware_execution_enabled')}")
        print(f"- auto_circulation_enabled: {auto_circulation_config.enabled}")
        print(f"- auto_circulation_start_liters: {auto_circulation_config.start_liters}")
        print(f"- auto_circulation_stop_liters: {auto_circulation_config.stop_liters}")
        print(f"- auto_circulation_outputs: {auto_circulation_config.outputs}")
        print()

        confirm = input("Fortfahren? ja/nein: ").strip().lower()
        if confirm != "ja":
            print("Abgebrochen.")
            return EXIT_OK

        if not require_hardware_confirmation(settings):
            return EXIT_FAILURE
    except KeyboardInterrupt:
        print("\n[ABORT] KeyboardInterrupt.")
        return EXIT_INTERRUPTED

    state = _CycleState()
    res = _Resources()

    try:
        _execute(state, res, settings, auto_circulation_config)

    except KeyboardInterrupt:
        print("\n[ABORT] KeyboardInterrupt.")
        state.outcome = "interrupted"
        state.exit_code = EXIT_INTERRUPTED
        state.abort_phase = state.phase
        state.error = "KeyboardInterrupt"
        _publish(state, res, "ERROR", error="KeyboardInterrupt")

    except Exception as exc:
        state.error = str(exc)
        state.abort_phase = state.phase

        if state.phase == "startup":
            # Covers e.g. ActuatorManager() failing to acquire the cross-process
            # hardware lock because another process is already using it - give a
            # clear message instead of a raw traceback.
            state.outcome = "startup_failed"
            print(f"\n[BLOCKED] Water-cycle konnte nicht gestartet werden: {exc}")
        else:
            state.outcome = "error"
            print(f"\n[ERROR] Water-cycle Fehler in Phase '{state.phase}': {type(exc).__name__}: {exc}")

        _publish(state, res, "ERROR", error=str(exc))

    finally:
        _cleanup(state, res)

    # The exit code is decided only now, after the complete cleanup.
    exit_code = state.exit_code
    if state.cleanup_errors:
        exit_code = EXIT_FAILURE

    if exit_code == EXIT_OK:
        print("[END] CDS water cycle process finished.")
    else:
        print(f"[END] CDS water cycle process ended unsuccessfully (exit code {exit_code}).")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
