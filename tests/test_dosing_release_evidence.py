"""AP-05 Stufe 2A: services/peristaltic/dosing_release/evidence.py (hardwarefrei).

Alle Fixtures sind synthetische Testdaten und liegen ausschliesslich in dieser
Testdatei; es wird nichts gelesen oder geschrieben."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from datetime import datetime, timezone

import pytest

from services.peristaltic.dosing_release import evidence as ev
from services.peristaltic.dosing_release.evidence import EvidenceKind
from services.peristaltic.dosing_release.reasons import (
    BlockCode,
    EvidenceAssurance,
    EvidenceOrigin,
    IdentityLevel,
    PolicyKind,
    ReleaseScope,
)

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-12-31T00:00:00Z"
FP_PROFILE = "sha256:" + "1" * 64
FP_CONFIG = "sha256:" + "2" * 64
FP_DIGEST = "sha256:" + "3" * 64
PORT = "/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-port0"


# --- Fixtures: gueltige rohe Eintraege --------------------------------------------------------------


def subject(kind: EvidenceKind) -> dict:
    pump = "P1" if kind in (EvidenceKind.PUMP_VERIFICATION, EvidenceKind.RUNTIME_OBSERVATION) else None
    role = "nutrient_a_1" if kind is EvidenceKind.PUMP_VERIFICATION else None
    return {"controller": "MCU_B", "pump": pump, "mapping_role": role, "profile_id": "mcu_b_ec_8_microsteps_cal_v2", "profile_fingerprint": FP_PROFILE}


PAYLOADS = {
    EvidenceKind.IDENTITY_ATTESTATION: lambda: {"level": "port_attested", "port_path": PORT, "attested_by": "bediener_1", "statement": "Kabel beschriftet, Ziehprobe durchgefuehrt"},
    EvidenceKind.FIRMWARE_IMAGE_ATTESTATION: lambda: {
        "profile_id": "mcu_b_ec_8_microsteps_cal_v2",
        "firmware_source_commit": "86b7b5d639ea7ba4cdcc97a28ba6c76ae382c3e1",
        "steps_per_rev": 200,
        "pump_ml_per_step": {"P1": 0.000192624768, "P2": 0.000191096, "P3": 0.000175235032, "P4": 0.000197402168},
        "attested_by": "bediener_1",
        "statement": "Image aus diesem Commit geflasht",
    },
    EvidenceKind.PUMP_VERIFICATION: lambda: {
        "purpose": "verification",
        "firmware_ml_per_step_used": 0.000192624768,
        "verified_ml_per_step": 0.000192,
        "trial_count": 5,
        "verified_at_ml": [5.0, 10.0],
        "trials_digest": FP_DIGEST,
    },
    EvidenceKind.RUNTIME_OBSERVATION: lambda: {
        "measurement": {"definition_version": "runtime_measurement_v1", "clock": "monotonic_ns", "start": "before_dose_write", "end": "done_line_received_by_reader"},
        "requested_ml": 10.0,
        "elapsed_ms": 19000,
        "outcome": "DONE",
        "boot_reset_observed": False,
        "declared_concurrent_pumps": ["P1", "P3"],
        "derived_concurrent_pumps": ["P1", "P3"],
        "tool_version": "verify_tool_0.1",
    },
    EvidenceKind.WATER_ONLY_TEST: lambda: {
        "test_id": "wt_001", "pumps_tested": ["P1", "P2", "P3", "P4"], "max_chunk_ml": 10.0, "result": "passed",
        "water_only_confirmed": True, "attested_by": "bediener_1", "statement": "nur Wasser in allen Leitungen",
    },
    EvidenceKind.RELEASE: lambda: {"scope": "water_only", "config_fingerprint": FP_CONFIG, "granted_by": "leitung_1", "granted_at": T0, "statement": "Freigabe fuer Wassertests"},
}

DEFAULT_ORIGIN = {
    EvidenceKind.IDENTITY_ATTESTATION: ("human_attestation", "attested"),
    EvidenceKind.FIRMWARE_IMAGE_ATTESTATION: ("human_attestation", "attested"),
    EvidenceKind.WATER_ONLY_TEST: ("human_attestation", "attested"),
    EvidenceKind.RELEASE: ("human_attestation", "attested"),
    EvidenceKind.RUNTIME_OBSERVATION: ("host_measured", "measured"),
    EvidenceKind.PUMP_VERIFICATION: ("derived", "derived"),
}


def _base_water_test() -> dict:
    raw = raw_record(EvidenceKind.WATER_ONLY_TEST, record_id="rec_base_water_test")
    return ev.seal_evidence_record(raw)


def raw_record(kind: EvidenceKind, *, record_id: str | None = None, refs: list | None = None) -> dict:
    origin, assurance = DEFAULT_ORIGIN[kind]
    if refs is None:
        refs = [_base_water_test()["fingerprint"]] if kind is EvidenceKind.RELEASE else []
    return {
        "record_id": record_id or f"rec_{kind.value}",
        "kind": kind.value,
        "origin": origin,
        "assurance": assurance,
        "integrity": "unsigned_sha256",
        "created_utc": T0,
        "valid_from_utc": T0,
        "valid_until_utc": T1,
        "subject": subject(kind),
        "payload": PAYLOADS[kind](),
        "references": refs,
    }


def sealed(raw: dict) -> dict:
    body = {k: v for k, v in raw.items() if k != "fingerprint"}
    return ev.seal_evidence_record(body)


def make(kind: EvidenceKind, mutate=None, **kwargs) -> dict:
    raw = raw_record(kind, **kwargs)
    if mutate is not None:
        mutate(raw)
    return sealed(raw)


def file_of(*records: dict) -> dict:
    return {"schema_version": 1, "records": list(records)}


def full_file() -> dict:
    base = [make(kind) for kind in EvidenceKind if kind is not EvidenceKind.RELEASE]
    release = make(EvidenceKind.RELEASE, refs=[r["fingerprint"] for r in base])
    return file_of(*base, release)


def codes(result) -> set[BlockCode]:
    return {reason.code for reason in result.reasons}


def parse_record(raw: dict):
    """Siegelt den Eintrag, soweit moeglich. Nicht kanonisierbare Werte (NaN,
    riesige Ganzzahlen) lassen sich nicht siegeln; dann wird ein Platzhalter
    eingesetzt - der Parser muss den Eintrag trotzdem kontrolliert ablehnen."""
    try:
        record = sealed(raw)
    except ev.FingerprintError:
        record = {**raw, "fingerprint": FP_CONFIG}
    records = [record]
    if raw.get("kind") == "release" and raw.get("references") == [_base_water_test()["fingerprint"]]:
        records.append(_base_water_test())
    return ev.parse_evidence_file(file_of(*records))


def valid_policy(**overrides) -> dict:
    data = {
        "schema_version": 1, "policy_kind": "test_simulation", "policy_id": "test_policy", "revision": 1,
        "parameters": {name: (3 if name in ("min_verification_trials", "min_observation_count") else 1.5) for name in ev.POLICY_PARAMETER_NAMES},
    }
    data.update(overrides)
    return data


def draft_policy() -> dict:
    return {
        "schema_version": 1, "policy_kind": "production", "policy_id": "production_policy", "revision": 1,
        "parameters": {name: None for name in ev.POLICY_PARAMETER_NAMES},
    }


# --- Zeitstempel -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ("2026-01-01T00:00:00+00:00", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ("2026-02-28T23:59:59.5Z", datetime(2026, 2, 28, 23, 59, 59, 500000, tzinfo=timezone.utc)),
        ("2026-02-28T23:59:59.123456Z", datetime(2026, 2, 28, 23, 59, 59, 123456, tzinfo=timezone.utc)),
    ],
)
def test_utc_timestamps_parse(text, expected):
    assert ev.parse_utc_timestamp(text) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "2026-01-01T00:00:00", "2026-01-01T00:00:00+02:00", "2026-01-01T00:00:00-00:00", "2026-01-01 00:00:00Z",
        "2026-01-01", "2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T00:60:00Z",
        "2026-01-01T00:00:00.1234567Z", "", "z", None, 0, 1.5, True, b"2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z\n",
        "2026-01-01T00:00:00Z" + " " * 30,
    ],
)
def test_non_utc_or_malformed_timestamps_are_rejected(bad):
    with pytest.raises(ValueError):
        ev.parse_utc_timestamp(bad)


def test_format_utc_roundtrip_and_requires_utc():
    moment = datetime(2026, 5, 6, 7, 8, 9, 250000, tzinfo=timezone.utc)
    assert ev.format_utc(moment) == "2026-05-06T07:08:09.250000Z"
    assert ev.parse_utc_timestamp(ev.format_utc(moment)) == moment
    assert ev.format_utc(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        ev.format_utc(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        ev.format_utc("2026-01-01T00:00:00Z")  # type: ignore[arg-type]


# --- Fingerprints -------------------------------------------------------------------------------------


def test_canonical_json_is_key_order_independent_and_compact():
    a = {"b": 1, "a": [1.5, "x", None, True]}
    b = {"a": [1.5, "x", None, True], "b": 1}
    assert ev.canonical_json(a) == ev.canonical_json(b) == b'{"a":[1.5,"x",null,true],"b":1}'


def test_fingerprint_matches_independent_sha256_computation():
    data = {"x": 1, "y": [1, 2]}
    expected = hashlib.sha256(b"cds-evidence-v1|release\x00" + b'{"x":1,"y":[1,2]}').hexdigest()
    assert ev.compute_fingerprint("cds-evidence-v1|release", data) == "sha256:" + expected


def test_fingerprint_is_deterministic_and_domain_separated():
    data = {"x": 1}
    assert ev.compute_fingerprint("cds-evidence-v1|release", data) == ev.compute_fingerprint("cds-evidence-v1|release", dict(data))
    assert ev.compute_fingerprint("cds-evidence-v1|release", data) != ev.compute_fingerprint("cds-evidence-v1|runtime_observation", data)
    assert ev.compute_fingerprint("cds-evidence-v1|release", data) != ev.compute_fingerprint("cds-policy-v1|production", data)
    assert ev.is_fingerprint(ev.compute_fingerprint("cds-evidence-v1|release", data))


@pytest.mark.parametrize("domain", ["", "release", "cds-evidence", "CDS-evidence-v1", "cds-evidence-v1|", "cds-evidence-v1|Release", None, 5, "cds-evidence-v1\n"])
def test_fingerprint_rejects_invalid_domains(domain):
    with pytest.raises(ev.FingerprintError):
        ev.compute_fingerprint(domain, {"x": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "data",
    [
        {"x": float("nan")}, {"x": float("inf")}, {"x": -float("inf")}, {"x": 2**63}, {"x": -(2**63) - 1}, {"x": 10**400},
        {"x": (1, 2)}, {"x": {1, 2}}, {"x": b"abc"}, {"x": object()}, {1: "a"}, {"x": {"fingerprint": "sha256:" + "a" * 64}},
        {"fingerprint": "sha256:" + "a" * 64}, {"x": [{"y": {"fingerprint": 1}}]},
    ],
)
def test_fingerprint_rejects_non_canonical_or_self_referential_data(data):
    with pytest.raises(ev.FingerprintError):
        ev.compute_fingerprint("cds-evidence-v1|release", data)


def test_fingerprint_rejects_too_deep_nesting():
    data: dict = {}
    cursor = data
    for _ in range(ev.MAX_JSON_DEPTH + 2):
        cursor["n"] = {}
        cursor = cursor["n"]
    with pytest.raises(ev.FingerprintError):
        ev.canonical_json(data)


def test_fingerprint_helpers_do_not_mutate_their_input():
    data = {"a": [1, {"b": 2.5}], "c": "x"}
    before = copy.deepcopy(data)
    ev.canonical_json(data)
    ev.compute_fingerprint("cds-evidence-v1|release", data)
    assert data == before
    raw = raw_record(EvidenceKind.RELEASE)
    raw_before = copy.deepcopy(raw)
    result = ev.seal_evidence_record(raw)
    assert raw == raw_before and "fingerprint" not in raw
    assert result is not raw and result["fingerprint"].startswith("sha256:")


def test_seal_rejects_existing_fingerprint_field_no_self_reference():
    with pytest.raises(ev.FingerprintError):
        ev.seal_evidence_record(sealed(raw_record(EvidenceKind.RELEASE)))


def test_record_fingerprint_ignores_the_fingerprint_field_itself():
    raw = raw_record(EvidenceKind.RELEASE)
    plain = ev.evidence_record_fingerprint(raw)
    assert ev.evidence_record_fingerprint({**raw, "fingerprint": "sha256:" + "f" * 64}) == plain
    assert ev.seal_evidence_record(raw)["fingerprint"] == plain


@pytest.mark.parametrize("kind", ["unknown", None, 5])
def test_record_fingerprint_needs_a_known_kind(kind):
    with pytest.raises(ev.FingerprintError):
        ev.evidence_record_fingerprint({"kind": kind})
    with pytest.raises(ev.FingerprintError):
        ev.evidence_record_fingerprint("nicht-dict")  # type: ignore[arg-type]


def test_every_field_change_changes_the_record_fingerprint():
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    base = ev.evidence_record_fingerprint(raw)
    for path, value in [
        (("payload", "elapsed_ms"), 19001), (("subject", "pump"), "P3"), (("record_id",), "other"), (("valid_until_utc",), "2027-01-01T00:00:00Z"),
        (("references",), [FP_CONFIG]), (("origin",), "synthetic_test"),
    ]:
        changed = copy.deepcopy(raw)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert ev.evidence_record_fingerprint(changed) != base, path


def test_fingerprint_text_validator():
    assert ev.is_fingerprint("sha256:" + "0123456789abcdef" * 4)
    for bad in [None, 5, "", "sha256:", "sha256:" + "A" * 64, "sha256:" + "g" * 64, "sha1:" + "a" * 64, "sha256:" + "a" * 65, ("sha256:" + "a" * 64) + "\n"]:
        assert not ev.is_fingerprint(bad)


# --- EvaluationRules ----------------------------------------------------------------------------------------


def test_evaluation_rules_production_never_allows_synthetic_origin():
    production = ev.EvaluationRules(PolicyKind.PRODUCTION, frozenset({EvidenceOrigin.HUMAN_ATTESTATION, EvidenceOrigin.HOST_MEASURED}))
    assert production.allows(EvidenceOrigin.HOST_MEASURED) and not production.allows(EvidenceOrigin.SYNTHETIC_TEST)
    with pytest.raises(ev.EvidenceModelError):
        ev.EvaluationRules(PolicyKind.PRODUCTION, frozenset(EvidenceOrigin))
    test_rules = ev.EvaluationRules(PolicyKind.TEST_SIMULATION, frozenset(EvidenceOrigin))
    assert test_rules.allows(EvidenceOrigin.SYNTHETIC_TEST)


@pytest.mark.parametrize(
    ("kind", "origins"),
    [("production", frozenset({EvidenceOrigin.DERIVED})), (PolicyKind.PRODUCTION, frozenset()), (PolicyKind.PRODUCTION, {EvidenceOrigin.DERIVED}),
     (PolicyKind.PRODUCTION, frozenset({"derived"})), (None, frozenset({EvidenceOrigin.DERIVED})), (PolicyKind.TEST_SIMULATION, None)],
)
def test_evaluation_rules_reject_invalid_arguments(kind, origins):
    with pytest.raises(ev.EvidenceModelError):
        ev.EvaluationRules(kind, origins)  # type: ignore[arg-type]


def test_evaluation_rules_are_immutable_and_carry_no_authority():
    rules = ev.EvaluationRules(PolicyKind.TEST_SIMULATION, frozenset(EvidenceOrigin))
    with pytest.raises(dataclasses.FrozenInstanceError):
        rules.kind = PolicyKind.PRODUCTION  # type: ignore[misc]
    public = {name for name in dir(ev) if not name.startswith("_")}
    assert not {name for name in public if "authoriz" in name.lower() or "dispatch" in name.lower()}


# --- Policy ------------------------------------------------------------------------------------------------------


def test_incomplete_production_draft_is_structurally_valid_but_not_complete():
    result = ev.parse_policy(draft_policy())
    assert result.ok
    policy = result.value
    assert policy.kind is PolicyKind.PRODUCTION
    assert not policy.is_complete
    assert policy.missing_parameters() == ev.POLICY_PARAMETER_NAMES
    assert all(getattr(policy.parameters, name) is None for name in ev.POLICY_PARAMETER_NAMES)


def test_complete_test_policy_parses_with_typed_values():
    policy = ev.parse_policy(valid_policy()).value
    assert policy.kind is PolicyKind.TEST_SIMULATION and policy.is_complete
    assert policy.parameters.min_verification_trials == 3 and type(policy.parameters.min_verification_trials) is int
    assert policy.parameters.runtime_margin_ms == 1.5 and type(policy.parameters.runtime_margin_ms) is float
    assert policy.parameters.to_dict() == {name: getattr(policy.parameters, name) for name in ev.POLICY_PARAMETER_NAMES}


def test_partially_filled_policy_reports_exactly_the_missing_parameters():
    data = valid_policy()
    data["parameters"]["runtime_margin_ms"] = None
    data["parameters"]["authorization_ttl_s"] = None
    policy = ev.parse_policy(data).value
    assert not policy.is_complete
    assert set(policy.missing_parameters()) == {"runtime_margin_ms", "authorization_ttl_s"}


def test_policy_parameter_names_are_frozen():
    assert ev.POLICY_PARAMETER_NAMES == (
        "min_verification_trials", "min_observation_count", "max_verification_age_days", "max_ml_per_step_deviation",
        "max_trial_spread", "runtime_margin_ms", "identity_attestation_max_age_days", "firmware_image_max_age_days",
        "water_test_max_age_days", "release_max_age_days", "authorization_ttl_s",
    )


@pytest.mark.parametrize("name", ev.POLICY_PARAMETER_NAMES)
def test_missing_policy_parameter_key_is_structural_damage(name):
    data = draft_policy()
    del data["parameters"][name]
    result = ev.parse_policy(data)
    assert not result.ok and codes(result) == {BlockCode.POLICY_INVALID}


def test_unknown_policy_fields_are_not_ignored():
    for mutate in (lambda d: d.update(extra=1), lambda d: d["parameters"].update(extra=1)):
        data = draft_policy()
        mutate(data)
        assert codes(ev.parse_policy(data)) == {BlockCode.POLICY_INVALID}


@pytest.mark.parametrize("bad", [True, False, "5", float("nan"), float("inf"), -float("inf"), 0, 0.0, -1, -0.5, 10**400, 2**63, [1], {"a": 1}])
def test_policy_number_parameters_reject_invalid_values(bad):
    data = draft_policy()
    data["parameters"]["runtime_margin_ms"] = bad
    assert codes(ev.parse_policy(data)) == {BlockCode.POLICY_INVALID}


@pytest.mark.parametrize("bad", [True, 2.0, "3", 0, -1, 2**63, 10**400, float("nan")])
def test_policy_count_parameters_need_real_positive_integers(bad):
    for name in ("min_verification_trials", "min_observation_count"):
        data = draft_policy()
        data["parameters"][name] = bad
        assert codes(ev.parse_policy(data)) == {BlockCode.POLICY_INVALID}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2), lambda d: d.update(schema_version=0), lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version="1"), lambda d: d.update(policy_kind="PRODUCTION"), lambda d: d.update(policy_kind="live"),
        lambda d: d.update(policy_kind=None), lambda d: d.update(policy_id=""), lambda d: d.update(policy_id="x" * 129),
        lambda d: d.update(policy_id="mit leerzeichen"), lambda d: d.update(policy_id=5), lambda d: d.update(revision=0),
        lambda d: d.update(revision=True), lambda d: d.update(revision=1.0), lambda d: d.update(revision=-3),
        lambda d: d.update(parameters=[]), lambda d: d.update(parameters=None), lambda d: d.pop("schema_version"),
        lambda d: d.pop("policy_kind"), lambda d: d.pop("parameters"),
    ],
)
def test_policy_header_validation(mutate):
    data = draft_policy()
    mutate(data)
    result = ev.parse_policy(data)
    assert not result.ok and result.value is None
    assert codes(result) == {BlockCode.POLICY_INVALID}


@pytest.mark.parametrize("data", [None, [], "policy", 5, True])
def test_policy_must_be_an_object(data):
    assert codes(ev.parse_policy(data)) == {BlockCode.POLICY_INVALID}


def test_policy_collects_all_errors_not_just_the_first():
    data = draft_policy()
    data["revision"] = 0
    data["policy_id"] = ""
    data["parameters"]["runtime_margin_ms"] = -1
    assert len(ev.parse_policy(data).reasons) >= 3


def test_policy_fingerprint_depends_on_content_kind_and_not_on_key_order():
    base = ev.parse_policy(draft_policy()).value.fingerprint
    reordered = dict(reversed(list(draft_policy().items())))
    assert ev.parse_policy(reordered).value.fingerprint == base
    changed = draft_policy()
    changed["revision"] = 2
    assert ev.parse_policy(changed).value.fingerprint != base
    other_kind = draft_policy()
    other_kind["policy_kind"] = "test_simulation"
    assert ev.parse_policy(other_kind).value.fingerprint != base
    assert ev.is_fingerprint(base)


def test_policy_parsing_does_not_mutate_input_nor_keep_references():
    data = valid_policy()
    before = copy.deepcopy(data)
    policy = ev.parse_policy(data).value
    assert data == before
    data["parameters"]["runtime_margin_ms"] = 999.0
    assert policy.parameters.runtime_margin_ms == 1.5
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.revision = 9  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.parameters.runtime_margin_ms = 1.0  # type: ignore[misc]


def test_policy_parameters_constructor_is_strict_too():
    assert ev.PolicyParameters().missing() == ev.POLICY_PARAMETER_NAMES
    assert ev.PolicyParameters(runtime_margin_ms=1).runtime_margin_ms == 1.0  # int wird zu float normalisiert (B1)
    assert type(ev.PolicyParameters(runtime_margin_ms=1).runtime_margin_ms) is float
    for kwargs in ({"runtime_margin_ms": float("nan")}, {"runtime_margin_ms": True}, {"min_observation_count": 2.0}, {"min_observation_count": True}, {"authorization_ttl_s": -1.0}):
        with pytest.raises(ev.EvidenceModelError):
            ev.PolicyParameters(**kwargs)


# --- Nachweisdatei: Gutfall ---------------------------------------------------------------------------------------


def test_full_evidence_file_parses_and_roundtrips_exactly():
    raw = full_file()
    result = ev.parse_evidence_file(raw)
    assert result.ok, [r.message for r in result.reasons]
    parsed = result.value
    assert len(parsed.records) == 6
    assert [r.to_dict() for r in parsed.records] == raw["records"]
    release = parsed.records_of_kind(EvidenceKind.RELEASE)[0]
    assert release.payload.scope is ReleaseScope.WATER_ONLY
    assert len(release.references) == 5
    assert parsed.by_fingerprint(release.fingerprint) is release and parsed.by_fingerprint("sha256:" + "0" * 64) is None


def test_empty_evidence_file_is_valid_and_means_no_evidence():
    result = ev.parse_evidence_file({"schema_version": 1, "records": []})
    assert result.ok and result.value.records == ()


def test_parsed_payloads_have_the_expected_typed_values():
    parsed = ev.parse_evidence_file(full_file()).value
    identity = parsed.records_of_kind(EvidenceKind.IDENTITY_ATTESTATION)[0]
    assert identity.payload.level is IdentityLevel.PORT_ATTESTED and identity.origin is EvidenceOrigin.HUMAN_ATTESTATION
    assert identity.assurance is EvidenceAssurance.ATTESTED
    image = parsed.records_of_kind(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION)[0].payload
    assert image.value_for("P3") == 0.000175235032 and image.steps_per_rev == 200
    verification = parsed.records_of_kind(EvidenceKind.PUMP_VERIFICATION)[0]
    assert verification.payload.verified_at_ml == (5.0, 10.0) and verification.subject.mapping_role == "nutrient_a_1"
    runtime = parsed.records_of_kind(EvidenceKind.RUNTIME_OBSERVATION)[0].payload
    assert runtime.definition_version == "runtime_measurement_v1" and runtime.elapsed_ms == 19000
    assert runtime.declared_concurrent_pumps == ("P1", "P3")


def test_parsed_objects_are_deeply_immutable_and_detached_from_the_input():
    raw = full_file()
    parsed = ev.parse_evidence_file(raw).value
    snapshot = [r.to_dict() for r in parsed.records]
    raw["records"][0]["payload"]["statement"] = "veraendert"
    raw["records"].append({})
    assert [r.to_dict() for r in parsed.records] == snapshot
    assert isinstance(parsed.records, tuple) and isinstance(parsed.records[0].references, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.records[0].record_id = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.records[0].payload.statement = "x"  # type: ignore[misc]
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        parsed.records[0].subject.pump = "P2"  # type: ignore[misc]


def test_parsing_does_not_mutate_the_input():
    raw = full_file()
    before = copy.deepcopy(raw)
    ev.parse_evidence_file(raw)
    assert raw == before


# --- Nachweisdatei: Struktur ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [None, [], "datei", 5, True])
def test_evidence_file_must_be_an_object(data):
    assert codes(ev.parse_evidence_file(data)) == {BlockCode.EVIDENCE_INVALID}


@pytest.mark.parametrize(
    "data",
    [
        {"records": []}, {"schema_version": 1}, {"schema_version": 2, "records": []}, {"schema_version": 0, "records": []},
        {"schema_version": True, "records": []}, {"schema_version": "1", "records": []}, {"schema_version": 1.0, "records": []},
        {"schema_version": 1, "records": {}}, {"schema_version": 1, "records": None}, {"schema_version": 1, "records": "x"},
        {"schema_version": 1, "records": [], "extra": 1}, {"schema_version": 1, "records": [None]}, {"schema_version": 1, "records": [[]]},
        {"schema_version": 1, "records": ["x"]},
    ],
)
def test_evidence_file_envelope_validation(data):
    result = ev.parse_evidence_file(data)
    assert not result.ok and result.value is None and codes(result) == {BlockCode.EVIDENCE_INVALID}


def test_too_many_records_are_rejected_before_parsing_them():
    result = ev.parse_evidence_file({"schema_version": 1, "records": [None] * (ev.MAX_RECORDS + 1)})
    assert codes(result) == {BlockCode.EVIDENCE_INVALID} and len(result.reasons) == 1


@pytest.mark.parametrize("key", ev._RECORD_KEYS)
def test_every_record_field_is_required(key):
    raw = sealed(raw_record(EvidenceKind.RUNTIME_OBSERVATION))
    del raw[key]
    result = ev.parse_evidence_file(file_of(raw))
    assert not result.ok and BlockCode.EVIDENCE_INVALID in codes(result)


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_unknown_fields_are_rejected_in_record_subject_and_payload(kind):
    for where in ("record", "subject", "payload"):
        raw = raw_record(kind)
        target = raw if where == "record" else raw[where]
        target["zusatzfeld"] = 1
        result = parse_record(raw)
        assert not result.ok, (kind, where)
        assert any("unbekannte Felder" in reason.message for reason in result.reasons), (kind, where)


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_every_payload_field_is_required(kind):
    for key in PAYLOADS[kind]():
        raw = raw_record(kind)
        del raw["payload"][key]
        assert not parse_record(raw).ok, (kind, key)


@pytest.mark.parametrize("key", ev._SUBJECT_KEYS)
def test_every_subject_field_is_required(key):
    raw = raw_record(EvidenceKind.PUMP_VERIFICATION)
    del raw["subject"][key]
    assert not parse_record(raw).ok


def test_multiple_problems_are_all_reported():
    raw = raw_record(EvidenceKind.PUMP_VERIFICATION)
    raw["payload"]["trial_count"] = 0
    raw["payload"]["verified_at_ml"] = []
    raw["payload"]["trials_digest"] = "x"
    raw["subject"]["profile_id"] = ""
    assert len(parse_record(raw).reasons) >= 4


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(record_id=""), lambda r: r.update(record_id="x" * 129), lambda r: r.update(record_id="a b"),
        lambda r: r.update(record_id=None), lambda r: r.update(record_id=5), lambda r: r.update(kind="other"),
        lambda r: r.update(origin="robot"), lambda r: r.update(assurance="proven"), lambda r: r.update(integrity="sha256_signed"),
        lambda r: r.update(integrity=None), lambda r: r.update(references="x"), lambda r: r.update(references=[5]),
        lambda r: r.update(references=["sha256:xyz"]), lambda r: r.update(references=[FP_CONFIG, FP_CONFIG]),
        lambda r: r.update(references=[FP_CONFIG] * (ev.MAX_REFERENCES + 1)), lambda r: r.update(subject=[]),
        lambda r: r.update(payload=None),
    ],
)
def test_record_header_validation(mutate):
    raw = raw_record(EvidenceKind.RELEASE)
    mutate(raw)
    result = parse_record(raw) if "fingerprint" not in raw else ev.parse_evidence_file(file_of(raw))
    assert not result.ok


# --- Zeitfelder ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["created_utc", "valid_from_utc", "valid_until_utc"])
@pytest.mark.parametrize("bad", ["2026-01-01T00:00:00", "2026-01-01T00:00:00+01:00", "gestern", 5, True, "", "2026-02-30T00:00:00Z"])
def test_record_timestamps_must_be_explicit_utc(field, bad):
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    raw[field] = bad
    assert not parse_record(raw).ok


@pytest.mark.parametrize("until", [T0, "2025-12-31T23:59:59Z"])
def test_invalid_validity_interval_is_rejected(until):
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    raw["valid_until_utc"] = until
    result = parse_record(raw)
    assert not result.ok and any("Zeitintervall" in r.message for r in result.reasons)


@pytest.mark.parametrize("kind", sorted(ev.ATTESTATION_KINDS, key=lambda k: k.value))
def test_attestations_need_an_expiry(kind):
    raw = raw_record(kind)
    raw["valid_until_utc"] = None
    result = parse_record(raw)
    assert not result.ok and any("Ablaufdatum" in r.message for r in result.reasons)


@pytest.mark.parametrize("kind", [EvidenceKind.RUNTIME_OBSERVATION, EvidenceKind.PUMP_VERIFICATION])
def test_measured_or_derived_records_may_have_open_end(kind):
    raw = raw_record(kind)
    raw["valid_until_utc"] = None
    result = parse_record(raw)
    assert result.ok and result.value.records[0].valid_until_utc is None


# --- Herkunft und Assurance ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(ev.ATTESTATION_KINDS, key=lambda k: k.value))
@pytest.mark.parametrize("assurance", ["measured", "derived"])
def test_human_attestations_are_never_labelled_measured_or_derived(kind, assurance):
    raw = raw_record(kind)
    raw["assurance"] = assurance
    assert not parse_record(raw).ok


@pytest.mark.parametrize("origin", ["host_measured", "derived"])
def test_attestations_cannot_claim_measured_origin(origin):
    raw = raw_record(EvidenceKind.IDENTITY_ATTESTATION)
    raw["origin"] = origin
    assert not parse_record(raw).ok


@pytest.mark.parametrize(
    ("kind", "origin", "assurance"),
    [
        (EvidenceKind.RUNTIME_OBSERVATION, "human_attestation", "measured"), (EvidenceKind.RUNTIME_OBSERVATION, "host_measured", "attested"),
        (EvidenceKind.RUNTIME_OBSERVATION, "derived", "measured"), (EvidenceKind.RUNTIME_OBSERVATION, "host_measured", "derived"),
        (EvidenceKind.PUMP_VERIFICATION, "human_attestation", "derived"), (EvidenceKind.PUMP_VERIFICATION, "derived", "attested"),
        (EvidenceKind.PUMP_VERIFICATION, "host_measured", "derived"), (EvidenceKind.PUMP_VERIFICATION, "derived", "measured"),
    ],
)
def test_measured_and_derived_kinds_reject_inconsistent_origin_or_assurance(kind, origin, assurance):
    raw = raw_record(kind)
    raw["origin"], raw["assurance"] = origin, assurance
    assert not parse_record(raw).ok


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_synthetic_origin_is_accepted_structurally_and_stays_visible(kind):
    raw = raw_record(kind)
    raw["origin"] = "synthetic_test"
    result = parse_record(raw)
    assert result.ok and result.value.records[0].origin is EvidenceOrigin.SYNTHETIC_TEST


def test_origin_assurance_rule_is_a_pure_total_function():
    for kind in EvidenceKind:
        for origin in EvidenceOrigin:
            for assurance in EvidenceAssurance:
                problem = ev.origin_assurance_problem(kind, origin, assurance)
                assert problem is None or isinstance(problem, str)
        assert any(ev.origin_assurance_problem(kind, o, a) is None for o in EvidenceOrigin for a in EvidenceAssurance)


# --- Fingerprint-Pruefung beim Parsen --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [k for k in EvidenceKind if k is not EvidenceKind.RELEASE])
def test_tampered_content_is_detected(kind):
    raw = make(kind)
    raw["payload"] = dict(raw["payload"])
    key = next(iter(raw["payload"]))
    raw["created_utc"] = "2026-01-02T00:00:00Z"  # Inhalt aendern, ohne neu zu siegeln
    result = ev.parse_evidence_file(file_of(raw))
    assert codes(result) == {BlockCode.EVIDENCE_FINGERPRINT_MISMATCH}
    assert key in raw["payload"]


def test_tampered_release_uses_the_release_specific_code():
    raw = make(EvidenceKind.RELEASE)
    raw["payload"]["statement"] = "nachtraeglich geaendert"
    assert codes(ev.parse_evidence_file(file_of(raw))) == {BlockCode.RELEASE_FINGERPRINT_MISMATCH}


def test_wrong_but_wellformed_fingerprint_is_detected_and_malformed_one_rejected():
    raw = make(EvidenceKind.WATER_ONLY_TEST)
    raw["fingerprint"] = "sha256:" + "0" * 64
    assert codes(ev.parse_evidence_file(file_of(raw))) == {BlockCode.EVIDENCE_FINGERPRINT_MISMATCH}
    raw["fingerprint"] = "nicht-ein-hash"
    assert codes(ev.parse_evidence_file(file_of(raw))) == {BlockCode.EVIDENCE_INVALID}


def test_fingerprint_does_not_depend_on_record_order_in_the_file():
    records = [make(k) for k in EvidenceKind if k is not EvidenceKind.RELEASE]
    assert ev.parse_evidence_file(file_of(*records)).ok and ev.parse_evidence_file(file_of(*reversed(records))).ok


def test_non_plain_values_in_a_record_are_rejected_not_hashed():
    raw = make(EvidenceKind.RUNTIME_OBSERVATION)
    raw["payload"]["requested_ml"] = float("nan")
    result = ev.parse_evidence_file(file_of(raw))
    assert not result.ok and codes(result) == {BlockCode.EVIDENCE_INVALID}


# --- Referenzen und Kette -------------------------------------------------------------------------------------------------------


def test_reference_to_unknown_fingerprint_breaks_the_chain():
    base = make(EvidenceKind.WATER_ONLY_TEST)
    release = make(EvidenceKind.RELEASE, refs=[base["fingerprint"], "sha256:" + "9" * 64])
    assert BlockCode.EVIDENCE_CHAIN_BROKEN in codes(ev.parse_evidence_file(file_of(base, release)))


def test_release_references_resolve_inside_the_file():
    base = make(EvidenceKind.WATER_ONLY_TEST)
    release = make(EvidenceKind.RELEASE, refs=[base["fingerprint"]])
    assert ev.parse_evidence_file(file_of(base, release)).ok
    assert ev.parse_evidence_file(file_of(release, base)).ok  # Reihenfolge egal


def test_release_without_references_is_rejected():
    result = parse_record(raw_record(EvidenceKind.RELEASE, refs=[]))
    assert BlockCode.RELEASE_RECORD_INVALID in codes(result)


def test_duplicate_record_ids_and_duplicate_records_are_rejected():
    first = make(EvidenceKind.WATER_ONLY_TEST)
    same_id = make(EvidenceKind.IDENTITY_ATTESTATION, record_id=first["record_id"])
    assert not ev.parse_evidence_file(file_of(first, same_id)).ok
    assert not ev.parse_evidence_file(file_of(first, copy.deepcopy(first))).ok


def test_a_record_can_never_reference_its_own_fingerprint():
    raw = raw_record(EvidenceKind.RELEASE)
    fingerprint = ev.evidence_record_fingerprint(raw)
    raw["references"] = [fingerprint]
    sealed_raw = sealed(raw)
    assert sealed_raw["fingerprint"] != fingerprint  # Fingerprint aendert sich durch die Referenz
    assert BlockCode.EVIDENCE_CHAIN_BROKEN in codes(ev.parse_evidence_file(file_of(sealed_raw)))


# --- Subjekt ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("controller", ["MCU_C", "mcu_b", "", None, 5, "MCU_B "])
def test_subject_controller_must_be_known(controller):
    raw = raw_record(EvidenceKind.WATER_ONLY_TEST)
    raw["subject"]["controller"] = controller
    assert not parse_record(raw).ok


@pytest.mark.parametrize("kind", [EvidenceKind.PUMP_VERIFICATION, EvidenceKind.RUNTIME_OBSERVATION])
@pytest.mark.parametrize("pump", [None, "P5", "p1", "", 1, "P1 "])
def test_pump_bound_kinds_need_a_valid_pump(kind, pump):
    raw = raw_record(kind)
    raw["subject"]["pump"] = pump
    assert not parse_record(raw).ok


@pytest.mark.parametrize("kind", sorted(ev.ATTESTATION_KINDS, key=lambda k: k.value))
def test_attestation_kinds_must_not_carry_a_pump_or_mapping_role(kind):
    for field, value in (("pump", "P1"), ("mapping_role", "nutrient_a_1")):
        raw = raw_record(kind)
        raw["subject"][field] = value
        assert not parse_record(raw).ok, (kind, field)


@pytest.mark.parametrize("role", [None, "", "x" * 129, 5])
def test_pump_verification_needs_a_mapping_role(role):
    raw = raw_record(EvidenceKind.PUMP_VERIFICATION)
    raw["subject"]["mapping_role"] = role
    assert not parse_record(raw).ok


@pytest.mark.parametrize("field", ["profile_id", "profile_fingerprint"])
@pytest.mark.parametrize("bad", [None, "", 5, "x" * 200])
def test_subject_profile_binding_is_mandatory(field, bad):
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    raw["subject"][field] = bad
    assert not parse_record(raw).ok


# --- Nutzlasten ---------------------------------------------------------------------------------------------------------------------


def mutated(kind, path, value):
    raw = raw_record(kind)
    target = raw["payload"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return raw


@pytest.mark.parametrize("level", ["firmware_proven", "none", "PORT_ATTESTED", None, 1])
def test_identity_file_evidence_can_only_attest_the_port(level):
    assert not parse_record(mutated(EvidenceKind.IDENTITY_ATTESTATION, ("level",), level)).ok


@pytest.mark.parametrize("path", ["", "relativ/pfad", "/dev/../etc/passwd", "/dev/tty\nUSB0", "x" * 600, None, 5])
def test_identity_port_path_must_be_a_clean_absolute_path(path):
    assert not parse_record(mutated(EvidenceKind.IDENTITY_ATTESTATION, ("port_path",), path)).ok


@pytest.mark.parametrize("path", [("attested_by",), ("statement",)])
@pytest.mark.parametrize("bad", ["", None, 5, "x" * 600, "Steuer\x00zeichen"])
def test_attestation_texts_are_bounded(path, bad):
    raw = mutated(EvidenceKind.WATER_ONLY_TEST, path, bad)
    if path == ("attested_by",) and bad == "x" * 600:
        pass
    assert not parse_record(raw).ok


def test_firmware_image_profile_id_must_match_the_subject():
    assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("profile_id",), "anderes_profil")).ok


@pytest.mark.parametrize("commit", ["", "86B7B5D", "86b7b5", "g" * 40, "86b7b5d" * 10, None, 5, "86b7b5d "])
def test_firmware_commit_must_be_a_lowercase_hex_hash(commit):
    assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("firmware_source_commit",), commit)).ok


@pytest.mark.parametrize("steps", [0, -1, 200.0, True, "200", None, 2**63, 10**400])
def test_firmware_steps_per_rev_must_be_a_positive_integer(steps):
    assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("steps_per_rev",), steps)).ok


def test_firmware_pump_values_need_exactly_the_four_pumps_with_valid_numbers():
    assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("pump_ml_per_step",), {"P1": 0.1})).ok
    full = PAYLOADS[EvidenceKind.FIRMWARE_IMAGE_ATTESTATION]()["pump_ml_per_step"]
    assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("pump_ml_per_step",), {**full, "P5": 0.1})).ok
    for bad in (True, "0.1", None, float("nan"), float("inf"), 0, -0.1, 10**400):
        assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("pump_ml_per_step", "P2"), bad)).ok, bad


@pytest.mark.parametrize("purpose", ["calibration", "calibrate", "VERIFICATION", "", None, True])
def test_only_explicit_verification_purpose_is_accepted(purpose):
    assert not parse_record(mutated(EvidenceKind.PUMP_VERIFICATION, ("purpose",), purpose)).ok


@pytest.mark.parametrize("field", ["firmware_ml_per_step_used", "verified_ml_per_step"])
@pytest.mark.parametrize("bad", [True, "0.1", None, float("nan"), float("inf"), 0, -1e-4, 10**400])
def test_verification_numbers_are_strict(field, bad):
    assert not parse_record(mutated(EvidenceKind.PUMP_VERIFICATION, (field,), bad)).ok


@pytest.mark.parametrize("count", [0, -1, 5.0, True, "5", None, 2**63])
def test_verification_trial_count_is_a_positive_integer(count):
    assert not parse_record(mutated(EvidenceKind.PUMP_VERIFICATION, ("trial_count",), count)).ok


@pytest.mark.parametrize("volumes", [[], [10.0, 5.0], [5.0, 5.0], [0.0], [-1.0, 2.0], [True], ["5"], [float("nan")], "5", None, [1.0] * (ev.MAX_LIST_ITEMS + 1)])
def test_verified_volumes_must_be_positive_and_strictly_increasing(volumes):
    assert not parse_record(mutated(EvidenceKind.PUMP_VERIFICATION, ("verified_at_ml",), volumes)).ok


def test_verification_digest_must_be_a_fingerprint():
    assert not parse_record(mutated(EvidenceKind.PUMP_VERIFICATION, ("trials_digest",), "abc")).ok


# --- Laufzeitbeobachtung (runtime_measurement_v1) ---------------------------------------------------------------------------------


def test_runtime_definition_table_is_frozen():
    assert list(ev.RUNTIME_MEASUREMENT_DEFINITIONS) == ["runtime_measurement_v1"]
    definition = ev.RUNTIME_MEASUREMENT_DEFINITIONS["runtime_measurement_v1"]
    assert (definition.clock, definition.start, definition.end) == ("monotonic_ns", "before_dose_write", "done_line_received_by_reader")
    with pytest.raises(dataclasses.FrozenInstanceError):
        definition.clock = "wall_clock"  # type: ignore[misc]


@pytest.mark.parametrize("version", ["runtime_measurement_v2", "runtime_measurement_v0", "RUNTIME_MEASUREMENT_V1", "", "v1"])
def test_unknown_measurement_definitions_get_their_own_code(version):
    result = parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("measurement", "definition_version"), version))
    assert BlockCode.RUNTIME_MEASUREMENT_DEFINITION_UNSUPPORTED in codes(result) or not result.ok
    if version:
        assert BlockCode.RUNTIME_MEASUREMENT_DEFINITION_UNSUPPORTED in codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [("clock", "wall_clock"), ("clock", "monotonic_ms"), ("start", "after_ok_start"), ("start", "ok_start_received"), ("end", "dequeued_by_consumer"), ("end", "")],
)
def test_measurement_points_must_match_the_versioned_definition(field, value):
    assert not parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("measurement", field), value)).ok


@pytest.mark.parametrize("outcome", ["ERR_TIMEOUT", "TIMEOUT", "STOP", "STOPALL", "DESYNC", "DESYNCED", "DISCONNECT", "NO_DONE", "ERR_BUSY", "BOOT_RESET", "done", "", None, True, 1, ["DONE"]])
def test_only_a_successfully_finished_run_is_valid_runtime_evidence(outcome):
    result = parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("outcome",), outcome))
    assert BlockCode.RUNTIME_EVIDENCE_INVALID_OUTCOME in codes(result)


def test_mcu_reset_during_the_run_invalidates_the_observation():
    result = parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("boot_reset_observed",), True))
    assert BlockCode.RUNTIME_EVIDENCE_INVALID_OUTCOME in codes(result)
    for bad in (None, 0, "false"):
        assert not parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("boot_reset_observed",), bad)).ok


@pytest.mark.parametrize("elapsed", [0, -1, -19000, 3.5, 19000.0, True, "19000", None, float("nan"), 2**63, 10**400])
def test_elapsed_time_must_be_a_positive_integer_millisecond_value(elapsed):
    assert not parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("elapsed_ms",), elapsed)).ok


@pytest.mark.parametrize("requested", [0, -1.0, True, "10", None, float("nan"), float("inf")])
def test_requested_volume_must_be_a_positive_finite_number(requested):
    assert not parse_record(mutated(EvidenceKind.RUNTIME_OBSERVATION, ("requested_ml",), requested)).ok


def test_declared_and_derived_parallel_groups_must_agree():
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    raw["payload"]["derived_concurrent_pumps"] = ["P1"]
    assert BlockCode.RUNTIME_GROUP_DECLARATION_MISMATCH in codes(parse_record(raw))


@pytest.mark.parametrize("group", [[], ["P3", "P1"], ["P1", "P1"], ["P5"], "P1", None, ["P1", "P2", "P3", "P4", "P1"], [1]])
def test_parallel_group_must_be_sorted_unique_known_pumps(group):
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    raw["payload"]["declared_concurrent_pumps"] = group
    raw["payload"]["derived_concurrent_pumps"] = group
    assert not parse_record(raw).ok


def test_the_observed_pump_must_belong_to_its_parallel_group():
    raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    raw["payload"]["declared_concurrent_pumps"] = ["P2", "P4"]
    raw["payload"]["derived_concurrent_pumps"] = ["P2", "P4"]
    assert not parse_record(raw).ok  # subject.pump == P1


def test_done_is_never_a_delivered_quantity():
    payload_fields = {field.name for field in dataclasses.fields(ev.RuntimeObservationPayload)}
    assert not payload_fields & {"measured_ml", "delivered_ml", "pumped_ml", "done_ml", "volume_ml"}
    for forbidden in ("measured_ml", "delivered_ml", "done_ml"):
        raw = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
        raw["payload"][forbidden] = 10.0
        assert not parse_record(raw).ok


# --- Water-Only-Test und Freigabe -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path, value", [(("result",), "failed"), (("result",), "PASSED"), (("water_only_confirmed",), False), (("water_only_confirmed",), 1),
                                         (("water_only_confirmed",), None), (("pumps_tested",), []), (("pumps_tested",), ["P2", "P1"]),
                                         (("max_chunk_ml",), True), (("max_chunk_ml",), 0), (("max_chunk_ml",), float("nan")), (("test_id",), "")])
def test_water_only_test_validation(path, value):
    assert not parse_record(mutated(EvidenceKind.WATER_ONLY_TEST, path, value)).ok


def test_release_scope_rules():
    assert parse_record(mutated(EvidenceKind.RELEASE, ("scope",), "chemical")).ok
    for scope in ("simulation", "SIMULATION", "live", None, True):
        assert not parse_record(mutated(EvidenceKind.RELEASE, ("scope",), scope)).ok
    assert BlockCode.RELEASE_RECORD_INVALID in codes(parse_record(mutated(EvidenceKind.RELEASE, ("scope",), "simulation")))


@pytest.mark.parametrize("path, value", [(("config_fingerprint",), "abc"), (("config_fingerprint",), None), (("granted_at",), "2026-01-01T00:00:00"),
                                         (("granted_at",), 5), (("granted_by",), ""), (("statement",), "")])
def test_release_payload_validation(path, value):
    assert not parse_record(mutated(EvidenceKind.RELEASE, path, value)).ok


def test_release_payload_carries_its_scope_inside_the_hashed_content():
    water = sealed(raw_record(EvidenceKind.RELEASE))
    chemical = sealed(mutated(EvidenceKind.RELEASE, ("scope",), "chemical"))
    assert water["fingerprint"] != chemical["fingerprint"]


# --- Sitzungsdaten ----------------------------------------------------------------------------------------------------------------------


def identity_args(**overrides):
    args = dict(session_id="sess_1", generation=0, port_realpath="/dev/ttyUSB0", observed_mcu_id=None,
                level=IdentityLevel.PORT_ATTESTED, established_monotonic_ns=123)
    args.update(overrides)
    return args


def test_identity_proof_basics_and_immutability():
    proof = ev.IdentityProof(**identity_args())
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.level = IdentityLevel.FIRMWARE_PROVEN  # type: ignore[misc]
    assert ev.IdentityProof(**identity_args(level=IdentityLevel.FIRMWARE_PROVEN, observed_mcu_id="MCU_B_EC")).level is IdentityLevel.FIRMWARE_PROVEN
    assert ev.IdentityProof(**identity_args(level=IdentityLevel.NONE)).observed_mcu_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": ""}, {"session_id": None}, {"session_id": "a b"}, {"generation": -1}, {"generation": True}, {"generation": 1.0},
        {"generation": "0"}, {"generation": 2**63}, {"port_realpath": "ttyUSB0"}, {"port_realpath": "/dev/../x"}, {"port_realpath": ""},
        {"observed_mcu_id": ""}, {"observed_mcu_id": 5}, {"level": "port_attested"}, {"level": None}, {"established_monotonic_ns": -1},
        {"established_monotonic_ns": 1.5}, {"established_monotonic_ns": True}, {"level": IdentityLevel.FIRMWARE_PROVEN, "observed_mcu_id": None},
    ],
)
def test_identity_proof_rejects_invalid_values(overrides):
    with pytest.raises(ev.EvidenceModelError):
        ev.IdentityProof(**identity_args(**overrides))


def claim_args(**overrides):
    args = dict(controller="MCU_B", lock_kind="flock", session_id="sess_1", generation=0, port_realpath="/dev/ttyUSB0", holder_pid=4242)
    args.update(overrides)
    return args


def test_mcu_ownership_claim_basics():
    claim = ev.McuOwnershipClaim(**claim_args())
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.holder_pid = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"controller": "MCU_C"}, {"controller": None}, {"lock_kind": "file"}, {"lock_kind": "FLOCK"}, {"session_id": ""}, {"generation": -1},
        {"generation": True}, {"port_realpath": "relativ"}, {"holder_pid": 0}, {"holder_pid": -5}, {"holder_pid": True}, {"holder_pid": 1.0},
        {"holder_pid": 2**63}, {"holder_pid": "42"},
    ],
)
def test_mcu_ownership_claim_rejects_invalid_values(overrides):
    with pytest.raises(ev.EvidenceModelError):
        ev.McuOwnershipClaim(**claim_args(**overrides))


# --- Statische Eigenschaften ------------------------------------------------------------------------------------------------------------


def test_evidence_module_exposes_no_authorization_type():
    source = open(ev.__file__, encoding="utf-8").read()  # nur Quelltext lesen (Testcode)
    assert "class DispatchAuthorization" not in source and "def authorize_dispatch" not in source
    import re

    assert not re.search(r"^(TEST|PRODUCTION)_RULES\s*[:=]", source, re.MULTILINE)


def test_evidence_kind_values_are_frozen():
    assert [kind.value for kind in EvidenceKind] == [
        "identity_attestation", "firmware_image_attestation", "pump_verification", "runtime_observation", "water_only_test", "release",
    ]
    assert ev.INTEGRITY_UNSIGNED_SHA256 == "unsigned_sha256"
    assert json.loads(json.dumps(ev.parse_evidence_file(full_file()).value.records[0].to_dict()))


# --- Robustheit: nie eine Ausnahme nach aussen --------------------------------------------------------------------------------------------

NASTY_VALUES = [None, [], {}, "", "x" * 1000, "mit\nZeilenumbruch", float("nan"), float("inf"), 2**70, 10**400, True, object(), ("t",), b"b"]


def _paths(value, prefix=()):
    yield prefix
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _paths(item, prefix + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _paths(item, prefix + (index,))


def _replace(raw, path, new):
    clone = copy.deepcopy(raw)
    if not path:
        return new
    target = clone
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = new
    return clone


def test_parsers_never_raise_for_hostile_values_at_any_position():
    raw = full_file()
    checked = 0
    for path in _paths(raw):
        for nasty in NASTY_VALUES:
            result = ev.parse_evidence_file(_replace(raw, path, nasty))
            assert isinstance(result, ev.ParseResult)
            if result.ok:
                assert not result.reasons and result.value is not None
            else:
                assert result.reasons and all(r.message and len(r.message) <= 500 for r in result.reasons)
            checked += 1
    assert checked > 1000


def test_policy_parser_never_raises_for_hostile_values_at_any_position():
    raw = valid_policy()
    for path in _paths(raw):
        for nasty in NASTY_VALUES:
            result = ev.parse_policy(_replace(raw, path, nasty))
            assert isinstance(result, ev.ParseResult)
            assert result.ok or result.reasons


def test_hostile_keys_do_not_break_error_reporting():
    raw = raw_record(EvidenceKind.RELEASE)
    raw["bad\nkey"] = 1
    raw["k" * 1000] = 2
    raw[5] = 3
    result = ev.parse_evidence_file(file_of(raw))
    assert not result.ok and all(len(r.message) <= 500 and "\n" not in r.message for r in result.reasons)
    policy = valid_policy()
    policy["parameters"]["wild\x00key"] = 1
    assert not ev.parse_policy(policy).ok


def test_identifiers_with_trailing_newline_are_rejected():
    for field in ("record_id",):
        raw = raw_record(EvidenceKind.WATER_ONLY_TEST)
        raw[field] = "abc\n"
        assert not parse_record(raw).ok
    assert not parse_record(mutated(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, ("firmware_source_commit",), "86b7b5d\n")).ok


# =============================================================================================================
# R2-B1: Fingerprint-Stabilitaet bei Roundtrips (normalisierter Inhalt, keine Neusignierung)
# =============================================================================================================


def with_fields(kind, **path_values):
    """Rohen Eintrag bauen und tiefe Felder setzen: key 'payload.requested_ml' -> Wert."""
    raw = raw_record(kind)
    for dotted, value in path_values.items():
        target = raw
        keys = dotted.split("__")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return raw


def roundtrip_ok(raw_sealed: dict):
    first = ev.parse_evidence_file(file_of(raw_sealed))
    assert first.ok, [r.message for r in first.reasons]
    record = first.value.records[0]
    again = ev.parse_evidence_file(file_of(record.to_dict()))
    assert again.ok, [r.message for r in again.reasons]
    assert again.value.records[0] == record
    assert again.value.records[0].fingerprint == raw_sealed["fingerprint"]
    return record


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_every_kind_survives_parse_to_dict_parse(kind):
    parent = None
    if kind is EvidenceKind.RELEASE:
        parent = make(EvidenceKind.WATER_ONLY_TEST)
        raw = sealed(raw_record(kind, refs=[parent["fingerprint"]]))
        records = [raw, parent]
    else:
        raw = make(kind)
        records = [raw]
    first = ev.parse_evidence_file(file_of(*records))
    assert first.ok, [r.message for r in first.reasons]
    second = ev.parse_evidence_file(first.value.to_dict())
    assert second.ok, [r.message for r in second.reasons]
    assert second.value == first.value
    assert [r.fingerprint for r in second.value.records] == [r.fingerprint for r in first.value.records]


def test_whole_file_with_all_six_kinds_roundtrips():
    first = ev.parse_evidence_file(full_file()).value
    again = ev.parse_evidence_file(first.to_dict())
    assert again.ok and again.value == first


@pytest.mark.parametrize("stamp", ["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.0+00:00"])
def test_utc_z_and_plus_zero_offset_give_the_same_fingerprint_and_roundtrip(stamp):
    reference = make(EvidenceKind.RUNTIME_OBSERVATION)
    raw = sealed(with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc=stamp, valid_from_utc=stamp))
    assert raw["fingerprint"] == reference["fingerprint"]
    record = roundtrip_ok(raw)
    assert record.to_dict()["created_utc"] == "2026-01-01T00:00:00Z"  # kanonische Ausgabe
    assert raw["created_utc"] == stamp  # Darstellung der Eingabe bleibt beim Siegeln erhalten


@pytest.mark.parametrize("fraction_forms", [("2026-03-04T05:06:07.5Z", "2026-03-04T05:06:07.500000Z", "2026-03-04T05:06:07.50+00:00"),
                                            ("2026-03-04T05:06:07.000001Z", "2026-03-04T05:06:07.000001+00:00"),
                                            ("2026-03-04T05:06:07.123Z", "2026-03-04T05:06:07.123000Z")])
def test_different_valid_fraction_spellings_are_equivalent(fraction_forms):
    fingerprints = {sealed(with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc=stamp, valid_from_utc=stamp))["fingerprint"] for stamp in fraction_forms}
    assert len(fingerprints) == 1
    for stamp in fraction_forms:
        roundtrip_ok(sealed(with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc=stamp, valid_from_utc=stamp)))


def test_a_sealed_record_stays_valid_when_only_the_utc_spelling_changes():
    raw = make(EvidenceKind.WATER_ONLY_TEST)
    spelled = copy.deepcopy(raw)
    spelled["created_utc"] = "2026-01-01T00:00:00+00:00"
    assert ev.parse_evidence_file(file_of(spelled)).ok


@pytest.mark.parametrize(
    ("kind", "int_variant"),
    [
        (EvidenceKind.RUNTIME_OBSERVATION, {"payload__requested_ml": 10}),
        (EvidenceKind.WATER_ONLY_TEST, {"payload__max_chunk_ml": 10}),
        (EvidenceKind.PUMP_VERIFICATION, {"payload__verified_at_ml": [5, 10]}),
        (EvidenceKind.PUMP_VERIFICATION, {"payload__verified_ml_per_step": 1, "payload__firmware_ml_per_step_used": 2}),
    ],
)
def test_int_and_float_spellings_of_numeric_fields_are_equivalent(kind, int_variant):
    float_variant = {key: ([float(x) for x in value] if isinstance(value, list) else float(value)) for key, value in int_variant.items()}
    as_int = sealed(with_fields(kind, **int_variant))
    as_float = sealed(with_fields(kind, **float_variant))
    assert as_int["fingerprint"] == as_float["fingerprint"]
    record = roundtrip_ok(as_int)
    assert record.to_dict()["payload"] == record.payload.to_dict()
    assert as_int["payload"] == with_fields(kind, **int_variant)["payload"]  # Eingabedarstellung beim Siegeln unveraendert


def test_firmware_pump_values_given_as_int_like_floats_roundtrip():
    raw = sealed(with_fields(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, payload__pump_ml_per_step={"P1": 1, "P2": 2, "P3": 3, "P4": 4}))
    record = roundtrip_ok(raw)
    assert record.payload.value_for("P2") == 2.0 and type(record.payload.value_for("P2")) is float


def test_integer_valued_fields_stay_strict_after_the_change():
    for kind, field in ((EvidenceKind.RUNTIME_OBSERVATION, "payload__elapsed_ms"), (EvidenceKind.PUMP_VERIFICATION, "payload__trial_count"),
                        (EvidenceKind.FIRMWARE_IMAGE_ATTESTATION, "payload__steps_per_rev")):
        assert not parse_record(with_fields(kind, **{field: 5.0})).ok


def test_seal_does_not_mutate_its_input_and_preserves_its_spelling():
    raw = with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc="2026-01-01T00:00:00+00:00", payload__requested_ml=10)
    before = copy.deepcopy(raw)
    result = ev.seal_evidence_record(raw)
    assert raw == before and "fingerprint" not in raw
    assert result["created_utc"] == "2026-01-01T00:00:00+00:00" and result["payload"]["requested_ml"] == 10


def test_record_fingerprint_function_agrees_with_seal_and_ignores_spelling():
    plain = raw_record(EvidenceKind.RUNTIME_OBSERVATION)
    spelled = with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc="2026-01-01T00:00:00+00:00", payload__requested_ml=10)
    assert ev.evidence_record_fingerprint(plain) == ev.evidence_record_fingerprint(spelled) == ev.seal_evidence_record(plain)["fingerprint"]


@pytest.mark.parametrize(
    "change",
    [
        {"payload__elapsed_ms": 19001}, {"payload__requested_ml": 11.0}, {"payload__tool_version": "other_tool"}, {"record_id": "rec_other"},
        {"created_utc": "2026-01-01T00:00:01Z"}, {"valid_from_utc": "2026-01-01T00:00:01Z"}, {"valid_until_utc": "2027-01-01T00:00:00Z"},
        {"valid_until_utc": None}, {"subject__pump": "P3"}, {"subject__profile_id": "other_profile"}, {"subject__profile_fingerprint": "sha256:" + "5" * 64},
        {"references": ["sha256:" + "6" * 64]}, {"origin": "synthetic_test"},
    ],
)
def test_real_content_changes_after_sealing_are_still_detected(change):
    original = make(EvidenceKind.RUNTIME_OBSERVATION)
    tampered = copy.deepcopy(original)
    for dotted, value in change.items():
        target = tampered
        keys = dotted.split("__")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    result = ev.parse_evidence_file(file_of(tampered))
    assert BlockCode.EVIDENCE_FINGERPRINT_MISMATCH in codes(result), [r.message for r in result.reasons]


def test_tampered_payload_of_every_other_kind_is_still_rejected():
    changes = {
        EvidenceKind.IDENTITY_ATTESTATION: ("payload", "statement", "anders"),
        EvidenceKind.FIRMWARE_IMAGE_ATTESTATION: ("payload", "steps_per_rev", 201),
        EvidenceKind.PUMP_VERIFICATION: ("payload", "trial_count", 6),
        EvidenceKind.WATER_ONLY_TEST: ("payload", "max_chunk_ml", 11.0),
        EvidenceKind.RELEASE: ("payload", "statement", "anders"),
    }
    for kind, (section, field, value) in changes.items():
        raw = make(kind)
        raw[section][field] = value
        assert not ev.parse_evidence_file(file_of(raw)).ok, kind


def test_policy_fingerprint_is_stable_for_int_float_spelling_and_roundtrips():
    as_float = valid_policy()
    as_int = valid_policy()
    for name in ("max_verification_age_days", "runtime_margin_ms", "authorization_ttl_s"):
        as_float["parameters"][name] = 2.0
        as_int["parameters"][name] = 2
    first = ev.parse_policy(as_float).value
    second = ev.parse_policy(as_int).value
    assert first.fingerprint == second.fingerprint == ev.policy_fingerprint(as_int)
    again = ev.parse_policy(first.to_dict())
    assert again.ok and again.value == first
    changed = valid_policy()
    changed["parameters"]["runtime_margin_ms"] = 2.5
    assert ev.parse_policy(changed).value.fingerprint != first.fingerprint


def test_draft_policy_roundtrips():
    draft = ev.parse_policy(draft_policy()).value
    assert ev.parse_policy(draft.to_dict()).value == draft


# =============================================================================================================
# R2-B2: Direkte Modellkonstruktion hat denselben Validierungsvertrag wie die Parser
# =============================================================================================================


def parsed_file():
    return ev.parse_evidence_file(full_file()).value


def record_kwargs(record):
    kwargs = {name: getattr(record, name) for name in ev._RECORD_FIELDS}
    kwargs["fingerprint"] = record.fingerprint
    return kwargs


def payload_kwargs(payload):
    return {f.name: getattr(payload, f.name) for f in dataclasses.fields(payload)}


def good_subject(**overrides):
    args = dict(controller="MCU_B", pump="P1", mapping_role="nutrient_a_1", profile_id="mcu_b_ec_8_microsteps_cal_v2", profile_fingerprint=FP_PROFILE)
    args.update(overrides)
    return args


def test_the_reported_invalid_subject_is_now_rejected():
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceSubject(controller="MCU_X", pump="P999", mapping_role="acid", profile_id="bad", profile_fingerprint="junk")


@pytest.mark.parametrize(
    "overrides",
    [
        {"controller": "MCU_X"}, {"controller": None}, {"controller": 5}, {"pump": "P999"}, {"pump": "p1"}, {"pump": 1}, {"pump": ""},
        {"mapping_role": ""}, {"mapping_role": 5}, {"mapping_role": "a b"}, {"profile_id": ""}, {"profile_id": None}, {"profile_id": "x" * 200},
        {"profile_fingerprint": "junk"}, {"profile_fingerprint": None}, {"profile_fingerprint": "sha256:" + "A" * 64},
    ],
)
def test_evidence_subject_rejects_invalid_values_and_reports_problems(overrides):
    with pytest.raises(ev.EvidenceModelError) as info:
        ev.EvidenceSubject(**good_subject(**overrides))
    assert info.value.problems and all(isinstance(message, str) and message for _, message in info.value.problems)


def test_evidence_subject_valid_and_immutable():
    subject_obj = ev.EvidenceSubject(**good_subject(pump=None, mapping_role=None))
    assert subject_obj.pump is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        subject_obj.controller = "MCU_A"  # type: ignore[misc]


def test_model_error_collects_all_problems_not_just_the_first():
    with pytest.raises(ev.EvidenceModelError) as info:
        ev.EvidenceSubject("MCU_X", "P999", "", "", "junk")
    assert len(info.value.problems) >= 5
    assert isinstance(ev.EvidenceModelError("text").problems[0][1], str)


# --- ReleasePolicy -----------------------------------------------------------------------------------


def policy_kwargs(policy):
    return {f.name: getattr(policy, f.name) for f in dataclasses.fields(policy)}


def test_valid_release_policy_can_be_constructed_directly_with_the_correct_fingerprint():
    parsed = ev.parse_policy(valid_policy()).value
    assert ev.ReleasePolicy(**policy_kwargs(parsed)) == parsed


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2}, {"schema_version": True}, {"schema_version": "1"}, {"kind": "production"}, {"kind": None}, {"policy_id": ""},
        {"policy_id": "a b"}, {"policy_id": None}, {"revision": 0}, {"revision": True}, {"revision": 1.0}, {"parameters": {}},
        {"parameters": None}, {"fingerprint": "junk"}, {"fingerprint": None}, {"fingerprint": "sha256:" + "0" * 64},
    ],
)
def test_release_policy_rejects_invalid_direct_construction(overrides):
    kwargs = policy_kwargs(ev.parse_policy(valid_policy()).value)
    kwargs.update(overrides)
    with pytest.raises(ev.EvidenceModelError):
        ev.ReleasePolicy(**kwargs)


def test_release_policy_fingerprint_must_match_the_content():
    kwargs = policy_kwargs(ev.parse_policy(valid_policy()).value)
    other = ev.parse_policy(draft_policy()).value
    kwargs["fingerprint"] = other.fingerprint
    with pytest.raises(ev.EvidenceModelError):
        ev.ReleasePolicy(**kwargs)


def test_release_policy_is_immutable():
    policy = ev.parse_policy(valid_policy()).value
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.kind = PolicyKind.PRODUCTION  # type: ignore[misc]


# --- Nutzlasten: direkte Konstruktion -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_every_valid_payload_can_be_rebuilt_directly_and_equals_the_parsed_one(kind):
    record = parsed_file().records_of_kind(kind)[0]
    payload = record.payload
    assert type(payload)(**payload_kwargs(payload)) == payload


PAYLOAD_BAD_VALUES = {
    EvidenceKind.IDENTITY_ATTESTATION: [("level", IdentityLevel.FIRMWARE_PROVEN), ("level", IdentityLevel.NONE), ("level", "port_attested"), ("port_path", "relativ"),
                                         ("port_path", "/dev/../x"), ("attested_by", ""), ("statement", "")],
    EvidenceKind.FIRMWARE_IMAGE_ATTESTATION: [("profile_id", ""), ("firmware_source_commit", "XYZ"), ("steps_per_rev", 0), ("steps_per_rev", 200.0), ("steps_per_rev", True),
                                              ("pump_ml_per_step", (("P1", 0.1),)), ("pump_ml_per_step", {"P1": 1, "P2": 1, "P3": 1}),
                                              ("pump_ml_per_step", {"P1": 1, "P2": 1, "P3": 1, "P4": float("nan")}), ("pump_ml_per_step", {"P1": 1, "P2": 1, "P3": 1, "P4": True}),
                                              ("pump_ml_per_step", {"P1": 1, "P2": 1, "P3": 1, "P5": 1}), ("pump_ml_per_step", "text"), ("pump_ml_per_step", [1, 2, 3, 4])],
    EvidenceKind.PUMP_VERIFICATION: [("purpose", "calibration"), ("firmware_ml_per_step_used", 0), ("verified_ml_per_step", float("inf")), ("trial_count", 0),
                                      ("verified_at_ml", ()), ("verified_at_ml", (10.0, 5.0)), ("verified_at_ml", "5"), ("trials_digest", "x")],
    EvidenceKind.RUNTIME_OBSERVATION: [("definition_version", "runtime_measurement_v2"), ("clock", "wall"), ("start", "after_ok_start"), ("end", "dequeued"),
                                        ("requested_ml", 0), ("elapsed_ms", 0), ("elapsed_ms", 1.5), ("outcome", "ERR_TIMEOUT"), ("boot_reset_observed", True),
                                        ("boot_reset_observed", "no"), ("declared_concurrent_pumps", ("P2",)), ("declared_concurrent_pumps", ()),
                                        ("derived_concurrent_pumps", "P1"), ("tool_version", "")],
    EvidenceKind.WATER_ONLY_TEST: [("test_id", ""), ("pumps_tested", ("P2", "P1")), ("pumps_tested", []), ("max_chunk_ml", True), ("result", "failed"),
                                    ("water_only_confirmed", False), ("water_only_confirmed", 1), ("statement", "")],
    EvidenceKind.RELEASE: [("scope", ReleaseScope.SIMULATION), ("scope", "water_only"), ("config_fingerprint", "junk"), ("granted_by", ""),
                            ("granted_at", datetime(2026, 1, 1)), ("granted_at", "2026-01-01T00:00:00Z"), ("statement", "")],
}


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [(kind, field, value) for kind, items in PAYLOAD_BAD_VALUES.items() for field, value in items],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_payload_dataclasses_reject_invalid_direct_construction(kind, field, value):
    payload = parsed_file().records_of_kind(kind)[0].payload
    kwargs = payload_kwargs(payload)
    kwargs[field] = value
    with pytest.raises(ev.EvidenceModelError):
        type(payload)(**kwargs)


def test_payload_codes_are_the_same_for_direct_construction_and_parser():
    payload = parsed_file().records_of_kind(EvidenceKind.RUNTIME_OBSERVATION)[0].payload
    cases = {
        ("definition_version", "runtime_measurement_v2"): BlockCode.RUNTIME_MEASUREMENT_DEFINITION_UNSUPPORTED,
        ("outcome", "ERR_TIMEOUT"): BlockCode.RUNTIME_EVIDENCE_INVALID_OUTCOME,
        ("boot_reset_observed", True): BlockCode.RUNTIME_EVIDENCE_INVALID_OUTCOME,
    }
    for (field, value), expected in cases.items():
        kwargs = payload_kwargs(payload)
        kwargs[field] = value
        with pytest.raises(ev.EvidenceModelError) as info:
            ev.RuntimeObservationPayload(**kwargs)
        assert expected in {code for code, _ in info.value.problems}
    kwargs = payload_kwargs(payload)
    kwargs["derived_concurrent_pumps"] = ("P1",)
    with pytest.raises(ev.EvidenceModelError) as info:
        ev.RuntimeObservationPayload(**kwargs)
    assert BlockCode.RUNTIME_GROUP_DECLARATION_MISMATCH in {code for code, _ in info.value.problems}
    release = payload_kwargs(parsed_file().records_of_kind(EvidenceKind.RELEASE)[0].payload)
    release["scope"] = ReleaseScope.SIMULATION
    with pytest.raises(ev.EvidenceModelError) as info:
        ev.ReleasePayload(**release)
    assert BlockCode.RELEASE_RECORD_INVALID in {code for code, _ in info.value.problems}


def test_firmware_payload_does_not_keep_a_mutable_reference_to_its_input():
    source = {"P1": 0.1, "P2": 0.2, "P3": 0.3, "P4": 0.4}
    kwargs = payload_kwargs(parsed_file().records_of_kind(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION)[0].payload)
    kwargs["pump_ml_per_step"] = source
    payload = ev.FirmwareImageAttestationPayload(**kwargs)
    source["P1"] = 999.0
    source["P9"] = 1.0
    assert payload.value_for("P1") == 0.1
    assert isinstance(payload.pump_ml_per_step, tuple) and all(type(pair) is tuple for pair in payload.pump_ml_per_step)
    as_list = [["P4", 0.4], ["P3", 0.3], ["P2", 0.2], ["P1", 0.1]]
    kwargs["pump_ml_per_step"] = as_list
    payload = ev.FirmwareImageAttestationPayload(**kwargs)
    as_list[0][1] = 5.0
    assert [pump for pump, _ in payload.pump_ml_per_step] == ["P1", "P2", "P3", "P4"] and payload.value_for("P4") == 0.4


@pytest.mark.parametrize(
    ("kind", "field"),
    [(EvidenceKind.PUMP_VERIFICATION, "verified_at_ml"), (EvidenceKind.RUNTIME_OBSERVATION, "declared_concurrent_pumps"),
     (EvidenceKind.RUNTIME_OBSERVATION, "derived_concurrent_pumps"), (EvidenceKind.WATER_ONLY_TEST, "pumps_tested")],
)
def test_list_inputs_are_copied_into_tuples(kind, field):
    payload = parsed_file().records_of_kind(kind)[0].payload
    kwargs = payload_kwargs(payload)
    source = list(kwargs[field])
    kwargs[field] = source
    if kind is EvidenceKind.RUNTIME_OBSERVATION:
        other = "derived_concurrent_pumps" if field == "declared_concurrent_pumps" else "declared_concurrent_pumps"
        kwargs[other] = list(kwargs[other])
    rebuilt = type(payload)(**kwargs)
    source.append(source[-1])
    source.clear()
    assert getattr(rebuilt, field) == getattr(payload, field) and isinstance(getattr(rebuilt, field), tuple)


def test_payload_numbers_are_normalized_to_float_like_the_parser_does():
    kwargs = payload_kwargs(parsed_file().records_of_kind(EvidenceKind.WATER_ONLY_TEST)[0].payload)
    kwargs["max_chunk_ml"] = 10
    assert type(ev.WaterOnlyTestPayload(**kwargs).max_chunk_ml) is float
    kwargs = payload_kwargs(parsed_file().records_of_kind(EvidenceKind.PUMP_VERIFICATION)[0].payload)
    kwargs["verified_at_ml"] = [5, 10]
    kwargs["verified_ml_per_step"] = 1
    built = ev.PumpVerificationPayload(**kwargs)
    assert built.verified_at_ml == (5.0, 10.0) and type(built.verified_ml_per_step) is float


def test_payloads_are_immutable_after_construction():
    for kind in EvidenceKind:
        payload = parsed_file().records_of_kind(kind)[0].payload
        field = dataclasses.fields(payload)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(payload, field, "x")


def test_release_granted_at_is_normalized_to_utc_and_must_be_aware():
    kwargs = payload_kwargs(parsed_file().records_of_kind(EvidenceKind.RELEASE)[0].payload)
    plus_two = timezone(__import__("datetime").timedelta(hours=2))
    kwargs["granted_at"] = datetime(2026, 1, 1, 2, 0, tzinfo=plus_two)
    with pytest.raises(ev.EvidenceModelError):
        ev.ReleasePayload(**kwargs)  # anderer Offset ist kein UTC-Bezug
    kwargs["granted_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ev.ReleasePayload(**kwargs).granted_at.tzinfo is timezone.utc


# --- EvidenceRecord: direkte Konstruktion -----------------------------------------------------------------------


def test_valid_record_can_be_rebuilt_directly_for_every_kind():
    for record in parsed_file().records:
        assert ev.EvidenceRecord(**record_kwargs(record)) == record


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_id", ""), ("record_id", None), ("kind", "release"), ("kind", None), ("origin", "human_attestation"), ("assurance", "attested"),
        ("created_utc", datetime(2026, 1, 1)), ("created_utc", "2026-01-01T00:00:00Z"), ("valid_from_utc", None),
        ("valid_until_utc", datetime(2025, 1, 1, tzinfo=timezone.utc)), ("valid_until_utc", None), ("subject", None),
        ("subject", {"controller": "MCU_B"}), ("payload", None), ("payload", {"statement": "x"}), ("references", "x"), ("references", [5]),
        ("references", ["junk"]), ("fingerprint", "junk"), ("fingerprint", None), ("fingerprint", "sha256:" + "0" * 64),
    ],
)
def test_evidence_record_rejects_invalid_direct_construction(field, value):
    record = parsed_file().records_of_kind(EvidenceKind.WATER_ONLY_TEST)[0]
    kwargs = record_kwargs(record)
    kwargs[field] = value
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceRecord(**kwargs)


def test_record_payload_type_must_match_its_kind():
    file = parsed_file()
    kwargs = record_kwargs(file.records_of_kind(EvidenceKind.WATER_ONLY_TEST)[0])
    kwargs["payload"] = file.records_of_kind(EvidenceKind.IDENTITY_ATTESTATION)[0].payload
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceRecord(**kwargs)


def test_record_subject_and_payload_rules_apply_to_direct_construction():
    file = parsed_file()
    runtime = record_kwargs(file.records_of_kind(EvidenceKind.RUNTIME_OBSERVATION)[0])
    runtime["subject"] = ev.EvidenceSubject(**good_subject(pump=None, mapping_role=None))
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceRecord(**runtime)  # Pumpe Pflicht
    runtime["subject"] = ev.EvidenceSubject(**good_subject(pump="P2", mapping_role=None))
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceRecord(**runtime)  # Pumpe nicht in der Parallelgruppe {P1,P3}
    water = record_kwargs(file.records_of_kind(EvidenceKind.WATER_ONLY_TEST)[0])
    water["subject"] = ev.EvidenceSubject(**good_subject(pump="P1", mapping_role=None))
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceRecord(**water)  # Attestierung traegt keine Pumpe
    firmware = record_kwargs(file.records_of_kind(EvidenceKind.FIRMWARE_IMAGE_ATTESTATION)[0])
    firmware["subject"] = ev.EvidenceSubject(**good_subject(pump=None, mapping_role=None, profile_id="anderes_profil"))
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceRecord(**firmware)  # profile_id widerspricht der Nutzlast


def test_record_origin_assurance_and_expiry_rules_apply_to_direct_construction():
    record = parsed_file().records_of_kind(EvidenceKind.WATER_ONLY_TEST)[0]
    for overrides in ({"assurance": EvidenceAssurance.MEASURED}, {"origin": EvidenceOrigin.HOST_MEASURED}, {"valid_until_utc": None}):
        kwargs = record_kwargs(record)
        kwargs.update(overrides)
        with pytest.raises(ev.EvidenceModelError):
            ev.EvidenceRecord(**kwargs)


def test_release_record_without_references_cannot_be_constructed_directly():
    release = parsed_file().records_of_kind(EvidenceKind.RELEASE)[0]
    kwargs = record_kwargs(release)
    kwargs["references"] = []
    with pytest.raises(ev.EvidenceModelError) as info:
        ev.EvidenceRecord(**kwargs)
    assert BlockCode.RELEASE_RECORD_INVALID in {code for code, _ in info.value.problems}


def test_record_fingerprint_mismatch_uses_the_specific_codes():
    file = parsed_file()
    for kind, expected in ((EvidenceKind.WATER_ONLY_TEST, BlockCode.EVIDENCE_FINGERPRINT_MISMATCH), (EvidenceKind.RELEASE, BlockCode.RELEASE_FINGERPRINT_MISMATCH)):
        kwargs = record_kwargs(file.records_of_kind(kind)[0])
        kwargs["fingerprint"] = "sha256:" + "0" * 64
        with pytest.raises(ev.EvidenceModelError) as info:
            ev.EvidenceRecord(**kwargs)
        assert expected in {code for code, _ in info.value.problems}


def test_record_does_not_keep_references_to_a_mutable_input_list():
    release = parsed_file().records_of_kind(EvidenceKind.RELEASE)[0]
    refs = list(release.references)
    kwargs = record_kwargs(release)
    kwargs["references"] = refs
    rebuilt = ev.EvidenceRecord(**kwargs)
    refs.clear()
    assert rebuilt.references == release.references and isinstance(rebuilt.references, tuple)


def test_record_datetimes_are_normalized_and_immutable():
    record = parsed_file().records[0]
    assert record.created_utc.tzinfo is timezone.utc
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.fingerprint = "sha256:" + "0" * 64  # type: ignore[misc]


# --- EvidenceFile: direkte Konstruktion -------------------------------------------------------------------------


def test_valid_evidence_file_can_be_constructed_directly_and_copies_its_list():
    parsed = parsed_file()
    records = list(parsed.records)
    rebuilt = ev.EvidenceFile(1, records)  # type: ignore[arg-type]
    records.clear()
    assert rebuilt == parsed and isinstance(rebuilt.records, tuple)
    assert ev.EvidenceFile(1, ()).records == ()


@pytest.mark.parametrize("version", [0, 2, True, "1", 1.0, None, -1])
def test_evidence_file_rejects_invalid_schema_version(version):
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceFile(version, ())  # type: ignore[arg-type]


@pytest.mark.parametrize("records", [None, "x", {}, [None], [{}], ["record"], [object()]])
def test_evidence_file_rejects_invalid_records_container(records):
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceFile(1, records)  # type: ignore[arg-type]


def test_evidence_file_rejects_duplicates_and_broken_chains_when_built_directly():
    parsed = parsed_file()
    first = parsed.records[0]
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceFile(1, (first, first))
    release = parsed.records_of_kind(EvidenceKind.RELEASE)[0]
    with pytest.raises(ev.EvidenceModelError) as info:
        ev.EvidenceFile(1, (release,))  # Referenzen zeigen auf nicht enthaltene Eintraege
    assert BlockCode.EVIDENCE_CHAIN_BROKEN in {code for code, _ in info.value.problems}


def test_evidence_file_rejects_too_many_records_without_building_them():
    with pytest.raises(ev.EvidenceModelError):
        ev.EvidenceFile(1, [None] * (ev.MAX_RECORDS + 1))  # type: ignore[arg-type]


def test_evidence_file_is_immutable():
    parsed = parsed_file()
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.records = ()  # type: ignore[misc]


# --- Parser und Konstruktor widersprechen sich nicht ------------------------------------------------------------------


def test_everything_the_parser_accepts_can_be_rebuilt_by_the_constructors():
    parsed = parsed_file()
    for record in parsed.records:
        subject_obj = ev.EvidenceSubject(**{f.name: getattr(record.subject, f.name) for f in dataclasses.fields(record.subject)})
        payload = type(record.payload)(**payload_kwargs(record.payload))
        rebuilt = ev.EvidenceRecord(**{**record_kwargs(record), "subject": subject_obj, "payload": payload})
        assert rebuilt == record
    assert ev.EvidenceFile(parsed.schema_version, parsed.records) == parsed


# =============================================================================================================
# R3-01: vierstellige Jahresdarstellung (strftime("%Y") ist unter Linux fuer Jahre < 1000 nicht nullgefuellt)
# =============================================================================================================

EARLY_YEARS = [1, 99, 999, 1000]


@pytest.mark.parametrize("year", EARLY_YEARS)
def test_format_utc_always_uses_four_digit_years(year):
    moment = datetime(year, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    text = ev.format_utc(moment)
    assert text == f"{year:04d}-01-02T03:04:05Z"
    assert ev.parse_utc_timestamp(text) == moment
    micro = datetime(year, 12, 31, 23, 59, 59, 250000, tzinfo=timezone.utc)
    assert ev.format_utc(micro) == f"{year:04d}-12-31T23:59:59.250000Z"
    assert ev.parse_utc_timestamp(ev.format_utc(micro)) == micro


def test_format_utc_supported_formats_are_unchanged():
    assert ev.format_utc(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00Z"
    assert ev.format_utc(datetime(2026, 5, 6, 7, 8, 9, 250000, tzinfo=timezone.utc)) == "2026-05-06T07:08:09.250000Z"
    assert ev.format_utc(datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)) == "9999-12-31T23:59:59.999999Z"


@pytest.mark.parametrize("year", EARLY_YEARS)
@pytest.mark.parametrize("spelling", ["Z", "+00:00"])
def test_early_years_survive_seal_parse_to_dict_parse_with_stable_fingerprint(year, spelling):
    created = f"{year:04d}-01-01T00:00:00{spelling}"
    until = "2026-12-31T00:00:00Z"
    reference = sealed(with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc=f"{year:04d}-01-01T00:00:00Z", valid_from_utc=f"{year:04d}-01-01T00:00:00Z", valid_until_utc=until))
    raw = sealed(with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc=created, valid_from_utc=created, valid_until_utc=until))
    assert raw["fingerprint"] == reference["fingerprint"]
    first = ev.parse_evidence_file(file_of(raw))
    assert first.ok, [r.message for r in first.reasons]
    dumped = first.value.to_dict()
    assert dumped["records"][0]["created_utc"] == f"{year:04d}-01-01T00:00:00Z"
    second = ev.parse_evidence_file(dumped)
    assert second.ok, [r.message for r in second.reasons]
    assert second.value == first.value
    assert second.value.records[0].fingerprint == raw["fingerprint"]
    third = ev.parse_evidence_file(second.value.to_dict())
    assert third.ok and third.value == first.value


@pytest.mark.parametrize("year", EARLY_YEARS)
@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_every_kind_with_early_year_timestamps_roundtrips(kind, year):
    stamp = f"{year:04d}-02-03T04:05:06.5Z"
    until = "2026-12-31T00:00:00Z"
    raw = raw_record(kind)
    raw["created_utc"] = raw["valid_from_utc"] = stamp
    raw["valid_until_utc"] = until
    if kind is EvidenceKind.RELEASE:
        raw["payload"]["granted_at"] = stamp
        base = make(EvidenceKind.WATER_ONLY_TEST)
        raw["references"] = [base["fingerprint"]]
        records = [sealed(raw), base]
    else:
        records = [sealed(raw)]
    first = ev.parse_evidence_file(file_of(*records))
    assert first.ok, [r.message for r in first.reasons]
    second = ev.parse_evidence_file(first.value.to_dict())
    assert second.ok and second.value == first.value
    assert all(a.fingerprint == b.fingerprint for a, b in zip(first.value.records, second.value.records))


@pytest.mark.parametrize("year", EARLY_YEARS)
def test_early_year_content_changes_are_still_detected(year):
    created = f"{year:04d}-01-01T00:00:00Z"
    tampered = sealed(with_fields(EvidenceKind.RUNTIME_OBSERVATION, created_utc=created, valid_from_utc=created))
    tampered["created_utc"] = f"{year:04d}-01-01T00:00:01Z"
    assert BlockCode.EVIDENCE_FINGERPRINT_MISMATCH in codes(ev.parse_evidence_file(file_of(tampered)))


@pytest.mark.parametrize("year", EARLY_YEARS)
def test_release_granted_at_with_early_year_roundtrips(year):
    stamp = f"{year:04d}-07-08T09:10:11Z"
    base = make(EvidenceKind.WATER_ONLY_TEST)
    raw = raw_record(EvidenceKind.RELEASE, refs=[base["fingerprint"]])
    raw["payload"]["granted_at"] = stamp
    first = ev.parse_evidence_file(file_of(sealed(raw), base))
    assert first.ok
    release = first.value.records_of_kind(EvidenceKind.RELEASE)[0]
    assert release.payload.to_dict()["granted_at"] == stamp
    assert ev.parse_evidence_file(first.value.to_dict()).ok


def test_unpadded_years_are_still_rejected_as_input():
    for bad in ("1-01-01T00:00:00Z", "99-01-01T00:00:00Z", "999-01-01T00:00:00Z", "00001-01-01T00:00:00Z"):
        with pytest.raises(ValueError):
            ev.parse_utc_timestamp(bad)
