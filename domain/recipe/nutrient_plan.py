"""Reiner, unveraenderlicher Dosierplan fuer die MCU_B-Naehrstoffdosierung
(AP-05 Stufe 1).

Eingabegrundlage ist ausschliesslich der validierte ``RunConfigSnapshot``
(bzw. dessen ``to_dict()``-Form). Es gibt KEINE zweite Rezeptberechnung: Summen,
A/B-Aufteilung und Volumen werden mit den vorhandenen Funktionen aus
``calculations.py``/``validation.py`` nur NACHGERECHNET und gegen den Snapshot
abgeglichen - jeder Widerspruch ist ein ``NutrientPlanError``.

Ein Plan ist KEIN Nachweis einer Hardwarefreigabe. Ob Profil, Kalibrierung,
MCU-Identitaet und Firmware physisch gueltig sind, entscheiden die separaten
Gates (Stufe 2). Dieses Modul sendet nie einen Befehl, liest keine Datei,
keinen Sensor und kennt keine Zeit.

Rechnung deterministisch in ganzzahligen Mikrolitern (uL):

* Gesamtmenge/A/B kommen aus dem Snapshot (``round_ml``-Regel, 3 Nachkommastellen
  = ganze uL).
* Je Loesung wird auf zwei Pumpen verteilt (A: P1 + P3, B: P2 + P4). Gewichtet
  wird mit den ausdruecklich uebergebenen Profilgewichten (``firmware_ml_per_step``):
  die ERSTE Pumpe (P1 bzw. P2) erhaelt ``floor(Menge * w1/(w1+w2) + 1/2)``
  (kaufmaennisch auf ganze uL gerundet), die ZWEITE (P3 bzw. P4) den Rest. Damit
  ist die Summe je Loesung exakt, eine ungerade uL-Menge landet eindeutig bei
  der ersten Pumpe (gleiche Gewichte) bzw. folgt der Rundungsregel. Es gibt
  keinen 50/50-Fallback und keine Standardgewichte.
* Je Pumpe wird in gleich grosse Teilauftraege (Chunks) <= Maximum zerlegt:
  ``n = ceil(Ziel / max)``, jeder Chunk ``Ziel // n``, die ersten ``Ziel % n``
  Chunks je 1 uL mehr. Kein winziger Restchunk, keine Null-Chunks.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
from types import MappingProxyType
from dataclasses import dataclass, fields
from enum import Enum
from fractions import Fraction
from typing import Any, Iterator, Mapping

from .calculations import (
    compute_base_volume_before_ro_correction_l,
    compute_nutrient_split,
    compute_total_nutrients_ml,
    nutrient_b_percent,
    round_l,
)
from .field_validators import validate_numeric_field, validate_strict_int_field
from .models import RECIPE_SCHEMA_VERSION, RunConfigSnapshot
from .validation import validate_recipe_values
from .volume_balance import (
    UL_PER_ML,
    is_finite_number,
    safe_repr,
    VolumeModelError,
    VolumePlan,
    ml_to_ul,
    ul_to_ml,
)

PLAN_VERSION = 1

# Referenzwert des Firmware-Limits (services/peristaltic/protocol.py::
# FIRMWARE_MAX_SINGLE_DOSE_ML). Bewusst nicht importiert: die Domain bleibt frei
# von Service-Abhaengigkeiten; ein Test prueft die Gleichheit.
ABSOLUTE_MAX_CHUNK_ML = 50.0

MCU_ID = "MCU_B"

# Verbindliche Kanalbelegung (config/peristaltic_mapping.json, MCU_B).
#
# Kanonische Quelle sind PRIVATE, unveraenderliche Tupel. Die oeffentlichen
# Konstanten sind nur schreibgeschuetzte Sichten darauf (MappingProxyType);
# Validierung und Berechnung lesen ausschliesslich die privaten Tupel, nie die
# oeffentlichen Namen. Weder eine Item-Zuweisung (TypeError) noch ein
# Neubinden des Modulattributs aendert daher die akzeptierte Belegung.
_CANONICAL_PUMP_ROLES: tuple[tuple[str, str], ...] = (
    ("P1", "nutrient_a_1"),
    ("P2", "nutrient_b_1"),
    ("P3", "nutrient_a_2"),
    ("P4", "nutrient_b_2"),
)
# (erste Pumpe, zweite Pumpe = Rest) je Loesung
_CANONICAL_SOLUTION_PAIRS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("A", ("P1", "P3")),
    ("B", ("P2", "P4")),
)
PUMP_ORDER: tuple[str, ...] = tuple(pump for pump, _ in _CANONICAL_PUMP_ROLES)

PUMP_ROLES: Mapping[str, str] = MappingProxyType(dict(_CANONICAL_PUMP_ROLES))
SOLUTION_PAIRS: Mapping[str, tuple[str, str]] = MappingProxyType(dict(_CANONICAL_SOLUTION_PAIRS))

# Technische RESSOURCEN-Begrenzung (kein physikalisches Pumpenlimit): hoechste
# Chunk-Anzahl je Pumpe, die ein Plan materialisieren darf. Herleitung: das
# Rezept-Gesamtvolumen ist <= 185 l; bei der vorlaeufigen Planungs-Chunkgroesse
# von 10 ml ergaeben sich im Extremfall hoechstens 18 500 Chunks fuer eine
# einzelne Pumpe, aufgerundet 20 000. Ein Auftrag, der mit dem uebergebenen
# ``max_chunk_ml`` mehr Chunks braeuchte, wird abgelehnt - die Chunks werden
# weder vergroessert noch wird die Menge veraendert. Das Plan-Maximum betraegt
# damit 4 x 20 000 = 80 000 Chunks.
MAX_CHUNKS_PER_PUMP = 20_000


class PlanErrorCode(str, Enum):
    INVALID_SNAPSHOT = "invalid_snapshot"
    INVALID_TYPES = "invalid_types"
    RECIPE_INVALID = "recipe_invalid"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    DISABLED_BUT_AMOUNT = "disabled_but_amount"
    ENABLED_ZERO_AMOUNT = "enabled_zero_amount"
    INVALID_PUMP_ROLES = "invalid_pump_roles"
    INVALID_WEIGHTS = "invalid_weights"
    INVALID_PROFILE_ID = "invalid_profile_id"
    INVALID_CHUNK_PARAMETER = "invalid_chunk_parameter"
    CHUNK_BUDGET_EXCEEDED = "chunk_budget_exceeded"
    CHUNK_BELOW_MINIMUM = "chunk_below_minimum"
    VOLUME_INVALID = "volume_invalid"
    INVALID_AMOUNT = "invalid_amount"


class NutrientPlanError(ValueError):
    """Kontrollierter Planungsfehler. ``code`` ist maschinenlesbar,
    ``messages`` enthaelt alle gefundenen Einzelprobleme."""

    def __init__(self, code: PlanErrorCode, messages: str | list[str] | tuple[str, ...]) -> None:
        items = (messages,) if isinstance(messages, str) else tuple(messages)
        super().__init__("; ".join(items))
        self.code = code
        self.messages = items


# --- Datenmodell -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DoseChunk:
    pump: str
    index: int  # 1-basiert je Pumpe
    amount_ul: int

    @property
    def amount_ml(self) -> float:
        return ul_to_ml(self.amount_ul)

    @property
    def dose_ml_text(self) -> str:
        """Genau die Darstellung des DOSE-Protokolls (3 Nachkommastellen)."""
        return f"{self.amount_ul // UL_PER_ML}.{self.amount_ul % UL_PER_ML:03d}"


@dataclass(frozen=True)
class PumpTarget:
    pump: str
    role: str
    solution: str  # "A" | "B"
    weight_ml_per_step: float  # verwendetes Profilgewicht (Eingabedatum)
    target_ul: int
    chunks_ul: tuple[int, ...]

    @property
    def target_ml(self) -> float:
        return ul_to_ml(self.target_ul)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks_ul)

    def chunks(self) -> tuple[DoseChunk, ...]:
        return tuple(DoseChunk(self.pump, i, amount) for i, amount in enumerate(self.chunks_ul, start=1))


@dataclass(frozen=True)
class NutrientDosingPlan:
    plan_version: int
    recipe_id: str
    recipe_name: str
    snapshot_created_at: str
    snapshot_schema_version: int

    dosing_enabled: bool
    controller: str
    profile_id: str | None

    total_ul: int
    a_ul: int
    b_ul: int
    pump_targets: tuple[PumpTarget, ...]  # leer bei deaktivierter Dosierung, sonst P1..P4

    max_chunk_ml: float | None
    min_chunk_ml: float | None
    volume: VolumePlan
    plan_hash: str

    @property
    def has_dosing_work(self) -> bool:
        return self.dosing_enabled and self.total_ul > 0

    @property
    def total_ml(self) -> float:
        return ul_to_ml(self.total_ul)

    @property
    def a_ml(self) -> float:
        return ul_to_ml(self.a_ul)

    @property
    def b_ml(self) -> float:
        return ul_to_ml(self.b_ul)

    @property
    def round_count(self) -> int:
        return max((t.chunk_count for t in self.pump_targets), default=0)

    def pump_target(self, pump: str) -> PumpTarget:
        for target in self.pump_targets:
            if target.pump == pump:
                return target
        raise KeyError(pump)

    def iter_rounds(self) -> Iterator[tuple[DoseChunk, ...]]:
        """Speicherschonend: erzeugt Runde fuer Runde (Runde r = r-ter Chunk
        jeder Pumpe, die noch Chunks hat) - je Aufruf nur eine Runde im
        Speicher. Reine Daten, es wird nichts gesendet."""
        for r in range(self.round_count):
            yield tuple(
                DoseChunk(t.pump, r + 1, t.chunks_ul[r]) for t in self.pump_targets if r < len(t.chunks_ul)
            )

    def rounds(self) -> tuple[tuple[DoseChunk, ...], ...]:
        """Materialisiert ALLE Runden (hoechstens 4 x ``MAX_CHUNKS_PER_PUMP``
        Chunk-Objekte, durch die Chunk-Begrenzung gedeckelt). Fuer die
        Integration ``iter_rounds()`` bevorzugen."""
        return tuple(self.iter_rounds())

    def core_dict(self) -> dict[str, Any]:
        """Alle Felder ausser dem Hash, kanonisch (fuer Hash und Journal)."""
        return {
            "plan_version": self.plan_version,
            "recipe_id": self.recipe_id,
            "recipe_name": self.recipe_name,
            "snapshot_created_at": self.snapshot_created_at,
            "snapshot_schema_version": self.snapshot_schema_version,
            "dosing_enabled": self.dosing_enabled,
            "controller": self.controller,
            "profile_id": self.profile_id,
            "total_ul": self.total_ul,
            "a_ul": self.a_ul,
            "b_ul": self.b_ul,
            "pump_targets": [
                {
                    "pump": t.pump,
                    "role": t.role,
                    "solution": t.solution,
                    "weight_ml_per_step": t.weight_ml_per_step,
                    "target_ul": t.target_ul,
                    "chunks_ul": list(t.chunks_ul),
                }
                for t in self.pump_targets
            ],
            "max_chunk_ml": self.max_chunk_ml,
            "min_chunk_ml": self.min_chunk_ml,
            "volume": {
                "target_ro_water_l": self.volume.target_ro_water_l,
                "estimated_process_volume_l": self.volume.estimated_process_volume_l,
                "planned_nutrient_ml": self.volume.planned_nutrient_ml,
                "max_mixer_l": self.volume.max_mixer_l,
                "prefill_target_l": self.volume.prefill_target_l,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.core_dict()
        data["plan_hash"] = self.plan_hash
        return data

    def verify_hash(self) -> bool:
        return _hash_core(self.core_dict()) == self.plan_hash


def _hash_core(core: dict[str, Any]) -> str:
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- reine Rechenfunktionen --------------------------------------------------------------


def _check_number(value: Any, name: str) -> float:
    if not is_finite_number(value):
        raise NutrientPlanError(PlanErrorCode.INVALID_TYPES, f"{name} muss eine endliche Zahl sein (war: {safe_repr(value)}).")
    return value


def split_pair_ul(total_ul: int, weight_first: float, weight_second: float) -> tuple[int, int]:
    """Verteilt ``total_ul`` auf zwei Pumpen anhand der Gewichte; Summe exakt.
    Erste Pumpe: kaufmaennisch gerundeter Anteil, zweite Pumpe: Rest."""
    if type(total_ul) is not int or total_ul < 0:
        raise NutrientPlanError(PlanErrorCode.INVALID_AMOUNT, f"total_ul muss eine nichtnegative Ganzzahl sein (war: {safe_repr(total_ul)}).")
    for name, weight in (("weight_first", weight_first), ("weight_second", weight_second)):
        _check_number(weight, name)
        if weight <= 0:
            raise NutrientPlanError(PlanErrorCode.INVALID_WEIGHTS, f"{name} muss > 0 sein (war: {safe_repr(weight)}).")
    wf, ws = Fraction(weight_first), Fraction(weight_second)
    first = math.floor(Fraction(total_ul) * wf / (wf + ws) + Fraction(1, 2))
    return first, total_ul - first


def plan_chunks_ul(
    target_ul: int,
    max_chunk_ml: float,
    min_chunk_ml: float | None = None,
    *,
    max_chunk_count: int = MAX_CHUNKS_PER_PUMP,
) -> tuple[int, ...]:
    """Zerlegt ``target_ul`` in gleich grosse Teilauftraege (Differenz hoechstens
    1 uL, absteigend sortiert), jeder <= ``max_chunk_ml``. Reine Datenfunktion.

    Nullmenge -> ()  (keine Null-Chunks). Ungueltige Parameter bzw. ein Chunk
    unter ``min_chunk_ml`` -> NutrientPlanError, ohne Korrektur und ohne
    Umverteilung.

    Ressourcenbegrenzung: die Chunk-ANZAHL wird vor jeder Allokation berechnet;
    uebersteigt sie ``max_chunk_count`` (Standard ``MAX_CHUNKS_PER_PUMP``),
    gibt es ``CHUNK_BUDGET_EXCEEDED``. ``max_chunk_count`` darf das feste
    Budget ``MAX_CHUNKS_PER_PUMP`` nur nach unten begrenzen; Werte darueber
    (oder < 1) werden abgelehnt, auch bei ``target_ul == 0``. Chunks werden nie vergroessert und
    ``max_chunk_ml`` wird nie ueberschritten."""
    if type(target_ul) is not int or target_ul < 0:
        raise NutrientPlanError(PlanErrorCode.INVALID_AMOUNT, f"target_ul muss eine nichtnegative Ganzzahl sein (war: {safe_repr(target_ul)}).")
    try:
        max_ul = ml_to_ul(max_chunk_ml, "max_chunk_ml")
    except VolumeModelError as exc:
        raise NutrientPlanError(PlanErrorCode.INVALID_CHUNK_PARAMETER, str(exc)) from exc
    if max_ul <= 0 or max_chunk_ml > ABSOLUTE_MAX_CHUNK_ML:
        raise NutrientPlanError(
            PlanErrorCode.INVALID_CHUNK_PARAMETER,
            f"max_chunk_ml muss in (0, {ABSOLUTE_MAX_CHUNK_ML}] liegen (war: {safe_repr(max_chunk_ml)}).",
        )
    min_ul = 0
    if min_chunk_ml is not None:
        try:
            min_ul = ml_to_ul(min_chunk_ml, "min_chunk_ml")
        except VolumeModelError as exc:
            raise NutrientPlanError(PlanErrorCode.INVALID_CHUNK_PARAMETER, str(exc)) from exc
        if min_ul <= 0 or min_ul > max_ul:
            raise NutrientPlanError(
                PlanErrorCode.INVALID_CHUNK_PARAMETER,
                f"min_chunk_ml muss in (0, max_chunk_ml] liegen (war: {safe_repr(min_chunk_ml)}).",
            )

    # max_chunk_count darf das absolute technische Budget nur NACH UNTEN begrenzen
    # und wird immer geprueft - auch bei target_ul == 0.
    if type(max_chunk_count) is not int or not 1 <= max_chunk_count <= MAX_CHUNKS_PER_PUMP:
        raise NutrientPlanError(
            PlanErrorCode.INVALID_CHUNK_PARAMETER,
            f"max_chunk_count muss eine Ganzzahl in [1, {MAX_CHUNKS_PER_PUMP}] sein "
            f"(war: {safe_repr(max_chunk_count)}); das technische Budget kann nicht erhoeht werden.",
        )

    if target_ul == 0:
        return ()

    count = -(-target_ul // max_ul)  # reine Ganzzahlrechnung, keine Allokation
    if count > max_chunk_count:
        raise NutrientPlanError(
            PlanErrorCode.CHUNK_BUDGET_EXCEEDED,
            f"Auftrag braeuchte mehr als {max_chunk_count} Teilauftraege (max_chunk_ml={max_chunk_ml}); "
            "Plan abgelehnt - Chunks werden nicht vergroessert und die Menge nicht veraendert.",
        )
    base, remainder = divmod(target_ul, count)
    chunks = tuple(base + 1 if i < remainder else base for i in range(count))
    if min_ul and chunks[-1] < min_ul:
        raise NutrientPlanError(
            PlanErrorCode.CHUNK_BELOW_MINIMUM,
            f"Teilauftrag {ul_to_ml(chunks[-1])} ml unterschreitet das Minimum {min_chunk_ml} ml "
            f"(Ziel {ul_to_ml(target_ul)} ml, Maximum {max_chunk_ml} ml).",
        )
    return chunks


# --- Snapshot-Eingang ------------------------------------------------------------------


def _coerce_snapshot(snapshot: Any) -> RunConfigSnapshot:
    if isinstance(snapshot, RunConfigSnapshot):
        return snapshot
    if isinstance(snapshot, Mapping):
        expected = {f.name for f in fields(RunConfigSnapshot)}
        given = set(snapshot.keys())
        problems = []
        if expected - given:
            problems.append(f"Fehlende Snapshot-Felder: {sorted(expected - given)}.")
        if given - expected:
            problems.append(f"Unbekannte Snapshot-Felder: {sorted(safe_repr(k) for k in given - expected)}.")
        if problems:
            raise NutrientPlanError(PlanErrorCode.INVALID_SNAPSHOT, problems)
        return RunConfigSnapshot(**dict(snapshot))
    raise NutrientPlanError(
        PlanErrorCode.INVALID_SNAPSHOT,
        f"RunConfigSnapshot oder dessen dict erwartet (war: Typ {type(snapshot).__name__}).",
    )


_FLOAT_FIELDS = (
    "target_ro_water_l",
    "max_mixer_liters",
    "target_ec_ms_cm",
    "nutrients_ml_per_100_l",
    "ec_adjustment_factor",
    "nutrient_a_percent",
    "nutrient_b_percent",
    "calculated_total_nutrients_ml",
    "calculated_nutrient_a_ml",
    "calculated_nutrient_b_ml",
    "estimated_process_volume_l",
)


def _numeric_message(name: str, value: Any, expected_type: type) -> str | None:
    """validate_numeric_field, ohne OverflowError bei nicht darstellbaren
    Ganzzahlen (z. B. 10**400)."""
    try:
        return validate_numeric_field(name, value, expected_type)
    except OverflowError:
        return f"'{name}' ist als Zahl nicht darstellbar (war: {safe_repr(value)})."


def _check_snapshot_types(snap: RunConfigSnapshot) -> None:
    errors: list[str] = []
    for name in _FLOAT_FIELDS:
        message = _numeric_message(name, getattr(snap, name), float)
        if message:
            errors.append(message)
    message = _numeric_message("ec_mixing_time_seconds", snap.ec_mixing_time_seconds, int)
    if message:
        errors.append(message)
    for name in ("nutrient_dosing_enabled", "legacy_dosing_needs_review"):
        value = getattr(snap, name)
        if type(value) is not bool:
            errors.append(f"'{name}' muss ein Wahrheitswert (true/false) sein (war: {safe_repr(value)}).")
    message = validate_strict_int_field("schema_version", snap.schema_version)
    if message:
        errors.append(message)
    elif not 1 <= snap.schema_version <= RECIPE_SCHEMA_VERSION:
        errors.append(
            f"'schema_version' muss in [1, {RECIPE_SCHEMA_VERSION}] liegen (war: {safe_repr(snap.schema_version)})."
        )
    for name in ("recipe_id", "recipe_name", "created_at"):
        if not isinstance(getattr(snap, name), str):
            errors.append(f"'{name}' muss ein String sein (war: {safe_repr(getattr(snap, name))}).")
    if not errors and not snap.recipe_id:
        errors.append("'recipe_id' darf nicht leer sein.")
    if errors:
        raise NutrientPlanError(PlanErrorCode.INVALID_TYPES, errors)


def _check_pump_roles(pump_roles: Any) -> None:
    canonical = dict(_CANONICAL_PUMP_ROLES)
    if not isinstance(pump_roles, Mapping) or dict(pump_roles) != canonical:
        raise NutrientPlanError(
            PlanErrorCode.INVALID_PUMP_ROLES,
            f"Pumpenzuordnung muss exakt {canonical} entsprechen (war: {safe_repr(pump_roles)}).",
        )


def _check_weights(pump_weights: Any) -> dict[str, float]:
    if not isinstance(pump_weights, Mapping) or set(pump_weights.keys()) != set(PUMP_ORDER):
        raise NutrientPlanError(
            PlanErrorCode.INVALID_WEIGHTS,
            f"Profilgewichte muessen genau fuer {list(PUMP_ORDER)} vorliegen (war: {safe_repr(pump_weights)}).",
        )
    result: dict[str, float] = {}
    errors: list[str] = []
    for pump in PUMP_ORDER:
        value = pump_weights[pump]
        if not is_finite_number(value) or value <= 0:
            errors.append(f"Profilgewicht {pump} muss eine endliche Zahl > 0 sein (war: {safe_repr(value)}).")
        else:
            result[pump] = float(value)
    if errors:
        raise NutrientPlanError(PlanErrorCode.INVALID_WEIGHTS, errors)
    return result


# --- Builder -----------------------------------------------------------------------------


def _build_plan(
    snapshot: RunConfigSnapshot | Mapping[str, Any],
    *,
    pump_roles: Mapping[str, str] | None = None,
    pump_weights: Mapping[str, float] | None = None,
    profile_id: str | None = None,
    max_chunk_ml: float | None = None,
    min_chunk_ml: float | None = None,
    prefill_target_l: float | None = None,
) -> NutrientDosingPlan:
    """Baut den unveraenderlichen Plan oder wirft ``NutrientPlanError``.

    Dosierung deaktiviert (``nutrient_dosing_enabled is False``): ein Plan ohne
    Pumpenziele und ohne Chunks (``has_dosing_work == False``); die
    Dosierparameter werden dann nicht benoetigt und nicht ausgewertet.

    Dosierung aktiviert: ``pump_roles``, ``pump_weights`` (vier Profilgewichte),
    ``profile_id``, ``max_chunk_ml`` und ``prefill_target_l`` sind Pflicht. Die
    Werte sind Eingabedaten; ihre physische Gueltigkeit prueft Stufe 2.
    """
    snap = _coerce_snapshot(snapshot)
    _check_snapshot_types(snap)

    recipe_errors = validate_recipe_values(
        target_ro_water_l=snap.target_ro_water_l,
        target_ec_ms_cm=snap.target_ec_ms_cm,
        ec_mixing_time_seconds=snap.ec_mixing_time_seconds,
        nutrients_ml_per_100_l=snap.nutrients_ml_per_100_l,
        nutrient_a_percent=snap.nutrient_a_percent,
        ec_adjustment_factor=snap.ec_adjustment_factor,
        nutrient_dosing_enabled=snap.nutrient_dosing_enabled,
        legacy_dosing_needs_review=snap.legacy_dosing_needs_review,
    )
    if recipe_errors:
        raise NutrientPlanError(PlanErrorCode.RECIPE_INVALID, recipe_errors)

    enabled = snap.nutrient_dosing_enabled

    # Nachrechnen mit den vorhandenen Domainfunktionen (keine zweite Formel).
    if enabled:
        total_ml = compute_total_nutrients_ml(snap.target_ro_water_l, snap.nutrients_ml_per_100_l, snap.ec_adjustment_factor)
        a_ml, b_ml = compute_nutrient_split(total_ml, snap.nutrient_a_percent)
    else:
        total_ml, a_ml, b_ml = 0.0, 0.0, 0.0

    mismatches = []
    for name, expected in (
        ("calculated_total_nutrients_ml", total_ml),
        ("calculated_nutrient_a_ml", a_ml),
        ("calculated_nutrient_b_ml", b_ml),
        ("nutrient_b_percent", nutrient_b_percent(snap.nutrient_a_percent)),
        (
            "estimated_process_volume_l",
            round_l(compute_base_volume_before_ro_correction_l(snap.target_ro_water_l, total_ml)),
        ),
    ):
        if getattr(snap, name) != expected:
            mismatches.append(f"Snapshot-Feld {name} = {safe_repr(getattr(snap, name))} widerspricht der Berechnung ({safe_repr(expected)}).")
    if mismatches:
        amounts_present = any(
            getattr(snap, name) != 0
            for name in ("calculated_total_nutrients_ml", "calculated_nutrient_a_ml", "calculated_nutrient_b_ml")
        )
        code = PlanErrorCode.DISABLED_BUT_AMOUNT if (not enabled and amounts_present) else PlanErrorCode.SNAPSHOT_MISMATCH
        raise NutrientPlanError(code, mismatches)

    try:
        total_ul = ml_to_ul(total_ml, "calculated_total_nutrients_ml")
        a_ul = ml_to_ul(a_ml, "calculated_nutrient_a_ml")
        b_ul = ml_to_ul(b_ml, "calculated_nutrient_b_ml")
    except VolumeModelError as exc:
        raise NutrientPlanError(PlanErrorCode.INVALID_AMOUNT, str(exc)) from exc
    if a_ul + b_ul != total_ul:
        raise NutrientPlanError(PlanErrorCode.SNAPSHOT_MISMATCH, "A + B entspricht nicht der Gesamtmenge.")

    if enabled and total_ul == 0:
        raise NutrientPlanError(
            PlanErrorCode.ENABLED_ZERO_AMOUNT,
            "Naehrstoffdosierung ist aktiviert, die berechnete Gesamtmenge ist 0 ml.",
        )

    try:
        volume = VolumePlan.create(
            target_ro_water_l=snap.target_ro_water_l,
            estimated_process_volume_l=snap.estimated_process_volume_l,
            planned_nutrient_ml=total_ml,
            max_mixer_l=snap.max_mixer_liters,
            prefill_target_l=prefill_target_l if enabled else None,
        )
    except VolumeModelError as exc:
        raise NutrientPlanError(PlanErrorCode.VOLUME_INVALID, f"{exc.reason.value}: {exc}") from exc

    targets: tuple[PumpTarget, ...] = ()
    used_profile: str | None = None
    used_max_chunk: float | None = None
    used_min_chunk: float | None = None

    if enabled:
        _check_pump_roles(pump_roles)
        weights = _check_weights(pump_weights)
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise NutrientPlanError(PlanErrorCode.INVALID_PROFILE_ID, f"profile_id muss ein nicht-leerer String sein (war: {safe_repr(profile_id)}).")
        if max_chunk_ml is None:
            raise NutrientPlanError(PlanErrorCode.INVALID_CHUNK_PARAMETER, "max_chunk_ml ist bei aktivierter Dosierung erforderlich.")

        solution_ul = {"A": a_ul, "B": b_ul}
        per_pump_ul: dict[str, int] = {}
        for solution, (first, second) in _CANONICAL_SOLUTION_PAIRS:
            per_pump_ul[first], per_pump_ul[second] = split_pair_ul(solution_ul[solution], weights[first], weights[second])

        built = []
        for pump in PUMP_ORDER:
            solution = "A" if pump in dict(_CANONICAL_SOLUTION_PAIRS)["A"] else "B"
            built.append(
                PumpTarget(
                    pump=pump,
                    role=dict(_CANONICAL_PUMP_ROLES)[pump],
                    solution=solution,
                    weight_ml_per_step=weights[pump],
                    target_ul=per_pump_ul[pump],
                    chunks_ul=plan_chunks_ul(per_pump_ul[pump], max_chunk_ml, min_chunk_ml),
                )
            )
        targets = tuple(built)

        if sum(t.target_ul for t in targets) != total_ul:  # Invariante, darf nie auftreten
            raise NutrientPlanError(PlanErrorCode.SNAPSHOT_MISMATCH, "Summe der Pumpenziele entspricht nicht der Gesamtmenge.")

        used_profile = profile_id.strip()
        used_max_chunk = float(max_chunk_ml)
        used_min_chunk = None if min_chunk_ml is None else float(min_chunk_ml)

    unhashed = NutrientDosingPlan(
        plan_version=PLAN_VERSION,
        recipe_id=snap.recipe_id,
        recipe_name=snap.recipe_name,
        snapshot_created_at=snap.created_at,
        snapshot_schema_version=snap.schema_version,
        dosing_enabled=enabled,
        controller=MCU_ID,
        profile_id=used_profile,
        total_ul=total_ul,
        a_ul=a_ul,
        b_ul=b_ul,
        pump_targets=targets,
        max_chunk_ml=used_max_chunk,
        min_chunk_ml=used_min_chunk,
        volume=volume,
        plan_hash="",
    )
    digest = _hash_core(unhashed.core_dict())
    return NutrientDosingPlan(**{**{f.name: getattr(unhashed, f.name) for f in fields(unhashed)}, "plan_hash": digest})


@functools.wraps(_build_plan)
def build_nutrient_dosing_plan(*args: Any, **kwargs: Any) -> NutrientDosingPlan:
    """Wie ``_build_plan`` (Signatur/Dokumentation uebernommen); zusaetzlich wird
    ein OverflowError aus nicht darstellbaren Zahlen nie nach aussen gereicht,
    sondern als ``NutrientPlanError(INVALID_AMOUNT)`` gemeldet."""
    try:
        return _build_plan(*args, **kwargs)
    except OverflowError as exc:
        raise NutrientPlanError(PlanErrorCode.INVALID_AMOUNT, f"Nicht darstellbare Zahl in den Eingaben: {exc}") from exc
