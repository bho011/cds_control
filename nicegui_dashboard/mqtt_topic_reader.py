import json
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt


class MqttTopicReader:
    """
    Einfacher MQTT-Read-only-Reader für ein einzelnes Topic.

    Wird im NiceGUI-Dashboard verwendet, um Statusdaten zu lesen.
    Keine Publish-Funktion.
    Keine Steuerbefehle.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        topic: str = "",
        max_age_seconds: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.topic = topic
        self.max_age_seconds = max_age_seconds

        self._latest_payload: dict[str, Any] | None = None
        self._latest_received_at: float | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

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

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        client.subscribe(self.topic, qos=0)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))

            with self._lock:
                self._latest_payload = payload
                self._latest_received_at = time.monotonic()
                self._error = None

        except Exception as exc:
            with self._lock:
                self._error = f"MQTT payload read error on {self.topic}: {exc}"

    def start(self) -> None:
        try:
            self.client.connect(self.host, self.port, 60)
            self.client.loop_start()
        except Exception as exc:
            with self._lock:
                self._error = f"MQTT connect failed on {self.topic}: {exc}"

    def get_latest(self) -> dict[str, Any] | None:
        with self._lock:
            if self._latest_payload is None or self._latest_received_at is None:
                return None

            age = time.monotonic() - self._latest_received_at

            if age > self.max_age_seconds:
                return None

            return dict(self._latest_payload)

    def get_error(self) -> str | None:
        with self._lock:
            return self._error

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
