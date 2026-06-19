import json
from datetime import datetime

import paho.mqtt.client as mqtt


class MqttPublisher:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        topic: str = "cds/status/process"
    ):
        self.host = host
        self.port = port
        self.topic = topic
        self.client = mqtt.Client()
        self.client.connect(self.host, self.port, 60)

    def publish_process_status(
        self,
        state: str,
        mixer_refill_pump,
        supply_valve,
        drain_valve,
        error: str | None = None
    ):
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "python",
            "process_state": state,
            "actuators": {
                "mixer_refill_pump": mixer_refill_pump.is_active,
                "supply_valve_6": supply_valve.is_active,
                "drain_valve_0": drain_valve.is_active,
                "transfer_pump": None
            },
            "error": error
        }

        self.client.publish(self.topic, json.dumps(payload))

    def close(self):
        self.client.disconnect()