"""Stabile, maschinenlesbare Typen der MCU_B-Freigabelogik (AP-05 Stufe 2A).

Reine Datentypen: kein I/O, keine Uhr, keine Hardware, keine Gate-Logik. Die
Enum-Werte sind nicht uebersetzt und serialisierbar (``str``-Enums); sie sind
Teil des Vertrags und werden durch Tests eingefroren. Ein Wert ausserhalb der
Enums wird kontrolliert abgelehnt (``parse_enum``), nie geraten.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, TypeVar

MAX_MESSAGE_LENGTH = 500
MAX_DETAIL_ITEMS = 20
MAX_DETAIL_TEXT_LENGTH = 200

_PUMPS = ("P1", "P2", "P3", "P4")  # gleich models.VALID_PUMPS (per Test geprueft)


class GateLevel(str, Enum):
    PLAN = "plan"
    CONFIG = "config"
    HARDWARE_EVIDENCE = "hardware_evidence"
    DISPATCH = "dispatch"


class GateStatus(str, Enum):
    PASS = "pass"
    BLOCKED = "blocked"
    NOT_EVALUATED = "not_evaluated"  # zaehlt nie als bestanden


class ReleaseScope(str, Enum):
    SIMULATION = "simulation"
    WATER_ONLY = "water_only"
    CHEMICAL = "chemical"


class IdentityLevel(str, Enum):
    NONE = "none"
    PORT_ATTESTED = "port_attested"  # menschliche Aussage, kein Geraetenachweis
    FIRMWARE_PROVEN = "firmware_proven"  # erfordert Firmware-ID (derzeit nicht verfuegbar)


class PolicyKind(str, Enum):
    PRODUCTION = "production"
    TEST_SIMULATION = "test_simulation"


class EvidenceOrigin(str, Enum):
    SYNTHETIC_TEST = "synthetic_test"
    HUMAN_ATTESTATION = "human_attestation"
    HOST_MEASURED = "host_measured"
    DERIVED = "derived"


class EvidenceAssurance(str, Enum):
    ATTESTED = "attested"  # menschliche Aussage
    MEASURED = "measured"  # vom Host gemessen
    DERIVED = "derived"  # aus anderen Daten berechnet


class BlockCode(str, Enum):
    """Sperrgruende. Wert == Name in Kleinbuchstaben (per Test erzwungen)."""

    # --- Ebene PLAN ---
    PLAN_MISSING = "plan_missing"
    PLAN_TYPE_INVALID = "plan_type_invalid"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    PLAN_VERSION_UNSUPPORTED = "plan_version_unsupported"
    NO_DOSING_WORK = "no_dosing_work"
    PLAN_CONTROLLER_NOT_MCU_B = "plan_controller_not_mcu_b"
    PLAN_PUMP_ROLES_MISMATCH = "plan_pump_roles_mismatch"
    PLAN_PROFILE_ID_MISMATCH = "plan_profile_id_mismatch"
    PLAN_WEIGHTS_MISMATCH = "plan_weights_mismatch"
    PLAN_CHUNK_ABOVE_LIMIT = "plan_chunk_above_limit"
    CHUNK_KINEMATIC_BOUND_EXCEEDED = "chunk_kinematic_bound_exceeded"

    # --- Ebene CONFIG ---
    MAPPING_UNAVAILABLE = "mapping_unavailable"
    MAPPING_INVALID = "mapping_invalid"
    MAPPING_ROLES_MISMATCH = "mapping_roles_mismatch"
    MAPPING_PORT_MISSING = "mapping_port_missing"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_INVALID = "profile_invalid"
    PROFILE_UNCONFIRMED = "profile_unconfirmed"
    PROFILE_ID_MISSING = "profile_id_missing"
    PROFILE_FIELDS_MISSING = "profile_fields_missing"
    PROFILE_PUMP_VALUE_MISSING = "profile_pump_value_missing"
    CALIBRATION_UNAVAILABLE = "calibration_unavailable"
    CALIBRATION_INVALID = "calibration_invalid"
    CALIBRATION_NOT_VERIFIED = "calibration_not_verified"
    CALIBRATION_STALE_FIRMWARE_VALUE = "calibration_stale_firmware_value"
    CALIBRATION_DEVIATION_EXCEEDED = "calibration_deviation_exceeded"
    CALIBRATION_SPREAD_EXCEEDED = "calibration_spread_exceeded"
    CALIBRATION_RANGE_NOT_COVERED = "calibration_range_not_covered"
    CALIBRATION_AGE_EXCEEDED = "calibration_age_exceeded"
    POLICY_MISSING = "policy_missing"
    POLICY_INVALID = "policy_invalid"
    POLICY_INCOMPLETE = "policy_incomplete"
    POLICY_KIND_MISMATCH = "policy_kind_mismatch"
    LOADER_TEST_PATH_FORBIDDEN = "loader_test_path_forbidden"

    # --- Ebene HARDWARE_EVIDENCE ---
    IDENTITY_UNVERIFIED = "identity_unverified"
    IDENTITY_LEVEL_INSUFFICIENT = "identity_level_insufficient"
    IDENTITY_PORT_MISMATCH = "identity_port_mismatch"
    IDENTITY_EVIDENCE_EXPIRED = "identity_evidence_expired"
    FIRMWARE_IMAGE_UNPROVEN = "firmware_image_unproven"
    FIRMWARE_IMAGE_MISMATCH = "firmware_image_mismatch"
    WATER_TEST_MISSING = "water_test_missing"
    WATER_TEST_STALE = "water_test_stale"
    RELEASE_RECORD_MISSING = "release_record_missing"
    RELEASE_RECORD_INVALID = "release_record_invalid"
    RELEASE_RECORD_EXPIRED = "release_record_expired"
    RELEASE_SCOPE_INSUFFICIENT = "release_scope_insufficient"
    RELEASE_FINGERPRINT_MISMATCH = "release_fingerprint_mismatch"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    EVIDENCE_INVALID = "evidence_invalid"
    EVIDENCE_FINGERPRINT_MISMATCH = "evidence_fingerprint_mismatch"
    EVIDENCE_CHAIN_BROKEN = "evidence_chain_broken"
    EVIDENCE_ORIGIN_FORBIDDEN = "evidence_origin_forbidden"
    EVIDENCE_NOT_YET_VALID = "evidence_not_yet_valid"
    EVIDENCE_EXPIRED = "evidence_expired"
    TRIALS_DIGEST_MISMATCH = "trials_digest_mismatch"
    RUNTIME_EVIDENCE_MISSING = "runtime_evidence_missing"
    RUNTIME_EVIDENCE_STALE_PROFILE = "runtime_evidence_stale_profile"
    RUNTIME_EVIDENCE_PARALLEL_GROUP_MISSING = "runtime_evidence_parallel_group_missing"
    RUNTIME_EVIDENCE_INSUFFICIENT_COUNT = "runtime_evidence_insufficient_count"
    RUNTIME_MARGIN_INSUFFICIENT = "runtime_margin_insufficient"
    RUNTIME_MEASUREMENT_DEFINITION_UNSUPPORTED = "runtime_measurement_definition_unsupported"
    RUNTIME_EVIDENCE_INVALID_OUTCOME = "runtime_evidence_invalid_outcome"
    RUNTIME_GROUP_DECLARATION_MISMATCH = "runtime_group_declaration_mismatch"

    # --- Ebene DISPATCH ---
    HARDWARE_EXECUTION_DISABLED = "hardware_execution_disabled"
    AUTH_EXPIRED = "auth_expired"
    AUTH_PLAN_MISMATCH = "auth_plan_mismatch"
    AUTH_CONFIG_CHANGED = "auth_config_changed"
    AUTH_SCOPE_SIMULATION = "auth_scope_simulation"
    HARDWARE_PROCESS_CONFLICT = "hardware_process_conflict"
    HARDWARE_LOCK_UNAVAILABLE = "hardware_lock_unavailable"
    CLIENT_NOT_OPEN = "client_not_open"
    CLIENT_DESYNCED = "client_desynced"
    CLIENT_WRITE_TIMEOUT_UNSET = "client_write_timeout_unset"
    IDENTITY_NOT_PROVEN_THIS_SESSION = "identity_not_proven_this_session"
    DOSE_STATE_UNCERTAIN = "dose_state_uncertain"
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    ABORT_REQUESTED = "abort_requested"
    STOP_PENDING = "stop_pending"
    MCU_OWNERSHIP_MISSING = "mcu_ownership_missing"
    MCU_OWNERSHIP_MISMATCH = "mcu_ownership_mismatch"
    MCU_LOCK_UNAVAILABLE = "mcu_lock_unavailable"
    INTERNAL_ERROR = "internal_error"


def _level_table() -> dict[BlockCode, GateLevel]:
    """Ordnet jeden Code der Ebene zu, in der er entsteht (aus der Reihenfolge
    der Enum-Deklaration; die Gruppen sind durch ihre Grenzcodes festgelegt)."""
    boundaries = (
        (BlockCode.PLAN_MISSING, GateLevel.PLAN),
        (BlockCode.MAPPING_UNAVAILABLE, GateLevel.CONFIG),
        (BlockCode.IDENTITY_UNVERIFIED, GateLevel.HARDWARE_EVIDENCE),
        (BlockCode.HARDWARE_EXECUTION_DISABLED, GateLevel.DISPATCH),
    )
    starts = {code: level for code, level in boundaries}
    table: dict[BlockCode, GateLevel] = {}
    current: GateLevel | None = None
    for code in BlockCode:
        current = starts.get(code, current)
        assert current is not None
        table[code] = current
    return table


_LEVEL_BY_CODE: Mapping[BlockCode, GateLevel] = _level_table()


def default_level(code: "BlockCode") -> GateLevel:
    """Ebene, in der ``code`` ueblicherweise entsteht."""
    return _LEVEL_BY_CODE[code]


E = TypeVar("E", bound=Enum)


class UnknownEnumValueError(ValueError):
    """Unbekannter oder falsch typisierter Enum-Wert (kontrollierte Ablehnung)."""


def parse_enum(enum_cls: type[E], value: Any, name: str = "Wert") -> E:
    """Strikt: nur ein Mitglied von ``enum_cls`` oder dessen exakter
    String-Wert. Gross-/Kleinschreibung, Leerraum, bool, int, None -> Fehler."""
    if isinstance(value, enum_cls):
        return value
    if type(value) is str:
        for member in enum_cls:
            if member.value == value:
                return member
    shown = value if type(value) is str and len(value) <= 40 else type(value).__name__
    raise UnknownEnumValueError(
        f"{name}: {shown!r} ist kein gueltiger {enum_cls.__name__} "
        f"(erlaubt: {[m.value for m in enum_cls]})."
    )


def _check_text(value: Any, name: str, limit: int) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise ValueError(f"{name} muss ein nichtleerer String mit hoechstens {limit} Zeichen sein.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{name} darf keine Steuerzeichen enthalten.")
    return value


@dataclass(frozen=True, slots=True)
class BlockReason:
    """Ein einzelner Sperrgrund. Unveraenderlich; ``detail`` ist ein Tupel von
    (Schluessel, Text)-Paaren, nie ein veraenderbares Dict."""

    code: BlockCode
    level: GateLevel
    message: str
    pump: str | None = None
    detail: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, BlockCode):
            raise ValueError("code muss ein BlockCode sein.")
        if not isinstance(self.level, GateLevel):
            raise ValueError("level muss ein GateLevel sein.")
        _check_text(self.message, "message", MAX_MESSAGE_LENGTH)
        if self.pump is not None and self.pump not in _PUMPS:
            raise ValueError(f"pump muss None oder eine von {_PUMPS} sein.")
        if type(self.detail) is not tuple or len(self.detail) > MAX_DETAIL_ITEMS:
            raise ValueError("detail muss ein Tupel mit hoechstens %d Eintraegen sein." % MAX_DETAIL_ITEMS)
        for item in self.detail:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("detail-Eintraege muessen (Schluessel, Text)-Tupel sein.")
            _check_text(item[0], "detail-Schluessel", MAX_DETAIL_TEXT_LENGTH)
            _check_text(item[1], "detail-Text", MAX_DETAIL_TEXT_LENGTH)

    @classmethod
    def create(
        cls,
        code: BlockCode,
        message: str,
        *,
        level: GateLevel | None = None,
        pump: str | None = None,
        detail: Mapping[str, str] | None = None,
    ) -> "BlockReason":
        if not isinstance(code, BlockCode):
            raise ValueError("code muss ein BlockCode sein.")
        items: tuple[tuple[str, str], ...] = ()
        if detail is not None:
            items = tuple(sorted((str(k), str(v)) for k, v in detail.items()))
        return cls(code, default_level(code) if level is None else level, message, pump, items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "level": self.level.value,
            "message": self.message,
            "pump": self.pump,
            "detail": [list(item) for item in self.detail],
        }


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Reiner Ergebnisdatentyp einer Gate-Ebene. Enthaelt KEINE Gate-Logik und
    ist keine Berechtigung: nichts, was Hardware ansteuert, nimmt ein Verdikt
    entgegen (siehe Plan R2/Addendum B6)."""

    level: GateLevel
    status: GateStatus
    reasons: tuple[BlockReason, ...] = ()
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.level, GateLevel):
            raise ValueError("level muss ein GateLevel sein.")
        if not isinstance(self.status, GateStatus):
            raise ValueError("status muss ein GateStatus sein.")
        if type(self.reasons) is not tuple or not all(isinstance(r, BlockReason) for r in self.reasons):
            raise ValueError("reasons muss ein Tupel von BlockReason sein.")
        if self.status is GateStatus.PASS and self.reasons:
            raise ValueError("Ein bestandenes Verdikt darf keine Sperrgruende enthalten.")
        if self.status is GateStatus.BLOCKED and not self.reasons:
            raise ValueError("Ein gesperrtes Verdikt braucht mindestens einen Sperrgrund.")
        if self.fingerprint is not None and not _is_fingerprint(self.fingerprint):
            raise ValueError("fingerprint muss None oder 'sha256:<64 Hex>' sein.")

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASS


def _is_fingerprint(value: Any) -> bool:
    if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):
        return False
    return all(ch in "0123456789abcdef" for ch in value[7:])
