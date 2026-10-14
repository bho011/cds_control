"""Nachweismodell der MCU_B-Freigabe (AP-05 Stufe 2A): Datenmodelle, strenge
Parser und reine Hilfsfunktionen.

Dieses Modul bewertet nichts und gibt nichts frei. Es liest keine Datei, keine
Uhr, keinen Port: es prueft ausschliesslich uebergebene Daten und liefert
unveraenderliche, typisierte Objekte oder maschinenlesbare Sperrgruende
(``ParseResult``). Gate-Logik (Stufe 2C/2D) und Dateiladen (Stufe 2B) fehlen
hier bewusst.

Grundsaetze:

* Streng und fail-closed: falscher Typ, ``bool`` als Zahl, NaN/Inf, zu grosse
  Ganzzahlen, fehlende oder UNBEKANNTE Felder, unbekannte Schema-Versionen,
  Zeitstempel ohne UTC-Bezug, ungueltige Intervalle und ueberlange/leere
  Kennungen werden abgelehnt. Es gibt keine stillen Standardwerte fuer
  sicherheitsrelevante Grenzwerte (fehlende Policy-Werte sind ``None``).
* Eine Regelbasis, zwei Eingaenge: Konstruktoren der Modellobjekte (und die
  gemeinsamen ``c_*``-Pruefer) sind die einzige Quelle der Validierungsregeln;
  die Parser lesen Rohdaten nur strukturell und uebergeben sie an dieselben
  Konstruktoren. Ein ungueltiges Modellobjekt kann auch direkt nicht entstehen
  (``EvidenceModelError``); veraenderbare Eingaben (Listen, Mappings) werden in
  Tupel kopiert, Zahlen werden normalisiert (``1`` -> ``1.0``), Zeitpunkte auf
  UTC.
* Fingerprint-Semantik (B1): der Fingerprint gilt dem NORMALISIERTEN
  Inhalt (``to_dict()`` ohne ``fingerprint``), nicht der zufaelligen
  Darstellung der Eingabe. ``Z`` und ``+00:00``, verschiedene
  Sekundenbruchteil-Schreibweisen sowie ``1`` und ``1.0`` ergeben denselben
  Fingerprint; jede inhaltliche Aenderung ergibt einen anderen. Damit ist jeder
  akzeptierte Nachweis nach ``parse -> to_dict -> parse`` weiterhin gueltig,
  ohne Neusiegeln. Was an der Darstellung geaendert wird, ohne den Inhalt zu
  aendern, bleibt unerkannt (gewollt).
* Ergebnisobjekte sind tief unveraenderlich (frozen Dataclasses, Tupel); die
  Eingabedaten werden nie veraendert und nie referenziert behalten.
* Ein Fingerprint (SHA-256, ``unsigned_sha256``) schuetzt nur vor
  VERSEHENTLICHER Aenderung. Er ist keine Signatur und kein kryptografischer
  oder physischer Nachweis; ``assurance``/``origin`` benennen, WORAUF ein
  Eintrag beruht (menschliche Aussage, Hostmessung, Ableitung).
* ``EvaluationRules`` und die Herkunftsmarken beschreiben Produktiv- gegen
  Testregeln, erzeugen aber KEINE Berechtigung. Es gibt hier keine
  ``DispatchAuthorization``; die Sicherheit beruht nicht auf privaten
  Konstruktoren oder ``frozen``, sondern darauf, dass spaeter kein Sendeweg ein
  Objekt als Berechtigung annimmt (siehe Plan, Addendum B6).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Generic, Mapping, TypeVar

from services.peristaltic.models import VALID_CONTROLLERS, VALID_PUMPS

from .reasons import (
    BlockCode,
    BlockReason,
    EvidenceAssurance,
    EvidenceOrigin,
    GateLevel,
    IdentityLevel,
    PolicyKind,
    ReleaseScope,
    UnknownEnumValueError,
    parse_enum,
)

# --- Schema- und Grenzkonstanten (reine Parser-Grenzen, KEINE Sicherheitsgrenzwerte) ---------

EVIDENCE_SCHEMA_VERSION = 1
POLICY_SCHEMA_VERSION = 1
INTEGRITY_UNSIGNED_SHA256 = "unsigned_sha256"

MAX_ID_LENGTH = 128
MAX_TEXT_LENGTH = 500
MAX_PATH_LENGTH = 512
MAX_RECORDS = 10_000
MAX_REFERENCES = 64
MAX_LIST_ITEMS = 64
MAX_INT_BITS = 63  # Ganzzahlen werden auf 64-Bit-Bereich begrenzt (kontrollierte Ablehnung)
MAX_JSON_DEPTH = 16

DOMAIN_EVIDENCE = "cds-evidence-v1"
DOMAIN_POLICY = "cds-policy-v1"

RUNTIME_MEASUREMENT_V1 = "runtime_measurement_v1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}\Z")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}\Z")
_DOMAIN_RE = re.compile(r"^cds-[a-z]+-v[0-9]+(\|[a-z0-9_]+)*\Z")
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|\+00:00)\Z"
)
_FP_PREFIX = "sha256:"


class EvidenceKind(str, Enum):
    IDENTITY_ATTESTATION = "identity_attestation"
    FIRMWARE_IMAGE_ATTESTATION = "firmware_image_attestation"
    PUMP_VERIFICATION = "pump_verification"
    RUNTIME_OBSERVATION = "runtime_observation"
    WATER_ONLY_TEST = "water_only_test"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class RuntimeMeasurementDefinition:
    """Versionierte Messdefinition (Plan Addendum B7). Beginn unmittelbar vor
    dem seriellen DOSE-Schreiben, Ende beim Empfang des zugehoerigen DONE,
    monotone Host-Zeit. DONE ist ein Protokollereignis, kein Mengennachweis."""

    version: str
    clock: str
    start: str
    end: str


RUNTIME_MEASUREMENT_DEFINITIONS: Mapping[str, RuntimeMeasurementDefinition] = {
    RUNTIME_MEASUREMENT_V1: RuntimeMeasurementDefinition(
        version=RUNTIME_MEASUREMENT_V1,
        clock="monotonic_ns",
        start="before_dose_write",
        end="done_line_received_by_reader",
    )
}

# --- Fehlertypen und Ergebniscontainer ------------------------------------------------------


class EvidenceModelError(ValueError):
    """Ein direkt konstruiertes Modellobjekt ist ungueltig. ``problems`` enthaelt
    alle gefundenen Einzelprobleme als (BlockCode oder None, Text). Die Parser
    werfen NICHT, sie liefern ``ParseResult`` mit Sperrgruenden (dieselben
    Probleme, aus denselben Konstruktoren)."""

    def __init__(self, problems: "str | Any") -> None:
        if isinstance(problems, str):
            items: tuple[tuple[Any, str], ...] = ((None, problems),)
        else:
            items = tuple((code, message) for code, message in problems)
        super().__init__("; ".join(message for _, message in items))
        self.problems = items


class FingerprintError(ValueError):
    """Daten sind nicht kanonisch serialisierbar (kein Fingerprint moeglich)."""


T = TypeVar("T")


@dataclass(frozen=True)
class ParseResult(Generic[T]):
    value: T | None
    reasons: tuple[BlockReason, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.reasons and self.value is not None


# --- Fingerprint-Hilfen ----------------------------------------------------------------------


def is_fingerprint(value: Any) -> bool:
    if type(value) is not str or len(value) != len(_FP_PREFIX) + 64 or not value.startswith(_FP_PREFIX):
        return False
    return all(ch in "0123456789abcdef" for ch in value[len(_FP_PREFIX):])


def _check_plain(value: Any, path: str, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise FingerprintError(f"{path}: zu tief verschachtelt.")
    if value is None or type(value) in (str, bool):
        return
    if type(value) is int:
        if value.bit_length() > MAX_INT_BITS:
            raise FingerprintError(f"{path}: Ganzzahl ausserhalb des 64-Bit-Bereichs.")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise FingerprintError(f"{path}: nicht endliche Zahl.")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _check_plain(item, f"{path}[{index}]", depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise FingerprintError(f"{path}: Schluessel muss ein String sein.")
            if key == "fingerprint":
                raise FingerprintError(f"{path}: das Feld 'fingerprint' darf nicht Teil der gehashten Daten sein (Selbstreferenz).")
            _check_plain(item, f"{path}.{key}", depth + 1)
        return
    raise FingerprintError(f"{path}: Typ {type(value).__name__} ist nicht JSON-plain (erlaubt: dict, list, str, int, float, bool, None).")


def canonical_json(data: Any) -> bytes:
    """Kanonische JSON-Form: sortierte Schluessel, feste Trenner, ASCII,
    ``allow_nan=False``. Prueft die Eingabe vorab (nur JSON-plain, endliche
    Zahlen, 64-Bit-Ganzzahlen, kein Schluessel 'fingerprint'); veraendert sie nie."""
    _check_plain(data, "$", 0)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True).encode("ascii")


def compute_fingerprint(domain: str, data: Any) -> str:
    """``sha256:<hex>`` ueber ``domain \\x00 canonical_json(data)``. Die Domaene
    trennt Verwendungszwecke (Nachweis-Art, Policy-Art); ``data`` darf kein Feld
    'fingerprint' enthalten (keine Selbstreferenz). Unsigniert."""
    if type(domain) is not str or not _DOMAIN_RE.match(domain):
        raise FingerprintError(f"Ungueltige Fingerprint-Domaene: {domain!r}.")
    digest = hashlib.sha256(domain.encode("ascii") + b"\x00" + canonical_json(data)).hexdigest()
    return _FP_PREFIX + digest


def evidence_domain(kind: EvidenceKind) -> str:
    return f"{DOMAIN_EVIDENCE}|{kind.value}"


def policy_domain(kind: PolicyKind) -> str:
    return f"{DOMAIN_POLICY}|{kind.value}"




# --- Zeitstempel ----------------------------------------------------------------------------


def parse_utc_timestamp(value: Any, name: str = "Zeitstempel") -> datetime:
    """Nur ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` bzw. ``+00:00``. Kein naiver
    Zeitstempel, kein anderer Offset, keine Kalenderfehler."""
    if type(value) is not str or len(value) > 40:
        raise ValueError(f"{name} muss ein UTC-Zeitstempel-String sein.")
    match = _TS_RE.match(value)
    if match is None:
        raise ValueError(f"{name} muss das Format YYYY-MM-DDTHH:MM:SS[.ffffff]Z (UTC) haben.")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int((match.group(7) or "0").ljust(6, "0"))
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{name} ist kein gueltiger Zeitpunkt ({exc}).") from exc


def format_utc(moment: datetime) -> str:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("format_utc braucht einen Zeitpunkt mit UTC-Bezug.")
    # Bewusst ohne strftime: ``%Y`` ist plattformabhaengig (unter Linux ohne
    # fuehrende Nullen fuer Jahre < 1000); das Jahr ist stets vierstellig.
    base = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    return f"{base}.{moment.microsecond:06d}Z" if moment.microsecond else f"{base}Z"


# --- Skalare Pruefer (werfen _Bad) ------------------------------------------------------------


class _Bad(Exception):
    pass


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def c_ident(value: Any) -> str:
    if type(value) is not str or not _ID_RE.match(value):
        raise _Bad(f"muss eine Kennung sein (1-{MAX_ID_LENGTH} Zeichen aus A-Z a-z 0-9 _ . : -, erstes Zeichen alphanumerisch)")
    return value


def c_text(value: Any) -> str:
    if type(value) is not str or not value or len(value) > MAX_TEXT_LENGTH or _has_control(value):
        raise _Bad(f"muss ein nichtleerer Text ohne Steuerzeichen mit hoechstens {MAX_TEXT_LENGTH} Zeichen sein")
    return value


def c_path(value: Any) -> str:
    if (
        type(value) is not str
        or not value.startswith("/")
        or len(value) > MAX_PATH_LENGTH
        or _has_control(value)
        or ".." in value.split("/")
    ):
        raise _Bad(f"muss ein absoluter Pfad ohne '..' und Steuerzeichen mit hoechstens {MAX_PATH_LENGTH} Zeichen sein")
    return value


def c_int(value: Any, minimum: int = 0) -> int:
    if type(value) is not int:
        raise _Bad("muss eine echte Ganzzahl sein (kein bool, kein float)")
    if value.bit_length() > MAX_INT_BITS:
        raise _Bad("Ganzzahl liegt ausserhalb des 64-Bit-Bereichs")
    if value < minimum:
        raise _Bad(f"muss mindestens {minimum} sein")
    return value


def c_num(value: Any, *, positive: bool = True) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise _Bad("muss eine Zahl sein (kein bool, kein String)")
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise _Bad("Ganzzahl liegt ausserhalb des 64-Bit-Bereichs")
    number = float(value)
    if not math.isfinite(number):
        raise _Bad("muss eine endliche Zahl sein")
    if positive and number <= 0:
        raise _Bad("muss groesser als 0 sein")
    if not positive and number < 0:
        raise _Bad("darf nicht negativ sein")
    return number


def c_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise _Bad("muss ein echter Wahrheitswert sein")
    return value


def c_ts(value: Any) -> datetime:
    try:
        return parse_utc_timestamp(value, "Wert")
    except ValueError as exc:
        raise _Bad(str(exc)) from exc


def c_fp(value: Any) -> str:
    if not is_fingerprint(value):
        raise _Bad("muss ein Fingerprint 'sha256:<64 Kleinbuchstaben-Hex>' sein")
    return value


def c_commit(value: Any) -> str:
    if type(value) is not str or not _COMMIT_RE.match(value):
        raise _Bad("muss ein Commit-Hash aus 7-64 Hex-Kleinbuchstaben sein")
    return value


def c_controller(value: Any) -> str:
    if type(value) is not str or value not in VALID_CONTROLLERS:
        raise _Bad(f"muss einer von {list(VALID_CONTROLLERS)} sein")
    return value


def c_pump(value: Any) -> str:
    if type(value) is not str or value not in VALID_PUMPS:
        raise _Bad(f"muss eine Pumpe aus {list(VALID_PUMPS)} sein")
    return value


def c_pump_list(value: Any) -> tuple[str, ...]:
    if type(value) not in (list, tuple) or not value or len(value) > len(VALID_PUMPS):
        raise _Bad("muss eine nichtleere Liste von Pumpen sein")
    pumps = tuple(c_pump(item) for item in value)
    if list(pumps) != sorted(set(pumps)):
        raise _Bad("Pumpen muessen eindeutig und aufsteigend sortiert sein")
    return pumps


def c_enum(enum_cls: type[Enum]) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        try:
            return parse_enum(enum_cls, value)
        except UnknownEnumValueError as exc:
            raise _Bad(str(exc)) from exc

    return check


def c_const(expected: Any) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if type(value) is not type(expected) or value != expected:
            raise _Bad(f"muss exakt {expected!r} sein")
        return value

    return check


def c_dt(value: Any) -> datetime:
    """Zeitpunkt mit UTC-Bezug (kein naiver Zeitpunkt, kein anderer Offset);
    normalisiert auf ``timezone.utc``."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _Bad("muss ein Zeitpunkt mit UTC-Bezug sein")
    return value.astimezone(timezone.utc)


def c_member(enum_cls: type[Enum]) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if not isinstance(value, enum_cls):
            raise _Bad(f"muss ein {enum_cls.__name__} sein")
        return value

    return check


def c_instance(cls: type) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if type(value) is not cls:
            raise _Bad(f"muss exakt ein {cls.__name__} sein")
        return value

    return check


def c_seq(value: Any) -> tuple[Any, ...]:
    """Liste oder Tupel -> neues Tupel (keine Referenz auf die Eingabe)."""
    if type(value) not in (list, tuple):
        raise _Bad("muss eine Liste oder ein Tupel sein")
    return tuple(value)


def c_schema_version(supported: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        number = c_int(value, 0)
        if number != supported:
            raise _Bad(f"nicht unterstuetzte Schema-Version {number} (unterstuetzt: {supported})")
        return number

    return check


def c_fp_list(value: Any) -> tuple[str, ...]:
    items = c_seq(value)
    if len(items) > MAX_REFERENCES:
        raise _Bad(f"hoechstens {MAX_REFERENCES} Referenzen erlaubt")
    fingerprints = tuple(c_fp(item) for item in items)
    if len(set(fingerprints)) != len(fingerprints):
        raise _Bad("Referenzen muessen eindeutig sein")
    return fingerprints


def c_volume_list(value: Any) -> tuple[float, ...]:
    items = c_seq(value)
    if not items or len(items) > MAX_LIST_ITEMS:
        raise _Bad("muss eine nichtleere Liste positiver Zahlen sein")
    numbers = tuple(c_num(item) for item in items)
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise _Bad("muss streng aufsteigend sein")
    return numbers


def c_pump_values(value: Any) -> tuple[tuple[str, float], ...]:
    """Genau die vier Pumpen mit positiven endlichen Zahlen; als Mapping oder
    Paarfolge erlaubt, normalisiert zu einem Tupel in Pumpenreihenfolge."""
    if isinstance(value, Mapping):
        items = list(value.items())
    elif type(value) in (list, tuple):
        items = []
        for entry in value:
            if type(entry) not in (list, tuple) or len(entry) != 2:
                raise _Bad("muss (Pumpe, Wert)-Paare enthalten")
            items.append((entry[0], entry[1]))
    else:
        raise _Bad("muss ein Mapping oder eine Paarfolge sein")
    if len(items) != len(VALID_PUMPS) or sorted(str(key) for key, _ in items) != sorted(VALID_PUMPS) or not all(type(key) is str for key, _ in items):
        raise _Bad(f"muss genau die Pumpen {list(VALID_PUMPS)} enthalten")
    values = dict(items)
    result = []
    for pump in VALID_PUMPS:
        try:
            result.append((pump, c_num(values[pump])))
        except _Bad as exc:
            raise _Bad(f"{pump}: {exc}") from exc
    return tuple(result)


def _model_check(fn: Callable[..., Any], value: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(value, *args, **kwargs)
    except _Bad as exc:
        raise EvidenceModelError(f"{name}: {exc}") from exc


# --- Fehlersammler und Objektleser ---------------------------------------------------------------

_BAD: Any = object()


class _Errs:
    def __init__(self, default_code: BlockCode) -> None:
        self.default_code = default_code
        self.items: list[BlockReason] = []

    def add(self, path: str, message: str, code: BlockCode | None = None, pump: str | None = None) -> None:
        use = code or self.default_code
        text = f"{path}: {message}"
        self.items.append(BlockReason.create(use, text[:500], pump=pump))

    def __bool__(self) -> bool:
        return bool(self.items)


class _Obj:
    def __init__(self, data: dict[str, Any], path: str, errs: _Errs) -> None:
        self.data = data
        self.path = path
        self.errs = errs

    @classmethod
    def of(cls, value: Any, path: str, errs: _Errs) -> "_Obj | None":
        if type(value) is not dict:
            errs.add(path, f"muss ein Objekt sein (war: Typ {type(value).__name__})")
            return None
        return cls(value, path, errs)

    def keys(self, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> bool:
        ok = True
        present = set(self.data.keys())
        for key in present:
            if type(key) is not str:
                self.errs.add(self.path, "Schluessel muessen Strings sein")
                return False
        missing = [k for k in required if k not in present]
        unknown = sorted(present - set(required) - set(optional))
        if missing:
            self.errs.add(self.path, f"Pflichtfelder fehlen: {missing}")
            ok = False
        if unknown:
            self.errs.add(self.path, f"unbekannte Felder (werden nicht ignoriert): {unknown}")
            ok = False
        return ok

    def read(self, key: str, check: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if key not in self.data:
            return _BAD
        try:
            return check(self.data[key], *args, **kwargs)
        except _Bad as exc:
            self.errs.add(f"{self.path}.{key}", str(exc))
            return _BAD

    def raw(self, key: str) -> Any:
        return self.data.get(key, _BAD)


def _all_ok(*values: Any) -> bool:
    return all(value is not _BAD for value in values)


# --- EvaluationRules ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationRules:
    """Beschreibt, welche Policy-Art und welche Nachweisherkuenfte eine
    Auswertung zulaesst. Reine Daten: keine Berechtigung. ``PRODUCTION`` darf
    ``SYNTHETIC_TEST`` nie erlauben. Die konkreten Konstanten (PRODUCTION_RULES
    bzw. TEST_RULES) folgen in Stufe 2C/Tests."""

    kind: PolicyKind
    allowed_origins: frozenset[EvidenceOrigin]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PolicyKind):
            raise EvidenceModelError("EvaluationRules.kind muss eine PolicyKind sein.")
        if type(self.allowed_origins) is not frozenset or not self.allowed_origins:
            raise EvidenceModelError("allowed_origins muss ein nichtleeres frozenset sein.")
        if not all(isinstance(origin, EvidenceOrigin) for origin in self.allowed_origins):
            raise EvidenceModelError("allowed_origins darf nur EvidenceOrigin enthalten.")
        if self.kind is PolicyKind.PRODUCTION and EvidenceOrigin.SYNTHETIC_TEST in self.allowed_origins:
            raise EvidenceModelError("Produktivregeln duerfen SYNTHETIC_TEST nie erlauben.")

    def allows(self, origin: EvidenceOrigin) -> bool:
        return origin in self.allowed_origins


# --- Gemeinsame Regelbasis fuer Konstruktoren UND Parser -------------------------------------------------
#
# Jede Regel steht genau einmal: entweder in einem Skalarpruefer (c_*) oder in
# der __post_init__/``_normalize_*``-Logik des Modellobjekts. Die Parser lesen
# Rohdaten nur strukturell (Schluesselmenge, JSON-plain, Umwandlung von
# Enum-/Zeitstempel-Strings) und uebergeben die Werte an die Konstruktoren;
# Konstruktorfehler werden als Sperrgruende uebernommen. Dadurch koennen
# Parser und direkte Konstruktion einander nicht widersprechen.

Problem = tuple[BlockCode | None, str]


class _Problems:
    def __init__(self) -> None:
        self.items: list[Problem] = []

    def add(self, field: str, message: str, code: BlockCode | None = None) -> None:
        self.items.append((code, f"{field}: {message}" if field else message))

    def get(self, field: str, check: Callable[..., Any], value: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return check(value, *args, **kwargs)
        except _Bad as exc:
            self.add(field, str(exc))
            return _BAD

    def raise_if(self) -> None:
        if self.items:
            raise EvidenceModelError(self.items)


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _add_model_problems(errs: _Errs, path: str, exc: EvidenceModelError) -> None:
    for code, message in exc.problems:
        errs.add(path, message, code)


# --- Policy ------------------------------------------------------------------------------------------

_POLICY_COUNT_PARAMS = ("min_verification_trials", "min_observation_count")
_POLICY_NUMBER_PARAMS = (
    "max_verification_age_days",
    "max_ml_per_step_deviation",
    "max_trial_spread",
    "runtime_margin_ms",
    "identity_attestation_max_age_days",
    "firmware_image_max_age_days",
    "water_test_max_age_days",
    "release_max_age_days",
    "authorization_ttl_s",
)
POLICY_PARAMETER_NAMES: tuple[str, ...] = _POLICY_COUNT_PARAMS + _POLICY_NUMBER_PARAMS


@dataclass(frozen=True, slots=True)
class PolicyParameters:
    """Alle Grenzwerte der Freigabe-Policy. ``None`` bedeutet ausdruecklich
    'noch nicht festgelegt' - es gibt keine Standardwerte. Zaehlwerte sind
    echte Ganzzahlen >= 1; die uebrigen Werte positive endliche Zahlen und
    werden als ``float`` normalisiert (``1`` und ``1.0`` sind gleich)."""

    min_verification_trials: int | None = None
    max_verification_age_days: float | None = None
    max_ml_per_step_deviation: float | None = None
    max_trial_spread: float | None = None
    min_observation_count: int | None = None
    runtime_margin_ms: float | None = None
    identity_attestation_max_age_days: float | None = None
    firmware_image_max_age_days: float | None = None
    water_test_max_age_days: float | None = None
    release_max_age_days: float | None = None
    authorization_ttl_s: float | None = None

    def __post_init__(self) -> None:
        p = _Problems()
        norm: dict[str, Any] = {}
        for name in _POLICY_COUNT_PARAMS:
            value = getattr(self, name)
            norm[name] = None if value is None else p.get(name, c_int, value, 1)
        for name in _POLICY_NUMBER_PARAMS:
            value = getattr(self, name)
            norm[name] = None if value is None else p.get(name, c_num, value)
        p.raise_if()
        for name, value in norm.items():
            _set(self, name, value)

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in POLICY_PARAMETER_NAMES if getattr(self, name) is None)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in POLICY_PARAMETER_NAMES}


def _policy_body(schema_version: int, kind: PolicyKind, policy_id: str, revision: int, parameters: PolicyParameters) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "policy_kind": kind.value,
        "policy_id": policy_id,
        "revision": revision,
        "parameters": parameters.to_dict(),
    }


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    """``fingerprint`` ist der Fingerprint des normalisierten Inhalts (siehe
    Modul-Docstring) und wird beim Konstruieren gegen den Inhalt geprueft."""

    schema_version: int
    kind: PolicyKind
    policy_id: str
    revision: int
    parameters: PolicyParameters
    fingerprint: str

    def __post_init__(self) -> None:
        p = _Problems()
        p.get("schema_version", c_schema_version(POLICY_SCHEMA_VERSION), self.schema_version)
        kind = p.get("kind", c_member(PolicyKind), self.kind)
        policy_id = p.get("policy_id", c_ident, self.policy_id)
        revision = p.get("revision", c_int, self.revision, 1)
        parameters = p.get("parameters", c_instance(PolicyParameters), self.parameters)
        fingerprint = p.get("fingerprint", c_fp, self.fingerprint)
        if not p.items and _all_ok(kind, policy_id, revision, parameters, fingerprint):
            expected = compute_fingerprint(policy_domain(kind), _policy_body(self.schema_version, kind, policy_id, revision, parameters))
            if expected != fingerprint:
                p.add("fingerprint", "stimmt nicht mit dem normalisierten Inhalt der Policy ueberein")
        p.raise_if()

    @property
    def is_complete(self) -> bool:
        """Struktur gueltig UND alle Parameter gesetzt. Ein unvollstaendiger
        Entwurf ist strukturell gueltig; die Gate-Schicht blockiert ihn spaeter
        mit POLICY_INCOMPLETE (hier nur modelliert)."""
        return not self.parameters.missing()

    def missing_parameters(self) -> tuple[str, ...]:
        return self.parameters.missing()

    def to_dict(self) -> dict[str, Any]:
        """Quellform der Policy (ohne Fingerprint; der Fingerprint wird aus dem
        Inhalt berechnet und steht nicht in der Policy-Datei). Wieder
        parsebar: ``parse_policy(policy.to_dict())`` ergibt dieselbe Policy."""
        return _policy_body(self.schema_version, self.kind, self.policy_id, self.revision, self.parameters)


def parse_policy(data: Any) -> ParseResult[ReleasePolicy]:
    errs = _Errs(BlockCode.POLICY_INVALID)
    obj = _Obj.of(data, "policy", errs)
    if obj is None:
        return ParseResult(None, tuple(errs.items))
    obj.keys(("schema_version", "policy_kind", "policy_id", "revision", "parameters"))
    version = obj.read("schema_version", c_schema_version(POLICY_SCHEMA_VERSION))
    kind = obj.read("policy_kind", c_enum(PolicyKind))
    policy_id = obj.read("policy_id", c_ident)
    revision = obj.read("revision", c_int, 1)
    params: Any = _BAD
    params_obj = _Obj.of(obj.raw("parameters"), "policy.parameters", errs) if "parameters" in obj.data else None
    if params_obj is not None and params_obj.keys(POLICY_PARAMETER_NAMES):
        try:
            params = PolicyParameters(**{name: params_obj.data[name] for name in POLICY_PARAMETER_NAMES})
        except EvidenceModelError as exc:
            _add_model_problems(errs, "policy.parameters", exc)
    if errs or not _all_ok(version, kind, policy_id, revision, params):
        return ParseResult(None, tuple(errs.items))
    fingerprint = compute_fingerprint(policy_domain(kind), _policy_body(version, kind, policy_id, revision, params))
    try:
        return ParseResult(ReleasePolicy(version, kind, policy_id, revision, params, fingerprint))
    except EvidenceModelError as exc:
        _add_model_problems(errs, "policy", exc)
        return ParseResult(None, tuple(errs.items))


def policy_fingerprint(raw: Mapping[str, Any]) -> str:
    """Fingerprint des NORMALISIERTEN Policy-Inhalts (``1`` und ``1.0`` gleich)."""
    result = parse_policy(raw)
    if not result.ok:
        raise FingerprintError("; ".join(reason.message for reason in result.reasons[:3]))
    return result.value.fingerprint


# --- Nachweis-Nutzlasten ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceSubject:
    controller: str
    pump: str | None
    mapping_role: str | None
    profile_id: str
    profile_fingerprint: str

    def __post_init__(self) -> None:
        p = _Problems()
        p.get("controller", c_controller, self.controller)
        if self.pump is not None:
            p.get("pump", c_pump, self.pump)
        if self.mapping_role is not None:
            p.get("mapping_role", c_ident, self.mapping_role)
        p.get("profile_id", c_ident, self.profile_id)
        p.get("profile_fingerprint", c_fp, self.profile_fingerprint)
        p.raise_if()

    def to_dict(self) -> dict[str, Any]:
        return {
            "controller": self.controller,
            "pump": self.pump,
            "mapping_role": self.mapping_role,
            "profile_id": self.profile_id,
            "profile_fingerprint": self.profile_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class IdentityAttestationPayload:
    """Menschliche Aussage 'dieser by-path-Anschluss ist MCU_B'. Kein
    Geraetenachweis (beide CH340 tragen dieselbe USB-ID). Als Dateinachweis ist
    nur ``port_attested`` zulaessig; ``firmware_proven`` kann nur eine Sitzung
    zur Laufzeit liefern."""

    level: IdentityLevel
    port_path: str
    attested_by: str
    statement: str

    def __post_init__(self) -> None:
        p = _Problems()
        if not isinstance(self.level, IdentityLevel):
            p.add("level", "muss eine IdentityLevel sein")
        elif self.level is not IdentityLevel.PORT_ATTESTED:
            p.add("level", "als Dateinachweis ist nur 'port_attested' zulaessig (firmware_proven braucht eine Firmware-ID zur Laufzeit)")
        p.get("port_path", c_path, self.port_path)
        p.get("attested_by", c_ident, self.attested_by)
        p.get("statement", c_text, self.statement)
        p.raise_if()

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.value, "port_path": self.port_path, "attested_by": self.attested_by, "statement": self.statement}


@dataclass(frozen=True, slots=True)
class FirmwareImageAttestationPayload:
    """Menschliche Aussage ueber das geflashte Image; kein Readback-Nachweis.
    ``pump_ml_per_step`` darf als Mapping oder Paarfolge uebergeben werden und
    wird zu einem Tupel in Pumpenreihenfolge normalisiert."""

    profile_id: str
    firmware_source_commit: str
    steps_per_rev: int
    pump_ml_per_step: tuple[tuple[str, float], ...]
    attested_by: str
    statement: str

    def __post_init__(self) -> None:
        p = _Problems()
        p.get("profile_id", c_ident, self.profile_id)
        p.get("firmware_source_commit", c_commit, self.firmware_source_commit)
        p.get("steps_per_rev", c_int, self.steps_per_rev, 1)
        pairs = p.get("pump_ml_per_step", c_pump_values, self.pump_ml_per_step)
        p.get("attested_by", c_ident, self.attested_by)
        p.get("statement", c_text, self.statement)
        p.raise_if()
        _set(self, "pump_ml_per_step", pairs)

    def value_for(self, pump: str) -> float:
        return dict(self.pump_ml_per_step)[pump]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "firmware_source_commit": self.firmware_source_commit,
            "steps_per_rev": self.steps_per_rev,
            "pump_ml_per_step": {pump: value for pump, value in self.pump_ml_per_step},
            "attested_by": self.attested_by,
            "statement": self.statement,
        }


@dataclass(frozen=True, slots=True)
class PumpVerificationPayload:
    purpose: str
    firmware_ml_per_step_used: float
    verified_ml_per_step: float
    trial_count: int
    verified_at_ml: tuple[float, ...]
    trials_digest: str

    def __post_init__(self) -> None:
        p = _Problems()
        p.get("purpose", c_const("verification"), self.purpose)
        used = p.get("firmware_ml_per_step_used", c_num, self.firmware_ml_per_step_used)
        verified = p.get("verified_ml_per_step", c_num, self.verified_ml_per_step)
        p.get("trial_count", c_int, self.trial_count, 1)
        volumes = p.get("verified_at_ml", c_volume_list, self.verified_at_ml)
        p.get("trials_digest", c_fp, self.trials_digest)
        p.raise_if()
        _set(self, "firmware_ml_per_step_used", used)
        _set(self, "verified_ml_per_step", verified)
        _set(self, "verified_at_ml", volumes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "firmware_ml_per_step_used": self.firmware_ml_per_step_used,
            "verified_ml_per_step": self.verified_ml_per_step,
            "trial_count": self.trial_count,
            "verified_at_ml": list(self.verified_at_ml),
            "trials_digest": self.trials_digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeObservationPayload:
    """Eine erfolgreich beendete Dosis (DONE) mit Hostzeit-Messung nach
    ``runtime_measurement_v1``. Laeufe mit Timeout, Abbruch, Desync, Reset
    oder ohne DONE sind keine gueltige Evidenz. DONE ist kein Mengennachweis."""

    definition_version: str
    clock: str
    start: str
    end: str
    requested_ml: float
    elapsed_ms: int
    outcome: str
    boot_reset_observed: bool
    declared_concurrent_pumps: tuple[str, ...]
    derived_concurrent_pumps: tuple[str, ...]
    tool_version: str

    def __post_init__(self) -> None:
        p = _Problems()
        definition = RUNTIME_MEASUREMENT_DEFINITIONS.get(self.definition_version) if type(self.definition_version) is str else None
        if definition is None:
            shown = self.definition_version[:60] if type(self.definition_version) is str else type(self.definition_version).__name__
            p.add(
                "measurement.definition_version",
                f"unbekannte Messdefinition {shown!r} (unterstuetzt: {sorted(RUNTIME_MEASUREMENT_DEFINITIONS)})",
                BlockCode.RUNTIME_MEASUREMENT_DEFINITION_UNSUPPORTED,
            )
        else:
            for field, expected in (("clock", definition.clock), ("start", definition.start), ("end", definition.end)):
                p.get(f"measurement.{field}", c_const(expected), getattr(self, field))
        requested = p.get("requested_ml", c_num, self.requested_ml)
        p.get("elapsed_ms", c_int, self.elapsed_ms, 1)
        if type(self.outcome) is not str or self.outcome != "DONE":
            p.add("outcome", "nur ein erfolgreich beendeter Lauf (DONE) ist gueltige Laufzeitevidenz", BlockCode.RUNTIME_EVIDENCE_INVALID_OUTCOME)
        if type(self.boot_reset_observed) is not bool:
            p.add("boot_reset_observed", "muss ein echter Wahrheitswert sein")
        elif self.boot_reset_observed:
            p.add("boot_reset_observed", "ein MCU-Reset waehrend des Laufs macht die Beobachtung ungueltig", BlockCode.RUNTIME_EVIDENCE_INVALID_OUTCOME)
        declared = p.get("declared_concurrent_pumps", c_pump_list, self.declared_concurrent_pumps)
        derived = p.get("derived_concurrent_pumps", c_pump_list, self.derived_concurrent_pumps)
        if _all_ok(declared, derived) and declared != derived:
            p.add("concurrent_pumps", "deklarierte und abgeleitete Parallelgruppe stimmen nicht ueberein", BlockCode.RUNTIME_GROUP_DECLARATION_MISMATCH)
        p.get("tool_version", c_ident, self.tool_version)
        p.raise_if()
        _set(self, "requested_ml", requested)
        _set(self, "declared_concurrent_pumps", declared)
        _set(self, "derived_concurrent_pumps", derived)

    def to_dict(self) -> dict[str, Any]:
        return {
            "measurement": {
                "definition_version": self.definition_version,
                "clock": self.clock,
                "start": self.start,
                "end": self.end,
            },
            "requested_ml": self.requested_ml,
            "elapsed_ms": self.elapsed_ms,
            "outcome": self.outcome,
            "boot_reset_observed": self.boot_reset_observed,
            "declared_concurrent_pumps": list(self.declared_concurrent_pumps),
            "derived_concurrent_pumps": list(self.derived_concurrent_pumps),
            "tool_version": self.tool_version,
        }


@dataclass(frozen=True, slots=True)
class WaterOnlyTestPayload:
    test_id: str
    pumps_tested: tuple[str, ...]
    max_chunk_ml: float
    result: str
    water_only_confirmed: bool
    attested_by: str
    statement: str

    def __post_init__(self) -> None:
        p = _Problems()
        p.get("test_id", c_ident, self.test_id)
        pumps = p.get("pumps_tested", c_pump_list, self.pumps_tested)
        chunk = p.get("max_chunk_ml", c_num, self.max_chunk_ml)
        p.get("result", c_const("passed"), self.result)
        p.get("water_only_confirmed", c_const(True), self.water_only_confirmed)
        p.get("attested_by", c_ident, self.attested_by)
        p.get("statement", c_text, self.statement)
        p.raise_if()
        _set(self, "pumps_tested", pumps)
        _set(self, "max_chunk_ml", chunk)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "pumps_tested": list(self.pumps_tested),
            "max_chunk_ml": self.max_chunk_ml,
            "result": self.result,
            "water_only_confirmed": self.water_only_confirmed,
            "attested_by": self.attested_by,
            "statement": self.statement,
        }


@dataclass(frozen=True, slots=True)
class ReleasePayload:
    """Menschliche Freigabeaussage, gebunden an den Konfigurations-Fingerprint.
    Beweist nichts allein; erst die Gates (Stufe 2C/2D) bewerten sie."""

    scope: ReleaseScope
    config_fingerprint: str
    granted_by: str
    granted_at: datetime
    statement: str

    def __post_init__(self) -> None:
        p = _Problems()
        if not isinstance(self.scope, ReleaseScope):
            p.add("scope", "muss ein ReleaseScope sein")
        elif self.scope is ReleaseScope.SIMULATION:
            p.add("scope", "ein Freigabeeintrag darf nicht den Scope 'simulation' haben", BlockCode.RELEASE_RECORD_INVALID)
        p.get("config_fingerprint", c_fp, self.config_fingerprint)
        p.get("granted_by", c_ident, self.granted_by)
        granted = p.get("granted_at", c_dt, self.granted_at)
        p.get("statement", c_text, self.statement)
        p.raise_if()
        _set(self, "granted_at", granted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "config_fingerprint": self.config_fingerprint,
            "granted_by": self.granted_by,
            "granted_at": format_utc(self.granted_at),
            "statement": self.statement,
        }


Payload = (
    IdentityAttestationPayload
    | FirmwareImageAttestationPayload
    | PumpVerificationPayload
    | RuntimeObservationPayload
    | WaterOnlyTestPayload
    | ReleasePayload
)

_PAYLOAD_TYPES: Mapping[EvidenceKind, type] = {
    EvidenceKind.IDENTITY_ATTESTATION: IdentityAttestationPayload,
    EvidenceKind.FIRMWARE_IMAGE_ATTESTATION: FirmwareImageAttestationPayload,
    EvidenceKind.PUMP_VERIFICATION: PumpVerificationPayload,
    EvidenceKind.RUNTIME_OBSERVATION: RuntimeObservationPayload,
    EvidenceKind.WATER_ONLY_TEST: WaterOnlyTestPayload,
    EvidenceKind.RELEASE: ReleasePayload,
}

ATTESTATION_KINDS = frozenset(
    {EvidenceKind.IDENTITY_ATTESTATION, EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, EvidenceKind.WATER_ONLY_TEST, EvidenceKind.RELEASE}
)
_PUMP_REQUIRED_KINDS = frozenset({EvidenceKind.PUMP_VERIFICATION, EvidenceKind.RUNTIME_OBSERVATION})
_ROLE_REQUIRED_KINDS = frozenset({EvidenceKind.PUMP_VERIFICATION})


def origin_assurance_problem(kind: EvidenceKind, origin: EvidenceOrigin, assurance: EvidenceAssurance) -> str | None:
    """Reine Konsistenzregel (None = in Ordnung). Menschliche Attestierungen
    sind immer ``attested`` und nie ``measured``/``derived``; Messungen und
    Ableitungen nie ``attested``. ``SYNTHETIC_TEST`` folgt denselben
    Assurance-Regeln, ist aber ueber die Regeln (EvaluationRules) fuer
    Produktion ausgeschlossen."""
    if kind in ATTESTATION_KINDS:
        if assurance is not EvidenceAssurance.ATTESTED:
            return f"{kind.value} muss assurance=attested haben"
        if origin not in (EvidenceOrigin.HUMAN_ATTESTATION, EvidenceOrigin.SYNTHETIC_TEST):
            return f"{kind.value} darf nur origin human_attestation oder synthetic_test haben"
        return None
    if kind is EvidenceKind.RUNTIME_OBSERVATION:
        if assurance is not EvidenceAssurance.MEASURED:
            return "runtime_observation muss assurance=measured haben"
        if origin not in (EvidenceOrigin.HOST_MEASURED, EvidenceOrigin.SYNTHETIC_TEST):
            return "runtime_observation darf nur origin host_measured oder synthetic_test haben"
        return None
    # PUMP_VERIFICATION
    if assurance is not EvidenceAssurance.DERIVED:
        return "pump_verification muss assurance=derived haben"
    if origin not in (EvidenceOrigin.DERIVED, EvidenceOrigin.SYNTHETIC_TEST):
        return "pump_verification darf nur origin derived oder synthetic_test haben"
    return None


# --- EvidenceRecord / EvidenceFile ------------------------------------------------------------------------

_RECORD_KEYS = (
    "record_id", "kind", "origin", "assurance", "integrity", "created_utc",
    "valid_from_utc", "valid_until_utc", "subject", "payload", "references", "fingerprint",
)
_SUBJECT_KEYS = ("controller", "pump", "mapping_role", "profile_id", "profile_fingerprint")
_RECORD_FIELDS = (
    "record_id", "kind", "origin", "assurance", "created_utc", "valid_from_utc", "valid_until_utc",
    "subject", "payload", "references",
)


def _normalize_record(fields: Mapping[str, Any]) -> tuple[dict[str, Any], list[Problem]]:
    """Gemeinsame Pruefung und Normalisierung aller Feldwerte eines
    Nachweiseintrags (ohne Fingerprint). Wird vom Konstruktor, vom Parser und
    vom Siegeln verwendet."""
    p = _Problems()
    record_id = p.get("record_id", c_ident, fields["record_id"])
    kind = p.get("kind", c_member(EvidenceKind), fields["kind"])
    origin = p.get("origin", c_member(EvidenceOrigin), fields["origin"])
    assurance = p.get("assurance", c_member(EvidenceAssurance), fields["assurance"])
    created = p.get("created_utc", c_dt, fields["created_utc"])
    valid_from = p.get("valid_from_utc", c_dt, fields["valid_from_utc"])
    valid_until: Any = None if fields["valid_until_utc"] is None else p.get("valid_until_utc", c_dt, fields["valid_until_utc"])
    subject = p.get("subject", c_instance(EvidenceSubject), fields["subject"])
    payload: Any = _BAD
    if kind is not _BAD:
        payload = p.get("payload", c_instance(_PAYLOAD_TYPES[kind]), fields["payload"])
    references = p.get("references", c_fp_list, fields["references"])
    if _all_ok(kind, origin, assurance):
        problem = origin_assurance_problem(kind, origin, assurance)
        if problem:
            p.add("", problem)
        if valid_until is None and assurance is EvidenceAssurance.ATTESTED:
            p.add("valid_until_utc", "eine Attestierung braucht ein Ablaufdatum (valid_until_utc darf nicht null sein)")
    if _all_ok(valid_from, valid_until) and valid_until is not None and valid_until <= valid_from:
        p.add("valid_until_utc", "muss nach valid_from_utc liegen (ungueltiges Zeitintervall)")
    if _all_ok(kind, subject):
        if subject.pump is None and kind in _PUMP_REQUIRED_KINDS:
            p.add("subject.pump", f"fuer {kind.value} ist eine Pumpe Pflicht")
        if subject.pump is not None and kind not in _PUMP_REQUIRED_KINDS:
            p.add("subject.pump", f"{kind.value} darf keine Pumpe im Subjekt tragen")
        if subject.mapping_role is None and kind in _ROLE_REQUIRED_KINDS:
            p.add("subject.mapping_role", f"fuer {kind.value} ist die Mapping-Rolle Pflicht")
        if subject.mapping_role is not None and kind in ATTESTATION_KINDS:
            p.add("subject.mapping_role", f"{kind.value} darf keine Mapping-Rolle tragen")
    if _all_ok(kind, references) and kind is EvidenceKind.RELEASE and not references:
        p.add("references", "ein Freigabeeintrag braucht mindestens eine Referenz", BlockCode.RELEASE_RECORD_INVALID)
    if _all_ok(kind, subject, payload):
        if kind is EvidenceKind.FIRMWARE_IMAGE_ATTESTATION and payload.profile_id != subject.profile_id:
            p.add("payload.profile_id", "muss mit subject.profile_id uebereinstimmen")
        if kind is EvidenceKind.RUNTIME_OBSERVATION and subject.pump is not None and subject.pump not in payload.declared_concurrent_pumps:
            p.add("payload", "die Pumpe des Nachweises muss Teil der Parallelgruppe sein")
    norm = {
        "record_id": record_id, "kind": kind, "origin": origin, "assurance": assurance, "created_utc": created,
        "valid_from_utc": valid_from, "valid_until_utc": valid_until, "subject": subject, "payload": payload,
        "references": references,
    }
    return norm, p.items


def _record_body(norm: Mapping[str, Any]) -> dict[str, Any]:
    """Normalisierter, fingerprint-freier Inhalt eines Eintrags - exakt das,
    was gehasht wird (siehe Modul-Docstring)."""
    return {
        "record_id": norm["record_id"],
        "kind": norm["kind"].value,
        "origin": norm["origin"].value,
        "assurance": norm["assurance"].value,
        "integrity": INTEGRITY_UNSIGNED_SHA256,
        "created_utc": format_utc(norm["created_utc"]),
        "valid_from_utc": format_utc(norm["valid_from_utc"]),
        "valid_until_utc": None if norm["valid_until_utc"] is None else format_utc(norm["valid_until_utc"]),
        "subject": norm["subject"].to_dict(),
        "payload": norm["payload"].to_dict(),
        "references": list(norm["references"]),
    }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Ein geprueftes Nachweis-Objekt. Der Konstruktor validiert ALLES (Typen,
    Zeitintervall, Herkunft/Assurance, Subjekt-/Nutzlast-Konsistenz,
    Fingerprint-Format UND Uebereinstimmung des Fingerprints mit dem
    normalisierten Inhalt) und kopiert veraenderbare Eingaben in Tupel. Ein
    ungueltiges Objekt kann nicht entstehen (``EvidenceModelError``). Das
    beweist keine Berechtigung: es ist eine unsignierte Integritaetspruefung."""

    record_id: str
    kind: EvidenceKind
    origin: EvidenceOrigin
    assurance: EvidenceAssurance
    created_utc: datetime
    valid_from_utc: datetime
    valid_until_utc: datetime | None
    subject: EvidenceSubject
    payload: Payload
    references: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        norm, problems = _normalize_record({name: getattr(self, name) for name in _RECORD_FIELDS})
        p = _Problems()
        p.items.extend(problems)
        fingerprint = p.get("fingerprint", c_fp, self.fingerprint)
        if not p.items and fingerprint is not _BAD:
            expected = compute_fingerprint(evidence_domain(norm["kind"]), _record_body(norm))
            if expected != fingerprint:
                code = BlockCode.RELEASE_FINGERPRINT_MISMATCH if norm["kind"] is EvidenceKind.RELEASE else BlockCode.EVIDENCE_FINGERPRINT_MISMATCH
                p.add("fingerprint", "stimmt nicht mit dem normalisierten Inhalt ueberein (unsignierte Integritaetspruefung)", code)
        p.raise_if()
        for name in _RECORD_FIELDS:
            _set(self, name, norm[name])

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data = _record_body({name: getattr(self, name) for name in _RECORD_FIELDS})
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    """Kompletter lokaler Nachweisbestand. Der Konstruktor prueft Schema-
    Version, eindeutige IDs/Fingerprints und die Referenzkette."""

    schema_version: int
    records: tuple[EvidenceRecord, ...]

    def __post_init__(self) -> None:
        p = _Problems()
        p.get("schema_version", c_schema_version(EVIDENCE_SCHEMA_VERSION), self.schema_version)
        records = p.get("records", c_seq, self.records)
        if records is not _BAD:
            if len(records) > MAX_RECORDS:
                p.add("records", f"hoechstens {MAX_RECORDS} Eintraege erlaubt")
            elif not all(type(record) is EvidenceRecord for record in records):
                p.add("records", "darf nur EvidenceRecord enthalten")
            else:
                ids: set[str] = set()
                fingerprints: set[str] = set()
                for record in records:
                    if record.record_id in ids:
                        p.add("records", f"doppelte record_id {record.record_id!r}")
                    if record.fingerprint in fingerprints:
                        p.add("records", f"doppelter Fingerprint bei {record.record_id!r}")
                    ids.add(record.record_id)
                    fingerprints.add(record.fingerprint)
                for record in records:
                    for ref in record.references:
                        if ref not in fingerprints:
                            p.add(f"records[{record.record_id}]", "Referenz verweist auf keinen vorhandenen Eintrag (Kette gebrochen)", BlockCode.EVIDENCE_CHAIN_BROKEN)
        p.raise_if()
        _set(self, "records", tuple(records))

    def records_of_kind(self, kind: EvidenceKind) -> tuple[EvidenceRecord, ...]:
        return tuple(record for record in self.records if record.kind is kind)

    def by_fingerprint(self, fingerprint: str) -> EvidenceRecord | None:
        for record in self.records:
            if record.fingerprint == fingerprint:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "records": [record.to_dict() for record in self.records]}


# --- Parser: Rohdaten strukturell lesen, Werte an die Konstruktoren uebergeben -------------------------

_PAYLOAD_KEYS: Mapping[EvidenceKind, tuple[str, ...]] = {
    EvidenceKind.IDENTITY_ATTESTATION: ("level", "port_path", "attested_by", "statement"),
    EvidenceKind.FIRMWARE_IMAGE_ATTESTATION: ("profile_id", "firmware_source_commit", "steps_per_rev", "pump_ml_per_step", "attested_by", "statement"),
    EvidenceKind.PUMP_VERIFICATION: ("purpose", "firmware_ml_per_step_used", "verified_ml_per_step", "trial_count", "verified_at_ml", "trials_digest"),
    EvidenceKind.RUNTIME_OBSERVATION: (
        "measurement", "requested_ml", "elapsed_ms", "outcome", "boot_reset_observed",
        "declared_concurrent_pumps", "derived_concurrent_pumps", "tool_version",
    ),
    EvidenceKind.WATER_ONLY_TEST: ("test_id", "pumps_tested", "max_chunk_ml", "result", "water_only_confirmed", "attested_by", "statement"),
    EvidenceKind.RELEASE: ("scope", "config_fingerprint", "granted_by", "granted_at", "statement"),
}
_MEASUREMENT_KEYS = ("definition_version", "clock", "start", "end")


def _construct(cls: type, kwargs: Mapping[str, Any], path: str, errs: _Errs) -> Any:
    if any(value is _BAD for value in kwargs.values()):
        return None
    try:
        return cls(**kwargs)
    except EvidenceModelError as exc:
        _add_model_problems(errs, path, exc)
        return None


def _build_payload(kind: EvidenceKind, obj: _Obj) -> Any:
    obj.keys(_PAYLOAD_KEYS[kind])
    kwargs: dict[str, Any] = {key: obj.raw(key) for key in _PAYLOAD_KEYS[kind]}
    if kind is EvidenceKind.IDENTITY_ATTESTATION:
        kwargs["level"] = obj.read("level", c_enum(IdentityLevel))
    elif kind is EvidenceKind.FIRMWARE_IMAGE_ATTESTATION:
        pumps = _Obj.of(kwargs["pump_ml_per_step"], f"{obj.path}.pump_ml_per_step", obj.errs) if kwargs["pump_ml_per_step"] is not _BAD else None
        if pumps is None or not pumps.keys(tuple(VALID_PUMPS)):
            kwargs["pump_ml_per_step"] = _BAD
    elif kind is EvidenceKind.RUNTIME_OBSERVATION:
        measured: dict[str, Any] = {key: _BAD for key in _MEASUREMENT_KEYS}
        measurement = _Obj.of(obj.raw("measurement"), f"{obj.path}.measurement", obj.errs) if "measurement" in obj.data else None
        if measurement is not None and measurement.keys(_MEASUREMENT_KEYS):
            measured = {key: measurement.raw(key) for key in _MEASUREMENT_KEYS}
        kwargs = {**measured, **{key: obj.raw(key) for key in _PAYLOAD_KEYS[kind] if key != "measurement"}}
    elif kind is EvidenceKind.RELEASE:
        kwargs["scope"] = obj.read("scope", c_enum(ReleaseScope))
        kwargs["granted_at"] = obj.read("granted_at", c_ts)
    return _construct(_PAYLOAD_TYPES[kind], kwargs, f"{obj.path}", obj.errs)


def _raw_to_fields(raw: Any, path: str, errs: _Errs, *, expect_fingerprint: bool) -> dict[str, Any] | None:
    """Liest einen rohen Eintrag strukturell (Schluesselmenge, JSON-plain,
    Umwandlung von Enum-/Zeitstempel-Strings) und baut Subjekt und Nutzlast
    ueber deren Konstruktoren. Liefert die Feldwerte (ohne Fingerprint)."""
    obj = _Obj.of(raw, path, errs)
    if obj is None:
        return None
    before = len(errs.items)
    try:
        _check_plain({k: v for k, v in raw.items() if k != "fingerprint"}, path, 0)
    except FingerprintError as exc:
        errs.add(path, str(exc))
        return None
    obj.keys(_RECORD_KEYS if expect_fingerprint else tuple(k for k in _RECORD_KEYS if k != "fingerprint"))
    obj.read("integrity", c_const(INTEGRITY_UNSIGNED_SHA256))
    kind = obj.read("kind", c_enum(EvidenceKind))
    origin = obj.read("origin", c_enum(EvidenceOrigin))
    assurance = obj.read("assurance", c_enum(EvidenceAssurance))
    created = obj.read("created_utc", c_ts)
    valid_from = obj.read("valid_from_utc", c_ts)
    valid_until: Any = _BAD
    if "valid_until_utc" in obj.data:
        valid_until = None if obj.data["valid_until_utc"] is None else obj.read("valid_until_utc", c_ts)
    subject: Any = _BAD
    if "subject" in obj.data:
        subject_obj = _Obj.of(obj.raw("subject"), f"{path}.subject", errs)
        if subject_obj is not None:
            subject_obj.keys(_SUBJECT_KEYS)
            subject = _construct(EvidenceSubject, {key: subject_obj.raw(key) for key in _SUBJECT_KEYS}, f"{path}.subject", errs)
            subject = _BAD if subject is None else subject
    payload: Any = _BAD
    if kind is not _BAD and "payload" in obj.data:
        payload_obj = _Obj.of(obj.raw("payload"), f"{path}.payload", errs)
        if payload_obj is not None:
            payload = _build_payload(kind, payload_obj)
            payload = _BAD if payload is None else payload
    fields = {
        "record_id": obj.raw("record_id"), "kind": kind, "origin": origin, "assurance": assurance,
        "created_utc": created, "valid_from_utc": valid_from, "valid_until_utc": valid_until,
        "subject": subject, "payload": payload, "references": obj.raw("references"),
    }
    if len(errs.items) > before or any(value is _BAD for value in fields.values()):
        return None
    return fields


def _fingerprint_of_raw(raw_without_fingerprint: Mapping[str, Any]) -> str:
    errs = _Errs(BlockCode.EVIDENCE_INVALID)
    fields = _raw_to_fields(dict(raw_without_fingerprint), "record", errs, expect_fingerprint=False)
    if fields is None:
        raise FingerprintError("; ".join(reason.message for reason in errs.items[:3]) or "ungueltiger Nachweiseintrag")
    norm, problems = _normalize_record(fields)
    if problems:
        raise FingerprintError("; ".join(message for _, message in problems[:3]))
    return compute_fingerprint(evidence_domain(norm["kind"]), _record_body(norm))


def evidence_record_fingerprint(raw: Mapping[str, Any]) -> str:
    """Fingerprint des NORMALISIERTEN Inhalts eines rohen Eintrags (ohne sein
    'fingerprint'-Feld). Darstellungsvarianten (``+00:00`` statt ``Z``,
    ``1`` statt ``1.0``, Sekundenbruchteile) aendern ihn nicht; jede
    inhaltliche Aenderung schon. Ein ungueltiger Eintrag hat keinen
    Fingerprint (``FingerprintError``). ``raw`` wird nicht veraendert."""
    if not isinstance(raw, dict):
        raise FingerprintError("Nachweiseintrag muss ein dict sein.")
    return _fingerprint_of_raw({key: value for key, value in raw.items() if key != "fingerprint"})


def seal_evidence_record(raw_without_fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """Liefert eine NEUE Kopie des Eintrags (Darstellung unveraendert) mit dem
    berechneten 'fingerprint' (fuer Fixtures/spaetere Werkzeuge). Ein bereits
    vorhandenes Feld 'fingerprint' ist ein Fehler. Kein Signieren; ein
    ungueltiger Eintrag wird nicht gesiegelt."""
    if not isinstance(raw_without_fingerprint, dict):
        raise FingerprintError("Nachweiseintrag muss ein dict sein.")
    if "fingerprint" in raw_without_fingerprint:
        raise FingerprintError("Eintrag enthaelt bereits ein Feld 'fingerprint'.")
    fingerprint = _fingerprint_of_raw(raw_without_fingerprint)
    sealed = json.loads(canonical_json(raw_without_fingerprint).decode("ascii"))
    sealed["fingerprint"] = fingerprint
    return sealed


def parse_evidence_file(data: Any) -> ParseResult[EvidenceFile]:
    """Parst den kompletten lokalen Nachweisbestand. Liest nichts von der Platte."""
    errs = _Errs(BlockCode.EVIDENCE_INVALID)
    obj = _Obj.of(data, "evidence", errs)
    if obj is None:
        return ParseResult(None, tuple(errs.items))
    obj.keys(("schema_version", "records"))
    version = obj.read("schema_version", c_schema_version(EVIDENCE_SCHEMA_VERSION))
    raw_records = obj.raw("records")
    records: list[EvidenceRecord] = []
    complete = "records" in obj.data
    if complete:
        if type(raw_records) is not list or len(raw_records) > MAX_RECORDS:
            errs.add("evidence.records", f"muss eine Liste mit hoechstens {MAX_RECORDS} Eintraegen sein")
            complete = False
        else:
            for index, item in enumerate(raw_records):
                path = f"records[{index}]"
                fields = _raw_to_fields(item, path, errs, expect_fingerprint=True)
                if fields is None:
                    complete = False
                    continue
                try:
                    records.append(EvidenceRecord(**fields, fingerprint=item["fingerprint"]))
                except EvidenceModelError as exc:
                    _add_model_problems(errs, path, exc)
                    complete = False
    if errs or not complete or version is _BAD:
        return ParseResult(None, tuple(errs.items))
    try:
        return ParseResult(EvidenceFile(version, tuple(records)))
    except EvidenceModelError as exc:
        _add_model_problems(errs, "evidence", exc)
        return ParseResult(None, tuple(errs.items))


# --- Laufzeitdaten der Sitzung (reine Datencontainer, kein Beweis) ---------------------------------------

LOCK_KIND_FLOCK = "flock"


@dataclass(frozen=True, slots=True)
class IdentityProof:
    """Sitzungsgebundener Identitaetsstand. Eine Behauptung der Sitzung, kein
    Beweis des Moduls: gueltig nur fuer ``session_id``/``generation``. Fuer
    ``FIRMWARE_PROVEN`` ist eine beobachtete MCU-ID noetig."""

    session_id: str
    generation: int
    port_realpath: str
    observed_mcu_id: str | None
    level: IdentityLevel
    established_monotonic_ns: int

    def __post_init__(self) -> None:
        _model_check(c_ident, self.session_id, "session_id")
        _model_check(c_int, self.generation, "generation", 0)
        _model_check(c_path, self.port_realpath, "port_realpath")
        if self.observed_mcu_id is not None:
            _model_check(c_ident, self.observed_mcu_id, "observed_mcu_id")
        if not isinstance(self.level, IdentityLevel):
            raise EvidenceModelError("level muss eine IdentityLevel sein.")
        _model_check(c_int, self.established_monotonic_ns, "established_monotonic_ns", 0)
        if self.level is IdentityLevel.FIRMWARE_PROVEN and self.observed_mcu_id is None:
            raise EvidenceModelError("FIRMWARE_PROVEN braucht eine beobachtete MCU-ID.")


@dataclass(frozen=True, slots=True)
class McuOwnershipClaim:
    """Behauptung der Sitzung ueber den MCU-Besitz (Plan Addendum B9). Das
    Modul kann den Lock nicht beweisen; die Durchsetzung folgt strukturell aus
    der Sitzung (Stufe 5)."""

    controller: str
    lock_kind: str
    session_id: str
    generation: int
    port_realpath: str
    holder_pid: int

    def __post_init__(self) -> None:
        if type(self.controller) is not str or self.controller not in VALID_CONTROLLERS:
            raise EvidenceModelError(f"controller muss einer von {list(VALID_CONTROLLERS)} sein.")
        if self.lock_kind != LOCK_KIND_FLOCK:
            raise EvidenceModelError(f"lock_kind muss {LOCK_KIND_FLOCK!r} sein.")
        _model_check(c_ident, self.session_id, "session_id")
        _model_check(c_int, self.generation, "generation", 0)
        _model_check(c_path, self.port_realpath, "port_realpath")
        _model_check(c_int, self.holder_pid, "holder_pid", 1)
