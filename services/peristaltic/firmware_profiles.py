"""
Firmwareprofile pro Controller (MCU_A/MCU_B): welche MICROSTEPS/
DEFAULT_SPEED/DEFAULT_ACCEL/MAX_RUNTIME_MS/ML_PER_STEP-Werte tatsächlich
bestätigt in die jeweilige MCU geflasht wurden.

Bewusst getrennt vom Kalibrier-Datenmodell (services/peristaltic/calibration.py):
ein Firmwareprofil beschreibt, WAS geflasht wurde; die Kalibrierdatei
beschreibt, WELCHE Messversuche mit diesem (oder einem älteren, ggf.
inzwischen falschen) Firmwarewert protokolliert wurden. Ein aus einer
Kalibrierung berechneter candidate_ml_per_step wird NIE automatisch in
dieses Profil übernommen - nur eine ausdrücklich bestätigte
Firmwareänderung darf profile-status/-Werte ändern. MCU_A und MCU_B dürfen
jederzeit unterschiedliche Profile haben (unterschiedliche Firmware-Builds,
siehe docs/PERISTALTIC_PROFILE_PLAN.md).

status == "unconfirmed" (durchgehend null-Werte) ist der sichere
Ausgangszustand für einen Controller, dessen tatsächlich geflashtes Profil
nicht zweifelsfrei bekannt ist. resolve_firmware_ml_per_step() lehnt jede
Auflösung für einen solchen Controller ab, ebenso für eine einzelne Pumpe
mit null-Wert oder ein fehlendes/strukturell ungültiges Profil - in jedem
dieser Fälle muss der Aufrufer (scripts/peristaltic_calibration_cli.py::
cmd_calibrate) vor jeder Pumpenbewegung abbrechen. Keine stillen Defaults.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.peristaltic.models import VALID_CONTROLLERS, VALID_PUMPS

SCHEMA_VERSION = 1
VALID_STATUSES: tuple[str, ...] = ("confirmed", "unconfirmed")


class FirmwareProfileError(ValueError):
    """Sammelt ALLE gefundenen Strukturprobleme beim Laden/Validieren; wird
    auch von resolve_firmware_ml_per_step() für Auflösungsfehler
    (unconfirmed, fehlender Pumpenwert, fehlendes/ungültiges Profil)
    geworfen - nie ein roher ValueError/TypeError/KeyError."""


@dataclass
class PumpFirmwareEntry:
    firmware_ml_per_step: float | None


@dataclass
class ControllerFirmwareProfile:
    status: str
    profile_id: str | None
    microsteps: int | None
    speed_rpm: int | None
    acceleration_steps_per_s2: int | None
    max_runtime_ms: int | None
    pumps: dict[str, PumpFirmwareEntry]


@dataclass
class FirmwareProfiles:
    schema_version: int
    controllers: dict[str, ControllerFirmwareProfile]


def default_firmware_profiles() -> FirmwareProfiles:
    """Frische, unabhängige unconfirmed/null-Struktur pro Aufruf (kein
    Modul-Singleton, gleiches Prinzip wie models.default_mapping()). Nur
    für eine komplett neue Datei gedacht - eine bestehende, aber
    unvollständige/kaputte Datei wird NIE stillschweigend hiermit
    überschrieben."""

    def _controller() -> ControllerFirmwareProfile:
        return ControllerFirmwareProfile(
            status="unconfirmed",
            profile_id=None,
            microsteps=None,
            speed_rpm=None,
            acceleration_steps_per_s2=None,
            max_runtime_ms=None,
            pumps={pump: PumpFirmwareEntry(firmware_ml_per_step=None) for pump in VALID_PUMPS},
        )

    return FirmwareProfiles(
        schema_version=SCHEMA_VERSION,
        controllers={controller_id: _controller() for controller_id in VALID_CONTROLLERS},
    )


def firmware_profiles_to_dict(profiles: FirmwareProfiles) -> dict[str, Any]:
    return {
        "schema_version": profiles.schema_version,
        "controllers": {
            controller_id: {
                "status": controller.status,
                "profile_id": controller.profile_id,
                "microsteps": controller.microsteps,
                "speed_rpm": controller.speed_rpm,
                "acceleration_steps_per_s2": controller.acceleration_steps_per_s2,
                "max_runtime_ms": controller.max_runtime_ms,
                "pumps": {
                    pump_id: {"firmware_ml_per_step": entry.firmware_ml_per_step}
                    for pump_id, entry in controller.pumps.items()
                },
            }
            for controller_id, controller in profiles.controllers.items()
        },
    }


def firmware_profiles_from_dict(data: dict[str, Any]) -> FirmwareProfiles:
    """Erwartet bereits validierte Daten (validate_firmware_profiles_dict()
    lief ohne Fehler) - keine erneute Prüfung hier, wie
    models.mapping_from_dict()."""
    controllers = {
        controller_id: ControllerFirmwareProfile(
            status=entry["status"],
            profile_id=entry["profile_id"],
            microsteps=entry["microsteps"],
            speed_rpm=entry["speed_rpm"],
            acceleration_steps_per_s2=entry["acceleration_steps_per_s2"],
            max_runtime_ms=entry["max_runtime_ms"],
            pumps={
                pump_id: PumpFirmwareEntry(firmware_ml_per_step=pump_entry["firmware_ml_per_step"])
                for pump_id, pump_entry in entry["pumps"].items()
            },
        )
        for controller_id, entry in data["controllers"].items()
    }
    return FirmwareProfiles(schema_version=data["schema_version"], controllers=controllers)


def _validate_optional_positive_int(value: Any, label: str, errors: list[str]) -> None:
    if value is None:
        return
    if type(value) is not int or value <= 0:
        errors.append(f"{label} muss null oder eine positive Ganzzahl sein (war: {value!r}).")


def _validate_optional_positive_float(value: Any, label: str, errors: list[str]) -> None:
    if value is None:
        return
    if type(value) is bool or not isinstance(value, (int, float)):
        errors.append(f"{label} muss null oder eine Zahl sein (war: {value!r}, Typ {type(value).__name__}).")
        return
    if not math.isfinite(value) or value <= 0:
        errors.append(f"{label} muss null oder eine endliche, positive Zahl sein (war: {value!r}).")


def validate_firmware_profiles_dict(data: Any) -> list[str]:
    """Sammelt ALLE gefundenen Probleme (nicht nur das erste), exakt wie
    models.validate_mapping_dict(). Bewusst KEINE Kopplung zwischen
    status=="confirmed" und "alle Pumpenwerte gesetzt": ob ein einzelner
    Pumpenwert fehlt (null), ist eine Auflösungsfrage pro angefragter
    Pumpe (siehe resolve_firmware_ml_per_step), keine Strukturverletzung -
    ein confirmed-Controller, bei dem erst eine von vier Pumpen einzeln
    vermessen wurde, ist strukturell gültig."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return [f"Firmwareprofil muss ein Objekt (dict) sein (war: Typ {type(data).__name__})."]

    schema_version = data.get("schema_version")
    if type(schema_version) is not int:
        errors.append(f"'schema_version' muss eine echte Ganzzahl sein (war: {schema_version!r}).")
    elif schema_version > SCHEMA_VERSION:
        errors.append(
            f"'schema_version' {schema_version} ist neuer als die unterstützte Version {SCHEMA_VERSION} "
            "- Datei wird nicht interpretiert."
        )

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

    for controller_id in sorted(controller_keys & expected_controller_keys):
        entry = controllers[controller_id]
        if not isinstance(entry, dict):
            errors.append(f"Controller '{controller_id}': muss ein Objekt (dict) sein (war: {entry!r}).")
            continue

        status = entry.get("status")
        if status not in VALID_STATUSES:
            errors.append(
                f"Controller '{controller_id}': 'status' muss einer von {list(VALID_STATUSES)} sein (war: {status!r})."
            )

        profile_id = entry.get("profile_id")
        if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
            errors.append(
                f"Controller '{controller_id}': 'profile_id' muss null oder ein nicht-leerer String sein "
                f"(war: {profile_id!r})."
            )

        _validate_optional_positive_int(entry.get("microsteps"), f"Controller '{controller_id}': 'microsteps'", errors)
        _validate_optional_positive_int(entry.get("speed_rpm"), f"Controller '{controller_id}': 'speed_rpm'", errors)
        _validate_optional_positive_int(
            entry.get("acceleration_steps_per_s2"), f"Controller '{controller_id}': 'acceleration_steps_per_s2'", errors
        )
        _validate_optional_positive_int(
            entry.get("max_runtime_ms"), f"Controller '{controller_id}': 'max_runtime_ms'", errors
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
            pump_entry = pumps[pump_id]
            if not isinstance(pump_entry, dict):
                errors.append(
                    f"Controller '{controller_id}' Pumpe '{pump_id}': muss ein Objekt (dict) sein (war: {pump_entry!r})."
                )
                continue
            if "firmware_ml_per_step" not in pump_entry:
                errors.append(f"Controller '{controller_id}' Pumpe '{pump_id}': 'firmware_ml_per_step' fehlt.")
                continue
            _validate_optional_positive_float(
                pump_entry.get("firmware_ml_per_step"),
                f"Controller '{controller_id}' Pumpe '{pump_id}': 'firmware_ml_per_step'",
                errors,
            )

    return errors


_SAVE_LOCK = threading.RLock()


def load_firmware_profiles(path: Path) -> FirmwareProfiles:
    """Fehlende Datei, ungültiges JSON oder ein strukturell ungültiges
    Profil sind allesamt Fehler - es gibt KEINEN stillen Fallback auf
    default_firmware_profiles() beim Laden. Der Aufrufer (cmd_calibrate)
    muss in jedem dieser Fälle vor jeder Pumpenbewegung abbrechen."""
    if not path.exists():
        raise FirmwareProfileError(f"Firmwareprofil-Datei nicht gefunden: {path}")

    with path.open("r", encoding="utf-8") as file:
        try:
            raw = json.load(file)
        except json.JSONDecodeError as exc:
            raise FirmwareProfileError(f"{path}: ungültiges JSON ({exc}).") from exc

    errors = validate_firmware_profiles_dict(raw)
    if errors:
        formatted = "\n".join(f"  - {message}" for message in errors)
        raise FirmwareProfileError(f"{path}: {len(errors)} Fehler im Firmwareprofil:\n{formatted}")

    return firmware_profiles_from_dict(raw)


def save_firmware_profiles(path: Path, profiles: FirmwareProfiles) -> None:
    with _SAVE_LOCK:
        _save_firmware_profiles_unlocked(path, profiles)


def _save_firmware_profiles_unlocked(path: Path, profiles: FirmwareProfiles) -> None:
    """Atomic write: tempfile.mkstemp im selben Verzeichnis, flush+fsync,
    os.chmod(0o644), .bak-Backup der vorherigen Datei, Path.replace() -
    identisches Muster zu models.py::_save_mapping_unlocked. Validiert vor
    dem Schreiben - ein ungültiges Profil wird nie gespeichert."""
    data = firmware_profiles_to_dict(profiles)
    errors = validate_firmware_profiles_dict(data)
    if errors:
        formatted = "\n".join(f"  - {message}" for message in errors)
        raise FirmwareProfileError(f"Firmwareprofil ungültig, wird nicht gespeichert:\n{formatted}")

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


def resolve_firmware_ml_per_step(profiles: FirmwareProfiles, controller: str, pump: str) -> float:
    """Liefert den bestätigten firmware_ml_per_step-Wert für genau diesen
    Controller/diese Pumpe. Wirft FirmwareProfileError (niemals einen
    stillen Default und niemals FIRMWARE_DEFAULT_ML_PER_STEP aus
    calibration.py) wenn: der Controller im Profil fehlt, sein Status
    nicht "confirmed" ist, die Pumpe im Profil fehlt, oder ihr
    firmware_ml_per_step null ist."""
    controller_profile = profiles.controllers.get(controller)
    if controller_profile is None:
        raise FirmwareProfileError(f"Kein Firmwareprofil für Controller {controller!r} vorhanden.")

    if controller_profile.status != "confirmed":
        raise FirmwareProfileError(
            f"Firmwareprofil für {controller} ist nicht bestätigt (status={controller_profile.status!r}). "
            "Kalibrierung wird abgebrochen, bevor eine Pumpe bewegt wird."
        )

    pump_entry = controller_profile.pumps.get(pump)
    if pump_entry is None:
        raise FirmwareProfileError(f"Kein Firmwareprofil-Eintrag für Pumpe {pump!r} an Controller {controller}.")

    if pump_entry.firmware_ml_per_step is None:
        raise FirmwareProfileError(
            f"firmware_ml_per_step für {controller}/{pump} ist im Firmwareprofil nicht gesetzt (null). "
            "Kalibrierung wird abgebrochen, bevor eine Pumpe bewegt wird."
        )

    return pump_entry.firmware_ml_per_step
