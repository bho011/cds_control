"""AP-05 Stufe 2A: services/peristaltic/dosing_release/reasons.py (hardwarefrei)."""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from services.peristaltic.dosing_release import reasons as rs
from services.peristaltic.models import VALID_PUMPS

PACKAGE_DIR = Path(rs.__file__).resolve().parent

EXPECTED_BLOCK_CODES = """
plan_missing plan_type_invalid plan_hash_mismatch plan_version_unsupported no_dosing_work
plan_controller_not_mcu_b plan_pump_roles_mismatch plan_profile_id_mismatch plan_weights_mismatch
plan_chunk_above_limit chunk_kinematic_bound_exceeded
mapping_unavailable mapping_invalid mapping_roles_mismatch mapping_port_missing profile_unavailable
profile_invalid profile_unconfirmed profile_id_missing profile_fields_missing profile_pump_value_missing
calibration_unavailable calibration_invalid calibration_not_verified calibration_stale_firmware_value
calibration_deviation_exceeded calibration_spread_exceeded calibration_range_not_covered
calibration_age_exceeded policy_missing policy_invalid policy_incomplete policy_kind_mismatch
loader_test_path_forbidden
identity_unverified identity_level_insufficient identity_port_mismatch identity_evidence_expired
firmware_image_unproven firmware_image_mismatch water_test_missing water_test_stale release_record_missing
release_record_invalid release_record_expired release_scope_insufficient release_fingerprint_mismatch
evidence_unavailable evidence_invalid evidence_fingerprint_mismatch evidence_chain_broken
evidence_origin_forbidden evidence_not_yet_valid evidence_expired trials_digest_mismatch
runtime_evidence_missing runtime_evidence_stale_profile runtime_evidence_parallel_group_missing
runtime_evidence_insufficient_count runtime_margin_insufficient runtime_measurement_definition_unsupported
runtime_evidence_invalid_outcome runtime_group_declaration_mismatch
hardware_execution_disabled auth_expired auth_plan_mismatch auth_config_changed auth_scope_simulation
hardware_process_conflict hardware_lock_unavailable client_not_open client_desynced
client_write_timeout_unset identity_not_proven_this_session dose_state_uncertain journal_unavailable
abort_requested stop_pending mcu_ownership_missing mcu_ownership_mismatch mcu_lock_unavailable
internal_error
""".split()


def test_block_codes_are_frozen_unique_and_untranslated():
    values = [code.value for code in rs.BlockCode]
    assert values == EXPECTED_BLOCK_CODES
    assert len(set(values)) == len(values)
    for code in rs.BlockCode:
        assert code.value == code.name.lower()
        assert code.value.isascii() and code.value.replace("_", "").isalnum()
        assert isinstance(code, str)


def test_every_block_code_has_a_level_and_levels_follow_the_groups():
    assert {code for code in rs.BlockCode} == set(rs._LEVEL_BY_CODE)
    assert rs.default_level(rs.BlockCode.PLAN_MISSING) is rs.GateLevel.PLAN
    assert rs.default_level(rs.BlockCode.CHUNK_KINEMATIC_BOUND_EXCEEDED) is rs.GateLevel.PLAN
    assert rs.default_level(rs.BlockCode.MAPPING_UNAVAILABLE) is rs.GateLevel.CONFIG
    assert rs.default_level(rs.BlockCode.LOADER_TEST_PATH_FORBIDDEN) is rs.GateLevel.CONFIG
    assert rs.default_level(rs.BlockCode.IDENTITY_UNVERIFIED) is rs.GateLevel.HARDWARE_EVIDENCE
    assert rs.default_level(rs.BlockCode.RUNTIME_GROUP_DECLARATION_MISMATCH) is rs.GateLevel.HARDWARE_EVIDENCE
    assert rs.default_level(rs.BlockCode.HARDWARE_EXECUTION_DISABLED) is rs.GateLevel.DISPATCH
    assert rs.default_level(rs.BlockCode.INTERNAL_ERROR) is rs.GateLevel.DISPATCH


