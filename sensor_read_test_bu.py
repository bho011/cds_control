import asyncio
from asyncua import Client


OPCUA_ENDPOINT = "opc.tcp://10.8.0.62:14840"

NODE_IDS = {
    "mixer_level_raw_cel1": "ns=4;s=Values.CEL1.PV_WaterLevel",
    "ro_level_raw_ibc1": "ns=4;s=Values.IBC1.PV_WaterLevel",
}


MIXER_VOLUME_LITERS = 200
RO_VOLUME_LITERS = 1300


def calculate_liters_from_percent(value: float, max_volume_liters: float) -> int:
    return round((value / 100) * max_volume_liters)


async def main():
    print("CDS OPC-UA Sensor Read Test")
    print("===========================")
    print(f"Endpoint: {OPCUA_ENDPOINT}")
    print()

    async with Client(url=OPCUA_ENDPOINT) as client:
        print("[OK] Verbindung zum OPC-UA-Server hergestellt.")
        print()

        while True:
            values = {}

            for name, node_id in NODE_IDS.items():
                try:
                    node = client.get_node(node_id)
                    value = await node.read_value()
                    values[name] = value

                    print(f"{name:22} | {node_id:40} | raw = {value}")

                except Exception as error:
                    print(f"[ERROR] {name} | {node_id} | {error}")

            if "mixer_level_raw_cel1" in values:
                mixer_liters = calculate_liters_from_percent(
                    values["mixer_level_raw_cel1"],
                    MIXER_VOLUME_LITERS
                )
                print(f"{'mixer_liters_calc':22} | {'Berech. nach Node-RED Formel':40} | liters = {mixer_liters}")

            if "ro_level_raw_ibc1" in values:
                ro_liters = calculate_liters_from_percent(
                    values["ro_level_raw_ibc1"],
                    RO_VOLUME_LITERS
                )
                print(f"{'ro_liters_calc':22} | {'Berech. nach Node-RED Formel':40} | liters = {ro_liters}")

            print("-" * 90)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())