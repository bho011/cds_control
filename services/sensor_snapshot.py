import json
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

from mqtt_sensor_bridge import MQTT_HOST, MQTT_PORT, MQTT_TOPIC


class SensorSnapshotReader:
    def __init__(
        self,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        topic: str = MQTT_TOPIC,
    ):
        self.host = host
        self.port = port
        self.topic = topic

        self._latest_payload: dict[str, Any] | None = None
        self._latest_received_at: float | None = None
        self._message_event = threading.Event()

        self.client = self._create_client()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _create_client(self) -> mqtt.Client:
        try:
            return mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )
        except (AttributeError, TypeError):
            return mqtt.Client()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(self.topic, qos=0)

    def _on_message(self, client, userdata, message):
        try:
            self._latest_payload = json.loads(message.payload.decode("utf-8"))
            self._latest_received_at = time.monotonic()
            self._message_event.set()
        except json.JSONDecodeError:
            pass

    def start(self):
        self.client.connect(self.host, self.port, 60)
        self.client.loop_start()

    def wait_for_first_snapshot(self, timeout_seconds: float = 5.0) -> bool:
        return self._message_event.wait(timeout_seconds)

    def get_latest(self, max_age_seconds: float | None = 5.0) -> dict[str, Any] | None:
        if self._latest_payload is None or self._latest_received_at is None:
            return None

        if max_age_seconds is not None:
            age = time.monotonic() - self._latest_received_at
            if age > max_age_seconds:
                return None

        return self._latest_payload

    def close(self):
        try:
            self.client.loop_stop()
        finally:
            self.client.disconnect()
