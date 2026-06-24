import json
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt


class MqttPublisher:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        topic: str = "cds/status/process",
        qos: int = 1,
        publish_timeout_seconds: float = 2.0,
        fail_soft: bool = True,
    ):
        self.host = host
        self.port = port
        self.topic = topic
        self.qos = qos
        self.publish_timeout_seconds = publish_timeout_seconds
        self.fail_soft = fail_soft

        self.client = self._create_client()
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        self._connect()

    
    def _create_client(self) -> mqtt.Client:
        """
        Uses the newer paho-mqtt callback API if available.
        Falls back to the older constructor if the installed version does not support it.
        """
        try:
            return mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )
        except (AttributeError, TypeError):
            return mqtt.Client()

    def _connect(self):
        result = self.client.connect(self.host, self.port, 60)

        if result != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(
                f"MQTT connect failed: host={self.host}, port={self.port}, rc={result}"
            )

        self.client.loop_start()

    def publish_json(self, payload: dict[str, Any]) -> bool:
        """
        Publishes a JSON payload and waits briefly until paho confirms publication.

        Returns:
            True  = publish was confirmed
            False = publish failed, but fail_soft=True prevented an exception
        """
        try:
            payload_json = json.dumps(payload, separators=(",", ":"))

            publish_info = self.client.publish(
                self.topic,
                payload_json,
                qos=self.qos,
                retain=False,
            )

            if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(
                    f"MQTT publish failed: topic={self.topic}, rc={publish_info.rc}"
                )

            publish_info.wait_for_publish(timeout=self.publish_timeout_seconds)

            if not publish_info.is_published():
                raise TimeoutError(
                    f"MQTT publish timeout after {self.publish_timeout_seconds}s: "
                    f"topic={self.topic}"
                )

            return True

        except Exception as exc:
            if self.fail_soft:
                print(f"[MQTT WARN] {exc}")
                return False

            raise

    def publish_process_status(
        self,
        state: str,
        mixer_refill_pump,
        supply_valve,
        drain_valve,
        error: str | None = None,
    ) -> bool:
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "python",
            "process_state": state,
            "actuators": {
                "mixer_refill_pump": mixer_refill_pump.is_active,
                "supply_valve_6": supply_valve.is_active,
                "drain_valve_0": drain_valve.is_active,
                "transfer_pump": None,
            },
            "error": error,
        }

        return self.publish_json(payload)

    def close(self):
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()