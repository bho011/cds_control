import asyncio
import csv
from datetime import datetime
from pathlib import Path

from asyncua import Client


OPCUA_ENDPOINT = "opc.tcp://10.8.0.62:14840"

NODE_IDS = {
    "mixer_level_raw_cel1": "ns=4;s=Values.CEL1.PV_WaterLevel",
    "ro_level_raw_ibc1": "ns=4;s=Values.IBC1.PV_WaterLevel",
}

# Werte aus der alten Node-RED-Logik
MIXER_VOLUME_LITERS = 200
RO_VOLUME_LITERS = 1000

# Messintervall
READ_INTERVAL_SECONDS = 1.0

# Log-Ordner
LOG_DIR = Path("logs")


def calculate_liters_from_percent(value: float, max_volume_liters: float) -> int:
    return round((value / 100) * max_volume_liters)


def create_log_file() -> Path:
    LOG_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"sensor_calibration_{timestamp}.csv"


def ask_measurement_context():
    print("Messkontext für Kalibrierung")
    print("============================")
    print("Beispiele für Notiz:")
    print("- Sensor leer im Mixing Tank")
    print("- Sensor im Eimer mit ca. 10 L")
    print("- RO-Tank ca. 2/3 voll")
    print()

    note = input("Notiz zur Messung: ").strip()

    actual_liters_input = input(
        "Bekannte echte Füllmenge in Litern, falls bekannt, sonst Enter: "
    ).strip()

    actual_liters = None

    if actual_liters_input:
        try:
            actual_liters = float(actual_liters_input.replace(",", "."))
        except ValueError:
            print("[WARN] Echte Füllmenge konnte nicht gelesen werden. Wird leer gelassen.")
            actual_liters = None

    return note, actual_liters


async def read_sensor_values():
    note, actual_liters = ask_measurement_context()
    log_file_path = create_log_file()

    print()
    print("CDS OPC-UA Sensor Read Test mit CSV-Logging")
    print("==========================================")
    print(f"Endpoint: {OPCUA_ENDPOINT}")
    print(f"Logdatei: {log_file_path}")
    print(f"Notiz: {note}")
    print(f"Echte Füllmenge: {actual_liters if actual_liters is not None else 'nicht angegeben'}")
    print()

    with log_file_path.open(mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file, delimiter=";")

        writer.writerow([
            "timestamp",
            "note",
            "actual_liters",
            "mixer_level_raw_cel1_percent",
            "mixer_liters_calc_node_red_formula",
            "ro_level_raw_ibc1_percent",
            "ro_liters_calc_node_red_formula",
            "opcua_endpoint",
            "mixer_node_id",
            "ro_node_id",
        ])

        async with Client(url=OPCUA_ENDPOINT) as client:
            print("[OK] Verbindung zum OPC-UA-Server hergestellt.")
            print("Abbruch mit STRG + C")
            print()

            while True:
                timestamp = datetime.now().isoformat(timespec="seconds")

                mixer_raw = None
                ro_raw = None
                mixer_liters_calc = None
                ro_liters_calc = None

                try:
                    mixer_node = client.get_node(NODE_IDS["mixer_level_raw_cel1"])
                    mixer_raw = await mixer_node.read_value()

                    mixer_liters_calc = calculate_liters_from_percent(
                        mixer_raw,
                        MIXER_VOLUME_LITERS
                    )

                except Exception as error:
                    print(f"[ERROR] Mixer-Level konnte nicht gelesen werden: {error}")

                try:
                    ro_node = client.get_node(NODE_IDS["ro_level_raw_ibc1"])
                    ro_raw = await ro_node.read_value()

                    ro_liters_calc = calculate_liters_from_percent(
                        ro_raw,
                        RO_VOLUME_LITERS
                    )

                except Exception as error:
                    print(f"[ERROR] RO-Level konnte nicht gelesen werden: {error}")

                writer.writerow([
                    timestamp,
                    note,
                    actual_liters if actual_liters is not None else "",
                    mixer_raw if mixer_raw is not None else "",
                    mixer_liters_calc if mixer_liters_calc is not None else "",
                    ro_raw if ro_raw is not None else "",
                    ro_liters_calc if ro_liters_calc is not None else "",
                    OPCUA_ENDPOINT,
                    NODE_IDS["mixer_level_raw_cel1"],
                    NODE_IDS["ro_level_raw_ibc1"],
                ])

                # Wichtig: direkt auf die SD-Karte schreiben,
                # damit bei Abbruch keine Messdaten verloren gehen.
                csv_file.flush()

                print(
                    f"{timestamp} | "
                    f"CEL1 raw: {mixer_raw} | "
                    f"MixerLiters calc: {mixer_liters_calc} | "
                    f"IBC1 raw: {ro_raw} | "
                    f"ROLiters calc: {ro_liters_calc}"
                )

                await asyncio.sleep(READ_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(read_sensor_values())
    except KeyboardInterrupt:
        print("\n[STOP] Messung durch Benutzer beendet.")