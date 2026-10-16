from gpiozero import OutputDevice


class DigitalOutput:
    def __init__(self, name: str, gpio_pin: int, active_low: bool):
        self.name = name
        self.gpio_pin = gpio_pin
        self.is_active = False
        self._closed = False

        self.device = OutputDevice(
            pin=gpio_pin,
            active_high=not active_low,
            initial_value=False
        )

    def on(self):
        if not self.is_active:
            print(f"[ON ] {self.name} | GPIO {self.gpio_pin}")
            self.device.on()
            self.is_active = True

    def off(self):
        # Kein elektrischer Readback: is_active ist nur ein Software-Flag und darf
        # das Abschalten nie ueberspringen. Das Flag faellt erst nach erfolgreichem
        # device.off(); eine Ausnahme bleibt erhalten, ein erneuter Versuch ist moeglich.
        if self._closed:
            return  # Handle bereits erfolgreich freigegeben
        print(f"[OFF] {self.name} | GPIO {self.gpio_pin}")
        self.device.off()
        self.is_active = False

    def close(self):
        # Schlaegt off() fehl, ist der physische Schaltzustand nicht nachgewiesen:
        # das Handle wird dann NICHT freigegeben (Ausnahme aus off() geht nach oben).
        if self._closed:
            return
        self.off()
        self.device.close()
        self._closed = True
