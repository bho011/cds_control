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


def main():
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

    try:
        settings = load_settings()
    except Exception as exc:
        print(f"[BLOCKED] Settings konnten nicht geladen werden: {exc}")
        return

    auto_circulation_config = load_auto_circulation_config(settings)

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
        return

    if not require_hardware_confirmation(settings):
        return

    sensor_reader = None
    mqtt_publisher = None
    logger = None
    actuators = None
    auto_circulation = None

    try:
        sensor_reader = MqttTopicReader(host=MQTT_HOST, port=MQTT_PORT, topic=MQTT_TOPIC)
        mqtt_publisher = MqttPublisher()
        logger = ProcessRunLogger(process_name="water_cycle")
        actuators = ActuatorManager(active_low=ACTIVE_LOW)

        sensor_reader.start()
        # MqttTopicReader.start() sets get_error() on a connect failure
        # instead of raising (unlike the old SensorSnapshotReader.start()) -
        # check explicitly instead of relying on wait_for_first_snapshot()'s
        # generic timeout message below, see Modularisierungs-Plan Phase 3.
        if sensor_reader.get_error() is not None:
            print(f"[ERROR] Sensor-MQTT-Verbindung fehlgeschlagen: {sensor_reader.get_error()}")
            return

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

        auto_circulation = AutoCirculationController(
            actuators=actuators,
            config=auto_circulation_config,
        )

        fill_result = run_fill_phase(
            settings=settings,
            sensor_reader=sensor_reader,
            actuators=actuators,
            mqtt_publisher=mqtt_publisher,
            logger=logger,
            auto_circulation=auto_circulation,
        )

        print()
        print(f"[FILL RESULT] success={fill_result.success}, reason={fill_result.stop_reason}")

        if not fill_result.success:
            print()
            print("[ABORT] Water-cycle wird nach fehlgeschlagener Fill-Phase beendet.")

            publish_process_status(
                mqtt_publisher,
                "WATER_CYCLE_ABORTED",
                actuators,
                error=fill_result.stop_reason,
                details={
                    "abort_phase": "fill",
                    "fill_success": fill_result.success,
                    "fill_stop_reason": fill_result.stop_reason,
                },
            )
            return

        drain_result = None

        if confirm_drain(settings):
            drain_result = run_drain_phase(
                settings=settings,
                sensor_reader=sensor_reader,
                actuators=actuators,
                mqtt_publisher=mqtt_publisher,
                logger=logger,
                auto_circulation=auto_circulation,
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

    except Exception as exc:
        # Covers e.g. ActuatorManager() failing to acquire the cross-process
        # hardware lock because another process is already using it - give a
        # clear message instead of a raw traceback. Nothing unsafe is left
        # behind either way: `actuators` stays None if construction itself
        # failed, so the finally block below has nothing to clean up.
        print(f"\n[BLOCKED] Water-cycle konnte nicht gestartet werden: {exc}")

        if mqtt_publisher is not None and actuators is not None:
            publish_process_status(
                mqtt_publisher,
                "ERROR",
                actuators,
                error=str(exc),
            )

    finally:
        print("[SAFE] Shutdown all actuators.")

        if auto_circulation is not None:
            auto_circulation.stop()

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
