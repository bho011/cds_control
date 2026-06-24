from hardware.digital_output import DigitalOutput


class ActuatorManager:
    def __init__(self, active_low: bool):
        self.active_low = active_low
        self._outputs: dict[str, DigitalOutput] = {}

    def add(self, name: str, gpio_pin: int) -> DigitalOutput:
        if name in self._outputs:
            raise ValueError(f"Actuator already registered: {name}")

        output = DigitalOutput(
            name=name,
            gpio_pin=gpio_pin,
            active_low=self.active_low,
        )

        self._outputs[name] = output
        return output

    def get(self, name: str) -> DigitalOutput:
        try:
            return self._outputs[name]
        except KeyError as exc:
            raise KeyError(f"Unknown actuator: {name}") from exc

    def status_payload(self) -> dict[str, bool]:
        return {
            name: output.is_active
            for name, output in self._outputs.items()
        }

    def safe_shutdown_all(self):
        print("[SAFE] Alle registrierten Aktoren werden ausgeschaltet.")

        for name, output in self._outputs.items():
            try:
                output.off()
            except Exception as exc:
                print(f"[SAFE WARN] Could not switch off {name}: {exc}")

    def close_all(self):
        self.safe_shutdown_all()

        for name, output in self._outputs.items():
            try:
                output.close()
            except Exception as exc:
                print(f"[CLOSE WARN] Could not close {name}: {exc}")
