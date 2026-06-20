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

    try:
        async with Client(url=OPCUA_ENDPOINT) as opcua_client:
            print("[OK] OPC-UA verbunden.")
            print("[OK] MQTT verbunden.")
            print("Abbruch mit STRG + C")
            print()

            while True:
                timestamp = datetime.now().isoformat(timespec="seconds")

                errors = []

                mixer_raw = None
                ro_raw = None
                mixer_liters = None
                ro_liters = None

                ec_value = None
                ph_value = None
                water_temperature_value = None
                dissolved_oxygen_value = None

                # Mixing Tank Level
                try:
                    mixer_raw = await read_opcua_value(
                        opcua_client,
                        "mixer_level_raw_cel1"
                    )
                    mixer_liters = calculate_liters_from_percent(
                        mixer_raw,
                        MIXER_VOLUME_LITERS
                    )
                except Exception as exc:
                    errors.append(f"Mixer-Level read error: {exc}")

                # RO Tank Level
                try:
                    ro_raw = await read_opcua_value(
                        opcua_client,
                        "ro_level_raw_ibc1"
                    )
                    ro_liters = calculate_liters_from_percent(
                        ro_raw,
                        RO_VOLUME_LITERS
                    )
                except Exception as exc:
                    errors.append(f"RO-Level read error: {exc}")

                # EC
                try:
                    ec_value = await read_opcua_value(opcua_client, "ec")
                except Exception as exc:
                    errors.append(f"EC read error: {exc}")

                # pH
                try:
                    ph_value = await read_opcua_value(opcua_client, "ph")
                except Exception as exc:
                    errors.append(f"pH read error: {exc}")

                # Water Temperature
                try:
                    water_temperature_value = await read_opcua_value(
                        opcua_client,
                        "water_temperature"
                    )
                except Exception as exc:
                    errors.append(f"Water temperature read error: {exc}")

                # Dissolved Oxygen
                try:
                    dissolved_oxygen_value = await read_opcua_value(
                        opcua_client,
                        "dissolved_oxygen"
                    )
                except Exception as exc:
                    errors.append(f"Dissolved oxygen read error: {exc}")

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

                    # Sensor bridge does not control actuators.
                    # Real actuator status comes from cds/status/process.
                    "actuators": {
                        "mixer_refill_pump": None,
                        "drain_valve": None,
                        "supply_valve_6": None,
                        "transfer_pump": None
                    },

                    "error": error
                }

                mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))

                print(
                    f"{timestamp} | "
                    f"Mixer: {format_value(mixer_raw)}% / {mixer_liters} L | "
                    f"RO: {format_value(ro_raw)}% / {ro_liters} L | "
                    f"EC: {format_value(ec_value / 1000 if ec_value is not None else None)} mS/cm | "
                    f"pH: {format_value(ph_value)} | "
                    f"Temp: {format_value(water_temperature_value)} °C | "
                    f"DO: {format_value(dissolved_oxygen_value)} | "
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