from gpiozero import OutputDevice


class DigitalOutput:
    def __init__(self, name: str, gpio_pin: int, active_low: bool):
        self.name = name
        self.gpio_pin = gpio_pin
        self.is_active = False

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
        if self.is_active:
            print(f"[OFF] {self.name} | GPIO {self.gpio_pin}")
            self.device.off()
            self.is_active = False

    def close(self):
        self.off()
        self.device.close()