@pytest.mark.parametrize(
    ("enum_cls", "expected"),
    [
        (rs.GateLevel, ["plan", "config", "hardware_evidence", "dispatch"]),
        (rs.GateStatus, ["pass", "blocked", "not_evaluated"]),
        (rs.ReleaseScope, ["simulation", "water_only", "chemical"]),
        (rs.IdentityLevel, ["none", "port_attested", "firmware_proven"]),
        (rs.PolicyKind, ["production", "test_simulation"]),
        (rs.EvidenceOrigin, ["synthetic_test", "human_attestation", "host_measured", "derived"]),
        (rs.EvidenceAssurance, ["attested", "measured", "derived"]),
    ],
)
def test_small_enums_are_frozen_and_json_serializable(enum_cls, expected):
    assert [member.value for member in enum_cls] == expected
    assert json.loads(json.dumps([member for member in enum_cls])) == expected


@pytest.mark.parametrize("enum_cls", [rs.GateLevel, rs.GateStatus, rs.ReleaseScope, rs.IdentityLevel, rs.PolicyKind, rs.EvidenceOrigin, rs.EvidenceAssurance, rs.BlockCode])
def test_parse_enum_accepts_member_and_exact_value(enum_cls):
    for member in enum_cls:
        assert rs.parse_enum(enum_cls, member) is member
        assert rs.parse_enum(enum_cls, member.value) is member


@pytest.mark.parametrize("bad", ["PASS", " pass", "pass ", "Pass", "", "unknown", None, True, False, 0, 1, 1.0, b"pass", ["pass"], {"pass": 1}, object()])
def test_parse_enum_rejects_unknown_and_wrongly_typed_values(bad):
    with pytest.raises(rs.UnknownEnumValueError):
        rs.parse_enum(rs.GateStatus, bad)


def test_parse_enum_rejects_member_of_other_enum_and_overlong_text():
    with pytest.raises(rs.UnknownEnumValueError):
        rs.parse_enum(rs.GateStatus, rs.PolicyKind.PRODUCTION)
    with pytest.raises(rs.UnknownEnumValueError) as info:
        rs.parse_enum(rs.GateStatus, "x" * 10_000)
    assert len(str(info.value)) < 400


def test_pump_constants_match_the_peristaltic_models():
    assert rs._PUMPS == tuple(VALID_PUMPS)


def test_block_reason_is_immutable_and_serializable():
    reason = rs.BlockReason.create(rs.BlockCode.PLAN_HASH_MISMATCH, "Hash passt nicht", pump="P2", detail={"b": "2", "a": "1"})
    assert reason.level is rs.GateLevel.PLAN
    assert reason.detail == (("a", "1"), ("b", "2"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        reason.message = "x"  # type: ignore[misc]
    assert json.dumps(reason.to_dict())


def test_block_reason_does_not_keep_a_reference_to_the_source_dict():
    detail = {"k": "v"}
    reason = rs.BlockReason.create(rs.BlockCode.INTERNAL_ERROR, "boom", detail=detail)
    detail["k"] = "changed"
    detail["new"] = "x"
    assert reason.detail == (("k", "v"),)
    assert isinstance(reason.detail, tuple)


def test_block_reason_level_can_be_overridden_explicitly():
    reason = rs.BlockReason.create(rs.BlockCode.INTERNAL_ERROR, "boom", level=rs.GateLevel.PLAN)
    assert reason.level is rs.GateLevel.PLAN


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "plan_missing"},
        {"code": None},
        {"level": "plan"},
        {"message": ""},
        {"message": 5},
        {"message": "x" * (rs.MAX_MESSAGE_LENGTH + 1)},
        {"message": "mit\nSteuerzeichen"},
        {"pump": "P5"},
        {"pump": "p1"},
        {"pump": 1},
        {"detail": [("a", "b")]},
        {"detail": (("a",),)},
        {"detail": (("a", 1),)},
        {"detail": (("", "b"),)},
        {"detail": tuple(("k%d" % i, "v") for i in range(rs.MAX_DETAIL_ITEMS + 1))},
        {"detail": (("a", "x" * (rs.MAX_DETAIL_TEXT_LENGTH + 1)),)},
    ],
)
def test_block_reason_rejects_invalid_fields(kwargs):
    base = {"code": rs.BlockCode.INTERNAL_ERROR, "level": rs.GateLevel.DISPATCH, "message": "ok"}
    base.update(kwargs)
    with pytest.raises(ValueError):
        rs.BlockReason(**base)


