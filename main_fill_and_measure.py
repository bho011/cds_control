import json
import time
from pathlib import Path

from gpio_config import OUTPUTS, ACTIVE_LOW
from hardware.actuator_manager import ActuatorManager
from services.mqtt_publisher import MqttPublisher
from services.sensor_snapshot import SensorSnapshotReader
from statemachine.fill_and_measure_state_machine import FillAndMeasureStateMachine
from services.process_run_logger import ProcessRunLogger


SETTINGS_PATH = Path("config/process_settings.json")


def load_settings() -> dict:
    with SETTINGS_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def publish_status(mqtt_publisher, state_machine, actuators):
    status = actuators.status_payload()

    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "python",
        "process_state": state_machine.state.name,
        "actuators": {
            "mixer_refill_pump": status.get("mixer_refill_pump", False),
            "supply_valve_6": status.get("supply_valve_6", False),
            "drain_valve_0": status.get("drain_valve_0", False),
            "transfer_pump": status.get("transfer_pump"),
            "mixing_circulation_pump": status.get("mixing_circulation_pump"),
            "sensor_circulation_pump": status.get("sensor_circulation_pump"),
        },
        "error": state_machine.error_message,
    }

    mqtt_publisher.publish_json(payload)

def log_process_step(logger, state_machine, actuators, sensor_reader):
    snapshot = sensor_reader.get_latest(max_age_seconds=5.0)
    actuator_status = actuators.status_payload()

    mixer_liters_filtered = None

    if snapshot is not None:
        try:
            mixer_liters_filtered = state_machine._filtered_mixer_liters(snapshot)
        except Exception:
            mixer_liters_filtered = None

    logger.write_step(
        state=state_machine.state.name,
        error=state_machine.error_message,
        snapshot=snapshot,
        actuator_status=actuator_status,
        mixer_liters_filtered=mixer_liters_filtered,
        start_mixer_liters=state_machine.start_mixer_liters,
        added_liters=state_machine.last_added_liters,
    )


def main():
    print("CDS Fill and Measure State Machine")
    print("==================================")

    settings = load_settings()

    print("Settings:")
    print(f"- fill_mode: {settings.get('fill_mode')}")
    print(f"- target_add_liters: {settings.get('target_add_liters')}")
    print(f"- target_total_liters: {settings.get('target_total_liters')}")
    print(f"- max_fill_seconds: {settings.get('max_fill_seconds')}")
    print(f"- mixing circulation enabled: {settings.get('enable_mixing_circulation')}")
    print(f"- sensor circulation enabled: {settings.get('enable_sensor_circulation')}")
    print()

    print("Wichtig:")
    print("- Keine Chemie")
    print("- Kein Routing")
    print("- Keine Transferpumpe")
    print("- Nur RO-Befüllung über Mixer Refill Pump + Valve 6")
    print("- Circulation-Pumpen nur, wenn in process_settings.json aktiviert")
    print()

    confirm = input("Fortfahren? ja/nein: ").strip().lower()

    if confirm != "ja":
        print("Abgebrochen.")
        return

    hardware_enabled = settings.get("hardware_execution_enabled", False)

    if not hardware_enabled:
        print("[BLOCKED] Hardware execution is disabled in config/process_settings.json.")
        print("[INFO] Set hardware_execution_enabled to true only when you are on site")
        print("[INFO] and the RO water path to the Mixing Tank is physically confirmed.")
        return

    required_text = settings.get("required_confirmation_text", "CONFIRM")
    print()
    print("Sicherheitsbestätigung erforderlich.")
    print(f"Zum Start exakt eingeben: {required_text}")
    confirmation_text = input("Bestätigung: ").strip()

    if confirmation_text != required_text:
        print("[BLOCKED] Sicherheitsbestätigung falsch. Abbruch.")
        return


    sensor_reader = SensorSnapshotReader()
    sensor_reader.start()

    print("[INFO] Warte auf erstes Sensor-MQTT-Payload...")

    if not sensor_reader.wait_for_first_snapshot(timeout_seconds=5.0):
        print("[ERROR] Kein Sensor-Payload empfangen. Abbruch.")
        sensor_reader.close()
        return

    print("[OK] Sensor-Payload empfangen.")


    actuators = ActuatorManager(active_low=ACTIVE_LOW)

    mixer_refill_pump = actuators.add(
        name="mixer_refill_pump",
        gpio_pin=OUTPUTS["mixer_refill_pump"],
    )

    supply_valve_6 = actuators.add(
        name="supply_valve_6",
        gpio_pin=OUTPUTS["test_supply_valve_6"],
    )

    
    mixing_circulation_pump = None
    sensor_circulation_pump = None

    if settings.get("enable_mixing_circulation", False):
        mixing_circulation_pump = actuators.add(
            name="mixing_circulation_pump",
            gpio_pin=OUTPUTS["contactor_2"],
        )

    if settings.get("enable_sensor_circulation", False):
        sensor_circulation_pump = actuators.add(
            name="sensor_circulation_pump",
            gpio_pin=OUTPUTS["contactor_3"],
        )

    mqtt_publisher = MqttPublisher()

    state_machine = FillAndMeasureStateMachine(
        mixer_refill_pump=mixer_refill_pump,
        ro_inlet_valve=supply_valve_6,
        mixing_circulation_pump=mixing_circulation_pump,
        sensor_circulation_pump=sensor_circulation_pump,
        get_sensor_snapshot=lambda: sensor_reader.get_latest(max_age_seconds=5.0),
        settings=settings,
    )

    process_logger = ProcessRunLogger(process_name="fill_and_measure")

    try:
        state_machine.start()
        publish_status(mqtt_publisher, state_machine, actuators)
        log_process_step(process_logger, state_machine, actuators, sensor_reader)

        while not state_machine.is_done:
            state_machine.update()
            publish_status(mqtt_publisher, state_machine, actuators)
            log_process_step(process_logger, state_machine, actuators, sensor_reader)
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n[ABORT] Abbruch durch Benutzer.")
        state_machine.error("KeyboardInterrupt")
        publish_status(mqtt_publisher, state_machine, actuators)
        log_process_step(process_logger, state_machine, actuators, sensor_reader)

    finally:
        state_machine.safe_shutdown()
        publish_status(mqtt_publisher, state_machine, actuators)
        log_process_step(process_logger, state_machine, actuators, sensor_reader)

        mqtt_publisher.close()
        actuators.close_all()
        sensor_reader.close()
        process_logger.close()

    print(f"[END] Endzustand: {state_machine.state.name}")


if __name__ == "__main__":
    main()
