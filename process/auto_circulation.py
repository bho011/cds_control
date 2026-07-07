"""
Automatic circulation control for the water-cycle process.

The circulation pumps are enabled only when the Mixing Tank contains enough
water and disabled again before the pumps can run dry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoCirculationConfig:
    enabled: bool
    start_liters: float
    stop_liters: float
    outputs: list[str]


class AutoCirculationController:
    def __init__(self, actuators, config: AutoCirculationConfig):
        self.actuators = actuators
        self.config = config
        self.is_running = False

        if self.config.start_liters <= self.config.stop_liters:
            raise ValueError(
                "auto_circulation_start_liters must be greater than "
                "auto_circulation_stop_liters"
            )

    def update(self, current_liters: float | None) -> None:
        if not self.config.enabled:
            self.stop()
            return

        if current_liters is None:
            self.stop()
            return

        if not self.is_running and current_liters >= self.config.start_liters:
            self._set_outputs(True)
            self.is_running = True
            print(
                "[AUTO-CIRCULATION] ON "
                f"(mixer={current_liters:.2f} L, "
                f"start={self.config.start_liters:.2f} L)"
            )
            return

        if self.is_running and current_liters <= self.config.stop_liters:
            print(
                "[AUTO-CIRCULATION] OFF "
                f"(mixer={current_liters:.2f} L, "
                f"stop={self.config.stop_liters:.2f} L)"
            )
            self.stop()

    def stop(self) -> None:
        if self.is_running:
            print("[AUTO-CIRCULATION] Safe stop requested.")

        self._set_outputs(False)
        self.is_running = False

    def _set_outputs(self, enabled: bool) -> None:
        for output_name in self.config.outputs:
            output = self.actuators.get(output_name)

            if enabled:
                output.on()
            else:
                output.off()


def load_auto_circulation_config(settings: dict) -> AutoCirculationConfig:
    outputs = settings.get("auto_circulation_outputs", [])

    if not isinstance(outputs, list):
        raise ValueError("auto_circulation_outputs must be a list")

    return AutoCirculationConfig(
        enabled=bool(settings.get("auto_circulation_enabled", False)),
        start_liters=float(settings.get("auto_circulation_start_liters", 30.0)),
        stop_liters=float(settings.get("auto_circulation_stop_liters", 25.0)),
        outputs=[str(output_name) for output_name in outputs],
    )