def test_block_reason_create_rejects_non_enum_code():
    with pytest.raises(ValueError):
        rs.BlockReason.create("plan_missing", "x")  # type: ignore[arg-type]


def _reason():
    return rs.BlockReason.create(rs.BlockCode.PLAN_MISSING, "fehlt")


def test_gate_verdict_invariants():
    ok = rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.PASS)
    assert ok.passed
    blocked = rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.BLOCKED, (_reason(),))
    assert not blocked.passed
    not_evaluated = rs.GateVerdict(rs.GateLevel.CONFIG, rs.GateStatus.NOT_EVALUATED)
    assert not not_evaluated.passed  # NOT_EVALUATED zaehlt nie als bestanden
    with pytest.raises(ValueError):
        rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.PASS, (_reason(),))
    with pytest.raises(ValueError):
        rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.BLOCKED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ok.status = rs.GateStatus.BLOCKED  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["pass", None, 1])
def test_gate_verdict_rejects_wrong_types(bad):
    with pytest.raises(ValueError):
        rs.GateVerdict(bad, rs.GateStatus.PASS)
    with pytest.raises(ValueError):
        rs.GateVerdict(rs.GateLevel.PLAN, bad)


@pytest.mark.parametrize("reasons", [[_reason()], (_reason(), "x"), "abc"])
def test_gate_verdict_rejects_non_tuple_or_foreign_reasons(reasons):
    with pytest.raises(ValueError):
        rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.NOT_EVALUATED, reasons)


@pytest.mark.parametrize("fingerprint", ["abc", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "md5:" + "a" * 64, 5])
def test_gate_verdict_rejects_malformed_fingerprint(fingerprint):
    with pytest.raises(ValueError):
        rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.PASS, (), fingerprint)


def test_gate_verdict_accepts_wellformed_fingerprint():
    verdict = rs.GateVerdict(rs.GateLevel.PLAN, rs.GateStatus.PASS, (), "sha256:" + "a" * 64)
    assert verdict.fingerprint.startswith("sha256:")


# --- statische Isolation (Stufe 2A: keine Hardware-, Netzwerk- oder Dateizugriffe) -----------------

FORBIDDEN_IMPORT_ROOTS = {
    "serial", "gpiozero", "lgpio", "paho", "socket", "asyncio", "subprocess", "requests", "urllib",
    "tests", "pytest", "hardware", "nicegui_dashboard", "process", "statemachine",
}
FORBIDDEN_MODULES = {
    "services.peristaltic.serial_client", "services.peristaltic.safety", "services.peristaltic.calibration",
    "services.peristaltic.calibration_storage", "services.peristaltic.dose_limits", "services.peristaltic.prime",
    "services.mqtt_publisher", "services.mqtt_topic_reader",
}
FORBIDDEN_NAMES = {"has_volumetric_calibration", "FIRMWARE_DEFAULT_ML_PER_STEP", "DispatchAuthorization", "authorize_dispatch", "TEST_RULES"}


def _module_sources():
    return sorted(PACKAGE_DIR.glob("*.py"))


@pytest.mark.parametrize("path", _module_sources(), ids=lambda p: p.name)
def test_package_has_no_hardware_network_or_file_access(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_IMPORT_ROOTS, alias.name
                assert alias.name not in FORBIDDEN_MODULES, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] not in FORBIDDEN_IMPORT_ROOTS, node.module
            assert node.module not in FORBIDDEN_MODULES, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "__import__"}, node.func.id
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"read_text", "write_text", "read_bytes", "write_bytes", "open", "mkdir", "unlink"}, node.attr
        elif isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, node.id


def test_package_has_only_the_approved_stage_2a_modules():
    assert [p.name for p in _module_sources()] == ["__init__.py", "evidence.py", "reasons.py"]
