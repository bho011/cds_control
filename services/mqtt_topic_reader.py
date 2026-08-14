"""
Vereinheitlichter MQTT-Einzeltopic-Reader.

Führt services/sensor_snapshot.py::SensorSnapshotReader und
nicegui_dashboard/mqtt_topic_reader.py::MqttTopicReader zusammen (siehe
Modularisierungs-Plan Phase 3) - die einzige bewusste, dokumentierte
Verhaltensänderung in diesem Refactor, kein reines Verschieben.

Zielvertrag (siehe Plan, Phase 3, Schritt 2):
- Bleibt identisch: _on_connect abonniert mit qos=0, start() verbindet und
  startet den paho-Loop, wait_for_first_snapshot(timeout) wie zuvor bei
  SensorSnapshotReader.
- Bewusst geändert (kleine, additive/defensive Deltas): get_latest() gibt
  immer eine Kopie zurück (nie mehr die interne Referenz), alle Zugriffe
  laufen unter self._lock, _error/get_error()/clear() sind immer
  verfügbar, _on_message/start() fangen breiter (except Exception).
- Neuer gemeinsamer Vertrag: get_latest(max_age_seconds=<Sentinel>) - drei
  unterscheidbare Fälle: Parameter weggelassen (self.max_age_seconds
  verwenden), explizit None (Altersprüfung deaktivieren, reproduziert
  SensorSnapshotReader.get_latest(None) exakt), explizit ein Float (dieser
  Wert statt self.max_age_seconds).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

# Eindeutiger Sentinel, unterscheidbar von None und jedem float - ein
# einfacher None-Default könnte "Parameter weggelassen" und "explizit None
# übergeben" nicht auseinanderhalten (siehe Modularisierungs-Plan Phase 3).
_UNSET = object()


class MqttTopicReader:
    """
    Read-only MQTT-Reader für ein einzelnes Topic.

    Verbindet sich, cacht die zuletzt empfangene Nachricht mit einem
    Alters-Timeout. Keine Publish-Funktion, keine Steuerbefehle.
    """

    def __init__(
        self,
        host: str,
        port: int,
        topic: str,
        max_age_seconds: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.topic = topic
        self.max_age_seconds = max_age_seconds

        self._latest_payload: dict[str, Any] | None = None
        self._latest_received_at: float | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
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

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        client.subscribe(self.topic, qos=0)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))

            with self._lock:
                self._latest_payload = payload
                self._latest_received_at = time.monotonic()
                self._error = None

            self._message_event.set()

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

    def wait_for_first_snapshot(self, timeout_seconds: float = 5.0) -> bool:
        return self._message_event.wait(timeout_seconds)

    def get_latest(self, max_age_seconds: float | None = _UNSET) -> dict[str, Any] | None:
        if max_age_seconds is _UNSET:
            max_age_seconds = self.max_age_seconds

        with self._lock:
            if self._latest_payload is None or self._latest_received_at is None:
                return None

            if max_age_seconds is not None:
                age = time.monotonic() - self._latest_received_at
                if age > max_age_seconds:
                    return None

            return dict(self._latest_payload)

    def get_error(self) -> str | None:
        with self._lock:
            return self._error

    def clear(self) -> None:
        with self._lock:
            self._latest_payload = None
            self._latest_received_at = None
            self._error = None

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
