"""Prüft MQTT-Erreichbarkeit und die Struktur des zuletzt veröffentlichten Sensor-Payloads."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

import paho.mqtt.client as mqtt

from services.system_config import get_mqtt_config

from .report import PreflightReport

_SYSTEM_MQTT_CONFIG = get_mqtt_config()

MQTT_HOST = str(_SYSTEM_MQTT_CONFIG["host"])
MQTT_PORT = int(_SYSTEM_MQTT_CONFIG["port"])
MQTT_TOPIC = str(_SYSTEM_MQTT_CONFIG["sensor_topic"])
MQTT_QOS = int(_SYSTEM_MQTT_CONFIG["qos"])  # ungenutzt, aus dem Original übernommen (reiner Verschieb)

MQTT_PAYLOAD_TIMEOUT_SECONDS = 5


def check_mqtt_tcp(report: PreflightReport):
    try:
        with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=3):
            report.ok("MQTT TCP connection", f"{MQTT_HOST}:{MQTT_PORT}")
    except OSError as exc:
        report.fail("MQTT TCP connection", str(exc))


def validate_sensor_payload(report: PreflightReport, payload: dict[str, Any]):
    required_top_level = [
        "timestamp",
        "source",
        "state",
        "mixer",
        "ro",
        "water_values",
        "error",
    ]

    missing = [key for key in required_top_level if key not in payload]

    if missing:
        report.fail("MQTT sensor payload structure", f"Missing keys: {missing}")
        return

    report.ok("MQTT sensor payload structure", "Required top-level keys found")

    if payload.get("state") != "SENSOR_BRIDGE_RUNNING":
        report.warn("MQTT sensor bridge state", f"state={payload.get('state')}")
    else:
        report.ok("MQTT sensor bridge state", "SENSOR_BRIDGE_RUNNING")

    if payload.get("error"):
        report.warn("MQTT sensor payload error", str(payload.get("error")))
    else:
        report.ok("MQTT sensor payload error", "None")

    ro = payload.get("ro") or {}
    mixer = payload.get("mixer") or {}
    water_values = payload.get("water_values") or {}

    if ro.get("level_percent") is None:
        report.fail("RO level value", "ro.level_percent is None")
    else:
        report.ok("RO level value", f"{ro.get('level_percent')} %")

    if mixer.get("level_percent") is None:
        report.warn(
            "Mixer level value",
            "mixer.level_percent is None or not readable. Check calibration/OPC-UA state."
        )
    else:
        report.ok("Mixer level value", f"{mixer.get('level_percent')} %")

    if water_values.get("ec_ms_cm") is None:
        report.warn("EC value", "water_values.ec_ms_cm is None")
    else:
        report.ok("EC value", f"{water_values.get('ec_ms_cm')} mS/cm")

    if water_values.get("ph") is None:
        report.warn("pH value", "water_values.ph is None")
    else:
        report.ok("pH value", str(water_values.get("ph")))

    if water_values.get("water_temperature") is None:
        report.warn("Water temperature", "water_values.water_temperature is None")
    else:
        report.ok("Water temperature", f"{water_values.get('water_temperature')} °C")

    if water_values.get("dissolved_oxygen") is None:
        report.warn("Dissolved oxygen", "water_values.dissolved_oxygen is None")
    else:
        report.ok("Dissolved oxygen", str(water_values.get("dissolved_oxygen")))


def check_latest_mqtt_sensor_payload(report: PreflightReport):
    message_received = threading.Event()
    received_payload: dict[str, Any] | None = None
    receive_error: str | None = None

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(MQTT_TOPIC, qos=0)

    def on_message(client, userdata, message):
        nonlocal received_payload, receive_error

        try:
            received_payload = json.loads(message.payload.decode("utf-8"))
        except Exception as exc:
            receive_error = str(exc)

        message_received.set()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, 60)
        client.loop_start()

        if not message_received.wait(MQTT_PAYLOAD_TIMEOUT_SECONDS):
            report.fail(
                "MQTT sensor payload",
                f"No message on {MQTT_TOPIC} within {MQTT_PAYLOAD_TIMEOUT_SECONDS} seconds"
            )
            return

        if receive_error:
            report.fail("MQTT sensor payload JSON", receive_error)
            return

        if received_payload is None:
            report.fail("MQTT sensor payload", "Message received but payload is empty")
            return

        report.ok("MQTT sensor payload", f"Received message on {MQTT_TOPIC}")
        validate_sensor_payload(report, received_payload)

    except Exception as exc:
        report.fail("MQTT sensor payload", str(exc))

    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
