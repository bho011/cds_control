import asyncio
import logging
from typing import Any

from nicegui_dashboard.process_controller import ProcessController
from preflight_check import run_preflight_report
from services.mqtt_topic_reader import MqttTopicReader
from services.system_config import get_mqtt_config


logger = logging.getLogger(__name__)

_SYSTEM_MQTT_CONFIG = get_mqtt_config()


class CdsController:
    """
    NiceGUI-Controller für das CDS-Dashboard.

    Aktueller Stand:
    - Sensorwerte lesen
    - Prozessstatus per MQTT lesen
    - Aktorstatus per MQTT lesen
    - Fill-and-Measure-Prozess vorbereitet
    """

    def __init__(self) -> None:
        self.controller_errors: list[str] = []

        self.sensor_reader = MqttTopicReader(
            host=_SYSTEM_MQTT_CONFIG["host"],
            port=_SYSTEM_MQTT_CONFIG["port"],
            topic=_SYSTEM_MQTT_CONFIG["sensor_topic"],
            max_age_seconds=5.0,
        )
        self.sensor_started = False
        self.sensor_error: str | None = None

        self.process_reader = MqttTopicReader(
            host=_SYSTEM_MQTT_CONFIG["host"],
            port=_SYSTEM_MQTT_CONFIG["port"],
            topic=_SYSTEM_MQTT_CONFIG["process_topic"],
            max_age_seconds=30.0,
        )
        self.process_reader_started = False
        self.process_reader_error: str | None = None

        self.process_controller = ProcessController(
            get_sensor_snapshot=lambda: self.sensor_reader.get_latest(
                max_age_seconds=5.0
            )
        )

        self._start_sensor_reader()
        self._start_process_reader()

    def _record_error(self, message: str, exc: Exception | None = None) -> None:
        self.controller_errors.append(message)
        self.controller_errors = self.controller_errors[-20:]

        if exc is None:
            logger.error(message)
        else:
            logger.exception(message)

    def _record_warning(self, message: str, exc: Exception | None = None) -> None:
        self.controller_errors.append(message)
        self.controller_errors = self.controller_errors[-20:]

        if exc is None:
            logger.warning(message)
        else:
            logger.warning("%s: %s", message, exc)

    def _start_sensor_reader(self) -> None:
        try:
            self.sensor_reader.start()
            self.sensor_started = True
            self.sensor_error = None
            print("[OK] SensorSnapshotReader started for NiceGUI dashboard.")
        except Exception as exc:
            self.sensor_started = False
            self.sensor_error = f"SensorSnapshotReader start failed: {exc}"
            self._record_error(self.sensor_error, exc)

    def _start_process_reader(self) -> None:
        try:
            self.process_reader.start()
            self.process_reader_started = True
            self.process_reader_error = None
            print("[OK] Process MQTT reader started for NiceGUI dashboard.")
        except Exception as exc:
            self.process_reader_started = False
            self.process_reader_error = f"Process MQTT reader start failed: {exc}"
            self._record_error(self.process_reader_error, exc)

    def start_fill_and_measure(self, confirmation_text: str) -> dict[str, Any]:
        return self.process_controller.start_fill_and_measure(confirmation_text)

    def acknowledge_error(self) -> dict[str, Any]:
        result = self.process_controller.acknowledge_error()

        if result.get("success"):
            try:
                self.process_reader.clear()
            except Exception as exc:
                self._record_warning("Process MQTT reader clear failed", exc)

        return result

    async def emergency_stop(self) -> dict[str, Any]:
        return await self.process_controller.emergency_stop()

    def start_manual_drain_jog(self) -> dict[str, Any]:
        return self.process_controller.start_manual_drain_jog()

    def stop_manual_drain_jog(self) -> dict[str, Any]:
        return self.process_controller.stop_manual_drain_jog()

    def start_tank_cleaning(self, confirmation_text: str) -> dict[str, Any]:
        return self.process_controller.start_tank_cleaning(confirmation_text)

    def stop_tank_cleaning(self) -> dict[str, Any]:
        return self.process_controller.stop_tank_cleaning()

    def start_prime(self, pumps: dict[str, list[str]]) -> dict[str, Any]:
        return self.process_controller.start_prime(pumps)

    async def stop_prime(self) -> dict[str, Any]:
        return await self.process_controller.stop_prime()

    async def run_preflight_check(self) -> dict[str, Any]:
        """
        Runs preflight_check.py's full check sequence in a worker thread, so
        the blocking parts (systemctl, socket, MQTT wait) don't stall the
        NiceGUI event loop for every connected dashboard client.
        """
        try:
            report = await asyncio.to_thread(run_preflight_report)
        except Exception as exc:
            self._record_error("Preflight check failed to run", exc)
            return {"status": "ERROR", "lines": [f"[ERROR] Preflight check failed to run: {exc}"]}

        if report.has_failures:
            status = "FAIL"
        elif report.has_warnings:
            status = "WARN"
        else:
            status = "OK"

        return {"status": status, "lines": report.format_report_lines()}

    def get_process_control_status(self) -> dict[str, Any]:
        status = self.process_controller.get_status()
        status["controller_errors"] = list(self.controller_errors)
        status["last_controller_error"] = (
            self.controller_errors[-1] if self.controller_errors else None
        )
        return status

    def get_sensor_status(self) -> dict[str, Any]:
        snapshot = None

        if self.sensor_started:
            try:
                snapshot = self.sensor_reader.get_latest(max_age_seconds=5.0)
            except Exception as exc:
                self.sensor_error = f"Sensor read failed: {exc}"
                self._record_warning(self.sensor_error, exc)

        # MqttTopicReader.start() sets get_error() on a connect failure
        # instead of raising (see Modularisierungs-Plan Phase 3) - checked
        # here the same way get_process_status() already checks
        # process_reader.get_error(), so a connect failure stays visible
        # instead of silently degrading to "no snapshot, no reason given".
        reader_error = self.sensor_reader.get_error()

        return {
            "sensor_started": self.sensor_started,
            "sensor_error": reader_error or self.sensor_error,
            "controller_errors": list(self.controller_errors),
            "last_controller_error": (
                self.controller_errors[-1] if self.controller_errors else None
            ),
            "snapshot_available": snapshot is not None,
            "snapshot": snapshot,
            "timestamp": self._get_nested(snapshot, "timestamp"),
            "bridge_state": self._get_nested(snapshot, "state"),
            "bridge_error": self._get_nested(snapshot, "error"),

            "ro_level_percent": self._get_nested(snapshot, "ro", "level_percent"),
            "ro_liters": self._get_nested(snapshot, "ro", "volume_liters_calc"),
            "ro_max_liters": self._get_nested(snapshot, "ro", "configured_max_liters"),

            "mixer_level_percent": self._get_nested(snapshot, "mixer", "level_percent"),
            "mixer_liters": self._get_nested(snapshot, "mixer", "volume_liters_calc"),
            "mixer_max_liters": self._get_nested(snapshot, "mixer", "configured_max_liters"),

            "ph": self._get_nested(snapshot, "water_values", "ph"),
            "ec_ms_cm": self._get_nested(snapshot, "water_values", "ec_ms_cm"),
            "water_temperature": self._get_nested(snapshot, "water_values", "water_temperature"),
            "dissolved_oxygen": self._get_nested(snapshot, "water_values", "dissolved_oxygen"),
        }

    def get_process_status(self) -> dict[str, Any]:
        payload = self.process_reader.get_latest()
        reader_error = self.process_reader.get_error()

        return {
            "reader_started": self.process_reader_started,
            "reader_error": reader_error or self.process_reader_error,
            "controller_errors": list(self.controller_errors),
            "last_controller_error": (
                self.controller_errors[-1] if self.controller_errors else None
            ),
            "payload_available": payload is not None,
            "timestamp": self._get_nested(payload, "timestamp"),
            "source": self._get_nested(payload, "source"),
            "process_state": self._get_nested(payload, "process_state"),
            "error": self._get_nested(payload, "error"),

            "mixer_refill_pump": self._get_nested(
                payload, "actuators", "mixer_refill_pump"
            ),
            "supply_valve_6": self._get_nested(
                payload, "actuators", "supply_valve_6"
            ),
            "drain_valve_0": self._get_nested(
                payload, "actuators", "drain_valve_0"
            ),
            "transfer_pump": self._get_nested(
                payload, "actuators", "transfer_pump"
            ),
            "mixing_circulation_pump": self._get_nested(
                payload, "actuators", "mixing_circulation_pump"
            ),
            "sensor_circulation_pump": self._get_nested(
                payload, "actuators", "sensor_circulation_pump"
            ),
            "valve_1": self._get_nested(payload, "actuators", "valve_1"),
            "valve_2": self._get_nested(payload, "actuators", "valve_2"),
            "valve_3": self._get_nested(payload, "actuators", "valve_3"),
            "valve_4": self._get_nested(payload, "actuators", "valve_4"),
            "valve_5": self._get_nested(payload, "actuators", "valve_5"),
        }

    def close(self) -> None:
        try:
            self.sensor_reader.close()
            print("[OK] SensorSnapshotReader closed.")
        except Exception as exc:
            self._record_warning("SensorSnapshotReader close failed", exc)

        try:
            self.process_reader.close()
            print("[OK] Process MQTT reader closed.")
        except Exception as exc:
            self._record_warning("Process MQTT reader close failed", exc)

        try:
            result = self.process_controller.shutdown(timeout_seconds=5.0)
            if result.get("success"):
                print("[OK] ProcessController shutdown completed.")
            else:
                self._record_warning(
                    f"ProcessController shutdown incomplete: {result.get('message')}"
                )
        except Exception as exc:
            self._record_error("ProcessController shutdown failed", exc)

    @staticmethod
    def _get_nested(data: dict[str, Any] | None, *keys: str) -> Any:
        if data is None:
            return None

        current: Any = data

        for key in keys:
            if not isinstance(current, dict):
                return None

            current = current.get(key)

            if current is None:
                return None

        return current
