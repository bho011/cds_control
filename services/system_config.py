from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


SYSTEM_CONFIG_PATH = Path("config/system_config.json")


DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
    "opcua": {
        "endpoint": "opc.tcp://10.8.0.62:14840",
        "read_timeout_seconds": 3.0,
    },
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "sensor_topic": "cds/status/sensors",
        "process_topic": "cds/status/process",
        "qos": 1,
        "publish_timeout_seconds": 2.0,
    },
    "mixer_level_calibration": {
        "volume_liters": 200.0,
        "factor": 0.175,
        "offset": 0.0,
        "status": "temporary_factor_0_175",
    },
}


def _deep_merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(default)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def load_system_config() -> dict[str, Any]:
    if not SYSTEM_CONFIG_PATH.exists():
        return deepcopy(DEFAULT_SYSTEM_CONFIG)

    with SYSTEM_CONFIG_PATH.open("r", encoding="utf-8") as file:
        loaded = json.load(file)

    return _deep_merge(DEFAULT_SYSTEM_CONFIG, loaded)


def get_opcua_config() -> dict[str, Any]:
    return load_system_config()["opcua"]


def get_mqtt_config() -> dict[str, Any]:
    return load_system_config()["mqtt"]


def get_mixer_level_calibration() -> dict[str, Any]:
    return load_system_config()["mixer_level_calibration"]
