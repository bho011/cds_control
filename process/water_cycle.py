from __future__ import annotations

from gpio_config import OUTPUTS, ACTIVE_LOW
from hardware.actuator_manager import ActuatorManager
from services.mqtt_publisher import MqttPublisher
from services.process_run_logger import ProcessRunLogger
from services.sensor_snapshot import SensorSnapshotReader

from .common import (
    load_settings,
    require_hardware_confirmation,
    confirm_sensor_pump,
    confirm_drain,
    publish_process_status,
)
from .refill import run_fill_phase
from .sensor_circulation import run_sensor_pump_phase
from .drain import run_drain_phase


def main():
    print("CDS Water Cycle Process")
    print("=======================")
    print()
    print("Ablauf:")
    print("1. RO Refill Pump füllt den Mixing Tank bis zum absoluten Zielwert.")
    print("2. Optional läuft die Sensorpumpe über contactor_2.")
    print("3. Sensorpumpenphase kann mit 'stop' + Enter beendet werden.")
    print("4. Danach wird gefragt, ob über Transferpumpe + Valve_0_Drain geleert wird.")
    print()
    print("Wichtig:")
    print("- RO-Leitung muss direkt in den Mixing Tank führen.")
    print("- Sensorpumpe hängt an contactor_2.")
    print("- Transferpumpe hängt an transfer_pump.")
    print("- Drain läuft über Valve_0_Drain.")
    print("- Keine Chemie, kein Routing zu Solution Tanks.")
    print()

    settings = load_settings()

    print("Settings:")
    print(f"- target_fill_total_liters: {settings.get('target_fill_total_liters')}")
    print(f"- max_fill_seconds: {settings.get('max_fill_seconds')}")
    print(f"- sensor_pump_seconds: {settings.get('sensor_pump_seconds')}")
    print(f"- transfer_pump_liters_per_minute: {settings.get('transfer_pump_liters_per_minute')}")
    print(f"- drain_timeout_buffer_seconds: {settings.get('drain_timeout_buffer_seconds')}")
    print(f"- empty_threshold_liters: {settings.get('empty_threshold_liters')}")
    print(f"- hardware_execution_enabled: {settings.get('hardware_execution_enabled')}")
    print()

    confirm = input("Fortfahren? ja/nein: ").strip().lower()
    if confirm != "ja":
        print("Abgebrochen.")
        return

    if not require_hardware_confirmation(settings):
        return

    sensor_reader = None
    mqtt_publisher = None
    logger = None
    actuators = None

    try:
        sensor_reader = SensorSnapshotReader()
        mqtt_publisher = MqttPublisher()
        logger = ProcessRunLogger(process_name="water_cycle")
        actuators = ActuatorManager(active_low=ACTIVE_LOW)

        sensor_reader.start()

        print("[INFO] Warte auf erstes Sensor-MQTT-Payload...")
        if not sensor_reader.wait_for_first_snapshot(timeout_seconds=5.0):
            print("[ERROR] Kein Sensor-Payload empfangen. Abbruch.")
            return

        print("[OK] Sensor-Payload empfangen.")

        actuators.add(
            name="mixer_refill_pump",
            gpio_pin=OUTPUTS["mixer_refill_pump"],
        )

        actuators.add(
            name="sensor_circulation_pump",
            gpio_pin=OUTPUTS["contactor_2"],
        )

        actuators.add(
            name="transfer_pump",
            gpio_pin=OUTPUTS["transfer_pump"],
        )

        actuators.add(
            name="drain_valve_0",
            gpio_pin=OUTPUTS["valve_0_drain"],
        )

        fill_result = run_fill_phase(
            settings=settings,
            sensor_reader=sensor_reader,
            actuators=actuators,
            mqtt_publisher=mqtt_publisher,
            logger=logger,
        )

        print()
        print(f"[FILL RESULT] success={fill_result.success}, reason={fill_result.stop_reason}")

        sensor_result = None

        if confirm_sensor_pump(settings):
            sensor_result = run_sensor_pump_phase(
                settings=settings,
                sensor_reader=sensor_reader,
                actuators=actuators,
                mqtt_publisher=mqtt_publisher,
                logger=logger,
            )

            print()
            print(f"[SENSOR RESULT] success={sensor_result.success}, reason={sensor_result.stop_reason}")

        drain_result = None

        if confirm_drain(settings):
            drain_result = run_drain_phase(
                settings=settings,
                sensor_reader=sensor_reader,
                actuators=actuators,
                mqtt_publisher=mqtt_publisher,
                logger=logger,
            )

            print()
            print(f"[DRAIN RESULT] success={drain_result.success}, reason={drain_result.stop_reason}")

        publish_process_status(
            mqtt_publisher,
            "WATER_CYCLE_FINISHED",
            actuators,
            details={
                "fill_success": fill_result.success,
                "fill_stop_reason": fill_result.stop_reason,
                "sensor_success": sensor_result.success if sensor_result else None,
                "sensor_stop_reason": sensor_result.stop_reason if sensor_result else None,
                "drain_success": drain_result.success if drain_result else None,
                "drain_stop_reason": drain_result.stop_reason if drain_result else None,
            },
        )

    except KeyboardInterrupt:
        print("\n[ABORT] KeyboardInterrupt.")

        if mqtt_publisher is not None and actuators is not None:
            publish_process_status(
                mqtt_publisher,
                "ERROR",
                actuators,
                error="KeyboardInterrupt",
            )

    finally:
        print("[SAFE] Shutdown all actuators.")

        if actuators is not None:
            actuators.safe_shutdown_all()

        if mqtt_publisher is not None and actuators is not None:
            publish_process_status(
                mqtt_publisher,
                "SAFE_SHUTDOWN",
                actuators,
            )

        if actuators is not None:
            actuators.close_all()

        if sensor_reader is not None:
            sensor_reader.close()

        if mqtt_publisher is not None:
            mqtt_publisher.close()

        if logger is not None:
            logger.close()

    print("[END] CDS water cycle process finished.")


if __name__ == "__main__":
    main()
