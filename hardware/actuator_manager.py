import logging

from hardware.digital_output import DigitalOutput


logger = logging.getLogger(__name__)


class ActuatorManager:
    def __init__(self, active_low: bool):
        self.active_low = active_low
        self._outputs: dict[str, DigitalOutput] = {}
        self.last_errors: list[str] = []

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

    def _record_error(self, message: str) -> None:
        self.last_errors.append(message)
        self.last_errors = self.last_errors[-20:]
        logger.error(message)

    def safe_shutdown_all(self) -> list[str]:
        """
        Schaltet alle registrierten Aktoren aus.

        Wichtig:
        Diese Methode wirft bewusst keine Exception nach außen, damit ein Fehler
        an einem Ausgang nicht verhindert, dass weitere Aktoren ebenfalls
        ausgeschaltet werden. Fehler werden aber geloggt und gespeichert.
        """
        print("[SAFE] Alle registrierten Aktoren werden ausgeschaltet.")
        errors: list[str] = []

        for name, output in self._outputs.items():
            try:
                output.off()
            except Exception as exc:
                message = f"Could not switch off actuator '{name}': {exc}"
                errors.append(message)
                self._record_error(message)

        return errors

    def close_all(self) -> list[str]:
        """
        Schaltet alle Aktoren aus und gibt GPIO-Ressourcen frei.

        Gibt eine Fehlerliste zurück, statt Fehler still zu verschlucken.
        """
        errors: list[str] = []
        errors.extend(self.safe_shutdown_all())

        for name, output in self._outputs.items():
            try:
                output.close()
            except Exception as exc:
                message = f"Could not close actuator '{name}': {exc}"
                errors.append(message)
                self._record_error(message)

        return errors

    def get_last_errors(self) -> list[str]:
        return list(self.last_errors)
