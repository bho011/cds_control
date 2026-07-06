import time
from gpiozero import OutputDevice

from gpio_config import OUTPUTS, ACTIVE_LOW


TEST_DURATION_SECONDS = 5.0


class DigitalOutput:
    def __init__(self, name: str, gpio_pin: int, active_low: bool):
        self.name = name
        self.gpio_pin = gpio_pin
        self.device = OutputDevice(
            pin=gpio_pin,
            active_high=not active_low,
            initial_value=False
        )

    def on(self):
        print(f"[ON ] {self.name} | GPIO {self.gpio_pin}")
        self.device.on()

    def off(self):
        print(f"[OFF] {self.name} | GPIO {self.gpio_pin}")
        self.device.off()

    def close(self):
        self.device.close()


def wait_with_countdown(seconds: float):
    remaining = int(seconds)

    while remaining > 0:
        print(f"[RUN] Noch {remaining} Sekunden...")
        time.sleep(1)
        remaining -= 1


def main():
    print("CDS Safe Water Test")
    print("===================")
    print("Ablauf:")
    print("1. Valve 6 öffnen")
    print("2. Drain Valve 1 öffnen")
    print("3. Mixer Refill Pump starten")
    print(f"4. {TEST_DURATION_SECONDS} Sekunden laufen lassen")
    print("5. Pumpe stoppen")
    print("6. Ventile schließen")
    print()

    print("Validierte Aktoren:")
    print(f"- mixer_refill_pump   -> GPIO {OUTPUTS['mixer_refill_pump']}")
    print(f"- test_supply_valve_6 -> GPIO {OUTPUTS['test_supply_valve_6']}")
    print(f"- test_drain_valve_1  -> GPIO {OUTPUTS['test_drain_valve_1']}")
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

    supply_valve_6 = DigitalOutput(
        name="test_supply_valve_6",
        gpio_pin=OUTPUTS["test_supply_valve_6"],
        active_low=ACTIVE_LOW
    )

    drain_valve_1 = DigitalOutput(
        name="test_drain_valve_1",
        gpio_pin=OUTPUTS["test_drain_valve_1"],
        active_low=ACTIVE_LOW
    )

    try:
        print("[STEP] Ventile öffnen")
        supply_valve_6.on()
        drain_valve_1.on()

        time.sleep(1)

        print("[STEP] Mixer Refill Pump starten")
        mixer_refill_pump.on()

        wait_with_countdown(TEST_DURATION_SECONDS)

    except KeyboardInterrupt:
        print("\n[ABORT] Abbruch durch Benutzer")

    finally:
        print("[SAFE STOP] Pumpe stoppen")
        mixer_refill_pump.off()

        time.sleep(0.5)

        print("[SAFE STOP] Ventile schließen")
        supply_valve_6.off()
        drain_valve_1.off()

        mixer_refill_pump.close()
        supply_valve_6.close()
        drain_valve_1.close()

    print("[DONE] Safe Water Test abgeschlossen.")


if __name__ == "__main__":
    main()