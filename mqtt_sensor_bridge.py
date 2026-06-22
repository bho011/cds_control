import asyncio
import json
from datetime import datetime

import paho.mqtt.client as mqtt
from asyncua import Client


OPCUA_ENDPOINT = "opc.tcp://10.8.0.62:14840"

NODE_IDS = {
    # Tank level values
    "mixer_level_raw_cel1": "ns=4;s=Values.CEL1.PV_WaterLevel",
    "ro_level_raw_ibc1": "ns=4;s=Values.IBC1.PV_WaterLevel",

    # Water quality values
    "ec": "ns=4;s=Values.IBC1.PV_WaterConductivity",
    "ph": "ns=4;s=Values.IBC1.PV_WaterpH",
    "water_temperature": "ns=4;s=Values.IBC1.PV_WaterTemperature",
    "dissolved_oxygen": "ns=4;s=Values.IBC1.PV_DissolvedOxygen",
}

MIXER_VOLUME_LITERS = 200
RO_VOLUME_LITERS = 1300

READ_INTERVAL_SECONDS = 1.0
RECONNECT_SECONDS = 10.0

MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "cds/status/sensors"


def calculate_liters_from_percent(value: float, max_volume_liters: float) -> int:
    return round((value / 100) * max_volume_liters)


def create_mqtt_client() -> mqtt.Client:
    client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    return client


async def read_opcua_value(opcua_client: Client, node_key: str):
    node_id = NODE_IDS[node_key]
    node = opcua_client.get_node(node_id)
    return await node.read_value()


def format_value(value, decimals: int = 3) -> str:
    if value is None:
        return "None"

    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


async def main():
    print("CDS MQTT Sensor Bridge")
    print("=====================")
    print(f"OPC-UA Endpoint: {OPCUA_ENDPOINT}")
    print(f"MQTT Broker:     {MQTT_HOST}:{MQTT_PORT}")
    print(f"MQTT Topic:      {MQTT_TOPIC}")
    print()

    mqtt_client = create_mqtt_client()
    last_error = None

    try:
        while True:
            try:
                print(f"[INFO] Verbinde mit OPC-UA: {OPCUA_ENDPOINT}")

                async with Client(url=OPCUA_ENDPOINT) as opcua_client:
                    print("[OK] OPC-UA verbunden.")
                    print("[OK] MQTT verbunden.")
                    print("Abbruch mit STRG + C")
                    print()

                    while True:
                        timestamp = datetime.now().isoformat(timespec="seconds")
                        errors = []
                        connection_broken = False

                        mixer_raw = None
                        ro_raw = None
                        mixer_liters = None
                        ro_liters = None

                        ec_value = None
                        ph_value = None
                        water_temperature_value = None
                        dissolved_oxygen_value = None

                        async def safe_read(node_key: str, label: str):
                            nonlocal connection_broken

                            try:
                                return await read_opcua_value(opcua_client, node_key)
                            except Exception as exc:
                                error_text = str(exc)
                                errors.append(f"{label} read error: {error_text}")

                                lower_error = error_text.lower()

                                if (
                                    "disconnect" in lower_error
                                    or "not connected" in lower_error
                                    or "connection" in lower_error
                                    or "client is disconnected" in lower_error
                                ):
                                    connection_broken = True

                                return None

                        mixer_raw = await safe_read(
                            "mixer_level_raw_cel1",
                            "Mixer-Level"
                        )

                        if mixer_raw is not None:
                            mixer_liters = calculate_liters_from_percent(
                                mixer_raw,
                                MIXER_VOLUME_LITERS
                            )

                        ro_raw = await safe_read(
                            "ro_level_raw_ibc1",
                            "RO-Level"
                        )

                        if ro_raw is not None:
                            ro_liters = calculate_liters_from_percent(
                                ro_raw,
                                RO_VOLUME_LITERS
                            )

                        ec_value = await safe_read("ec", "EC")
                        ph_value = await safe_read("ph", "pH")

                        water_temperature_value = await safe_read(
                            "water_temperature",
                            "Water temperature"
                        )

                        dissolved_oxygen_value = await safe_read(
                            "dissolved_oxygen",
                            "Dissolved oxygen"
                        )

                        error = " | ".join(errors) if errors else None

                        payload = {
                            "timestamp": timestamp,
                            "source": "python",
                            "state": "SENSOR_BRIDGE_RUNNING",

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

                            "water_values": {
                                "ec_raw": ec_value,
                                "ec_ms_cm": ec_value / 1000 if ec_value is not None else None,
                                "ph": ph_value,
                                "water_temperature": water_temperature_value,
                                "dissolved_oxygen": dissolved_oxygen_value,
                                "calibration_status": "not_final_validated"
                            },

                            "actuators": {
                                "mixer_refill_pump": None,
                                "drain_valve": None,
                                "supply_valve_6": None,
                                "transfer_pump": None
                            },

                            "error": error
                        }

                        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))

                        if error != last_error:
                            if error:
                                print(f"{timestamp} | [ERROR] {error}")
                            elif last_error:
                                print(f"{timestamp} | [OK] sensor_bridge error cleared.")

                            last_error = error

                        if connection_broken:
                            raise ConnectionError(error or "OPC-UA connection broken")

                        await asyncio.sleep(READ_INTERVAL_SECONDS)

            except Exception as exc:
                timestamp = datetime.now().isoformat(timespec="seconds")
                reconnect_error = f"OPC-UA reconnect required: {exc}"

                if reconnect_error != last_error:
                    print(f"{timestamp} | [ERROR] {reconnect_error}")
                    last_error = reconnect_error

                await asyncio.sleep(RECONNECT_SECONDS)

    finally:
        mqtt_client.disconnect()
        print("[STOP] MQTT getrennt.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOP] Bridge durch Benutzer beendet.")