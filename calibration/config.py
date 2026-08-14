"""Konstanten und Settings-Laden für die Mixing-Tank-Kalibrierung."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from services.settings_validation import SettingField, validate_settings
from services.system_config import get_mixer_level_calibration, get_mqtt_config, get_opcua_config

_SYSTEM_OPCUA_CONFIG = get_opcua_config()
_SYSTEM_MQTT_CONFIG = get_mqtt_config()
_SYSTEM_MIXER_CALIBRATION = get_mixer_level_calibration()

OPCUA_ENDPOINT = str(_SYSTEM_OPCUA_CONFIG["endpoint"])

# Mixing-Tank-Wasserstand / Sensorwert.
# Wichtig: Das ist der OPC-UA-Wert, den wir kalibrieren wollen.
MIXER_RAW_NODE_ID = "ns=4;s=Values.CEL1.PV_WaterLevel"

# Optionaler zusätzlicher System-Literwert.
# Nur setzen, wenn der Node wirklich bekannt ist.
MIXER_SYSTEM_LITERS_NODE_ID: Optional[str] = None

# Bis hierhin wird von Hand mit Eimer/Kanne befüllt - feine Auflösung im
# unteren, ohnehin bekannt nichtlinearen Bereich. Danach übernimmt die Pumpe.
MANUAL_FILL_TARGET_L = 30.0
DEFAULT_FILL_STEP_L = 5.0

# Ab MANUAL_FILL_TARGET_L füllt die Mixer-Refill-Pumpe automatisch: einmal
# PUMP_FILL_FIRST_STEP_L (Aufrunden auf die erste Tankmarkierung), danach in
# PUMP_FILL_STEP_L-Schritten bis PUMP_FILL_TARGET_L.
PUMP_FILL_FIRST_STEP_L = 20.0
PUMP_FILL_STEP_L = 25.0
PUMP_FILL_TARGET_L = 200.0

# Der Drain nach dem Pump-Fill läuft in denselben 25L-Markierungsschritten
# wieder bis 0 L runter, ebenfalls pumpengesteuert.
PUMP_DRAIN_STEP_L = 25.0

SAMPLES_PER_MEASUREMENT = 30
DELAY_BETWEEN_SAMPLES_S = 0.2
SETTLING_TIME_S = 10

# Wie oft (in Sekunden) während eines laufenden Pump-Segments ein
# Rohmesswert in die Trace-CSV geloggt wird.
TRACE_LOG_INTERVAL_S = 1.0

DATA_DIR = Path("calibration_data")

# Settings aus deinem bestehenden Refill/Drain-Test.
# Wird für Sicherheitsbestätigung und valve_settle_seconds genutzt.
SETTINGS_PATH = Path("config/calibration_settings.json")

# Wenn True, kann das Skript Mixer-Refill-Pumpe, Transferpumpe und
# Drainventil für die pumpengesteuerten Fill-/Drain-Segmente steuern.
# Es wird trotzdem zusätzlich abgefragt und sicherheitsbestätigt.
PUMP_CONTROL_ENABLED = True

# Safety-Timeouts pro Pump-Segment (nicht für den ganzen Fill-/Drain-Vorgang).
# Enter (sobald die Markierung erreicht ist) stoppt normalerweise früher,
# aber die Pumpe darf nie unbegrenzt laufen.
DEFAULT_CALIBRATION_FILL_MAX_SECONDS = 300.0
DEFAULT_CALIBRATION_DRAIN_MAX_SECONDS = 120.0

# ============================================================
# AKTUELLE MQTT-BRIDGE-KALIBRIERUNG NUR ZUR DOKUMENTATION
# Diese Werte werden im Kalibrier-Skript NICHT auf den Messwert angewendet,
# sondern nur mitgeloggt und zum Vergleich berechnet.
# ============================================================

BRIDGE_MIXER_VOLUME_LITERS = float(_SYSTEM_MIXER_CALIBRATION["volume_liters"])
BRIDGE_MIXER_SENSOR_LITER_FACTOR = float(_SYSTEM_MIXER_CALIBRATION["factor"])
BRIDGE_MIXER_SENSOR_LITER_OFFSET = float(_SYSTEM_MIXER_CALIBRATION["offset"])
BRIDGE_MIXER_SENSOR_CALIBRATION_STATUS = str(_SYSTEM_MIXER_CALIBRATION["status"])


# ============================================================
# SETTINGS
# ============================================================
#
# Keine getippten Bestätigungsphrasen/hardware_execution_enabled-Abfrage
# mehr in diesem Skript - auf ausdrücklichen Wunsch entfernt, da dieses
# Skript ohnehin nur manuell, mit anwesendem Bediener, gestartet wird.
# Der Pumpen-Schutz (Auto-Stopp-Timeout pro Segment) bleibt bestehen, siehe
# calibration_fill_max_seconds / calibration_drain_max_seconds unten.

CALIBRATION_SETTINGS_SCHEMA = [
    SettingField("valve_settle_seconds", float, required=False, default=1.0, min_value=0.0),
    SettingField("calibration_fill_max_seconds", float, required=False, default=300.0, min_value=0.0),
    SettingField("calibration_drain_max_seconds", float, required=False, default=120.0, min_value=0.0),
]


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        print(f"[WARN] Settings-Datei nicht gefunden: {SETTINGS_PATH}")
        print("[WARN] Nutze interne Default-Werte.")
        settings = {"valve_settle_seconds": 1.0}
    else:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            settings = json.load(file)

    return validate_settings(settings, CALIBRATION_SETTINGS_SCHEMA, str(SETTINGS_PATH))
