import time
from gpiozero import OutputDevice

from gpio_config import OUTPUTS, ACTIVE_LOW, PULSE_SECONDS


class RelayOutput:
    def __init__(self, name: str, gpio_pin: int, active_low: bool):
        self.name = name
        self.gpio_pin = gpio_pin
        self.device = OutputDevice(
            pin=gpio_pin,
            active_high=not active_low,
            initial_value=False
        )

    def on(self):
        print(f"[TEST] {self.name} EIN | GPIO {self.gpio_pin}")
        self.device.on()

    def off(self):
        print(f"[TEST] {self.name} AUS | GPIO {self.gpio_pin}")
        self.device.off()

    def pulse(self, seconds: float):
        try:
            self.on()
            time.sleep(seconds)
        finally:
            self.off()


def show_outputs():
    print("\nVerfügbare Ausgänge:\n")
    for name, pin in OUTPUTS.items():
        print(f"{name:12} -> GPIO {pin}")
    print()


def main():
    print("CDS Hardware-Einzeltest")
    print("=======================")
    print(f"Active-Low: {ACTIVE_LOW}")
    print(f"Schaltdauer: {PULSE_SECONDS} Sekunden")

    show_outputs()

    selected = input("Welchen Ausgang testen? Beispiel: valve_0 oder contactor_0: ").strip()

    if selected not in OUTPUTS:
        print(f"[ERROR] Unbekannter Ausgang: {selected}")
        return

    gpio_pin = OUTPUTS[selected]

    print()
    print(f"Ausgewählt: {selected}")
    print(f"GPIO:       {gpio_pin}")
    print()
    print("ACHTUNG: Der Ausgang wird geschaltet.")
    #print("Prüfe, dass keine gefährliche Bewegung, Pumpe oder Chemikalienförderung entstehen kann.")
    print()

    confirm = input("Fortfahren? ja/nein: ").strip().lower()

    if confirm != "ja":
        print("Abgebrochen.")
        return

    relay = RelayOutput(
        name=selected,
        gpio_pin=gpio_pin,
        active_low=ACTIVE_LOW
    )

    relay.pulse(PULSE_SECONDS)

    print("[DONE] Test abgeschlossen.")


if __name__ == "__main__":
    main()
