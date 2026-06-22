import time

from services.mqtt_publisher import MqttPublisher
from gpio_config import OUTPUTS, ACTIVE_LOW
from hardware.digital_output import DigitalOutput
from statemachine.water_test_state_machine import WaterTestStateMachine


def publish_status(mqtt_publisher, state_machine, mixer_refill_pump, supply_valve, drain_valve):
    print(
        f"[MQTT DEBUG] state={state_machine.state.name} | "
        f"pump={mixer_refill_pump.is_active} | "
        f"supply={supply_valve.is_active} | "
        f"drain={drain_valve.is_active}"
    )

    mqtt_publisher.publish_process_status(
        state=state_machine.state.name,
        mixer_refill_pump=mixer_refill_pump,
        supply_valve=supply_valve,
        drain_valve=drain_valve,
        error=state_machine.error_message
    )

# Hauptfunktion für den Test der Wasser-Entleerungs- und Befüllungslogik.
def main():
    print("CDS Water Test State Machine")
    print("============================")
    print("Ablauf:")
    print("1. Valve 6 öffnen")
    print("2. Drain Valve 0 öffnen")
    print("3. Mixer Refill Pump starten")
    print("4. 5 Sekunden laufen lassen")
    print("5. Pumpe stoppen")
    print("6. Ventile schließen")
    print()

    print("Verwendete Ausgänge:")
    print(f"mixer_refill_pump   -> GPIO {OUTPUTS['mixer_refill_pump']}")
    print(f"test_supply_valve_6 -> GPIO {OUTPUTS['test_supply_valve_6']}")
    print(f"drain_valve_0       -> GPIO {OUTPUTS['valve_0_drain']}")
    print()

    
    confirm = input("Fortfahren? ja/nein: ").strip().lower()

    if confirm != "ja":
        print("Abgebrochen.")
        return

    mixer_refill_pump = DigitalOutput(
        name="mixer_refill_pump",
        gpio_pin=OUTPUTS["mixer_refill_pump"],
        active_low=ACTIVE_LOW
    )

    supply_valve = DigitalOutput(
        name="test_supply_valve_6",
        gpio_pin=OUTPUTS["test_supply_valve_6"],
        active_low=ACTIVE_LOW
    )

    drain_valve = DigitalOutput(
        name="drain_valve_0",
        gpio_pin=OUTPUTS["valve_0_drain"],
        active_low=ACTIVE_LOW
    )

    state_machine = WaterTestStateMachine(
        mixer_refill_pump=mixer_refill_pump,
        supply_valve=supply_valve,
        drain_valve=drain_valve,
        run_seconds=5.0,
        valve_settle_seconds=1.0
    )

    mqtt_publisher = MqttPublisher()

    try:
        state_machine.start()

        publish_status(
            mqtt_publisher,
            state_machine,
            mixer_refill_pump,
            supply_valve,
            drain_valve
        )

        while not state_machine.is_done:
            state_machine.update()

            publish_status(
                mqtt_publisher,
                state_machine,
                mixer_refill_pump,
                supply_valve,
                drain_valve
            )

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n[ABORT] Abbruch durch Benutzer.")
        state_machine.error("KeyboardInterrupt")

        publish_status(
            mqtt_publisher,
            state_machine,
            mixer_refill_pump,
            supply_valve,
            drain_valve
        )

    finally:
        state_machine.safe_shutdown()

        publish_status(
            mqtt_publisher,
            state_machine,
            mixer_refill_pump,
            supply_valve,
            drain_valve
        )

        mqtt_publisher.close()

        mixer_refill_pump.close()
        supply_valve.close()
        drain_valve.close()

    print(f"[END] Endzustand: {state_machine.state.name}")


if __name__ == "__main__":
    main()