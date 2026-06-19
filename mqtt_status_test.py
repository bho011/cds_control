import json
import time
import paho.mqtt.client as mqtt


MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_STATUS = "cds/status"


def main():
    client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, 60)

    counter = 0

    try:
        while True:
            payload = {
                "state": "TEST_RUNNING",
                "counter": counter,
                "mixer_refill_pump": False,
                "drain_valve": False,
                "supply_valve_6": False,
                "mixer_level_raw": None,
                "mixer_liters_calc": None,
                "ro_level_raw": None,
                "ro_liters_calc": None,
                "error": None
            }

            client.publish(MQTT_TOPIC_STATUS, json.dumps(payload))
            print(f"[MQTT] gesendet: {payload}")

            counter += 1
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[STOP] MQTT-Test beendet.")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()