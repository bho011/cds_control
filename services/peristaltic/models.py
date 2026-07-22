"""
Datentypen für den Peristaltik-Testclient: geparste serielle Antworten
(LineKind/ParsedLine) und das versionierte MCU-Mapping-Schema
(ControllerMapping/PeristalticMapping) inklusive Validierung und
Atomic-Write-Persistenz.

Kein Wrapper-Objekt je Pumpe im Mapping - "pumps" ist bewusst ein einfaches
dict[str, str] (Pumpen-Kürzel -> chemische Rolle), exakt wie im
config/peristaltic_mapping.json-Beispiel der Aufgabenstellung.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any


class LineKind(Enum):
    PING_OK = auto()
    STATUS_OK = auto()
    DOSE_STARTED = auto()
    RUNNING = auto()
    DONE = auto()
    ERR_BUSY = auto()
    ERR_LIMIT_EXCEEDED = auto()
    ERR_TIMEOUT = auto()
    ERR_INVALID_PUMP = auto()
    ERR_INVALID_COMMAND = auto()
    STOP_OK = auto()
    STOPALL_OK = auto()
    BOOT = auto()
    DIAGNOSTIC = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class ParsedLine:
    raw: str
    kind: LineKind
    pump: str | None = None            # "P1".."P4"
    pump_token: str | None = None      # exakt echoter Token bei ERR_INVALID_PUMP
    ml: float | None = None            # DOSE_STARTED/DONE-Menge oder RUNNING-Restmenge
    statuses: dict[str, str] | None = None   # STATUS_OK -> {"P1": "IDLE", ...}


# --- Mapping-Schema, Feldnamen exakt wie im Aufgaben-Beispiel ----------------

VALID_CONTROLLERS: tuple[str, ...] = ("MCU_A", "MCU_B")
VALID_PUMPS: tuple[str, ...] = ("P1", "P2", "P3", "P4")
REQUIRED_BAUDRATE = 115200
UNASSIGNED = "unassigned"


class MappingValidationError(ValueError):
    """Wird mit ALLEN gefundenen Problemen geworfen, nicht nur dem ersten."""


@dataclass
class ControllerMapping:
    role: str
    port: str | None
    baudrate: int
    pumps: dict[str, str]      # "P1" -> chemische Rolle


@dataclass
class PeristalticMapping:
    schema_version: int
    controllers: dict[str, ControllerMapping]


def default_mapping() -> PeristalticMapping:
    """Factory-Funktion statt Modul-Singleton - erzeugt bei JEDEM Aufruf
    frische ControllerMapping-Instanzen mit frischen, unabhängigen
    pumps-Dicts. Absichtlich KEINE `DEFAULT_MAPPING = PeristalticMapping(...)`
    Modulkonstante, da verschachtelte Dicts eines solchen Singletons von
    CLI/Tests/Mapping-Dialog aus versehentlich geteilt und mutiert werden
    könnten - jeder Aufruf hier liefert ein komplett unabhängiges Objekt."""
    return PeristalticMapping(
        schema_version=1,
        controllers={
            "MCU_A": ControllerMapping(
                role="PH",
                port=None,
                baudrate=REQUIRED_BAUDRATE,
                pumps={"P1": "ph_acid", "P2": "ph_base", "P3": UNASSIGNED, "P4": UNASSIGNED},
            ),
            "MCU_B": ControllerMapping(
                role="EC",
                port=None,
                baudrate=REQUIRED_BAUDRATE,
                pumps={"P1": "nutrient_a_1", "P2": "nutrient_b_1", "P3": "nutrient_a_2", "P4": "nutrient_b_2"},
            ),
        },
    )


def mapping_to_dict(mapping: PeristalticMapping) -> dict[str, Any]:
    return {
        "schema_version": mapping.schema_version,
        "controllers": {
            controller_id: {
                "role": controller.role,
                "port": controller.port,
                "baudrate": controller.baudrate,
                "pumps": dict(controller.pumps),
            }
            for controller_id, controller in mapping.controllers.items()
        },
    }


def mapping_from_dict(data: dict[str, Any]) -> PeristalticMapping:
    """Erwartet bereits validierte Daten (validate_mapping_dict() lief ohne
    Fehler) - keine erneute Prüfung hier."""
    controllers = {
        controller_id: ControllerMapping(
            role=entry["role"],
            port=entry["port"],
            baudrate=entry["baudrate"],
            pumps=dict(entry["pumps"]),
        )
        for controller_id, entry in data["controllers"].items()
    }
    return PeristalticMapping(schema_version=data["schema_version"], controllers=controllers)


def validate_mapping_dict(data: Any) -> list[str]:
    """Sammelt ALLE gefundenen Probleme (nicht nur das erste):

    - genau die Controller MCU_A und MCU_B (nicht mehr, nicht weniger)
    - je Controller genau die Pumpen P1..P4 (nicht mehr, nicht weniger)
    - keine doppelte chemische Rolle über alle 8 Pumpen-Slots hinweg,
      außer der Literalstring "unassigned"
    - port: null oder ein String
    - baudrate: exakt 115200 (type(value) is int, kein bool, kein "115200")
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return [f"Mapping muss ein Objekt (dict) sein (war: Typ {type(data).__name__})."]

    schema_version = data.get("schema_version")
    if type(schema_version) is not int:
        errors.append(f"'schema_version' muss eine echte Ganzzahl sein (war: {schema_version!r}).")

    controllers = data.get("controllers")
    if not isinstance(controllers, dict):
        errors.append(f"'controllers' muss ein Objekt (dict) sein (war: {controllers!r}).")
        return errors

    controller_keys = set(controllers.keys())
    expected_controller_keys = set(VALID_CONTROLLERS)
    missing_controllers = expected_controller_keys - controller_keys
    unexpected_controllers = controller_keys - expected_controller_keys

    if missing_controllers:
        errors.append(
            f"Fehlende Controller: {sorted(missing_controllers)} (erwartet genau {sorted(expected_controller_keys)})."
        )
    if unexpected_controllers:
        errors.append(
            f"Unbekannte Controller: {sorted(unexpected_controllers)} (erwartet genau {sorted(expected_controller_keys)})."
        )

    all_roles: list[tuple[str, str, str]] = []  # (controller_id, pump_id, role)

    for controller_id in sorted(controller_keys & expected_controller_keys):
        entry = controllers[controller_id]
        if not isinstance(entry, dict):
            errors.append(f"Controller '{controller_id}': muss ein Objekt (dict) sein (war: {entry!r}).")
            continue

        role = entry.get("role")
        if not isinstance(role, str) or not role:
            errors.append(f"Controller '{controller_id}': 'role' muss ein nicht-leerer String sein (war: {role!r}).")

        port = entry.get("port")
        if port is not None and not isinstance(port, str):
            errors.append(f"Controller '{controller_id}': 'port' muss null oder ein String sein (war: {port!r}).")

        baudrate = entry.get("baudrate")
        if type(baudrate) is not int or baudrate != REQUIRED_BAUDRATE:
            errors.append(
                f"Controller '{controller_id}': 'baudrate' muss exakt {REQUIRED_BAUDRATE} sein (war: {baudrate!r})."
            )

        pumps = entry.get("pumps")
        if not isinstance(pumps, dict):
            errors.append(f"Controller '{controller_id}': 'pumps' muss ein Objekt (dict) sein (war: {pumps!r}).")
            continue

        pump_keys = set(pumps.keys())
        expected_pump_keys = set(VALID_PUMPS)
        missing_pumps = expected_pump_keys - pump_keys
        unexpected_pumps = pump_keys - expected_pump_keys

        if missing_pumps:
            errors.append(
                f"Controller '{controller_id}': fehlende Pumpen {sorted(missing_pumps)} "
                f"(erwartet genau {sorted(expected_pump_keys)})."
            )
        if unexpected_pumps:
            errors.append(
                f"Controller '{controller_id}': unbekannte Pumpennummern {sorted(unexpected_pumps)} "
                f"(erwartet genau {sorted(expected_pump_keys)})."
            )

        for pump_id in sorted(pump_keys & expected_pump_keys):
            role_value = pumps[pump_id]
            if not isinstance(role_value, str) or not role_value:
                errors.append(
                    f"Controller '{controller_id}' Pumpe '{pump_id}': chemische Rolle muss ein "
                    f"nicht-leerer String sein (war: {role_value!r})."
                )
                continue
            all_roles.append((controller_id, pump_id, role_value))

    seen: dict[str, list[str]] = {}
    for controller_id, pump_id, role_value in all_roles:
        if role_value == UNASSIGNED:
            continue
        seen.setdefault(role_value, []).append(f"{controller_id}.{pump_id}")

    for role_value, locations in seen.items():
        if len(locations) > 1:
            errors.append(
                f"Chemische Rolle '{role_value}' ist mehrfach vergeben ({', '.join(locations)}) - "
                f"jede Rolle außer '{UNASSIGNED}' darf nur einmal vorkommen."
            )

    return errors


