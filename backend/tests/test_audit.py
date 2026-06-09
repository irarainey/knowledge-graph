"""Unit tests for the per-request audit trail."""

from __future__ import annotations

from authz import Principal
from common.audit import (
    OUTCOME_ANSWERED,
    OUTCOME_ERROR,
    OUTCOME_REFUSED_OFF_TOPIC,
    build_audit,
    log_audit,
    schema_fingerprint,
)


def _principal() -> Principal:
    return Principal(
        id="maintenance_engineer",
        displayName="Maintenance Engineer",
        role="maintenance",
        clearance="official",
        clearanceRank=1,
        policyVersion="2026-01-01",
    )


def test_schema_fingerprint_is_stable_and_short() -> None:
    a = schema_fingerprint("(:Flight)-[:HAS]->(:System)")
    b = schema_fingerprint("(:Flight)-[:HAS]->(:System)")
    c = schema_fingerprint("(:Flight)-[:HAS]->(:Other)")
    assert a == b
    assert a != c
    assert len(a) == 12


def test_build_audit_populates_principal_fields() -> None:
    record = build_audit(
        principal=_principal(),
        question="How many flights?",
        outcome=OUTCOME_ANSWERED,
        schema_fingerprint="abc123",
        cypher=["MATCH (f:Flight) RETURN count(f)"],
        record_count=1,
        llm_calls=3,
        denied=[],
        duration_ms=42.0,
    )
    assert record.outcome == OUTCOME_ANSWERED
    assert record.user == "maintenance_engineer"
    assert record.role == "maintenance"
    assert record.clearance == "official"
    assert record.policyVersion == "2026-01-01"
    assert record.schemaFingerprint == "abc123"
    assert record.recordCount == 1
    assert record.llmCalls == 3
    assert record.cypher == ["MATCH (f:Flight) RETURN count(f)"]
    assert record.timestamp.endswith("+00:00") or "T" in record.timestamp


def test_build_audit_without_principal_defaults_to_none() -> None:
    record = build_audit(
        principal=None,
        question="anything",
        outcome=OUTCOME_REFUSED_OFF_TOPIC,
        schema_fingerprint=None,
        cypher=[],
        record_count=0,
        llm_calls=0,
        denied=["Disallowed Cypher construct: procedure call (CALL)."],
        duration_ms=1.0,
    )
    assert record.user is None
    assert record.role is None
    assert record.clearance is None
    assert record.policyVersion is None
    assert record.denied == ["Disallowed Cypher construct: procedure call (CALL)."]
    assert record.outcome == OUTCOME_REFUSED_OFF_TOPIC


def test_log_audit_returns_dict() -> None:
    record = build_audit(
        principal=_principal(),
        question="q",
        outcome=OUTCOME_ERROR,
        schema_fingerprint="fp",
        cypher=[],
        record_count=0,
        llm_calls=0,
        denied=[],
        duration_ms=0.0,
    )
    payload = log_audit(record)
    assert payload["outcome"] == OUTCOME_ERROR
    assert payload["user"] == "maintenance_engineer"
    assert payload["schemaFingerprint"] == "fp"
