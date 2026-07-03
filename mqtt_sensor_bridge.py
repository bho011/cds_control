import asyncio
import json
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt
from asyncua import Client


OPCUA_ENDPOINT = "opc.tcp://10.8.0.62:14840"

NODE_IDS = {
    "mixer_level_raw_cel1": "ns=4;s=Values.CEL1.PV_WaterLevel",
    "ro_level_raw_ibc1": "ns=4;s=Values.IBC1.PV_WaterLevel",

    "ec": "ns=4;s=Values.IBC1.PV_WaterConductivity",
    "ph": "ns=4;s=Values.IBC1.PV_WaterpH",
    "water_temperature": "ns=4;s=Values.IBC1.PV_WaterTemperature",
    "dissolved_oxygen": "ns=4;s=Values.IBC1.PV_DissolvedOxygen",
}

MIXER_VOLUME_LITERS = 200.0
RO_VOLUME_LITERS = 1300.0

# Temporäre Kalibrierung Mixing-Tank-Sensor.
# Beobachtung: Sensor zeigt aktuell ungefähr Faktor 10 zu hoch.
# Beispiel: Rohwert 2.0 L -> kalibrierter Wert 0.2 L
MIXER_SENSOR_LITER_FACTOR = 0.175
MIXER_SENSOR_LITER_OFFSET = 0.0
MIXER_SENSOR_CALIBRATION_STATUS = "temporary_factor_0_1"

READ_INTERVAL_SECONDS = 1.0

MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "cds/status/sensors"


def calculate_liters_from_percent(value: float, max_volume_liters: float) -> float:
    return (float(value) / 100.0) * float(max_volume_liters)


def apply_mixer_liter_calibration(raw_liters: float) -> float:
    calibrated_liters = (
        float(raw_liters) * MIXER_SENSOR_LITER_FACTOR
    ) + MIXER_SENSOR_LITER_OFFSET

    return max(0.0, calibrated_liters)


def round_or_none(value: Any, digits: int = 2):
    if value is None:
        return None

    return round(float(value), digits)


def create_mqtt_client() -> mqtt.Client:
    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
    except (AttributeError, TypeError):
        client = mqtt.Client()

    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    return client


async def main():
    print("CDS MQTT Sensor Bridge")
    print("=====================")
    print(f"OPC-UA Endpoint: {OPCUA_ENDPOINT}")
    print(f"MQTT Broker:     {MQTT_HOST}:{MQTT_PORT}")
    print(f"MQTT Topic:      {MQTT_TOPIC}")
    print(f"Mixer factor:    {MIXER_SENSOR_LITER_FACTOR}")
    print()

    mqtt_client = create_mqtt_client()
    last_error = None

    async def publish_payload(payload: dict[str, Any]):
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=0)

    try:
        async with Client(url=OPCUA_ENDPOINT) as opcua_client:
            print("[OK] OPC-UA verbunden.")
            print("[OK] MQTT verbunden.")
            print("Abbruch mit STRG + C")
            print()

            async def safe_read(node_key: str, label: str):
                try:
                    node = opcua_client.get_node(NODE_IDS[node_key])
                    return await node.read_value()
                except Exception as exc:
                    raise RuntimeError(f"{label} read error: {exc}") from exc

            while True:
                timestamp = datetime.now().isoformat(timespec="seconds")

                mixer_raw = None
                mixer_liters_raw = None
                mixer_liters = None

                ro_raw = None
                ro_liters = None

                ec_value = None
                ec_ms_cm = None
                ph_value = None
                water_temperature_value = None
                dissolved_oxygen_value = None

                errors: list[str] = []

                try:
                    mixer_raw = await safe_read(
                        "mixer_level_raw_cel1",
                        "Mixer-Level"
                    )

                    if mixer_raw is not None:
                        mixer_liters_raw = calculate_liters_from_percent(
                            mixer_raw,
                            MIXER_VOLUME_LITERS
                        )

                        mixer_liters = apply_mixer_liter_calibration(
                            mixer_liters_raw
                        )

                except Exception as exc:
                    errors.append(str(exc))

                try:
                    ro_raw = await safe_read(
                        "ro_level_raw_ibc1",
                        "RO-Level"
                    )

                    if ro_raw is not None:
                        ro_liters = calculate_liters_from_percent(
                            ro_raw,
                            RO_VOLUME_LITERS
                        )

                except Exception as exc:
                    errors.append(str(exc))

                try:
                    ec_value = await safe_read("ec", "EC")
                    if ec_value is not None:
                        ec_ms_cm = float(ec_value) / 1000.0
                except Exception as exc:
                    errors.append(str(exc))

                try:
                    ph_value = await safe_read("ph", "pH")
                except Exception as exc:
                    errors.append(str(exc))

                try:
                    water_temperature_value = await safe_read(
                        "water_temperature",
                        "Water temperature"
                    )
                except Exception as exc:
                    errors.append(str(exc))

                try:
                    dissolved_oxygen_value = await safe_read(
                        "dissolved_oxygen",
                        "Dissolved oxygen"
                    )
                except Exception as exc:
                    errors.append(str(exc))

                error = " | ".join(errors) if errors else None


                mixer_percent_calibrated = None

                if mixer_liters is not None:
                    mixer_percent_calibrated = (mixer_liters / MIXER_VOLUME_LITERS) * 100.0
                    
                payload = {
                    "timestamp": timestamp,
                    "source": "python",
                    "state": "SENSOR_BRIDGE_RUNNING",

                    "mixer": {
                        "node_id": NODE_IDS["mixer_level_raw_cel1"],
                        "level_percent_raw": round_or_none(mixer_raw, 3),
                        "level_percent": round_or_none(mixer_percent_calibrated, 3),
                        "volume_liters_raw": round_or_none(mixer_liters_raw, 2),
                        "volume_liters_calc": round_or_none(mixer_liters, 2),
                        "configured_max_liters": MIXER_VOLUME_LITERS,
                        "calibration_factor": MIXER_SENSOR_LITER_FACTOR,
                        "calibration_offset": MIXER_SENSOR_LITER_OFFSET,
                        "calibration_status": MIXER_SENSOR_CALIBRATION_STATUS,
                    },

                    "ro": {
                        "node_id": NODE_IDS["ro_level_raw_ibc1"],
                        "level_percent": round_or_none(ro_raw, 3),
                        "volume_liters_calc": round_or_none(ro_liters, 2),
                        "configured_max_liters": RO_VOLUME_LITERS,
                        "calibration_status": "plausible_validated",
                    },

                    "water_values": {
                        "ec_raw": round_or_none(ec_value, 3),
                        "ec_ms_cm": round_or_none(ec_ms_cm, 3),
                        "ph": round_or_none(ph_value, 3),
                        "water_temperature": round_or_none(
                            water_temperature_value,
                            3
                        ),
                        "dissolved_oxygen": round_or_none(
                            dissolved_oxygen_value,
                            3
                        ),
                        "calibration_status": "not_final_validated",
                    },

                    "actuators": {
                        "mixer_refill_pump": None,
                        "drain_valve": None,
                        "supply_valve_6": None,
                        "transfer_pump": None,
                    },

                    "error": error,
                }

                await publish_payload(payload)

                # Nur loggen, wenn sich der Fehlerstatus ändert.
                if error != last_error:
                    if error:
                        print(f"{timestamp} | [ERROR] {error}")
                    elif last_error:
                        print(f"{timestamp} | [OK] sensor_bridge error cleared.")

                    last_error = error

                await asyncio.sleep(READ_INTERVAL_SECONDS)

    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        print("[STOP] MQTT getrennt.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOP] Bridge durch Benutzer beendet.")