_SAVE_LOCK = threading.RLock()


def load_mapping(path: Path) -> PeristalticMapping:
    """Fehlende Datei ist ein Fehler, kein stillschweigender Default - der
    Default wird nur beim erstmaligen Erzeugen der Datei über
    save_mapping(path, default_mapping()) geschrieben."""
    if not path.exists():
        raise MappingValidationError(f"Mapping-Datei nicht gefunden: {path}")

    with path.open("r", encoding="utf-8") as file:
        try:
            raw = json.load(file)
        except json.JSONDecodeError as exc:
            raise MappingValidationError(f"{path}: ungültiges JSON ({exc}).") from exc

    errors = validate_mapping_dict(raw)
    if errors:
        formatted = "\n".join(f"  - {message}" for message in errors)
        raise MappingValidationError(f"{path}: {len(errors)} Fehler im Mapping:\n{formatted}")

    return mapping_from_dict(raw)


def save_mapping(path: Path, mapping: PeristalticMapping) -> None:
    with _SAVE_LOCK:
        _save_mapping_unlocked(path, mapping)


def _save_mapping_unlocked(path: Path, mapping: PeristalticMapping) -> None:
    """Atomic write: tempfile.mkstemp im selben Verzeichnis, flush+fsync,
    os.chmod(0o644), .bak-Backup der vorherigen Datei, Path.replace() -
    Muster aus nicegui_dashboard/recipe_store.py::_save_recipe_book_unlocked.
    Validiert vor dem Schreiben - ein ungültiges Mapping wird nie
    gespeichert."""
    data = mapping_to_dict(mapping)
    errors = validate_mapping_dict(data)
    if errors:
        formatted = "\n".join(f"  - {message}" for message in errors)
        raise MappingValidationError(f"Mapping ungültig, wird nicht gespeichert:\n{formatted}")

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())

        os.chmod(tmp_path, 0o644)

        if path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            try:
                shutil.copy2(path, backup_path)
            except OSError:
                pass  # Backup ist best-effort, darf ein gültiges Speichern nicht blockieren

        tmp_path.replace(path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
