import asyncio
import json
from datetime import datetime

import paho.mqtt.client as mqtt
from asyncua import Client


OPCUA_ENDPOINT = "opc.tcp://10.8.0.62:14840"

NODE_IDS = {
    "mixer_level_raw_cel1": "ns=4;s=Values.CEL1.PV_WaterLevel",
    "ro_level_raw_ibc1": "ns=4;s=Values.IBC1.PV_WaterLevel",
}

MIXER_VOLUME_LITERS = 200
RO_VOLUME_LITERS = 1300

READ_INTERVAL_SECONDS = 1.0

MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "cds/status/sensors"


def calculate_liters_from_percent(value: float, max_volume_liters: float) -> int:
    return round((value / 100) * max_volume_liters)


def create_mqtt_client() -> mqtt.Client:
    client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    return client


async def main():
    print("CDS MQTT Sensor Bridge")
    print("=====================")
    print(f"OPC-UA Endpoint: {OPCUA_ENDPOINT}")
    print(f"MQTT Broker:     {MQTT_HOST}:{MQTT_PORT}")
    print(f"MQTT Topic:      {MQTT_TOPIC}")
    print()

    mqtt_client = create_mqtt_client()

    try:
        async with Client(url=OPCUA_ENDPOINT) as opcua_client:
            print("[OK] OPC-UA verbunden.")
            print("[OK] MQTT verbunden.")
            print("Abbruch mit STRG + C")
            print()

            while True:
                timestamp = datetime.now().isoformat(timespec="seconds")

                mixer_raw = None
                ro_raw = None
                mixer_liters = None
                ro_liters = None
                error = None

                try:
                    mixer_node = opcua_client.get_node(NODE_IDS["mixer_level_raw_cel1"])
                    mixer_raw = await mixer_node.read_value()
                    mixer_liters = calculate_liters_from_percent(
                        mixer_raw,
                        MIXER_VOLUME_LITERS
                    )
                except Exception as exc:
                    error = f"Mixer-Level read error: {exc}"

                try:
                    ro_node = opcua_client.get_node(NODE_IDS["ro_level_raw_ibc1"])
                    ro_raw = await ro_node.read_value()
                    ro_liters = calculate_liters_from_percent(
                        ro_raw,
                        RO_VOLUME_LITERS
                    )
                except Exception as exc:
                    error = f"RO-Level read error: {exc}"

                payload = {
                    "timestamp": timestamp,
                    "source": "python",
                    "mixer": {
                        "node_id": NODE_IDS["mixer_level_raw_cel1"],
                        "level_percent": mixer_raw,
                        "volume_liters_calc": mixer_liters,
                        "configured_max_liters": MIXER_VOLUME_LITERS,
                        "calibration_status": "not_final_calibrated"
                    },
                    "ro": {
                        "node_id": NODE_IDS["ro_level_raw_ibc1"],
                        "level_percent": ro_raw,
                        "volume_liters_calc": ro_liters,
                        "configured_max_liters": RO_VOLUME_LITERS,
                        "calibration_status": "plausible_validated"
                    },
                    "error": error
                }

                mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))

                print(
                    f"{timestamp} | "
                    f"Mixer: {mixer_raw:.3f}% / {mixer_liters} L | "
                    f"RO: {ro_raw:.3f}% / {ro_liters} L | "
                    f"Error: {error}"
                )

                await asyncio.sleep(READ_INTERVAL_SECONDS)

    finally:
        mqtt_client.disconnect()
        print("[STOP] MQTT getrennt.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOP] Bridge durch Benutzer beendet.")
