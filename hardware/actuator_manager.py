import fcntl
import logging
from pathlib import Path

from hardware.digital_output import DigitalOutput


logger = logging.getLogger(__name__)

# Absolute path: the six entry points that each construct an ActuatorManager
# (dashboard fill-and-measure/manual-drain/tank-cleaning, water_cycle.py,
# calibration_mixing_tank.py, main_safe_drain.py) can run from different
# working directories, so a cwd-relative path would silently break the
# cross-process guarantee for some of them.
LOCK_PATH = Path(__file__).resolve().parent.parent / "logs" / ".hardware.lock"


class HardwareLockError(RuntimeError):
    """Raised when another process already holds the exclusive hardware lock."""


class ActuatorManager:
    def __init__(self, active_low: bool):
        self.active_low = active_low
        self._outputs: dict[str, DigitalOutput] = {}
        self.last_errors: list[str] = []
        self._lock_file = self._acquire_hardware_lock()

    @staticmethod
    def _acquire_hardware_lock():
        """
        Exclusive, non-blocking cross-process lock: only one ActuatorManager
        anywhere (dashboard threads or a separate CLI script/process) may
        hold real GPIO outputs at a time. fcntl.flock() locks the *open file
        description*, not the process, so two threads in the same process
        that each open() this path independently also correctly conflict
        with each other - this is a backstop in addition to (not instead of)
        the in-process ProcessController locking.
        """
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_file = LOCK_PATH.open("w")

        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise HardwareLockError(
                "Hardware wird bereits von einem anderen Prozess verwendet "
                f"(Sperrdatei: {LOCK_PATH}). Warte, bis der andere Prozess "
                "beendet ist, bevor du erneut startest."
            ) from exc

        return lock_file

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

        # Release the cross-process hardware lock last, after every GPIO
        # output has actually been switched off and closed. Idempotent:
        # self._lock_file is set to None below, so a second close_all() call
        # (a documented-safe pattern used across the codebase) skips this.
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            except Exception as exc:
                message = f"Could not release hardware lock: {exc}"
                errors.append(message)
                self._record_error(message)
            finally:
                try:
                    self._lock_file.close()
                except Exception:
                    pass
                self._lock_file = None

        return errors

    def get_last_errors(self) -> list[str]:
        return list(self.last_errors)
